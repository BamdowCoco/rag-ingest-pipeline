"""
分块仓储：认领 / 完成 / 回收 —— 阶段 C 的任务分发。

这里用 MySQL 的行锁做「极简任务队列」：``SELECT ... FOR UPDATE SKIP LOCKED``。
它和分布式锁的分工正好形成对照：

    锁（Redis）          = 这件事只能一个人做（保护临界区）
    SKIP LOCKED（MySQL） = 这堆活大家分着做（分发任务）

``SKIP LOCKED`` 让每个 worker 拿到 **不同的那一批** ，天然不重复。
"""
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_chunk import DocumentChunk, EmbeddingStatus
from app.utils.time import utcnow


class ChunkRepo:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def claim_pending(self, limit: int) -> list[DocumentChunk]:
        """
        认领一批待向量化的 chunk，并置为 processing。

        SKIP LOCKED 是关键：已被别人锁住的行 **直接跳过** ，所以并发调用拿到的是
        互不相交的两批。朴素写法（先 SELECT 再 UPDATE）会让两个 worker 拿到同一批。
        """
        stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.embedding_status == EmbeddingStatus.PENDING,
                # 排除已作废的块 —— 所属版本已经不是当前版本了，再向量化就是白花钱
                DocumentChunk.superseded_at.is_(None),
            )
            .order_by(DocumentChunk.id)          # 稳定的顺序，避免长期饿死某些行
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self.db.execute(stmt)).scalars().all())
        now = utcnow()
        for row in rows:
            row.embedding_status = EmbeddingStatus.PROCESSING
            row.updated_at = now
        await self.db.commit()                  # 提交才释放行锁
        return rows

    async def reclaim_stale(self, older_than: datetime) -> int:
        """
        把卡在 processing 太久的 chunk 放回 pending，返回回收条数。

        **这一环最容易漏** ：worker 领了任务然后崩了，那条就永远卡在 processing ——
        没人会再碰它。没有回收，任务队列会慢慢"漏"掉一部分活。
        """
        stmt = (
            update(DocumentChunk)
            .where(
                DocumentChunk.embedding_status == EmbeddingStatus.PROCESSING,
                DocumentChunk.updated_at < older_than,
                # 已作废的不必再放回 pending —— 反正 claim_pending 也不认领它
                DocumentChunk.superseded_at.is_(None),
            )
            .values(embedding_status=EmbeddingStatus.PENDING, updated_at=utcnow())
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount or 0

    async def mark_done(self, chunk_id: int) -> bool:
        """把某个 chunk 标成 done。返回 False 表示没这个 id。"""
        stmt = (
            update(DocumentChunk)
            .where(DocumentChunk.id == chunk_id)
            .values(embedding_status=EmbeddingStatus.DONE, updated_at=utcnow())
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return (result.rowcount or 0) > 0

    # ---------- 更新 / 删除语义（阶段 F）----------

    async def supersede_by_document(self, document_id: int) -> int:
        """
        作废某个版本的全部块，返回条数。

        故意 **不动 ``retracted_at``** —— 哪些块需要下游撤回，交给 ``claim_retractions``
        按「已 done 且未 retracted」自己筛。于是「作废时还停在 pending 的块不用撤回」
        （下游从没见过它）这件事 **自动成立** ，不用在这里特判。
        """
        stmt = (
            update(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.superseded_at.is_(None),
            )
            .values(superseded_at=utcnow())
        )
        result = await self.db.execute(stmt)
        return result.rowcount or 0

    async def claim_retractions(self, limit: int) -> list[DocumentChunk]:
        """
        认领一批「要下游把向量删掉」的块。

        **它和补齐流不一样：这里没有 in-flight 状态。** 因为删向量是 **幂等** 的
        （同一个向量删两次没差别），所以允许多个 worker 拿到同一批、下游重复删 ——
        换来的是「worker 崩了任务也不会静默丢失，下一轮自然重试」。
        也就是说它 **不共享** ``SKIP LOCKED`` 的「各拿一批」语义，这是刻意的取舍。

        只在 **已 done** 的块上产生任务：没被认领过的 pending 块压根没向量可删。
        """
        stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.superseded_at.is_not(None),
                DocumentChunk.retracted_at.is_(None),
                DocumentChunk.embedding_status == EmbeddingStatus.DONE,
            )
            .order_by(DocumentChunk.id)
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def mark_retracted(self, chunk_id: int) -> bool:
        """下游确认「该向量已删」。返回 False 表示没这个 id、或已经确认过了。"""
        stmt = (
            update(DocumentChunk)
            .where(DocumentChunk.id == chunk_id, DocumentChunk.retracted_at.is_(None))
            .values(retracted_at=utcnow())
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return (result.rowcount or 0) > 0
