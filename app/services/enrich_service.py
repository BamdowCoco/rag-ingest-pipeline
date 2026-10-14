"""
AI 接缝：给文档补 doc_type / tags / ai_summary。

现在是**空实现**，但接口、调用点、降级逻辑都就位了。将来接大模型只需填 `_call_model()`，
表结构（阶段 2 已预留三列）、管道位置、失败处理都不用动。

**铁律：AI 是【可选增强】，不是管道的必经环节。**
任何失败都吞掉、留 NULL —— 绝不让一篇文档因为 AI 挂了而入库失败。

【位置提醒】enrich 目前被调用在**分布式锁内部**（判重之后、落库之前）。今天它是
空实现、耗时为零，所以没问题；但接上真实大模型（秒级）之后，就变成「在锁里做慢调用」：
锁被占住数秒、甚至可能超过 LOCK_TTL_MS 而中途过期。届时的处理选项：
  ① 调大 LOCK_TTL_MS（最省事，代价是崩溃恢复变慢）；
  ② 把 enrich 移到锁外（代价：重复文档会白调一次模型）；
  ③ 先落库（三列留空）、再后台异步补（最像生产做法，需引入后台任务/队列）。
"""
import logging

logger = logging.getLogger(__name__)

# 降级时统一返回的空值（每次返回副本，避免调用方误改这个共享 dict）
_NO_AI = {"doc_type": None, "tags": None, "ai_summary": None}


async def enrich(document: dict) -> dict:
    """
    传入标准化后的文档 dict，返回要写入的三列。

    **绝不抛异常** —— 出任何错都降级为三列全 NULL。
    """
    try:
        return await _call_model(document)
    except Exception:
        logger.exception("enrich 失败，降级为留空（不影响入库）")
        return dict(_NO_AI)


async def _call_model(document: dict) -> dict:
    """
    真正调用模型的地方 —— 将来改这里。

    现在直接返回空值。接大模型时大致形状：
        调模型 → 解析出 doc_type / tags / ai_summary → 返回同名字典
    """
    return dict(_NO_AI)
