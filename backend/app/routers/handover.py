"""交接与事件日迁冷接口：跨序列索引与冷热分层的按事件日操作。

这些路径注册在通用资源路由之前，避免被 ``/api/{module}`` 动态路径抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import handover_state as hs
from app.services.series_index import SeriesIndexService
from app.services.tier_storage import ColdWriteError, TierStorageService

router = APIRouter(prefix="/api", tags=["事件日交接"])

_index_service = SeriesIndexService()
_tier_service = TierStorageService()


class RollupWindowPayload(BaseModel):
    window_seq: int = Field(ge=1)
    value: float
    late_report: bool = False


class SealPayload(BaseModel):
    next_day_index_id: int
    next_day_window_seq: int = 1
    sealed_by: str | None = None


class VerifyPayload(BaseModel):
    segment: str
    verified_by: str | None = None


class CorrectionPayload(BaseModel):
    segment: str
    corrected_value: float
    window_seq: int | None = None


class RecomputePayload(BaseModel):
    segment: str
    approved: bool = False


class EvictPayload(BaseModel):
    fail_after: int | None = Field(default=None, ge=0)
    actor: str | None = None


def _raise_if_error(result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result

@router.get("/handovers")
def list_handovers(
    point: str | None = Query(default=None),
    event_day: str | None = Query(default=None),
) -> dict[str, Any]:
    return _index_service.list_handovers(point_key=point, event_day=event_day)


@router.get("/handovers/{handover_no}")
def get_handover(handover_no: str) -> dict[str, Any]:
    return _raise_if_error(_index_service.get_handover(handover_no))


@router.get("/handovers/{handover_no}/readback")
def read_back(
    handover_no: str,
    segment: str | None = Query(default=None),
) -> dict[str, Any]:
    return _raise_if_error(_tier_service.read_back(handover_no, segment))


@router.post("/handovers/{handover_no}/evict")
def evict_event_day(handover_no: str, payload: EvictPayload) -> dict[str, Any]:
    try:
        result = _tier_service.evict_event_day(
            handover_no, fail_after=payload.fail_after, actor=payload.actor)
    except ColdWriteError as exc:
        # 中断是预期内的可恢复状态：返回 409 和已持久化的检查点，便于用原交接号续写。
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "recoverable": True,
                "handover_no": handover_no,
                "checkpoint": _tier_service.migration_checkpoint(handover_no),
            },
        ) from exc
    return _raise_if_error(result)


@router.get("/handovers/{handover_no}/checkpoint")
def migration_checkpoint(handover_no: str) -> dict[str, Any]:
    return _tier_service.migration_checkpoint(handover_no)


@router.post("/handovers/{handover_no}/reconcile")
def reconcile_event_day(handover_no: str) -> dict[str, Any]:
    return _raise_if_error(_tier_service.reconcile_event_day(handover_no))


@router.post("/series_index/{index_id}/handover")
def open_handover(index_id: int, previous_day_handover: str | None = Query(default=None)) -> dict[str, Any]:
    return _raise_if_error(
        _index_service.open_handover(index_id, previous_day_handover=previous_day_handover))


@router.post("/series_index/{index_id}/handover/windows")
def record_rollup_window(index_id: int, payload: RollupWindowPayload) -> dict[str, Any]:
    return _raise_if_error(
        _index_service.record_rollup_window(
            index_id,
            window_seq=payload.window_seq,
            value=payload.value,
            late_report=payload.late_report,
        )
    )


@router.post("/series_index/{index_id}/handover/seal")
def seal_event_day(index_id: int, payload: SealPayload) -> dict[str, Any]:
    return _raise_if_error(
        _index_service.confirm_next_day_window(
            index_id,
            next_day_index_id=payload.next_day_index_id,
            next_day_window_seq=payload.next_day_window_seq,
            sealed_by=payload.sealed_by,
        )
    )


@router.post("/series_index/{index_id}/handover/verify")
def verify_segment(index_id: int, payload: VerifyPayload) -> dict[str, Any]:
    return _raise_if_error(
        _index_service.verify_segment(index_id, payload.segment, verified_by=payload.verified_by))


@router.post("/series_index/{index_id}/handover/correction")
def record_correction(index_id: int, payload: CorrectionPayload) -> dict[str, Any]:
    return _raise_if_error(
        _index_service.record_correction(
            index_id,
            payload.segment,
            corrected_value=payload.corrected_value,
            window_seq=payload.window_seq,
        )
    )


@router.post("/series_index/{index_id}/handover/recompute")
def recompute_segment(index_id: int, payload: RecomputePayload) -> dict[str, Any]:
    return _raise_if_error(
        _index_service.recompute_segment(index_id, payload.segment, approved=payload.approved))


__all__ = ["router", "hs"]
