# -*- coding: utf-8 -*-
"""巡检执行（模拟）任务状态。

设计原则：
- 任务状态是普通字典，由页面放在当前用户的 Streamlit Session State 中；
  本模块不使用全局变量，也不跨会话共享。
- 所有修改函数都返回新的深拷贝状态，不原地修改调用方对象。
- 任务只记录“确认检查完成”，不修改 A 的检测结论，也不表示设备已修复。
- 前进只能沿剩余路线走相邻节点，并且该通道必须 enabled=true 且未关闭；
  不提供“直接改当前位置”的接口。
- 重新规划复用 D 的 plan_inspection，不重置当前位置、已走距离和已完成设备。
"""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.cd_state import detection_result_signature
from spatial_module.plan import (
    ROOT,
    PlanInputError,
    _available_graph,
    _load_scene,
    _plan_inspection_for_scene,
    plan_inspection,
)
from spatial_module.visualize import render_route

MISSION_SCHEMA_VERSION = "1.0"
PATCH_VERSION = "CD-R2-20261025"
STATUS_ACTIVE = "active"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETED = "completed"
STATUS_NO_TARGETS = "no_targets"
MISSION_STATUSES = {
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_NO_TARGETS,
}
SOURCE_DETECTION = "detection"
SOURCE_SIMULATION = "simulation_case"
VALID_SOURCES = {SOURCE_DETECTION, SOURCE_SIMULATION}

COMPLETION_NOTE = (
    "“检查完成”只表示人员确认本轮检查结束，不代表故障已维修或设备已恢复正常；"
    "本任务不修改 A 的原始检测结论。"
)
DISTANCE_TOL = 1e-9


