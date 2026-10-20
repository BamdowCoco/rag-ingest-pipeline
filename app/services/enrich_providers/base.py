"""
enrich 适配层的公共底座：协议 + 版本号 + 空值 + 逐项降级。

【为什么单独一个模块】三个 provider（``rule`` / ``model`` / ``openai_compat``）都要这几样东西，
而它们各自 **只** 依赖这里 —— 于是 facade（``enrich_service``）可以放心 import 注册表， **不会成环** 。

【「形状」指的是什么】各家的 **请求体与响应体** 不同（见 ``openai_compat.py`` 的说明），
但 **provider 的出参形状是统一的** —— 就是下面 ``EMPTY`` 那六把键。
这正是"换 provider 不用改调用方"的原因：``document_repo.mark_enrich_done()`` 拿它直接展开写库。
"""
import logging
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 规则改了就把这个 +1 —— 与 clean_version 一个思路：得知道哪些行是用旧规则 enrich 的
ENRICH_VERSION = 1

# 各字段降级时的空值
EMPTY: dict[str, Any] = {
    "doc_type": None,          # 仍留给模型（本项目到今没填）
    "tags": None,
    "ai_summary": None,
    "language": None,
    "author": None,
    "enrich_version": ENRICH_VERSION,
}

# provider=off：和"做了但都是空"要区分开，所以 enrich_version 写 NULL
OFF: dict[str, Any] = {**EMPTY, "enrich_version": None}


class EnrichProvider(Protocol):
    """
    一个 enrich provider 的形状。

    - ``text`` 是 **正文** ；``author`` 是 **上游给的值** （可能为 None，用来判断该不该从正文开头补署名）
    - 返回那六把键（见 ``EMPTY``）
    - **失败要抛异常** （除了 ``rule``）：facade 靠"抛没抛"来决定要不要走降级链
    """

    name: str

    async def enrich(self, text: str, *, author: str | None = None) -> dict[str, Any]:
        """
        抽取文档元数据。

        :param text: 正文。
        :param author: 上游已经给的署名；``None`` 表示允许 provider 从正文开头补。
        :return: 六项结果的 dict（形状见 ``EMPTY``）。
        :raises Exception: 失败要向上抛，由 facade 决定回落规则。
        """
        ...


def run_safely(result: dict, key: str, run: Callable[[], Any]) -> None:
    """
     **逐项降级** ：某一项失败只让它自己留 NULL， **绝不阻断入库** 。

    单独抽出来是因为"每一项各管各的 try/except"这件事本身就是设计的一部分 ——
    语言挂了不该连累摘要，摘要挂了不该连累关键词。

    :param result: 结果 dict；成功时就地写入 ``key``。
    :param key: 本次要计算的字段名。
    :param run: 返回该字段值的零参函数；抛异常时该字段保持原值（通常是 ``None``）。
    """
    try:
        result[key] = run()
    except Exception:
        logger.exception("enrich 的 %s 失败，留空（不影响入库）", key)
