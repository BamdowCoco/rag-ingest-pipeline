"""
文档身份表：一个 **源文档** 一行 —— 它回答的是「这是不是同一篇」。

**为什么必须单独一张表：** ``document_clean`` 上一个 ``external_id`` 会有 **多行版本**
（每次更新都新增一行），所以 ``UNIQUE(source, external_id)`` 根本没地方建。
「身份」和「版本」是两件事 —— 一张表装不下。这正是本段要解决的那个诊断。

同步器侧的 ``external_id`` 就是飞书 / Confluence 的文档 ID， **必填** ——
它就是「文档身份」；没有身份，系统分不清"同一篇改过了"和"这是一篇新文档"。
"""
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class DocumentSource(Base):
    __tablename__ = "document_source"
    # 同一源系统的同一篇文档只该有一行身份 —— 并发【创建】由这个唯一约束兜底
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_source_external"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    # 源系统发删除信号时置上；同一事务里 active 版本会被一并作废
    # （于是「deleted ⇒ 没有 active 版本」这条不变式成立，读路径不必 join 到本表）
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
