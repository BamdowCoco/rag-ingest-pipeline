"""
日志配置

用标准库 logging：给根 logger 挂一个控制台 handler。
- 级别从 settings.log_level 取（.env 里 LOG_LEVEL=INFO）
- 统一格式：时间 | 级别 | logger名 | 消息
- 输出到 stdout（容器里 `docker logs` 才能收到；不要打在 stderr 之外的地方）

用法：在 app/main.py 顶部调用一次 setup_logging()，之后各模块
    logger = logging.getLogger(__name__)
    logger.info("...")
"""
import logging
import sys

from app.core.config import settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 模块级标志：保证只配置一次
_configured = False


def setup_logging(level: str | None = None) -> None:
    """
    配置根 logger。重复调用是安全的（第二次直接返回）。

    :param level: 覆盖日志级别（如 "DEBUG"）；不传则用 settings.log_level
    """
    global _configured
    if _configured:
        return

    # setLevel 只认大写名字（"INFO"），.env 里写小写也能用
    root = logging.getLogger()
    root.setLevel((level or settings.log_level).upper())

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(handler)

    _configured = True
