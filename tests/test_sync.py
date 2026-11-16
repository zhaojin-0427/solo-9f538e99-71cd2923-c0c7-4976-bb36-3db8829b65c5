"""供数同步 API 的端到端关键场景。

覆盖：
- 首次快照（空库 / 含数据）与快照-水位一致性；
- 按水位续读、同水位重复请求幂等、分页不漏组不重组；
- 更正先整组撤下旧供数，当前修订结果生成后再发布新归属；
- 显式对旧修订生成结果不改变同步内容；
- 同一修订重复生成（归属未变）不产生同步组；
- 撤下/新归属的字段（批次号+观测编号定位、接受名、结果号）；
- 失败不留半组、历史结果仍可查。
"""
import pytest

from app import sync


def _batch(client, observations, note=None):
    return client.post("/batches", json={"observations": observations, "note": note})


def _taxonomy(client, accepted, edges=None, note=None):
    return client.post(
        "/taxonomies",
        json={"accepted_names": accepted, "synonym_edges": edges or [], "note": note},
    )


OBS = [
    {"code": "o1", "name": "旧名甲"},   # v1/v2 均纳入 -> 接受名A
    {"code": "o2", "name": "分叉名"},   # v1 分叉待核，v2 更正后纳入 -> 接受名A
    {"code": "o3", "name": "环名x"},    # 恒待核
    {"code": "o4", "name": "无人名"},   # 恒待核
    {"code": "o5", "name": "菱形名"},   # 纳入 -> 接受名A
    {"code": "o6", "name": "接受名A"},  # 纳入 -> 接受名A
]

EDGES_V1 = [
    {"synonym": "旧名甲", "target": "中间名"},
    {"synonym": "中间名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名B"},
    {"synonym": "环名x", "target": "环名y"},
    {"synonym": "环名y", "target": "环名x"},
    {"synonym": "菱形名", "target": "s1"},
    {"synonym": "菱形名", "target": "s2"},
    {"synonym": "s1", "target": "接受名A"},
    {"synonym": "s2", "target": "接受名A"},
]


def _snapshot(client):
    r = client.get("/sync/supply")
    assert r.status_code == 200
    return r.json()


def _changes(client, after, limit=100):
    r = client.get("/sync/changes", params={"after": after, "limit": limit})
    assert r.status_code == 200
    return r.json()


def _locator_set(supply):
    return {(item["batch_id"], item["observation_code"]) for item in supply}


def test_snapshot_empty_when_no_supply(client):
    body = _snapshot(client)
    assert body == {"watermark": 0, "supply_count": 0, "supply": []}
    # 无组可领：水位保持 0，重复请求一致
    body = _changes(client, 0)
    assert body["watermark"] == 0
    assert body["has_more"] is False
    assert body["groups"] == []
    assert body["group_count"] == 0 and body["change_count"] == 0


def test_first_generation_publishes_snapshot_and_upsert_group(client):
    bid = _batch(client, OBS).json()["id"]
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1, note="v1")
    r1 = client.post("/results", json={"batch_id": bid}).json()

    snap = _snapshot(client)
    assert snap["supply_count"] == 3  # o1, o5, o6
    assert _locator_set(snap["supply"]) == {(bid, c) for c in ("o1", "o5", "o6")}
    for item in snap["supply"]:
        assert item["accepted_name"] == "接受名A"
        assert item["result_id"] == r1["result_id"]

    # 水位 0 续读：一个 generation 组，3 条 upsert，组水位 = 快照水位
    body = _changes(client, 0)
    assert body["after"] == 0
    assert body["group_count"] == 1
    group = body["groups"][0]
    assert group["group_type"] == "generation"
    assert group["batch_id"] == bid
    assert group["trigger_result_id"] == r1["result_id"]
    assert group["watermark"] == snap["watermark"] == body["watermark"]
    assert {c["change_type"] for c in group["changes"]} == {"upsert"}
    assert {c["observation_code"] for c in group["changes"]} == {"o1", "o5", "o6"}
    for c in group["changes"]:
        assert c["accepted_name"] == "接受名A"
        assert c["result_id"] == r1["result_id"]
        assert c["batch_id"] == bid

    # 同水位重复请求返回完全相同的一组
    again = _changes(client, 0)
    assert again == body
    # 已读到最新水位后再请求：无新组，水位回显请求水位
    caught_up = _changes(client, body["watermark"])
    assert caught_up["groups"] == []
    assert caught_up["watermark"] == body["watermark"]


