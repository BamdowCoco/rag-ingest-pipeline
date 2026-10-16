"""文档出入参。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DocumentCreate(BaseModel):
    """POST /documents 的入参（原始文档，未标准化）。"""
    source: str = Field(..., min_length=1, max_length=50)
    # 源系统里这篇文档的稳定 ID（飞书 / Confluence 的 doc id）—— 必填，它就是「文档身份」。
    # 有了身份才分得清「同一篇改过了（更新）」和「这是一篇新文档（新增）」。
    external_id: str = Field(..., min_length=1, max_length=200)
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
    external_id: str | None = None
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
    language: str | None = None


class DocumentDelete(BaseModel):
    """删除信号：源系统推过来的「这篇没了」。按身份删，所以只要 source + external_id。"""
    source: str = Field(..., min_length=1, max_length=50)
    external_id: str = Field(..., min_length=1, max_length=200)


class DeleteResult(BaseModel):
    """删除是 **幂等** 的（删除信号和重复文档一样会被反复重发），所以不报 404。"""
    deleted: bool        # 本次是否真的删掉了；重复删返回 False


class BulkDocumentCreate(BaseModel):
    """POST /documents/bulk 的入参 —— 同步器的真实调用方式，一次最多 200 篇。"""
    documents: list[DocumentCreate] = Field(..., min_length=1, max_length=200)


class BulkItemResult(BaseModel):
    """批量提交里【单篇】的结果。"""
    index: int                                        # 在请求里的位置
    status: Literal["created", "updated", "duplicate", "rejected"]
    id: int | None = None                             # created/updated = 新版本 id；duplicate = 已存在那篇的 id


class BulkResult(BaseModel):
    """批量提交的汇总结果。重复【不是错误】，按条跳过 —— 见 README「为什么重复不是错误」。"""
    received: int
    created: int
    updated: int                                      # 同 external_id 内容变了 → 更新
    duplicates: int
    rejected: int                                     # 空正文，或落库失败
    items: list[BulkItemResult]
