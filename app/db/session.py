"""
异步数据库：引擎 + 会话工厂 + 请求级依赖

和 17 课（17_异步数据库/database.py）基本一致，差别只有一处：
DSN 从硬编码常量换成了 settings.database_url（阶段 0 拆字段拼出来的）。
"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# ---------- 引擎 ----------
# 进程内共用这一个，内部维护连接池
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,        # 本地 DEBUG=true 才打印 SQL
    pool_size=10,               # 池内常驻连接数
    max_overflow=20,            # 峰值可临时超出的数量（总上限 = 10 + 20）
    pool_recycle=3600,          # 连接活过 1 小时就回收，避免被 MySQL wait_timeout 掐断
    pool_pre_ping=True,         # 取连接前先 ping，坏了就重连（对付 wait_timeout 的另一道保险）
)

# ---------- 会话工厂 ----------
# 每个请求开一个会话。async_sessionmaker 返回的是「工厂」，调用一次得到一个会话。
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,     # 异步下的关键：commit 后属性不过期，避免再访问触发同步懒加载报 MissingGreenlet
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    请求级依赖：给每个请求一个 AsyncSession，请求结束自动关闭；出错则回滚。
    用法：db: AsyncSession = Depends(get_db)

    （阶段 3 会在 app/api/deps.py 里 `from app.db.session import get_db` 再转出去，
      依赖注入链末端用它；这里先作为规范来源。）
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
