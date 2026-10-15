"""
向量化任务的认领与回报（阶段 C）。

下游 worker 的用法：

    POST /api/v1/chunks/claim          → 拿一批待处理的 chunk
    （去做 embedding —— 这里是最慢的一步，也是要花钱的一步）
    POST /api/v1/chunks/{id}/complete  → 回报完成

为什么用 DB 当队列、而不引 Redis/消息队列：本项目的数据量级不需要；
而 `SKIP LOCKED` 恰好提供了「多个 worker 各拿一批、互不重复」的语义。
真上规模时再换 MQ/RabbitMQ，接口形状可以不变。

注意： **超时回收不在这里** ，它是个独立的后台循环（`reclaimer.py`）—— 为什么拆出去，
见那个文件的说明。
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ChunkNotFoundError
from app.models.document_chunk import DocumentChunk
from app.repositories.chunk_repo import ChunkRepo

logger = logging.getLogger(__name__)


class TaskService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ChunkRepo(db)

    async def claim(self, limit: int) -> list[DocumentChunk]:
        """认领一批任务。超时回收由独立的 reclaim_loop 负责（见 reclaimer.py）。"""
        rows = await self.repo.claim_pending(limit)
        logger.info("认领 %d 个待向量化 chunk", len(rows))
        return rows

    async def claim_retractions(self, limit: int) -> list[DocumentChunk]:
        """
        认领一批「要下游把向量删掉」的块。

        注意它和 `claim` **不是一回事** ：撤回没有 in-flight 状态，多个 worker 可能拿到同一批
        （删向量是幂等的）—— 详细取舍见 `chunk_repo.claim_retractions`。
        """
        rows = await self.repo.claim_retractions(limit)
        if rows:
            logger.info("认领 %d 个待撤回 chunk（所属版本已作废）", len(rows))
        return rows

    async def complete(self, chunk_id: int) -> DocumentChunk:
        """回报某个 chunk 已完成。id 不存在 → 404。"""
        if not await self.repo.mark_done(chunk_id):
            logger.info("回报完成失败：chunk 不存在 id=%s", chunk_id)
            raise ChunkNotFoundError()
        logger.info("chunk %s 已完成", chunk_id)

        # mark_done 用的是 Core update()，它绕过 ORM、不会刷新身份映射里的对象。
        # 所以先让会话里可能存在的旧对象失效，再取，拿到的才是真的新状态。
        self.db.expire_all()
        obj = await self.db.get(DocumentChunk, chunk_id)
        assert obj is not None                      # 刚刚 update 成功过，必然存在
        return obj

    async def retract(self, chunk_id: int) -> DocumentChunk:
        """
        回报「该向量已从向量库删掉」。id 不存在、或已经确认过 → 404。

        `mark_done` 那条注释同样适用：Core update() 绕过 ORM，取之前要先 `expire_all()`。
        """
        if not await self.repo.mark_retracted(chunk_id):
            logger.info("回报撤回失败：chunk 不存在或已确认过 id=%s", chunk_id)
            raise ChunkNotFoundError()
        logger.info("chunk %s 的向量已撤回", chunk_id)

        self.db.expire_all()
        obj = await self.db.get(DocumentChunk, chunk_id)
        assert obj is not None
        return obj
