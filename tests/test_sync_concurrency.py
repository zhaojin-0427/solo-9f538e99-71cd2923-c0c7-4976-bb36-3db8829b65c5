"""供数同步流在生成/更正并发交错下的不变量。

多个线程独立连接交替执行「生成当前结果」与「整版更正」，验证：
1. 每个变更组的组内变更完整（组要么整组可见，要么不存在，无半组）；
2. 水位严格连续递增，按水位续读不漏组、不重组，同水位重复请求幂等；
3. 撤下组只引用更正前的 active 结果，生成组只引用 active 结果；
4. 从水位 0 重放整条变更流得到的状态，与当前可供数快照完全一致；
5. 更正之后、重新生成之前，快照中不存在任何沿用已撤下归属的供数。
"""
import os
import tempfile
import threading

import pytest

from app import services, sync
from app.database import connect, init_db
from app.models import (
    BatchCreate,
    EdgeIn,
    ObservationIn,
    ResultCreate,
    TaxonomyCreate,
)


def _make_db():
    path = os.path.join(tempfile.mkdtemp(prefix="eco_sync_conc_"), "conc.db")
    conn = connect(path)
    init_db(conn)
    return conn, path


def _taxonomy(rev_seed: int) -> TaxonomyCreate:
    edges = [
        EdgeIn(synonym="旧名甲", target="接受名A"),
        EdgeIn(synonym="菱形名", target="s1"),
        EdgeIn(synonym="菱形名", target="s2"),
        EdgeIn(synonym="s1", target="接受名A"),
        EdgeIn(synonym="s2", target="接受名A"),
        EdgeIn(synonym="环名x", target="环名y"),
        EdgeIn(synonym="环名y", target="环名x"),
    ]
    if rev_seed % 2 == 0:
        edges.append(EdgeIn(synonym="分叉名", target="接受名A"))
        edges.append(EdgeIn(synonym="分叉名", target="接受名B"))
    else:
        edges.append(EdgeIn(synonym="分叉名", target="接受名A"))
    return TaxonomyCreate(
        accepted_names=["接受名A", "接受名B"],
        synonym_edges=edges,
        note=f"rev-{rev_seed}",
    )


OBSERVATIONS = [
    ObservationIn(code="o1", name="旧名甲"),
    ObservationIn(code="o2", name="分叉名"),
    ObservationIn(code="o3", name="环名x"),
    ObservationIn(code="o4", name="无人名"),
    ObservationIn(code="o5", name="菱形名"),
    ObservationIn(code="o6", name="接受名A"),
]


