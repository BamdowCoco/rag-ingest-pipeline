"""
AI 接缝：给文档补 language / tags / ai_summary（+ 从正文开头补 author；doc_type 预留给模型）。

铁律：AI 是【可选增强】，不是管道的必经环节。
**任何失败都吞掉、留 NULL** —— 绝不让一篇文档因为 AI 挂了而入库失败。

【位置：为什么现在还留在锁内】`enrich` 跑在分布式锁 + 事务里，但这一轮它只是
**四个纯 Python 函数** （语言检测 / 首段尾段摘要 / jieba 关键词 / 开头署名），
一次几十毫秒；而且锁是 **按身份** 的（只影响"同一篇的并发推"，不是全局）。
所以这一轮 **有意不动它** 。

**接模型服务（阶段 H）时必须重新评估** —— 秒级调用会占着行锁和事务好几秒，
甚至可能超过 `LOCK_TTL_MS` 而中途过期。到那时再谈"移出锁 / 后台异步补"。

【接缝还在哪】`enrich(cleaned, raw) -> dict` 这个形状、调用点、以及「逐项降级」的约定，
就是让 H 只改这一个模块、管道一行不动的原因。
"""
import logging
from collections.abc import Callable
from typing import Any

from app.utils.keywords import extract_keywords
from app.utils.language import detect_language
from app.utils.metadata import extract_author
from app.utils.summary import summarize

logger = logging.getLogger(__name__)

# 规则改了就把这个 +1 —— 与 clean_version 一个思路：得知道哪些行是用旧规则 enrich 的
ENRICH_VERSION = 1

# 降级时统一返回的空值（每次返回副本，避免调用方误改这个共享 dict）
_EMPTY = {
    "doc_type": None,          # 仍留给模型（阶段 H）
    "tags": None,              # 关键词（jieba）
    "ai_summary": None,
    "language": None,
    "author": None,            # 只在调用方没给时才从正文开头补
    "enrich_version": ENRICH_VERSION,
}


def _run_safely(result: dict, key: str, run: Callable[[], Any]) -> None:
    """
    **逐项降级** ：某一项失败只让它自己留 NULL， **绝不阻断入库** 。

    单独抽出来是因为"每一项各管各的 try/except"这件事本身就是设计的一部分 ——
    语言挂了不该连累摘要，摘要挂了不该连累关键词。
    """
    try:
        result[key] = run()
    except Exception:
        logger.exception("enrich 的 %s 失败，留空（不影响入库）", key)


async def enrich(cleaned: dict, raw: dict) -> dict:
    """
    传入 **标准化后的正文** 与 **原始入参** ，返回要写入的那几列。 **绝不抛异常** 。

    为什么还要 `raw`：署名要 **只在调用方没给时才补** ，而 `author` 在 raw 上、不在 cleaned 上。
    """
    text = cleaned.get("content") or ""
    result = dict(_EMPTY)

    _run_safely(result, "language", lambda: detect_language(text))
    _run_safely(result, "ai_summary", lambda: summarize(text))
    # 空列表归一成 None —— 与"没有值就不写"一致
    _run_safely(result, "tags", lambda: extract_keywords(text) or None)

    # 署名：只在调用方没给的时候才从正文开头补，别覆盖上游明确给的值
    if not raw.get("author"):
        _run_safely(result, "author", lambda: extract_author(text))

    return result
