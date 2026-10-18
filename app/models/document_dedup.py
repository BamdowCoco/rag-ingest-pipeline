"""
内容索引：记录 **「当前仍 active 的内容」** —— 一份内容只属于一个活着的版本。

它管的是 **「新内容进门的准入」** ：这份内容是不是已经属于别人了（多源转载就靠它挡）。
**不是** 「这是不是同一篇」—— 那是 ``document_source`` 的事。两个键正交。

``content_hash`` 的唯一索引就是 **权威** ，Redis Set 只是快路径。

【关键】它会 **被释放** ：旧版本被更新 / 删除时，它的内容要从这里 **删掉** 。
不删的话，旧内容会永远占着位，把一份 **其实已经不在语料里** 的内容一直判成"重复" ——
而且它指向的那个版本早就被作废了，回给调用方的会是个死版本。
（这就是它从「见过就永远占位」改成「active 内容索引」的原因。）
"""
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentDedup(Base):
    __tablename__ = "document_dedup"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # sha256 的十六进制是 64 字符；unique=True 本身就会建成唯一索引，别再叠加 index=True。
    # collation 显式写 ascii_bin：它是纯 ASCII 的小写 hex，用 ascii 比 utf8mb4 省 ——
    # 这条唯一索引因此从 256 字节缩到 64 字节；顺带语义也从「大小写不敏感」变成确定的二进制比较。
    content_hash: Mapped[str] = mapped_column(
        String(64, collation="ascii_bin"), unique=True, nullable=False
    )
    # 指向【当前持有这份内容的那个 active 版本】
    clean_id: Mapped[int] = mapped_column(
        ForeignKey("document_clean.id", ondelete="CASCADE"), nullable=False
    )

    # 【两个死列的下场】`seen_count` 随「active 内容索引」那次大改删掉；`first_seen_at`
    # 在 2026-10-17 收口时删掉（全仓零引用）。要再加"这份内容被转载了几次"这类字段，
    # 先想清楚代价：命中快路径现在是零 DB 写，计数意味着「一次曝光换一次写」。见 §9.3。
