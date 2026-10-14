"""
去重：Redis Set 是【快路径】，MySQL 唯一索引是【权威】。

快路径挡掉绝大多数重复、省一次数据库往返；但 Redis 会被清空/淘汰/重启丢数据，
不能当真相 —— 最终由 document_dedup.content_hash 的唯一索引说了算。
"""
from redis.asyncio import Redis

DEDUP_KEY = "dedup:documents"   # 存「已见过的 content_hash」的 Set


async def check_and_mark(redis: Redis, content_hash: str) -> bool:
    """
    原子地「判断并占位」：SADD 返回 1 = 之前没有（新文档），0 = 已存在（重复）。

    必须用 SADD 的**返回值**一步判断。写成 `SISMEMBER` 再 `SADD` 会在这两步之间留下
    竞态窗口：两个请求可能同时读到「不存在」，然后都去插入。
    """
    added = await redis.sadd(DEDUP_KEY, content_hash)
    return added == 1


async def rollback_mark(redis: Redis, content_hash: str) -> None:
    """
    落库失败时撤掉快路径的占位。
    不撤的话，Redis 会一直认为「这篇见过」，从此永久拒收它 —— 而且因为权威索引里
    其实没有这条记录，你还会觉得莫名其妙。
    """
    await redis.srem(DEDUP_KEY, content_hash)


DEDUP_REJECTED_KEY = "documents:dedup_rejected"   # 判重拒收累计计数


async def incr_rejected(redis: Redis) -> None:
    """判重拒收时 +1（统计接口读它）。"""
    await redis.incr(DEDUP_REJECTED_KEY)


async def get_rejected_count(redis: Redis) -> int:
    v = await redis.get(DEDUP_REJECTED_KEY)
    return int(v) if v is not None else 0
