"""
FastAPI 入口

lifespan 是「应用启动到关闭」的生命周期钩子：yield 之前是启动逻辑，yield 之后是关停逻辑。
启动时探活 Redis、建表——有问题就在启动阶段暴露，别等第一个请求才炸。
"""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.db.init_db import init_models
from app.db.session import engine
from app.infra.redis_client import close_redis, redis_client

setup_logging()  # 必须在其它模块打日志之前调用
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ---------- 启动 ----------
    logger.info("启动 %s (debug=%s)", settings.app_name, settings.debug)
    # 只打 host:port，别把带密码的 database_url 整个打出来
    logger.info("MySQL -> %s:%s/%s", settings.db_host, settings.db_port, settings.db_name)
    logger.info("Redis -> %s:%s/%s", settings.redis_host, settings.redis_port, settings.redis_db)

    await redis_client.ping()   # 探活：Redis 不可用就在启动阶段直接失败（fail fast）
    logger.info("Redis 连接正常")

    await init_models()         # 建表（阶段 2 有模型后才会真正建出表）
    logger.info("数据库表已就绪")

    yield

    # ---------- 关停 ----------
    await close_redis()         # 释放 Redis 连接池
    await engine.dispose()      # 释放数据库连接池
    logger.info("已关闭 %s", settings.app_name)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
register_exception_handlers(app)     # 业务异常 → HTTP 的统一映射在这里挂上
app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
