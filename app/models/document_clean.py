"""标准化后正文：管道第二站，存标准化（去 HTML / 全角转半角 / 折叠空白…）后的结果。

**一行 = 一个版本** ，不是一篇文档 —— 同一篇文档更新后会有多行，其中只有一行是
当前版本（`superseded_at IS NULL`）。身份在 `document_source`。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class DocumentClean(Base):
    __tablename__ = "document_clean"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    raw_id: Mapped[int] = mapped_column(
        ForeignKey("document_raw.id", ondelete="CASCADE"), nullable=False
    )
    # 指向身份行；可空 —— 没带 external_id 的文档没有身份，走老的「只新增」路径
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_source.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # source 放在这里：阶段 7 列表要按它过滤、阶段 8 统计要按它分组，省一次 join
    source: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    # 与 source 同理的反规范化：出参/列表要显示它，省一次 join 到 document_source。
    # 对某一行版本而言它是【不可变】的（这行永远属于同一个身份），所以不存在同步问题。
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(120), nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    doc_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)
    char_count: Mapped[int] = mapped_column(default=0, nullable=False)
    clean_version: Mapped[int] = mapped_column(default=1, nullable=False)  # 标准化规则改了就把版本号 +1

    # 本版本的指纹。必须自己存一份 —— document_dedup 的 hash 是全局唯一，
    # 且它记住的 clean_id 在版本作废之后就指错了，不能拿来判断"当前版本内容变了没"。
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 非 NULL = 「我已不是当前版本」。列表 / 统计一律把它过滤掉。
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---------- AI 接缝（阶段 9 留位，阶段 G 起真的开始填） ----------
    doc_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 标签天然是多值 —— 用 JSON 存列表，而不是逗号分隔的 String
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # 用哪一版规则 enrich 的（对齐 clean_version）—— 规则改了，得知道哪些行该重跑。
    # 可空是为了将来 provider=off 时能写 NULL（表示"没做过"），与"做了但都是空"区分开。
    enrich_version: Mapped[int | None] = mapped_column(nullable=True)
