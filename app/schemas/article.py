"""文章出入参。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ArticleCreate(BaseModel):
    """POST /articles 的入参（原始舆情数据，未清洗）。"""
    source: str = Field(..., min_length=1, max_length=50)
    title: str = Field(..., min_length=1, max_length=500)     # 原始可到 500，清洗后截到 255
    content: str = Field(..., min_length=1)
    author: str | None = Field(None, max_length=120)
    url: str | None = Field(None, max_length=500)
    published_at: datetime | None = None


class ArticleOut(BaseModel):
    """出参：对应清洗后的 ArticleClean。from_attributes 让它能直接从 ORM 对象读属性。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    title: str
    content: str
    author: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    fetched_at: datetime
    char_count: int
    sentiment: str | None = None
    category: str | None = None
    ai_summary: str | None = None
