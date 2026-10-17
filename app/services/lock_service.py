"""
分布式锁：按 content_hash 粒度，防止同一篇文档被并发重复处理。

三个要点：

 - ``SET key token NX PX ttl`` —— NX 保证互斥，PX 保证「崩溃安全」（持有者挂了锁最终会自己过期）
 - ``token = uuid4().hex`` —— 标识「这把锁是我的」
 - 释放必须用 Lua 原子地「比对 token 再 DEL」
"""
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

from app.core.config import settings

# 释放锁必须原子：比对 token 后才 DEL。
# 为什么不能裸 DEL：若处理耗时超过 TTL，锁已过期、被别人抢到，你的 DEL 会删掉【别人】的锁。
# 为什么不能「先 GET 比对、再 DEL」：那是两次往返，中间仍有窗口，且整体不是原子操作。
# 只有交给 Redis 在服务端原子执行才安全（r.eval，redis-py 会自动 EVALSHA→EVAL 回退）。
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class LockNotAcquired(Exception):
    """没抢到锁：已经有别的请求在处理同一篇文档。"""


def lock_key(lock_id: str) -> str:
    """
    按 ``lock_id`` 粒度上锁 —— 绝不是全局锁，否则并发能力直接归零。

    ``lock_id`` 传什么取决于走哪条路（阶段 F）：

    - 匿名文档（没给 external_id）：传 ``content_hash`` （今天的老行为）
    - 有身份的文档：传身份串（``src:{source}:{external_id}``）

    **有身份时必须按身份锁，不能按内容锁** —— 同一篇文档"改了内容"前后是两个不同的
    hash，按 hash 锁等于各拿各的锁，根本拦不住"同时写同一个身份"。
    """
    return f"lock:doc:{lock_id}"


async def acquire_lock(redis: Redis, key: str, ttl_ms: int) -> str | None:
    """``SET key token NX PX ttl``。抢到返回 token，没抢到返回 None。"""
    token = uuid.uuid4().hex
    ok = await redis.set(key, token, nx=True, px=ttl_ms)
    return token if ok else None


async def release_lock(redis: Redis, key: str, token: str) -> bool:
    """Lua 原子释放：token 对得上才删。返回 True 表示确实删掉了自己的锁。"""
    result = await redis.eval(_RELEASE_LUA, 1, key, token)
    return result == 1



# TODO @asynccontextmanager 作用
#   async with 要求对象实现 异步上下文管理器协议，
#   也就是有 __aenter__ 和 __aexit__ 两个异步方法。
#   @asynccontextmanager 是一个语法糖：让你写一个 async 生成器函数（用 yield），
#   它自动帮你把这个函数包装成一个满足上述协议的对象，而不用手写 __aenter__ / __aexit__。

@asynccontextmanager
async def document_lock(
    redis: Redis, lock_id: str, ttl_ms: int | None = None
) -> AsyncIterator[None]:
    """
    按 ``lock_id`` 加锁的上下文；抢不到锁抛 ``LockNotAcquired``。

        async with document_lock(redis, content_hash):
            ... 「判重 → 落库」的临界区 ...

    ``lock_id`` 可以是 content_hash（匿名文档）或身份串（有身份的文档），见 ``lock_key``。

    TTL 默认取 ``settings.lock_ttl_ms``。TTL 是「崩溃安全」的代价：持有者若真的挂了，
    锁要等 TTL 到期才自动释放——太长拖慢恢复，太短则可能处理没完锁就没了
    （那种情况需要 fencing token，本项目不做）。
    """
    key = lock_key(lock_id)
    token = await acquire_lock(redis, key, ttl_ms or settings.lock_ttl_ms)
    if token is None:
        raise LockNotAcquired(key)
    try:
        yield
    finally:
        # try/finally 保证异常路径（含 409）也会释放；释放用 Lua 保证只删自己的锁
        await release_lock(redis, key, token)
