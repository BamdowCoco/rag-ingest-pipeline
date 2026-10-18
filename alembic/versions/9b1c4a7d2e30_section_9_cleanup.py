"""section 9 cleanup: drop the idle index / dead columns, pin collations, align the constraint name

把 `docs/数据库设计.md` 第九节里标「该改（现在）」的那几条一次做掉（外加 §9.13-a 的定删项）：

 - 9.2  删 `document_clean.title` 那个索引（全代码唯一用法是 `LIKE '%q%'`，前导通配用不上 B-tree）
 - 9.3  删死列 `document_dedup.first_seen_at`（全仓零引用）
 - 9.4  标识列 / 指纹列显式 collation，别继承库默认的 `utf8mb4_unicode_ci`
 - 9.7① 把 `document_dedup` 那个唯一键改名成命名约定下的名字
 - 9.13-a 删 `document_raw.payload`（零读路径 + 与自身几列全重复）
 - 9.13-b `document_raw.external_id` 提成真列（删掉 payload 后，raw 就少了"我是上游哪一篇"的线索）

Revision ID: 9b1c4a7d2e30
Revises: d216f384bd55
Create Date: 2026-10-17
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "9b1c4a7d2e30"
down_revision: str | None = "d216f384bd55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 六条标识列（表, 列, 长度, 可空）—— upgrade 与 downgrade 都要用，抽出来免得抄两遍抄错。
# 为什么三张表一起改：`document_source` 是唯一性的权威，`clean` / `raw` 上的同名列只是它的副本，
# 让两侧 collation 一致，才不会出现「身份大小写敏感、列表过滤器不敏感」这种错位。
_IDENTITY_COLUMNS = (
    ("document_source", "source", 50, False),
    ("document_source", "external_id", 200, False),
    ("document_clean", "source", 50, False),
    ("document_clean", "external_id", 200, True),
    ("document_raw", "source", 50, False),
    ("document_raw", "external_id", 200, True),
)


def upgrade() -> None:
    # ---------- 9.2 一个空转的索引 ----------
    op.drop_index(op.f("ix_document_clean_title"), table_name="document_clean")

    # ---------- 9.3 死列（另一个死列 seen_count 早前那次大改就删了）----------
    op.drop_column("document_dedup", "first_seen_at")

    # ---------- 9.13-a 删掉整包请求体 ----------
    op.drop_column("document_raw", "payload")

    # ---------- 9.13-b raw 自描述：把 external_id 提成真列 ----------
    # 可空只为兼容「身份功能之前」的历史行（那些行的 clean.external_id 也是 NULL）；
    # DocumentCreate.external_id 是必填的，所以新写入的行必然非空。
    # 回填借 raw → clean 的 1:1 关系：`insert_raw` 只在 `_persist` 里与 `insert_clean` 各调一次，
    # 一条 raw 恰好对应一条 clean（更新是新建一对，不是一对多）。
    op.add_column("document_raw", sa.Column("external_id", sa.String(length=200), nullable=True))
    op.execute(
        "UPDATE document_raw r JOIN document_clean c ON c.raw_id = r.id "
        "SET r.external_id = c.external_id"
    )

    # ---------- 9.4 collation：标识列钉死 utf8mb4_bin ----------
    # 不钉就继承库默认 utf8mb4_unicode_ci（大小写不敏感）⇒「ABC」与「abc」判成同一个 ID，
    # 后到的那个请求拿 409，被误判成"同一篇在并发创建"。
    # 【MySQL 的 MODIFY 要完整列定义】所以 existing_type / existing_nullable 都得给。
    for table, column, length, nullable in _IDENTITY_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.String(length=length),
            type_=sa.String(length=length, collation="utf8mb4_bin"),
            existing_nullable=nullable,
        )

    # ---------- 9.4 指纹列：sha256 小写 hex 是纯 ASCII ----------
    # 换 ascii_bin 之后，dedup 那条唯一索引从 256 字节（64×4）缩到 64 字节，
    # 语义也从"碰巧大小写不敏感"变成确定的二进制比较（对纯 hex 而言两者等价）。
    for table in ("document_clean", "document_dedup"):
        op.alter_column(
            table,
            "content_hash",
            existing_type=sa.String(length=64),
            type_=sa.String(length=64, collation="ascii_bin"),
            existing_nullable=False,
        )

    # ---------- 9.7① 命名约定：唯一一处要顺着改的名字 ----------
    # 【为什么是条件式的】这条只对「迁移之前就存在的库」有意义 —— 那种库的表是当年 `create_all`
    # （那会儿还没有命名约定）建的，唯一键自动名叫 `content_hash`。而**空库从零跑迁移**时，
    # Alembic 会直接按 `target_metadata` 的约定把它建成 `uq_document_dedup_content_hash`
    # （同一个库，两条路径，历史不同 —— 这正是 `op.f()` 存在的理由：标记"这名字已是最终形态"。
    # 上一版这里无条件 RENAME，空库路径直接报 1176 Key 'content_hash' doesn't exist）。
    # 用 RENAME INDEX 而不是 drop + create —— 不重建数据；【必须排在上面 MODIFY 之后】。
    # （库里的 FK 名 `document_clean_ibfk_1` 之类不用动：实测 FK / PK 的名字不参与 autogenerate 比较。）
    _index_names = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("document_dedup")}
    if "content_hash" in _index_names:
        op.execute(
            "ALTER TABLE document_dedup "
            "RENAME INDEX `content_hash` TO `uq_document_dedup_content_hash`"
        )


def downgrade() -> None:
    # 反向一路退回去，顺序与 upgrade 相反：改名 → collation → 列 → 索引。
    # 改名同样条件式（见 upgrade 里那段说明）。
    _index_names = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("document_dedup")}
    if "uq_document_dedup_content_hash" in _index_names:
        op.execute(
            "ALTER TABLE document_dedup "
            "RENAME INDEX `uq_document_dedup_content_hash` TO `content_hash`"
        )

    for table in ("document_dedup", "document_clean"):
        op.alter_column(
            table,
            "content_hash",
            existing_type=sa.String(length=64, collation="ascii_bin"),
            type_=sa.String(length=64),
            existing_nullable=False,
        )

    # 【注意】这里改回的是"继承库默认"的 utf8mb4_unicode_ci。若库里此时已经有只差大小写的
    # 标识行，回退会在 uq_source_external 上撞唯一键 —— 那正是 upgrade 要修掉的问题。
    for table, column, length, nullable in _IDENTITY_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.String(length=length, collation="utf8mb4_bin"),
            type_=sa.String(length=length),
            existing_nullable=nullable,
        )

    op.drop_column("document_raw", "external_id")

    # 加回列【能回来，值回不来】：payload 与 first_seen_at 的数据在 upgrade 时就删了。
    op.add_column("document_raw", sa.Column("payload", sa.JSON(), nullable=True))
    # first_seen_at 是 NOT NULL —— 照抄 d216 那次的老办法：先带 server_default 把行回填成某个值，
    # 再把默认值摘掉（让库最终与模型一致，模型用的是 Python 端 default）。
    # 非空表直接加"NOT NULL 且无默认"的列会失败，这条路本项目走过一次。
    op.add_column(
        "document_dedup",
        sa.Column(
            "first_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
    )
    op.alter_column("document_dedup", "first_seen_at", server_default=None)

    op.create_index(op.f("ix_document_clean_title"), "document_clean", ["title"], unique=False)
