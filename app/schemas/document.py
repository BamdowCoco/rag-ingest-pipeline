"""文档出入参。"""
from datetime import datetime

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
