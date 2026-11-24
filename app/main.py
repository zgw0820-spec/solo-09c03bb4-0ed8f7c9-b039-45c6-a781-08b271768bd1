"""FastAPI wrapper around the tape restore scheduler."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .models import DEFAULT_EXAMPLE, ScheduleRequest, ScheduleResponse
from .solver import solve

app = FastAPI(
    title="磁带跨带恢复排程 API",
    version="1.0.0",
    summary="用最少磁带装带次数把跨带文件恢复到中转盘并交付",
    description=(
        "提交中转盘空闲量、磁带上限与文件目录（分片、各分片副本所在带号与位置），"
        "返回最少装带次数的排程。\n\n"
        "* 每片只选一份副本，装带内可跳过分片但只能向前读；\n"
        "* 回到带头重读另算一次装带；\n"
        "* 中转盘必须先容纳所有暂存分片，文件读齐后校验、交付并立即释放其空间；\n"
        "* 最多 6 盘带、12 个分片、48 份副本。\n\n"
        "无解时返回缺失副本的分片，以及需要同时暂存却超出中转盘容量的文件。"
        "可直接用页面上的 *Try it out* 运行内置样例。"
    ),
    openapi_tags=[
        {
            "name": "schedule",
            "description": "排程计算与样例",
        }
    ],
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["schedule"], summary="健康检查")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/example",
    response_model=ScheduleRequest,
    tags=["schedule"],
    summary="获取可直接提交的样例请求",
)
def example() -> ScheduleRequest:
    return ScheduleRequest.model_validate(DEFAULT_EXAMPLE)


@app.post(
    "/schedule",
    response_model=ScheduleResponse,
    tags=["schedule"],
    summary="计算最少装带次数的恢复排程",
    responses={
        200: {"description": "排程结果（可行或不可行）"},
        422: {"description": "请求格式或上限校验失败"},
    },
)
def schedule(request: ScheduleRequest) -> ScheduleResponse:
    try:
        return solve(request)
    except Exception as exc:  # noqa: BLE001 - surface a clear API error
        raise HTTPException(status_code=500, detail=f"排程计算失败: {exc}") from exc
