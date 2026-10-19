"""
数据库瞬时错误的重试。

MySQL 的 **死锁**（1213）与 **锁等待超时**（1205）都是瞬时错误：事务已经被服务端回滚，
重跑通常就成功。不重试它们会一路冒成 HTTP 500 —— 压测里有 90 次 500 全是 1213。
"""
import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

T = TypeVar("T")

# InnoDB：1213 = deadlock，1205 = lock wait timeout
TRANSIENT_MYSQL_CODES = frozenset({1213, 1205})


def mysql_code(exc: OperationalError) -> int | None:
    """从 SQLAlchemy 的 OperationalError 里取出 MySQL 错误码。"""
    args = getattr(getattr(exc, "orig", None), "args", None)
    return args[0] if args and isinstance(args[0], int) else None


async def with_transient_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    session: AsyncSession,
    attempts: int = 5,
    base_delay: float = 0.05,
) -> T:
    """
    执行 ``fn()``；遇到 1213 / 1205 就先回滚、退避后重试。

     **必须先 ``rollback``** ：死锁时 InnoDB 已经把整个事务回滚了，
    SQLAlchemy 的 session 也进入失效状态 —— 不回滚直接重跑会报
    "This session is in 'inactive' state"。

    只放行这两个错误码：其它 ``OperationalError`` 第一次就抛，不掩盖真故障。

    默认 5 次 / 50ms 起：压测并发 16–32 时，3 次预算仍会有约 0.3% 的请求
    连续撞死锁；5 次能把它们吸收掉。最坏情况额外延迟约 0.75s，换来不返 500。
    """
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except OperationalError as exc:
            code = mysql_code(exc)
            await session.rollback()
            if code not in TRANSIENT_MYSQL_CODES or attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random())
            logger.warning(
                "数据库瞬时错误 code=%s，%.0fms 后重试（%d/%d）",
                code, delay * 1000, attempt, attempts,
            )
            await asyncio.sleep(delay)

    raise AssertionError("unreachable：attempts >= 1 时循环内必然 return 或 raise")
