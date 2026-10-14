"""分块相关的出入参。"""
from pydantic import BaseModel, ConfigDict, Field


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    chunk_index: int
    content: str
    token_count: int
    embedding_status: str


class ChunkClaimRequest(BaseModel):
    """worker 认领任务的请求体。"""
    limit: int = Field(20, ge=1, le=200)


class ClaimedChunk(BaseModel):
    """认领到的任务单元 —— worker 要的就是 id + 内容，别的字段对它没用。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    chunk_index: int
    content: str
    token_count: int


class ChunkClaimResult(BaseModel):
    chunks: list[ClaimedChunk]


class ChunkCompleteResult(BaseModel):
    id: int
    embedding_status: str
