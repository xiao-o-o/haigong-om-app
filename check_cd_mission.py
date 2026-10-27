# -*- coding: utf-8 -*-
"""C/D 第二轮：巡检执行（模拟）检查入口。

运行：
    .venv\\Scripts\\python.exe check_cd_mission.py

本脚本覆盖任务状态、行走、确认、重规划、封路、受阻/完成区分、Agent 任务重规划、
页面状态隔离和导出。A 系统异常、LLM 失败使用本地临时替身，不调用付费 API，
也不修改模型文件；文档中不得写成“真实 LLM 已通过”。
"""
from __future__ import annotations

import copy
import json
import platform
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.loop import run_agent  # noqa: E402
from agent.tools import detect_assets_audio  # noqa: E402
from core import cd_state, llm  # noqa: E402
from core.cd_cases import case_detection_results, load_cases  # noqa: E402
from core.local_modules import ModuleUnavailable, list_assets  # noqa: E402
from spatial_module.mission import (  # noqa: E402
    MissionError,
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_NO_TARGETS,
    confirm_asset,
    create_mission,
    mission_is_stale,
    mission_to_json,
    replan_mission,
    step_forward,
)

SAMPLE_ITEMS = [
    {
        "asset_id": "P01",
        "path": str(ROOT / "samples" / "P01_sample.wav"),
        "filename": "P01_sample.wav",
    },
    {
        "asset_id": "P04",
        "path": str(ROOT / "samples" / "P04_sample.wav"),
        "filename": "P04_sample.wav",
    },
]
DET_DEFAULT = [
    {"asset_id": "P01", "status": "normal"},
    {"asset_id": "P04", "status": "suspected_anomaly"},
]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _approx(actual: float | None, expected: float, tol: float = 1e-6) -> bool:
    return actual is not None and abs(float(actual) - float(expected)) <= tol


def _c1_case() -> dict:
    return next(case for case in load_cases()["cases"] if case["case_id"] == "C1")


def _c1_mission(output_dir: Path) -> dict:
    case = _c1_case()
    return create_mission(
        case_detection_results(case),
        source_kind="simulation_case",
        source_label=case["name"],
        case_id=case["case_id"],
        is_mock=True,
        start_node=case["start_node"],
        closed_edges=case["closed_edges"],
        output_dir=str(output_dir),
    )


def _walk(mission: dict, count: int, output_dir: Path) -> dict:
    state = mission
    for _ in range(count):
        state = step_forward(state)
    return state


def _context(mission: dict | None, detections: list[dict], signature: str = "") -> dict:
    return {
        "files": SAMPLE_ITEMS,
        "detections": detections,
        "detections_is_mock": True,
        "start_node": (mission or {}).get("current_node") or "S",
        "closed_edges": (mission or {}).get("closed_edges") or [],
        "mission": mission,
        "mission_stale": False,
        "detection_signature": signature,
    }