def test_correction_withdraws_then_regeneration_publishes_new_attribution(client):
    bid = _batch(client, OBS).json()["id"]
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1, note="v1")
    r1 = client.post("/results", json={"batch_id": bid}).json()
    wm1 = _snapshot(client)["watermark"]

    # 更正：分叉名在 v2 只指向 A -> 旧供数先整组撤下
    edges_v2 = [e for e in EDGES_V1 if not (
        e["synonym"] == "分叉名" and e["target"] == "接受名B"
    )]
    rev2 = _taxonomy(client, ["接受名A", "接受名B"], edges_v2, note="v2").json()["revision_id"]

    # 更正后、重新生成前：当前可供数为空（旧归属不得沿用）
    snap_empty = _snapshot(client)
    assert snap_empty["supply"] == []
    assert snap_empty["watermark"] > wm1

    body = _changes(client, wm1)
    assert body["group_count"] == 1
    g = body["groups"][0]
    assert g["group_type"] == "correction"
    assert g["trigger_result_id"] == r1["result_id"]
    assert {c["change_type"] for c in g["changes"]} == {"withdrawn"}
    assert {c["observation_code"] for c in g["changes"]} == {"o1", "o5", "o6"}
    for c in g["changes"]:
        # 撤下给出撤下前的接受名与结果号，新接受名为空
        assert c["accepted_name"] == "接受名A"
        assert c["result_id"] == r1["result_id"]
    wm2 = body["watermark"]

    # 当前修订结果生成后才发布新归属
    r2 = client.post("/results", json={"batch_id": bid}).json()
    assert r2["revision_id"] == rev2 and r2["status"] == "active"
    snap = _snapshot(client)
    assert snap["supply_count"] == 4  # o2 新纳入
    assert _locator_set(snap["supply"]) == {(bid, c) for c in ("o1", "o2", "o5", "o6")}

    body = _changes(client, wm2)
    assert body["group_count"] == 1
    g = body["groups"][0]
    assert g["group_type"] == "generation"
    assert g["trigger_result_id"] == r2["result_id"]
    # 更正撤下已把流状态清空，重新生成后 4 条纳入观测全部作为新接受名发布；
    # 下游重放「撤下组 + 发布组」即得到与快照一致的状态（含新纳入的 o2）。
    assert len(g["changes"]) == 4
    by_code = {c["observation_code"]: c for c in g["changes"]}
    assert set(by_code) == {"o1", "o2", "o5", "o6"}
    for c in g["changes"]:
        assert c["change_type"] == "upsert"
        assert c["accepted_name"] == "接受名A"
        assert c["result_id"] == r2["result_id"]


def test_explicit_old_revision_generation_does_not_change_sync(client):
    bid = _batch(client, OBS).json()["id"]
    rev1 = _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1).json()["revision_id"]
    client.post("/results", json={"batch_id": bid})
    wm_before = _snapshot(client)["watermark"]

    _taxonomy(client, ["接受名A"], [], note="v2")
    # 更正产生撤下组
    wm_after_correction = _snapshot(client)["watermark"]
    assert wm_after_correction > wm_before

    # 显式对旧修订生成 -> expired，同步内容不变（无新组）
    old = client.post("/results", json={"batch_id": bid, "revision_id": rev1})
    assert old.json()["status"] == "expired"
    assert _snapshot(client)["watermark"] == wm_after_correction
    body = _changes(client, wm_after_correction)
    assert body["groups"] == []
    assert body["watermark"] == wm_after_correction


