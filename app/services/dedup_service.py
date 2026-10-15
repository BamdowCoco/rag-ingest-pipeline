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

    必须用 SADD 的 **返回值** 一步判断。写成 `SISMEMBER` 再 `SADD` 会在这两步之间留下
    竞态窗口：两个请求可能同时读到「不存在」，然后都去插入。
    """
    added = await redis.sadd(DEDUP_KEY, content_hash)
    return added == 1


async def check_and_mark_bulk(redis: Redis, hashes: list[str]) -> list[bool]:
    """
    批量「判断并占位」：一次 pipeline 把 N 次 SADD 压成 **一次往返** 。

    返回与 hashes 等长的列表，True = 新增（要落库），False = 已存在（重复）。
    每条 SADD 仍然是 **原子的** —— pipeline 只是把 N 个命令打包一起发出去，
    并没有把它们合成一个非原子操作，所以不存在「SISMEMBER + SADD」那种竞态。
    """
    if not hashes:
        return []
    pipe = redis.pipeline()
    for h in hashes:
        pipe.sadd(DEDUP_KEY, h)
    results = await pipe.execute()
    return [r == 1 for r in results]


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


async def add_rejected(redis: Redis, n: int) -> None:
    """批量判重：一次性 +n —— 而不是循环 N 次 `incr`（省往返，批量入口才有意义）。"""
    if n:
        await redis.incrby(DEDUP_REJECTED_KEY, n)


async def get_rejected_count(redis: Redis) -> int:
    v = await redis.get(DEDUP_REJECTED_KEY)
    return int(v) if v is not None else 0
