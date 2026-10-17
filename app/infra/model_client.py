"""
模型服务的 HTTP 客户端（``httpx.AsyncClient``）。

完全照 ``redis_client.py`` 的形状：**模块级单例 + lifespan 关停时释放连接池**。
别每次调用新建客户端 —— 那样每个请求都要重新建连，连接池就白搭了。

【只在 ``provider=model`` 时才会真的被用到】其余时候它只是躺着；
``httpx.AsyncClient`` **构造时不建立任何连接**，所以这里不会拖慢启动。
"""
import httpx

from app.core.config import settings

# 超时跟着配置走：外部依赖必须有超时，否则一个卡住的服务会把后台循环拖死
model_client: httpx.AsyncClient = httpx.AsyncClient(timeout=settings.enrich_timeout_seconds)


def get_model_client() -> httpx.AsyncClient:
    """给调用方拿这个共享客户端用。"""
    return model_client


async def close_model_client() -> None:
    """关停时释放连接池（由 ``app/main.py`` 的 lifespan 调用）。"""
    await model_client.aclose()
