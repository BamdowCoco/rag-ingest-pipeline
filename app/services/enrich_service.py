"""
AI 接缝：给文档补 language / tags / ai_summary / author（``doc_type`` 仍预留给模型）。

铁律：AI 是【可选增强】，不是管道的必经环节。
**任何失败都吞掉、留 NULL** —— 绝不让一篇文档因为 AI 挂了而入库失败。

【位置：它已经不在请求路径上了】阶段 H 起，``enrich`` **由后台循环调用**
（``services/enricher.py``），而不是在 ``ingest`` 里同步跑。原因见那个文件 ——
一句话：模型调用是 **秒级外部依赖** ，而 ``bulk_ingest`` 是逐篇的，
留在请求路径里会让"同步器一次推 200 篇"变成 10 分钟。

**【可插拔】provider 四档（ ``settings.enrich_provider`` ）** ：

 - ``rule``：纯 Python 那四项（默认； **也是其余档失败后的回落目标** ）
 - ``model``：自定契约的 HTTP 上游（ **网关形态** —— 契约由我方定义）
 - ``openai_compat``： **OpenAI 兼容协议** （自建 vLLM / Ollama 与不少云厂商的兼容端点都是它）
 - ``off``：什么都不做，几列全 NULL（``enrich_version`` 也 NULL = "根本没做过"）

**本模块只负责分派与降级** —— 各家 **请求 / 响应形状** 的"翻译"在 ``enrich_providers/``：
一个 provider 一个文件，接第三家厂商不用动这里，更不用动管道。

【接缝在哪】``enrich(cleaned, raw) -> dict`` 这个形状没变 —— 换 provider 只动 ``enrich_providers/``。
"""
import logging

from app.core.config import settings
from app.services.enrich_providers import ENRICH_VERSION, OFF, RULE, get_provider

__all__ = ["ENRICH_VERSION", "enrich"]

logger = logging.getLogger(__name__)


async def enrich(cleaned: dict, raw: dict) -> dict:
    """
    按 ``settings.enrich_provider`` 分派。 **绝不抛异常** 。

    ``raw`` 是给署名用的：要判断"调用方给没给 author"。
    """
    provider_name = settings.enrich_provider

    if provider_name == "off":
        logger.debug("enrich 已关闭（provider=off），留空")
        return dict(OFF)

    # provider 只需要这两样 —— 不把 payload 的形状泄进适配层
    text = cleaned.get("content") or ""
    author = raw.get("author")
    provider = get_provider(provider_name)

    if provider_name == "rule":
        # 规则档从不抛（逐项 run_safely），没必要包 try —— 也不该自己回落到自己
        return await provider.enrich(text, author=author)

    try:
        return await provider.enrich(text, author=author)
    except Exception:
        # 降级链的第一跳：上游不可用 → 回落到纯 Python
        # （超时 / 4xx / 5xx / 解析失败都走这里 —— 这就是"外部依赖绝不拦住主流程"的落点）
        logger.exception("provider=%s 失败 → 回落到规则", provider_name)
        return await RULE.enrich(text, author=author)
