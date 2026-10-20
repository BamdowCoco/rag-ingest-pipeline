"""
元数据抽取（纯函数）—— **只抽署名，而且只看开头** 。

为什么这么保守：从正文里正则猜元数据，难的不是写不出来，而是 **错了没人知道** 。
``作者：李白`` 若是出自 **引用别人的文档** ，我们就把 author 填成了错的人 ——
而 **错值比 NULL 更糟** ：NULL 是"不知道"，错值是"知道"，下游会当真。

所以两条自我设限：

 - **只看开头几行** ：那里的署名基本就是这篇文档自己的署名；
 - **日期不抽** ：正文里的日期未必是 ``doc_updated_at`` （ **源文档** 的更新时间）——
   一句"本规范自 2026-03-05 起施行"会被当成"文档更新于那天"，语义最容易错、收益又最小。

（"编号"也没抽：表里根本没有对应的列，抽出来没地方放。）
"""
import re

HEAD_LINES = 5          # 只看正文开头这几行
MAX_AUTHOR_LEN = 120    # 对齐 `author` 列的长度

# 行首的强署名标记：`作者：X` / `作者: X` / `撰文：X` / `文/ X`
_AUTHOR_RE = re.compile(r"^\s*(?:作者|撰文|文)\s*[:：/]\s*(?P<name>\S.{0,60})$")


def extract_author(text: str, *, head_lines: int = HEAD_LINES) -> str | None:
    """
    在正文 **开头几行** 里找署名；找不到 → ``None`` 。

    **故意不全文扫** —— 正文中间出现的"作者："很可能只是在引用别人的东西。

    :param text: 正文。
    :param head_lines: 只看开头多少行。
    :return: 识别到的署名；找不到则 ``None``。
    """
    for line in text.splitlines()[:head_lines]:
        if not line.strip():
            continue
        match = _AUTHOR_RE.match(line)
        if match:
            return match.group("name").strip()[:MAX_AUTHOR_LEN]
    return None
