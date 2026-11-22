"""增量缺口重放清单领域服务。

增量追踪出现缺口时，前班值班员把增量游标和缺口日志导出为「增量缺口重放清单」
文件（manifest），接班人导入后按来源端点 + 原游标 + 已验证覆盖依据认领补采。

核心领域规则：
1. 每段重放范围绑定来源端点、原游标和已验证的覆盖依据；范围相近但来源不同的
   两段永远不合并。
2. 导入清单时若同一缺口已有两个观察范围（已验证覆盖依据），被覆盖的部分不再
   派发补采，待重放区间按实际覆盖缩减。
3. 导入中断后续做，以实际补齐回执为准缩减待重放区间；同一文件重复提交只保留
   一份可认领工作（幂等），已确认范围仍可复核。
4. 重放队列用实际补齐回执确认哪些数据还需领取，重放结果同步回导入清单。

区间统一采用整数闭区间 [cursor_from, cursor_to]，两端游标均包含在内，
因此 20-40 的缺口被 20-28 的覆盖依据确认后，待重放区间从 29 开始。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

TABLE_HANDOFF = "incr_gap_handoff"
TABLE_SEGMENT = "incr_gap_segment"
TABLE_SUBMISSION = "incr_gap_submission"

SEGMENT_PENDING = "待重放"
SEGMENT_COMPLETED = "已补齐"
SEGMENT_REVIEWED = "已复核"

HANDOFF_DRAFT = "待认领"
HANDOFF_PARTIAL = "部分完成"
HANDOFF_DONE = "已完成"
HANDOFF_REVIEWED = "已复核"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(r.get("id", 0)) for r in rows), default=0) + 1


def canonical_manifest(manifest: dict[str, Any]) -> str:
    """清单文件的规范化序列化：不含导入侧生成的 file_hash，保证导出/导入一致。"""
    payload = {k: v for k, v in manifest.items() if k != "file_hash"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_manifest(manifest).encode("utf-8")).hexdigest()[:16]


def _subtract_interval(
    intervals: list[list[int]],
    covered_from: int,
    covered_to: int,
) -> list[list[int]]:
    """从一组整数闭区间中裁掉 [covered_from, covered_to]，返回剩余闭区间。

    裁剪只发生在同一来源端点 + 同一原游标的段内部，因此这里不做跨段合并；
    同一组区间本身互不相交，相邻区间也保持独立，避免来源不同的范围被拼接。
    """
    remaining: list[list[int]] = []
    for start, end in intervals:
        if covered_to < start or covered_from > end:
            remaining.append([start, end])
            continue
        if covered_from > start:
            remaining.append([start, min(covered_from - 1, end)])
        if covered_to < end:
            remaining.append([max(covered_to + 1, start), end])
    return remaining


def _interval_size(start: int, end: int) -> int:
    return end - start + 1


def _format_interval(start: int, end: int, endpoint: str) -> str:
    return f"[{start},{end}]@{endpoint}"


class GapHandoffService(BaseModuleService):
    """增量缺口重放清单：导出/导入/回执/同步的全链路领域服务。"""

    MODULE = "incr_gap_handoff"
    ENTITY = "增量缺口重放清单"
    OPERATION_TERM = "交接"
    LIST_FIELDS = [
        "清单编号", "来源端点", "原游标", "缺口区间", "缺口条数",
        "待重放区间", "待重放条数", "已补齐条数", "值班班组", "导出时间",
    ]
    REQUIRED_FIELDS = ["清单编号", "来源端点", "原游标"]
    STATUS_ORDER = [HANDOFF_DRAFT, "补采中", HANDOFF_PARTIAL, HANDOFF_DONE, HANDOFF_REVIEWED]
    ACTION_RULES = {"认领清单": "补采中", "同步回执": HANDOFF_PARTIAL, "确认复核": HANDOFF_REVIEWED}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------ 导出

    def export_manifest(
        self,
        *,
        tracker_id: int | None = None,
        endpoint: str | None = None,
        origin_cursor: str | None = None,
        gaps: list[dict[str, Any]] | None = None,
        operator: str = "前班值班员",
        shift_team: str = "采集值班A组",
        observations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """把增量游标和缺口日志导出为增量缺口重放清单文件。"""
        tracker: dict[str, Any] | None = None
        if tracker_id is not None:
            tracker = store.find("incr_tracker", tracker_id)
        if tracker is not None:
            endpoint = endpoint or tracker.get("目标端点", "")
            origin_cursor = origin_cursor or tracker.get("当前游标", "")
            tracker_no = tracker.get("标记编号", "")
        else:
            origin_cursor = origin_cursor or "0"
            tracker_no = ""

        gap_rows = [dict(g) for g in (gaps or [])]
        for gap in gap_rows:
            gap.setdefault("endpoint", endpoint)
            gap.setdefault("origin_cursor", origin_cursor)

        handoff_rows = store.rows(TABLE_HANDOFF)
        seq = _next_id(handoff_rows)
        handoff_no = f"GAP-EXPORT-{str(seq).zfill(4)}"
        exported_at = _now()

        manifest = {
            "manifest_type": "incremental_gap_replay_manifest",
            "version": 1,
            "handoff_no": handoff_no,
            "tracker_no": tracker_no,
            "source_endpoint": endpoint or "",
            "origin_cursor": str(origin_cursor),
            "exported_by": operator,
            "shift_team": shift_team,
            "exported_at": exported_at,
            "gaps": [
                {
                    "gap_no": g.get("gap_no", f"GAP-{str(i + 1).zfill(2)}"),
                    "source_endpoint": g["source_endpoint"],
                    "origin_cursor": str(g["origin_cursor"]),
                    "cursor_from": int(g["cursor_from"]),
                    "cursor_to": int(g["cursor_to"]),
                    "gap_log": g.get("gap_log", ""),
                    "coverage_basis": g.get("coverage_basis", "缺口日志待补采"),
                }
                for i, g in enumerate(gap_rows)
            ],
            # 已验证的覆盖依据（观察范围）：导入时据此缩减待重放区间。
            "observations": [
                {
                    "source_endpoint": str(o["source_endpoint"]),
                    "origin_cursor": str(o["origin_cursor"]),
                    "cursor_from": int(o["cursor_from"]),
                    "cursor_to": int(o["cursor_to"]),
                    "basis": str(o.get("basis", "已验证观察范围")),
                    "verified_by": str(o.get("verified_by", operator)),
                }
                for o in (observations or [])
            ],
        }
        manifest["file_hash"] = manifest_hash(manifest)
        return manifest

    # ------------------------------------------------------------------ 导入

    def import_manifest(
        self,
        manifest: dict[str, Any],
        *,
        observations: list[dict[str, Any]] | None = None,
        operator: str = "接班值班员",
        shift_team: str = "采集值班B组",
    ) -> tuple[dict[str, Any] | None, str]:
        """导入清单文件并生成可认领的重放交接记录（同一文件幂等）。

        observations 为导入时刻已存在的观察范围（同一缺口的第二个观察范围），
        与清单内 observations 合并后作为已验证覆盖依据缩减待重放区间。
        """
        if manifest.get("manifest_type") != "incremental_gap_replay_manifest":
            return None, "不是增量缺口重放清单文件，无法导入"
        if not manifest.get("gaps"):
            return None, "清单中没有缺口记录，无需交接"

        file_hash = manifest.get("file_hash") or manifest_hash(manifest)
        handoff_rows = store.rows(TABLE_HANDOFF)
        existing = next((h for h in handoff_rows if h.get("file_hash") == file_hash), None)

        all_observations = list(manifest.get("observations", [])) + list(observations or [])

        if existing is not None:
            # 连续提交同一文件：不重复建工作，只追加一次提交记录，已确认范围仍可复核。
            self._append_submission(existing["id"], file_hash, operator, "重复提交(幂等)")
            return existing, f"清单{existing['清单编号']}已存在，重复提交未生成新工作，已确认范围仍可复核"

        handoff_id = _next_id(handoff_rows)
        handoff_no = manifest.get("handoff_no") or f"GAP-IMPORT-{str(handoff_id).zfill(4)}"
        endpoint = manifest.get("source_endpoint", "")
        origin_cursor = str(manifest.get("origin_cursor", ""))

        handoff = {
            "id": handoff_id,
            "清单编号": handoff_no,
            "来源端点": endpoint,
            "原游标": origin_cursor,
            "缺口区间": "",
            "缺口条数": 0,
            "待重放区间": "",
            "待重放条数": 0,
            "已补齐条数": 0,
            "值班班组": shift_team,
            "导出时间": manifest.get("exported_at", _now()),
            "status": HANDOFF_DRAFT,
            "pending": True,
            "abnormal": False,
            "file_hash": file_hash,
            "导出班组": manifest.get("shift_team", ""),
            "导出人": manifest.get("exported_by", ""),
            "导入人": operator,
            "导入时间": _now(),
        }
        handoff_rows.append(handoff)

        segment_rows = store.rows(TABLE_SEGMENT)
        total_gap = 0
        pending_total = 0
        covered_total = 0
        gap_texts: list[str] = []
        pending_texts: list[str] = []

        for gap in manifest["gaps"]:
            gap_endpoint = gap.get("source_endpoint", endpoint)
            gap_cursor = str(gap.get("origin_cursor", origin_cursor))
            g_from, g_to = int(gap["cursor_from"]), int(gap["cursor_to"])
            total_gap += _interval_size(g_from, g_to)
            gap_texts.append(_format_interval(g_from, g_to, gap_endpoint))

            # 只接受同来源端点、同原游标的已验证覆盖依据；来源不同的观察范围不参与裁剪。
            pending_intervals = [[g_from, g_to]]
            applied_basis: list[dict[str, Any]] = []
            for obs in all_observations:
                if str(obs.get("source_endpoint")) != gap_endpoint:
                    continue
                if str(obs.get("origin_cursor")) != gap_cursor:
                    continue
                before = [list(iv) for iv in pending_intervals]
                pending_intervals = _subtract_interval(
                    pending_intervals, int(obs["cursor_from"]), int(obs["cursor_to"]))
                if pending_intervals != before:
                    applied_basis.append(dict(obs))

            # 已被覆盖依据确认的区间：落「已补齐」段，保留重放前的不派发说明供复核。
            covered_intervals = [[g_from, g_to]]
            for p_from, p_to in pending_intervals:
                covered_intervals = _subtract_interval(covered_intervals, p_from, p_to)
            for c_from, c_to in covered_intervals:
                covered_total += _interval_size(c_from, c_to)
                segment_rows.append(self._build_covered_segment(
                    handoff_id, handoff_no, gap, gap_endpoint, gap_cursor,
                    c_from, c_to, applied_basis))

            for p_from, p_to in pending_intervals:
                pending_total += _interval_size(p_from, p_to)
                pending_texts.append(_format_interval(p_from, p_to, gap_endpoint))
                segment_rows.append(self._build_pending_segment(
                    handoff_id, handoff_no, gap, gap_endpoint, gap_cursor,
                    p_from, p_to, applied_basis))

        handoff["缺口区间"] = "；".join(gap_texts)
        handoff["缺口条数"] = total_gap
        handoff["待重放区间"] = "；".join(pending_texts) if pending_texts else "无（全部由覆盖依据确认）"
        handoff["待重放条数"] = pending_total
        handoff["已补齐条数"] = covered_total
        self._append_submission(handoff_id, file_hash, operator, "首次导入")
        self._refresh_handoff_status(handoff)
        return handoff, (
            f"清单{handoff_no}导入完成：原始缺口{total_gap}条，"
            f"已验证覆盖{covered_total}条，待重放{pending_total}条"
        )

    def _build_pending_segment(
        self, handoff_id: int, handoff_no: str, gap: dict[str, Any],
        endpoint: str, origin_cursor: str, seg_from: int, seg_to: int,
        applied_basis: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "id": _next_id(store.rows(TABLE_SEGMENT)),
            "handoff_id": handoff_id,
            "段编号": f"{handoff_no}-S{seg_from}-{seg_to}",
            "来源端点": endpoint,
            "原游标": origin_cursor,
            "起始游标": seg_from,
            "结束游标": seg_to,
            "条数": _interval_size(seg_from, seg_to),
            "覆盖依据": "；".join(
                f"{b.get('basis', '观察范围')}[{b['cursor_from']},{b['cursor_to']}]"
                for b in applied_basis
            ) or gap.get("coverage_basis", "缺口日志待补采"),
            "缺口日志": gap.get("gap_log", ""),
            "status": SEGMENT_PENDING,
            "pending": True,
            "abnormal": False,
            "retry_task_id": None,
            "回执记录": [],
            "不派发说明": "",
        }

    def _build_covered_segment(
        self, handoff_id: int, handoff_no: str, gap: dict[str, Any],
        endpoint: str, origin_cursor: str, seg_from: int, seg_to: int,
        applied_basis: list[dict[str, Any]],
    ) -> dict[str, Any]:
        basis_text = "；".join(
            f"{b.get('basis', '观察范围')}[{b['cursor_from']},{b['cursor_to']}]"
            for b in applied_basis
        ) or gap.get("coverage_basis", "")
        return {
            "id": _next_id(store.rows(TABLE_SEGMENT)),
            "handoff_id": handoff_id,
            "段编号": f"{handoff_no}-C{seg_from}-{seg_to}",
            "来源端点": endpoint,
            "原游标": origin_cursor,
            "起始游标": seg_from,
            "结束游标": seg_to,
            "条数": _interval_size(seg_from, seg_to),
            "覆盖依据": basis_text,
            "缺口日志": gap.get("gap_log", ""),
            "status": SEGMENT_COMPLETED,
            "pending": False,
            "abnormal": False,
            "retry_task_id": None,
            "回执记录": [{
                "cursor_from": seg_from, "cursor_to": seg_to,
                "basis": basis_text, "recorded_at": _now(),
                "source": "导入时覆盖依据确认",
            }],
            "不派发说明": (
                f"导入时同一缺口已有已验证观察范围覆盖[{seg_from},{seg_to}]，"
                f"依据：{basis_text}，重放前即确认无需派发补采"
            ),
        }

    # ------------------------------------------------------------------ 回执

    def submit_receipt(
        self,
        handoff_id: int,
        receipts: list[dict[str, Any]],
        *,
        operator: str = "接班值班员",
    ) -> tuple[dict[str, Any] | None, str]:
        """按实际补齐回执缩减待重放区间（导入中断后续做的核心入口）。"""
        handoff = store.find(TABLE_HANDOFF, handoff_id)
        if handoff is None:
            return None, f"交接记录 {handoff_id} 不存在"

        segments = [s for s in store.rows(TABLE_SEGMENT) if s["handoff_id"] == handoff_id]
        applied = 0
        rejected: list[str] = []
        for receipt in receipts:
            r_endpoint = str(receipt.get("source_endpoint", handoff["来源端点"]))
            r_cursor = str(receipt.get("origin_cursor", handoff["原游标"]))
            r_from, r_to = int(receipt["cursor_from"]), int(receipt["cursor_to"])
            # 重放范围绑定来源端点+原游标：来源不同或游标不同的回执不能缩减本段。
            targets = [
                s for s in segments
                if s["status"] == SEGMENT_PENDING
                and s["来源端点"] == r_endpoint
                and s["原游标"] == r_cursor
                and not (r_to < s["起始游标"] or r_from > s["结束游标"])
            ]
            if not targets:
                rejected.append(f"[{r_from},{r_to}]@{r_endpoint}无匹配的待重放段（来源或原游标不一致）")
                continue

            for segment in targets:
                seg_from, seg_to = segment["起始游标"], segment["结束游标"]
                remaining = _subtract_interval([[seg_from, seg_to]], r_from, r_to)
                self._record_receipt(segment, r_from, r_to, receipt, operator)
                if not remaining:
                    self._complete_segment(segment, receipt)
                    applied += 1
                    continue
                # 段被部分补齐：回执与本段的交集落一条已补齐段，剩余区间保留为待重放。
                cut_from = max(seg_from, r_from)
                cut_to = min(seg_to, r_to)
                segment_rows = store.rows(TABLE_SEGMENT)
                segment_rows.append(self._spawn_completed_piece(
                    handoff_id, handoff["清单编号"], segment,
                    cut_from, cut_to, receipt, operator))
                # 原段收缩为第一个剩余区间；若回执落在段中部，第二个剩余区间新建待重放段。
                first = remaining[0]
                segment["起始游标"], segment["结束游标"] = first
                segment["条数"] = _interval_size(first[0], first[1])
                segment["段编号"] = f"{handoff['清单编号']}-S{first[0]}-{first[1]}"
                for extra_from, extra_to in remaining[1:]:
                    segment_rows.append(self._spawn_pending_piece(
                        handoff_id, handoff["清单编号"], segment, extra_from, extra_to))
                applied += 1

        self._refresh_handoff_status(handoff)
        msg = f"已按{applied}份实际补齐回执缩减待重放区间"
        if rejected:
            msg += f"；{len(rejected)}份回执因来源/原游标不匹配被拒绝"
        return handoff, msg

    def _record_receipt(self, segment: dict, r_from: int, r_to: int,
                        receipt: dict, operator: str) -> None:
        segment.setdefault("回执记录", []).append({
            "cursor_from": r_from,
            "cursor_to": r_to,
            "basis": receipt.get("basis", "实际补齐回执"),
            "recorded_at": _now(),
            "recorded_by": operator,
            "source": receipt.get("source", "重放补齐回执"),
        })
        # 若该段已挂钩重放队列任务，回执即实际补齐依据，任务随之成功。
        task_id = segment.get("retry_task_id")
        if task_id is not None:
            task = store.find("retry_queue", task_id)
            if task is not None and task.get("status") != "已成功":
                task["status"] = "已成功"
                task["pending"] = False
                task["队列状态"] = f"receipt_confirmed:[{r_from},{r_to}]"
                task["最后重试"] = _now()

    def _complete_segment(self, segment: dict, receipt: dict) -> None:
        segment["status"] = SEGMENT_COMPLETED
        segment["pending"] = False
        segment["不派发说明"] = (
            f"[{segment['起始游标']},{segment['结束游标']}]已由实际补齐回执确认"
            f"（{receipt.get('basis', '重放补齐回执')}），不再派发"
        )

    def _spawn_completed_piece(self, handoff_id: int, handoff_no: str,
                               base_segment: dict, cut_from: int, cut_to: int,
                               receipt: dict, operator: str) -> dict:
        return {
            "id": _next_id(store.rows(TABLE_SEGMENT)),
            "handoff_id": handoff_id,
            "段编号": f"{handoff_no}-C{cut_from}-{cut_to}",
            "来源端点": base_segment["来源端点"],
            "原游标": base_segment["原游标"],
            "起始游标": cut_from,
            "结束游标": cut_to,
            "条数": _interval_size(cut_from, cut_to),
            "覆盖依据": base_segment.get("覆盖依据", ""),
            "缺口日志": base_segment.get("缺口日志", ""),
            "status": SEGMENT_COMPLETED,
            "pending": False,
            "abnormal": False,
            "retry_task_id": None,
            "回执记录": [{
                "cursor_from": cut_from, "cursor_to": cut_to,
                "basis": receipt.get("basis", "实际补齐回执"),
                "recorded_at": _now(), "recorded_by": operator,
                "source": receipt.get("source", "重放补齐回执"),
            }],
            "不派发说明": f"[{cut_from},{cut_to}]已由实际补齐回执确认，不再派发",
        }

    def _spawn_pending_piece(self, handoff_id: int, handoff_no: str,
                             base_segment: dict, seg_from: int, seg_to: int) -> dict:
        return {
            "id": _next_id(store.rows(TABLE_SEGMENT)),
            "handoff_id": handoff_id,
            "段编号": f"{handoff_no}-S{seg_from}-{seg_to}",
            "来源端点": base_segment["来源端点"],
            "原游标": base_segment["原游标"],
            "起始游标": seg_from,
            "结束游标": seg_to,
            "条数": _interval_size(seg_from, seg_to),
            "覆盖依据": base_segment.get("覆盖依据", ""),
            "缺口日志": base_segment.get("缺口日志", ""),
            "status": SEGMENT_PENDING,
            "pending": True,
            "abnormal": False,
            "retry_task_id": base_segment.get("retry_task_id"),
            "回执记录": [],
            "不派发说明": "",
        }

    # ------------------------------------------------------------------ 认领/同步

    def claim_handoff(self, handoff_id: int, *, operator: str = "接班值班员") -> tuple[dict | None, str]:
        """接班人认领：为每个仍待重放的段在重放队列建立挂钩任务。"""
        handoff = store.find(TABLE_HANDOFF, handoff_id)
        if handoff is None:
            return None, f"交接记录 {handoff_id} 不存在"
        retry_rows = store.rows("retry_queue")
        linked = 0
        for segment in self._pending_segments(handoff_id):
            if segment.get("retry_task_id") is not None:
                continue
            task = {
                "id": _next_id(retry_rows),
                "任务编号": f"RETR-GAP-{handoff['清单编号'].split('-')[-1]}-{segment['id']}",
                "失败端点": segment["来源端点"],
                "失败原因": (
                    f"增量缺口重放:游标[{segment['起始游标']},{segment['结束游标']}]"
                    f"/原游标={segment['原游标']}"
                ),
                "重试次数": 0,
                "最大重试": 5,
                "退避策略": "exponential:5s/baseline",
                "最后重试": "",
                "队列状态": f"gap_handoff:{handoff['清单编号']}",
                "status": "待重试",
                "pending": True,
                "abnormal": True,
            }
            retry_rows.append(task)
            segment["retry_task_id"] = task["id"]
            linked += 1
        handoff["status"] = "补采中"
        handoff["pending"] = True
        return handoff, f"已认领清单{handoff['清单编号']}，{linked}个待重放段挂钩重放队列"

    def sync_replay_queue(self, handoff_id: int) -> tuple[dict | None, str]:
        """用重放队列的实际补齐回执确认哪些数据还需领取，结果同步回导入清单。"""
        handoff = store.find(TABLE_HANDOFF, handoff_id)
        if handoff is None:
            return None, f"交接记录 {handoff_id} 不存在"
        synced = 0
        for segment in self._pending_segments(handoff_id):
            task_id = segment.get("retry_task_id")
            if task_id is None:
                continue
            task = store.find("retry_queue", task_id)
            if task is not None and task.get("status") == "已成功":
                self._record_receipt(
                    segment, segment["起始游标"], segment["结束游标"],
                    {"basis": "重放队列已成功回执", "source": "retry_queue_sync"}, "系统同步")
                self._complete_segment(
                    segment, {"basis": "重放队列已成功回执"})
                synced += 1
        self._refresh_handoff_status(handoff)
        return handoff, f"重放队列已同步：{synced}个段凭实际回执确认补齐"

    def review_handoff(self, handoff_id: int) -> tuple[dict | None, str]:
        """追踪员复核：已确认范围仍可复核，复核后整单归档。"""
        handoff = store.find(TABLE_HANDOFF, handoff_id)
        if handoff is None:
            return None, f"交接记录 {handoff_id} 不存在"
        pending = self._pending_segments(handoff_id)
        if pending:
            return None, f"仍有{len(pending)}个待重放段，不能复核归档"
        for segment in store.rows(TABLE_SEGMENT):
            if segment["handoff_id"] == handoff_id and segment["status"] == SEGMENT_COMPLETED:
                segment["status"] = SEGMENT_REVIEWED
        handoff["status"] = HANDOFF_REVIEWED
        handoff["pending"] = False
        return handoff, f"清单{handoff['清单编号']}复核通过，原始导出范围与补齐记录均可追溯"

    # ------------------------------------------------------------------ 查询

    def list_handoffs(self) -> list[dict[str, Any]]:
        return list(store.rows(TABLE_HANDOFF))

    def get_handoff_detail(self, handoff_id: int) -> dict[str, Any] | None:
        handoff = store.find(TABLE_HANDOFF, handoff_id)
        if handoff is None:
            return None
        segments = [s for s in store.rows(TABLE_SEGMENT) if s["handoff_id"] == handoff_id]
        submissions = [s for s in store.rows(TABLE_SUBMISSION) if s["handoff_id"] == handoff_id]
        return {
            "handoff": handoff,
            "segments": segments,
            "submissions": submissions,
            "dispatch_notes": self._dispatch_notes(segments),
            "original_gap": handoff.get("缺口区间", ""),
            "current_pending": handoff.get("待重放区间", ""),
        }

    def _dispatch_notes(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """重放前后的记录都能说明某段为什么不再派发。"""
        notes = []
        for segment in segments:
            if segment["status"] == SEGMENT_PENDING:
                continue
            notes.append({
                "段编号": segment["段编号"],
                "来源端点": segment["来源端点"],
                "原游标": segment["原游标"],
                "区间": f"[{segment['起始游标']},{segment['结束游标']}]",
                "状态": segment["status"],
                "不派发说明": segment.get("不派发说明", ""),
                "覆盖依据": segment.get("覆盖依据", ""),
                "回执记录": segment.get("回执记录", []),
            })
        return notes

    # ------------------------------------------------------------------ 辅助

    def run_action(self, entry_id: int, action: str):
        """通用工作台动作入口，转接到专用领域动作。"""
        if action == "认领清单":
            entry, message = self.claim_handoff(entry_id)
        elif action == "同步回执":
            entry, message = self.sync_replay_queue(entry_id)
        elif action == "确认复核":
            entry, message = self.review_handoff(entry_id)
        else:
            return super().run_action(entry_id, action)
        if entry is None:
            return None, message
        return entry, message

    def _pending_segments(self, handoff_id: int) -> list[dict[str, Any]]:
        return [
            s for s in store.rows(TABLE_SEGMENT)
            if s["handoff_id"] == handoff_id and s["status"] == SEGMENT_PENDING
        ]

    def _refresh_handoff_status(self, handoff: dict[str, Any]) -> None:
        segments = [s for s in store.rows(TABLE_SEGMENT) if s["handoff_id"] == handoff["id"]]
        pending = [s for s in segments if s["status"] == SEGMENT_PENDING]
        completed = [s for s in segments if s["status"] in (SEGMENT_COMPLETED, SEGMENT_REVIEWED)]
        handoff["待重放条数"] = sum(s["条数"] for s in pending)
        handoff["已补齐条数"] = sum(s["条数"] for s in completed)
        handoff["待重放区间"] = (
            "；".join(
                _format_interval(s["起始游标"], s["结束游标"], s["来源端点"]) for s in pending
            ) or "无（全部由覆盖依据/补齐回执确认）"
        )
        if not pending:
            handoff["status"] = HANDOFF_DONE
            handoff["pending"] = False
        elif completed:
            handoff["status"] = HANDOFF_PARTIAL
            handoff["pending"] = True
        else:
            handoff["status"] = HANDOFF_DRAFT
            handoff["pending"] = True

    def _append_submission(self, handoff_id: int, file_hash: str,
                           operator: str, result: str) -> None:
        store.rows(TABLE_SUBMISSION).append({
            "id": _next_id(store.rows(TABLE_SUBMISSION)),
            "handoff_id": handoff_id,
            "file_hash": file_hash,
            "提交人": operator,
            "提交时间": _now(),
            "处理结果": result,
        })
