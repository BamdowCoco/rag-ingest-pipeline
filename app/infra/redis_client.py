"""
Redis 客户端（redis.asyncio）

和 14/17 课的 redis_client.py 基本一致，差别有三：
- URL 来自 settings.redis_url（同样靠环境变量切本地/容器）
- 加了 health_check_interval，空闲连接定期探活
- 额外提供 close_redis()，供 main.py 的 lifespan 关停时释放连接池
"""
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis

from app.core.config import settings

# 模块级单例：整个进程共用一个客户端，它内部维护连接池。
# from_url 只是构造对象，【不立即连接】；首次执行命令时才连。
redis_client: aioredis.Redis = aioredis.from_url(
    settings.redis_url,
    decode_responses=True,      # 取出来直接是 str，省掉手动 .decode()
    max_connections=20,         # 连接池上限
    health_check_interval=30,   # 空闲连接每 30s 探活一次（相当于 Redis 版的 pool_pre_ping）
)


async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """
    请求级依赖：直接把共享客户端交给调用方。
    连接池在客户端内部管理，所以【不需要】每个请求新建客户端；用 yield 是为了
    将来若有「请求级」逻辑（如按请求开 pipeline）能有挂载点。
    """
    yield redis_client


async def close_redis() -> None:
    """关停时释放连接池"""
    await redis_client.aclose()
