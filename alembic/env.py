"""
Alembic 环境（ **异步** 版）。

两点和默认模板不同：

1. **DSN 从 app 的 ``settings`` 取** ，不写在 ``alembic.ini`` 里 ——
   这样迁移连的库和应用连的库 **永远是同一个** （都从 compose 注入的服务名 ``mysql:3306`` 来，
   不靠人记、也不会错位）。
2. 用 **async engine** （项目是 aiomysql）—— 所以走 ``connection.run_sync(...)``
   把同步的 ``context.run_migrations()`` 丢进去跑。
"""
import asyncio
import contextlib
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401  —— 只为一个副作用：把五张表注册进 Base.metadata
from app.core.config import settings
from app.core.logging import is_configured as logging_is_configured
from app.db.base import Base

config = context.config

# 【为什么这里要分情况】`fileConfig` 不是"加一个 handler"，它是按 alembic.ini
# 重建根 logger：root 级别设成 WARNING、并把已有的 logger 全 禁用
# （`disable_existing_loggers` 默认就是 True）。
# 命令行里独立跑 `alembic upgrade` 时这正好合适 —— 迁移自己的日志够用、也不吵；
# 但 lifespan 是在 应用进程内 调 upgrade 的（见 app/main.py），
# 那时应用刚配好的日志会被整个顶掉：启动之后自己的 INFO 日志全部静默消失，
# 连 "后台任务已启动" 这种话都看不到 —— 排查时最要命。
# 所以只在 "应用还没配过日志"（= 独立跑迁移）时才套 alembic.ini。
if config.config_file_name is not None and not logging_is_configured():
    fileConfig(config.config_file_name)

# configparser 会把 % 当插值符 —— DSN 里若有 % （比如口令）要先转义
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

# autogenerate 拿它和数据库现状对比
target_metadata = Base.metadata

# 迁移咨询锁的名字：4 个 uvicorn worker 共享同一个库，必须串行迁移
MIGRATION_LOCK = "rag_ingest_migrate"


def run_migrations_offline() -> None:
    """离线模式：不连库，只把 SQL 打出来。"""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        # 【多 worker 必备】每个 uvicorn worker 的 lifespan 都会跑 upgrade head。
        # 用 MySQL 的连接级咨询锁把迁移串行化，否则多个进程会同时 DDL 同一个库。
        # GET_LOCK 绑定在连接上，所以必须和迁移用同一个 connection（NullPool 正好保证）。
        acquired = (
            await connection.exec_driver_sql(
                f"SELECT GET_LOCK('{MIGRATION_LOCK}', 30)"
            )
        ).scalar()
        if acquired != 1:
            raise RuntimeError("等待迁移锁超时：另一个进程正在迁移")
        try:
            await connection.run_sync(do_run_migrations)
        finally:
            # 连接断了 MySQL 会自动释放；这里吞掉异常，别掩盖迁移本身的错误
            with contextlib.suppress(Exception):
                await connection.exec_driver_sql(
                    f"SELECT RELEASE_LOCK('{MIGRATION_LOCK}')"
                )
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
