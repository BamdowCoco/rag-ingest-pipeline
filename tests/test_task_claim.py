"""任务认领 / 回报（单线程语义；并发语义见 test_skip_locked.py）。"""


async def test_claim_then_complete(client, make_doc):
    await client.post("/api/v1/documents", json=make_doc())

    r = await client.post("/api/v1/chunks/claim", json={"limit": 10})
    assert r.status_code == 200
    chunks = r.json()["chunks"]
    assert len(chunks) >= 1
    # worker 拿到的是任务单元：这几个字段就够它干活了
    assert set(chunks[0]) == {"id", "document_id", "chunk_index", "content", "token_count"}

    # 认领过的已置 processing，再认领拿不到
    again = await client.post("/api/v1/chunks/claim", json={"limit": 10})
    assert again.json()["chunks"] == []

    cid = chunks[0]["id"]
    done = await client.post(f"/api/v1/chunks/{cid}/complete")
    assert done.status_code == 200
    assert done.json() == {"id": cid, "embedding_status": "done"}


async def test_complete_unknown_id_is_404(client):
    r = await client.post("/api/v1/chunks/999999/complete")
    assert r.status_code == 404


async def test_claim_limit_out_of_range_is_422(client):
    assert (await client.post("/api/v1/chunks/claim", json={"limit": 0})).status_code == 422
    assert (await client.post("/api/v1/chunks/claim", json={"limit": 999})).status_code == 422
