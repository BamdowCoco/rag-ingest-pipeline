"""
Alembic 环境（ **异步** 版）。

两点和默认模板不同：

1. **DSN 从 app 的 `settings` 取** ，不写在 `alembic.ini` 里 ——
   这样迁移连的库和应用连的库 **永远是同一个** （容器里是 `mysql`、本地是 `localhost`，
   靠环境变量切，不会错位）。
2. 用 **async engine** （项目是 aiomysql）—— 所以走 `connection.run_sync(...)`
   把同步的 `context.run_migrations()` 丢进去跑。
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401  —— 只为一个副作用：把五张表注册进 Base.metadata
from app.core.config import settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# configparser 会把 % 当插值符 —— DSN 里若有 % （比如口令）要先转义
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

# autogenerate 拿它和数据库现状对比
target_metadata = Base.metadata


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
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
