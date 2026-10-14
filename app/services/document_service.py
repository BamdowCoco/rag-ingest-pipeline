"""
文档业务编排。

阶段 3：list_documents（不加缓存）。
阶段 5：ingest() —— 标准化 → 判重(快路径) → 三表落库。
阶段 6：给 ingest 的临界区套上按 hash 粒度的分布式锁。
阶段 7：列表与单篇都走 Cache-Aside；写入成功后 bump 列表版本号。
阶段 9：service 不再抛 HTTPException（改为业务异常，HTTP 映射在 core/exceptions.py）；
        补处理日志；插入 AI 接缝 enrich（失败不阻断入库）。
"""
import logging

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    DocumentInProgressError,
    DocumentNotFoundError,
    DuplicateDocumentError,
    EmptyContentError,
)
from app.models.document_clean import DocumentClean
from app.repositories.document_repo import DocumentRepo
from app.repositories.dedup_repo import DedupRepo
from app.schemas.document import DocumentCreate, DocumentOut
from app.services.cache_service import (
    document_cache_key,
    bump_list_version,
    cache_get,
    cache_set,
    get_list_version,
    list_cache_key,
    stats_cache_key,
)
from app.services.normalize_service import normalize_document
from app.services.dedup_service import (
    check_and_mark,
    get_rejected_count,
    incr_rejected,
    rollback_mark,
)
from app.services.enrich_service import enrich
from app.services.lock_service import LockNotAcquired, document_lock
from app.utils.time import beijing_today_utc_range

logger = logging.getLogger(__name__)


