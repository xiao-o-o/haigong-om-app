# -*- coding: utf-8 -*-
"""C/D 联合优化检查入口。

运行：
    .venv\\Scripts\\python.exe check_cd.py

覆盖：
- 默认样例检测分数与 10 米路线、关 E11 partial、重新开放恢复；
- 起点变化后旧路线失效、检测结果保留；
- 同名文件内容变化 / 设备映射 / 运行模式 / 来源变化的输入指纹；
- 规则版“检测P01”只检测 P01，“检测P99”明确报未知设备；
- 页面与 Agent 共用起点/封路状态，关闭和重新开放都能重规划；
- 三个模拟案例、固定顺序基线、距离差与改善比例；
- 报告记录实际起点、关闭通道和完整路线；
- Streamlit 页面上的旧结果清理与状态隔离。

本脚本只记录实际跑过的结果，不用“应该通过”代替验证。
"""
from __future__ import annotations

import csv
import json
import platform
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.loop import run_agent  # noqa: E402
from agent.planner import _validate_plan, rule_plan  # noqa: E402
from agent.tools import detect_assets_audio, plan_asset_inspection  # noqa: E402
from core import cd_state  # noqa: E402
from core.cd_cases import case_detection_results, load_cases  # noqa: E402
from core.local_modules import list_assets  # noqa: E402
from core.report import build_markdown_report  # noqa: E402
from spatial_module.plan import (  # noqa: E402
    compare_inspection_plans,
    plan_inspection,
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
DET_P04 = [{"asset_id": "P04", "status": "suspected_anomaly"}]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _approx(actual: float | None, expected: float, tol: float = 1e-6) -> bool:
    return actual is not None and abs(float(actual) - float(expected)) <= tol


def _png_ok(path_value: str | None) -> bool:
    if not path_value:
        return False
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    return path.is_file() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


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
        except Exception as exc:  # noqa: BLE001 - 检查脚本要保留原始失败
            elapsed = (time.perf_counter() - started) * 1000
            detail = f"{type(exc).__name__}: {exc}"
            self.results.append(
                {
                    "label": label,
                    "ok": False,
                    "detail": detail,
                    "elapsed_ms": round(elapsed, 1),
                    "traceback": traceback.format_exc(),
                }
            )
            print(f"[失败] {label}：{detail}")
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


def _check_case(case: dict, output_dir: Path) -> tuple[dict, str]:
    result = compare_inspection_plans(
        case_detection_results(case),
        start_node=case["start_node"],
        closed_edges=case["closed_edges"],
        output_dir=str(output_dir),
    )
    expected = case.get("expected") or {}
    optimized = result.get("optimized") or {}
    baseline = result.get("baseline") or {}

    _assert(result.get("status") == expected.get("status"), "状态不符")
    _assert(
        list(optimized.get("visit_order") or [])
        == list(expected.get("optimized_visit_order") or []),
        f"优化顺序不符：{optimized.get('visit_order')}",
    )
    _assert(
        _approx(
            optimized.get("total_distance_m"),
            expected.get("optimized_total_distance_m", 0),
        ),
        f"优化距离不符：{optimized.get('total_distance_m')}",
    )
    _assert(
        list(baseline.get("visit_order") or [])
        == list(expected.get("baseline_visit_order") or []),
        f"固定顺序不符：{baseline.get('visit_order')}",
    )
    _assert(
        _approx(
            baseline.get("total_distance_m"),
            expected.get("baseline_total_distance_m", 0),
        ),
        f"固定距离不符：{baseline.get('total_distance_m')}",
    )
    _assert(
        sorted(result.get("unreachable_asset_ids") or [])
        == sorted(expected.get("unreachable_asset_ids") or []),
        f"不可达设备不符：{result.get('unreachable_asset_ids')}",
    )

    map_files = result.get("map_files") or {}
    for key in ("optimized_route", "fixed_route", "comparison_chart"):
        _assert(_png_ok(map_files.get(key)), f"{key} 不是有效 PNG")

    detail = (
        f"{case['case_id']} status={result.get('status')}；"
        f"优化 {'→'.join(optimized.get('visit_order') or []) or '无'} "
        f"{optimized.get('total_distance_m')} m；"
        f"固定 {'→'.join(baseline.get('visit_order') or []) or '无'} "
        f"{baseline.get('total_distance_m')} m"
    )
    return result, detail


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / "runs" / "check_cd" / stamp
    out.mkdir(parents=True, exist_ok=False)
    checker = Checker()
    a_results: list[dict] = []

    def test_environment() -> str:
        import matplotlib
        import networkx
        import numpy
        import pandas
        import sklearn
        import soundfile
        import streamlit

        return (
            f"Python {platform.python_version()}；sklearn {sklearn.__version__}；"
            f"numpy {numpy.__version__}；pandas {pandas.__version__}；"
            f"networkx {networkx.__version__}；matplotlib {matplotlib.__version__}；"
            f"soundfile {soundfile.__version__}；streamlit {streamlit.__version__}"
        )

    def test_default_detection() -> str:
        nonlocal a_results
        tr = detect_assets_audio(
            asset_ids=["P01", "P04"], files=SAMPLE_ITEMS, mode="real"
        )
        _assert(tr.ok, tr.message)
        a_results = list(tr.data["results"])
        by_id = {row["asset_id"]: row for row in a_results}
        _assert(
            by_id["P01"]["status"] == "normal"
            and _approx(by_id["P01"]["score"], 0.4557, 1e-4),
            f"P01 结果不符：{by_id['P01']}",
        )
        _assert(
            by_id["P04"]["status"] == "suspected_anomaly"
            and _approx(by_id["P04"]["score"], 0.6187, 1e-4),
            f"P04 结果不符：{by_id['P04']}",
        )
        _assert(_approx(by_id["P01"]["threshold"], 0.5455, 1e-4), "阈值不符")
        return "P01 normal 0.4557；P04 suspected_anomaly 0.6187；阈值 0.5455"

    def test_default_plan() -> str:
        result = plan_inspection(DET_DEFAULT, output_dir=str(out / "plans" / "default"))
        _assert(result["status"] == "ok", f"status={result['status']}")
        _assert(result["visit_order"] == ["P04"], f"order={result['visit_order']}")
        _assert(result["route_nodes"] == ["S", "N04"], f"route={result['route_nodes']}")
        _assert(_approx(result["total_distance_m"], 10.0), "距离不是 10 m")
        _assert(result.get("start_node") == "S", "未返回实际起点")
        _assert(_png_ok(result.get("map_file")), "路线图不是有效 PNG")
        _assert(
            _approx((result.get("baseline") or {}).get("total_distance_m"), 10.0),
            "基线距离不是 10 m",
        )
        return "S→N04，10 m；基线与优化一致"

    def test_close_e11() -> str:
        result = plan_inspection(
            DET_DEFAULT,
            closed_edges=["E11"],
            output_dir=str(out / "plans" / "closed_e11"),
        )
        _assert(result["status"] == "partial", f"status={result['status']}")
        _assert(result["visit_order"] == [], f"order={result['visit_order']}")
        _assert(result["unreachable_asset_ids"] == ["P04"], "P04 未列入不可达")
        _assert(_approx(result["total_distance_m"], 0.0), "距离不是 0")
        _assert(result["route_nodes"] == ["S"], f"route={result['route_nodes']}")
        _assert(result.get("closed_edges") == ["E11"], "未返回实际关闭通道")
        return "status=partial；P04 不可达；空访问顺序；0 m"

    def test_reopen_e11() -> str:
        result = plan_inspection(
            DET_DEFAULT,
            closed_edges=[],
            output_dir=str(out / "plans" / "reopened"),
        )
        _assert(result["status"] == "ok", f"status={result['status']}")
        _assert(_approx(result["total_distance_m"], 10.0), "重新开放后未恢复 10 m")
        return "重新开放 E11 后 status=ok，10 m"

    def test_start_change() -> str:
        result = plan_inspection(
            DET_P04,
            start_node="J01",
            output_dir=str(out / "plans" / "start_j01"),
        )
        _assert(result["status"] == "ok", f"status={result['status']}")
        _assert(result["start_node"] == "J01", "未使用 J01 起点")
        _assert(_approx(result["total_distance_m"], 30.0), "J01 起点距离应为 30 m")
        _assert(result["route_nodes"][0] == "J01", "路线未从 J01 出发")
        return "起点 J01，30 m"

    def test_invalid_not_normal() -> str:
        detections = [
            {"asset_id": "P01", "status": "normal"},
            {
                "asset_id": "P04",
                "status": "invalid_input",
                "notes": ["测试用坏文件，需要补录"],
            },
        ]
        result = plan_inspection(detections, output_dir=str(out / "plans" / "invalid_note"))
        _assert(result["status"] == "no_targets", f"status={result['status']}")
        _assert(result["visit_order"] == [], "invalid_input 不应成为目标")
        notes = "；".join(result.get("notes") or [])
        _assert("补录" in notes, "没有提示补录")
        _assert("invalid_input" in notes, "没有保留 invalid_input 说明")
        return "invalid_input 提示补录，不加入目标"

    def test_all_unreachable() -> str:
        detections = [
            {"asset_id": "P02", "status": "suspected_anomaly"},
            {"asset_id": "P04", "status": "suspected_anomaly"},
        ]
        result = plan_inspection(
            detections,
            closed_edges=["E09", "E11"],
            output_dir=str(out / "plans" / "all_unreachable"),
        )
        _assert(result["status"] == "partial", f"status={result['status']}")
        _assert(
            sorted(result["unreachable_asset_ids"]) == ["P02", "P04"],
            "未列出全部不可达目标",
        )
        _assert(result["visit_order"] == [], "全部不可达时不应有访问顺序")
        _assert(_approx(result["total_distance_m"], 0.0), "距离应为 0")
        return "全部异常目标不可达时仍为 partial，空顺序，0 m"

    def test_plan_tool_invalid() -> str:
        bad = plan_asset_inspection(
            DET_P04, start_node="XX", input_is_mock=False
        )
        _assert(not bad.ok, "D 的 invalid_input 被工具当成成功")
        _assert("未知起点" in bad.message, f"错误信息不明确：{bad.message}")
        good = plan_asset_inspection(DET_P04, start_node="S", input_is_mock=False)
        _assert(good.ok and good.data["status"] == "ok", "正常规划未成功")
        return "非法起点按失败返回；正常规划仍成功"

    def test_input_signature() -> str:
        base = {
            "asset_id": "P01",
            "filename": "same_name.wav",
            "content_sha256": cd_state.sha256_bytes(b"content-A"),
        }
        changed = dict(base, content_sha256=cd_state.sha256_bytes(b"content-B"))
        sig_a = cd_state.build_detection_signature("mock", "上传文件", [base])
        sig_a2 = cd_state.build_detection_signature("mock", "上传文件", [base])
        sig_content = cd_state.build_detection_signature("mock", "上传文件", [changed])
        sig_asset = cd_state.build_detection_signature(
            "mock", "上传文件", [dict(base, asset_id="P02")]
        )
        sig_mode = cd_state.build_detection_signature("real", "上传文件", [base])
        sig_source = cd_state.build_detection_signature("mock", "内置演示", [base])
        _assert(sig_a == sig_a2, "相同输入指纹不稳定")
        _assert(sig_a != sig_content, "同名文件内容变化未被识别")
        _assert(sig_a != sig_asset, "设备映射变化未被识别")
        _assert(sig_a != sig_mode, "运行模式变化未被识别")
        _assert(sig_a != sig_source, "音频来源变化未被识别")
        ctx = cd_state.plan_context("S", ["E11", "E04"])
        _assert(cd_state.same_plan_context(ctx, "S", ["E04", "E11"]), "规划上下文比较失败")
        _assert(
            not cd_state.same_plan_context(ctx, "J01", ["E04", "E11"]),
            "起点变化未被识别",
        )
        return "同名不同内容/设备映射/模式/来源均能区分；规划上下文可比较"

    def test_rule_parser() -> str:
        ctx = {
            "files": SAMPLE_ITEMS,
            "detections": [],
            "start_node": "S",
            "closed_edges": [],
        }
        only_p01 = rule_plan("检测P01", ctx)
        _assert(
            only_p01 == [("detect_assets_audio", {"asset_ids": ["P01"]})],
            f"检测P01 解析错误：{only_p01}",
        )
        both = rule_plan("检测P01和P04", ctx)
        _assert(
            both[0][1]["asset_ids"] == ["P01", "P04"],
            f"中文紧挨编号解析错误：{both}",
        )
        unknown = rule_plan("检测P99", ctx)
        _assert(
            unknown == [("detect_assets_audio", {"asset_ids": ["P99"]})],
            f"P99 解析错误：{unknown}",
        )
        close_ctx = dict(ctx, start_node="J01", detections=DET_P04)
        close_steps = rule_plan("关闭E04后重新规划", close_ctx)
        _assert(
            close_steps[0][1]
            == {
                "closed_edges": ["E04"],
                "start_node": "J01",
                "mission_action": "auto",
            },
            f"关闭通道/起点解析错误：{close_steps}",
        )
        open_ctx = dict(ctx, closed_edges=["E11"], detections=DET_P04)
        open_steps = rule_plan("重新开放E11并重新规划", open_ctx)
        _assert(
            open_steps[0][1]
            == {
                "closed_edges": [],
                "start_node": "S",
                "mission_action": "auto",
            },
            f"重新开放解析错误：{open_steps}",
        )
        from_ctx = dict(ctx, detections=DET_P04)
        from_steps = rule_plan("从J01出发重新规划", from_ctx)
        _assert(
            from_steps[0][1]
            == {
                "closed_edges": [],
                "start_node": "J01",
                "mission_action": "auto",
            },
            f"中文起点解析错误：{from_steps}",
        )
        steps, error = _validate_plan(
            {"steps": [{"tool": "plan_asset_inspection", "args": {}}]},
            close_ctx,
            max_steps=6,
        )
        _assert(not error, f"LLM 校验报错：{error}")
        _assert(
            steps[0][1]
            == {
                "closed_edges": [],
                "start_node": "J01",
                "mission_action": "auto",
            },
            f"缺省参数没有沿用页面状态：{steps}",
        )
        return "检测P01/P99、关闭/开放、中文紧挨编号均正确"

    def test_agent_detection_scope() -> str:
        result = run_agent(
            "检测P01",
            mode="mock",
            context={"files": SAMPLE_ITEMS, "detections": []},
            planner="rule",
        )
        _assert(result.detections_attempted, "未标记检测尝试")
        _assert(
            [row["asset_id"] for row in result.detections] == ["P01"],
            f"检测范围错误：{[row['asset_id'] for row in result.detections]}",
        )
        _assert(
            result.steps[0].args == {"asset_ids": ["P01"]},
            f"工具参数错误：{result.steps[0].args}",
        )
        return "规则版只检测 P01，没有带上 P04"

    def test_agent_unknown_clears() -> str:
        result = run_agent(
            "检测P99",
            mode="mock",
            context={
                "files": SAMPLE_ITEMS,
                "detections": [{"asset_id": "P01", "status": "normal"}],
            },
            planner="rule",
        )
        _assert(result.answer.startswith("【失败】"), "未知设备没有明确失败")
        _assert(result.detections_attempted, "未标记检测尝试")
        _assert(result.detections == [], "失败任务仍带出旧检测")
        _assert("P99" in result.answer and "未知设备" in result.answer, "未报出 P99")
        return "P99 明确报未知设备，失败结果不带旧检测"

    def test_agent_plan_state() -> str:
        close = run_agent(
            "关闭E11后重新规划",
            mode="mock",
            context={
                "files": SAMPLE_ITEMS,
                "detections": DET_P04,
                "start_node": "S",
                "closed_edges": [],
            },
            planner="rule",
        )
        _assert(close.plan is not None, "关闭通道后没有规划")
        _assert(close.plan["status"] == "partial", f"status={close.plan['status']}")
        _assert(close.plan_closed_edges == ["E11"], "Agent 未使用 E11")
        _assert(close.plan_start_node == "S", "Agent 起点不一致")

        reopen = run_agent(
            "重新开放E11并重新规划",
            mode="mock",
            context={
                "files": SAMPLE_ITEMS,
                "detections": DET_P04,
                "start_node": "S",
                "closed_edges": ["E11"],
            },
            planner="rule",
        )
        _assert(reopen.plan is not None, "重新开放后没有规划")
        _assert(reopen.plan["status"] == "ok", f"status={reopen.plan['status']}")
        _assert(_approx(reopen.plan["total_distance_m"], 10.0), "未恢复 10 m")
        _assert(reopen.plan_closed_edges == [], "重新开放后仍带着 E11")

        start = run_agent(
            "关闭E04后重新规划",
            mode="mock",
            context={
                "files": SAMPLE_ITEMS,
                "detections": DET_P04,
                "start_node": "J01",
                "closed_edges": [],
            },
            planner="rule",
        )
        _assert(start.plan is not None, "起点 J01 没有规划")
        _assert(start.plan_start_node == "J01", "Agent 未沿用页面起点 J01")
        _assert(_approx(start.plan["total_distance_m"], 30.0), "J01 距离不是 30 m")
        return "关闭 E11→partial；开放 E11→ok 10 m；J01 起点沿用且 30 m"

    def test_cases() -> str:
        bundle = load_cases()
        _assert(len(bundle["cases"]) == 3, "案例数量不是 3")
        _assert("不是 A 的录音检测结果" in bundle["disclaimer"], "缺少模拟来源说明")
        case_dir = out / "cases"
        details = []
        for case in bundle["cases"]:
            result, detail = _check_case(case, case_dir)
            details.append(detail)
            if case["case_id"] == "C1":
                priority_order = [
                    item["asset_id"] for item in result.get("priority") or []
                ]
                _assert(
                    priority_order == ["P02", "P05"],
                    f"C1 priority 不是等级/编号顺序：{priority_order}",
                )
                _assert(
                    result["optimized"]["visit_order"] == ["P05", "P02"],
                    "C1 priority 与实际 visit_order 未区分",
                )
                comparison = result["comparison"]
                _assert(
                    _approx(comparison["distance_difference_m"], 20.0),
                    f"C1 距离差不符：{comparison}",
                )
                _assert(
                    _approx(comparison["improvement_percent"], 22.22, 0.01),
                    f"C1 改善比例不符：{comparison}",
                )
            if case["case_id"] == "C2":
                criticality = {
                    item["asset_id"]: int(item["criticality"])
                    for item in list_assets()
                }
                order = result["optimized"]["visit_order"]
                levels = [criticality[asset_id] for asset_id in order]
                _assert(
                    levels == sorted(levels, reverse=True),
                    f"C2 访问顺序不满足等级约束：{levels}",
                )
            if case["case_id"] == "C3":
                _assert(
                    result["status"] == "partial"
                    and result["unreachable_asset_ids"] == ["P02"],
                    "C3 未按 partial 处理 P02",
                )
        return "；".join(details)

    def test_zero_baseline_ratio() -> str:
        result = compare_inspection_plans(
            [{"asset_id": "P01", "status": "normal"}],
            output_dir=str(out / "cases" / "no_targets"),
        )
        _assert(result["status"] == "no_targets", f"status={result['status']}")
        comparison = result["comparison"]
        _assert(comparison["baseline_zero"] is True, "基线为 0 未标记")
        _assert(comparison["improvement_ratio"] is None, "基线为 0 时比例应为空")
        _assert("不适用" in comparison["improvement_text"], "比例没有写不适用")
        return "无目标时基线为 0，改善比例明确为不适用"

    def test_case_script_outputs() -> str:
        case_out = out / "script_cases"
        env = dict(__import__("os").environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "examples" / "run_cd_cases.py"),
                "--output-dir",
                str(case_out),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env=env,
        )
        _assert(
            proc.returncode == 0,
            f"案例脚本退出码 {proc.returncode}；stderr={proc.stderr[-500:]}",
        )
        run_dirs = sorted(case_out.glob("case_run_*"))
        _assert(run_dirs, "案例脚本没有生成输出目录")
        run_dir = run_dirs[-1]
        for name in ("C1_result.json", "C2_result.json", "C3_result.json", "manifest.json", "summary.csv"):
            _assert((run_dir / name).is_file(), f"缺少输出文件：{name}")
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        _assert(manifest.get("all_expected_match") is True, "案例脚本报告 expected 不符")
        with (run_dir / "summary.csv").open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        _assert(len(rows) == 3, f"CSV 行数不是 3：{len(rows)}")
        by_id = {row["case_id"]: row for row in rows}
        _assert(_approx(float(by_id["C1"]["optimized_distance_m"]), 70.0), "C1 优化距离不符")
        _assert(_approx(float(by_id["C1"]["baseline_distance_m"]), 90.0), "C1 固定距离不符")
        _assert(by_id["C3"]["status"] == "partial", "C3 状态不符")
        for row in rows:
            for key in ("optimized_map", "baseline_map", "comparison_chart"):
                _assert(_png_ok(row[key]), f"{row['case_id']} 的 {key} 不是 PNG")
        return f"{run_dir.relative_to(ROOT).as_posix()}；3 个案例，JSON/CSV/9 张图齐全"

    def test_report_actual_params() -> str:
        plan = plan_inspection(
            DET_P04,
            start_node="J01",
            closed_edges=["E04"],
            output_dir=str(out / "plans" / "report"),
        )
        rows = [
            {
                "asset_id": "P04",
                "status": "suspected_anomaly",
                "score": 0.6187,
                "threshold": 0.5455,
                "quality": {"filename": "P04_sample.wav"},
            }
        ]
        report = build_markdown_report(
            rows,
            False,
            "check-run",
            "",
            plan=plan,
            plan_is_mock=False,
            start_node="S",
            closed_edges=[],
        )
        _assert("实际使用起点：J01" in report, "报告未记录实际起点 J01")
        _assert("实际关闭通道：E04" in report, "报告未记录实际关闭通道 E04")
        _assert(
            "完整路线节点：J01 → S → N04" in report,
            "报告未记录完整路线",
        )
        _assert("总距离：30.0 m" in report, "报告距离不是实际 30 m")
        _assert("合成演示" in report, "报告缺少当前来源口径")
        _assert(
            "MIMII 是计划采用的公开数据集参考" in report,
            "报告把 MIMII 写成了已完成训练",
        )
        _assert(
            "基于公开工业数据（MIMII 水泵）训练" not in report,
            "报告仍保留旧来源声明",
        )
        invalid_plan = {
            "status": "invalid_input",
            "notes": ["测试非法起点"],
            "closed_edges": [],
            "visit_order": [],
            "route_nodes": [],
            "total_distance_m": None,
            "unreachable_asset_ids": [],
        }
        invalid_report = build_markdown_report(
            rows, False, "bad-run", "", plan=invalid_plan
        )
        _assert(
            "本次规划失败" in invalid_report and "不能用旧路线" in invalid_report,
            "invalid_input 没有按失败写入报告",
        )
        return "报告使用 J01/E04/完整路线；invalid_input 按失败记录"

    def test_page_state_flow() -> str:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        _assert(not at.exception, f"初始页面异常：{at.exception}")
        at.button(key="run_detect").click().run()
        _assert(not at.exception, f"检测后页面异常：{at.exception}")
        detections = at.session_state["detections"]
        _assert(len(detections) == 2, f"检测条数异常：{len(detections)}")
        _assert(
            [(r["asset_id"], r["status"]) for r in detections]
            == [("P01", "normal"), ("P04", "suspected_anomaly")],
            "默认检测结果不符",
        )

        at.button(key="run_plan").click().run()
        plan = at.session_state["plan_result"] or {}
        _assert(plan.get("status") == "ok", f"默认规划状态：{plan.get('status')}")
        _assert(_approx(plan.get("total_distance_m"), 10.0), "默认规划不是 10 m")

        at.multiselect(key="plan_closed_edges").set_value(["E11"]).run()
        _assert(at.session_state["plan_result"] is None, "关路后旧路线未清除")
        _assert(len(at.session_state["detections"]) == 2, "关路时检测结果被误删")
        _assert(bool(at.session_state["plan_notice"]), "关路后没有提示重新规划")

        at.button(key="run_plan").click().run()
        plan = at.session_state["plan_result"] or {}
        _assert(plan.get("status") == "partial", f"E11 状态：{plan.get('status')}")
        _assert(plan.get("unreachable_asset_ids") == ["P04"], "E11 未列出 P04")
        _assert(_approx(plan.get("total_distance_m"), 0.0), "E11 距离不是 0")

        at.multiselect(key="plan_closed_edges").set_value([]).run()
        _assert(at.session_state["plan_result"] is None, "重新开放后旧路线未清除")
        at.button(key="run_plan").click().run()
        plan = at.session_state["plan_result"] or {}
        _assert(plan.get("status") == "ok", "重新开放后未恢复 ok")
        _assert(_approx(plan.get("total_distance_m"), 10.0), "重新开放后未恢复 10 m")

        at.selectbox(key="plan_start_node").set_value("J01").run()
        _assert(at.session_state["plan_result"] is None, "起点变化后旧路线未清除")
        _assert(len(at.session_state["detections"]) == 2, "起点变化时检测结果被误删")
        at.button(key="run_plan").click().run()
        plan = at.session_state["plan_result"] or {}
        _assert(plan.get("start_node") == "J01", "页面规划未使用 J01")
        _assert(_approx(plan.get("total_distance_m"), 30.0), "J01 距离不是 30 m")

        options = at.radio(key="audio_source").options
        at.radio(key="audio_source").set_value(options[2]).run()
        _assert(at.session_state["detections"] == [], "切换上传页后旧检测未清除")
        _assert(at.session_state["plan_result"] is None, "切换上传页后旧路线未清除")
        _assert(at.session_state["last_agent"] is None, "切换上传页后旧回答未清除")
        return "检测→规划→关 E11→重开→换起点→切上传页，状态清理符合预期"

    def test_page_agent_scope() -> str:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        at.button(key="run_detect").click().run()

        at.text_input(key="agent_question").set_value("检测P01")
        at.run()
        at.button(key="run_agent").click().run()
        _assert(
            [r["asset_id"] for r in at.session_state["detections"]] == ["P01"],
            "页面 Agent 的检测P01 仍带上了 P04",
        )

        at.text_input(key="agent_question").set_value("检测P04并关闭E11后重新规划")
        at.run()
        at.button(key="run_agent").click().run()
        _assert(not at.exception, f"Agent 检测+规划异常：{at.exception}")
        _assert(
            [r["asset_id"] for r in at.session_state["detections"]] == ["P04"],
            "Agent 第二次检测没有替换旧结果",
        )
        plan = at.session_state["plan_result"] or {}
        _assert(plan.get("status") == "partial", "Agent 关闭 E11 未得到 partial")
        _assert(
            at.session_state["plan_closed_edges"] == ["E11"],
            "页面关闭通道状态与 Agent 不一致",
        )

        at.text_input(key="agent_question").set_value("重新开放E11并重新规划")
        at.run()
        at.button(key="run_agent").click().run()
        plan = at.session_state["plan_result"] or {}
        _assert(plan.get("status") == "ok", "Agent 重新开放 E11 未恢复 ok")
        _assert(_approx(plan.get("total_distance_m"), 10.0), "重新开放后不是 10 m")
        _assert(at.session_state["plan_closed_edges"] == [], "页面仍显示 E11 已关闭")

        at.text_input(key="agent_question").set_value("检测P99")
        at.run()
        at.button(key="run_agent").click().run()
        _assert(at.session_state["detections"] == [], "P99 失败后旧检测未清除")
        _assert(at.session_state["plan_result"] is None, "P99 失败后旧路线未清除")
        _assert(
            "旧检测结果" in at.session_state["detect_error"],
            "P99 失败后没有明确提示旧结果已清除",
        )

        # 失败任务回答也不能跨输入继续显示。
        source_options = at.radio(key="audio_source").options
        at.radio(key="audio_source").set_value(source_options[2]).run()
        _assert(at.session_state["last_agent"] is None, "切换输入后旧任务回答未清除")
        _assert(at.session_state["detect_error"] == "", "切换输入后旧失败提示未清除")
        return "页面 Agent 只检测 P01；关闭/开放 E11 同步；P99 失败清旧结果与旧回答"

    def test_page_case_separate() -> str:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        at.button(key="run_detect").click().run()
        detections_before = [
            (r["asset_id"], r["status"]) for r in at.session_state["detections"]
        ]
        at.button(key="run_plan").click().run()
        plan_before = dict(at.session_state["plan_result"] or {})

        at.button(key="run_cd_case").click().run()
        entry = at.session_state["cd_case_result"] or {}
        result = entry.get("result") or {}
        _assert(result.get("status") == "ok", "C1 页面案例未成功")
        _assert(
            result.get("optimized", {}).get("visit_order") == ["P05", "P02"],
            "C1 页面优化顺序不符",
        )
        _assert(
            _approx(result.get("optimized", {}).get("total_distance_m"), 70.0),
            "C1 页面优化距离不符",
        )
        _assert(
            [(r["asset_id"], r["status"]) for r in at.session_state["detections"]]
            == detections_before,
            "模拟案例覆盖了录音检测结果",
        )
        _assert(
            (at.session_state["plan_result"] or {}).get("status")
            == plan_before.get("status"),
            "模拟案例覆盖了正常规划结果",
        )

        case_select = at.selectbox(key="cd_case_select")
        case_select.set_value(case_select.options[2])
        at.run()
        at.button(key="run_cd_case").click().run()
        entry = at.session_state["cd_case_result"] or {}
        result = entry.get("result") or {}
        _assert(result.get("status") == "partial", "C3 页面案例未得到 partial")
        _assert(
            result.get("unreachable_asset_ids") == ["P02"],
            "C3 页面不可达设备不符",
        )
        _assert(
            [(r["asset_id"], r["status"]) for r in at.session_state["detections"]]
            == detections_before,
            "切换案例后覆盖了录音检测结果",
        )
        return "案例对照独立保存；C1 70 m、C3 partial，均未改动检测/规划状态"

    def test_page_mode_clears() -> str:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        at.button(key="run_detect").click().run()
        at.button(key="run_plan").click().run()
        options = at.radio(key="run_mode").options
        at.radio(key="run_mode").set_value(options[1]).run()
        _assert(at.session_state["detections"] == [], "切换 real 模式后旧检测未清除")
        _assert(at.session_state["plan_result"] is None, "切换 real 模式后旧路线未清除")
        return "mock/real 切换会清除旧检测和旧路线"

    def test_page_same_name_and_invalid_upload() -> str:
        from streamlit.testing.v1 import AppTest

        wav_a = (ROOT / "samples" / "P01_sample.wav").read_bytes()
        wav_b = (ROOT / "samples" / "P04_sample.wav").read_bytes()

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
        at.button(key="run_detect").click().run()
        _assert(len(at.session_state["detections"]) == 2, "内置样例检测未成功")

        source_options = at.radio(key="audio_source").options
        at.radio(key="audio_source").set_value(source_options[2]).run()
        _assert(at.session_state["detections"] == [], "切到上传页后旧检测未清除")

        at.file_uploader[0].upload("same.wav", wav_a, "audio/wav")
        at.run()
        for widget in at.selectbox:
            if widget.key and widget.key.startswith("asset_pick"):
                widget.set_value("P01")
        at.run()
        at.button(key="run_detect").click().run()
        _assert(
            [(r["asset_id"], r["status"]) for r in at.session_state["detections"]]
            == [("P01", "normal")],
            "上传同名文件后的检测结果不符",
        )
        signature_before = at.session_state["detection_input_signature"]

        # 同名文件换内容：文件名不变，仍必须让旧检测失效。
        at.file_uploader[0].upload("same.wav", wav_b, "audio/wav")
        at.run()
        _assert(
            at.session_state["detections"] == [],
            "同名文件内容变化后旧检测未清除",
        )
        _assert(
            at.session_state["detection_input_signature"] != signature_before,
            "同名文件内容变化后输入指纹没有变化",
        )
        _assert(bool(at.session_state["detect_notice"]), "内容变化后没有提示重新检测")

        # 坏 WAV + real：A 返回 invalid_input，页面照实显示，不当成正常。
        at.file_uploader[0].clear()
        at.run()
        at.file_uploader[0].upload("bad.wav", b"not a wave", "audio/wav")
        at.run()
        for widget in at.selectbox:
            if widget.key and widget.key.startswith("asset_pick"):
                widget.set_value("P01")
        at.run()
        mode_options = at.radio(key="run_mode").options
        at.radio(key="run_mode").set_value(mode_options[1]).run()
        at.button(key="run_detect").click().run()
        rows = at.session_state["detections"]
        _assert(len(rows) == 1, f"坏文件检测条数异常：{len(rows)}")
        _assert(rows[0]["status"] == "invalid_input", "坏文件没有返回 invalid_input")
        _assert(rows[0]["reason_code"] == "decode_failed", "坏文件原因码不符")
        _assert(at.session_state["plan_result"] is None, "坏文件检测后仍显示旧路线")
        warnings = "；".join(w.value for w in at.warning)
        _assert("输入不合法" in warnings, "页面没有提示输入不合法（需补录）")
        return "同名换内容清旧检测；坏 WAV 显示 invalid_input 并提示补录"

    checks = [
        ("运行环境", test_environment),
        ("默认样例真实检测", test_default_detection),
        ("默认样例规划 10 米", test_default_plan),
        ("关闭 E11 后 partial", test_close_e11),
        ("重新开放 E11 恢复", test_reopen_e11),
        ("起点改为 J01", test_start_change),
        ("invalid_input 不算正常", test_invalid_not_normal),
        ("全部异常目标不可达", test_all_unreachable),
        ("D invalid_input 按失败处理", test_plan_tool_invalid),
        ("输入指纹与规划上下文", test_input_signature),
        ("规则版编号与封路解析", test_rule_parser),
        ("Agent 只检测 P01", test_agent_detection_scope),
        ("Agent P99 失败清旧结果", test_agent_unknown_clears),
        ("Agent 起点/封路状态", test_agent_plan_state),
        ("三个模拟案例与基线", test_cases),
        ("基线为 0 的改善比例", test_zero_baseline_ratio),
        ("案例脚本 JSON/CSV/图", test_case_script_outputs),
        ("报告记录实际参数", test_report_actual_params),
        ("页面状态清理与重规划", test_page_state_flow),
        ("页面 Agent 状态同步", test_page_agent_scope),
        ("页面案例状态隔离", test_page_case_separate),
        ("页面模式切换清旧结果", test_page_mode_clears),
        ("页面同名文件与坏 WAV", test_page_same_name_and_invalid_upload),
    ]

    for label, func in checks:
        checker.check(label, func)

    passed = sum(1 for r in checker.results if r["ok"])
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
