"""
纯 Python 规则档（默认，也是 **降级链的终点** ）。

四项都零网络依赖：语言靠 ``py3langid`` 内置模型、关键词靠 ``jieba`` 本地词典、摘要与署名是自己写的纯函数。
所以这一档 **永远可用** —— 这正是它能当降级终点、也适合当默认档的原因。
"""
from typing import Any

from app.utils.keywords import extract_keywords
from app.utils.language import detect_language
from app.utils.metadata import extract_author
from app.utils.summary import summarize

from .base import EMPTY, run_safely


def fill_author(result: dict[str, Any], text: str, given: str | None) -> None:
    """
    署名： **只在调用方没给的时候** 才从正文开头补，别覆盖上游明确给的值。

    两个 HTTP 档也调它 —— 模型对署名没有额外信息：那是" **只看开头几行** "的活
    （正文中间出现的"作者："很可能只是在引用别人， **猜错比留空更糟** ）。

    :param result: 结果 dict；需要补署名时就地写入 ``author``。
    :param text: 正文，用于从开头几行识别署名。
    :param given: 上游已经给的署名；非空时不覆盖。
    """
    if not given:
        run_safely(result, "author", lambda: extract_author(text))


class RuleProvider:
    """纯 Python 那四项。 **从不抛异常** —— 逐项 ``run_safely`` 各自兜住。"""

    name = "rule"

    async def enrich(self, text: str, *, author: str | None = None) -> dict[str, Any]:
        """
        用纯 Python 规则补语言 / 摘要 / 关键词 / 署名。

        :param text: 正文。
        :param author: 上游已经给的署名；非空时不覆盖。
        :return: 六项结果的 dict。
        """
        result = dict(EMPTY)

        run_safely(result, "language", lambda: detect_language(text))
        run_safely(result, "ai_summary", lambda: summarize(text))
        # 空列表归一成 None —— 与"没有值就不写"一致
        run_safely(result, "tags", lambda: extract_keywords(text) or None)
        fill_author(result, text, author)

        return result
