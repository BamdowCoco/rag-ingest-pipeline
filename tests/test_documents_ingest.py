"""单篇摄入： **重复不是错误** （201 → 200），且判重是「先标准化、后哈希」。"""


async def test_first_201_then_200_same_id(client, make_doc):
    doc = make_doc()

    r1 = await client.post("/api/v1/documents", json=doc)
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/documents", json=doc)
    assert r2.status_code == 200

    # 返回的是【已存在那篇】，不是新插一条
    assert r2.json()["id"] == r1.json()["id"]


async def test_normalize_before_hash(client, make_doc):
    """
    同一篇内容、不同的原始空白 → 判为重复。

    这正是「必须先标准化、后哈希」的证据：不先标准化，`"标题"` 与 `" 标题 "`、
    `"第一步    第二步"` 与 `"第一步 第二步"` 会算出 **两个不同的 hash** ，判重直接失效。
    """
    r1 = await client.post(
        "/api/v1/documents",
        json=make_doc(title="部署手册", content="第一步    第二步"),
    )
    assert r1.status_code == 201

    r2 = await client.post(
        "/api/v1/documents",
        json=make_doc(title=" 部署手册 ", content="第一步 第二步"),
    )
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]


async def test_empty_after_normalize_is_422(client, make_doc):
    """原始内容非空、但标准化后为空 → 拒收（422）。"""
    r = await client.post("/api/v1/documents", json=make_doc(content="   <p></p>  "))
    assert r.status_code == 422


async def test_missing_required_field_is_422(client):
    r = await client.post("/api/v1/documents", json={"source": "feishu", "title": "缺正文"})
    assert r.status_code == 422
