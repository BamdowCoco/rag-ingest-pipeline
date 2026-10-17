"""
异步建表

关键：base.py 不认识任何模型，必须靠这里 ``import app.models`` 把三张表「注册」进
Base.metadata，``create_all`` 才知道要建哪些表。少了这行 import，metadata 是空的，
create_all 会一个表都不建（而且不报错，很容易一头雾水）。
"""
from app.db.base import Base
from app.db.session import engine


async def init_models() -> None:
    """建表。create_all 默认 checkfirst=True：表已存在就跳过，可反复调用。"""
    # 在函数内 import：只为触发「模型注册」这个副作用，放模块顶层也行但不够显式
    import app.models  # noqa: F401

    # run_sync 把同步的 create_all 丢到线程里跑（元数据操作本身是同步 API）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