class DocumentService:
    def __init__(self, db: AsyncSession, redis: Redis) -> None:
        self.db = db
        self.redis = redis          # 快路径判重、分布式锁、缓存/版本号
        self.repo = DocumentRepo(db)
        self.dedup_repo = DedupRepo(db)

    # ---------- 读 ----------

    async def list_documents(
        self, *, page: int, limit: int, source: str | None = None, q: str | None = None
    ) -> tuple[list[dict], int]:
        """
        列表查询（Cache-Aside）。返回 (本页数据 dict 列表, 满足条件的总数)。

        带 `q` 的请求【不进缓存】：q 是自由文本、键空间无上限（能被刷爆），
        且命中率极低 —— 直接查库反而更划算。
        """
        cache_key: str | None = None
        if q is None:
            version = await get_list_version(self.redis)
            cache_key = list_cache_key(version, page=page, limit=limit, source=source)
            cached = await cache_get(self.redis, cache_key)
            if cached is not None:
                return cached["items"], cached["total"]

        items = await self.repo.list_documents(page=page, limit=limit, source=source, q=q)
        total = await self.repo.count_documents(source=source, q=q)
        # 转成 JSON 友好的 dict（mode="json" 让 datetime 变字符串），缓存和返回都用它
        payload = {
            "items": [DocumentOut.model_validate(o).model_dump(mode="json") for o in items],
            "total": total,
        }
        if cache_key is not None:
            await cache_set(self.redis, cache_key, payload, ttl=settings.cache_ttl)
        return payload["items"], payload["total"]

    async def get_document(self, document_id: int) -> DocumentOut:
        """
        单篇查询（Cache-Aside）。

        注意：本项目文档**写入后永不修改**，所以单条缓存用纯 TTL 就够，
        不需要「写时删」—— 失效策略取决于**数据可变性**，可变数据才需要写时失效。
        """
        cache_key = document_cache_key(document_id)
        cached = await cache_get(self.redis, cache_key)
        if cached is not None:
            return DocumentOut.model_validate(cached)

        obj = await self.repo.get_by_id(document_id)
        if obj is None:
            logger.info("文档不存在 id=%s", document_id)
            raise DocumentNotFoundError()

        out = DocumentOut.model_validate(obj)
        await cache_set(self.redis, cache_key, out.model_dump(mode="json"), ttl=settings.cache_ttl)
        return out

    async def get_stats(self) -> dict:
        """
        统计：总数 / 按来源 top10 / 今日新增 / 判重拒收累计。

        整体缓存，与列表共用同一个版本号 —— 一次 INCR 同时作废两者。
        「今日」按【北京时间】算（库里存的是 UTC，所以这里做一次边界换算）。
        """
        version = await get_list_version(self.redis)
        cache_key = stats_cache_key(version)
        cached = await cache_get(self.redis, cache_key)
        if cached is not None:
            return cached

        today_start, today_end = beijing_today_utc_range()
        by_source = await self.repo.count_by_source(limit=10)
        payload = {
            "total": await self.repo.count_documents(),
            "by_source": [{"source": s, "count": n} for s, n in by_source],
            "today_count": await self.repo.count_since(today_start, today_end),
            "dedup_rejected": await get_rejected_count(self.redis),
        }
        await cache_set(self.redis, cache_key, payload, ttl=settings.cache_ttl)
        return payload

    # ---------- 写 ----------

    async def ingest(self, payload: DocumentCreate) -> DocumentClean:
        """
        整条管道：标准化 → 加锁 → 判重 → 三表落库 → 提交 → bump 列表版本号。
        重复、或抢不到锁（同一篇正在被处理）都抛 409。
        """
        raw = payload.model_dump()
        cleaned = normalize_document(raw)
        content_hash = cleaned["content_hash"]
        logger.info("ingest 开始 source=%s hash=%s", raw["source"], content_hash[:12])

        # 标准化后正文为空 → 拒收。放锁外做，省一次加锁（反正它不涉及竞态）。
        if not cleaned["content"]:
            logger.info("拒收：标准化后正文为空 hash=%s", content_hash[:12])
            raise EmptyContentError()

        try:
            # ---------- 临界区：只有「判重 → 落库」需要互斥 ----------
            # 锁按 content_hash 粒度：不同文档互不阻塞，同一篇才串行
            async with document_lock(self.redis, content_hash):
                # ① 快路径：Redis 原子判重
                if not await check_and_mark(self.redis, content_hash):
                    await incr_rejected(self.redis)
                    logger.info("拒收：判重（Redis 快路径）hash=%s", content_hash[:12])
                    raise DuplicateDocumentError()

                try:
                    # ---- AI 接缝：判重之后、落库之前 ----
                    # enrich 内部已兜住所有异常，失败返回三列 None，不会阻断入库
                    ai = await enrich(cleaned)

                    # ② 三表顺序落库：raw → clean → dedup（对应三表管道）
                    #    payload 用 mode="json" —— datetime 得变成字符串，JSON 列才存得进去；
                    #    而 doc_updated_at 那列要的是真 datetime，所以两个 dump 各取所需
                    raw_row = await self.repo.insert_raw(
                        source=raw["source"],
                        raw_title=raw["title"],
                        raw_content=raw["content"],
                        payload=payload.model_dump(mode="json"),
                    )
                    clean_row = await self.repo.insert_clean(
                        raw_id=raw_row.id,
                        source=raw["source"],
                        title=cleaned["title"],
                        content=cleaned["content"],
                        author=raw.get("author"),
                        url=raw.get("url"),
                        doc_updated_at=raw.get("doc_updated_at"),
                        char_count=cleaned["char_count"],
                        clean_version=cleaned["clean_version"],
                        **ai,                                  # doc_type / tags / ai_summary
                    )
                    await self.dedup_repo.insert_dedup(content_hash, clean_row.id)

                    # ③ 一次 commit 落定整笔
                    await self.db.commit()
                except IntegrityError:
                    # 权威兜底：Redis 被清空，或锁 TTL 已过、有别人冲进来 → 唯一索引拦下。
                    # 【必须 rollback】：不回滚，session 就废了，后续任何操作都会报错。
                    await self.db.rollback()
                    await rollback_mark(self.redis, content_hash)   # 撤掉快路径的错误占位
                    await incr_rejected(self.redis)                 # 唯一索引拦下的也算拒收
                    logger.warning("拒收：判重（唯一索引兜底）hash=%s", content_hash[:12])
                    raise DuplicateDocumentError() from None
        except LockNotAcquired:
            # 已有别的请求在跑同一篇：立刻报 409，让调用方稍后重试
            logger.info("未拿到锁（同篇正在处理）hash=%s", content_hash[:12])
            raise DocumentInProgressError() from None

        # ④ 有新数据了：版本号 +1，所有旧列表缓存瞬间失效
        #    （单条缓存不用管：文档写入后不会改，TTL 自然过期即可）
        await bump_list_version(self.redis)
        logger.info(
            "ingest 成功 hash=%s clean_id=%s 字数=%s",
            content_hash[:12], clean_row.id, cleaned["char_count"],
        )
        return clean_row
