"""分块表：文档切成的 chunk，是下游向量化的【任务单元】。"""
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class EmbeddingStatus(StrEnum):
    """
    chunk 的向量化状态机：pending → processing → done。

    用 StrEnum 而不是裸字符串：状态名写错会立刻报错，
    而不是悄悄往库里存一个谁也不认识的字符串。
    """

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"


class DocumentChunk(Base):
    __tablename__ = "document_chunk"
    # 同一文档内 chunk_index 唯一 —— 多一道防「重复切块」的保险
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_index"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 指向 document_clean.id —— 站在消费方视角，这就是「这篇文档」
    document_id: Mapped[int] = mapped_column(
        ForeignKey("document_clean.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(nullable=False)      # 0 起，同文档内有序
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(default=0, nullable=False)
    # 阶段 C 的认领查询按它筛，所以现在就加索引
    embedding_status: Mapped[str] = mapped_column(
        String(16), default=EmbeddingStatus.PENDING, index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
