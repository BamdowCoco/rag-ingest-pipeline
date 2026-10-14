"""切块服务测试（纯函数：不碰 DB / Redis / 网络，只依赖 tiktoken 词表）。"""
import pytest

from app.services.chunk_service import CHUNK_OVERLAP, CHUNK_SIZE, make_chunks, split_text
from app.utils.tokenizer import count_tokens


def test_empty_returns_no_chunks():
    assert split_text("") == []
    assert split_text("   \n\n  ") == []
    assert split_text(None) == []


def test_short_text_is_one_chunk():
    assert split_text("只有一小段。") == ["只有一小段。"]


def test_long_text_splits_and_respects_size():
    """尺寸单位是 token —— 所以断言要比 token，不是比字符数。"""
    parts = split_text("啊" * 3000)
    assert len(parts) > 1
    assert all(count_tokens(p) <= CHUNK_SIZE for p in parts)


def test_adjacent_chunks_overlap():
    """核心：相邻块必须重叠 —— 否则被切断的语义两块都不完整。"""
    parts = split_text("这是完整的一句话。" * 100)
    assert len(parts) > 1
    assert parts[0][-20:] in parts[1]


def test_paragraph_boundary_preferred():
    """第一级：段落边界优先 —— 有段落时，块尾应落在完整段落上。"""
    text = "\n\n".join("段落" * 30 for _ in range(10))
    parts = split_text(text)
    assert len(parts) > 1
    assert all(p.endswith("段落") for p in parts[:-1])


def test_paragraph_longer_than_budget_falls_back_to_sentences():
    """第二级：单段超限 → 退到句子边界切。"""
    para = "。".join(["这句话不长"] * 300)          # 单个段落，远超 512 token
    parts = split_text(para)
    assert len(parts) > 1
    assert all(count_tokens(p) <= CHUNK_SIZE for p in parts)


def test_no_separator_falls_back_to_token_hard_split():
    """第三级：没有任何分隔符 → 按 token 硬切，且不抛错（此时不做重叠）。"""
    # 连续相同字符在 cl100k 里 token 效率很高（`"a"*3000` 只有 375 token），
    # 所以这里要放得足够长 —— 顺便自证「前提确实超预算」。
    text = "a" * 8000
    assert count_tokens(text) > CHUNK_SIZE
    parts = split_text(text)
    assert len(parts) > 1
    assert all(count_tokens(p) <= CHUNK_SIZE for p in parts)


def test_make_chunks_shape():
    chunks = make_chunks({"content": "啊" * 3000})
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert all(c["token_count"] > 0 for c in chunks)
    assert all(c["token_count"] <= CHUNK_SIZE for c in chunks)


def test_bad_params_rejected():
    with pytest.raises(ValueError):
        split_text("x", size=0)
    with pytest.raises(ValueError):
        split_text("x", size=100, overlap=100)


def test_count_tokens():
    """只断言【行为】，不写死数字 —— cl100k 对中文的切分并不直观。"""
    assert count_tokens("") == 0
    assert count_tokens("hello world") > 0
    # 确定性：同样的输入永远同样的 token 数
    assert count_tokens("中文测试" * 10) == count_tokens("中文测试" * 10)
    # 单调：文本越长 token 越多
    assert count_tokens("中" * 100) > count_tokens("中" * 10)
    # 中英都给非零
    assert count_tokens("中") >= 1
