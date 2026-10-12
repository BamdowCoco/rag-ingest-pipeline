"""原始留痕表：管道第一站，收到的数据原样存下（不清洗），日后清洗规则变了还能重跑。"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class ArticleRaw(Base):
    __tablename__ = "article_raw"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    raw_title: Mapped[str] = mapped_column(String(500), nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 原始 JSON 留档
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
