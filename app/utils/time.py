"""
时间工具

全项目统一 UTC，且一律返回「naive」（不带 tzinfo时区信息）的 datetime。
原因：MySQL 的 DATETIME 列本身不存时区
约定：库里一律「UTC + naive」，只在给用户展示的边界才转本地时区。
"""
from datetime import date, datetime, time, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（naive）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def day_range(d: date) -> tuple[datetime, datetime]:
    """
    返回某天的 [00:00:00, 23:59:59.999999]（naive UTC）。

    统计「今日新增」时用范围查询：fetched_at >= start AND fetched_at <= end，
    比 `DATE(fetched_at) = ?` 好 —— 后者会让 fetched_at 上的索引失效。
    """
    return datetime.combine(d, time.min), datetime.combine(d, time.max)
