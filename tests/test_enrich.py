"""enrich（阶段 G）：语言检测 + 首段/尾段摘要 —— 两个纯函数 + 一条联机断言。

纯函数不碰 DB / Redis / 网络（语言检测只依赖 py3langid 的内置模型），所以它们不用 fixture。
"""
from app.services.enrich_service import ENRICH_VERSION, enrich
from app.utils.language import detect_language
from app.utils.summary import split_paragraphs, summarize


# ---------- detect_language ----------

def test_detects_chinese():
    assert detect_language("这是一段中文文本，用来验证语言检测能不能认出简体中文。") == "zh"


def test_detects_english():
    assert detect_language("This is a paragraph of English text, used to verify detection.") == "en"


def test_too_short_returns_none():
    assert detect_language("") is None
    assert detect_language("你好") is None          # 太短，判出来没意义
    assert detect_language("   \n  ") is None


# ---------- summarize ----------

def test_empty_returns_none():
    assert summarize("") is None
    assert summarize("   \n\n   ") is None


def test_single_paragraph_is_itself():
    assert summarize("只有一段话。") == "只有一段话。"


def test_first_and_last_paragraph():
    text = "第一段：点题。\n\n中间的细节不该进摘要。\n\n最后一段：给出结论。"
    out = summarize(text)
    assert out is not None
    assert out.startswith("第一段：点题。")
    assert out.endswith("最后一段：给出结论。")
    assert "中间的细节" not in out


def test_truncates_to_limit():
    out = summarize("首" * 100 + "\n\n" + "尾" * 100, limit=20)
    assert out is not None
    assert len(out) == 20


def test_split_paragraphs_drops_blank():
    assert split_paragraphs("a\n\n\n\n b \n\n") == ["a", "b"]


# ---------- enrich 编排 ----------

async def test_enrich_fills_all_keys():
    out = await enrich({"content": "这是一段足够长的中文正文，用来验证 enrich 会同时填语言和摘要。"})
    assert out["language"] == "zh"
    assert out["ai_summary"]
    assert out["enrich_version"] == ENRICH_VERSION
    assert set(out) == {"doc_type", "tags", "ai_summary", "language", "enrich_version"}


async def test_enrich_degrades_item_by_item(monkeypatch):
    """语言挂了不该连累摘要 —— 逐项降级，各自留 NULL。"""
    import app.services.enrich_service as mod

    def boom(_text: str) -> str:
        raise RuntimeError("语言检测炸了")

    monkeypatch.setattr(mod, "detect_language", boom)

    out = await mod.enrich({"content": "一段足够长的正文，摘要应该照常生成出来。"})
    assert out["language"] is None            # 挂掉的那项留空
    assert out["ai_summary"]                  # 另一项照常
    assert out["enrich_version"] == ENRICH_VERSION


async def test_ingest_fills_language_and_summary(client, make_doc):
    """联机：POST 一篇多段中文 → 库里 language / ai_summary 都有值，且摘要含尾段。"""
    doc = make_doc(
        source="feishu",
        external_id="enrich-1",
        content="第一步：准备环境。\n\n第二步：执行部署脚本。\n\n最后一步：验证健康检查通过。",
    )
    body = (await client.post("/api/v1/documents", json=doc)).json()

    assert body["language"] == "zh"
    assert body["ai_summary"]
    assert "最后一步" in body["ai_summary"]     # 尾段确实进了摘要
