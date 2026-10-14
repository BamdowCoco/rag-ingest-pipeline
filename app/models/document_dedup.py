"""去重表：判重的【最终权威】。content_hash 唯一索引，Redis 只是快路径。"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class DocumentDedup(Base):
    __tablename__ = "document_dedup"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # sha256 的十六进制是 64 字符；unique=True 本身就会建成唯一索引，别再叠加 index=True
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    clean_id: Mapped[int] = mapped_column(
        ForeignKey("document_clean.id", ondelete="CASCADE"), nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    seen_count: Mapped[int] = mapped_column(default=1, nullable=False)
