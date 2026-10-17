"""
自定契约的 HTTP 上游：``POST {ENRICH_MODEL_URL} {"text": ...} → {"summary", "keywords", "language"}``。

【这一档的真实身份是「内部 AI 网关」】契约由 **我方** 定义 —— 企业里大量项目接的就是自家网关，
而网关的契约本来就是自己定的（它替我们屏蔽厂商差异、统一配额与审计）。
所以这一档不是"假的"，是 **一种真实的形态** ；``model/`` 容器里那个服务才是替身。
厂商各自的形状长什么样、为什么需要适配层，见 ``docs/RAG数据接入与预处理管道项目疑问.md`` 的 Q3。

【失败要抛】解析不出来就抛，由 facade 回落规则 —— **绝不在这里默默留 NULL** ：
"接错了"和"这篇文档没有摘要"是两件事，静默会把它们混成一个。
"""
from typing import Any

from app.core.config import settings
from app.infra.model_client import get_model_client
from app.utils.sample import sample_text

from .base import EMPTY
from .rule import fill_author


class ModelProvider:
    """走我方契约的 HTTP 上游（网关形态）。"""

    name = "model"

    async def enrich(self, text: str, *, author: str | None = None) -> dict[str, Any]:
        # 只发样本（首段 + 尾段 + 长度上限）：摘要本来只看首尾段，整篇发出去是白烧 token
        response = await get_model_client().post(
            settings.enrich_model_url,
            json={"text": sample_text(text, limit=settings.enrich_sample_limit)},
        )
        response.raise_for_status()
        data = response.json()

        result = dict(EMPTY)
        result["ai_summary"] = data.get("summary")
        result["tags"] = data.get("keywords") or None
        result["language"] = data.get("language")
        fill_author(result, text, author)
        return result
