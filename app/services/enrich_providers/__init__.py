"""
enrich provider 注册表。

【接缝在哪】``enrich_service.enrich()`` 只做三件事：``off`` 短路 → 按名查 provider → 失败回落规则。
**要接一家新的上游，就加一个文件、在这里注册一行** —— 表结构、状态机、后台循环、调用方都不用动。

【名字从哪来】``settings.enrich_provider`` 用的是 ``Literal`` —— 写错值 **启动就报错** ，
所以这里的 ``get_provider()`` 不需要处理"名字不存在"（那根本到不了运行时）。
"""
from .base import EMPTY, ENRICH_VERSION, OFF, EnrichProvider, run_safely
from .model import ModelProvider
from .openai_compat import OpenAICompatProvider
from .rule import RuleProvider

# 降级链的终点单例 —— facade 回落时直接用它
RULE = RuleProvider()

_PROVIDERS: dict[str, EnrichProvider] = {
    "rule": RULE,
    "model": ModelProvider(),
    "openai_compat": OpenAICompatProvider(),
}


def get_provider(name: str) -> EnrichProvider:
    """按名字取 provider。"""
    return _PROVIDERS[name]


def available() -> list[str]:
    """有哪些 provider 可选 —— 给排错与文档用。"""
    return sorted(_PROVIDERS)


__all__ = [
    "EMPTY",
    "ENRICH_VERSION",
    "OFF",
    "RULE",
    "EnrichProvider",
    "available",
    "get_provider",
    "run_safely",
]
