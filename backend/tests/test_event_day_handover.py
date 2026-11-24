"""事件日交接与按日迁冷场景测试。

场景：旧日两个归并窗口分别产生 20（前段）和 30（末段），新日首窗 40。
旧日封存后末段才到一笔迟报 7，末段又被更正（30 → 35）但尚未完成重算；
旧班按独立核验的前段 20 交接，当班按整日核对。验证旧日迁冷只归档前段 20，
末段留热区等待重算，新日 40 仍归新日；更正获准重算后再迁冷读回更正后的值；
冷区写入中断后按原交接号续写且前段 20 不变。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import handover_state as hs
from app.services.series_index import SeriesIndexService
from app.services.tier_storage import ColdWriteError, TierStorageService
from app.store import store

OLD_DAY = "2026-11-20"
NEW_DAY = "2026-11-21"
POINT = "SENS-HO-1"
LATE_VALUE = 7
CORRECTED_TAIL = 35


@pytest.fixture(autouse=True)
def isolated_data_dir():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patch("app.services.base.DATA_ROOT", root):
            # 清理可能残留的分区行，保证用例互不影响。
            for module in ("series_index", "tier_storage"):
                rows = store.rows(module)
                rows[:] = [r for r in rows if POINT not in str(r.get("测点编号", ""))]
            yield root


def _make_partition(svc: SeriesIndexService, index_no: str, day: str) -> dict:
    row = svc.create_entry({
        "索引编号": index_no,
        "测点编号": POINT,
        "时间分区": day,
        "点区间": "0-0",
        "索引粒度": "1h",
        "索引条数": "0",
        "存储位置": "",
        "索引版本": "v1",
        "交接号": "",
        "交接状态": "",
        "存储层级": "",
    })[0]
    assert row is not None
    return row


def _build_scenario() -> dict:
    svc = SeriesIndexService()
    old_row = _make_partition(svc, "IDX-OLD", OLD_DAY)
    new_row = _make_partition(svc, "IDX-NEW", NEW_DAY)

    # 旧日两个窗口：前段 20、末段 30
    svc.record_rollup_window(old_row["id"], window_seq=1, value=20)
    svc.record_rollup_window(old_row["id"], window_seq=2, value=30)
    # 新日首窗 40
    svc.record_rollup_window(new_row["id"], window_seq=1, value=40)

    # 旧班已独立核验前段
    svc.verify_segment(old_row["id"], hs.SEGMENT_FRONT, verified_by="旧班")
    # 新日首窗确认，旧日封存；随后旧日末段到一笔迟报
    svc.confirm_next_day_window(
        old_row["id"], next_day_index_id=new_row["id"], sealed_by="新日首窗确认")
    svc.record_rollup_window(old_row["id"], window_seq=3, value=LATE_VALUE, late_report=True)
    # 末段收到更正但尚未重算
    svc.record_correction(
        old_row["id"], hs.SEGMENT_TAIL,
        corrected_value=CORRECTED_TAIL, window_seq=2)
    return {"old": old_row, "new": new_row, "svc": svc, "tier": TierStorageService()}


def test_old_day_has_confirmed_front_and_rewrite_pending_tail():
    ctx = _build_scenario()
    old_no = hs.make_handover_no(POINT, OLD_DAY)
    handover = hs.load_state(old_no)
    assert handover is not None

    front = handover["segments"][hs.SEGMENT_FRONT]
    tail = handover["segments"][hs.SEGMENT_TAIL]
    assert front["state"] == hs.SEG_COVERED
    assert front["verified"] is True
    assert front["published_value"] == 20
    # 末段：迟报后待重写，更正后待重算，发布值仍为原 30
    assert tail["state"] == hs.SEG_CORRECTED
    assert tail["published_value"] == 30
    assert tail["corrected_value"] == CORRECTED_TAIL
    assert handover["status"] == "已确认覆盖（末段待重写）"
    assert handover["sealed"] is True
    # 迟报没有改写已发布的末段口径：末段仍是 30，迟报 7 单列
    assert tail["windows"] == [
        {"seq": 2, "value": 30, "late_report": False},
        {"seq": 3, "value": LATE_VALUE, "late_report": True},
    ]


def test_new_day_window_still_belongs_to_new_day():
    ctx = _build_scenario()
    new_no = hs.make_handover_no(POINT, NEW_DAY)
    new_state = hs.load_state(new_no)
    assert new_state is not None
    assert new_state["event_day"] == NEW_DAY
    assert new_state["segments"][hs.SEGMENT_FRONT]["published_value"] == 40
    assert hs.day_total(new_state) == 40
    # 前后日互相引用，跨日核对不会串日
    old_no = hs.make_handover_no(POINT, OLD_DAY)
    assert new_state["previous_day"] == old_no
    assert hs.load_state(old_no)["next_day"] == new_no


def test_evict_archives_only_front_and_reads_back_from_tier():
    ctx = _build_scenario()
    tier: TierStorageService = ctx["tier"]
    old_no = hs.make_handover_no(POINT, OLD_DAY)

    result = tier.evict_event_day(old_no, actor="值班员")
    assert result["archived_segments"] == [hs.SEGMENT_FRONT]
    assert hs.SEGMENT_TAIL in result["skipped_segments"]
    assert "重算" in result["skipped_segments"][hs.SEGMENT_TAIL]
    assert result["tier"] == hs.TIER_PARTIAL

    # 直接从业已迁冷的层级读回前段：确实来自冷区，值仍是 20
    readback = tier.read_back(old_no, hs.SEGMENT_FRONT)
    front = readback["requested_segment"]
    assert front["source"] == "cold"
    assert front["values"] == [20]
    assert front["total"] == 20

    # 末段仍留热区，并给出等待重算的原因
    tail = readback["segments"][hs.SEGMENT_TAIL]
    assert tail["source"] == "hot"
    assert tail["tier"] == hs.TIER_HOT
    assert tail["reason"] and "重算" in tail["reason"]

    # 整日核对：新日 40 仍归新日
    new_no = hs.make_handover_no(POINT, NEW_DAY)
    new_readback = tier.read_back(new_no)
    assert new_readback["segments"][hs.SEGMENT_FRONT]["total"] == 40
    assert new_readback["event_day"] == NEW_DAY


def test_reconcile_reports_pending_tail_and_no_discrepancy():
    ctx = _build_scenario()
    tier: TierStorageService = ctx["tier"]
    old_no = hs.make_handover_no(POINT, OLD_DAY)
    tier.evict_event_day(old_no, actor="值班员")

    reconciled = tier.reconcile_event_day(old_no)
    assert reconciled["reconciled"] is True
    assert reconciled["pending_segments"] == [hs.SEGMENT_TAIL]
    assert reconciled["published"][hs.SEGMENT_FRONT]["source"] == "cold"
    assert reconciled["published"][hs.SEGMENT_TAIL]["source"] == "hot"


def test_correction_requires_approval_then_evicts_again_with_corrected_value():
    ctx = _build_scenario()
    tier: TierStorageService = ctx["tier"]
    svc: SeriesIndexService = ctx["svc"]
    old_row = ctx["old"]
    old_no = hs.make_handover_no(POINT, OLD_DAY)
    tier.evict_event_day(old_no, actor="值班员")

    # 未获准的重算不能把末段转成已确认覆盖
    rejected = svc.recompute_segment(old_row["id"], hs.SEGMENT_TAIL, approved=False)
    assert "error" in rejected

    # 获准重算：末段转为已确认覆盖，发布口径进版
    approved = svc.recompute_segment(old_row["id"], hs.SEGMENT_TAIL, approved=True)
    assert "error" not in approved
    assert approved["handover"]["segments"][hs.SEGMENT_TAIL]["state"] == hs.SEG_COVERED
    assert approved["handover"]["published_version"] == "v2"

    # 再次迁冷：这次末段也可以归档，整日已全冷
    again = tier.evict_event_day(old_no, actor="值班员")
    assert again["archived_segments"] == [hs.SEGMENT_FRONT, hs.SEGMENT_TAIL]
    assert again["tier"] == hs.TIER_COLD

    readback = tier.read_back(old_no)
    assert readback["segments"][hs.SEGMENT_FRONT]["source"] == "cold"
    assert readback["segments"][hs.SEGMENT_FRONT]["total"] == 20
    assert readback["segments"][hs.SEGMENT_TAIL]["source"] == "cold"
    # 更正 30 → 35 后连同已重写的迟报 7 一起归档：35 + 7 = 42
    assert readback["segments"][hs.SEGMENT_TAIL]["total"] == CORRECTED_TAIL + LATE_VALUE


def test_interrupted_cold_write_resumes_with_original_handover_no():
    ctx = _build_scenario()
    svc: SeriesIndexService = ctx["svc"]
    old_row = ctx["old"]
    tier: TierStorageService = ctx["tier"]
    old_no = hs.make_handover_no(POINT, OLD_DAY)

    # 先让末段获准重算，制造一段多窗口、需要分多次写的冷区写入
    svc.recompute_segment(old_row["id"], hs.SEGMENT_TAIL, approved=True)

    # 模拟写到第 2 个窗口前中断：第一条已写入并推进检查点
    with pytest.raises(ColdWriteError) as excinfo:
        tier.evict_event_day(old_no, fail_after=1, actor="值班员")
    assert old_no in str(excinfo.value)
    checkpoint = tier.migration_checkpoint(old_no)
    assert checkpoint["status"] == "failed"
    assert checkpoint["completed"][hs.SEGMENT_FRONT] == [1]

    # 冷区里只有写完的那一条，且前段 20 已经可读回
    cold_file = hs.cold_archive_path(old_no)
    records = [json.loads(line) for line in cold_file.read_text(encoding="utf-8").splitlines() if line]
    assert len(records) == 1
    assert records[0]["segment"] == hs.SEGMENT_FRONT
    assert records[0]["value"] == 20

    # 重启后用原交接号续写：不重写已完成的前段窗口
    resumed = tier.evict_event_day(old_no, actor="值班员")
    assert resumed["archived_segments"] == [hs.SEGMENT_FRONT, hs.SEGMENT_TAIL]
    assert resumed["checkpoint"]["attempts"] == 2

    front_records = [
        r for r in hs.read_cold_records(old_no) if r["segment"] == hs.SEGMENT_FRONT
    ]
    # 前段窗口只被写过一次（续写时跳过）
    assert [r["window_seq"] for r in front_records] == [1]

    readback = tier.read_back(old_no, hs.SEGMENT_FRONT)
    assert readback["requested_segment"]["values"] == [20]
    assert readback["requested_segment"]["source"] == "cold"

    # 冷区记录经过去重后仍是一份完整事件日
    deduped = hs.dedupe_cold_records(hs.read_cold_records(old_no))
    assert {(r["segment"], r["window_seq"]) for r in deduped} == {
        (hs.SEGMENT_FRONT, 1), (hs.SEGMENT_TAIL, 2), (hs.SEGMENT_TAIL, 3)
    }


def test_half_written_line_is_repaired_before_resume():
    ctx = _build_scenario()
    svc: SeriesIndexService = ctx["svc"]
    tier: TierStorageService = ctx["tier"]
    svc.recompute_segment(ctx["old"]["id"], hs.SEGMENT_TAIL, approved=True)
    old_no = hs.make_handover_no(POINT, OLD_DAY)

    with pytest.raises(ColdWriteError):
        tier.evict_event_day(old_no, fail_after=1, actor="值班员")

    # 模拟进程在写末段窗口 2 时被杀：文件尾部留下半行 JSON
    cold_file = hs.cold_archive_path(old_no)
    with open(cold_file, "a", encoding="utf-8") as fh:
        fh.write('{"handover_no": "%s", "segment": "末段", "window_seq": 2, "val' % old_no)

    resumed = tier.evict_event_day(old_no, actor="值班员")
    assert resumed["tier"] == hs.TIER_COLD

    # 修掉半行后，每条记录都能解析，前段 20 没有被重写或损坏
    raw_lines = [
        line for line in cold_file.read_text(encoding="utf-8").splitlines() if line
    ]
    parsed = [json.loads(line) for line in raw_lines]
    assert len(parsed) == 3
    front = [r for r in parsed if r["segment"] == hs.SEGMENT_FRONT]
    assert front == [
        {
            "handover_no": old_no,
            "point": POINT,
            "event_day": OLD_DAY,
            "segment": hs.SEGMENT_FRONT,
            "window_seq": 1,
            "value": 20,
            "late_report": False,
            "published_version": "v2",
            "revision": 2,
            "archived_at": front[0]["archived_at"],
        }
    ]

    readback = tier.read_back(old_no, hs.SEGMENT_FRONT)
    assert readback["requested_segment"]["values"] == [20]
    assert readback["requested_segment"]["source"] == "cold"


def test_evict_refuses_unsealed_day():
    svc = SeriesIndexService()
    row = _make_partition(svc, "IDX-OPEN", OLD_DAY)
    svc.record_rollup_window(row["id"], window_seq=1, value=20)
    svc.record_rollup_window(row["id"], window_seq=2, value=30)
    result = TierStorageService().evict_event_day(hs.make_handover_no(POINT, OLD_DAY))
    assert "error" in result
    assert "封存" in result["error"]


def test_partition_rows_expose_handover_state():
    ctx = _build_scenario()
    rows, total = ctx["svc"].list_entries(filters={"交接状态": "已确认覆盖（末段待重写）"})
    assert total == 1
    assert rows[0]["时间分区"] == OLD_DAY
    assert rows[0]["存储层级"] == hs.TIER_HOT
    assert rows[0]["pending"] is True
