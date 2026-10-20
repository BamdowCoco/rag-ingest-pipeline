"""
Token 计数（精确）。

用 tiktoken 实打实编码一次、数 token —— 不再自己估算。
下游受约束的就是 token（模型上下文上限、embedding 计费），所以这里必须准。

【部署提醒】tiktoken 第一次 ``get_encoding`` 会 **联网下载 BPE 词表** 并缓存到本地。
容器里为了不在运行时才发现没网，Dockerfile 在 **构建期** 就预热了一次。
"""
import tiktoken

# GPT-4 / 3.5 系的经典编码，兼容面最广（多数 embedding 模型也用它的算法）。
# 换模型或换语料时改这一行即可；中文为主且下游用新模型时可考虑 o200k_base（更省 token）。
ENCODING_NAME = "cl100k_base"

enc = tiktoken.get_encoding(ENCODING_NAME)


def count_tokens(text: str) -> int:
    """
    精确 token 数。

    :param text: 任意文本。
    :return: token 数；空文本为 0。
    """
    if not text:
        return 0
    return len(enc.encode(text))


def decode_tokens(ids: list[int]) -> str:
    """
    把 token 子序列解回文本。 **按 token 切片时务必用它** —— 别直接 ``enc.decode`` 。

    为什么：tiktoken 的 ``decode`` 遇到 **不完整的 UTF-8 序列** 会默认填 ``U+FFFD``（就是 ``�``），
    而"按 token 切片"天然会切在 **多字节字符中间** —— 中文尤其常见（一个汉字常跨 2 个 token）。
    实测：同一句中文取最后 12 个 token，默认解出 ``'�到其他的…'``，用 ``errors="ignore"`` 解出干净的 ``'到其他的…'``。

    这里用 ``errors="ignore"`` 把那半个字符 **丢掉** ：宁可少一个字符，也不要往库里写一个 ``�``
    —— 它进 embedding 就是个噪声 token，还可能被检索命中（真事：``docs/问题记录.md``）。

    :param ids: token id 子序列。
    :return: 解码后的文本；不完整的 UTF-8 字节会被丢弃。
    """
    return enc.decode(ids, errors="ignore")
