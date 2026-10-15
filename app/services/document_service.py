"""
文档业务编排。

阶段 3：list_documents（不加缓存）。
阶段 5：ingest() —— 标准化 → 判重(快路径) → 三表落库。
阶段 6：给 ingest 的临界区套上按 hash 粒度的分布式锁。
阶段 7：列表与单篇都走 Cache-Aside；写入成功后 bump 列表版本号。
阶段 9：service 不再抛 HTTPException（改为业务异常，HTTP 映射在 core/exceptions.py）；
        补处理日志；插入 AI 接缝 enrich（失败不阻断入库）。
阶段 B：落库后接上【切块】；新增分块查询（纯 TTL 缓存）。
阶段 D：重复改为「不是错误」（返回已存在那篇）；新增批量摄入 bulk_ingest（pipeline 判重）。
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
from app.schemas.chunk import ChunkOut
from app.schemas.document import BulkItemResult, BulkResult, DocumentCreate, DocumentOut
from app.services.cache_service import (
    chunk_list_cache_key,
    document_cache_key,
    bump_list_version,
    cache_get,
    cache_set,
    get_list_version,
    list_cache_key,
    stats_cache_key,
)
from app.services.chunk_service import make_chunks
from app.services.normalize_service import normalize_document
from app.services.dedup_service import (
    add_rejected,
    check_and_mark,
    check_and_mark_bulk,
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

        注意：本项目文档写入后永不修改，所以单条缓存用纯 TTL 就够，
        不需要「写时删」—— 失效策略取决于数据可变性，可变数据才需要写时失效。
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

    async def list_chunks(self, document_id: int, *, page: int, limit: int) -> tuple[list[dict], int]:
        """
        分块列表（Cache-Aside，纯 TTL）。

        为什么不像列表/统计那样用版本号：chunk 是 **一次写入、永不修改** 的 ——
        没有「写后失效」这回事，TTL 自然过期就够了。
        这正是「失效策略取决于数据可变性」的正面例子。
        """
        cache_key = chunk_list_cache_key(document_id, page=page, limit=limit)
        cached = await cache_get(self.redis, cache_key)
        if cached is not None:
            return cached["items"], cached["total"]

        rows = await self.repo.list_chunks(document_id, page=page, limit=limit)
        total = await self.repo.count_chunks(document_id)
        # 空结果时再确认文档是否存在 —— 免得把「id 打错了」显示成「文档存在但没有块」。
        # 只在空结果时多查一次，常见路径不吃这个开销。
        if total == 0 and await self.repo.get_by_id(document_id) is None:
            raise DocumentNotFoundError()

        payload = {
            "items": [ChunkOut.model_validate(r).model_dump(mode="json") for r in rows],
            "total": total,
        }
        await cache_set(self.redis, cache_key, payload, ttl=settings.cache_ttl)
        return payload["items"], payload["total"]

    # ---------- 写 ----------

    async def _persist(self, payload: DocumentCreate, raw: dict, cleaned: dict, ai: dict) -> DocumentClean:
        """
        三表顺序落库：raw → clean → dedup（顺带切块）， **只 flush、不 commit** 。

        单篇与批量都要走这一段，但事务边界各自由调用方决定（批量是每篇一个事务），
        所以提交不放在这里。
        """
        # payload 用 mode="json" —— datetime 得变成字符串，JSON 列才存得进去；
        # 而 doc_updated_at 那列要的是真 datetime，所以两个 dump 各取所需
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
        # 切块：文档落库后立刻切。chunk 与文档在【同一个事务】里 ——
        # 要么文档和它的块都在，要么都不在，不留半截数据。
        chunks = make_chunks(cleaned)
        await self.repo.insert_chunks(clean_row.id, chunks)

        await self.dedup_repo.insert_dedup(cleaned["content_hash"], clean_row.id)
        logger.info("落库完成 doc_id=%s 块数=%d", clean_row.id, len(chunks))
        return clean_row

    async def _find_existing(self, content_hash: str) -> DocumentClean | None:
        """按内容指纹回读「已存在」的那篇文档（重复时接口层要把它回给调用方）。"""
        dedup = await self.dedup_repo.get_by_hash(content_hash)
        if dedup is None:
            return None
        return await self.repo.get_by_id(dedup.clean_id)

    async def _require_existing(self, content_hash: str) -> DocumentClean:
        """
        回读「已存在」那篇；读不到说明是 **不一致态** （Redis 有占位、库里却没记录），
        罕见且自愈，仍按 409 处理。
        """
        existing = await self._find_existing(content_hash)
        if existing is None:
            logger.warning("Redis 有判重占位但库里查不到，属不一致态 hash=%s", content_hash[:12])
            raise DuplicateDocumentError()
        return existing

    async def ingest(self, payload: DocumentCreate) -> tuple[DocumentClean, bool]:
        """
        整条管道：标准化 → 加锁 → 判重 → 三表落库 → 提交 → bump 列表版本号。

        返回 (文档, 是否新建)。 **【重复不是错误】** —— 同步器周期性重推是正常行为，
        所以内容已存在时返回【已存在那篇】并置 False，由接口层回 200；
        只有抢不到锁（同一篇正在被处理）才 409。
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
                # ① 快路径：Redis 原子判重。命中不是错误 → 回已存在那篇
                if not await check_and_mark(self.redis, content_hash):
                    await incr_rejected(self.redis)          # 统计「挡掉了多少重复同步」
                    logger.info("判重（Redis 快路径）→ 已存在 hash=%s", content_hash[:12])
                    return await self._require_existing(content_hash), False

                try:
                    # ---- AI 接缝：判重之后、落库之前 ----
                    # enrich 内部已兜住所有异常，失败返回三列 None，不会阻断入库
                    ai = await enrich(cleaned)
                    clean_row = await self._persist(payload, raw, cleaned, ai)
                    # 一次 commit 落定整笔
                    await self.db.commit()
                except IntegrityError:
                    # 权威兜底：Redis 被清空，或锁 TTL 已过、有别人冲进来 → 唯一索引拦下。
                    # 【必须 rollback】：不回滚，session 就废了，后续任何操作都会报错。
                    await self.db.rollback()
                    await rollback_mark(self.redis, content_hash)   # 撤掉快路径的错误占位
                    await incr_rejected(self.redis)                 # 唯一索引拦下的也算拒收
                    logger.info("判重（唯一索引兜底）→ 已存在 hash=%s", content_hash[:12])
                    return await self._require_existing(content_hash), False
        except LockNotAcquired:
            # 已有别的请求在跑同一篇：报 409，让调用方稍后重试
            logger.info("未拿到锁（同篇正在处理）hash=%s", content_hash[:12])
            raise DocumentInProgressError() from None

        # 有新数据了：版本号 +1，所有旧列表缓存瞬间失效
        # （单条缓存不用管：文档写入后不会改，TTL 自然过期即可）
        await bump_list_version(self.redis)
        logger.info("ingest 成功 hash=%s clean_id=%s", content_hash[:12], clean_row.id)
        return clean_row, True

    async def bulk_ingest(self, payloads: list[DocumentCreate]) -> BulkResult:
        """
        批量摄入（同步器的真实调用方式）。与单篇有三点不同，都是为了「一次推很多篇」：

        1. **判重走 pipeline** ：N 次 SADD 压成 **一次往返** （单篇那套是每篇一次）
        2. **不加分布式锁** ：一次加锁 ≈ 2 次往返/篇，逐篇加锁会把 pipeline 省下的
           往返又加回去；而 pipeline 的 SADD **本身就是原子的预留** （只有一个人拿到
           1），唯一索引仍是最终兜底
        3. 重复 **按条跳过** 、不报错：同步器周期重推是正常行为
        """
        total = len(payloads)
        items: list[BulkItemResult | None] = [None] * total

        # ① 先全部标准化（纯函数、便宜），顺带挑出空正文的
        prepared: list[tuple[int, DocumentCreate, dict, dict]] = []   # (index, payload, raw, cleaned)
        # TODO: enumerate
        #   遍历可迭代对象时，同时拿到「下标」和「元素」
        for i, payload in enumerate(payloads):
            raw = payload.model_dump()
            cleaned = normalize_document(raw)
            if not cleaned["content"]:
                items[i] = BulkItemResult(index=i, status="rejected")
                continue
            prepared.append((i, payload, raw, cleaned))

        # ② 一次 pipeline 预留所有 hash —— N 次往返压成 1 次（本段的核心演示点）
        flags = await check_and_mark_bulk(
            self.redis, [c["content_hash"] for _, _, _, c in prepared]
        )

        created = duplicates = 0
        # TODO: zip 把多个可迭代对象"按位置配对"，每次循环各取一个
        for (i, payload, raw, cleaned), is_new in zip(prepared, flags):
            content_hash = cleaned["content_hash"]

            # ③ pipeline 说「已有」→ 直接跳过，【连数据库都不碰】
            #    （省下的就是这一趟往返 + 后续重新切块、向量化的钱）
            if not is_new:
                duplicates += 1
                existing = await self._find_existing(content_hash)
                items[i] = BulkItemResult(
                    index=i, status="duplicate", id=existing.id if existing else None
                )
                continue

            try:
                ai = await enrich(cleaned)
                clean_row = await self._persist(payload, raw, cleaned, ai)
                await self.db.commit()                        # 每篇一个事务
            except IntegrityError:
                # 权威兜底：Redis 被清空 → 唯一索引拦下
                await self.db.rollback()
                await rollback_mark(self.redis, content_hash)
                duplicates += 1
                existing = await self._find_existing(content_hash)
                items[i] = BulkItemResult(
                    index=i, status="duplicate", id=existing.id if existing else None
                )
            except Exception:
                # 其它任何失败也必须撤掉占位：这个 hash 是本批【预先预留】的，
                # 不 SREM 就会永久留在 Set 里、从此把这篇文档误判成重复
                # —— 这是「先预留、后落库」独有的坑。
                await self.db.rollback()
                await rollback_mark(self.redis, content_hash)
                logger.exception("批量落库失败 index=%d hash=%s", i, content_hash[:12])
                items[i] = BulkItemResult(index=i, status="rejected")
            else:
                created += 1
                items[i] = BulkItemResult(index=i, status="created", id=clean_row.id)

        if duplicates:
            await add_rejected(self.redis, duplicates)         # 一次性 +n，而不是 N 次 incr
        if created:
            await bump_list_version(self.redis)
        logger.info("bulk_ingest 完成 收到=%d 新建=%d 重复=%d", total, created, duplicates)
        return BulkResult(
            received=total,
            created=created,
            duplicates=duplicates,
            rejected=total - created - duplicates,
            items=[it for it in items if it is not None],
        )
