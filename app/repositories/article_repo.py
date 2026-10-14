"""
文章仓储：对 article_clean 的读写集中在这里，只写「查询」，不含业务判断。
"""
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_clean import ArticleClean
from app.models.article_raw import ArticleRaw


class ArticleRepo:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    @staticmethod
    def _filters(stmt: Select, *, source: str | None, q: str | None) -> Select:
        if source:
            stmt = stmt.where(ArticleClean.source == source)
        if q:
            # ilike 在 MySQL 上就是 LIKE + 列的排序规则（utf8mb4_unicode_ci 本身不区分大小写）
            stmt = stmt.where(ArticleClean.title.ilike(f"%{q}%"))
        return stmt

    async def list_articles(
        self, *, page: int, limit: int, source: str | None = None, q: str | None = None
    ) -> list[ArticleClean]:
        """
            SELECT * FROM article_clean as ac
            WHERE ...
            ORDER BY ac.fetched_at desc, ac.id desc
            OFFSET ...
            LIMIT ...
        """
        stmt = select(ArticleClean)
        stmt = self._filters(stmt, source=source, q=q)
        # 排序要稳定：fetched_at 可能重复，补一个 id 兜底
        stmt = stmt.order_by(ArticleClean.fetched_at.desc(), ArticleClean.id.desc())
        stmt = stmt.offset((page - 1) * limit).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_articles(self, *, source: str | None = None, q: str | None = None) -> int:
        """
            SELECT COUNT(*) FROM article_clean
            WHERE ...
        """
        stmt = select(func.count()).select_from(ArticleClean)
        stmt = self._filters(stmt, source=source, q=q)
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    async def get_by_id(self, article_id: int) -> ArticleClean | None:
        """按主键取单篇（阶段 7 的单篇查询用）。"""
        stmt = select(ArticleClean).where(ArticleClean.id == article_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    # ---------- 聚合（阶段 8） ----------

    async def count_by_source(self, limit: int = 10) -> list[tuple[str, int]]:
        """按来源分组取 top N（条数降序）。"""
        stmt = (
            select(ArticleClean.source, func.count().label("n"))
            .group_by(ArticleClean.source)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [(row.source, row.n) for row in result.all()]

    async def count_since(self, start: datetime, end: datetime) -> int:
        """
        区间计数。

        必须是**范围写法**：`fetched_at >= start AND fetched_at <= end` —— 这样能吃到
        `fetched_at` 上的索引。写成 `func.date(fetched_at) == ...` 会对列做函数运算，
        **索引直接失效**（且 MySQL 没有 `date_trunc`，那是 Postgres 的）。
        """
        stmt = (
            select(func.count())
            .select_from(ArticleClean)
            .where(ArticleClean.fetched_at >= start, ArticleClean.fetched_at <= end)
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    # ---------- 写入（阶段 5） ----------

    async def insert_raw(
        self, *, source: str, raw_title: str, raw_content: str, payload: dict | None = None
    ) -> ArticleRaw:
        row = ArticleRaw(
            source=source, raw_title=raw_title, raw_content=raw_content, payload=payload
        )
        self.db.add(row)
        await self.db.flush()   # flush 只为拿到自增 id 给 clean.raw_id 用，≠ commit
        return row

    async def insert_clean(
        self,
        *,
        raw_id: int,
        source: str,
        title: str,
        content: str,
        author: str | None,
        url: str | None,
        published_at: datetime | None,
        char_count: int,
        clean_version: int,
        sentiment: str | None = None,
        category: str | None = None,
        ai_summary: str | None = None,
    ) -> ArticleClean:
        row = ArticleClean(
            raw_id=raw_id,
            source=source,
            title=title,
            content=content,
            author=author,
            url=url,
            published_at=published_at,
            char_count=char_count,
            clean_version=clean_version,
            sentiment=sentiment,
            category=category,
            ai_summary=ai_summary,
        )
        self.db.add(row)
        await self.db.flush()   # 同样只为拿 clean.id 给 dedup.clean_id 用
        return row
