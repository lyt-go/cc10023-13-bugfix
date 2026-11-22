"""领域动作接口：字段发现、修订校验、计算下发与历史查询。

路径都放在通用 ``/api/{module}/{entry_id}`` 之前注册，避免 ``history`` /
``pending`` 这类固定路径被整数路径参数抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.services import service_for

router = APIRouter(prefix="/api", tags=["领域动作"])


def _service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


def _identifier(value: str) -> Any:
    return int(value) if value.isdigit() else value


# ── 特征源：字段发现与回执 ────────────────────────────────────────────────
@router.post("/feature_source/{source_id}/discover")
def discover_source_fields(
    source_id: int,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    service = _service("feature_source")
    return service.fetch_source_schema(
        source_id,
        payload.get("fields", payload.get("字段清单")),
        receipt_id=payload.get("receipt_id") or payload.get("回执编号"),
    )


@router.post("/feature_source/{source_id}/receipts")
def submit_source_receipt(
    source_id: int,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    service = _service("feature_source")
    return service.sync_source_receipt(
        source_id,
        payload.get("receipt", payload) if payload else None,
        receipt_id=payload.get("receipt_id") or payload.get("回执编号"),
    )


@router.get("/feature_source/{source_id}/fields")
def source_field_catalog(source_id: int) -> dict[str, Any]:
    return _service("feature_source").source_field_catalog(source_id)


# ── 特征定义：待校验修订、详情、更正与生效 ────────────────────────────────
@router.get("/feature_define/pending")
def pending_definitions(
    source_id: int | None = Query(default=None),
    field: str | None = Query(default=None),
    include_drafts: bool = Query(default=False),
) -> dict[str, Any]:
    return _service("feature_define").list_pending_revisions(
        source_id=source_id, field=field, include_drafts=include_drafts)


@router.get("/feature_define/detail/{identifier}")
def definition_detail(identifier: str) -> dict[str, Any]:
    detail = _service("feature_define").definition_detail(_identifier(identifier))
    if detail is None:
        raise HTTPException(status_code=404, detail=f"特征定义 {identifier} 不存在")
    return detail


@router.post("/feature_define/{identifier}/approve")
def approve_definition(identifier: str) -> dict[str, Any]:
    return _service("feature_define").approve_revision(_identifier(identifier))


@router.post("/feature_define/{identifier}/corrections")
def submit_definition_correction(
    identifier: str,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    changes = payload.get("changes") or {
        key: value
        for key, value in payload.items()
        if key not in ("source_field", "字段", "字段名")
    }
    return _service("feature_define").submit_revision(
        _identifier(identifier),
        changes,
        source_field=payload.get("source_field") or payload.get("字段") or payload.get("字段名"),
    )

@router.post("/feature_define/push")
def push_definitions_to_compute(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    service = _service("feature_define")
    return service.push_to_compute_tasks(
        payload.get("define_ids") or payload.get("revisions") or [],
        revision=payload.get("revision") or payload.get("修订编号"),
        input_value=payload.get("input_value"),
        input_values=payload.get("input_values"),
        slice_name=payload.get("slice_name"),
    )


# ── 特征计算：执行、重放与历史 ────────────────────────────────────────────
@router.post("/feature_compute/replay/{task_id}")
def replay_compute_task(
    task_id: int,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    return _service("feature_compute").replay_task(task_id, payload.get("input_value"))


@router.post("/feature_compute/run/{task_id}")
def run_compute_task(
    task_id: int,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    return _service("feature_compute").execute_and_snapshot(task_id, payload.get("input_value"))


@router.get("/feature_compute/history")
def compute_history(task_id: int | None = Query(default=None)) -> dict[str, Any]:
    return _service("feature_compute").compute_history(task_id=task_id)


@router.get("/feature_compute/tasks/{task_id}")
def compute_task_detail(task_id: int) -> dict[str, Any]:
    return _service("feature_compute").query_task(task_id)


# ── 通用领域方法分派：按模块 key 调用服务方法，便于脚本化演练 ────────────
@router.post("/{module}/domain/{method}")
def call_domain_method(
    module: str,
    method: str,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> Any:
    service = _service(module)
    if method.startswith("_"):
        raise HTTPException(status_code=404, detail=f"未知领域方法：{method}")
    handler = getattr(service, method, None)
    if handler is None or not callable(handler):
        raise HTTPException(status_code=404, detail=f"未知领域方法：{method}")
    try:
        return handler(**payload)
    except TypeError as error:
        raise HTTPException(status_code=400, detail=f"参数不匹配：{error}") from error
