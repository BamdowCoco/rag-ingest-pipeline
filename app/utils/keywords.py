"""
关键词抽取（纯函数）。

用 **jieba 的 TF-IDF** ：
 - TF（词频）：一个词在文档里出现越多，越重要。
 - IDF（逆文档频率）：一个词在很多文档里都出现，越不重要（如"的"、"是"）。
 - 它自带中文词典与 idf 表，抽出来的是"成词"的词而不是单字（"的"、"是"当标签没任何用）。
 - 英文词也会作为 token 出来（不在 idf 表里就按默认权重算），所以中英混排不用另外兜底。

【部署】jieba 首次调用要 **加载词典** （秒级）。所以 Dockerfile 在 **构建期** 预热一次，
别让线上第一个请求付这个代价 —— 和 tiktoken 词表是同一个坑。
"""
import jieba.analyse

DEFAULT_TOPN = 8


def extract_keywords(text: str, *, topn: int = DEFAULT_TOPN) -> list[str]:
    """
    TF-IDF 抽关键词， **去空、去重（保序）** ；文本为空 → `[]` 。

    返回列表而不是 `None`：把"没有"归一成 `None` 是调用方（enrich）的事 ——
    它才知道那几列怎么写。
    """
    if not text.strip():
        return []

    seen: set[str] = set()
    result: list[str] = []
    for tag in jieba.analyse.extract_tags(text, topK=topn):
        word = tag.strip()
        if word and word not in seen:
            seen.add(word)
            result.append(word)
    return result
