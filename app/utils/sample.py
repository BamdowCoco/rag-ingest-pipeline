"""
给模型看的 **样本** （纯函数）。

【为什么不能发整篇】摘要本来只用首尾段（规则侧就是"首段 + 尾段"），而调用是按 **输入全长** 计费的 ——
整篇发出去，长文档就是 **白烧 token** 。所以发之前先取样本。

【为什么是首尾段】与 ``summary.py`` 同一个理由：中文技术 / 文档类文本 **开头点题、结尾给结论** ，
这两段信息密度最高。超长（很少见）时退化成"头一半 + 标记 + 尾一半"，仍然保住两头。
"""
from app.utils.summary import split_paragraphs

# 中间确实被丢了的时候才写它 —— 否则是在对模型说假话（"这里缺内容"）。
# 用明确的文字而不是"…"：省略号会混进正文里，让"这段摘要是模型给的还是规则给的"
# 变得看不出来（验收时吃过这个亏 —— 规则侧的分隔符恰好也是"…"）。
_OMITTED = "[中间省略]"
_PARA_SEP = "\n\n"
_OMITTED_SEP = f"\n\n{_OMITTED}\n\n"


def _two_ends(text: str, limit: int) -> str:
    """头一半 + ``[中间省略]`` + 尾一半；``limit`` 太小放不下标记时退化成前缀。"""
    if limit <= len(_OMITTED):
        return text[:limit]
    keep = limit - len(_OMITTED)
    head_len = keep // 2
    tail_len = keep - head_len
    return f"{text[:head_len]}{_OMITTED}{text[-tail_len:]}"


def sample_text(text: str, *, limit: int) -> str:
    """
    取"首段 + 尾段"，超过 ``limit`` 字就两头截； **空文本 → 空串** 。

    结果 **永不超过** ``limit`` 字 —— 上限是给钱包设的，不能失守。
    """
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return ""

    if len(paragraphs) == 1:
        body = paragraphs[0]
    elif len(paragraphs) == 2:
        # 两段之间什么都没丢 —— 老实拼起来，不写省略标记
        body = f"{paragraphs[0]}{_PARA_SEP}{paragraphs[-1]}"
    else:
        body = f"{paragraphs[0]}{_OMITTED_SEP}{paragraphs[-1]}"

    return body if len(body) <= limit else _two_ends(body, limit)
