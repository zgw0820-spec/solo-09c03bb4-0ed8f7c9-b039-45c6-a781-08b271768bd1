"""FastAPI application: the restore scheduler and the read-verification audit."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .audit_api import router as audit_router
from .models import DEFAULT_EXAMPLE, ScheduleRequest, ScheduleResponse
from .solver import solve

app = FastAPI(
    title="磁带恢复排程与读片巡检 API",
    version="1.1.0",
    summary="最少装带次数排程 + 磁带读片校验回执的故障归因",
    description=(
        "## 排程 /schedule\n"
        "提交中转盘空闲量、磁带上限与文件目录（分片、各分片副本所在带号与位置），"
        "返回最少装带次数的排程。每片只选一份副本，装带内只能向前读；回到带头"
        "另算一次装带；文件读齐即校验交付并释放空间。\n\n"
        "## 巡检 /audit\n"
        "用 /schedule 格式的目录与两台带机编号开一轮巡检，然后按回执号成批报送或"
        "撤回读片校验结果。每条回执指明带机与具体副本（replica_id 或位置）。\n"
        "* 读片时带机与副本都正常则通过，任一有故障则失败；\n"
        "* 假设带机与副本合计最多 2 处持续故障；\n"
        "* 同号原样重报不重复计数，内容不同整批拒收，撤回后该号不复用；\n"
        "* 列出能解释全部有效回执、且去掉任一故障就解释不通的极小组合；\n"
        "* 无法解释的批次整批拒收；并发更新不会丢掉已生效改动，重启后目录与有效回执一致。\n\n"
        "可直接用各端点页面的 *Try it out* 运行内置样例。"
    ),
    openapi_tags=[
        {"name": "schedule", "description": "排程计算与样例"},
        {
            "name": "audit",
            "description": "读片巡检：回执报送、故障归因、过滤后的排程目录",
        },
    ],
)
app.include_router(audit_router)


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
    summary="获取可直接提交的排程样例请求",
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