def test_regeneration_with_same_attribution_emits_no_group(client):
    bid = _batch(client, OBS).json()["id"]
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1)
    r1 = client.post("/results", json={"batch_id": bid}).json()
    wm = _snapshot(client)["watermark"]

    # 同一当前修订重复生成：结果内容一致，同步流不产生新组
    r2 = client.post("/results", json={"batch_id": bid}).json()
    assert r2["result_id"] != r1["result_id"]
    assert _snapshot(client)["watermark"] == wm
    assert _changes(client, wm)["groups"] == []

    # 历史结果仍可查
    assert client.get(f"/results/{r1['result_id']}").status_code == 200
    assert client.get(f"/results/{r1['result_id']}").json()["status"] == "expired"


def test_pagination_returns_every_group_once_in_order(client):
    bid = _batch(client, OBS).json()["id"]
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1)
    client.post("/results", json={"batch_id": bid})                 # 组1 发布
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1, note="v2")  # 组2 撤下
    client.post("/results", json={"batch_id": bid})                 # 组3 重新发布
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1, note="v3")  # 组4 撤下
    client.post("/results", json={"batch_id": bid})                 # 组5 重新发布

    seen, after = [], 0
    for _ in range(10):
        body = _changes(client, after, limit=2)
        if not body["groups"]:
            break
        assert len(body["groups"]) <= 2
        seen.extend(g["watermark"] for g in body["groups"])
        after = body["watermark"]
        if not body["has_more"]:
            break
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == 5
    # 续读到头后再请求为空
    tail = _changes(client, after)
    assert tail["groups"] == [] and tail["watermark"] == after


def test_changes_are_located_by_batch_and_observation_code(client):
    b1 = _batch(client, [{"code": "x1", "name": "旧名甲"}], note="b1").json()["id"]
    b2 = _batch(client, [{"code": "x2", "name": "接受名A"}], note="b2").json()["id"]
    _taxonomy(
        client,
        ["接受名A"],
        [{"synonym": "旧名甲", "target": "接受名A"}],
    )
    client.post("/results", json={"batch_id": b1})
    client.post("/results", json={"batch_id": b2})

    body = _changes(client, 0)
    locators = {
        (c["batch_id"], c["observation_code"])
        for g in body["groups"] for c in g["changes"]
    }
    assert locators == {(b1, "x1"), (b2, "x2")}
    # 每个批次各自成组
    assert {g["batch_id"] for g in body["groups"]} == {b1, b2}
    assert all(g["group_type"] == "generation" for g in body["groups"])


def test_correction_groups_cover_each_batch_with_active_supply(client):
    b1 = _batch(client, [{"code": "x1", "name": "旧名甲"}]).json()["id"]
    b2 = _batch(client, [{"code": "x2", "name": "接受名A"}]).json()["id"]
    _taxonomy(
        client, ["接受名A"], [{"synonym": "旧名甲", "target": "接受名A"}]
    )
    client.post("/results", json={"batch_id": b1})
    client.post("/results", json={"batch_id": b2})
    wm = _snapshot(client)["watermark"]

    # 更正使旧名断链：两个批次的当前供数都须撤下（b1 撤 x1，b2 撤 x2）
    _taxonomy(client, ["接受名A"], [{"synonym": "旧名", "target": "孤儿"}], note="v2")
    assert _snapshot(client)["supply"] == []
    body = _changes(client, wm)
    assert body["group_count"] == 2
    per_batch = {g["batch_id"]: g["changes"] for g in body["groups"]}
    assert {c["observation_code"] for c in per_batch[b1]} == {"x1"}
    assert {c["change_type"] for c in per_batch[b1]} == {"withdrawn"}
    assert {c["observation_code"] for c in per_batch[b2]} == {"x2"}
    # 水位严格递增、组间不交错
    wms = [g["watermark"] for g in body["groups"]]
    assert wms == sorted(wms) and len(set(wms)) == 2


