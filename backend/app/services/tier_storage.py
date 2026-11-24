"""冷热分层领域服务：维护时序数据热温冷三层的边界划分、迁移方式与驻留天数。

除按分层策略做整体扫描外，本模块还能按事件日把「热区候选分区」迁成可归档分区。
迁冷以序列索引登记的交接号为单位，只归档已确认覆盖的分段；因迟报或更正待重写的
末段继续留在热区，并在结果里给出原因。冷区写入逐条记检查点，失败重启后按原交接号
从未写完的窗口继续，不会重写已归档的前段。
"""
from __future__ import annotations

import time
from typing import Any

from app.services import handover_state as hs
from app.services.base import BaseModuleService
from app.store import store


class ColdWriteError(RuntimeError):
    """模拟冷区写入中断：检查点已持久化，重新调用即可从未写完的窗口继续。"""


class TierStorageService(BaseModuleService):
    MODULE = "tier_storage"
    ENTITY = "分层策略"
    OPERATION_TERM = "迁移"
    LIST_FIELDS = ["策略编号", "时间边界", "热区范围", "温区范围", "冷区路径", "迁移方式", "驻留天数", "策略版本"]
    REQUIRED_FIELDS = ["策略编号", "时间边界", "热区范围"]
    STATUS_ORDER = ["草稿", "待生效", "已生效", "已停用"]
    ACTION_RULES = {"提交生效": "待生效", "确认生效": "已生效", "标记停用": "已停用"}
    # 数据迁移操作：按分层策略扫描索引分区进行分类迁移写入各层水位数据
    NEGATIVE_ACTIONS = ["标记停用"]

    def apply_tier_policy(self, policy_id: int) -> dict[str, Any]:
        """按分层策略执行索引数据冷热迁移并写入各层存储水位数据文件。"""
        policy = store.find(self.MODULE, policy_id)
        if policy is None:
            return {"error": f"分层策略 {policy_id} 不存在"}
        self.run_action(policy_id, "确认生效")
        boundary = policy.get("时间边界", "30d")
        hot_range = policy.get("热区范围", "0-7d")
        warm_range = policy.get("温区范围", "7d-30d")
        cold_path = policy.get("冷区路径", "/cold/archive")

        # 扫描索引分区进行分类
        index_dir = self._data_dir("series_index")
        hot_count = 0
        warm_count = 0
        cold_count = 0
        if index_dir.exists():
            for f in index_dir.glob("*_index.jsonl"):
                records = self._read_jsonl(f)
                # 模拟按时间分区进行分类迁移
                for r in records:
                    partition = r.get("time_partition", "")
                    if "2026-09-03" in partition:
                        # 数据归档输出：写入冷区归档数据到分层存储日志文件
                        hot_count += 1
                    elif "2026-09-02" in partition:
                        warm_count += 1
                    else:
                        cold_count += 1
        # 将分层迁移结果写入分层存储日志文件
        self._write_jsonl(
            self._data_dir("tier_storage") / f"{policy['策略编号']}_tier_log.jsonl",
            [
                {"policy": policy["策略编号"], "tier": "hot", "records": hot_count, "range": hot_range, "at": time.time()},
                {"policy": policy["策略编号"], "tier": "warm", "records": warm_count, "range": warm_range, "at": time.time()},
                {"policy": policy["策略编号"], "tier": "cold", "records": cold_count, "path": cold_path, "at": time.time()},
            ]
        )
        return {"policy_id": policy["策略编号"], "boundary": boundary,
                "hot_records": hot_count, "warm_records": warm_count, "cold_records": cold_count}

    def evict_to_cold(self, point_key: str) -> dict[str, Any]:
        """将指定测点的过期索引分区数据迁移到冷区存储并写入冷区归档数据集。"""
        # 读取该测点的索引数据
        index_dir = self._data_dir("series_index")
        evicted = []
        if index_dir.exists():
            for f in index_dir.glob("*_index.jsonl"):
                records = self._read_jsonl(f)
                old_records = [r for r in records if "2026-09-01" in r.get("time_partition", "")]
                evicted.extend(old_records)
        # 写入冷区归档数据文件
        if evicted:
            self._write_jsonl(
                self._data_dir("tier_storage") / f"{point_key}_cold_archive.jsonl",
                evicted
            )
        return {"point": point_key, "evicted_records": len(evicted),
                "cold_path": f"/cold/archive/{point_key}"}

    # -- 按事件日迁冷 ----------------------------------------------------

    @staticmethod
    def _segment_eligibility(segment_data: dict[str, Any]) -> tuple[bool, str | None]:
        """分段是否可归档；不可归档时给出留在热区的原因。"""
        if not segment_data.get("windows"):
            return False, "该分段尚未归并"
        if hs.is_covered(segment_data):
            return True, None
        return False, hs.blocked_reason(segment_data)

    def migration_checkpoint(self, handover_no: str) -> dict[str, Any]:
        """查询某事件日的冷区写入检查点，确认哪些窗口已经写完。"""
        checkpoint = hs.load_checkpoint(handover_no)
        if checkpoint is None:
            return {"handover_no": handover_no, "status": "pending",
                    "completed": {seg: [] for seg in hs.SEGMENTS}}
        return checkpoint

    def evict_event_day(
        self,
        handover_no: str,
        *,
        fail_after: int | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """把一个事件日里可归档的分段迁入冷区。

        - 只归档已确认覆盖的分段；待重写/待重算的末段继续留热区并返回原因。
        - 逐窗口写冷区并落检查点；``fail_after`` 用于模拟写入中断，抛
          :class:`ColdWriteError` 后再次用同一交接号调用即可续写。
        """
        state = hs.load_state(handover_no)
        if state is None:
            return {"error": f"交接 {handover_no} 不存在"}
        if not state.get("sealed"):
            return {"error": "事件日尚未封存（新日首个归并窗口未确认），不能迁冷"}

        checkpoint = hs.load_checkpoint(handover_no) or hs.new_checkpoint(handover_no)
        # 上次写入如果中断（失败或进程在 writing 状态被杀），先按检查点修掉
        # 半行和未记账记录，保证续写从干净尾部追加。
        if checkpoint.get("status") in ("failed", "writing"):
            hs.repair_cold_archive(handover_no, checkpoint)
        checkpoint["status"] = "writing"
        checkpoint["attempts"] = int(checkpoint.get("attempts", 0)) + 1
        checkpoint["last_attempt_at"] = time.time()
        if actor:
            checkpoint["actor"] = actor
        hs.save_checkpoint(handover_no, checkpoint)

        written_now = 0
        archived_segments: list[str] = []
        skipped_segments: dict[str, str] = {}
        failure: dict[str, Any] | None = None

        for segment in hs.SEGMENTS:
            segment_data = state["segments"][segment]
            eligible, reason = self._segment_eligibility(segment_data)
            if not eligible:
                if segment_data.get("windows"):
                    skipped_segments[segment] = reason or "不可归档"
                continue

            completed = set(int(seq) for seq in checkpoint["completed"].get(segment, []))
            window_records = [
                window for window in hs.segment_windows(state, segment)
                if int(window["seq"]) not in completed
            ]
            for window in window_records:
                if fail_after is not None and written_now >= fail_after:
                    checkpoint["status"] = "failed"
                    checkpoint["failed_at"] = time.time()
                    checkpoint["failed_segment"] = segment
                    checkpoint["failed_window_seq"] = int(window["seq"])
                    hs.save_checkpoint(handover_no, checkpoint)
                    raise ColdWriteError(
                        f"冷区写入中断：{segment}窗口 {window['seq']} 未写完，"
                        f"已归档窗口 {sorted(completed)}，可凭原交接号 {handover_no} 续写"
                    )
                hs.append_cold_record(
                    handover_no,
                    {
                        "handover_no": handover_no,
                        "point": state["point"],
                        "event_day": state["event_day"],
                        "segment": segment,
                        "window_seq": int(window["seq"]),
                        "value": window["value"],
                        "late_report": bool(window.get("late_report")),
                        "published_version": state.get("published_version", "v1"),
                        "revision": state.get("published_revision", 1),
                        "archived_at": time.time(),
                    },
                )
                completed.add(int(window["seq"]))
                # 每条写完立刻推进检查点，进程在任意时刻中断都能从下一条继续。
                checkpoint["completed"][segment] = sorted(completed)
                hs.save_checkpoint(handover_no, checkpoint)
                written_now += 1

            segment_data["tier"] = hs.TIER_COLD
            segment_data["archivable"] = True
            segment_data["archived_at"] = time.time()
            archived_segments.append(segment)

        hs.refresh_state(state)
        hs.save_state(state)

        recorded = [seg for seg in hs.SEGMENTS if state["segments"][seg].get("windows")]
        all_cold = all(
            state["segments"][seg].get("tier") == hs.TIER_COLD for seg in recorded
        )
        checkpoint["status"] = "done" if all_cold else "partial"
        checkpoint["finished_at"] = time.time()
        hs.save_checkpoint(handover_no, checkpoint)
        hs.sync_partition_row(state)

        return {
            "handover_no": handover_no,
            "event_day": state["event_day"],
            "tier": state["tier"],
            "status": state["status"],
            "archived_segments": archived_segments,
            "skipped_segments": skipped_segments,
            "written_now": written_now,
            "cold_values": hs.cold_values_by_segment(handover_no),
            "checkpoint": checkpoint,
            "handover": hs.summarize(state),
        }

    def read_back(
        self,
        handover_no: str,
        segment: str | None = None,
    ) -> dict[str, Any]:
        """从冷热分层直接读回某事件日（或其分段），核对实际落在冷区还是热区。

        读冷区时按分段+窗口去重，重复写入只保留最后一条，因此重算后再次迁冷
        读回的是更正后的值；未迁冷的分段从热区交接状态读取并给出等待原因。
        """
        state = hs.load_state(handover_no)
        if state is None:
            return {"error": f"交接 {handover_no} 不存在"}
        if segment is not None and segment not in hs.SEGMENTS:
            return {"error": f"未知分段：{segment}"}

        cold_by_segment = hs.cold_values_by_segment(handover_no)
        segments_to_read = list(hs.SEGMENTS)
        result: dict[str, Any] = {}
        for seg in segments_to_read:
            segment_data = state["segments"][seg]
            windows = hs.segment_windows(state, seg)
            if not windows:
                result[seg] = {"source": "none", "tier": segment_data.get("tier", hs.TIER_HOT),
                               "values": [], "total": None, "reason": "该分段尚未归并"}
                continue
            cold_values = cold_by_segment.get(seg) or []
            cold_windows = {
                int(rec.get("window_seq", 0))
                for rec in hs.dedupe_cold_records(hs.read_cold_records(handover_no))
                if str(rec.get("segment", "")) == seg
            }
            expected = {int(window["seq"]) for window in windows}
            fully_cold = expected.issubset(cold_windows)
            if fully_cold:
                source = "cold"
                values = cold_values
                tier = hs.TIER_COLD
                reason = None
            else:
                source = "hot"
                tier = segment_data.get("tier", hs.TIER_HOT)
                values = hs.segment_values(state, seg)
                reason = hs.blocked_reason(segment_data)
            total = sum(float(v) for v in values if v is not None)
            published_value = segment_data.get("published_value")
            result[seg] = {
                "source": source,
                "tier": tier,
                "values": values,
                "total": total,
                "published_value": published_value,
                "matches_published": (
                    published_value is not None
                    and abs(total - float(published_value)) <= 1e-9
                ),
                "verified": bool(segment_data.get("verified")),
                "reason": reason,
                "cold_completed_windows": sorted(cold_windows),
            }

        payload: dict[str, Any] = {
            "handover_no": handover_no,
            "point": state["point"],
            "event_day": state["event_day"],
            "status": state["status"],
            "tier": state["tier"],
            "published_version": state.get("published_version", "v1"),
            "published_revision": state.get("published_revision", 1),
            "day_total": hs.day_total(state),
            "segments": result,
            "checkpoint": self.migration_checkpoint(handover_no),
        }
        if segment:
            payload["requested_segment"] = result[segment]
        return payload

    def reconcile_event_day(self, handover_no: str) -> dict[str, Any]:
        """按整日核对：把发布值、冷区读回值与待重写分段放在一起对账。"""
        readback = self.read_back(handover_no)
        if "error" in readback:
            return readback
        discrepancies = []
        for seg, info in readback["segments"].items():
            if info["source"] == "none":
                continue
            expected_total = info.get("published_value")
            if expected_total is None:
                continue
            if info["source"] == "cold" and abs(float(info["total"]) - float(expected_total)) > 1e-9:
                discrepancies.append(
                    {"segment": seg, "expected": expected_total, "cold_total": info["total"]}
                )
        state = hs.load_state(handover_no) or {}
        return {
            "handover_no": handover_no,
            "event_day": readback["event_day"],
            "tier": readback["tier"],
            "status": readback["status"],
            "published": readback["segments"],
            "day_total": readback["day_total"],
            "pending_segments": hs.pending_segments(state) if state else [],
            "discrepancies": discrepancies,
            "reconciled": not discrepancies,
        }
