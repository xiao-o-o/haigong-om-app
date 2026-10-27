# -*- coding: utf-8 -*-
"""读取 C/D 规划模拟案例（examples/cd_cases.json）。

案例是“团队手工设定的检测状态”，不是 A 的模型输出：
- 只包含设备编号、状态、来源说明；
- 不接受 score / threshold 字段，避免把模拟分数混进检测结论；
- 通过 case_detection_results() 转成 D 能接受的检测结果列表。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = ROOT / "examples" / "cd_cases.json"
ALLOWED_STATUSES = {"normal", "suspected_anomaly", "invalid_input"}
REQUIRED_TARGET_FIELDS = ("asset_id", "status", "source_note")


class CaseFileError(ValueError):
    """案例文件缺失、格式错误或缺少模拟来源说明。"""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CaseFileError(f"案例文件不存在：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaseFileError(f"无法读取案例文件 {path.name}：{exc}") from exc
    if not isinstance(raw, dict):
        raise CaseFileError("案例文件顶层必须是对象")
    return raw


def validate_cases(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """校验并返回案例列表；错误时抛出带原因的 CaseFileError。"""
    disclaimer = str(raw.get("disclaimer") or "").strip()
    if "不是" not in disclaimer or "录音检测" not in disclaimer:
        raise CaseFileError(
            "案例文件必须写明「不是 A 的录音检测结果」这类来源说明"
        )

    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CaseFileError("cases 必须是非空数组")

    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise CaseFileError(f"第 {index} 个案例不是对象")
        case_id = str(case.get("case_id") or "").strip()
        if not case_id:
            raise CaseFileError(f"第 {index} 个案例缺少 case_id")
        if case_id in seen:
            raise CaseFileError(f"案例编号重复：{case_id}")
        seen.add(case_id)

        name = str(case.get("name") or "").strip()
        if not name:
            raise CaseFileError(f"案例 {case_id} 缺少 name")

        start = str(case.get("start_node") or "").strip()
        if not start:
            raise CaseFileError(f"案例 {case_id} 缺少 start_node")

        closed = case.get("closed_edges") or []
        if not isinstance(closed, list) or any(
            not isinstance(edge, str) or not edge.strip() for edge in closed
        ):
            raise CaseFileError(f"案例 {case_id} 的 closed_edges 必须是非空字符串数组")

        targets = case.get("targets")
        if not isinstance(targets, list) or not targets:
            raise CaseFileError(f"案例 {case_id} 的 targets 必须是非空数组")

        cleaned_targets: list[dict[str, str]] = []
        seen_assets: set[str] = set()
        for target_index, target in enumerate(targets, start=1):
            if not isinstance(target, dict):
                raise CaseFileError(
                    f"案例 {case_id} 的第 {target_index} 个目标不是对象"
                )
            missing = [
                field for field in REQUIRED_TARGET_FIELDS
                if not str(target.get(field) or "").strip()
            ]
            if missing:
                raise CaseFileError(
                    f"案例 {case_id} 的第 {target_index} 个目标缺少：{'、'.join(missing)}"
                )
            if "score" in target or "threshold" in target:
                raise CaseFileError(
                    f"案例 {case_id} 的目标不应包含 score/threshold；"
                    "模拟案例只给设备编号、状态和来源说明"
                )
            asset_id = str(target["asset_id"]).strip().upper()
            status = str(target["status"]).strip()
            if status not in ALLOWED_STATUSES:
                raise CaseFileError(
                    f"案例 {case_id} 的设备 {asset_id} 状态不合法：{status}"
                )
            if asset_id in seen_assets:
                raise CaseFileError(f"案例 {case_id} 的设备重复：{asset_id}")
            seen_assets.add(asset_id)
            cleaned_targets.append(
                {
                    "asset_id": asset_id,
                    "status": status,
                    "source_note": str(target["source_note"]).strip(),
                }
            )

        cleaned.append(
            {
                "case_id": case_id,
                "name": name,
                "description": str(case.get("description") or "").strip(),
                "start_node": start,
                "closed_edges": [edge.strip().upper() for edge in closed],
                "targets": cleaned_targets,
                "expected": case.get("expected") if isinstance(case.get("expected"), dict) else {},
            }
        )
    return cleaned


def load_cases(path: str | Path | None = None) -> dict[str, Any]:
    """读取案例文件，返回 {disclaimer, cases, path}。"""
    case_path = Path(path) if path else DEFAULT_CASES_PATH
    if not case_path.is_absolute():
        case_path = ROOT / case_path
    raw = _load_json(case_path)
    return {
        "schema_version": str(raw.get("schema_version") or "1.0"),
        "disclaimer": str(raw.get("disclaimer") or "").strip(),
        "cases": validate_cases(raw),
        "path": case_path.relative_to(ROOT).as_posix()
        if case_path.is_relative_to(ROOT)
        else str(case_path),
    }


def case_detection_results(case: dict[str, Any]) -> list[dict[str, Any]]:
    """把案例目标转成 D 的检测结果列表。

    不生成 score / threshold；额外字段只用于在页面上标明模拟来源。
    """
    results: list[dict[str, Any]] = []
    for target in case.get("targets") or []:
        results.append(
            {
                "asset_id": target["asset_id"],
                "status": target["status"],
                "simulation": True,
                "simulation_case_id": case["case_id"],
                "source_note": target["source_note"],
                "notes": [target["source_note"]],
            }
        )
    return results
