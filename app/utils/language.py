"""
语言检测（纯函数）。

为什么要它：下游按语言过滤语料；它也解释了"为什么切块尺寸可以用中文口径"。
每篇文档记一个 `language`，就存在 `document_clean` 上。

用 **py3langid** ：纯 Python，而且 **模型内置在包里** —— 不需要构建期联网预热
（这点和 tiktoken 要下词表、jieba 要加载词典都不一样）。
**不用 `langdetect`** ：它默认带随机性，必须手动 `DetectorFactory.seed = 0` 才稳定，容易踩。
"""
from py3langid import classify

# 太短的文本判出来没有意义（一两个词会被误判），够不着这个长度就直接放弃
MIN_CHARS = 20
# 判语言不需要看完全文 —— 截一下，把单次调用的开销压住
MAX_CHARS = 2000


def detect_language(text: str) -> str | None:
    """
    返回 ISO 639-1 语言码（`zh` / `en` …）； **太短或判不出 → None** 。

    纯函数：只依赖 py3langid 的内置模型，不碰 IO。
    """
    sample = text.strip()[:MAX_CHARS]
    if len(sample) < MIN_CHARS:
        return None

    code, _confidence = classify(sample)
    return code or None
