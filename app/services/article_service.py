"""
文章业务编排。

阶段 3：只有 list_articles（不加缓存）。
阶段 5 会加 ingest()（清洗→去重→入库），阶段 7 给列表加 Cache-Aside。
redis 先收进来存着，阶段 7 直接用。
"""
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_clean import ArticleClean
from app.repositories.article_repo import ArticleRepo


class ArticleService:
    def __init__(self, db: AsyncSession, redis: Redis) -> None:
        self.db = db
        self.redis = redis          # 阶段 7 才用（缓存/版本号），先接上让依赖链一次成型
        self.repo = ArticleRepo(db)

    async def list_articles(
        self, *, page: int, limit: int, source: str | None = None, q: str | None = None
    ) -> tuple[list[ArticleClean], int]:
        """返回 (本页数据, 满足条件的总数)。"""
        items = await self.repo.list_articles(page=page, limit=limit, source=source, q=q)
        total = await self.repo.count_articles(source=source, q=q)
        return items, total
