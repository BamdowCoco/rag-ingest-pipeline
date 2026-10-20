"""
OpenAI 兼容协议的适配器：``POST {ENRICH_OPENAI_BASE_URL}/v1/chat/completions``。

【这一档覆盖两类上游】自建推理（vLLM / TGI / Ollama）与不少云厂商的 **兼容端点** 讲的都是这个协议 ——
所以一个实现就够起步。 **厂商差异被关在这一个文件里** ，接第三家、第四家不再动主流程。

【和 ``model.py`` 的区别就是「形状」—— 这正是适配层要翻译的东西】

 - **请求体** ：我们是 ``{"text": ...}``；它要 ``messages`` 数组（system + user， **prompt 得自己拼** ）
 - **结果在哪** ：我们的在顶层 ``summary``；它的藏在 ``choices[0].message.content`` 里，
   而且是一段 **字符串** （还得再 ``json.loads`` 一次）
 - **长度怎么控** ：我们由上游自己定；它靠 ``max_tokens`` （不填有些实现会用很宽的默认值）
 - **结束原因** ：我们没有；它有 ``finish_reason`` （``length`` = 被截断了，要告警）
 - **记账** ：我们没有；它有 ``usage`` （按 token 计费的唯一依据）

> 注：这里的对照 **特意用逐条列表而不是表格** —— docstring 是 reStructuredText，
> 管道表格在 RST 里渲染不出来（见 memory 的「RST 体例」）。

【``response_format`` 做成了 **默认关闭** 的开关（``ENRICH_JSON_MODE``）】开着它，模型更不容易跑偏；
但它 **不是所有兼容实现都支持** （vLLM / Ollama 各版本参差不齐），不支持的服务会直接 **400 → 整档不可用** ——
失败模式太硬，所以默认关。关着时靠"prompt 里要求纯 JSON + 解析时剥代码围栏"兜底，代价是偶尔要剥壳。
**注意** ：开它的厂商（DeepSeek / 通义 / GLM）要求 prompt 里出现 ``json`` 字样，否则直接报错 ——
我们的系统提示里已写"只输出一个 JSON 对象"，改 prompt 时别把这句删了。
（RST 不允许把代码段嵌在加粗里面，所以这句的加粗只圈住"注意"。）
"""
import json
import logging
import re
from typing import Any

from app.core.config import settings
from app.infra.model_client import get_model_client
from app.utils.sample import sample_text

from .base import EMPTY
from .rule import fill_author

logger = logging.getLogger(__name__)

# 只认这三个字段；缺了就留空 —— 不编造
_SYSTEM_PROMPT = (
    "你在做文档元数据抽取。只输出一个 JSON 对象，不要解释、不要加代码围栏。"
    '格式：{"summary": "一句话摘要", "keywords": ["关键词1", "关键词2"], "language": "zh 或 en"}'
)

# 有些实现仍会把 JSON 包在 ```json … ``` 里 —— 剥掉再解析，比赌它听话便宜
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def strip_fence(content: str) -> str:
    """剥掉代码围栏（有则剥、没有就原样）。

    公开而非私有：`scripts/probe_upstream.py` 也用同一个函数去判"上游给的是不是合法 JSON" ——
    两边判据必须一致，否则探针说"不合法"、管道却照收，看着像 bug。

    :param content: 上游返回的文本（可能带 ```` ```json ```` 围栏）。
    :return: 去掉围栏并 strip 后的文本。
    """
    return _FENCE.sub("", content.strip()).strip()


class OpenAICompatProvider:
    """走 OpenAI 兼容协议的 HTTP 上游。"""

    name = "openai_compat"

    async def enrich(self, text: str, *, author: str | None = None) -> dict[str, Any]:
        """
        调 OpenAI 兼容的 chat/completions 端点。

        :param text: 正文；发出去前会截成首段+尾段样本。
        :param author: 上游已经给的署名；非空时不覆盖。
        :return: 六项结果的 dict。
        :raises Exception: 上游不可用、响应结构不对、content 不是合法 JSON 等失败都向上抛。
        """
        sample = sample_text(text, limit=settings.enrich_sample_limit)
        payload: dict[str, Any] = {
            "model": settings.enrich_model_name,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": sample},
            ],
            # 抽取任务要确定性 —— 同一个样本给同一个答案（测试也好写）
            "temperature": 0,
            # 摘要不该无限长；它同时是成本闸门
            "max_tokens": settings.enrich_max_tokens,
        }
        # 端点路径可配（默认 `/v1/chat/completions`）—— 各家不一定一样，
        # 比如智谱 GLM 是 `/api/paas/v4/chat/completions`。两侧都归一，免得用户写成 `v1/...`
        url = f"{settings.enrich_openai_base_url.rstrip('/')}/{settings.enrich_openai_path.lstrip('/')}"
        if settings.enrich_json_mode:
            # 让上游按 JSON 约束输出（多数厂商支持；不支持会 400 → 由 facade 回落规则）
            payload["response_format"] = {"type": "json_object"}

        # 自建推理（Ollama / vLLM）不需要 key；云厂商的兼容端点才需要 —— 所以 key 是可选的
        headers = (
            {"Authorization": f"Bearer {settings.enrich_api_key}"} if settings.enrich_api_key else None
        )

        response = await get_model_client().post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

        # 【为什么这里失败要抛】下面几步任意一步不成立（没有 choices、content 不是 JSON、字段不对），
        # 都说明"接法不对"，而不是"这篇文档没有摘要"。抛出去 → facade 回落规则 → 几列仍有值。
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        parsed = json.loads(strip_fence(content))

        if choice.get("finish_reason") == "length":
            # 撞上 max_tokens：通常 json.loads 已经因为截断的 JSON 抛了，
            # 真走到这说明它恰好是个合法前缀 —— 仍要告警：摘要可能不完整
            logger.warning("openai_compat 的输出撞到 max_tokens，结果可能被截断")

        # 记账：这是"成本花在哪"的唯一数据源（假服务给的是粗略估算，真上游给的是真值）
        usage = data.get("usage") or {}
        logger.info(
            "enrich(openai_compat, model=%s) input=%s output=%s tokens",
            settings.enrich_model_name,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )

        result = dict(EMPTY)
        result["ai_summary"] = parsed.get("summary")
        result["tags"] = parsed.get("keywords") or None
        result["language"] = parsed.get("language")
        fill_author(result, text, author)
        return result
