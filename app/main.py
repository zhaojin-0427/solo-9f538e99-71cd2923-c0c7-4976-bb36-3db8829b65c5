"""FastAPI 应用：观测录入、分类表整版更正、供数结果生成与查询。

启动后访问 /docs 可直接在 Swagger UI 中录入、生成并查询结果。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlite3 import Connection

from . import services, sync
from .database import connect, init_db
from .models import (
    BatchCreate,
    BatchOut,
    BatchSummaryOut,
    EdgeOut,
    ObservationOut,
    ResultCreate,
    ResultDetailOut,
    ResultItemOut,
    ResultSummaryOut,
    SyncChangeOut,
    SyncChangesOut,
    SyncGroupOut,
    SyncSnapshotOut,
    SyncSupplyItemOut,
    TaxonomyCreate,
    TaxonomyOut,
    TaxonomySummaryOut,
)
from .services import ConflictError, NotFoundError


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="生态监测名称归并供数 API",
    description=(
        "把野外观测的旧物种名沿分类表同义名关系追踪到接受名。"
        "观测至多 100 条/批；分类表整版更正生成新修订（同义关系至多 40 条，允许分叉）；"
        "结果固定批次与修订，可按结果号重读，更正后旧结果自动过期。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn() -> Connection:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _row_to_batch_out(conn: Connection, batch_id: int):
    row = services.get_batch(conn, batch_id)
    obs_rows = services.list_observations(conn, batch_id)
    return BatchOut(
        id=row["id"],
        note=row["note"],
        created_at=row["created_at"],
        observation_count=len(obs_rows),
        observations=[
            ObservationOut(line_no=r["line_no"], code=r["code"], name=r["name"])
            for r in obs_rows
        ],
    )


@app.get("/health", tags=["系统"], summary="健康检查")
def health():
    return {"status": "ok"}


# ---------- 观测批次 ----------

@app.post(
    "/batches",
    response_model=BatchOut,
    status_code=201,
    tags=["观测批次"],
    summary="录入一批观测（至多 100 条，编号在批内唯一）",
)
def create_batch(payload: BatchCreate, conn: Connection = Depends(get_conn)):
    try:
        batch_id = services.create_batch(conn, payload)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _row_to_batch_out(conn, batch_id)


@app.get(
    "/batches",
    response_model=list[BatchSummaryOut],
    tags=["观测批次"],
    summary="列出观测批次",
)
def list_batches(conn: Connection = Depends(get_conn)):
    return [dict(r) for r in services.list_batches(conn)]


@app.get(
    "/batches/{batch_id}",
    response_model=BatchOut,
    tags=["观测批次"],
    summary="读取一个观测批次（含全部观测）",
)
def get_batch(batch_id: int, conn: Connection = Depends(get_conn)):
    if services.get_batch(conn, batch_id) is None:
        raise HTTPException(status_code=404, detail=f"观测批次不存在：batch_id={batch_id}")
    return _row_to_batch_out(conn, batch_id)


# ---------- 分类表修订 ----------

@app.post(
    "/taxonomies",
    response_model=TaxonomyOut,
    status_code=201,
    tags=["分类表"],
    summary="整版提交分类表（自动生成新修订，旧修订与旧结果过期）",
)
def create_taxonomy(payload: TaxonomyCreate, conn: Connection = Depends(get_conn)):
    try:
        revision_id = services.create_taxonomy_revision(conn, payload)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _row_to_taxonomy_out(conn, revision_id)


def _row_to_taxonomy_out(conn: Connection, revision_id: int) -> TaxonomyOut:
    row = services.get_revision(conn, revision_id)
    accepted = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM accepted_name WHERE revision_id = ? ORDER BY rowid",
            (revision_id,),
        )
    ]
    edges = [
        EdgeOut(synonym=r["synonym"], target=r["target"])
        for r in conn.execute(
            "SELECT synonym, target FROM synonym_edge WHERE revision_id = ? ORDER BY rowid",
            (revision_id,),
        )
    ]
    return TaxonomyOut(
        revision_id=row["revision_id"],
        note=row["note"],
        created_at=row["created_at"],
        superseded_at=row["superseded_at"],
        accepted_count=len(accepted),
        edge_count=len(edges),
        accepted_names=accepted,
        synonym_edges=edges,
    )


@app.get(
    "/taxonomies",
    response_model=list[TaxonomySummaryOut],
    tags=["分类表"],
    summary="列出分类表修订",
)
def list_taxonomies(conn: Connection = Depends(get_conn)):
    return [dict(r) for r in services.list_revisions(conn)]


@app.get(
    "/taxonomies/{revision_id}",
    response_model=TaxonomyOut,
    tags=["分类表"],
    summary="读取一个分类表修订的完整内容",
)
def get_taxonomy(revision_id: int, conn: Connection = Depends(get_conn)):
    if services.get_revision(conn, revision_id) is None:
        raise HTTPException(
            status_code=404, detail=f"分类表修订不存在：revision_id={revision_id}"
        )
    return _row_to_taxonomy_out(conn, revision_id)


# ---------- 供数结果 ----------

@app.post(
    "/results",
    response_model=ResultDetailOut,
    status_code=201,
    tags=["供数结果"],
    summary="生成供数结果（固定批次与修订，按结果号可重读）",
)
def generate_result(payload: ResultCreate, conn: Connection = Depends(get_conn)):
    try:
        result_id = services.generate_result(conn, payload)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _row_to_result_detail(conn, result_id)


def _row_to_result_detail(conn: Connection, result_id: int) -> ResultDetailOut:
    row = services.get_result(conn, result_id)
    items = services.list_result_items(conn, result_id)
    return ResultDetailOut(
        result_id=row["result_id"],
        batch_id=row["batch_id"],
        revision_id=row["revision_id"],
        status=row["status"],
        created_at=row["created_at"],
        included_count=row["included_count"],
        pending_count=row["pending_count"],
        items=[
            ResultItemOut(
                line_no=r["line_no"],
                observation_code=r["observation_code"],
                input_name=r["input_name"],
                item_status=r["item_status"],
                accepted_name=r["accepted_name"],
                reason_code=r["reason_code"],
                reason=r["reason"],
            )
            for r in items
        ],
    )


@app.get(
    "/results",
    response_model=list[ResultSummaryOut],
    tags=["供数结果"],
    summary="列出供数结果（可按批次、状态过滤）",
)
def list_results(
    conn: Connection = Depends(get_conn),
    batch_id: int | None = Query(None, description="按观测批次过滤"),
    status: str | None = Query(None, pattern="^(active|expired)$", description="active/expired"),
):
    return [dict(r) for r in services.list_results(conn, batch_id, status)]


@app.get(
    "/results/{result_id}",
    response_model=ResultDetailOut,
    tags=["供数结果"],
    summary="按结果号重读供数结果（过期结果仍可读取，status 标记为 expired）",
)
def get_result(result_id: int, conn: Connection = Depends(get_conn)):
    if services.get_result(conn, result_id) is None:
        raise HTTPException(status_code=404, detail=f"供数结果不存在：result_id={result_id}")
    return _row_to_result_detail(conn, result_id)


# ---------- 供数同步（变更组 + 水位） ----------

@app.get(
    "/sync/supply",
    response_model=SyncSnapshotOut,
    tags=["供数同步"],
    summary="首次读取：当前各批次已纳入供数的观测全量快照与当前水位",
)
def get_sync_snapshot(conn: Connection = Depends(get_conn)):
    supply_rows, watermark = sync.read_snapshot(conn)
    return SyncSnapshotOut(
        watermark=watermark,
        supply_count=len(supply_rows),
        supply=[
            SyncSupplyItemOut(
                batch_id=r["batch_id"],
                line_no=r["line_no"],
                observation_code=r["observation_code"],
                accepted_name=r["accepted_name"],
                result_id=r["result_id"],
            )
            for r in supply_rows
        ],
    )


@app.get(
    "/sync/changes",
    response_model=SyncChangesOut,
    tags=["供数同步"],
    summary="按水位领取变更组（after 为已读水位，返回水位用于续读，同水位重复请求结果一致）",
)
def get_sync_changes(
    conn: Connection = Depends(get_conn),
    after: int = Query(0, ge=0, description="已读水位；只返回水位严格大于它的变更组"),
    limit: int = Query(
        100, ge=1, le=500, description="本次最多返回的变更组数（每组内变更不限）"
    ),
):
    group_rows, changes_by_group, has_more, response_watermark = sync.read_changes(
        conn, after, limit
    )
    groups = [
        SyncGroupOut(
            watermark=g["watermark"],
            group_type=g["group_type"],
            batch_id=g["batch_id"],
            trigger_result_id=g["trigger_result_id"],
            created_at=g["created_at"],
            changes=[
                SyncChangeOut(
                    group_id=r["group_id"],
                    batch_id=r["batch_id"],
                    observation_code=r["observation_code"],
                    line_no=r["line_no"],
                    change_type=r["change_type"],
                    accepted_name=r["accepted_name"],
                    result_id=r["result_id"],
                    change_seq=r["change_seq"],
                )
                for r in changes_by_group.get(g["watermark"], [])
            ],
        )
        for g in group_rows
    ]
    change_count = sum(len(g.changes) for g in groups)
    return SyncChangesOut(
        after=after,
        watermark=response_watermark,
        has_more=has_more,
        group_count=len(groups),
        change_count=change_count,
        groups=groups,
    )
