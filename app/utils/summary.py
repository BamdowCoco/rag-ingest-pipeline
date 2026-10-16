"""
摘要（纯函数）。

第一版就是 **首段 + 尾段** —— 不上 textrank、更不上 LLM。

为什么是这两段：中文技术 / 文档类文本 **开头点题、结尾给结论** ，这两段信息密度最高；
而且它 **零依赖、确定性、好写测试** —— 先用最简单的东西把「AI 接缝」跑通。
"""

DEFAULT_LIMIT = 200
_SEPARATOR = " … "


def split_paragraphs(text: str) -> list[str]:
    """按空行切段，顺带把空段丢掉。"""
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def summarize(text: str, *, limit: int = DEFAULT_LIMIT) -> str | None:
    """
    首段 + 尾段，合并后截断到 `limit` 字； **空文本 → None** 。

    只有一段时就取它自己 —— 否则会拼出"首段 … 尾段"这种和原文一模一样的东西。
    """
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return None

    summary = paragraphs[0] if len(paragraphs) == 1 else paragraphs[0] + _SEPARATOR + paragraphs[-1]
    return summary[:limit]
