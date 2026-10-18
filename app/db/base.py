"""
ORM 基类
"""
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 【为什么要写死一组命名约定】不写的话，约束名 / 索引名由 SQLAlchemy 自动生成且不可预测；
# 而 autogenerate 会把「名字对不上」当成一次 schema 变更，于是每次都吐一堆噪音 diff
# （甚至反复 drop / create）。在 metadata 上设一次，之后新建的对象名字就稳定了。
#
# 【实测的边界】alembic 1.20 对着本项目的活库跑过一遍，结论只有三条：
#
#  - FK 与 PK 的名字不参与比较 —— 库里既有的 `document_clean_ibfk_1` 这类 MySQL 自动名不用动，
#    不会产生 diff（也就不必去 drop / add 外键，那在 MySQL 上要连索引一起重建）。
#  - `index=True` 生成的索引本来就叫 `ix_<表>_<列>`，与 `ix` 模板一致，同样不产生 diff。
#  - 唯一要顺着改的是 `document_dedup.content_hash`（`unique=True` 生成的那个）：自动名叫
#    `content_hash`，套上约定后变成 `uq_document_dedup_content_hash` —— 所以本轮那条迁移里
#    带了一句 `RENAME INDEX`（见 `alembic/versions/` 里 section 9 那条）。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
