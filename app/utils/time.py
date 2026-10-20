"""
时间工具

全项目统一 UTC，且一律返回「naive」（不带 tzinfo时区信息）的 datetime。
原因：MySQL 的 DATETIME 列本身不存时区
约定：库里一律「UTC + naive」，只在给用户展示的边界才转本地时区。
"""
from datetime import date, datetime, time, timedelta, timezone


def utcnow() -> datetime:
    """
    当前 UTC 时间（naive）。

    :return: 不带 tzinfo 的 UTC 时间。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def day_range(d: date) -> tuple[datetime, datetime]:
    """
    返回某天的 [00:00:00, 23:59:59.999999]（naive UTC）。

    统计「今日新增」时用范围查询：fetched_at >= start AND fetched_at <= end，
    比 ``DATE(fetched_at) = ?`` 好 —— 后者会让 fetched_at 上的索引失效。

    :param d: 日期。
    :return: ``(当天 00:00:00, 当天 23:59:59.999999)``，均为 naive UTC。
    """
    return datetime.combine(d, time.min), datetime.combine(d, time.max)


# 中国时区固定 UTC+8（无夏令时）。业务上的「今日」按它算，库里存的仍是 naive UTC。
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_today_utc_range() -> tuple[datetime, datetime]:
    """
    「北京时间的今天」对应的 **UTC** 区间 [start, end]（naive），供范围查询用。

    例：北京 2026-10-14 的一整天
        = 北京 00:00:00 ~ 23:59:59.999999
        = UTC  2026-10-13 16:00:00 ~ 2026-10-14 15:59:59.999999

    别直接用 UTC 的「今天」—— 那会比北京日期差 8 小时。

    :return: 北京时间今天的 UTC 区间 ``(start, end)``，均为 naive UTC。
    """
    today_bj = datetime.now(BEIJING_TZ).date()
    start_bj, end_bj = day_range(today_bj)          # 复用现成的 day_range
    start = start_bj.replace(tzinfo=BEIJING_TZ).astimezone(timezone.utc)
    end = end_bj.replace(tzinfo=BEIJING_TZ).astimezone(timezone.utc)
    return start.replace(tzinfo=None), end.replace(tzinfo=None)
