"""
时间工具

全项目统一 UTC，且一律返回「naive」（不带 tzinfo时区信息）的 datetime。
原因：MySQL 的 DATETIME 列本身不存时区
约定：库里一律「UTC + naive」，只在给用户展示的边界才转本地时区。
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（naive）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
