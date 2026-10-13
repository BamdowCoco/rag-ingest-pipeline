"""
文本清洗（纯函数：无 DB / Redis / 网络，容易测）。

按序执行，clean_article() 把整条线串起来，
顺便算出 content_hash —— 保证「先清洗后哈希」
"""
import html
import re
import unicodedata

from app.utils.hashing import make_content_hash

# 清洗规则改了就把版本号 +1，日后能识别某一行是哪个版本洗出来的
CLEAN_VERSION = 1

MAX_TITLE_LEN = 255      # 对应 article_clean.title 的列宽
MAX_CONTENT_LEN = 20000  # 正文上限（列是 Text，这里是业务上自我克制）

# ① 整块删除 <script>…</script> / <style>…</style>
#    必须在「去标签」之前：否则块里的 JS/CSS 会漏出来变成正文
_RE_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
# ② HTML 注释（不删的话注释里的 '>' 会干扰后面的标签正则）
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
# ③ 块级边界 → 换行（保住段落）
_RE_BLOCK_BREAK = re.compile(r"</(?:p|div|li|h[1-6]|tr|section|article|blockquote)>|<br\s*/?>", re.I)
# ④ 其余标签 → 空格（用空格而非空串：'<p>a</p><p>b</p>' 才不会粘成 'ab'）
_RE_TAG = re.compile(r"<[^>]+>")
# ⑤ 零宽 / BOM / 软连字符：肉眼看不见，却会让哈希不一致
_RE_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")

_RE_INLINE_WS = re.compile(r"[^\S\n]+")                 # 非换行的空白（空格/制表/全角空格）
_RE_BLANK_LINES = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")  # 连续空行


def _to_str(value: object) -> str:
    """守卫：非字符串一律当空串（None、数字、缺字段都走这里）。"""
    return value if isinstance(value, str) else ""


def _strip_markup(raw: object) -> str:
    """步骤 ①–⑦：删块 → 删注释 → 块级转换行 → 删标签 → unescape → NFKC → 删零宽。"""
    s = _to_str(raw).strip()
    if not s:
        return ""
    s = _RE_SCRIPT_STYLE.sub(" ", s)          # ①
    s = _RE_COMMENT.sub(" ", s)               # ②
    s = _RE_BLOCK_BREAK.sub("\n", s)          # ③
    s = _RE_TAG.sub(" ", s)                   # ④
    s = html.unescape(s)                      # ⑤ 必须在去标签【之后】
    s = unicodedata.normalize("NFKC", s)      # ⑥ 全角→半角（含全角空格 U+3000）
    s = _RE_INVISIBLE.sub("", s)              # ⑦
    return s


def clean_title(raw: object) -> str:
    """标题：折成单行，截断到 255。"""
    s = _strip_markup(raw)
    s = re.sub(r"\s+", " ", s).strip()        # 所有空白（含换行）折成单个空格
    return s[:MAX_TITLE_LEN]


def clean_content(raw: object) -> str:
    """正文：保留段落 —— 行内空白折成单空格，连续空行最多留一个。"""
    s = _strip_markup(raw)
    s = _RE_INLINE_WS.sub(" ", s)                       # 行内空白 → 单空格
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = _RE_BLANK_LINES.sub("\n\n", s)                  # 连续空行 → 一个空行
    s = s.strip()
    return s[:MAX_CONTENT_LEN]


def clean_article(raw: dict) -> dict:
    """
    清洗编排：raw dict → 清洗后的字段 + 哈希。

    返回 {"title", "content", "char_count", "clean_version", "content_hash"}。
    hash 在这里算，且基于【截断后】的文本 —— 与真正入库的内容完全一致。
    """
    title = clean_title(raw.get("title"))
    content = clean_content(raw.get("content"))
    return {
        "title": title,
        "content": content,
        "char_count": len(content),
        "clean_version": CLEAN_VERSION,
        "content_hash": make_content_hash(title, content),
    }