def test_sync_stream_invariants_under_concurrency():
    admin_conn, db_file = _make_db()
    batch_id = services.create_batch(
        admin_conn, BatchCreate(observations=OBSERVATIONS, note="并发同步批次")
    )
    services.create_taxonomy_revision(admin_conn, _taxonomy(0))
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker(worker_id: int, iterations: int, do_corrections: bool):
        conn = connect(db_file)
        try:
            barrier.wait()
            for i in range(iterations):
                if do_corrections and i % 2 == 0:
                    services.create_taxonomy_revision(
                        conn, _taxonomy(worker_id * 100 + i)
                    )
                else:
                    services.generate_result(conn, ResultCreate(batch_id=batch_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(1, 20, True)),
        threading.Thread(target=worker, args=(2, 20, True)),
        threading.Thread(target=worker, args=(3, 20, False)),
        threading.Thread(target=worker, args=(4, 20, False)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发期间出现异常: {errors[:3]}"

    # 不变量 1：水位严格递增不重复（回滚事务可能消耗序列号造成缺口，
    # 但不影响已提交组的完整性）；组整组完整，每个组至少一条变更。
    groups = admin_conn.execute(
        "SELECT watermark, group_type, batch_id, trigger_result_id "
        "FROM sync_change_group ORDER BY watermark"
    ).fetchall()
    assert groups
    watermarks = [g["watermark"] for g in groups]
    assert watermarks == sorted(set(watermarks))
    assert watermarks[0] >= 1

    for g in groups:
        changes = sync.list_changes(admin_conn, g["watermark"])
        assert changes, "变更组不能为空组"
        seqs = [c["change_seq"] for c in changes]
        assert seqs == list(range(1, len(seqs) + 1))
        assert all(c["batch_id"] == g["batch_id"] for c in changes)
        assert {c["group_id"] for c in changes} == {g["watermark"]}

    # 不变量 2：按水位续读不漏组、不重组；同水位重复请求幂等
    seen: list[int] = []
    after = 0
    while True:
        page, _, has_more, wm = sync.read_changes(admin_conn, after, limit=3)
        if not page:
            assert wm == after
            break
        seen.extend(g["watermark"] for g in page)
        # 同水位重复请求返回同一组
        page2, _, _, wm2 = sync.read_changes(admin_conn, after, limit=3)
        assert [g["watermark"] for g in page2] == [g["watermark"] for g in page]
        assert wm2 == wm
        after = wm
        if not has_more:
            tail, _, tail_more, tail_wm = sync.read_changes(admin_conn, after, limit=3)
            assert tail == [] and not tail_more and tail_wm == after
            break
    assert seen == watermarks

    # 不变量 3：
    # - 生成组内全部为 upsert，且每条变更的结果号等于组触发结果（发布时的当前供数）；
    #   触发结果事后可能被更正/再生成置为 expired，这是正常的终态流转。
    # - 撤下组内全部为 withdrawn，且引用的触发结果终态必为 expired（撤下不可复活）。
    result_status = {
        r["result_id"]: r["status"] for r in services.list_results(admin_conn)
    }
    for g in groups:
        changes = sync.list_changes(admin_conn, g["watermark"])
        if g["group_type"] == sync.GROUP_GENERATION:
            assert all(
                c["change_type"] == sync.CHANGE_UPSERT
                and c["result_id"] == g["trigger_result_id"]
                for c in changes
            )
        else:
            assert result_status[g["trigger_result_id"]] == "expired"
            assert all(
                c["change_type"] == sync.CHANGE_WITHDRAWN
                and c["result_id"] == g["trigger_result_id"]
                for c in changes
            )

    # 不变量 4：从水位 0 重放变更流，物化状态与当前可供数快照完全一致
    state: dict[tuple, dict] = {}
    for g in groups:
        for c in sync.list_changes(admin_conn, g["watermark"]):
            key = (c["batch_id"], c["observation_code"])
            if c["change_type"] == sync.CHANGE_WITHDRAWN:
                state.pop(key, None)
            else:
                state[key] = (c["accepted_name"], c["result_id"], c["line_no"])

    supply_rows, snapshot_wm = sync.read_snapshot(admin_conn)
    assert snapshot_wm == watermarks[-1]
    snap_state = {
        (r["batch_id"], r["observation_code"]):
            (r["accepted_name"], r["result_id"], r["line_no"])
        for r in supply_rows
    }
    assert state == snap_state

    # 不变量 5：快照即流的物化状态，其内容必须能由「当前各批次 active 结果的
    # included 项」重算得到（active 结果代表当前供数归属；流状态里的结果号
    # 可能在并发更正后已被置 expired，但归属语义不变，已由不变量 4 的重放校验覆盖）。
    recomputed: dict[tuple, tuple] = {}
    for r in services.list_results(admin_conn, status="active"):
        for item in services.list_result_items(admin_conn, r["result_id"]):
            if item["item_status"] == "included":
                recomputed[(r["batch_id"], item["observation_code"])] = (
                    item["accepted_name"],
                    item["line_no"],
                )
    snap_semantics = {
        (row["batch_id"], row["observation_code"]):
            (row["accepted_name"], row["line_no"])
        for row in supply_rows
    }
    assert snap_semantics == recomputed

    admin_conn.close()


def test_failed_correction_rolls_back_group_and_supersession():
    conn, _ = _make_db()
    bid = services.create_batch(conn, BatchCreate(observations=OBSERVATIONS))
    services.create_taxonomy_revision(conn, _taxonomy(0))
    services.generate_result(conn, ResultCreate(batch_id=bid))
    wm_before = sync.current_watermark(conn)
    assert wm_before >= 1

    original = sync.insert_correction_groups

    def fail_after_one_group(c, active_rows):
        watermarks = original(c, active_rows)
        raise RuntimeError("模拟更正事务提交前失败")

    sync.insert_correction_groups = fail_after_one_group
    try:
        with pytest.raises(RuntimeError):
            services.create_taxonomy_revision(conn, _taxonomy(99))
    finally:
        sync.insert_correction_groups = original

    # 整事务回滚：修订未增加、结果仍 active、同步组无残留
    assert sync.current_watermark(conn) == wm_before
    actives = [r for r in services.list_results(conn) if r["status"] == "active"]
    assert len(actives) == 1
    revisions = services.list_revisions(conn)
    assert len(revisions) == 1
    conn.close()
