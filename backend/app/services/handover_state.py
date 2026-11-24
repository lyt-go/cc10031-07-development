"""事件日分区交接状态：序列索引与冷热分层共享的持久化布局与状态工具。

一个「事件日分区」以交接号 ``HO-<测点>-<事件日>`` 为稳定标识，状态落在::

    data/handover/<交接号>.json                       事件日交接状态（分段、发布值、层级）
    data/tier_storage/<交接号>_cold_archive.jsonl     冷区归档记录（追加写，按分段+窗口去重）
    data/tier_storage/<交接号>_cold_checkpoint.json   冷区写入检查点（逐条写入水位）

状态文件与检查点都通过「临时文件 + 原子替换」落盘，因此冷区写到一半失败、
进程重启后仍能按原交接号找到未写完的区间继续写，并读回已经归档的前段。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from app.services import base as base_module
from app.store import store

SEGMENT_FRONT = "前段"
SEGMENT_TAIL = "末段"
SEGMENTS: tuple[str, str] = (SEGMENT_FRONT, SEGMENT_TAIL)

# 分段状态
SEG_PENDING = "待确认"
SEG_RECORDED = "待封存"
SEG_COVERED = "已确认覆盖"
SEG_LATE = "待重写"
SEG_CORRECTED = "更正待重算"

# 分段真正可归档的口径：只有「已确认覆盖」可迁冷。
COVERED_STATES = {SEG_COVERED}
# 事件日「已确认覆盖」的判定口径：每个已归并分段都已核验或已明确待重写/待重算；
# 待重写的末段仍算当日已确认覆盖，但要单列出来，且不能迁冷。
CONFIRMED_STATES = {SEG_COVERED, SEG_LATE, SEG_CORRECTED}

TIER_HOT = "hot"
TIER_COLD = "cold"
TIER_PARTIAL = "partial_cold"


# ---------------------------------------------------------------- 路径与文件

def data_root() -> Path:
    """每次调用时读取 base.DATA_ROOT，测试里 patch 基类即可整体改到临时目录。"""
    return Path(base_module.DATA_ROOT)


def handover_dir() -> Path:
    p = data_root() / "handover"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tier_dir() -> Path:
    p = data_root() / "tier_storage"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sanitize_handover_no(handover_no: str) -> str:
    cleaned = str(handover_no).strip().replace("/", "-").replace("\\", "-").replace("\0", "")
    if not cleaned:
        raise ValueError("交接号不能为空")
    return cleaned


def make_handover_no(point_key: str, event_day: str) -> str:
    return sanitize_handover_no(f"HO-{point_key}-{event_day}")


def state_path(handover_no: str) -> Path:
    return handover_dir() / f"{sanitize_handover_no(handover_no)}.json"


def cold_archive_path(handover_no: str) -> Path:
    return tier_dir() / f"{sanitize_handover_no(handover_no)}_cold_archive.jsonl"


def checkpoint_path(handover_no: str) -> Path:
    return tier_dir() / f"{sanitize_handover_no(handover_no)}_cold_checkpoint.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------- 状态结构

def _blank_segment() -> dict[str, Any]:
    return {
        "state": SEG_PENDING,
        "windows": [],
        "published_value": None,
        "corrected_value": None,
        "verified": False,
        "reason": None,
        "tier": TIER_HOT,
        "archivable": False,
        "rewrite_pending": False,
        "archived_at": None,
        "recomputed_at": None,
    }


def new_state(point_key: str, event_day: str, handover_no: str) -> dict[str, Any]:
    now = time.time()
    return {
        "version": 1,
        "handover_no": handover_no,
        "point": point_key,
        "event_day": event_day,
        "status": "已登记",
        "sealed": False,
        "sealed_by": None,
        "next_day": None,
        "previous_day": None,
        "published_version": "v1",
        "published_revision": 1,
        "segments": {seg: _blank_segment() for seg in SEGMENTS},
        "tier": TIER_HOT,
        "created_at": now,
        "updated_at": now,
    }


def load_state(handover_no: str) -> dict[str, Any] | None:
    state = read_json(state_path(handover_no))
    if state is None:
        return None
    # 补齐旧状态文件里可能缺失的字段，保证跨版本可读。
    state.setdefault("segments", {seg: _blank_segment() for seg in SEGMENTS})
    for seg in SEGMENTS:
        segment = state["segments"].setdefault(seg, _blank_segment())
        for key, default in _blank_segment().items():
            segment.setdefault(key, default)
    state.setdefault("published_revision", 1)
    state.setdefault("published_version", "v1")
    state.setdefault("tier", TIER_HOT)
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = time.time()
    atomic_write_json(state_path(state["handover_no"]), state)


# ---------------------------------------------------------------- 分段与发布值

def window_segment(window_seq: int) -> str:
    """首个归并窗口是旧班交接读取的「前段」，其余窗口归「末段」。"""
    return SEGMENT_FRONT if int(window_seq) == 1 else SEGMENT_TAIL


def segment_windows(state: dict[str, Any], segment: str) -> list[dict[str, Any]]:
    return sorted(
        state["segments"][segment].get("windows", []),
        key=lambda window: int(window.get("seq", 0)),
    )


def segment_values(state: dict[str, Any], segment: str) -> list[Any]:
    return [window.get("value") for window in segment_windows(state, segment)]


def is_covered(segment_data: dict[str, Any]) -> bool:
    return segment_data.get("state") in COVERED_STATES and bool(segment_data.get("windows"))


def blocked_reason(segment_data: dict[str, Any]) -> str | None:
    if is_covered(segment_data):
        return None
    if not segment_data.get("windows"):
        return "该分段尚未归并"
    state = segment_data.get("state")
    if state == SEG_LATE:
        return segment_data.get("reason") or "末段收到迟报，等待重写"
    if state == SEG_CORRECTED:
        return segment_data.get("reason") or "末段收到更正但重算未完成，仍留热区"
    if state in (SEG_PENDING, SEG_RECORDED):
        return "该事件日尚未封存，不能迁冷"
    return segment_data.get("reason") or f"分段状态为{state}"


def published_map(state: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for seg in SEGMENTS:
        if segment_windows(state, seg):
            result[seg] = state["segments"][seg].get("published_value")
    return result


def day_total(state: dict[str, Any]) -> float:
    return sum(
        float(value or 0)
        for value in published_map(state).values()
        if value is not None
    )


def pending_segments(state: dict[str, Any]) -> list[str]:
    return [
        seg for seg in SEGMENTS
        if segment_windows(state, seg) and not is_covered(state["segments"][seg])
    ]


def derive_tier(state: dict[str, Any]) -> str:
    recorded = [seg for seg in SEGMENTS if segment_windows(state, seg)]
    if not recorded:
        return TIER_HOT
    cold = [seg for seg in recorded if state["segments"][seg].get("tier") == TIER_COLD]
    if len(cold) == len(recorded):
        return TIER_COLD
    if cold:
        return TIER_PARTIAL
    return TIER_HOT


def refresh_state(state: dict[str, Any]) -> dict[str, Any]:
    """按分段状态重新计算事件日覆盖状态与层级。"""
    state["tier"] = derive_tier(state)
    recorded = [seg for seg in SEGMENTS if segment_windows(state, seg)]
    if not recorded:
        state["status"] = "已登记"
    elif not state.get("sealed"):
        state["status"] = "待封存"
    elif not all(seg_data.get("state") in CONFIRMED_STATES
                 for seg, seg_data in state["segments"].items() if segment_windows(state, seg)):
        state["status"] = "待确认"
    elif pending_segments(state):
        state["status"] = "已确认覆盖（末段待重写）"
    else:
        state["status"] = "已确认覆盖"
    return state


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    """面向接口的交接视图：分段状态、发布值、层级一屏可核对。"""
    segments: dict[str, Any] = {}
    for seg in SEGMENTS:
        data = state["segments"][seg]
        segments[seg] = {
            "state": data.get("state"),
            "recorded": bool(data.get("windows")),
            "verified": bool(data.get("verified")),
            "tier": data.get("tier", TIER_HOT),
            "published_value": data.get("published_value"),
            "corrected_value": data.get("corrected_value"),
            "reason": data.get("reason"),
            "archivable": bool(data.get("archivable")),
            "archived_at": data.get("archived_at"),
            "recomputed_at": data.get("recomputed_at"),
            "windows": [
                {
                    "seq": int(window.get("seq", 0)),
                    "value": window.get("value"),
                    "late_report": bool(window.get("late_report")),
                }
                for window in segment_windows(state, seg)
            ],
        }
    return {
        "handover_no": state["handover_no"],
        "point": state["point"],
        "event_day": state["event_day"],
        "status": state["status"],
        "sealed": bool(state.get("sealed")),
        "sealed_by": state.get("sealed_by"),
        "tier": state.get("tier", TIER_HOT),
        "published_version": state.get("published_version", "v1"),
        "published_revision": state.get("published_revision", 1),
        "published": published_map(state),
        "day_total": day_total(state),
        "pending_segments": pending_segments(state),
        "segments": segments,
        "next_day": state.get("next_day"),
        "previous_day": state.get("previous_day"),
    }


# ---------------------------------------------------------------- 冷区归档

def read_cold_records(handover_no: str) -> list[dict[str, Any]]:
    path = cold_archive_path(handover_no)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def dedupe_cold_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一分段+窗口重复写入时保留最后一条，保证读回不重复。"""
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for rec in records:
        key = (str(rec.get("segment", "")), int(rec.get("window_seq", 0)))
        latest[key] = rec
    return sorted(latest.values(), key=lambda r: (str(r.get("segment", "")), int(r.get("window_seq", 0))))


