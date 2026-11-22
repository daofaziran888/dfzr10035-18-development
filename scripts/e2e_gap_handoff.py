"""端到端验收：增量缺口重放清单的导出/导入/回执/同步/复核全链路。

区间采用整数闭区间 [from,to]（两端游标均包含）。
运行（在 backend 目录下）：
  .venv/bin/python -I /workspace/scripts/e2e_gap_handoff.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/workspace/backend")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def post(path: str, body: dict | None = None) -> dict:
    resp = client.post(path, json=body or {})
    assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text}"
    return resp.json()


def get(path: str) -> dict:
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text}"
    return resp.json()


print("== 场景0：健康检查与模块注册 ==")
health = get("/api/health")
check("服务健康", health["ok"] is True, str(health))
page = get("/api/incr_gap_handoff")
check("通用资源路由可访问新模块", page["total"] == 0, str(page))

print("== 场景1：前班导出 20-40 的增量缺口重放清单（游标+缺口日志） ==")
export = post("/api/incr-gap/export", {
    "endpoint": "EP-ORDER-SYNC",
    "origin_cursor": "18",
    "gaps": [{
        "gap_no": "GAP-01",
        "source_endpoint": "EP-ORDER-SYNC",
        "origin_cursor": "18",
        "cursor_from": 20,
        "cursor_to": 40,
        "gap_log": "增量追踪缺口:游标20-40缺失",
        "coverage_basis": "缺口日志待补采",
    }],
    "operator": "前班值班员张三",
    "shift_team": "采集值班A组",
})
check("导出成功", export["ok"] is True, export["message"])
manifest = export["manifest"]
check("清单绑定来源端点", manifest["source_endpoint"] == "EP-ORDER-SYNC")
check("清单绑定原游标", manifest["origin_cursor"] == "18")
check("清单包含缺口日志", manifest["gaps"][0]["gap_log"].startswith("增量追踪缺口"))
check("清单含文件指纹", len(manifest["file_hash"]) == 16)

# 单独验证与增量追踪标记的联动：tracker_id 导出时自动带入标记编号与游标
linked = post("/api/incr-gap/export", {
    "tracker_id": 2,
    "gaps": [{"cursor_from": 1, "cursor_to": 2, "gap_log": "联动验证"}],
})
check("tracker_id导出自动带入标记编号", linked["manifest"]["tracker_no"] == "INCR-0002",
      linked["manifest"]["tracker_no"])
check("tracker_id导出自动带入标记当前游标作为原游标",
      linked["manifest"]["origin_cursor"] == "增量追踪样例2")

print("== 场景2：前班提前补采20-28，导入时同一缺口已有两个观察范围 ==")
# 观察范围1随清单文件携带，观察范围2在导入时提交，二者共同构成已验证覆盖依据
manifest_with_obs = dict(manifest)
manifest_with_obs["observations"] = [{
    "source_endpoint": "EP-ORDER-SYNC",
    "origin_cursor": "18",
    "cursor_from": 20,
    "cursor_to": 23,
    "basis": "前班补采批次B1回执",
}]
imp = post("/api/incr-gap/import", {
    "manifest": manifest_with_obs,
    "observations": [{
        "source_endpoint": "EP-ORDER-SYNC",
        "origin_cursor": "18",
        "cursor_from": 24,
        "cursor_to": 28,
        "basis": "前班补采批次B2回执",
        "verified_by": "前班值班员张三",
    }],
    "operator": "接班值班员李四",
    "shift_team": "采集值班B组",
})
check("导入成功", imp["ok"] is True, imp["message"])
handoff = imp["handoff"]
detail = imp["detail"]
check("交接记录仍保留原始20-40缺口（21条）", handoff["缺口条数"] == 21
      and "[20,40]@EP-ORDER-SYNC" in handoff["缺口区间"], handoff["缺口区间"])
check("待重放条数为12（29-40）", handoff["待重放条数"] == 12, str(handoff["待重放条数"]))
check("当前待重放区间只包含29-40",
      handoff["待重放区间"] == "[29,40]@EP-ORDER-SYNC", handoff["待重放区间"])
covered = [s for s in detail["segments"] if s["status"] == "已补齐"]
check("20-28 留有已确认段可供复核",
      any(s["起始游标"] == 20 and s["结束游标"] == 28 for s in covered),
      str([(s["段编号"], s["起始游标"], s["结束游标"], s["status"]) for s in detail["segments"]]))
note2028 = next(n for n in detail["dispatch_notes"] if n["区间"] == "[20,28]")
check("重放前记录说明20-28为何不再派发，且含两个观察范围依据",
      "观察范围" in note2028["不派发说明"]
      and "B1" in note2028["不派发说明"] and "B2" in note2028["不派发说明"],
      note2028["不派发说明"])
check("覆盖段绑定来源端点与原游标", note2028["来源端点"] == "EP-ORDER-SYNC"
      and note2028["原游标"] == "18")
handoff_id = handoff["id"]

print("== 场景3：范围相近但来源不同的两段不能合并 ==")
foreign = post("/api/incr-gap/import", {
    "manifest": {
        "manifest_type": "incremental_gap_replay_manifest",
        "version": 1,
        "handoff_no": "GAP-FOREIGN-1",
        "source_endpoint": "EP-OTHER-SYNC",
        "origin_cursor": "18",
        "exported_by": "前班值班员张三",
        "shift_team": "采集值班A组",
        "exported_at": "2026-11-22 08:00:00",
        "gaps": [{
            "gap_no": "GAP-X1",
            "source_endpoint": "EP-OTHER-SYNC",
            "origin_cursor": "18",
            "cursor_from": 20,
            "cursor_to": 40,
            "gap_log": "另一来源端点的缺口",
            "coverage_basis": "缺口日志待补采",
        }],
        "observations": [],
    },
    "observations": [{
        # 与20-28范围相近，但来源端点不同，绝不能缩减 EP-ORDER-SYNC 的待重放区间
        "source_endpoint": "EP-OTHER-SYNC",
        "origin_cursor": "18",
        "cursor_from": 20,
        "cursor_to": 28,
        "basis": "外部来源观察范围",
    }],
    "operator": "接班值班员李四",
})
check("不同来源清单独立建单", foreign["ok"] is True)
check("外部来源只按自己端点的依据裁剪",
      foreign["handoff"]["待重放区间"] == "[29,40]@EP-OTHER-SYNC",
      foreign["handoff"]["待重放区间"])
still = get(f"/api/incr-gap/handoffs/{handoff_id}")["detail"]
check("原清单29-40待重放区间未被外部来源合并",
      still["handoff"]["待重放区间"] == "[29,40]@EP-ORDER-SYNC",
      still["handoff"]["待重放区间"])

print("== 场景4：连续两次提交同一文件，只留一份可认领工作 ==")
dup1 = post("/api/incr-gap/import", {"manifest": manifest_with_obs})
dup2 = post("/api/incr-gap/import", {"manifest": manifest_with_obs})
check("重复提交返回幂等提示", "已存在" in dup1["message"] and "已存在" in dup2["message"],
      dup1["message"])
listing = get("/api/incr-gap/handoffs")
same_hash = [h for h in listing["items"] if h["file_hash"] == manifest["file_hash"]]
check("同一文件只对应一份交接记录", len(same_hash) == 1, str(len(same_hash)))
subs = get(f"/api/incr-gap/handoffs/{handoff_id}")["detail"]["submissions"]
check("三次提交均留痕（首次导入+两次重复提交）", len(subs) == 3, str(subs))
check("已确认范围仍可复核", any(
    s["status"] == "已补齐"
    for s in get(f"/api/incr-gap/handoffs/{handoff_id}")["detail"]["segments"]))

print("== 场景5：接班人认领，待重放段挂钩重放队列 ==")
claim = post(f"/api/incr-gap/handoffs/{handoff_id}/claim")
check("认领成功", claim["ok"] is True, claim["message"])
pending_seg = next(s for s in claim["detail"]["segments"] if s["status"] == "待重放")
check("待重放段已挂钩重放队列任务", pending_seg["retry_task_id"] is not None)
retry_page = get("/api/retry_queue?status=待重试")
gap_tasks = [r for r in retry_page["items"] if r.get("队列状态", "").startswith("gap_handoff")]
check("重放队列出现缺口重放任务", len(gap_tasks) >= 1, str(len(gap_tasks)))

print("== 场景6：导入中断后续做——按实际回执缩减待重放区间（29-33先补齐） ==")
receipt = post(f"/api/incr-gap/handoffs/{handoff_id}/receipts", {
    "operator": "接班值班员李四",
    "receipts": [{
        "source_endpoint": "EP-ORDER-SYNC",
        "origin_cursor": "18",
        "cursor_from": 29,
        "cursor_to": 33,
        "basis": "重放补齐批次R1回执",
    }],
})
check("回执受理", receipt["ok"] is True, receipt["message"])
h = receipt["handoff"]
check("待重放区间缩减为34-40", h["待重放区间"] == "[34,40]@EP-ORDER-SYNC",
      h["待重放区间"])
check("待重放条数缩减为7", h["待重放条数"] == 7, str(h["待重放条数"]))
check("状态变为部分完成", h["status"] == "部分完成", h["status"])
done_piece = next(n for n in receipt["detail"]["dispatch_notes"] if n["区间"] == "[29,33]")
check("29-33 有不再派发的回执说明", "回执" in done_piece["不派发说明"],
      done_piece["不派发说明"])

print("== 场景7：来源/原游标不匹配的回执必须被拒绝 ==")
bad = post(f"/api/incr-gap/handoffs/{handoff_id}/receipts", {
    "receipts": [{
        "source_endpoint": "EP-ORDER-SYNC",
        "origin_cursor": "99",  # 原游标不一致
        "cursor_from": 34,
        "cursor_to": 40,
        "basis": "伪造回执",
    }],
})
check("不匹配回执被拒绝并给出提示", "拒绝" in bad["message"], bad["message"])
check("拒绝后待重放区间不变",
      bad["handoff"]["待重放区间"] == "[34,40]@EP-ORDER-SYNC",
      bad["handoff"]["待重放区间"])
bad2 = post(f"/api/incr-gap/handoffs/{handoff_id}/receipts", {
    "receipts": [{
        "source_endpoint": "EP-OTHER-SYNC",  # 来源端点不一致
        "origin_cursor": "18",
        "cursor_from": 34,
        "cursor_to": 40,
        "basis": "外部来源回执",
    }],
})
check("来源不同的回执同样被拒绝", "拒绝" in bad2["message"], bad2["message"])

print("== 场景8：重放队列用实际补齐回执确认剩余数据，并同步回清单 ==")
seg = next(s for s in get(f"/api/incr-gap/handoffs/{handoff_id}")["detail"]["segments"]
           if s["status"] == "待重放")
task_id = seg["retry_task_id"]
action = post(f"/api/retry_queue/{task_id}/actions", {"action": "手动重试"})
check("重放队列任务手动重试成功", action["ok"] is True, action["message"])
sync = post(f"/api/incr-gap/handoffs/{handoff_id}/sync-replay")
check("重放结果同步回导入清单", sync["ok"] is True, sync["message"])
check("同步后无待重放数据", sync["handoff"]["待重放条数"] == 0
      and "无" in sync["handoff"]["待重放区间"], sync["handoff"]["待重放区间"])
check("清单状态为已完成", sync["handoff"]["status"] == "已完成", sync["handoff"]["status"])

print("== 场景9：追踪员核对原导出范围与本轮新取得的数据 ==")
final = get(f"/api/incr-gap/handoffs/{handoff_id}")["detail"]
check("交接记录仍能看到原始20-40的来源",
      final["original_gap"] == "[20,40]@EP-ORDER-SYNC", final["original_gap"])
check("当前待重放范围为空", "无" in final["current_pending"], final["current_pending"])
seg_ranges = sorted((s["起始游标"], s["结束游标"], s["status"]) for s in final["segments"])
print("    段轨迹:", seg_ranges)
check("段轨迹完整覆盖20-40且无重叠缺口",
      [r[:2] for r in seg_ranges] == [(20, 28), (29, 33), (34, 40)], str(seg_ranges))
notes = {n["区间"]: n for n in final["dispatch_notes"]}
check("重放前记录说明20-28不再派发（已验证观察范围）",
      "观察范围" in notes["[20,28]"]["不派发说明"], notes["[20,28]"]["不派发说明"])
check("重放后记录说明29-33不再派发（实际补齐回执）",
      "回执" in notes["[29,33]"]["不派发说明"], notes["[29,33]"]["不派发说明"])
check("重放后记录说明34-40不再派发（重放队列回执）",
      "回执" in notes["[34,40]"]["不派发说明"], notes["[34,40]"]["不派发说明"])
review = post(f"/api/incr-gap/handoffs/{handoff_id}/review")
check("复核通过并归档", review["ok"] is True and review["handoff"]["status"] == "已复核",
      review["message"])

print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
