"""供数同步：变更组（水位）、流状态物化与同步读取。

设计要点
--------
1. 同步流只描述「当前可供数（active 结果中的 included 观测）」的变化，
   用 (batch_id, observation_code) 定位记录：
   - ``upsert``：该观测纳入供数或归属的接受名变化，携带当前接受名与结果号；
   - ``withdrawn``：该观测不再可供数（分类表更正撤下），携带撤下前的
     接受名与结果号，新接受名为空。
2. ``sync_supply_state`` 是同步流的物化状态（最新水位处下游应持有的归属），
   与变更组在**同一个 IMMEDIATE 事务**内更新：一次更正或一次生成的变更
   整组可见，任何失败整体回滚，不存在半组；快照直接读该表，与水位天然一致。
3. 水位单调递增且变更内容不可变：按返回水位续读不漏组，同一水位重复
   请求永远返回同一组。
4. 针对旧修订显式生成的结果直接落为 expired，不进入当前供数，因此
   不产生任何同步组——同步内容只随「更正撤下」与「当前修订结果生成」变化。
"""
from __future__ import annotations

import sqlite3

from .database import read_tx

GROUP_CORRECTION = "correction"
GROUP_GENERATION = "generation"

CHANGE_WITHDRAWN = "withdrawn"
CHANGE_UPSERT = "upsert"


# ---------- 写入：必须在调用方的 IMMEDIATE 事务内执行 ----------

def _insert_group_and_changes(
    conn: sqlite3.Connection,
    group_type: str,
    batch_id: int,
    trigger_result_id: int | None,
    changes: list[tuple],
) -> int:
    """插入一个变更组及其组内变更。changes 元素为
    (batch_id, code, line_no, change_type, accepted_name, result_id)。"""
    cur = conn.execute(
        """
        INSERT INTO sync_change_group(group_type, batch_id, trigger_result_id)
        VALUES (?, ?, ?)
        """,
        (group_type, batch_id, trigger_result_id),
    )
    watermark = cur.lastrowid
    conn.executemany(
        """
        INSERT INTO sync_change(group_id, batch_id, observation_code, line_no,
                                change_type, accepted_name, result_id, change_seq)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (watermark, *change, seq)
            for seq, change in enumerate(changes, start=1)
        ],
    )
    return watermark


def _remove_state(conn: sqlite3.Connection, batch_id: int, code: str) -> None:
    conn.execute(
        "DELETE FROM sync_supply_state WHERE batch_id = ? AND observation_code = ?",
        (batch_id, code),
    )


def _upsert_state(
    conn: sqlite3.Connection,
    batch_id: int,
    line_no: int,
    code: str,
    accepted_name: str,
    result_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_supply_state(batch_id, observation_code, line_no,
                                      accepted_name, result_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(batch_id, observation_code) DO UPDATE SET
            line_no = excluded.line_no,
            accepted_name = excluded.accepted_name,
            result_id = excluded.result_id
        """,
        (batch_id, code, line_no, accepted_name, result_id),
    )


def insert_correction_groups(
    conn: sqlite3.Connection, active_rows: list[sqlite3.Row]
) -> list[int]:
    """分类表更正：把每个受影响批次的当前供数整组撤下。

    撤下组与流状态删除在调用方事务内完成；每个受影响批次一个组。
    :param active_rows: 更正前各批次 active 结果行（含 result_id/batch_id）。
    :return: 新建变更组的水位列表（按批次）。
    """
    watermarks: list[int] = []
    for row in active_rows:
        result_id = row["result_id"]
        batch_id = row["batch_id"]
        included = conn.execute(
            """
            SELECT line_no, observation_code, accepted_name
              FROM result_item
             WHERE result_id = ? AND item_status = 'included'
             ORDER BY line_no
            """,
            (result_id,),
        ).fetchall()
        if not included:
            # 当前供数没有任何纳入观测，撤下不会改变同步内容，无需成组
            continue
        changes = [
            (
                batch_id,
                item["observation_code"],
                item["line_no"],
                CHANGE_WITHDRAWN,
                item["accepted_name"],
                result_id,
            )
            for item in included
        ]
        watermark = _insert_group_and_changes(
            conn, GROUP_CORRECTION, batch_id, result_id, changes
        )
        # 同步物化状态：整组撤下
        for item in included:
            _remove_state(conn, batch_id, item["observation_code"])
        watermarks.append(watermark)
    return watermarks


