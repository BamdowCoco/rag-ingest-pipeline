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


class RetractionItem(BaseModel):
    """要下游从向量库删掉的块 —— 只需要 id 与它属于哪篇文档。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int


class ChunkClaimResult(BaseModel):
    chunks: list[ClaimedChunk]
    # 追加字段（对旧调用方是向后兼容的）：这些块的所属版本已作废，
    # 下游该把它们对应的向量从向量库里删掉。见「撤回流」。
    retractions: list[RetractionItem] = []


class ChunkCompleteResult(BaseModel):
    id: int
    embedding_status: str


class ChunkRetractResult(BaseModel):
    """回报「该向量已从向量库删掉」。"""
    id: int
    retracted: bool
