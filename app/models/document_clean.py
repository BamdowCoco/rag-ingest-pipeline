"""标准化后正文：管道第二站，存标准化（去 HTML / 全角转半角 / 折叠空白…）后的结果。

**一行 = 一个版本** ，不是一篇文档 —— 同一篇文档更新后会有多行，其中只有一行是
当前版本（``superseded_at IS NULL``）。身份在 ``document_source``。
"""
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class EnrichStatus(StrEnum):
    """
    文档 enrich 的状态机：pending → processing → done。

    和 ``document_chunk.embedding_status`` **同一个形状** —— 因为 enrich 现在也是
    **后台补的活** ：模型调用是秒级的 **外部依赖** ，不能压在请求路径里（见 ``services/enricher.py``）。
    """

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"


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
    # collation 与身份表一致（bin）—— 不一致会出现"身份大小写敏感、过滤器不敏感"的错位。
    # 副作用（是特性不是 bug）：`?source=Feishu` 不再命中 source='feishu'，stats 分组同理。
    source: Mapped[str] = mapped_column(String(50, collation="utf8mb4_bin"), index=True, nullable=False)
    # 与 source 同理的反规范化：出参/列表要显示它，省一次 join 到 document_source。
    # 对某一行版本而言它是【不可变】的（这行永远属于同一个身份），所以不存在同步问题。
    external_id: Mapped[str | None] = mapped_column(
        String(200, collation="utf8mb4_bin"), nullable=True
    )
    # 【为什么不加索引（2026-10-17 删掉的那没用的索引）】全代码只有一处碰 title：
    # `document_repo` 的 `ilike(f"%{q}%")` —— 前导通配符用不上 B-tree，索引纯属写放大。
    # 真要做标题搜索，正解是 MySQL 8 的 FULLTEXT + ngram 解析器（中文必需）。见 §9.2。
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(120), nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    doc_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)
    char_count: Mapped[int] = mapped_column(default=0, nullable=False)
    clean_version: Mapped[int] = mapped_column(default=1, nullable=False)  # 标准化规则改了就把版本号 +1

    # 本版本的指纹。必须自己存一份 —— document_dedup 的 hash 是全局唯一，
    # 且它记住的 clean_id 在版本作废之后就指错了，不能拿来判断"当前版本内容变了没"。
    # collation 与 dedup 上那一份一致（ascii_bin）：sha256 小写 hex 是纯 ASCII。
    content_hash: Mapped[str] = mapped_column(String(64, collation="ascii_bin"), nullable=False)
    # 非 NULL = 「我已不是当前版本」。列表 / 统计一律把它过滤掉。
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---------- AI 接缝（阶段 9 留位，阶段 G 起真的开始填） ----------
    doc_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 标签天然是多值 —— 用 JSON 存列表，而不是逗号分隔的 String
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # 用哪一版规则 enrich 的（对齐 clean_version）—— 规则改了，得知道哪些行该重跑。
    # 可空是为了 provider=off 时能写 NULL（表示"没做过"），与"做了但都是空"区分开。
    enrich_version: Mapped[int | None] = mapped_column(nullable=True)
    # enrich 的状态机：落库时 pending → 后台循环认领置 processing → 补完置 done。
    # pending 就是「待补的积压」，和 chunk 的 pending 一个意思。
    enrich_status: Mapped[str] = mapped_column(
        String(16), default=EnrichStatus.PENDING, index=True, nullable=False
    )
    # 认领时刻 —— 用于把卡住的 processing 放回 pending（补它的进程挂了）。
    # 这张表本来就"写后不变"，所以不给它加通用的 updated_at，只加这一个专用的时间戳。
    enrich_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
