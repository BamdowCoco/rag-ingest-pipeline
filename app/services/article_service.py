"""
文章业务编排。

阶段 3：list_articles（不加缓存）。
阶段 5：ingest() —— 清洗 → 判重(快路径) → 三表落库。
阶段 6：给 ingest 的临界区套上按 hash 粒度的分布式锁。
阶段 7：列表与单篇都走 Cache-Aside；写入成功后 bump 列表版本号。
"""
from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.article_clean import ArticleClean
from app.repositories.article_repo import ArticleRepo
from app.repositories.dedup_repo import DedupRepo
from app.schemas.article import ArticleCreate, ArticleOut
from app.services.cache_service import (
    article_cache_key,
    bump_list_version,
    cache_get,
    cache_set,
    get_list_version,
    list_cache_key,
)
from app.services.clean_service import clean_article
from app.services.dedup_service import check_and_mark, rollback_mark
from app.services.lock_service import LockNotAcquired, article_lock


class ArticleService:
    def __init__(self, db: AsyncSession, redis: Redis) -> None:
        self.db = db
        self.redis = redis          # 快路径判重、分布式锁、缓存/版本号
        self.repo = ArticleRepo(db)
        self.dedup_repo = DedupRepo(db)

    # ---------- 读 ----------

    async def list_articles(
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

        items = await self.repo.list_articles(page=page, limit=limit, source=source, q=q)
        total = await self.repo.count_articles(source=source, q=q)
        # 转成 JSON 友好的 dict（mode="json" 让 datetime 变字符串），缓存和返回都用它
        payload = {
            "items": [ArticleOut.model_validate(o).model_dump(mode="json") for o in items],
            "total": total,
        }
        if cache_key is not None:
            await cache_set(self.redis, cache_key, payload, ttl=settings.cache_ttl)
        return payload["items"], payload["total"]

    async def get_article(self, article_id: int) -> ArticleOut:
        """
        单篇查询（Cache-Aside）。

        注意：本项目文章**写入后永不修改**，所以单条缓存用纯 TTL 就够，
        不需要「写时删」—— 失效策略取决于**数据可变性**，可变数据才需要写时失效。
        """
        cache_key = article_cache_key(article_id)
        cached = await cache_get(self.redis, cache_key)
        if cached is not None:
            return ArticleOut.model_validate(cached)

        obj = await self.repo.get_by_id(article_id)
        if obj is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="文章不存在")

        out = ArticleOut.model_validate(obj)
        await cache_set(self.redis, cache_key, out.model_dump(mode="json"), ttl=settings.cache_ttl)
        return out

    # ---------- 写 ----------

    async def ingest(self, payload: ArticleCreate) -> ArticleClean:
        """
        整条管道：清洗 → 加锁 → 判重 → 三表落库 → 提交 → bump 列表版本号。
        重复、或抢不到锁（同一篇正在被处理）都抛 409。
        """
        raw = payload.model_dump()
        cleaned = clean_article(raw)
        content_hash = cleaned["content_hash"]

        # 清洗后正文为空 → 拒收。放锁外做，省一次加锁（反正它不涉及竞态）。
        if not cleaned["content"]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="清洗后正文为空")

        try:
            # ---------- 临界区：只有「判重 → 落库」需要互斥 ----------
            # 锁按 content_hash 粒度：不同文章互不阻塞，同一篇才串行
            async with article_lock(self.redis, content_hash):
                # ① 快路径：Redis 原子判重
                if not await check_and_mark(self.redis, content_hash):
                    raise HTTPException(status.HTTP_409_CONFLICT, detail="文章已存在（判重拒收）")

                try:
                    # ② 三表顺序落库：raw → clean → dedup（对应三表管道）
                    #    payload 用 mode="json" —— datetime 得变成字符串，JSON 列才存得进去；
                    #    而 published_at 那列要的是真 datetime，所以两个 dump 各取所需
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
                        published_at=raw.get("published_at"),
                        char_count=cleaned["char_count"],
                        clean_version=cleaned["clean_version"],
                    )
                    await self.dedup_repo.insert_dedup(content_hash, clean_row.id)

                    # ③ 一次 commit 落定整笔
                    await self.db.commit()
                except IntegrityError:
                    # 权威兜底：Redis 被清空，或锁 TTL 已过、有别人冲进来 → 唯一索引拦下。
                    # 【必须 rollback】：不回滚，session 就废了，后续任何操作都会报错。
                    await self.db.rollback()
                    await rollback_mark(self.redis, content_hash)   # 撤掉快路径的错误占位
                    raise HTTPException(
                        status.HTTP_409_CONFLICT, detail="文章已存在（唯一索引拦下）"
                    ) from None
        except LockNotAcquired:
            # 已有别的请求在跑同一篇：立刻回 409，让调用方稍后重试
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail="该文章正在处理中，请稍后重试"
            ) from None

        # ④ 有新数据了：版本号 +1，所有旧列表缓存瞬间失效
        #    （单条缓存不用管：文章写入后不会改，TTL 自然过期即可）
        await bump_list_version(self.redis)
        return clean_row