class MissionError(ValueError):
    """任务输入、状态或路线不合法。页面应原样显示原因，不能当作完成。"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_mission_id() -> str:
    return "mis-" + uuid.uuid4().hex[:12]


def _copy(mission: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(mission)


def _require_state(mission: Any) -> dict[str, Any]:
    if not isinstance(mission, dict):
        raise MissionError("任务状态必须是字典")
    if mission.get("schema_version") != MISSION_SCHEMA_VERSION:
        raise MissionError("任务状态版本不合法或不是本模块创建的任务")
    if not mission.get("mission_id"):
        raise MissionError("任务状态缺少 mission_id")
    return mission


def _invalid_snapshot_notes(snapshot: list[dict]) -> tuple[list[str], list[str]]:
    assets: list[str] = []
    notes: list[str] = []
    for row in snapshot:
        if not isinstance(row, dict) or row.get("status") != "invalid_input":
            continue
        asset_id = str(row.get("asset_id") or "").strip().upper()
        if asset_id and asset_id not in assets:
            assets.append(asset_id)
        detail_parts: list[str] = []
        for key in ("notes", "message"):
            value = row.get(key)
            if isinstance(value, (list, tuple)):
                detail_parts.extend(str(part).strip() for part in value if str(part).strip())
            elif isinstance(value, str) and value.strip():
                detail_parts.append(value.strip())
        detail = "；".join(detail_parts)
        notes.append(
            f"{asset_id or '未知设备'} 的录音为 invalid_input，需要补录"
            + (f"：{detail}" if detail else "")
        )
    return assets, notes


def _plan_for(
    rows: list[dict],
    current_node: str,
    closed_edges: list[str],
    scene_dir: str | Path | None,
    output_dir: str | Path,
) -> dict[str, Any]:
    try:
        if scene_dir is None:
            plan = plan_inspection(
                rows,
                start_node=current_node,
                closed_edges=closed_edges,
                output_dir=output_dir,
            )
        else:
            plan = _plan_inspection_for_scene(
                rows,
                current_node,
                closed_edges,
                output_dir,
                scene_dir=Path(scene_dir),
            )
    except (PlanInputError, OSError, ValueError) as exc:
        raise MissionError(f"剩余路线规划失败：{exc}") from exc
    if not isinstance(plan, dict):
        raise MissionError("D 规划返回格式不合法")
    if plan.get("status") == "invalid_input":
        detail = "；".join(str(n) for n in (plan.get("notes") or []) if str(n).strip())
        raise MissionError("D 返回 invalid_input" + (f"：{detail}" if detail else ""))
    return plan


def _pending_rows(mission: dict[str, Any]) -> list[dict]:
    snapshot = (mission.get("source") or {}).get("detection_snapshot") or []
    by_asset = {
        str(row.get("asset_id") or "").strip().upper(): row
        for row in snapshot
        if isinstance(row, dict) and row.get("asset_id")
    }
    rows: list[dict] = []
    for asset_id in mission.get("pending_assets") or []:
        row = by_asset.get(asset_id)
        if row is None:
            raise MissionError(f"任务快照中缺少待检设备 {asset_id} 的检测记录")
        if row.get("status") != "suspected_anomaly":
            raise MissionError(
                f"待检设备 {asset_id} 的原始状态不是 suspected_anomaly，不能继续规划"
            )
        rows.append(copy.deepcopy(row))
    return rows


def _apply_plan(mission: dict[str, Any], plan: dict[str, Any]) -> None:
    """把 D 的剩余规划写入任务，但不碰当前位置、已完成和已走距离。"""
    pending = list(mission.get("pending_assets") or [])
    plan_unreachable = {
        str(asset).strip().upper()
        for asset in (plan.get("unreachable_asset_ids") or [])
    }
    unreachable = [asset for asset in pending if asset in plan_unreachable]
    reachable = [asset for asset in pending if asset not in plan_unreachable]

    mission["remaining_plan"] = plan
    plan_closed = (
        plan.get("closed_edges")
        if "closed_edges" in plan
        else mission.get("closed_edges") or []
    )
    mission["closed_edges"] = list(plan_closed)
    mission["unreachable_assets"] = unreachable
    mission["reachable_assets"] = reachable
    mission["remaining_distance_m"] = round(
        float(plan.get("total_distance_m") or 0.0), 6
    )

    initial_targets = list(mission.get("initial_targets") or [])
    if not initial_targets:
        status = STATUS_NO_TARGETS
    elif not pending:
        status = STATUS_COMPLETED
    elif unreachable and len(unreachable) == len(pending):
        status = STATUS_BLOCKED
    else:
        status = STATUS_ACTIVE
    mission["status"] = status

    if status == STATUS_COMPLETED:
        mission["expected_total_distance_m"] = round(
            float(mission.get("walked_distance_m") or 0.0), 6
        )
    elif status == STATUS_NO_TARGETS:
        mission["expected_total_distance_m"] = 0.0
    elif status == STATUS_ACTIVE and not unreachable:
        mission["expected_total_distance_m"] = round(
            float(mission.get("walked_distance_m") or 0.0)
            + float(mission.get("remaining_distance_m") or 0.0),
            6,
        )
    else:
        # 有不可达目标时，可行路段的距离不能冒充“完成全部任务”的总里程。
        mission["expected_total_distance_m"] = None


def _event(mission: dict[str, Any], event_type: str, **details: Any) -> None:
    events = mission.setdefault("events", [])
    events.append(
        {
            "seq": len(events) + 1,
            "at": _now(),
            "type": event_type,
            **details,
        }
    )


def create_mission(
    detection_results: Any,
    *,
    source_kind: str,
    source_label: str,
    is_mock: bool = False,
    source_signature: str = "",
    case_id: str = "",
    source_note: str = "",
    start_node: str = "S",
    closed_edges: list[str] | None = None,
    scene_dir: str | Path | None = None,
    output_dir: str | Path = "runs/missions",
) -> dict[str, Any]:
    """创建一个巡检任务。

    detection_results 是任务的输入快照；source_kind 取 detection / simulation_case。
    任务创建后不再从全局检测结果读取目标，后续重规划只使用快照中尚未完成的目标。
    """
    if source_kind not in VALID_SOURCES:
        raise MissionError(f"未知任务来源：{source_kind!r}")
    if not isinstance(detection_results, list) or not detection_results:
        raise MissionError("创建巡检任务需要至少一条检测结果")
    snapshot = copy.deepcopy(detection_results)
    invalid_assets, invalid_notes = _invalid_snapshot_notes(snapshot)

    plan = _plan_for(
        snapshot,
        current_node=start_node,
        closed_edges=list(closed_edges or []),
        scene_dir=scene_dir,
        output_dir=output_dir,
    )
    initial_targets = [
        str(asset).strip().upper()
        for asset in (plan.get("target_asset_ids") or [])
    ]
    if not initial_targets:
        initial_targets = [
            str(row.get("asset_id")).strip().upper()
            for row in snapshot
            if isinstance(row, dict) and row.get("status") == "suspected_anomaly"
        ]

    mission: dict[str, Any] = {
        "schema_version": MISSION_SCHEMA_VERSION,
        "patch_version": PATCH_VERSION,
        "mission_id": _new_mission_id(),
        "created_at": _now(),
        "updated_at": _now(),
        "status": STATUS_ACTIVE,
        "source": {
            "kind": source_kind,
            "label": str(source_label).strip(),
            "is_mock": bool(is_mock),
            "case_id": str(case_id).strip(),
            "source_note": str(source_note).strip(),
            "input_signature": str(source_signature or ""),
            "result_signature": detection_result_signature(snapshot),
            "detection_snapshot": snapshot,
            "invalid_input_assets": invalid_assets,
            "invalid_input_notes": invalid_notes,
        },
        "start_node": str(plan.get("start_node") or start_node),
        "current_node": str(plan.get("start_node") or start_node),
        "initial_targets": initial_targets,
        "pending_assets": list(initial_targets),
        "completed_assets": [],
        "unreachable_assets": [],
        "reachable_assets": [],
        "closed_edges": [],
        "walked_distance_m": 0.0,
        "remaining_distance_m": 0.0,
        "expected_total_distance_m": 0.0,
        "remaining_plan": plan,
        "events": [],
        "completion_note": COMPLETION_NOTE,
    }
    _apply_plan(mission, plan)
    _event(
        mission,
        "created",
        source_kind=source_kind,
        source_label=mission["source"]["label"],
        case_id=mission["source"]["case_id"],
        is_mock=mission["source"]["is_mock"],
        start_node=mission["current_node"],
        closed_edges=list(mission["closed_edges"]),
        initial_targets=list(initial_targets),
        plan_status=plan.get("status"),
        plan_order=list(plan.get("visit_order") or []),
        remaining_distance_m=mission["remaining_distance_m"],
    )
    return mission


def replan_mission(
    mission: dict[str, Any],
    *,
    closed_edges: list[str] | None = None,
    scene_dir: str | Path | None = None,
    output_dir: str | Path = "runs/missions",
    reason: str = "manual",
) -> dict[str, Any]:
    """从当前位置重新规划剩余目标；不重置位置、已走距离和已完成设备。"""
    state = _copy(_require_state(mission))
    if state.get("status") == STATUS_COMPLETED:
        raise MissionError("巡检已完成，不能重新规划；如需新任务请重新创建")
    new_closed = (
        list(state.get("closed_edges") or [])
        if closed_edges is None
        else [str(edge).strip().upper() for edge in closed_edges if str(edge).strip()]
    )
    previous_closed = list(state.get("closed_edges") or [])
    previous_status = state.get("status")
    previous_route = list((state.get("remaining_plan") or {}).get("route_nodes") or [])

    plan = _plan_for(
        _pending_rows(state),
        current_node=str(state.get("current_node") or ""),
        closed_edges=new_closed,
        scene_dir=scene_dir,
        output_dir=output_dir,
    )
    _apply_plan(state, plan)
    state["updated_at"] = _now()
    _event(
        state,
        "replan",
        reason=reason,
        start_node=state["current_node"],
        closed_edges_before=previous_closed,
        closed_edges_after=list(state["closed_edges"]),
        plan_status=plan.get("status"),
        plan_order=list(plan.get("visit_order") or []),
        route_nodes=list(plan.get("route_nodes") or []),
        remaining_distance_m=state["remaining_distance_m"],
        walked_distance_m=state["walked_distance_m"],
        pending_assets=list(state["pending_assets"]),
        unreachable_assets=list(state["unreachable_assets"]),
        previous_status=previous_status,
        previous_route_nodes=previous_route,
    )
    if previous_status != STATUS_COMPLETED and state["status"] == STATUS_COMPLETED:
        _event(
            state,
            "completed",
            walked_distance_m=state["walked_distance_m"],
            completed_assets=list(state["completed_assets"]),
        )
    return state


def _render_current_route(
    mission: dict[str, Any],
    *,
    scene_dir: str | Path | None,
    output_dir: str | Path,
) -> str | None:
    """按当前剩余路线画一张进度图；失败时返回 None，不让整个任务崩溃。"""
    route = list((mission.get("remaining_plan") or {}).get("route_nodes") or [])
    if not route:
        return None
    base = Path(output_dir)
    if not base.is_absolute():
        base = ROOT / base
    try:
        base = base.resolve()
        if not base.is_relative_to(ROOT):
            return None
        progress_dir = base / "progress"
        progress_dir.mkdir(parents=True, exist_ok=True)
        relative = (
            progress_dir
            / f"{mission['mission_id']}_step_{len(mission.get('events') or []) + 1}.png"
        ).relative_to(ROOT).as_posix()
        returned = render_route(
            scene_dir if scene_dir is not None else "scene",
            route,
            relative,
            closed_edges=list(mission.get("closed_edges") or []),
        )
    except Exception:
        return None
    path = Path(str(returned))
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        return None
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def step_forward(
    mission: dict[str, Any],
    *,
    scene_dir: str | Path | None = None,
    output_dir: str | Path = "runs/missions",
) -> dict[str, Any]:
    """沿当前剩余路线前进一步。只能走一条开放、enabled 的相邻通道。"""
    state = _copy(_require_state(mission))
    if state.get("status") in (STATUS_COMPLETED, STATUS_NO_TARGETS):
        raise MissionError("任务已结束，不能继续行走")
    if state.get("status") == STATUS_BLOCKED:
        raise MissionError("任务受阻：待检目标全部不可达，没有可行路线")

    current = str(state.get("current_node") or "")
    scene = _load_scene(scene_dir or (ROOT / "scene"))
    pending_here = [
        asset_id
        for asset_id in state.get("pending_assets") or []
        if asset_id in scene.assets
        and scene.assets[asset_id]["node_id"] == current
    ]
    if pending_here:
        raise MissionError(
            f"当前节点 {current} 的待检设备 "
            f"{'、'.join(pending_here)} 尚未确认检查完成；"
            "请先确认，不能直接继续行走"
        )

    plan = state.get("remaining_plan") or {}
    route = list(plan.get("route_nodes") or [])
    if len(route) < 2:
        raise MissionError("当前剩余路线没有可前进的下一步；请确认检查或重新规划")
    if route[0] != current:
        raise MissionError("剩余路线与当前位置不一致，请先重新规划")
    next_node = str(route[1])

    graph = _available_graph(scene, list(state.get("closed_edges") or []))
    if current not in graph or next_node not in graph:
        raise MissionError(f"路线包含未知节点：{current} 或 {next_node}")
    if not graph.has_edge(current, next_node):
        raise MissionError(
            f"不能从 {current} 直接走到 {next_node}：该通道已关闭、未启用或不存在"
        )

    edge = next(
        (
            item
            for item in scene.edges.values()
            if frozenset((item["from_node"], item["to_node"]))
            == frozenset((current, next_node))
        ),
        None,
    )
    if edge is None or not edge.get("enabled"):
        raise MissionError(f"通道不可用：{current} → {next_node}")
    if edge["edge_id"] in set(state.get("closed_edges") or []):
        raise MissionError(f"通道 {edge['edge_id']} 已关闭，不能通过")
    length = float(edge["length_m"])

    state["current_node"] = next_node
    state["walked_distance_m"] = round(
        float(state.get("walked_distance_m") or 0.0) + length, 6
    )

    new_plan = copy.deepcopy(plan)
    new_plan["route_nodes"] = route[1:]
    new_plan["start_node"] = next_node
    new_plan["total_distance_m"] = round(
        max(0.0, float(plan.get("total_distance_m") or 0.0) - length), 6
    )
    state["remaining_plan"] = new_plan
    new_plan["map_file"] = _render_current_route(
        state,
        scene_dir=scene_dir,
        output_dir=output_dir,
    )
    state["remaining_distance_m"] = float(new_plan["total_distance_m"])
    if state.get("status") == STATUS_ACTIVE and not state.get("unreachable_assets"):
        state["expected_total_distance_m"] = round(
            state["walked_distance_m"] + state["remaining_distance_m"], 6
        )
    else:
        state["expected_total_distance_m"] = None
    state["updated_at"] = _now()
    _event(
        state,
        "step",
        from_node=current,
        to_node=next_node,
        edge_id=edge["edge_id"],
        length_m=length,
        walked_distance_m=state["walked_distance_m"],
        remaining_distance_m=state["remaining_distance_m"],
        map_file=new_plan.get("map_file"),
    )
    return state


def confirm_asset(
    mission: dict[str, Any],
    asset_id: str,
    *,
    scene_dir: str | Path | None = None,
    output_dir: str | Path = "runs/missions",
) -> dict[str, Any]:
    """在当前设备节点由人员确认检查完成；不自动确认，不重复统计。"""
    state = _copy(_require_state(mission))
    asset = str(asset_id or "").strip().upper()
    if not asset:
        raise MissionError("确认检查需要指定设备编号")
    if state.get("status") in (STATUS_COMPLETED, STATUS_NO_TARGETS):
        raise MissionError("任务已结束，不能继续确认检查")
    if asset in set(state.get("completed_assets") or []):
        raise MissionError(f"{asset} 已确认检查完成，不能重复统计")
    if asset not in set(state.get("pending_assets") or []):
        raise MissionError(f"{asset} 不在当前待检设备中")

    scene = _load_scene(scene_dir or (ROOT / "scene"))
    if asset not in scene.assets:
        raise MissionError(f"未知设备：{asset}")
    target_node = scene.assets[asset]["node_id"]
    current = str(state.get("current_node") or "")
    if current != target_node:
        raise MissionError(
            f"当前在 {current}，{asset} 对应节点是 {target_node}；"
            "未到达设备节点，不能确认检查完成"
        )

    # 重要等级约束：仍有更高等级、当前可达的待检目标时，不能先确认低等级设备。
    asset_level = int(scene.assets[asset]["criticality"])
    reachable = set(state.get("reachable_assets") or [])
    if not reachable:
        reachable = set(state.get("pending_assets") or [])
    blocking = [
        other
        for other in state.get("pending_assets") or []
        if other != asset
        and other in reachable
        and int(scene.assets[other]["criticality"]) > asset_level
    ]
    if blocking:
        details = "、".join(
            f"{other}（等级 {scene.assets[other]['criticality']}）"
            for other in blocking
        )
        raise MissionError(
            f"不能先确认 {asset}（等级 {asset_level}）；"
            f"仍有更高等级的可达待检目标：{details}，必须先按等级顺序检查"
        )

    state["pending_assets"] = [
        item for item in state.get("pending_assets") or [] if item != asset
    ]
    state["completed_assets"] = list(state.get("completed_assets") or []) + [asset]
    _event(
        state,
        "confirm",
        asset_id=asset,
        node_id=target_node,
        walked_distance_m=state["walked_distance_m"],
        completed_assets=list(state["completed_assets"]),
        pending_assets=list(state["pending_assets"]),
    )
    state = replan_mission(
        state,
        closed_edges=list(state.get("closed_edges") or []),
        scene_dir=scene_dir,
        output_dir=output_dir,
        reason=f"confirm:{asset}",
    )
    return state


def mission_is_stale(
    mission: dict[str, Any] | None,
    current_signature: str | None,
    current_result_signature: str | None = None,
) -> bool:
    """录音检测来源的任务，输入或检测结果变化后标记为失效。

    模拟案例任务不依赖录音检测，因此不受影响。
    """
    if not isinstance(mission, dict):
        return False
    source = mission.get("source") or {}
    if source.get("kind") != SOURCE_DETECTION:
        return False
    saved_input = str(source.get("input_signature") or "")
    if saved_input and saved_input != str(current_signature or ""):
        return True
    if current_result_signature is not None:
        saved_result = str(source.get("result_signature") or "")
        if saved_result and saved_result != str(current_result_signature or ""):
            return True
    return False


def mission_to_json(mission: dict[str, Any]) -> str:
    """导出任务 JSON（不会修改任务本身）。"""
    state = _copy(_require_state(mission))
    return json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
