"""
去重：Redis Set 是【快路径】，MySQL 唯一索引是【权威】。

它管的是 **「新建文档的准入」** —— 「这份内容是不是已经属于别人了」。
（阶段 F 之后，文档身份由 ``document_source`` 回答，那是另一件事。）

快路径挡掉绝大多数重复、省一次数据库往返；但 Redis 会被清空/淘汰/重启丢数据，
不能当真相 —— 最终由 document_dedup.content_hash 的唯一索引说了算。
"""
from redis.asyncio import Redis

DEDUP_KEY = "dedup:documents"   # 存「已见过的 content_hash」的 Set


async def check_and_mark(redis: Redis, content_hash: str) -> bool:
    """
    原子地「判断并占位」：SADD 返回 1 = 之前没有（新文档），0 = 已存在（重复）。

    必须用 SADD 的 **返回值** 一步判断。写成 ``SISMEMBER`` 再 ``SADD`` 会在这两步之间留下
    竞态窗口：两个请求可能同时读到「不存在」，然后都去插入。

    :param redis: 共享 Redis 客户端。
    :param content_hash: 内容指纹。
    :return: ``True`` = 本次新占位（大概率是新内容）；``False`` = 之前已存在（可能重复）。
    """
    added = await redis.sadd(DEDUP_KEY, content_hash)
    return added == 1


async def release_mark(redis: Redis, content_hash: str) -> None:
    """
    撤掉快路径的占位（``SREM``）。两个用途：

    - **落库失败时** ：不撤的话 Redis 会一直认为"这篇见过"，从此永久拒收它 ——
      而权威索引里其实没有这条记录，你还会觉得莫名其妙。
    - **正常释放时** （旧版本被更新 / 删除）：那份内容不再 active，快路径也得跟着放行，
      否则它会一直被当成"见过"。

    :param redis: 共享 Redis 客户端。
    :param content_hash: 要释放的内容指纹。
    """
    await redis.srem(DEDUP_KEY, content_hash)


DEDUP_REJECTED_KEY = "documents:dedup_rejected"   # 判重拒收累计计数


async def incr_rejected(redis: Redis) -> None:
    """
    判重拒收时 +1（统计接口读它）。

    :param redis: 共享 Redis 客户端。
    """
    await redis.incr(DEDUP_REJECTED_KEY)


async def get_rejected_count(redis: Redis) -> int:
    """
    读取「判重拒收」累计计数。

    :param redis: 共享 Redis 客户端。
    :return: 累计计数；键不存在时按 0 处理。
    """
    v = await redis.get(DEDUP_REJECTED_KEY)
    return int(v) if v is not None else 0


async def incr_rejected_by(redis: Redis, n: int) -> None:
    """
    一次加 n（批量入口用）。

    批量提交时如果逐条 ``INCR``，片回退 + 逐篇重放会把同一篇计两次；
    改成「请求结束按最终 duplicates 一次 INCRBY」，口径才准。

    :param redis: 共享 Redis 客户端。
    :param n: 本次要增加的计数；``<= 0`` 时直接跳过。
    """
    if n > 0:
        await redis.incrby(DEDUP_REJECTED_KEY, n)
