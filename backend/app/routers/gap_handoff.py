"""增量缺口重放清单专用路由：导出清单文件、导入认领、回执缩减、重放队列同步。

路由前缀 /api/incr-gap 与通用资源路由 /api/{module} 区分，必须在资源路由之前注册。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.gap_handoff import GapHandoffService, manifest_hash

router = APIRouter(prefix="/api/incr-gap", tags=["增量缺口重放清单"])

service = GapHandoffService()


class GapSpec(BaseModel):
    gap_no: str | None = None
    source_endpoint: str | None = None
    origin_cursor: str | None = None
    cursor_from: int
    cursor_to: int
    gap_log: str = ""
    coverage_basis: str = "缺口日志待补采"


class ObservationSpec(BaseModel):
    source_endpoint: str
    origin_cursor: str
    cursor_from: int
    cursor_to: int
    basis: str = "已验证观察范围"
    verified_by: str | None = None


class ExportPayload(BaseModel):
    tracker_id: int | None = None
    endpoint: str | None = None
    origin_cursor: str | None = None
    operator: str = "前班值班员"
    shift_team: str = "采集值班A组"
    gaps: list[GapSpec] = Field(default_factory=list)
    observations: list[ObservationSpec] = Field(default_factory=list)


class ImportPayload(BaseModel):
    manifest: dict[str, Any]
    observations: list[ObservationSpec] = Field(default_factory=list)
    operator: str = "接班值班员"
    shift_team: str = "采集值班B组"


class ReceiptSpec(BaseModel):
    source_endpoint: str | None = None
    origin_cursor: str | None = None
    cursor_from: int
    cursor_to: int
    basis: str = "实际补齐回执"
    source: str = "重放补齐回执"


class ReceiptPayload(BaseModel):
    operator: str = "接班值班员"
    receipts: list[ReceiptSpec] = Field(default_factory=list)


def _result(ok: bool, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": ok, "message": message, **extra}


@router.post("/export")
def export_manifest(payload: ExportPayload) -> dict[str, Any]:
    """导出增量缺口重放清单文件（含增量游标、缺口日志和文件指纹）。"""
    manifest = service.export_manifest(
        tracker_id=payload.tracker_id,
        endpoint=payload.endpoint,
        origin_cursor=payload.origin_cursor,
        gaps=[g.model_dump() for g in payload.gaps],
        operator=payload.operator,
        shift_team=payload.shift_team,
        observations=[o.model_dump(exclude_none=True) for o in payload.observations],
    )
    return _result(True, "增量缺口重放清单已导出", manifest=manifest, file_hash=manifest["file_hash"])


@router.post("/import")
def import_manifest(payload: ImportPayload) -> dict[str, Any]:
    """导入清单文件：按已验证覆盖依据缩减待重放区间，同一文件重复提交幂等。"""
    handoff, message = service.import_manifest(
        payload.manifest,
        observations=[o.model_dump(exclude_none=True) for o in payload.observations],
        operator=payload.operator,
        shift_team=payload.shift_team,
    )
    detail = service.get_handoff_detail(handoff["id"]) if handoff is not None else None
    return _result(handoff is not None, message, handoff=handoff, detail=detail,
                   file_hash=payload.manifest.get("file_hash") or manifest_hash(payload.manifest))


@router.get("/handoffs")
def list_handoffs() -> dict[str, Any]:
    """交接记录列表：追踪员核对原导出范围与本轮新取得的数据。"""
    items = service.list_handoffs()
    return _result(True, "ok", items=items, total=len(items))


@router.get("/handoffs/{handoff_id}")
def get_handoff(handoff_id: int) -> dict[str, Any]:
    detail = service.get_handoff_detail(handoff_id)
    if detail is None:
        return _result(False, f"交接记录 {handoff_id} 不存在")
    return _result(True, "ok", detail=detail)


@router.post("/handoffs/{handoff_id}/claim")
def claim_handoff(handoff_id: int, payload: ReceiptPayload | None = None) -> dict[str, Any]:
    """接班人认领补采：待重放段挂钩重放队列。"""
    operator = payload.operator if payload else "接班值班员"
    handoff, message = service.claim_handoff(handoff_id, operator=operator)
    detail = service.get_handoff_detail(handoff_id) if handoff is not None else None
    return _result(handoff is not None, message, handoff=handoff, detail=detail)


@router.post("/handoffs/{handoff_id}/receipts")
def submit_receipts(handoff_id: int, payload: ReceiptPayload) -> dict[str, Any]:
    """按实际补齐回执缩减待重放区间（导入中断后续做）。"""
    handoff, message = service.submit_receipt(
        handoff_id, [r.model_dump(exclude_none=True) for r in payload.receipts],
        operator=payload.operator)
    detail = service.get_handoff_detail(handoff_id) if handoff is not None else None
    return _result(handoff is not None, message, handoff=handoff, detail=detail)


@router.post("/handoffs/{handoff_id}/sync-replay")
def sync_replay(handoff_id: int) -> dict[str, Any]:
    """重放队列用实际补齐回执确认待领取数据，结果同步回导入清单。"""
    handoff, message = service.sync_replay_queue(handoff_id)
    detail = service.get_handoff_detail(handoff_id) if handoff is not None else None
    return _result(handoff is not None, message, handoff=handoff, detail=detail)


@router.post("/handoffs/{handoff_id}/review")
def review_handoff(handoff_id: int) -> dict[str, Any]:
    """追踪员复核原导出范围与本轮新取得的数据。"""
    handoff, message = service.review_handoff(handoff_id)
    detail = service.get_handoff_detail(handoff_id) if handoff is not None else None
    return _result(handoff is not None, message, handoff=handoff, detail=detail)
