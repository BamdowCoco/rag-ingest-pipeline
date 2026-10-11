"""
FastAPI 入口

阶段 0：先做到「能起来 + 能打 /health + 起停有日志」。
lifespan 就是「应用启动到关闭」期间的生命周期钩子：yield 之前是启动逻辑，yield 之后是关停逻辑。

阶段 1/3 会往这里补（现在还没有那些模块，所以先不写）：
  启动：await redis.ping()、await init_models()
  关停：await close_redis()、await engine.dispose()
"""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.logging import setup_logging

setup_logging()  # 必须在其它模块打日志之前调用
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ---------- 启动 ----------
    logger.info("启动 %s (debug=%s)", settings.app_name, settings.debug)
    logger.info("MySQL -> %s:%s/%s", settings.db_host, settings.db_port, settings.db_name)
    logger.info("Redis -> %s:%s/%s", settings.redis_host, settings.redis_port, settings.redis_db)
    yield
    # ---------- 关停 ----------
    logger.info("已关闭 %s", settings.app_name)


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
