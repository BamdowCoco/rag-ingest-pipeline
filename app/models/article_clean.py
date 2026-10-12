"""清洗后正文：管道第二站，存清洗（去 HTML / 全角转半角 / 折叠空白…）后的结果。"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class ArticleClean(Base):
    __tablename__ = "article_clean"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    raw_id: Mapped[int] = mapped_column(
        ForeignKey("article_raw.id", ondelete="CASCADE"), nullable=False
    )
    # source 放在这里：阶段 7 列表要按它过滤、阶段 8 统计要按它分组，省一次 join
    source: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(120), nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)
    char_count: Mapped[int] = mapped_column(default=0, nullable=False)
    clean_version: Mapped[int] = mapped_column(default=1, nullable=False)  # 清洗规则改了就把版本号 +1

    # ---------- AI 接缝（阶段 9 才填，先留可空，接大模型时不用改表） ----------
    sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
