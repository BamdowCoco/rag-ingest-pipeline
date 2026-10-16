"""
AI 接缝：给文档补 language / tags / ai_summary（doc_type 仍预留给模型）。

铁律：AI 是【可选增强】，不是管道的必经环节。
**任何失败都吞掉、留 NULL** —— 绝不让一篇文档因为 AI 挂了而入库失败。

【位置：为什么现在还留在锁内】`enrich` 跑在分布式锁 + 事务里，但这一轮它只是
**两个纯 Python 函数**（语言检测 + 首段尾段摘要），一次几十毫秒；而且锁是 **按身份** 的
（只影响"同一篇的并发推"，不是全局）。所以这一轮 **有意不动它**。

**接模型服务（阶段 H）时必须重新评估** —— 秒级调用会占着行锁和事务好几秒，
甚至可能超过 `LOCK_TTL_MS` 而中途过期。到那时再谈"移出锁 / 后台异步补"。

【接缝还在哪】`enrich(document) -> dict` 这个形状、调用点、以及「逐项降级」的约定，
就是让 H 只改这一个模块、管道一行不动的原因。
"""
import logging

from app.utils.language import detect_language
from app.utils.summary import summarize

logger = logging.getLogger(__name__)

# 规则改了就把这个 +1 —— 与 clean_version 一个思路：得知道哪些行是用旧规则 enrich 的
ENRICH_VERSION = 1

# 降级时统一返回的空值（每次返回副本，避免调用方误改这个共享 dict）
_EMPTY = {
    "doc_type": None,          # 仍留给模型（阶段 H）
    "tags": None,              # 关键词（jieba）是下一轮的事
    "ai_summary": None,
    "language": None,
    "enrich_version": ENRICH_VERSION,
}


async def enrich(document: dict) -> dict:
    """
    传入标准化后的文档 dict，返回要写入的那几列。 **绝不抛异常** 。

    **逐项降级**：每一项各管各的 `try/except` —— 语言挂了不该连累摘要。
    某一项失败只让它自己留 NULL，**绝不阻断入库**。
    """
    text = document.get("content") or ""
    result = dict(_EMPTY)

    for key, detect in (("language", detect_language), ("ai_summary", summarize)):
        try:
            result[key] = detect(text)
        except Exception:
            logger.exception("enrich 的 %s 失败，留空（不影响入库）", key)

    return result
