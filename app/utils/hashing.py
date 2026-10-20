"""
内容哈希：判重用的指纹。

约定：只对【标准化后】的标题与正文算哈希（先标准化后哈希）。
否则 "标题 " 与 "标题" 会算出两个不同哈希，判重直接失效
—— tests/test_normalize_service.py 里有一条测试专门盯这一点。
"""
import hashlib


def make_content_hash(title: str, content: str) -> str:
    r"""
    sha256(标题 + "\n" + 正文) 的十六进制串（64 字符，正好对应 content_hash 列）。

    中间那个 "\n" 是分隔符：否则 ("ab", "c") 与 ("a", "bc") 会撞成同一个哈希。

    :param title: 标准化后的标题。
    :param content: 标准化后的正文。
    :return: 64 字符的小写十六进制 sha256。
    """
    return hashlib.sha256(f"{title}\n{content}".encode("utf-8")).hexdigest()
