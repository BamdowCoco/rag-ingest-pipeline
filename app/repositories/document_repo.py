"""
文档仓储：对 document_clean 的读写集中在这里，只写「查询」，不含业务判断。
"""
from datetime import datetime

from sqlalchemy import Select, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_chunk import DocumentChunk
from app.models.document_clean import DocumentClean
from app.models.document_raw import DocumentRaw


class DocumentRepo:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    @staticmethod
    def _filters(stmt: Select, *, source: str | None, q: str | None) -> Select:
        if source:
            stmt = stmt.where(DocumentClean.source == source)
        if q:
            # ilike 在 MySQL 上就是 LIKE + 列的排序规则（utf8mb4_unicode_ci 本身不区分大小写）
            stmt = stmt.where(DocumentClean.title.ilike(f"%{q}%"))
        return stmt

    async def list_documents(
        self, *, page: int, limit: int, source: str | None = None, q: str | None = None
    ) -> list[DocumentClean]:
        """
            SELECT * FROM document_clean as ac
            WHERE ...
            ORDER BY ac.fetched_at desc, ac.id desc
            OFFSET ...
            LIMIT ...
        """
        stmt = select(DocumentClean)
        stmt = self._filters(stmt, source=source, q=q)
        # 排序要稳定：fetched_at 可能重复，补一个 id 兜底
        stmt = stmt.order_by(DocumentClean.fetched_at.desc(), DocumentClean.id.desc())
        stmt = stmt.offset((page - 1) * limit).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_documents(self, *, source: str | None = None, q: str | None = None) -> int:
        """
            SELECT COUNT(*) FROM document_clean
            WHERE ...
        """
        stmt = select(func.count()).select_from(DocumentClean)
        stmt = self._filters(stmt, source=source, q=q)
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    async def get_by_id(self, document_id: int) -> DocumentClean | None:
        """按主键取单篇（阶段 7 的单篇查询用）。"""
        stmt = select(DocumentClean).where(DocumentClean.id == document_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    # ---------- 聚合（阶段 8） ----------

    async def count_by_source(self, limit: int = 10) -> list[tuple[str, int]]:
        """按来源分组取 top N（条数降序）。"""
        stmt = (
            select(DocumentClean.source, func.count().label("n"))
            .group_by(DocumentClean.source)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [(row.source, row.n) for row in result.all()]

    async def count_since(self, start: datetime, end: datetime) -> int:
        """
        区间计数。

        必须是范围写法：`fetched_at >= start AND fetched_at <= end` —— 这样能吃到
        `fetched_at` 上的索引。写成 `func.date(fetched_at) == ...` 会对列做函数运算，
        索引直接失效（且 MySQL 没有 `date_trunc`，那是 Postgres 的）。
        """
        stmt = (
            select(func.count())
            .select_from(DocumentClean)
            .where(DocumentClean.fetched_at >= start, DocumentClean.fetched_at <= end)
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    async def count_chunks_by_status(self) -> list[tuple[str, int]]:
        """
        按 embedding_status 分组计数 —— 就是看板上的「向量化积压」（pending 的那部分）。

        一次 GROUP BY 拿全，而不是发三次 COUNT；状态就三种，结果最多三行。
        """
        stmt = (
            select(DocumentChunk.embedding_status, func.count().label("n"))
            .group_by(DocumentChunk.embedding_status)
        )
        result = await self.db.execute(stmt)
        return [(row.embedding_status, row.n) for row in result.all()]

    # ---------- 写入（阶段 5） ----------

    async def insert_raw(
        self, *, source: str, raw_title: str, raw_content: str, payload: dict | None = None
    ) -> DocumentRaw:
        row = DocumentRaw(
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
        doc_updated_at: datetime | None,
        char_count: int,
        clean_version: int,
        doc_type: str | None = None,
        tags: list[str] | None = None,
        ai_summary: str | None = None,
    ) -> DocumentClean:
        row = DocumentClean(
            raw_id=raw_id,
            source=source,
            title=title,
            content=content,
            author=author,
            url=url,
            doc_updated_at=doc_updated_at,
            char_count=char_count,
            clean_version=clean_version,
            doc_type=doc_type,
            tags=tags,
            ai_summary=ai_summary,
        )
        self.db.add(row)
        await self.db.flush()   # 同样只为拿 clean.id 给 dedup.clean_id 用
        return row

    # ---------- chunk（阶段 B） ----------

    async def insert_chunks(self, document_id: int, chunks: list[dict]) -> int:
        """
        批量插入 chunk，返回插入条数。

        与 raw/clean 那两条不同：这里不需要回读自增 id，所以可以是 **真批量** ——
        一次 executemany 往返，而不是 N 次 INSERT。
        """
        if not chunks:
            return 0
        await self.db.execute(
            insert(DocumentChunk),
            [{"document_id": document_id, **c} for c in chunks],
        )
        return len(chunks)

    async def list_chunks(self, document_id: int, *, page: int, limit: int) -> list[DocumentChunk]:
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)      # 按 chunk_index 有序，翻页才稳定
            .offset((page - 1) * limit)
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def count_chunks(self, document_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )
        return int((await self.db.execute(stmt)).scalar_one())
