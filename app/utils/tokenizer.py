"""
Token 计数（精确）。

用 tiktoken 实打实编码一次、数 token —— 不再自己估算。
下游受约束的就是 token（模型上下文上限、embedding 计费），所以这里必须准。

【部署提醒】tiktoken 第一次 `get_encoding` 会**联网下载 BPE 词表**并缓存到本地。
容器里为了不在运行时才发现没网，Dockerfile 在**构建期**就预热了一次。
"""
import tiktoken

# GPT-4 / 3.5 系的经典编码，兼容面最广（多数 embedding 模型也用它的算法）。
# 换模型或换语料时改这一行即可；中文为主且下游用新模型时可考虑 o200k_base（更省 token）。
ENCODING_NAME = "cl100k_base"

enc = tiktoken.get_encoding(ENCODING_NAME)


def count_tokens(text: str) -> int:
    """精确 token 数。"""
    if not text:
        return 0
    return len(enc.encode(text))
