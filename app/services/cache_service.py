"""
缓存：Cache-Aside + 版本号失效。

- 读：先查缓存，命中直接返回；未命中查库，回填并设 TTL。
- 写：不逐个删键，而是 `INCR` 一个全局版本号 —— 键里带 `v{version}`，版本一变，
  所有旧键瞬间「不可达」（O(1) 失效，不必用 `SCAN` 遍历删除那些键）。

代价说清楚：每一次写都至少造成一次必然的 miss（版本变了，下一个读必回源）。
所以命中率大致由读写比决定 ≈ (读次数 − 写次数) / 读次数 —— 读远多于写才划算。
"""
import json
from typing import Any

from redis.asyncio import Redis

_LIST_VERSION_KEY = "documents:list:version"


async def cache_get(redis: Redis, key: str) -> Any | None:
    data = await redis.get(key)
    return None if data is None else json.loads(data)


async def cache_set(redis: Redis, key: str, value: Any, ttl: int) -> None:
    # ensure_ascii=False：中文在 redis-cli 里可读；default=str：datetime 等非 JSON 类型退化成字符串
    await redis.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=ttl)


async def cache_delete(redis: Redis, key: str) -> None:
    await redis.delete(key)


async def get_list_version(redis: Redis) -> int:
    """列表缓存的当前版本号；键不存在时按 0 处理（于是键为 v0）。"""
    v = await redis.get(_LIST_VERSION_KEY)
    return int(v) if v is not None else 0


async def bump_list_version(redis: Redis) -> None:
    """写入成功后调用：版本号 +1，让所有旧的列表缓存瞬间失效。"""
    await redis.incr(_LIST_VERSION_KEY)


def list_cache_key(version: int, *, page: int, limit: int, source: str | None) -> str:
    """
    列表缓存键：包含所有影响结果的参数（page/limit/source）。

    特意【不含 q】：q 是自由文本，键空间无上限 —— 任意客户端能用随机 q 刷出无穷多个键
    把 Redis 撑爆，而且带 q 的请求命中率本来就极低。带 q 的查询直接走库。
    """
    return f"documents:list:v{version}:{page}:{limit}:{source or ''}"


def document_cache_key(document_id: int) -> str:
    """单篇文档的缓存键。"""
    return f"document:{document_id}"


def stats_cache_key(version: int) -> str:
    """统计缓存键。与列表共用同一个版本号 —— 一次 INCR 同时作废两者。"""
    return f"documents:stats:v{version}"


def chunk_list_cache_key(document_id: int, *, page: int, limit: int) -> str:
    """
    分块列表的缓存键。

    **不带版本号** —— chunk 是一次写入、永不修改的，没有「写后失效」这回事，
    TTL 自然过期就够了。这正是「失效策略取决于数据可变性」的正面例子。
    """
    return f"document:{document_id}:chunks:{page}:{limit}"


def document_version_key(source: str, external_id: str) -> str:
    """
    「身份 → 当前版本」的快路径键（阶段 F）。

    存 `{"clean_id": N, "content_hash": "..."}` —— 光有 hash 不够，还得有 id 才能把
    文档本体取出来回给调用方（所以别只存 hash）。

    它挡的是 **最常见的那一路流量** ：同步器重推一篇没变的文档。命中就直接回 200，
    既不用查库，也不用去抢那把 `SELECT ... FOR UPDATE` 行锁。

    **失败模式要说清楚** ：这份缓存错了是 **「漏一次更新」** （重推的新内容被当成没变），
    不是去重 SET 那种「多拒一次」。所以它必须 **write-through** （DB 提交成功后才写），
    并且靠 TTL 兜底 —— Redis 被清空时未命中，落到库路径，结论仍然正确。
    """
    return f"docver:{source}:{external_id}"