def insert_generation_group(
    conn: sqlite3.Connection,
    batch_id: int,
    new_result_id: int,
) -> int | None:
    """生成当前供数结果：对比同步流物化状态与新结果的纳入集合，整组发布差异。

    以 ``sync_supply_state``（流的当前状态）为基线：并发下两个同修订生成
    事务可能先后产出内容相同的 active 结果，后者顶替前者但归属并未变化，
    此时不发组，保证流重放状态与快照一致。

    :return: 新建变更组水位；无差异（含显式对旧修订生成的 expired 结果，
        该情况在调用方被排除）时返回 None，不产生同步内容。
    """
    baseline = {
        row["observation_code"]: row
        for row in conn.execute(
            """
            SELECT line_no, observation_code, accepted_name, result_id
              FROM sync_supply_state
             WHERE batch_id = ?
            """,
            (batch_id,),
        )
    }

    new_items = conn.execute(
        """
        SELECT line_no, observation_code, accepted_name
          FROM result_item
         WHERE result_id = ? AND item_status = 'included'
         ORDER BY line_no
        """,
        (new_result_id,),
    ).fetchall()
    new_codes = {item["observation_code"] for item in new_items}

    changes: list[tuple] = []

    # 撤下：流状态中存在、新结果不再纳入的观测（引用撤下前的结果号）
    for code in sorted(baseline):
        if code not in new_codes:
            item = baseline[code]
            changes.append(
                (
                    batch_id,
                    code,
                    item["line_no"],
                    CHANGE_WITHDRAWN,
                    item["accepted_name"],
                    item["result_id"],
                )
            )

    # 新增或归属变化：新纳入，或接受名与流状态不同
    for item in new_items:
        old_item = baseline.get(item["observation_code"])
        if old_item is None or old_item["accepted_name"] != item["accepted_name"]:
            changes.append(
                (
                    batch_id,
                    item["observation_code"],
                    item["line_no"],
                    CHANGE_UPSERT,
                    item["accepted_name"],
                    new_result_id,
                )
            )

    if not changes:
        # 当前可供数相对同步流没有任何变化：同步内容保持不变，不发组
        return None

    watermark = _insert_group_and_changes(
        conn, GROUP_GENERATION, batch_id, new_result_id, changes
    )

    # 同步物化状态：与变更组同事务推进到最新水位
    for change in changes:
        _, code, line_no, change_type, accepted_name, result_id = change
        if change_type == CHANGE_WITHDRAWN:
            _remove_state(conn, batch_id, code)
        else:
            _upsert_state(
                conn, batch_id, line_no, code, accepted_name, result_id
            )
    return watermark


# ---------- 读取：快照与按水位续读 ----------

def current_watermark(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(watermark), 0) AS wm FROM sync_change_group"
    ).fetchone()
    return row["wm"]


def list_active_supply(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """当前可供数快照：直接读同步流物化状态，与最新水位同点一致。"""
    return conn.execute(
        """
        SELECT batch_id, line_no, observation_code, accepted_name, result_id
          FROM sync_supply_state
         ORDER BY batch_id, line_no
        """
    ).fetchall()


def fetch_groups_after(
    conn: sqlite3.Connection, after: int, limit: int
) -> tuple[list[sqlite3.Row], bool]:
    """领取水位严格大于 ``after`` 的变更组，按水位升序，至多 ``limit`` 组。

    返回 (组行列表, 是否还有更多组)。多取一组用于判断 has_more。
    """
    rows = conn.execute(
        """
        SELECT watermark, group_type, batch_id, trigger_result_id, created_at
          FROM sync_change_group
         WHERE watermark > ?
         ORDER BY watermark
         LIMIT ?
        """,
        (after, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    return rows[:limit], has_more


def list_changes(conn: sqlite3.Connection, group_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT group_id, batch_id, observation_code, line_no,
               change_type, accepted_name, result_id, change_seq
          FROM sync_change
         WHERE group_id = ?
         ORDER BY change_seq
        """,
        (group_id,),
    ).fetchall()


def read_snapshot(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], int]:
    """首次读取：同一只读事务内取当前可供数全量与当前水位，保证二者一致。"""
    with read_tx(conn):
        return list_active_supply(conn), current_watermark(conn)


def read_changes(
    conn: sqlite3.Connection, after: int, limit: int
) -> tuple[list[sqlite3.Row], dict[int, list[sqlite3.Row]], bool, int]:
    """续读：同一只读事务内取组、组内变更与响应水位。

    返回 (组行列表, 组水位 -> 组内变更行列表, 是否还有更多, 响应水位)。
    响应水位 = 本次返回最后一组的水位；没有可领组时等于请求的 after，
    因此同一水位重复请求得到完全相同的响应。
    """
    with read_tx(conn):
        groups, has_more = fetch_groups_after(conn, after, limit)
        changes_by_group: dict[int, list[sqlite3.Row]] = {}
        for group in groups:
            changes_by_group[group["watermark"]] = list_changes(
                conn, group["watermark"]
            )
        response_watermark = groups[-1]["watermark"] if groups else after
    return groups, changes_by_group, has_more, response_watermark
