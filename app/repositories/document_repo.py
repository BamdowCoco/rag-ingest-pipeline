"""
文档仓储：对 document_clean 的读写集中在这里，只写「查询」，不含业务判断。

阶段 F 起：document_clean **一行 = 一个版本** ，只有 ``superseded_at IS NULL`` 的那行是当前版本。
所以所有「面向用户」的查询（列表 / 统计）都必须过滤掉历史版本。
"""
from datetime import datetime

from sqlalchemy import Select, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_chunk import DocumentChunk
from app.models.document_clean import DocumentClean, EnrichStatus
from app.models.document_raw import DocumentRaw
from app.models.document_source import DocumentSource
from app.utils.time import utcnow


class DocumentRepo:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    @staticmethod
    def _filters(stmt: Select, *, source: str | None, q: str | None) -> Select:
        # 列表与总数都走这里 —— 历史版本一律不算「文档」
        stmt = stmt.where(DocumentClean.superseded_at.is_(None))
        if source:
            stmt = stmt.where(DocumentClean.source == source)
        if q:
            # 【这行注释 2026-10-17 改准了】旧注释说"ilike 在 MySQL 上就是 LIKE + 列的排序规则"，
            # 实测（SQLAlchemy 2.1 + MySQL 方言）它编译成 `lower(title) LIKE lower('%q%')` ——
            # 多包了一层 lower()。这里【故意不改写 like】：显式写出"大小写不敏感"，
            # 而不是偷偷依赖 title 列的 collation（哪天真跟着标识列改成 _bin，检索会静默变敏感）。
            # 真正让这条查询走不了索引的是前导 %（B-tree 对前缀通配符无能为力）。
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
        """按来源分组取 top N（条数降序）。只数当前版本。"""
        stmt = (
            select(DocumentClean.source, func.count().label("n"))
            .where(DocumentClean.superseded_at.is_(None))
            .group_by(DocumentClean.source)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [(row.source, row.n) for row in result.all()]

    async def count_since(self, start: datetime, end: datetime) -> int:
        """
        区间计数。

        必须是范围写法：``fetched_at >= start AND fetched_at <= end`` —— 这样能吃到
        ``fetched_at`` 上的索引。写成 ``func.date(fetched_at) == ...`` 会对列做函数运算，
        索引直接失效（且 MySQL 没有 ``date_trunc``，那是 Postgres 的）。
        """
        stmt = (
            select(func.count())
            .select_from(DocumentClean)
            .where(
                DocumentClean.fetched_at >= start,
                DocumentClean.fetched_at <= end,
                DocumentClean.superseded_at.is_(None),
            )
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    async def count_chunks_by_status(self) -> list[tuple[str, int]]:
        """
        按 embedding_status 分组计数 —— 就是看板上的「向量化积压」（pending 的那部分）。

        一次 GROUP BY 拿全，而不是发三次 COUNT；状态就三种，结果最多三行。

        **必须过滤已作废的块** ：作废时若它还停在 ``pending``，这辈子都不会被认领，
        却会被永远算成「积压」—— 积压数字就再也降不到 0 了。
        """
        stmt = (
            select(DocumentChunk.embedding_status, func.count().label("n"))
            .where(DocumentChunk.superseded_at.is_(None))
            .group_by(DocumentChunk.embedding_status)
        )
        result = await self.db.execute(stmt)
        return [(row.embedding_status, row.n) for row in result.all()]

    # ---------- 身份（阶段 F）----------

    async def get_source(
        self, source: str, external_id: str, *, for_update: bool = False
    ) -> DocumentSource | None:
        """
        取身份行。``for_update=True`` 加行锁 —— 这是更新语义的 **权威** ：
        把「读当前版本 → 写新版本」这段串行化。

        Redis 那把身份锁只负责「快速 409」这个体验优化；**真正保证不出现两个 active 版本的，
        是这把行锁** （尤其 ``bulk_ingest`` 根本就没拿 Redis 锁）。
        """
        stmt = select(DocumentSource).where(
            DocumentSource.source == source,
            DocumentSource.external_id == external_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def insert_source(self, source: str, external_id: str) -> DocumentSource:
        row = DocumentSource(source=source, external_id=external_id)
        self.db.add(row)
        await self.db.flush()        # flush 只为拿自增 id 给 clean.source_id 用，≠ commit
        return row

    async def get_active_by_id(self, clean_id: int) -> DocumentClean | None:
        """
        按 id 取 **当前版本**（已作废的返回 None）。

        快路径兜底用：缓存里的 clean_id 可能已被并发的更新作废。
        必须在 **SQL 层**过滤 ``superseded_at`` —— ``supersede_version`` 是 Core UPDATE，
        identity map 里的 ORM 对象可能仍是旧值，只看对象属性会漏判。
        """
        stmt = select(DocumentClean).where(
            DocumentClean.id == clean_id,
            DocumentClean.superseded_at.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_active_version(
        self, source_id: int, *, for_update: bool = False
    ) -> DocumentClean | None:
        """
        当前版本 = 该身份下没被作废的那一行。

        ``for_update=True`` 走 **当前读** —— 看到的是最新已提交的行，而不是事务快照。
        身份路径必须用它：``get_source(..., for_update=True)`` 是当前读，能看到并发刚提交的身份行；
        这里若用快照读，可能看不到同一时刻写入的版本行，于是误判成「身份已存在 + 新内容」。
        """
        stmt = (
            select(DocumentClean)
            .where(
                DocumentClean.source_id == source_id,
                DocumentClean.superseded_at.is_(None),
            )
            .order_by(DocumentClean.id.desc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def supersede_version(self, clean_id: int) -> None:
        """把某个版本标成「已不是当前版本」。不物理删 —— 历史版本留着可按 id 查。"""
        await self.db.execute(
            update(DocumentClean)
            .where(DocumentClean.id == clean_id)
            .values(superseded_at=utcnow())
        )

    # ---------- enrich 状态机（阶段 H）----------

    async def claim_pending_enrich(self, limit: int) -> list[DocumentClean]:
        """
        认领一批「待补 enrich」的文档，置为 processing 并记下认领时刻。

        和 ``chunk_repo.claim_pending`` 同一套路： **``SKIP LOCKED`` 让多实例各拿一批、互不重复**
        （所以 ``--scale api`` 时多个进程各跑一个 enricher 循环也不会重复调模型）。
        只认领 **当前版本** —— 历史版本没必要补。
        """
        stmt = (
            select(DocumentClean)
            .where(
                DocumentClean.enrich_status == EnrichStatus.PENDING,
                DocumentClean.superseded_at.is_(None),
            )
            .order_by(DocumentClean.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self.db.execute(stmt)).scalars().all())
        now = utcnow()
        for row in rows:
            row.enrich_status = EnrichStatus.PROCESSING
            row.enrich_claimed_at = now
        await self.db.commit()          # 提交才释放行锁
        return rows

    async def mark_enrich_done(self, clean_id: int, ai: dict) -> None:
        """
        写回 enrich 结果，并把状态推到 done。

        ``ai`` 里 **允许全是 NULL** （provider=off，或模型与规则都失败）—— 那就写 NULL，
        但状态照样推到 done：意思是" **试过了** "。
        不这么做它就会一直留在 pending、每轮被重新认领一次 → **无限重试** （毒丸）。
        """
        await self.db.execute(
            update(DocumentClean)
            .where(DocumentClean.id == clean_id)
            .values(enrich_status=EnrichStatus.DONE, **ai)
        )
        await self.db.commit()

    async def reclaim_stale_enrich(self, older_than: datetime) -> int:
        """把卡在 processing 太久（补它的进程挂了）的文档放回 pending，返回条数。"""
        stmt = (
            update(DocumentClean)
            .where(
                DocumentClean.enrich_status == EnrichStatus.PROCESSING,
                DocumentClean.enrich_claimed_at < older_than,
            )
            .values(enrich_status=EnrichStatus.PENDING, enrich_claimed_at=None)
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount or 0

    # ---------- 写入（阶段 5） ----------

    async def insert_raw(
        self,
        *,
        source: str,
        external_id: str | None,
        raw_title: str,
        raw_content: str,
    ) -> DocumentRaw:
        row = DocumentRaw(
            source=source,
            external_id=external_id,
            raw_title=raw_title,
            raw_content=raw_content,
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
        content_hash: str,
        source_id: int | None = None,
        external_id: str | None = None,
        doc_type: str | None = None,
        tags: list[str] | None = None,
        ai_summary: str | None = None,
        language: str | None = None,
        enrich_version: int | None = None,
    ) -> DocumentClean:
        row = DocumentClean(
            raw_id=raw_id,
            source=source,
            source_id=source_id,
            external_id=external_id,
            title=title,
            content=content,
            author=author,
            url=url,
            doc_updated_at=doc_updated_at,
            char_count=char_count,
            clean_version=clean_version,
            content_hash=content_hash,
            doc_type=doc_type,
            tags=tags,
            ai_summary=ai_summary,
            language=language,
            enrich_version=enrich_version,
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
