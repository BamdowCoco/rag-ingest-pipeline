"""
向量化任务的认领与回报（阶段 C）—— 给下游 worker 调的。

注意路径顺序：`/chunks/claim`（两段）与 `/chunks/{chunk_id}/complete`（三段）
段数不同，不会互相遮蔽。但 **将来若加 `/chunks/{chunk_id}`，必须注册在 `/claim` 之后**
—— 就是阶段 7 那个 `/{id}` 吃 `/stats` 的同类坑。
"""
from fastapi import APIRouter, Depends, Path

from app.api.deps import get_task_service
from app.schemas.chunk import ChunkClaimRequest, ChunkClaimResult, ChunkCompleteResult
from app.services.task_service import TaskService

router = APIRouter()


@router.post("/claim", response_model=ChunkClaimResult, summary="认领待向量化的分块")
async def claim_chunks(
    payload: ChunkClaimRequest,
    service: TaskService = Depends(get_task_service),
) -> ChunkClaimResult:
    chunks = await service.claim(payload.limit)
    return ChunkClaimResult(chunks=chunks)


@router.post("/{chunk_id}/complete", response_model=ChunkCompleteResult, summary="回报分块已完成")
async def complete_chunk(
    chunk_id: int = Path(..., ge=1),
    service: TaskService = Depends(get_task_service),
) -> ChunkCompleteResult:
    chunk = await service.complete(chunk_id)
    return ChunkCompleteResult(id=chunk.id, embedding_status=chunk.embedding_status)
