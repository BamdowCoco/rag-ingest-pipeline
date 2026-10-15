"""文档出入参。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DocumentCreate(BaseModel):
    """POST /documents 的入参（原始文档，未标准化）。"""
    source: str = Field(..., min_length=1, max_length=50)
    title: str = Field(..., min_length=1, max_length=500)     # 原始可到 500，标准化后截到 255
    content: str = Field(..., min_length=1)
    author: str | None = Field(None, max_length=120)
    url: str | None = Field(None, max_length=500)
    doc_updated_at: datetime | None = None


class DocumentOut(BaseModel):
    """出参：对应标准化后的 DocumentClean。from_attributes 让它能直接从 ORM 对象读属性。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    title: str
    content: str
    author: str | None = None
    url: str | None = None
    doc_updated_at: datetime | None = None
    fetched_at: datetime
    char_count: int
    doc_type: str | None = None
    tags: list[str] | None = None
    ai_summary: str | None = None


class BulkDocumentCreate(BaseModel):
    """POST /documents/bulk 的入参 —— 同步器的真实调用方式，一次最多 200 篇。"""
    documents: list[DocumentCreate] = Field(..., min_length=1, max_length=200)


class BulkItemResult(BaseModel):
    """批量提交里【单篇】的结果。"""
    index: int                                        # 在请求里的位置
    status: Literal["created", "duplicate", "rejected"]
    id: int | None = None                             # created = 新建的 id；duplicate = 已存在那篇的 id


class BulkResult(BaseModel):
    """批量提交的汇总结果。重复【不是错误】，按条跳过 —— 见 README「为什么重复不是错误」。"""
    received: int
    created: int
    duplicates: int
    rejected: int                                     # 空正文，或落库失败
    items: list[BulkItemResult]
