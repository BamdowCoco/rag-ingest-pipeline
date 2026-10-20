"""
切块（纯函数：唯一的外部依赖是 tiktoken 的词表，不碰 DB / Redis / 网络）。

**尺寸用 token 精确控制** —— 不是字符、也不是估算。下游受约束的就是 token。

策略：递归字符分割（Recursive Character Splitting），从「最自然的边界」往下降级：
  ① 段落 ``\\n\\n``     一段话装得下就整段成块
  ② 句子 ``。！？；``  段落超限才退到这一级，尽量把完整句子装进预算
  ③ 单字符         句子还超限（罕见）才硬切 —— 到这一级是 **按 token 切** 的

【将来】若切块参数需要反复试，就该像 ``clean_version`` 那样给 chunk 记一个
``chunker_version`` —— 否则无法知道哪些块是用旧参数切的、该不该重切。
"""
from app.utils.tokenizer import count_tokens, decode_tokens, enc

CHUNK_SIZE = 512                                  # 目标块大小，单位：token
OVERLAP_RATIO = 0.125                             # 相邻块重叠比例（经验区间 10%~15%）
CHUNK_OVERLAP = int(CHUNK_SIZE * OVERLAP_RATIO)   # 64 token

# 分隔符按【语义强度】从强到弱；末尾的 "" 表示退无可退时按 token 硬切
SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "! ", "? ", "; ", ". ", "，", ", ", " ", ""]


def _split_keeping_sep(text: str, sep: str) -> list[str]:
    """
    按 ``sep`` 切开，并把 ``sep`` 留在 **前一片结尾** —— 否则句末标点会被吃掉。

    :param text: 要切的文本。
    :param sep: 分隔符（可以是 ``\\n\\n``、句号等）。
    :return: 切分结果；除最后一片外，每片都以 ``sep`` 结尾。
    """
    parts = text.split(sep)
    return [p + sep for p in parts[:-1]] + [parts[-1]]


def _hard_split_tokens(text: str, size: int) -> list[str]:
    """
    退无可退：按 **token** 硬切（encode → 切片 → decode）。

    :param text: 要切的文本。
    :param size: 每片最多多少 token。
    :return: 切分结果；相邻片之间没有重叠。
    """
    ids = enc.encode(text)
    # decode 必须走 decode_tokens：切片会切在多字节字符中间，直接 decode 会填 `�`（见那个函数的说明）
    return [decode_tokens(ids[i:i + size]) for i in range(0, len(ids), size)]


def _split_recursive(text: str, size: int, separators: list[str]) -> list[str]:
    """
    递归分割：先用 separators[0] 切；哪一片还是超限，就对那一片换下一级分隔符继续切。
    保证返回的每一片不超过 size 个 token。

    :param text: 要切的文本。
    :param size: 每片最多多少 token。
    :param separators: 从强到弱的分隔符列表；空列表表示已退无可退，走 token 硬切。
    :return: 切分结果，每一片都不超过 ``size`` 个 token。
    """
    if count_tokens(text) <= size:
        return [text]
    if not separators:
        return _hard_split_tokens(text, size)

    sep, rest = separators[0], separators[1:]
    if sep == "" or sep not in text:
        return _split_recursive(text, size, rest)            # 这一级用不上 → 降级

    out: list[str] = []
    for piece in _split_keeping_sep(text, sep):
        if count_tokens(piece) <= size:
            out.append(piece)
        else:
            out.extend(_split_recursive(piece, size, rest))  # 这片还大 → 换下一级
    return out


def _token_tail(text: str, n_tokens: int) -> str:
    """
    取 text 的 **最后 n_tokens 个 token** 对应的文本 —— 用来做重叠。

    :param text: 源文本。
    :param n_tokens: 要取多少个尾部 token；``<= 0`` 时返回空串。
    :return: 尾部文本；整段不足 ``n_tokens`` 时返回原文。
    """
    if n_tokens <= 0:
        return ""
    ids = enc.encode(text)
    if len(ids) <= n_tokens:
        return text                     # 整块都不到 n_tokens，就整块给它
    # 同样走 decode_tokens：这一刀最容易切在汉字中间 —— 而它取的文本会成为下一块的开头，
    # 那个 `�` 会直接出现在下一块的第一个字符上（当初就是这么被发现的）
    return decode_tokens(ids[-n_tokens:])


def _merge(pieces: list[str], size: int, overlap: int) -> list[str]:
    """
    把小片贪心合并回接近 size 的块，并让相邻块重叠 overlap 个 token。

    重叠的作用：一句话正好被切在两块之间时，两块里都能看到完整的它 —— 检索才匹配得上。

    :param pieces: 已经切到不超过 ``size`` 的小片。
    :param size: 目标块大小（token）。
    :param overlap: 相邻块重叠的 token 数。
    :return: 合并后的块（已去掉首尾空白与空块）。
    """
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        # 注意：必须对【拼好的整体】编码一次。token 数不是可加的 ——
        # BPE 在拼接处会合并，`count(cur) + count(p)` 会高估。
        if cur and count_tokens(cur + p) > size:
            chunks.append(cur)
            # 新块以当前块尾部 overlap 个 token 开头；但不能长到装不下 p，否则突破上限
            tail_n = min(overlap, max(0, size - count_tokens(p)))
            cur = _token_tail(cur, tail_n)
        cur += p
    if cur:
        chunks.append(cur)
    return [c.strip() for c in chunks if c and c.strip()]


def split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    文本 → 块列表。size / overlap 的单位都是 token。

    边界情况：完全没有分隔符的文本（如无标点的长串）会一路降级到 token 硬切，
    此时块与块 **不会有重叠** —— 没有语义边界时，重叠任意内容收益很低。

    :param text: 要切的文本。
    :param size: 每块最多多少 token。
    :param overlap: 相邻块重叠的 token 数。
    :return: 切分后的块列表；空文本返回空列表。
    :raises ValueError: ``size <= 0`` 或 ``overlap`` 不在 ``[0, size)`` 内。
    """
    if not text or not text.strip():
        return []
    if size <= 0:
        raise ValueError("size 必须为正")
    if not 0 <= overlap < size:
        raise ValueError("overlap 必须满足 0 <= overlap < size")

    pieces = _split_recursive(text.strip(), size, SEPARATORS)
    return _merge(pieces, size, overlap)


def make_chunks(cleaned: dict) -> list[dict]:
    """
    文档（标准化后的 dict）→ chunk 列表，字段正好对应 ``document_chunk`` 的列。

    :param cleaned: ``normalize_document`` 的产物（至少包含 ``content``）。
    :return: 每个元素含 ``chunk_index`` / ``content`` / ``token_count``。
    """
    parts = split_text(cleaned.get("content", ""))
    return [
        {"chunk_index": i, "content": p, "token_count": count_tokens(p)}
        for i, p in enumerate(parts)
    ]