def test_failed_write_leaves_no_half_group(client, conn, monkeypatch):
    bid = _batch(client, OBS).json()["id"]
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1)

    before_groups = conn.execute(
        "SELECT COUNT(*) AS n FROM sync_change_group"
    ).fetchone()["n"]
    before_results = conn.execute("SELECT COUNT(*) AS n FROM result").fetchone()["n"]

    def boom(*args, **kwargs):
        raise RuntimeError("模拟同步写入失败")

    # 生成事务内发布变更组时失败：整事务回滚，结果与同步组都不得留下
    monkeypatch.setattr(sync, "insert_generation_group", boom)
    with pytest.raises(RuntimeError):
        client.post("/results", json={"batch_id": bid})

    assert conn.execute("SELECT COUNT(*) AS n FROM result").fetchone()["n"] == before_results
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM sync_change_group"
    ).fetchone()["n"] == before_groups
    assert conn.execute("SELECT COUNT(*) AS n FROM sync_change").fetchone()["n"] == 0
    # 水位无残留推进：快照仍为空、水位为 0
    snap = _snapshot(client)
    assert snap["watermark"] == 0 and snap["supply"] == []


def test_snapshot_matches_replaying_change_stream(client):
    bid = _batch(client, OBS).json()["id"]
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1)
    client.post("/results", json={"batch_id": bid})
    edges_v2 = [e for e in EDGES_V1 if not (
        e["synonym"] == "分叉名" and e["target"] == "接受名B"
    )]
    _taxonomy(client, ["接受名A", "接受名B"], edges_v2, note="v2")
    client.post("/results", json={"batch_id": bid})
    _taxonomy(client, ["接受名A", "接受名B"], EDGES_V1, note="v3")
    client.post("/results", json={"batch_id": bid})

    # 从水位 0 重放整条变更流，物化出的状态必须与快照一致
    state: dict[tuple, dict] = {}
    after = 0
    while True:
        body = _changes(client, after)
        for g in body["groups"]:
            for c in g["changes"]:
                key = (c["batch_id"], c["observation_code"])
                if c["change_type"] == "withdrawn":
                    state.pop(key, None)
                else:
                    state[key] = {
                        "accepted_name": c["accepted_name"],
                        "result_id": c["result_id"],
                        "line_no": c["line_no"],
                    }
        after = body["watermark"]
        if not body["groups"]:
            break

    snap = _snapshot(client)
    assert snap["watermark"] == after
    replayed = {
        (item["batch_id"], item["observation_code"]): {
            "accepted_name": item["accepted_name"],
            "result_id": item["result_id"],
            "line_no": item["line_no"],
        }
        for item in snap["supply"]
    }
    assert state == replayed


def test_invalid_after_and_limit_rejected(client):
    assert client.get("/sync/changes", params={"after": -1}).status_code == 422
    assert client.get("/sync/changes", params={"limit": 0}).status_code == 422
    assert client.get("/sync/changes", params={"limit": 501}).status_code == 422
    # 合法参数正常
    assert client.get("/sync/changes").status_code == 200


def test_attribution_change_emits_withdrawn_then_upsert(client):
    # 同一观测更正后接受名变化：先撤下旧名，再 upsert 新接受名
    bid = _batch(client, [{"code": "o1", "name": "旧名"}]).json()["id"]
    _taxonomy(client, ["A"], [{"synonym": "旧名", "target": "A"}], note="v1")
    r1 = client.post("/results", json={"batch_id": bid}).json()
    wm1 = _snapshot(client)["watermark"]

    _taxonomy(client, ["B"], [{"synonym": "旧名", "target": "B"}], note="v2")
    mid = _changes(client, wm1)
    assert mid["groups"][0]["group_type"] == "correction"
    wm2 = mid["watermark"]

    r2 = client.post("/results", json={"batch_id": bid}).json()
    body = _changes(client, wm2)
    g = body["groups"][0]
    assert g["group_type"] == "generation"
    assert len(g["changes"]) == 1
    c = g["changes"][0]
    assert c["observation_code"] == "o1"
    assert c["change_type"] == "upsert"
    assert c["accepted_name"] == "B"
    assert c["result_id"] == r2["result_id"]
    assert c["result_id"] != r1["result_id"]

    snap = _snapshot(client)
    assert snap["supply_count"] == 1
    assert snap["supply"][0]["accepted_name"] == "B"
    assert snap["supply"][0]["result_id"] == r2["result_id"]
    # 历史结果仍可查，内容冻结
    frozen = client.get(f"/results/{r1['result_id']}").json()
    assert frozen["status"] == "expired"
    assert frozen["items"][0]["accepted_name"] == "A"