class Checker:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def check(self, label: str, func) -> None:
        started = time.perf_counter()
        try:
            detail = func() or ""
        except AssertionError as exc:
            elapsed = (time.perf_counter() - started) * 1000
            self.results.append(
                {
                    "label": label,
                    "ok": False,
                    "detail": str(exc),
                    "elapsed_ms": round(elapsed, 1),
                }
            )
            print(f"[失败] {label}：{exc}")
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.perf_counter() - started) * 1000
            self.results.append(
                {
                    "label": label,
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round(elapsed, 1),
                    "traceback": traceback.format_exc(),
                }
            )
            print(f"[失败] {label}：{type(exc).__name__}: {exc}")
        else:
            elapsed = (time.perf_counter() - started) * 1000
            self.results.append(
                {
                    "label": label,
                    "ok": True,
                    "detail": str(detail),
                    "elapsed_ms": round(elapsed, 1),
                }
            )
            print(f"[通过] {label}：{detail}（{elapsed:.0f} ms）")


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / "runs" / "check_cd_mission" / stamp
    out.mkdir(parents=True, exist_ok=False)
    checker = Checker()
    mission_dir = out / "missions"

    def test_environment() -> str:
        import matplotlib
        import networkx
        import numpy
        import pandas
        import sklearn
        import streamlit

        lock = ROOT / "docs" / "CD_ENV_LOCK_20261025.txt"
        _assert(lock.is_file(), "缺少精确依赖清单 docs/CD_ENV_LOCK_20261025.txt")
        lock_text = lock.read_text(encoding="utf-8")
        _assert("Python 3.13.13" in lock_text, "依赖清单没有标注 Python 3.13.13")
        _assert("Windows" in lock_text, "依赖清单没有标注 Windows")
        return (
            f"Python {platform.python_version()}；sklearn {sklearn.__version__}；"
            f"numpy {numpy.__version__}；pandas {pandas.__version__}；"
            f"networkx {networkx.__version__}；matplotlib {matplotlib.__version__}；"
            f"streamlit {streamlit.__version__}"
        )

    def test_c1_create() -> str:
        mission = _c1_mission(mission_dir)
        _assert(mission["status"] == STATUS_ACTIVE, f"status={mission['status']}")
        _assert(mission["current_node"] == "S", "起点不是 S")
        _assert(mission["pending_assets"] == ["P02", "P05"], "待检设备不符")
        _assert(mission["completed_assets"] == [], "新建任务不应有已完成设备")
        _assert(_approx(mission["walked_distance_m"], 0.0), "初始已走不是 0")
        _assert(_approx(mission["remaining_distance_m"], 70.0), "初始剩余不是 70")
        _assert(_approx(mission["expected_total_distance_m"], 70.0), "预计总里程不是 70")
        _assert(
            mission["remaining_plan"]["visit_order"] == ["P05", "P02"],
            f"优化顺序不符：{mission['remaining_plan']['visit_order']}",
        )
        _assert(mission["source"]["case_id"] == "C1", "来源案例未保存")
        _assert(mission["source"]["is_mock"] is True, "模拟来源未标注")
        return "C1 从 S 创建，待检 P02/P05，已走 0 m，剩余 70 m"

    def test_c2_criticality_constraint() -> str:
        case = next(
            item for item in load_cases()["cases"] if item["case_id"] == "C2"
        )
        mission = create_mission(
            case_detection_results(case),
            source_kind="simulation_case",
            source_label=case["name"],
            case_id=case["case_id"],
            is_mock=True,
            start_node=case["start_node"],
            closed_edges=case["closed_edges"],
            output_dir=str(mission_dir),
        )
        criticality = {
            item["asset_id"]: int(item["criticality"]) for item in list_assets()
        }
        order = mission["remaining_plan"]["visit_order"]
        levels = [criticality[asset] for asset in order]
        _assert(
            levels == sorted(levels, reverse=True),
            f"C2 任务剩余顺序不满足等级约束：{order} / {levels}",
        )
        _assert(
            mission["pending_assets"] == case["expected"]["optimized_visit_order"]
            or set(mission["pending_assets"]) == set(case["expected"]["optimized_visit_order"]),
            "C2 待检目标与案例不符",
        )

        # 防御性检查：即使人为把位置放到 P04，也不能绕过 P01/P02 先确认低等级设备。
        skipped = copy.deepcopy(mission)
        skipped["current_node"] = "N04"
        try:
            confirm_asset(skipped, "P04", output_dir=str(mission_dir))
        except MissionError as exc:
            _assert("更高等级" in str(exc), f"等级拦截提示不明确：{exc}")
        else:
            raise AssertionError("站在 P04 节点绕过了 P01/P02 的高等级约束")
        _assert(
            skipped["pending_assets"] == mission["pending_assets"],
            "被拒绝的确认改动了待检列表",
        )
        return f"C2 剩余顺序 {'→'.join(order)} 等级不升高；低等级不能绕过更高等级"

    def test_walk_and_confirm() -> str:
        mission = _walk(_c1_mission(mission_dir), 2, mission_dir)
        _assert(mission["current_node"] == "N05", f"当前位置 {mission['current_node']}")
        _assert(_approx(mission["walked_distance_m"], 30.0), "走到 N05 应为 30 m")
        _assert(_approx(mission["remaining_distance_m"], 40.0), "到 N05 后剩余应为 40 m")
        map_file = mission["remaining_plan"].get("map_file")
        _assert(bool(map_file), "前进一步后没有更新剩余路线图")
        map_path = Path(map_file)
        if not map_path.is_absolute():
            map_path = ROOT / map_path
        _assert(map_path.is_file(), "前进一步后的剩余路线图不存在")

        # 到达 P05 节点但未确认时，不能直接继续走。
        unconfirmed = copy.deepcopy(mission)
        try:
            step_forward(unconfirmed)
        except MissionError as exc:
            _assert("尚未确认" in str(exc), f"未确认拦截提示不明确：{exc}")
        else:
            raise AssertionError("到达 P05 未确认仍能继续行走")
        _assert(unconfirmed["current_node"] == "N05", "未确认拦截改变了当前位置")
        _assert(
            _approx(unconfirmed["walked_distance_m"], 30.0),
            "未确认拦截改变了已走距离",
        )
        _assert(
            unconfirmed["pending_assets"] == ["P02", "P05"],
            "未确认拦截改变了待检列表",
        )

        confirmed = confirm_asset(mission, "P05", output_dir=str(mission_dir))
        _assert(confirmed["status"] == STATUS_ACTIVE, "确认 P05 后仍应有待检目标")
        _assert(confirmed["pending_assets"] == ["P02"], "待检应只剩 P02")
        _assert(confirmed["completed_assets"] == ["P05"], "已完成应包含 P05")
        _assert(_approx(confirmed["walked_distance_m"], 30.0), "确认不应改变已走距离")
        _assert(_approx(confirmed["remaining_distance_m"], 40.0), "剩余应保持 40 m")
        _assert(
            confirmed["remaining_plan"]["visit_order"] == ["P02"],
            "确认后剩余顺序不符",
        )
        return "走到 N05 已走 30 m；确认 P05 后待检 P02，剩余 40 m"

    def test_replan_close_open_e04() -> str:
        mission = _walk(_c1_mission(mission_dir), 2, mission_dir)
        mission = confirm_asset(mission, "P05", output_dir=str(mission_dir))
        closed = replan_mission(
            mission, closed_edges=["E04"], output_dir=str(mission_dir)
        )
        _assert(_approx(closed["walked_distance_m"], 30.0), "关 E04 后已走距离被重置")
        _assert(_approx(closed["remaining_distance_m"], 80.0), "关 E04 后剩余不是 80")
        _assert(_approx(closed["expected_total_distance_m"], 110.0), "预计总里程不是 110")
        _assert(closed["closed_edges"] == ["E04"], "关闭通道未保存")
        _assert(closed["current_node"] == "N05", "重规划改变了当前位置")
        _assert("E04" not in closed["remaining_plan"]["route_nodes"], "路线未避开 E04")

        opened = replan_mission(
            closed, closed_edges=[], output_dir=str(mission_dir)
        )
        _assert(_approx(opened["walked_distance_m"], 30.0), "重开 E04 后已走距离被重置")
        _assert(_approx(opened["remaining_distance_m"], 40.0), "重开 E04 后剩余不是 40")
        _assert(_approx(opened["expected_total_distance_m"], 70.0), "重开后总里程不是 70")
        _assert(opened["closed_edges"] == [], "重开 E04 后关闭列表未清空")
        return "N05 关 E04：已走 30、剩余 80、合计 110；重开 E04：剩余 40"

    def test_blocked_vs_completed() -> str:
        mission = _walk(_c1_mission(mission_dir), 2, mission_dir)
        mission = confirm_asset(mission, "P05", output_dir=str(mission_dir))
        blocked = replan_mission(
            mission, closed_edges=["E09"], output_dir=str(mission_dir)
        )
        _assert(blocked["status"] == STATUS_BLOCKED, f"status={blocked['status']}")
        _assert(blocked["pending_assets"] == ["P02"], "不可达目标从待检中消失")
        _assert(blocked["unreachable_assets"] == ["P02"], "不可达设备记录不符")
        _assert(_approx(blocked["walked_distance_m"], 30.0), "受阻时已走距离变了")
        _assert(_approx(blocked["remaining_distance_m"], 0.0), "受阻时剩余可行距离不是 0")
        _assert(blocked["expected_total_distance_m"] is None, "受阻时不应给预计总里程")
        _assert(blocked["status"] != STATUS_COMPLETED, "受阻被误报为完成")

        reopened = replan_mission(
            blocked, closed_edges=[], output_dir=str(mission_dir)
        )
        _assert(reopened["status"] == STATUS_ACTIVE, "恢复 E09 后未回到进行中")
        _assert(_approx(reopened["remaining_distance_m"], 40.0), "恢复 E09 后剩余不是 40")
        walked = _walk(reopened, 3, mission_dir)
        _assert(walked["current_node"] == "N02", f"当前位置 {walked['current_node']}")
        _assert(_approx(walked["walked_distance_m"], 70.0), "到 N02 累计不是 70")
        _assert(walked["status"] == STATUS_ACTIVE, "未确认前不应自动完成")
        final = confirm_asset(walked, "P02", output_dir=str(mission_dir))
        _assert(final["status"] == STATUS_COMPLETED, "全部确认后未完成")
        _assert(final["pending_assets"] == [], "完成后仍有待检设备")
        _assert(final["completed_assets"] == ["P05", "P02"], "完成设备记录不符")
        _assert(_approx(final["walked_distance_m"], 70.0), "完成时已走不是 70")
        _assert(_approx(final["expected_total_distance_m"], 70.0), "完成时总里程不是 70")
        (out / "C1_completed_export.json").write_text(
            mission_to_json(final), encoding="utf-8"
        )
        return "E09 受阻时未完成、剩余 0；恢复后走完并确认 P02 才完成"

    def test_invalid_confirm_and_step() -> str:
        mission = _c1_mission(mission_dir)
        before = copy.deepcopy(mission)
        try:
            confirm_asset(mission, "P05", output_dir=str(mission_dir))
        except MissionError as exc:
            _assert("未到达设备节点" in str(exc), f"错误信息不明确：{exc}")
        else:
            raise AssertionError("在 S 确认 P05 竟然成功")

        walked = _walk(mission, 2, mission_dir)
        done = confirm_asset(walked, "P05", output_dir=str(mission_dir))
        walked_before = done["walked_distance_m"]
        try:
            confirm_asset(done, "P05", output_dir=str(mission_dir))
        except MissionError as exc:
            _assert("重复" in str(exc) or "已确认" in str(exc), f"错误信息不明确：{exc}")
        else:
            raise AssertionError("重复确认 P05 竟然成功")
        _assert(_approx(done["walked_distance_m"], walked_before), "重复确认改变了里程")
        _assert(done["completed_assets"] == ["P05"], "重复确认改变了完成列表")
        _assert(_approx(before["walked_distance_m"], 0.0), "原任务对象被原地修改")
        return "未到节点拒绝确认；重复确认不重复统计；原任务对象未被修改"

    def test_step_closed_and_completed() -> str:
        mission = _c1_mission(mission_dir)
        # 人为构造“路线还没更新但封路已生效”的不一致状态，验证 step 自己会拦截。
        inconsistent = copy.deepcopy(mission)
        inconsistent["closed_edges"] = ["E01"]
        closed_before = inconsistent["walked_distance_m"]
        try:
            step_forward(inconsistent)
        except MissionError as exc:
            _assert("E01" in str(exc) or "关闭" in str(exc), f"错误信息不明确：{exc}")
        else:
            raise AssertionError("关闭 E01 后仍从 S 走到 J01")
        _assert(
            _approx(inconsistent["walked_distance_m"], closed_before),
            "失败的前进修改了里程",
        )

        finished = _walk(mission, 2, mission_dir)
        finished = confirm_asset(finished, "P05", output_dir=str(mission_dir))
        finished = replan_mission(finished, closed_edges=[], output_dir=str(mission_dir))
        finished = _walk(finished, 3, mission_dir)
        finished = confirm_asset(finished, "P02", output_dir=str(mission_dir))
        try:
            step_forward(finished)
        except MissionError as exc:
            _assert("已结束" in str(exc) or "完成" in str(exc), f"错误信息不明确：{exc}")
        else:
            raise AssertionError("完成任务后仍能继续走")
        return "关闭通道不能穿过；完成后不能再前进；失败操作不增加里程"

    def test_replan_no_distance_increase() -> str:
        mission = _walk(_c1_mission(mission_dir), 1, mission_dir)
        _assert(_approx(mission["walked_distance_m"], 20.0), "先走一步应为 20 m")
        events_before = len(mission["events"])
        s1 = replan_mission(mission, output_dir=str(mission_dir))
        s2 = replan_mission(s1, output_dir=str(mission_dir))
        s3 = replan_mission(s2, output_dir=str(mission_dir))
        _assert(_approx(s3["walked_distance_m"], 20.0), "重复重规划增加了已走距离")
        _assert(_approx(s3["remaining_distance_m"], 50.0), "重复重规划改变了剩余距离")
        _assert(
            len(s3["events"]) == events_before + 3,
            f"事件数不符：{len(s3['events'])}",
        )
        return "已走 20 m 后连续重规划 3 次，已走仍 20 m、剩余仍 50 m"

    def test_no_targets_and_invalid() -> str:
        detections = [
            {"asset_id": "P01", "status": "normal"},
            {
                "asset_id": "P04",
                "status": "invalid_input",
                "notes": ["模拟坏文件，需要补录"],
            },
        ]
        mission = create_mission(
            detections,
            source_kind="detection",
            source_label="测试无目标",
            is_mock=False,
            source_signature="sig-a",
            start_node="S",
            output_dir=str(mission_dir),
        )
        _assert(mission["status"] == STATUS_NO_TARGETS, f"status={mission['status']}")
        _assert(mission["pending_assets"] == [], "无目标时仍有待检设备")
        _assert(_approx(mission["remaining_distance_m"], 0.0), "无目标时剩余不是 0")
        _assert(
            mission["source"]["invalid_input_assets"] == ["P04"],
            "无效录音设备未保存",
        )
        _assert(
            "补录" in " ".join(mission["source"]["invalid_input_notes"]),
            "没有提示补录",
        )
        _assert(mission["status"] != STATUS_COMPLETED, "无目标不应显示巡检完成")
        return "无异常目标显示 no_targets；P04 invalid_input 保留补录提示"

    def test_snapshot_and_stale() -> str:
        detections = copy.deepcopy(DET_DEFAULT)
        mission = create_mission(
            detections,
            source_kind="detection",
            source_label="录音任务",
            is_mock=False,
            source_signature="sig-a",
            start_node="S",
            output_dir=str(mission_dir),
        )
        detections[1]["status"] = "normal"
        _assert(
            mission["source"]["detection_snapshot"][1]["status"]
            == "suspected_anomaly",
            "任务快照被外部列表改动",
        )
        _assert(mission_is_stale(mission, "sig-b"), "输入指纹变化未标记失效")
        _assert(not mission_is_stale(mission, "sig-a"), "相同指纹被误判失效")
        same_result = mission["source"]["result_signature"]
        changed_result = cd_state.detection_result_signature(
            [
                {"asset_id": "P01", "status": "normal"},
                {"asset_id": "P04", "status": "normal"},
            ]
        )
        _assert(
            mission_is_stale(mission, "sig-a", changed_result),
            "检测结果集变化未标记失效",
        )
        _assert(
            not mission_is_stale(mission, "sig-a", same_result),
            "相同检测结果被误判失效",
        )
        case = _c1_case()
        sim = _c1_mission(mission_dir)
        _assert(
            not mission_is_stale(sim, "anything", changed_result),
            "模拟案例任务不应被录音指纹/结果判失效",
        )
        _assert(sim["source"]["case_id"] == case["case_id"], "模拟案例来源丢失")
        return "任务快照独立；录音任务输入或结果变化失效；模拟任务不受影响"

    def test_export_json() -> str:
        mission = _walk(_c1_mission(mission_dir), 2, mission_dir)
        mission = confirm_asset(mission, "P05", output_dir=str(mission_dir))
        text = mission_to_json(mission)
        (out / "C1_mission_export.json").write_text(text, encoding="utf-8")
        _assert("NaN" not in text and "Infinity" not in text, "导出含非有限数值")
        data = json.loads(text)
        for key in (
            "patch_version",
            "mission_id",
            "source",
            "initial_targets",
            "current_node",
            "pending_assets",
            "completed_assets",
            "closed_edges",
            "walked_distance_m",
            "remaining_distance_m",
            "remaining_plan",
            "events",
            "completion_note",
        ):
            _assert(key in data, f"导出缺少字段 {key}")
        _assert(data["source"]["kind"] == "simulation_case", "导出丢失来源类型")
        _assert(data["source"]["case_id"] == "C1", "导出丢失案例编号")
        _assert(
            any(event["type"] == "step" for event in data["events"]),
            "导出缺少行走事件",
        )
        _assert(
            any(event["type"] == "confirm" for event in data["events"]),
            "导出缺少确认事件",
        )
        return "JSON 含来源、目标、位置、距离、封路、操作事件；无非有限数值"

    def test_two_sessions_independent() -> str:
        first = _c1_mission(mission_dir)
        second = _c1_mission(mission_dir)
        stepped = step_forward(first)
        _assert(stepped["walked_distance_m"] > 0, "第一个任务没有前进")
        _assert(_approx(second["walked_distance_m"], 0.0), "第二个任务被第一个影响")
        _assert(first["mission_id"] != second["mission_id"], "任务编号重复")
        return "两个任务对象独立，互不影响"

    def test_agent_replan_and_conflict() -> str:
        mission = _walk(_c1_mission(mission_dir), 2, mission_dir)
        mission = confirm_asset(mission, "P05", output_dir=str(mission_dir))
        det = case_detection_results(_c1_case())
        result = run_agent(
            "关闭E04后重新规划",
            mode="mock",
            context=_context(mission, det),
            planner="rule",
        )
        _assert(result.mission is not None, f"Agent 未返回任务：{result.answer}")
        _assert(result.mission["current_node"] == "N05", "Agent 改变了当前位置")
        _assert(_approx(result.mission["walked_distance_m"], 30.0), "Agent 重置了已走距离")
        _assert(_approx(result.mission["remaining_distance_m"], 80.0), "Agent 剩余不是 80")
        _assert(result.mission["closed_edges"] == ["E04"], "Agent 未使用 E04")

        original = copy.deepcopy(result.mission)
        conflict = run_agent(
            "从S重新规划",
            mode="mock",
            context=_context(result.mission, det),
            planner="rule",
        )
        _assert(conflict.answer.startswith("【失败】"), "冲突起点没有失败")
        _assert("起点冲突" in conflict.answer, f"没有明确报起点冲突：{conflict.answer}")
        _assert(conflict.mission is None, "冲突请求仍返回了新任务")
        _assert(original["current_node"] == "N05", "冲突请求改变了原任务")

        reopened = run_agent(
            "重新开放E04并重新规划",
            mode="mock",
            context=_context(result.mission, det),
            planner="rule",
        )
        _assert(reopened.mission is not None, "Agent 重开失败")
        _assert(_approx(reopened.mission["remaining_distance_m"], 40.0), "重开剩余不是 40")
        _assert(reopened.mission["closed_edges"] == [], "重开后仍有关闭通道")
        return "Agent 关 E04 后已走 30/剩余 80；从 S 冲突被拒；重开恢复 40"

    def test_agent_create_and_refuse_replace() -> str:
        det = case_detection_results(_c1_case())
        created = run_agent(
            "新建巡检任务",
            mode="mock",
            context=_context(None, det, signature="sig-c1"),
            planner="rule",
        )
        _assert(created.mission is not None, f"Agent 未创建任务：{created.answer}")
        _assert(created.mission["source"]["kind"] == "detection", "来源类型不对")
        _assert(created.mission["pending_assets"] == ["P02", "P05"], "目标不符")
        _assert(_approx(created.mission["remaining_distance_m"], 70.0), "剩余不是 70")
        _assert(
            created.mission["source"]["input_signature"] == "sig-c1",
            "创建时未保存输入指纹",
        )

        refused = run_agent(
            "新建巡检任务",
            mode="mock",
            context=_context(created.mission, det),
            planner="rule",
        )
        _assert(refused.answer.startswith("【失败】"), "活动任务替换没有失败")
        _assert("不能直接替换" in refused.answer, "没有说明要页面确认替换")
        _assert(refused.mission is None, "拒绝替换仍返回新任务")
        return "无任务时 Agent 可新建；有活动任务时拒绝直接替换"

    def test_simulated_llm_mission_end_to_end() -> str:
        # 本地伪造 LLM 输出，但后续真实经过 make_plan → 校验 → Agent 循环 →
        # plan_asset_inspection → mission.replan_mission，不调用任何外部 API。
        mission = _walk(_c1_mission(mission_dir), 2, mission_dir)
        mission = confirm_asset(mission, "P05", output_dir=str(mission_dir))
        context = _context(mission, case_detection_results(_c1_case()))
        fake_output = {
            "reason": "本地模拟：关闭 E04 后从 N05 重规划剩余目标",
            "steps": [
                {
                    "tool": "plan_asset_inspection",
                    "args": {
                        "mission_action": "replan",
                        "start_node": "N05",
                        "closed_edges": ["E04"],
                    },
                }
            ],
        }
        with patch(
            "agent.planner.llm.current_config",
            return_value=SimpleNamespace(
                configured=True, model="fake-model", missing=[]
            ),
        ), patch(
            "agent.planner.llm.chat_json",
            return_value=fake_output,
        ):
            result = run_agent(
                "关闭E04后重新规划",
                mode="mock",
                context=context,
                planner="llm",
            )
        _assert(result.planner == "llm", f"没有走模拟 LLM 路径：{result.planner}")
        _assert(result.mission is not None, f"模拟 LLM 没有返回任务：{result.answer}")
        _assert(result.mission["current_node"] == "N05", "模拟 LLM 改变当前位置")
        _assert(
            _approx(result.mission["walked_distance_m"], 30.0),
            "模拟 LLM 改变已走距离",
        )
        _assert(
            _approx(result.mission["remaining_distance_m"], 80.0),
            f"模拟 LLM 重规划剩余不是 80：{result.mission['remaining_distance_m']}",
        )
        _assert(result.mission["closed_edges"] == ["E04"], "模拟 LLM 未应用 E04")
        _assert(
            result.mission["pending_assets"] == ["P02"],
            "模拟 LLM 改动了待检目标",
        )
        return "本地模拟 LLM 经 Agent/工具/任务全链路：N05、已走 30、关 E04 后剩余 80（未调用 API）"

    def test_simulated_llm_failure() -> str:
        mission = _c1_mission(mission_dir)
        with patch(
            "agent.planner.llm.current_config",
            return_value=SimpleNamespace(
                configured=True, model="fake-model", missing=[]
            ),
        ), patch(
            "agent.planner.llm.chat_json",
            side_effect=llm.LLMError("模拟 LLM 超时"),
        ):
            result = run_agent(
                "重新规划",
                mode="mock",
                context=_context(mission, case_detection_results(_c1_case())),
                planner="llm",
            )
        _assert(result.answer.startswith("【失败】"), "模拟 LLM 失败没有明确报错")
        _assert("模拟 LLM 超时" in result.answer, "没有带出模拟失败原因")
        _assert(result.mission is None, "模拟 LLM 失败仍改动了任务")
        return "模拟 LLM 超时按失败返回，未改动任务；这不是真实 LLM 测试"

    def test_a_module_failure_stub() -> str:
        with patch(
            "agent.tools.audio_analyzer",
            side_effect=ModuleUnavailable("模拟 A 模块缺失"),
        ):
            result = detect_assets_audio(
                asset_ids=["P01"], files=SAMPLE_ITEMS, mode="real"
            )
        _assert(not result.ok, "A 模块缺失被当成成功")
        _assert("模拟 A 模块缺失" in result.message, "没有带出 A 失败原因")
        return "A 模块异常用临时替身测试，返回失败，未改动模型文件"

    def test_page_c1_full_flow() -> str:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        _assert(not at.exception, f"初始页面异常：{at.exception}")
        source = at.selectbox(key="mission_source_select")
        c1_option = next(option for option in source.options if option.startswith("C1"))
        source.set_value(c1_option)
        at.run()
        at.button(key="mission_create").click().run()
        mission = at.session_state["mission"] or {}
        _assert(mission.get("status") == STATUS_ACTIVE, "C1 页面任务未创建")
        _assert(_approx(mission.get("remaining_distance_m"), 70.0), "初始剩余不是 70")
        _assert(
            any(button.key == "mission_download" for button in at.download_button),
            "页面缺少任务 JSON 导出按钮",
        )

        at.button(key="mission_step").click().run()
        at.button(key="mission_step").click().run()
        mission = at.session_state["mission"] or {}
        _assert(_approx(mission.get("walked_distance_m"), 30.0), "页面走到 N05 不是 30")
        _assert(
            at.button(key="mission_step").disabled,
            "页面到达 P05 未确认时仍允许继续前进",
        )
        at.button(key="mission_confirm").click().run()
        mission = at.session_state["mission"] or {}
        _assert(mission.get("pending_assets") == ["P02"], "页面确认 P05 后待检不对")
        _assert(_approx(mission.get("remaining_distance_m"), 40.0), "页面剩余不是 40")
        _assert(
            not at.button(key="mission_step").disabled,
            "确认 P05 后仍不能继续前进",
        )

        at.multiselect(key="mission_closed_edges").set_value(["E04"]).run()
        at.button(key="mission_replan").click().run()
        mission = at.session_state["mission"] or {}
        _assert(_approx(mission.get("walked_distance_m"), 30.0), "页面关 E04 重置了已走")
        _assert(_approx(mission.get("remaining_distance_m"), 80.0), "页面关 E04 剩余不是 80")
        _assert(_approx(mission.get("expected_total_distance_m"), 110.0), "页面总里程不是 110")

        at.multiselect(key="mission_closed_edges").set_value([]).run()
        at.button(key="mission_replan").click().run()
        mission = at.session_state["mission"] or {}
        _assert(_approx(mission.get("remaining_distance_m"), 40.0), "页面重开 E04 不是 40")

        at.multiselect(key="mission_closed_edges").set_value(["E09"]).run()
        at.button(key="mission_replan").click().run()
        mission = at.session_state["mission"] or {}
        _assert(mission.get("status") == STATUS_BLOCKED, "页面关 E09 未显示受阻")
        _assert(mission.get("status") != STATUS_COMPLETED, "页面把受阻显示成完成")
        _assert(_approx(mission.get("remaining_distance_m"), 0.0), "受阻剩余不是 0")
        _assert(mission.get("expected_total_distance_m") is None, "受阻仍给总里程")
        _assert(
            at.button(key="mission_step").disabled,
            "受阻时仍允许前进一步",
        )

        at.multiselect(key="mission_closed_edges").set_value([]).run()
        at.button(key="mission_replan").click().run()
        for _ in range(3):
            at.button(key="mission_step").click().run()
        mission = at.session_state["mission"] or {}
        _assert(mission.get("current_node") == "N02", "页面未走到 N02")
        _assert(_approx(mission.get("walked_distance_m"), 70.0), "页面累计不是 70")
        _assert(mission.get("status") == STATUS_ACTIVE, "未确认 P02 就完成了")
        at.button(key="mission_confirm").click().run()
        mission = at.session_state["mission"] or {}
        _assert(mission.get("status") == STATUS_COMPLETED, "页面确认 P02 后未完成")
        _assert(mission.get("pending_assets") == [], "页面完成后仍有待检")
        success = "；".join(item.value for item in at.success)
        _assert("巡检已完成" in success, "页面没有显示巡检完成")
        return "页面 C1 全流程：0→30→80→40→受阻 0→恢复→70→完成"

    def test_page_agent_mission() -> str:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        source = at.selectbox(key="mission_source_select")
        c1_option = next(option for option in source.options if option.startswith("C1"))
        source.set_value(c1_option)
        at.run()
        at.button(key="mission_create").click().run()
        at.button(key="mission_step").click().run()
        at.button(key="mission_step").click().run()
        at.button(key="mission_confirm").click().run()

        def ask(text: str) -> dict:
            at.text_input(key="agent_question").set_value(text)
            at.run()
            at.button(key="run_agent").click().run()
            return at.session_state["mission"] or {}

        mission = ask("关闭E04后重新规划")
        _assert(_approx(mission.get("walked_distance_m"), 30.0), "Agent 重置了已走距离")
        _assert(_approx(mission.get("remaining_distance_m"), 80.0), "Agent 剩余不是 80")
        _assert(mission.get("current_node") == "N05", "Agent 改变了当前位置")
        _assert(mission.get("closed_edges") == ["E04"], "Agent 未关闭 E04")
        _assert(
            at.session_state["mission_closed_edges"] == ["E04"],
            "页面任务封路控件没有同步 E04",
        )

        unchanged = copy.deepcopy(mission)
        conflict = ask("从S重新规划")
        _assert(conflict.get("current_node") == "N05", "冲突请求把人瞬移回 S")
        _assert(
            _approx(conflict.get("walked_distance_m"), 30.0),
            "冲突请求改动了已走距离",
        )
        _assert(
            "起点冲突" in at.session_state["mission_error"],
            "页面没有显示起点冲突",
        )
        _assert(
            conflict.get("remaining_distance_m") == unchanged.get("remaining_distance_m"),
            "冲突请求改变了剩余路线",
        )

        reopened = ask("重新开放E04并重新规划")
        _assert(_approx(reopened.get("remaining_distance_m"), 40.0), "重开 E04 不是 40")
        _assert(reopened.get("closed_edges") == [], "重开后任务仍有关闭通道")
        _assert(
            at.session_state["mission_closed_edges"] == [],
            "页面任务封路控件没有同步重开",
        )

        before = copy.deepcopy(reopened)
        refused = ask("新建巡检任务")
        _assert(
            refused.get("mission_id") == before.get("mission_id"),
            "Agent 直接替换了活动任务",
        )
        _assert(
            "不能直接替换" in at.session_state["mission_error"],
            "页面没有说明不能直接替换任务",
        )
        return "页面 Agent 关/开 E04 同步；从 S 冲突被拒；新建被拒且原任务保留"

    def test_page_refresh_and_replan_no_double_count() -> str:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        source = at.selectbox(key="mission_source_select")
        c1_option = next(option for option in source.options if option.startswith("C1"))
        source.set_value(c1_option)
        at.run()
        at.button(key="mission_create").click().run()
        at.button(key="mission_step").click().run()
        mission = at.session_state["mission"] or {}
        walked = mission.get("walked_distance_m")
        events = len(mission.get("events") or [])
        _assert(_approx(walked, 20.0), "第一步不是 20 m")

        # 纯刷新两次，不点任何按钮。
        at.run()
        at.run()
        mission = at.session_state["mission"] or {}
        _assert(_approx(mission.get("walked_distance_m"), walked), "刷新增加了里程")
        _assert(len(mission.get("events") or []) == events, "刷新增加了事件")

        at.button(key="mission_replan_same").click().run()
        at.button(key="mission_replan_same").click().run()
        mission = at.session_state["mission"] or {}
        _assert(_approx(mission.get("walked_distance_m"), walked), "重规划增加了里程")
        _assert(_approx(mission.get("remaining_distance_m"), 50.0), "重规划后剩余不是 50")
        return "刷新 2 次、按当前封路重算 2 次，已走仍 20 m、事件不因刷新重复"

    def test_page_stale_and_sessions() -> str:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        at.button(key="run_detect").click().run()
        at.button(key="mission_create").click().run()
        mission = at.session_state["mission"] or {}
        _assert(mission.get("source", {}).get("kind") == "detection", "录音任务未创建")
        source_options = at.radio(key="audio_source").options
        at.radio(key="audio_source").set_value(source_options[2]).run()
        _assert(at.session_state["mission"] is not None, "输入变化后任务被静默删除")
        errors = "；".join(item.value for item in at.error)
        _assert("失效" in errors, "输入变化后没有提示任务失效")
        _assert(
            not any(button.key == "mission_step" for button in at.button),
            "失效任务仍显示前进一步按钮",
        )

        second = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        _assert(second.session_state["mission"] is None, "新会话串到了旧任务")
        return "输入变化保留任务并标记失效；新页面会话没有旧任务"

    def test_page_isolation_from_static_and_case() -> str:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        at.button(key="run_detect").click().run()
        at.button(key="run_plan").click().run()
        static_plan = dict(at.session_state["plan_result"] or {})
        at.button(key="run_cd_case").click().run()
        case_result = copy.deepcopy(at.session_state["cd_case_result"])

        source = at.selectbox(key="mission_source_select")
        c1_option = next(option for option in source.options if option.startswith("C1"))
        source.set_value(c1_option)
        at.run()
        at.button(key="mission_create").click().run()
        first_id = (at.session_state["mission"] or {}).get("mission_id")
        at.button(key="mission_step").click().run()
        _assert(
            (at.session_state["plan_result"] or {}).get("total_distance_m")
            == static_plan.get("total_distance_m"),
            "任务执行覆盖了第一轮静态规划",
        )
        _assert(
            (at.session_state["cd_case_result"] or {}).get("case", {}).get("case_id")
            == case_result.get("case", {}).get("case_id"),
            "任务执行覆盖了模拟案例对照",
        )

        # 页面新建 vs 重规划：有活动任务时，替换必须先勾选确认。
        _assert(
            at.button(key="mission_create").disabled,
            "有活动任务时新建按钮没有被替换确认拦住",
        )
        replace_key = f"mission_replace_confirm_{first_id}"
        at.checkbox(key=replace_key).set_value(True)
        at.run()
        _assert(
            not at.button(key="mission_create").disabled,
            "勾选替换确认后新建按钮仍不可用",
        )
        at.button(key="mission_create").click().run()
        second_id = (at.session_state["mission"] or {}).get("mission_id")
        _assert(second_id and second_id != first_id, "页面替换任务没有生成新任务")
        _assert(
            (at.session_state["plan_result"] or {}).get("total_distance_m")
            == static_plan.get("total_distance_m"),
            "替换任务覆盖了静态规划",
        )
        return "任务执行不覆盖静态规划/案例；替换任务需勾选确认并生成新任务编号"

    checks = [
        ("运行环境与依赖清单", test_environment),
        ("C1 创建任务", test_c1_create),
        ("C2 任务等级约束", test_c2_criticality_constraint),
        ("走到 N05 并确认 P05", test_walk_and_confirm),
        ("关/开 E04 重规划", test_replan_close_open_e04),
        ("受阻与完成区分", test_blocked_vs_completed),
        ("确认与坐标校验", test_invalid_confirm_and_step),
        ("关闭通道与完成后禁止前进", test_step_closed_and_completed),
        ("重复重规划不增加里程", test_replan_no_distance_increase),
        ("无目标与无效录音", test_no_targets_and_invalid),
        ("输入快照与任务失效", test_snapshot_and_stale),
        ("任务 JSON 导出", test_export_json),
        ("不同任务对象隔离", test_two_sessions_independent),
        ("Agent 重规划/起点冲突", test_agent_replan_and_conflict),
        ("Agent 新建与拒绝替换", test_agent_create_and_refuse_replace),
        ("模拟 LLM 输出全链路", test_simulated_llm_mission_end_to_end),
        ("模拟 LLM 失败路径", test_simulated_llm_failure),
        ("A 模块异常替身", test_a_module_failure_stub),
        ("页面 C1 全流程", test_page_c1_full_flow),
        ("页面 Agent 任务重规划", test_page_agent_mission),
        ("页面刷新/重规划不重复记账", test_page_refresh_and_replan_no_double_count),
        ("页面失效与新会话隔离", test_page_stale_and_sessions),
        ("页面与静态规划/案例隔离", test_page_isolation_from_static_and_case),
    ]

    for label, func in checks:
        checker.check(label, func)

    passed = sum(1 for result in checker.results if result["ok"])
    failed = len(checker.results) - passed
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "environment": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "passed": passed,
        "failed": failed,
        "total": len(checker.results),
        "results": checker.results,
        "output_dir": out.relative_to(ROOT).as_posix(),
        "note": (
            "A 异常和 LLM 失败使用本地临时替身；本脚本没有调用付费 LLM API，"
            "也没有修改 A 的模型文件。"
        ),
    }
    result_path = out / "check_result.json"
    result_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print()
    print(f"总计：{passed} 通过 / {failed} 失败 / {len(checker.results)} 项")
    print(f"记录：{result_path.relative_to(ROOT).as_posix()}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
