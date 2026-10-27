# -*- coding: utf-8 -*-
"""运行 C/D 规划模拟案例，输出原始 JSON、汇总 CSV 和三张图。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe examples\\run_cd_cases.py
    .venv\\Scripts\\python.exe examples\\run_cd_cases.py --output-dir runs/cd_cases

说明：
- 案例状态由团队手工设定，不是 A 的录音检测结果；
- 距离由 D 实际按 length_m 计算，脚本不把 70/90 等数字写死为结果；
- expected 只用于事后核对，不参与规划。
"""
from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cd_cases import case_detection_results, load_cases  # noqa: E402
from spatial_module.plan import compare_inspection_plans  # noqa: E402

DISTANCE_TOL = 1e-6


def _display(path: str | Path) -> str:
    value = Path(path)
    try:
        return value.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(value)


def _check_expected(result: dict, expected: dict) -> tuple[bool, list[str]]:
    """按案例文件里的 expected 核对实际结果；不参与计算。"""
    problems: list[str] = []
    if not expected:
        return True, []

    if expected.get("status") != result.get("status"):
        problems.append(
            f"status 期望 {expected.get('status')}，实际 {result.get('status')}"
        )

    optimized = result.get("optimized") or {}
    baseline = result.get("baseline") or {}

    if "optimized_visit_order" in expected and list(optimized.get("visit_order") or []) != list(
        expected["optimized_visit_order"]
    ):
        problems.append(
            "优化访问顺序期望 "
            f"{expected['optimized_visit_order']}，实际 {optimized.get('visit_order')}"
        )
    if "optimized_total_distance_m" in expected:
        expected_distance = float(expected["optimized_total_distance_m"])
        actual_distance = float(optimized.get("total_distance_m") or 0.0)
        if abs(actual_distance - expected_distance) > DISTANCE_TOL:
            problems.append(
                f"优化距离期望 {expected_distance:g} m，实际 {actual_distance:g} m"
            )

    if "baseline_visit_order" in expected and list(baseline.get("visit_order") or []) != list(
        expected["baseline_visit_order"]
    ):
        problems.append(
            "固定顺序期望 "
            f"{expected['baseline_visit_order']}，实际 {baseline.get('visit_order')}"
        )
    if "baseline_total_distance_m" in expected:
        expected_distance = float(expected["baseline_total_distance_m"])
        actual_distance = float(baseline.get("total_distance_m") or 0.0)
        if abs(actual_distance - expected_distance) > DISTANCE_TOL:
            problems.append(
                f"固定顺序距离期望 {expected_distance:g} m，实际 {actual_distance:g} m"
            )

    if "unreachable_asset_ids" in expected:
        expected_unreachable = sorted(expected["unreachable_asset_ids"])
        actual_unreachable = sorted(result.get("unreachable_asset_ids") or [])
        if actual_unreachable != expected_unreachable:
            problems.append(
                f"不可达设备期望 {expected_unreachable}，实际 {actual_unreachable}"
            )

    return not problems, problems


def _summary_row(case: dict, result: dict, match: bool, problems: list[str]) -> dict:
    optimized = result.get("optimized") or {}
    baseline = result.get("baseline") or {}
    comparison = result.get("comparison") or {}
    timing = result.get("timing") or {}
    return {
        "case_id": case["case_id"],
        "name": case["name"],
        "status": result.get("status"),
        "start_node": result.get("start_node"),
        "closed_edges": "、".join(result.get("closed_edges") or []),
        "target_asset_ids": "、".join(result.get("target_asset_ids") or []),
        "unreachable_asset_ids": "、".join(result.get("unreachable_asset_ids") or []),
        "optimized_visit_order": " → ".join(optimized.get("visit_order") or []),
        "optimized_route_nodes": " → ".join(optimized.get("route_nodes") or []),
        "optimized_distance_m": optimized.get("total_distance_m"),
        "baseline_visit_order": " → ".join(baseline.get("visit_order") or []),
        "baseline_route_nodes": " → ".join(baseline.get("route_nodes") or []),
        "baseline_distance_m": baseline.get("total_distance_m"),
        "distance_difference_m": comparison.get("distance_difference_m"),
        "improvement_percent": (
            comparison.get("improvement_percent")
            if comparison.get("improvement_percent") is not None
            else "不适用（基线距离为0）"
        ),
        "same_order": comparison.get("same_order"),
        "calculation_total_ms": timing.get("calculation_total_ms"),
        "drawing_total_ms": timing.get("drawing_total_ms"),
        "optimized_map": optimized.get("map_file"),
        "baseline_map": baseline.get("map_file"),
        "comparison_chart": (result.get("map_files") or {}).get("comparison_chart"),
        "expected_match": match,
        "expected_problems": "；".join(problems),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 C/D 规划模拟案例")
    parser.add_argument("--cases", default="examples/cd_cases.json")
    parser.add_argument("--output-dir", default="runs/cd_cases")
    args = parser.parse_args()

    bundle = load_cases(args.cases)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    run_dir = output_dir / f"case_run_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)

    manifest = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": bundle["path"],
        "disclaimer": bundle["disclaimer"],
        "environment": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "timing_note": (
            "计算耗时和绘图耗时分别记录，但都是单次本机运行值；"
            "受首次加载、字体缓存、磁盘和图像大小影响，不是可靠性能基准。"
        ),
        "cases": [],
    }
    rows: list[dict] = []
    all_match = True

    for case in bundle["cases"]:
        result = compare_inspection_plans(
            case_detection_results(case),
            start_node=case["start_node"],
            closed_edges=case["closed_edges"],
            output_dir=_display(run_dir / "maps"),
        )
        match, problems = _check_expected(result, case.get("expected") or {})
        all_match = all_match and match
        if not match:
            print(
                f"[不符] {case['case_id']}：{'；'.join(problems)}",
                file=sys.stderr,
            )

        raw_path = run_dir / f"{case['case_id']}_result.json"
        raw_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        row = _summary_row(case, result, match, problems)
        rows.append(row)
        manifest["cases"].append(
            {
                "case_id": case["case_id"],
                "name": case["name"],
                "description": case.get("description", ""),
                "source_note": "团队手工设定，不是 A 的录音检测结果",
                "targets": case["targets"],
                "expected": case.get("expected") or {},
                "expected_match": match,
                "expected_problems": problems,
                "raw_result": _display(raw_path),
            }
        )
        print(
            f"[案例] {case['case_id']} status={result.get('status')} "
            f"优化={' → '.join((result.get('optimized') or {}).get('visit_order') or [])} "
            f"{(result.get('optimized') or {}).get('total_distance_m')} m；"
            f"固定={' → '.join((result.get('baseline') or {}).get('visit_order') or [])} "
            f"{(result.get('baseline') or {}).get('total_distance_m')} m；"
            f"核对={'通过' if match else '不符'}"
        )

    csv_path = run_dir / "summary.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    manifest["summary_csv"] = _display(csv_path)
    manifest["all_expected_match"] = all_match
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )

    print(f"[输出] { _display(run_dir) }")
    print(f"[汇总] { _display(csv_path) }")
    print(f"[记录] { _display(manifest_path) }")
    return 0 if all_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
