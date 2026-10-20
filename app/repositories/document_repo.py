"""
文档仓储：对 document_clean 的读写集中在这里，只写「查询」，不含业务判断。

阶段 F 起：document_clean **一行 = 一个版本** ，只有 ``superseded_at IS NULL`` 的那行是当前版本。
所以所有「面向用户」的查询（列表 / 统计）都必须过滤掉历史版本。
"""
from datetime import datetime

from sqlalchemy import Select, func, insert, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_chunk import DocumentChunk
from app.models.document_clean import DocumentClean, EnrichStatus
from app.models.document_raw import DocumentRaw
from app.models.document_source import DocumentSource
from app.utils.time import utcnow


class DocumentRepo:
    def __init__(self, db: AsyncSession) -> None:
        """
        绑定请求级会话；所有方法都只读写这个会话，不自行开事务。

        :param db: 请求级异步会话（事务边界由 service 控制）。
        """
        self.db = db

    @staticmethod
    def _filters(stmt: Select, *, source: str | None, q: str | None) -> Select:
        """
        给列表 / 计数查询套上公共过滤条件。

        :param stmt: 已经 ``select`` 好的语句（``DocumentClean`` 或基于它的 count）。
        :param source: 按来源精确过滤；``None`` 不过滤。
        :param q: 标题关键词（``ilike``，大小写不敏感）；``None`` 不过滤。
        :return: 追加了「只保留当前版本 + 可选来源/关键词」条件的语句。
        """
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
        分页列出当前版本的文档，按 ``fetched_at`` 倒序（同时间用 id 兜底，保证翻页稳定）。

        :param page: 页码，从 1 开始。
        :param limit: 每页条数。
        :param source: 按来源过滤；``None`` 不过滤。
        :param q: 标题关键词；``None`` 不过滤。
        :return: 本页的 ``DocumentClean`` 行（已过滤历史版本）。
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
        统计满足过滤条件的当前版本总数（与列表同一套过滤条件）。

        :param source: 按来源过滤；``None`` 不过滤。
        :param q: 标题关键词；``None`` 不过滤。
        :return: 满足条件的文档数。
        """
        stmt = select(func.count()).select_from(DocumentClean)
        stmt = self._filters(stmt, source=source, q=q)
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    async def get_by_id(self, document_id: int) -> DocumentClean | None:
        """
        按主键取单篇（不判断是否为当前版本，由调用方按语义使用）。

        :param document_id: ``document_clean.id``。
        :return: 对应的行；不存在则为 ``None``。
        """
        stmt = select(DocumentClean).where(DocumentClean.id == document_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    # ---------- 聚合（阶段 8） ----------

    async def count_by_source(self, limit: int = 10) -> list[tuple[str, int]]:
        """
        按来源分组取 top N（条数降序），只统计当前版本。

        :param limit: 最多返回多少个来源。
        :return: ``[(source, count), ...]``，按 count 降序。
        """
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

        :param start: 区间起点（含）。
        :param end: 区间终点（含）。
        :return: 该区间内入库的当前版本数。
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

        :return: ``[(embedding_status, count), ...]``，已排除作废的块。
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

        :param source: 文档来源。
        :param external_id: 源系统里的稳定文档 id。
        :param for_update: 是否加行锁（当前读）；身份路径必须为 ``True``。
        :return: 身份行；不存在则为 ``None``。
        """
        stmt = select(DocumentSource).where(
            DocumentSource.source == source,
            DocumentSource.external_id == external_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def insert_source(self, source: str, external_id: str) -> DocumentSource:
        """
        新建身份行并 flush（拿到自增 id 给 ``document_clean.source_id`` 用）。

        :param source: 文档来源。
        :param external_id: 源系统里的稳定文档 id。
        :return: 新建的身份行（已 flush、未 commit）。
        """
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

        :param clean_id: ``document_clean.id``。
        :return: 该 id 对应的当前版本；已作废或不存在则为 ``None``。
        """
        stmt = select(DocumentClean).where(
            DocumentClean.id == clean_id,
            DocumentClean.superseded_at.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_sources_by_keys_for_update(
        self, pairs: list[tuple[str, str]]
    ) -> list[DocumentSource]:
        """
        一次取回本批所有身份行并加锁（多行写入的读阶段）。

        :param pairs: 本批文档的 ``(source, external_id)`` 组合；空列表直接返回空结果。
        :return: 命中的身份行。没命中的身份不会出现在结果里，由调用方按「新建」处理。
        """
        if not pairs:
            return []
        stmt = (
            select(DocumentSource)
            .where(tuple_(DocumentSource.source, DocumentSource.external_id).in_(pairs))
            .with_for_update()
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_active_versions_by_source_ids_for_update(
        self, source_ids: list[int]
    ) -> list[DocumentClean]:
        """
        一次取回这批身份下的全部当前版本并加锁。

        :param source_ids: 身份行 id 列表；空列表直接返回空结果。
        :return: 这些身份下 ``superseded_at IS NULL`` 的版本行。
        """
        if not source_ids:
            return []
        stmt = (
            select(DocumentClean)
            .where(
                DocumentClean.source_id.in_(source_ids),
                DocumentClean.superseded_at.is_(None),
            )
            .with_for_update()
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_active_version(
        self, source_id: int, *, for_update: bool = False
    ) -> DocumentClean | None:
        """
        当前版本 = 该身份下没被作废的那一行。

        ``for_update=True`` 走 **当前读** —— 看到的是最新已提交的行，而不是事务快照。
        身份路径必须用它：``get_source(..., for_update=True)`` 是当前读，能看到并发刚提交的身份行；
        这里若用快照读，可能看不到同一时刻写入的版本行，于是误判成「身份已存在 + 新内容」。

        :param source_id: 身份行 id。
        :param for_update: 是否走当前读（加行锁）；身份路径必须为 ``True``。
        :return: 当前版本行；该身份下没有 active 版本则为 ``None``。
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
        """
        把某个版本标成「已不是当前版本」。不物理删 —— 历史版本留着可按 id 查。

        :param clean_id: 要作废的版本 id。
        """
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

        :param limit: 本轮最多认领多少篇。
        :return: 被认领并已置为 ``processing`` 的文档行（本方法内部 commit 释放行锁）。
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

        :param clean_id: 要写回的当前版本 id。
        :param ai: enrich 产出的列（language / ai_summary / doc_type / tags 等），允许为空值。
        """
        await self.db.execute(
            update(DocumentClean)
            .where(DocumentClean.id == clean_id)
            .values(enrich_status=EnrichStatus.DONE, **ai)
        )
        await self.db.commit()

    async def reclaim_stale_enrich(self, older_than: datetime) -> int:
        """
        把卡在 processing 太久（补它的进程挂了）的文档放回 pending。

        :param older_than: 认领时刻早于它的 processing 行会被放回 pending。
        :return: 本次回收的条数。
        """
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
        """
        写入一条原始留痕并 flush（拿到自增 id 给 ``document_clean.raw_id`` 用）。

        :param source: 文档来源。
        :param external_id: 源系统里的稳定文档 id；历史数据可为 ``None``。
        :param raw_title: 收到的原始标题（未标准化）。
        :param raw_content: 收到的原始正文（未标准化）。
        :return: 新建的留痕行（已 flush、未 commit）。
        """
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
        """
        写入一个新的文档版本并 flush（拿到自增 id 给 ``document_chunk`` / ``document_dedup`` 用）。

        :param raw_id: 对应的 ``document_raw.id``。
        :param source: 文档来源。
        :param title: 标准化后的标题。
        :param content: 标准化后的正文。
        :param author: 原始入参里的署名（enrich 补的由后台写回）。
        :param url: 原文链接。
        :param doc_updated_at: 源文档的更新时间。
        :param char_count: 正文长度。
        :param clean_version: 标准化规则版本。
        :param content_hash: 本版本指纹。
        :param source_id: 身份行 id；历史数据可能为 ``None``。
        :param external_id: 源系统里的稳定文档 id。
        :param doc_type: AI 补的文档类型（落库时通常为空）。
        :param tags: AI 补的标签列表。
        :param ai_summary: AI 补的摘要。
        :param language: AI 补的语言。
        :param enrich_version: 用哪版 enrich 规则补的。
        :return: 新建的版本行（已 flush、未 commit）。
        """
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

        :param document_id: 该文档的当前版本 id（``document_clean.id``）。
        :param chunks: ``make_chunks`` 的产物，含 ``chunk_index`` / ``content`` / ``token_count``。
        :return: 插入的 chunk 条数。
        """
        if not chunks:
            return 0
        await self.db.execute(
            insert(DocumentChunk),
            [{"document_id": document_id, **c} for c in chunks],
        )
        return len(chunks)

    # ---------- 多行写入（bulk planner 用）----------

    async def insert_sources_bulk(self, rows: list[dict]) -> int | None:
        """
        一条多行 INSERT 写入多个身份。

        :param rows: 每行的列字典（source / external_id / created_at / updated_at / deleted_at）。
        :return: 本语句分配的第一个自增 id；空列表返回 ``None``。
        """
        if not rows:
            return None
        result = await self.db.execute(insert(DocumentSource).values(rows))
        return result.lastrowid

    async def insert_raws_bulk(self, rows: list[dict]) -> int | None:
        """
        一条多行 INSERT 写入多条原始留痕。

        :param rows: 每行的列字典（source / external_id / raw_title / raw_content / received_at）。
        :return: 本语句分配的第一个自增 id；空列表返回 ``None``。
        """
        if not rows:
            return None
        result = await self.db.execute(insert(DocumentRaw).values(rows))
        return result.lastrowid

    async def insert_cleans_bulk(self, rows: list[dict]) -> int | None:
        """
        一条多行 INSERT 写入多个文档版本。

        :param rows: 每行的列字典；所有非空列都必须显式给值。
        :return: 本语句分配的第一个自增 id；空列表返回 ``None``。
        """
        if not rows:
            return None
        result = await self.db.execute(insert(DocumentClean).values(rows))
        return result.lastrowid

    async def insert_chunks_bulk(self, rows: list[dict]) -> int:
        """
        所有文档的 chunk 合成一条多行 INSERT（不是 executemany）。

        :param rows: 每行的列字典，``document_id`` 由 planner 按顺序映射好。
        :return: 插入的行数。
        """
        if not rows:
            return 0
        await self.db.execute(insert(DocumentChunk).values(rows))
        return len(rows)

    async def supersede_versions(self, clean_ids: list[int], at: datetime) -> int:
        """
        批量把多个版本标成「已不是当前版本」。

        :param clean_ids: 要作废的版本 id 列表。
        :param at: 统一的作废时间。
        :return: 实际更新的行数。
        """
        if not clean_ids:
            return 0
        result = await self.db.execute(
            update(DocumentClean)
            .where(DocumentClean.id.in_(clean_ids))
            .values(superseded_at=at)
        )
        return result.rowcount or 0

    async def supersede_chunks_by_documents(self, clean_ids: list[int], at: datetime) -> int:
        """
        批量作废这些文档版本名下的全部块。

        :param clean_ids: 文档版本 id 列表（对应 ``document_chunk.document_id``）。
        :param at: 统一的作废时间，同时更新 ``updated_at``。
        :return: 实际更新的行数。
        """
        if not clean_ids:
            return 0
        result = await self.db.execute(
            update(DocumentChunk)
            .where(DocumentChunk.document_id.in_(clean_ids))
            .values(superseded_at=at, updated_at=at)
        )
        return result.rowcount or 0

    async def revive_sources(self, source_ids: list[int], at: datetime) -> int:
        """
        批量复活被删除过的身份行（清空 ``deleted_at``）。

        :param source_ids: 要复活的 ``document_source.id`` 列表。
        :param at: 统一更新 ``updated_at``。
        :return: 实际更新的行数。
        """
        if not source_ids:
            return 0
        result = await self.db.execute(
            update(DocumentSource)
            .where(DocumentSource.id.in_(source_ids))
            .values(deleted_at=None, updated_at=at)
        )
        return result.rowcount or 0

    async def list_chunks(self, document_id: int, *, page: int, limit: int) -> list[DocumentChunk]:
        """
        分页取某文档的分块，按 ``chunk_index`` 升序（翻页稳定）。

        :param document_id: 文档当前版本 id。
        :param page: 页码，从 1 开始。
        :param limit: 每页条数。
        :return: 本页的 chunk 行。
        """
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)      # 按 chunk_index 有序，翻页才稳定
            .offset((page - 1) * limit)
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def count_chunks(self, document_id: int) -> int:
        """
        统计某文档的分块总数。

        :param document_id: 文档当前版本 id。
        :return: 分块总数。
        """
        stmt = (
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )
        return int((await self.db.execute(stmt)).scalar_one())
