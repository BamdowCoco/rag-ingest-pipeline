"""原始留痕表：管道第一站，收到的数据原样存下（不标准化），日后标准化规则变了还能重跑。"""
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class DocumentRaw(Base):
    __tablename__ = "document_raw"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 标识列一律钉 utf8mb4_bin，别继承库默认（默认是 _ci = 大小写不敏感）。理由见 document_source.py
    source: Mapped[str] = mapped_column(String(50, collation="utf8mb4_bin"), nullable=False)
    # 【为什么留痕表也要 external_id】它是「这是上游哪一篇」的线索 —— 少了它，得 join 回 clean
    # 才知道一条 raw 对应谁。可空只为兼容「身份功能之前」的历史行（那些行的 clean.external_id 也是 NULL）。
    external_id: Mapped[str | None] = mapped_column(
        String(200, collation="utf8mb4_bin"), nullable=True
    )
    raw_title: Mapped[str] = mapped_column(String(500), nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    # 【曾经有个 payload（JSON，整个请求体）】2026-10-17 删掉了，别再把它加回来：
    # 它零读路径、与上面那几个列全重复（同一份正文因此存了三遍），而且它宣称的价值
    # （把"不认识的字段"一起留档）是假的 —— DocumentCreate 没开 extra="allow"，
    # pydantic 默认把未知字段丢掉，所以里头根本不会有"不认识的字段"。见 docs/数据库设计.md §9.13-a。
