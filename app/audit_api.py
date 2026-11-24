"""FastAPI routes for the tape read-verification (巡检) API.

An inspection round is created from a catalogue in ``/schedule`` shape plus the
ids of the two tape drives under test.  Receipt batches are filed by receipt
number; the inspection always reports every minimal explanation of the
effective receipts.  The ``/schedule`` catalogue filters only replicas that are
bad under *every* explanation.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse

from .audit_models import (
    DEFAULT_BATCH_EXAMPLE,
    DEFAULT_ROUND_EXAMPLE,
    AuditCatalogueResponse,
    AuditStartRequest,
    AuditState,
    BatchRequest,
    BatchResult,
    InspectionResponse,
    RetractRequest,
    RetractResult,
)
from .audit_store import (
    BatchReferenceError,
    RevisionMismatch,
    RoundNotFound,
    create_round,
    get_state,
    inspection,
    retract_batch,
    schedule_catalogue,
    submit_batch,
)

router = APIRouter(tags=["audit"], prefix="/audit")


@router.get(
    "/example",
    summary="获取可直接提交的巡检开轮样例",
)
def round_example() -> dict:
    return DEFAULT_ROUND_EXAMPLE


@router.get(
    "/example/batch",
    summary="获取可直接提交的回执批次样例（及撤回样例）",
)
def batch_example() -> dict:
    return {
        "batch": DEFAULT_BATCH_EXAMPLE,
        "retract": {"receipt_id": DEFAULT_BATCH_EXAMPLE["receipt_id"], "reason": "误报"},
    }


@router.post(
    "/rounds",
    response_model=AuditState,
    status_code=201,
    summary="开一轮巡检：提交 /schedule 格式目录与两台带机编号",
    responses={422: {"description": "目录格式、副本编号或带机范围校验失败"}},
)
def start_round(request: AuditStartRequest) -> AuditState:
    return create_round(request)


@router.get(
    "/rounds/{round_id}",
    response_model=InspectionResponse,
    summary="巡检结论：全部极小故障解释与停用/隔离建议",
    responses={404: {"description": "巡检轮次不存在"}},
)
def get_round(round_id: str) -> InspectionResponse:
    try:
        return inspection(round_id)
    except RoundNotFound as exc:
        raise HTTPException(status_code=404, detail=f"巡检轮次不存在: {exc}") from exc


@router.get(
    "/rounds/{round_id}/state",
    response_model=AuditState,
    summary="轮次状态与修订号",
)
def get_round_state(round_id: str) -> AuditState:
    try:
        return get_state(round_id)
    except RoundNotFound as exc:
        raise HTTPException(status_code=404, detail=f"巡检轮次不存在: {exc}") from exc


@router.get(
    "/rounds/{round_id}/schedule-catalogue",
    response_model=AuditCatalogueResponse,
    summary="生成供 /schedule 使用的目录（仅滤除确定损坏的副本）",
    responses={
        404: {"description": "巡检轮次不存在"},
        409: {"description": "有效回执无法解释，需先撤回误报"},
    },
)
def get_schedule_catalogue(round_id: str) -> AuditCatalogueResponse:
    try:
        return schedule_catalogue(round_id)
    except RoundNotFound as exc:
        raise HTTPException(status_code=404, detail=f"巡检轮次不存在: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/rounds/{round_id}/batches",
    response_model=BatchResult,
    summary="按回执号成批报送读片校验结果",
    responses={
        200: {"description": "原样重报，不重复计数"},
        201: {"description": "整批生效"},
        404: {"description": "巡检轮次不存在"},
        409: {"description": "内容冲突、回执号已撤回或解释不通；整批拒收（附结构化结果）"},
        422: {"description": "请求结构不合法（如缺副本编号、同批重复）"},
    },
)
def post_batch(round_id: str, request: BatchRequest, response: Response):
    try:
        result = submit_batch(round_id, request)
    except RoundNotFound as exc:
        raise HTTPException(status_code=404, detail=f"巡检轮次不存在: {exc}") from exc
    except RevisionMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BatchReferenceError as exc:
        return JSONResponse(status_code=422, content=exc.result.model_dump())

    status_code = {
        "accepted": 201,
        "duplicate": 200,
        "rejected": 409,
    }[result.status]
    if status_code == 409:
        return JSONResponse(status_code=409, content=result.model_dump())
    response.status_code = status_code
    return result


@router.post(
    "/rounds/{round_id}/retract",
    response_model=RetractResult,
    summary="按回执号整批撤回；撤回后的号码不复用",
    responses={
        404: {"description": "巡检轮次不存在"},
        409: {"description": "修订号不匹配"},
    },
)
def post_retract(round_id: str, request: RetractRequest) -> RetractResult:
    try:
        return retract_batch(
            round_id,
            request.receipt_id,
            request.reason,
            request.expected_revision,
        )
    except RoundNotFound as exc:
        raise HTTPException(status_code=404, detail=f"巡检轮次不存在: {exc}") from exc
    except RevisionMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
