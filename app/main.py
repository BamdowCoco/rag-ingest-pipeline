"""
FastAPI 入口

lifespan 是「应用启动到关闭」的生命周期钩子：yield 之前是启动逻辑，yield 之后是关停逻辑。
启动时探活 Redis、 **把库升到最新** ——有问题就在启动阶段暴露，别等第一个请求才炸。
"""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.db.session import engine
from app.infra.redis_client import close_redis, redis_client
from app.services.reclaimer import reclaim_loop

setup_logging()  # 必须在其它模块打日志之前调用
logger = logging.getLogger(__name__)

# alembic.ini 在仓库根（app/ 的上一层）—— 用绝对路径，不依赖启动时的 CWD
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _upgrade_schema() -> None:
    """
    把库升到最新一版（alembic 是同步 API，调用方要丢到线程里跑）。

    换成迁移之后， **加列不再等于"重建库、丢数据"** —— 空库首启它会把五张表建出来，
    以后改 schema 就写一条迁移升上去。这正是前几轮反复踩的那个坑。
    """
    command.upgrade(AlembicConfig(str(_ALEMBIC_INI)), "head")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ---------- 启动 ----------
    logger.info("启动 %s (debug=%s)", settings.app_name, settings.debug)
    # 只打 host:port，别把带密码的 database_url 整个打出来
    logger.info("MySQL -> %s:%s/%s", settings.db_host, settings.db_port, settings.db_name)
    logger.info("Redis -> %s:%s/%s", settings.redis_host, settings.redis_port, settings.redis_db)

    await redis_client.ping()   # 探活：Redis 不可用就在启动阶段直接失败（fail fast）
    logger.info("Redis 连接正常")

    # 迁移是阻塞的（连库 + 执行 DDL），所以丢到线程里，别卡住事件循环
    await asyncio.to_thread(_upgrade_schema)
    logger.info("数据库表已就绪")

    # 后台回收任务：起一个 asyncio task（为什么独立出来，见 reclaimer.py 的说明）
    reclaimer = asyncio.create_task(reclaim_loop())

    yield

    # ---------- 关停 ----------
    # 顺序要紧：先取消后台任务、再关连接池。反过来的话，任务可能拿到已 dispose 的池。
    reclaimer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reclaimer
    await close_redis()         # 释放 Redis 连接池
    await engine.dispose()      # 释放数据库连接池
    logger.info("已关闭 %s", settings.app_name)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
register_exception_handlers(app)     # 业务异常 → HTTP 的统一映射在这里挂上
app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
