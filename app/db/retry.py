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
    """
    从 SQLAlchemy 的 OperationalError 里取出 MySQL 错误码。

    :param exc: SQLAlchemy 包过的数据库异常。
    :return: MySQL 错误码（如 1213）；取不到则为 ``None``。
    """
    args = getattr(getattr(exc, "orig", None), "args", None)
    return args[0] if args and isinstance(args[0], int) else None


def is_transient_error(exc: BaseException) -> bool:
    """
    判断是不是「回滚后重跑通常就成功」的瞬时错误（1213 / 1205）。

    :param exc: 捕获到的异常。
    :return: ``True`` 表示属于可重试的瞬时错误。
    """
    return isinstance(exc, OperationalError) and mysql_code(exc) in TRANSIENT_MYSQL_CODES


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

    :param fn: 要执行的零参异步函数（通常是一个包住 ``_apply_identified`` 的 lambda）。
    :param session: 出错时用来 ``rollback`` 的会话；必须是 ``fn`` 使用的同一个。
    :param attempts: 最多尝试几次（含第一次）。
    :param base_delay: 首次退避的秒数；之后指数递增并加随机抖动。
    :return: ``fn`` 的返回值。
    :raises OperationalError: 非瞬时错误，或重试次数耗尽。
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