def append_cold_record(handover_no: str, record: dict[str, Any]) -> None:
    path = cold_archive_path(handover_no)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 保证与上一条之间一定换行：进程中断可能留下没有换行的半行，续写时先补齐。
    needs_newline = False
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as probe:
            probe.seek(-1, os.SEEK_END)
            needs_newline = probe.read(1) != b"\n"
    with open(path, "a", encoding="utf-8") as fh:
        if needs_newline:
            fh.write("\n")
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def repair_cold_archive(handover_no: str, checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """以检查点为准修复冷区归档：丢弃中断留下的半行与未记账记录。

    检查点只在整条记录落盘后才推进，因此「检查点里没有的窗口」一律视为没写完。
    修复后文件尾部不会再有半行，续写可以直接追加。
    """
    path = cold_archive_path(handover_no)
    if not path.exists():
        return []
    completed = {
        (seg, int(seq))
        for seg, seqs in checkpoint.get("completed", {}).items()
        for seq in seqs
    }
    kept = dedupe_cold_records(
        [
            rec for rec in read_cold_records(handover_no)
            if (str(rec.get("segment", "")), int(rec.get("window_seq", 0))) in completed
        ]
    )
    tmp = path.with_name(f"{path.name}.repair-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in kept:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)
    return kept


def new_checkpoint(handover_no: str) -> dict[str, Any]:
    return {
        "handover_no": handover_no,
        "status": "pending",  # pending / writing / partial / done
        "completed": {seg: [] for seg in SEGMENTS},
        "attempts": 0,
        "last_attempt_at": None,
        "failed_at": None,
        "finished_at": None,
    }


def load_checkpoint(handover_no: str) -> dict[str, Any] | None:
    checkpoint = read_json(checkpoint_path(handover_no))
    if checkpoint is None:
        return None
    completed = checkpoint.setdefault("completed", {})
    for seg in SEGMENTS:
        completed.setdefault(seg, [])
    return checkpoint


def save_checkpoint(handover_no: str, checkpoint: dict[str, Any]) -> None:
    atomic_write_json(checkpoint_path(handover_no), checkpoint)


def cold_values_by_segment(handover_no: str) -> dict[str, list[Any]]:
    values: dict[str, list[Any]] = {seg: [] for seg in SEGMENTS}
    for rec in dedupe_cold_records(read_cold_records(handover_no)):
        seg = str(rec.get("segment", ""))
        values.setdefault(seg, []).append(rec.get("value"))
    return values


# ---------------------------------------------------------------- 索引分区行

def sync_partition_row(state: dict[str, Any]) -> dict[str, Any]:
    """把交接状态同步到序列索引分区行，供列表接口按「交接状态」检索。"""
    from app.services.series_index import SeriesIndexService  # 延迟导入避免环

    rows = store.rows(SeriesIndexService.MODULE)
    row = next((r for r in rows if r.get("交接号") == state["handover_no"]), None)
    if row is None:
        row = next(
            (
                r for r in rows
                if str(r.get("测点编号")) == state["point"]
                and str(r.get("时间分区")) == state["event_day"]
            ),
            None,
        )
    if row is None:
        row = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        rows.append(row)

    recorded_segments = [seg for seg in SEGMENTS if segment_windows(state, seg)]
    record_count = sum(len(segment_windows(state, seg)) for seg in recorded_segments)
    blocked = pending_segments(state)
    cold_file = cold_archive_path(state["handover_no"]) if state.get("tier") != TIER_HOT else None
    row.update(
        {
            "索引编号": row.get("索引编号") or f"IDX-{state['point']}-{state['event_day']}",
            "测点编号": state["point"],
            "时间分区": state["event_day"],
            "实际采样日": state["event_day"],
            "点区间": f"1-{record_count}" if record_count else "0-0",
            "索引粒度": row.get("索引粒度") or "1h",
            "索引条数": str(record_count),
            "存储位置": str(cold_file or state_path(state["handover_no"])),
            "索引版本": state.get("published_version", "v1"),
            "交接号": state["handover_no"],
            "交接状态": state["status"],
            "存储层级": state.get("tier", TIER_HOT),
            "status": "已构建" if record_count else "草稿",
            "pending": bool(blocked),
            "abnormal": bool(blocked),
        }
    )
    return row
