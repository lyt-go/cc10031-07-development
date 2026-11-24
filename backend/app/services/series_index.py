"""序列索引领域服务：维护时序数据按时间分区的点区间索引与存储位置登记。

索引分区对应一个实际采样日（事件日）。除点区间与存储位置外，分区还持有该事件日
的交接状态：前段（首个归并窗口）与末段（其余窗口）各自是否已确认覆盖、是否因迟报
或更正待重写。状态落在 ``data/handover/<交接号>.json``，由本模块登记、冷热分层读取。
"""
from __future__ import annotations

import time
from typing import Any

from app.services import handover_state as hs
from app.services.base import BaseModuleService
from app.store import store


class SeriesIndexService(BaseModuleService):
    MODULE = "series_index"
    ENTITY = "索引分区"
    OPERATION_TERM = "存储"
    LIST_FIELDS = [
        "索引编号", "测点编号", "时间分区", "点区间", "索引粒度", "索引条数",
        "存储位置", "索引版本", "交接号", "交接状态", "存储层级",
    ]
    REQUIRED_FIELDS = ["索引编号", "测点编号", "时间分区"]
    STATUS_ORDER = ["草稿", "待构建", "已构建", "已失效"]
    ACTION_RULES = {"启动构建": "待构建", "确认构建": "已构建", "标记失效": "已失效"}
    # 数据存储操作：扫描归并窗口结果按时间分区构建序列索引分区写入索引元数据
    NEGATIVE_ACTIONS = ["标记失效"]

    def build_index(self, index_id: int) -> dict[str, Any]:
        """按时间分区扫描归并窗口结果数据构建序列索引分区并写入索引存储元数据文件。"""
        entry = store.find(self.MODULE, index_id)
        if entry is None:
            return {"error": f"索引分区 {index_id} 不存在"}
        self.run_action(index_id, "启动构建")
        point_key = entry["测点编号"]
        partition = entry.get("时间分区", time.strftime("%Y-%m-%d", time.localtime()))

        # 扫描归并窗口数据文件
        rollup_dir = self._data_dir("rollup_windows")
        indexed_points = []
        if rollup_dir.exists():
            for f in rollup_dir.glob("*_rollup.jsonl"):
                records = self._read_jsonl(f)
                # 数据产物输出：将索引扫描结果写入序列索引数据JSONL文件
                for r in records:
                    indexed_points.append({
                        "index_id": entry["索引编号"],
                        "point": r.get("point", point_key),
                        "time_partition": partition,
                        "window_index": r.get("window_start", 0),
                        "record_count": r.get("record_count", 0),
                        "rollup_value": r.get("rollup_value", 0),
                    })
        # 也扫描降采样数据
        ds_dir = self._data_dir("downsampled")
        if ds_dir.exists():
            for f in ds_dir.glob("*_downsampled.jsonl"):
                records = self._read_jsonl(f)
                for r in records:
                    indexed_points.append({
                        "index_id": entry["索引编号"],
                        "point": r.get("point", point_key),
                        "time_partition": partition,
                        "window_index": r.get("window_index", 0),
                        "aggregated_value": r.get("aggregated_value", 0),
                    })
        # 将索引扫描结果写入序列索引数据文件
        output_file = self._data_dir("series_index") / f"{entry['索引编号']}_index.jsonl"
        written = self._write_jsonl(output_file, indexed_points)
        entry["索引条数"] = str(written)
        entry["点区间"] = f"1-{written}"
        entry["存储位置"] = str(output_file)
        entry["索引版本"] = "v1.0"
        self.run_action(index_id, "确认构建")
        return {"index_id": entry["索引编号"], "point": point_key,
                "partition": partition, "indexed_count": written}

    def build_all_partitions(self, point_key: str) -> dict[str, Any]:
        """为指定测点构建所有时间分区的序列索引数据集。"""
        partitions = ["2026-09-01", "2026-09-02", "2026-09-03"]
        results = []
        for part in partitions:
            tid = max((int(r.get("id", 0)) for r in store.rows(self.MODULE)), default=0) + 1
            self._auto_create_seed(self.MODULE, {
                "索引编号": f"IDX-{point_key}-{part}",
                "测点编号": point_key,
                "时间分区": part,
                "点区间": "0-0",
                "索引粒度": "1h",
                "索引条数": "0",
                "存储位置": "",
                "索引版本": "v1.0",
            })
            r = self.build_index(tid)
            results.append(r)
        return {"point": point_key, "partitions_built": len(results), "results": results}

    # -- 事件日交接状态 --------------------------------------------------

    def _partition_row(self, index_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, index_id)

    def _state_for_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """取分区行对应的交接状态；行上已有交接号时严格按交接号读取。"""
        handover_no = row.get("交接号") or hs.make_handover_no(
            str(row["测点编号"]), str(row["时间分区"]))
        state = hs.load_state(handover_no)
        if state is None:
            state = hs.new_state(str(row["测点编号"]), str(row["时间分区"]), handover_no)
            hs.refresh_state(state)
            hs.save_state(state)
        return state

    def open_handover(
        self,
        index_id: int,
        *,
        previous_day_handover: str | None = None,
        sealed_by: str | None = None,
    ) -> dict[str, Any]:
        """登记事件日交接：把索引分区登记为待封存，并记下前一日交接号用于迟到核对。"""
        row = self._partition_row(index_id)
        if row is None:
            return {"error": f"索引分区 {index_id} 不存在"}
        state = self._state_for_row(row)
        state["previous_day"] = previous_day_handover or state.get("previous_day")
        if previous_day_handover:
            previous = hs.load_state(previous_day_handover)
            if previous is None:
                return {"error": f"前一日交接 {previous_day_handover} 不存在"}
            previous["next_day"] = state["handover_no"]
            hs.refresh_state(previous)
            hs.save_state(previous)
        if sealed_by:
            state["sealed_by"] = sealed_by
        hs.refresh_state(state)
        hs.save_state(state)
        hs.sync_partition_row(state)
        return {"handover": hs.summarize(state), "row": row}

    def record_rollup_window(
        self,
        index_id: int,
        *,
        window_seq: int,
        value: float,
        late_report: bool = False,
    ) -> dict[str, Any]:
        """登记一笔归并窗口结果。

        首个窗口归入前段（旧班交接读取的部分），其余窗口归入末段。封存后再到的是迟报：
        事件日整体不得迁冷，末段标记为「待重写」并保留待重算原因。
        """
        row = self._partition_row(index_id)
        if row is None:
            return {"error": f"索引分区 {index_id} 不存在"}
        if int(window_seq) < 1:
            return {"error": "窗口序号必须从 1 开始"}
        state = self._state_for_row(row)
        segment = hs.window_segment(int(window_seq))
        segment_data = state["segments"][segment]
        if segment_data.get("tier") == hs.TIER_COLD:
            return {"error": f"{segment}已迁入冷区，新写入须走更正或重算流程"}

        existing = next(
            (w for w in segment_data["windows"] if int(w.get("seq", 0)) == int(window_seq)),
            None,
        )
        if existing is not None and not late_report:
            return {"error": f"{segment}窗口 {window_seq} 已存在，请改用更正流程"}

        sealed = bool(state.get("sealed"))
        if existing is not None:
            existing["value"] = value
            existing["late_report"] = True
        else:
            segment_data["windows"].append(
                {"seq": int(window_seq), "value": value, "late_report": late_report}
            )
        segment_data["windows"].sort(key=lambda w: int(w.get("seq", 0)))

        if late_report and sealed:
            # 封存后到的迟报：保持原发布值不动，末段标记「待重写」，整日不得迁冷。
            segment_data["state"] = hs.SEG_LATE
            segment_data["reason"] = "封存后收到迟报，末段待重写"
            segment_data["archivable"] = False
        elif sealed:
            return {"error": f"事件日已封存，{segment}新窗口须走迟报登记"}
        else:
            segment_data["state"] = hs.SEG_RECORDED
            segment_data["published_value"] = sum(
                float(v) for v in hs.segment_values(state, segment))
            segment_data["reason"] = None

        hs.refresh_state(state)
        hs.save_state(state)
        hs.sync_partition_row(state)
        return {
            "handover_no": state["handover_no"],
            "segment": segment,
            "window_seq": int(window_seq),
            "late_report": bool(late_report),
            "handover": hs.summarize(state),
            "row": row,
        }

    def confirm_next_day_window(
        self,
        index_id: int,
        *,
        next_day_index_id: int,
        next_day_window_seq: int = 1,
        sealed_by: str | None = None,
    ) -> dict[str, Any]:
        """新日首个归并窗口确认：旧日到此封存，交接状态固定为已确认覆盖 + 待重写末段。"""
        row = self._partition_row(index_id)
        if row is None:
            return {"error": f"索引分区 {index_id} 不存在"}
        next_row = self._partition_row(next_day_index_id)
        if next_row is None:
            return {"error": f"新日索引分区 {next_day_index_id} 不存在"}
        state = self._state_for_row(row)
        next_state = self._state_for_row(next_row)
        if str(next_state["event_day"]) <= str(state["event_day"]):
            return {"error": "新日分区的事件日必须晚于当前事件日"}

        state["sealed"] = True
        state["sealed_by"] = sealed_by or state.get("sealed_by") or "新日首窗确认"
        state["next_day"] = next_state["handover_no"]
        next_state["previous_day"] = state["handover_no"]

        # 新日首个归并窗口到来说明旧日不会再按日推进：旧日已归并的分段都转为
        # 「已确认覆盖」。封存后再到的迟报会单独把末段翻回「待重写」。
        for seg in hs.SEGMENTS:
            segment_data = state["segments"][seg]
            if segment_data.get("windows") and segment_data.get("state") == hs.SEG_RECORDED:
                segment_data["state"] = hs.SEG_COVERED
                segment_data["reason"] = None

        for part in (state, next_state):
            hs.refresh_state(part)
            hs.save_state(part)
        hs.sync_partition_row(state)
        hs.sync_partition_row(next_state)
        return {
            "handover": hs.summarize(state),
            "next_day_handover": hs.summarize(next_state),
            "row": row,
        }

    def verify_segment(
        self,
        index_id: int,
        segment: str,
        *,
        verified_by: str | None = None,
    ) -> dict[str, Any]:
        """按前段或末段做独立核验；只有被核验的分段才允许作为交接依据。"""
        row = self._partition_row(index_id)
        if row is None:
            return {"error": f"索引分区 {index_id} 不存在"}
        if segment not in hs.SEGMENTS:
            return {"error": f"未知分段：{segment}"}
        state = self._state_for_row(row)
        segment_data = state["segments"][segment]
        if not segment_data.get("windows"):
            return {"error": f"{segment}尚无归并窗口，无法核验"}
        segment_data["verified"] = True
        segment_data["reason"] = None
        if segment_data["state"] not in (hs.SEG_LATE, hs.SEG_CORRECTED):
            segment_data["state"] = hs.SEG_COVERED
        if verified_by:
            state["verified_by"] = verified_by
        hs.refresh_state(state)
        hs.save_state(state)
        hs.sync_partition_row(state)
        return {"handover": hs.summarize(state), "row": row}

    def record_correction(
        self,
        index_id: int,
        segment: str,
        *,
        corrected_value: float,
        window_seq: int | None = None,
    ) -> dict[str, Any]:
        """末段收到更正：登记更正后的值，但在重算完成前仍标记待重算、不得迁冷。"""
        row = self._partition_row(index_id)
        if row is None:
            return {"error": f"索引分区 {index_id} 不存在"}
        if segment not in hs.SEGMENTS:
            return {"error": f"未知分段：{segment}"}
        state = self._state_for_row(row)
        segment_data = state["segments"][segment]
        windows = hs.segment_windows(state, segment)
        if not windows:
            return {"error": f"{segment}尚无归并窗口，无法更正"}
        target_seq = int(window_seq) if window_seq is not None else int(windows[-1]["seq"])
        target = next((w for w in segment_data["windows"] if int(w["seq"]) == target_seq), None)
        if target is None:
            return {"error": f"{segment}不存在窗口 {target_seq}"}

        segment_data["corrected_value"] = corrected_value
        segment_data["state"] = hs.SEG_CORRECTED
        segment_data["reason"] = "收到更正但尚未完成重算，末段仍留热区"
        segment_data["archivable"] = False
        # 更正意图写入更正记录，重算完成后才回写窗口值。
        segment_data.setdefault("corrections", []).append(
            {"window_seq": target_seq, "value": corrected_value, "at": time.time(),
             "recomputed": False}
        )
        hs.refresh_state(state)
        hs.save_state(state)
        hs.sync_partition_row(state)
        return {"handover": hs.summarize(state), "row": row,
                "awaiting_recompute": True, "window_seq": target_seq}

    def recompute_segment(
        self,
        index_id: int,
        segment: str,
        *,
        approved: bool = False,
    ) -> dict[str, Any]:
        """完成更正后的末段重算；获准后才转为已确认覆盖，可再次迁冷。"""
        row = self._partition_row(index_id)
        if row is None:
            return {"error": f"索引分区 {index_id} 不存在"}
        if segment not in hs.SEGMENTS:
            return {"error": f"未知分段：{segment}"}
        state = self._state_for_row(row)
        segment_data = state["segments"][segment]
        if segment_data.get("state") != hs.SEG_CORRECTED:
            return {"error": f"{segment}不在待重算状态"}
        if not approved:
            return {"error": "更正后的重算结果未获准，不能转已确认覆盖"}

        applied = []
        for correction in segment_data.get("corrections", []):
            if correction.get("recomputed"):
                continue
            window = next(
                (w for w in segment_data["windows"]
                 if int(w["seq"]) == int(correction["window_seq"])),
                None,
            )
            if window is None:
                continue
            window["value"] = correction["value"]
            correction["recomputed"] = True
            applied.append(int(window["seq"]))

        segment_data["state"] = hs.SEG_COVERED
        segment_data["verified"] = True
        segment_data["reason"] = None
        segment_data["archivable"] = True
        segment_data["published_value"] = sum(
            float(v) for v in hs.segment_values(state, segment)
        )
        segment_data["recomputed_at"] = time.time()
        state["published_revision"] = int(state.get("published_revision", 1)) + 1
        state["published_version"] = f"v{state['published_revision']}"
        hs.refresh_state(state)
        hs.save_state(state)
        hs.sync_partition_row(state)
        return {"handover": hs.summarize(state), "row": row,
                "recomputed_windows": applied}

    def get_handover(self, handover_no: str) -> dict[str, Any]:
        state = hs.load_state(handover_no)
        if state is None:
            return {"error": f"交接 {handover_no} 不存在"}
        return {"handover": hs.summarize(state)}

    def list_handovers(
        self,
        *,
        point_key: str | None = None,
        event_day: str | None = None,
    ) -> dict[str, Any]:
        rows = []
        for path in sorted(hs.handover_dir().glob("*.json")):
            state = hs.load_state(path.stem)
            if state is None:
                continue
            if point_key and state["point"] != point_key:
                continue
            if event_day and state["event_day"] != event_day:
                continue
            rows.append(hs.summarize(state))
        return {"items": rows, "total": len(rows)}