# -*- coding: utf-8 -*-
"""Agent 工具集 v0.2：对接 A / D 的真实本地模块。

工具清单（实施手册 B 部分，3 个，不多不少）：
1. detect_assets_audio    批量检测音频 → 直接调 A 的 analyze_audio
2. plan_asset_inspection  生成巡检计划 → 直接调 D 的 plan_inspection（入参就是 A 的结果）
3. get_checklist          查询本地检查资料（读 knowledge/，不碰外部模块）

与 v0.1 的差异（队友交付后的契约对齐）：
- 不再走 HTTP；real 模式改为进程内 import（A/D 交付的是函数不是服务）；
- 检测入参从「文件名列表」改为「文件 + 设备编号」二元组，A 不从文件名推断编号；
- 规划入参从「设备列表 + 日期」改为「A 的检测结果列表 + 关闭通道」，日期由 D 内部生成；
- 规划强依赖检测结果：没有检测结果直接报错，不用设备清单硬凑。

红线（沿用 v0.1，不放宽）：
- 调用失败时 ok=False 并原样带出错误，绝不回退到模拟数据；
- 模型缺失属于系统错误，不能当成「设备正常」；
- 批量检测无论文件数量，Agent 侧只计 1 次工具调用。
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import settings
from core.local_modules import (
    EXPECTED_SAMPLE_RATE,
    ModuleUnavailable,
    audio_analyzer,
    asset_ids as scene_asset_ids,
    route_planner,
)

# knowledge/ 目录：get_checklist 的数据源
KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

# A 的模型阈值（mock 数据也用同一阈值，保证两种模式判定口径一致）
DEFAULT_THRESHOLD = 0.5455

# 模拟模式固定分数：P01/P04 直接取 A 的样例真实分数，其余按场景补一组确定的演示值。
# 目的：演示可复现（同一设备每次结果一致），且 3 台异常 / 3 台正常便于展示 partial 路线。
_MOCK_SCORES = {
    "P01": 0.4557,
    "P02": 0.5013,
    "P03": 0.5908,
    "P04": 0.6187,
    "P05": 0.4762,
    "P06": 0.5831,
}


@dataclass
class ToolResult:
    """工具执行结果。ok=False 时 message 必须带出真实原因。"""

    tool: str
    ok: bool
    is_mock: bool
    data: Any = None
    message: str = ""


# ---------------------------------------------------------------------------
# 工具 1：批量音频检测（对接 A）
# ---------------------------------------------------------------------------

def _mock_item(asset_id: str, filename: str, rng: random.Random) -> dict:
    """生成一条 A schema 1.0 的模拟结果。

    字段与 A 的真实返回严格同构，这样下游 D 的规划在 mock 模式下也能跑通；
    缺的只有 plot_file（模拟模式不产频谱图）。
    """
    score = _MOCK_SCORES.get(asset_id) or round(rng.uniform(0.30, 0.78), 4)
    status = "suspected_anomaly" if score >= DEFAULT_THRESHOLD else "normal"
    return {
        "schema_version": "1.0",
        "asset_id": asset_id,
        "model_version": "mock",
        "score": score,
        "threshold": DEFAULT_THRESHOLD,
        "status": status,
        "quality": {
            "sample_rate_hz": EXPECTED_SAMPLE_RATE,
            "sample_rate_ok": True,
            "channels": 1,
            "duration_s": 10.0,
            "filename": filename,
        },
        "plot_file": None,
        "notes": ["模拟数据：未调用 A 的模型，仅供流程演示"],
        "run_id": f"mock-{asset_id}",
        "method": "mock",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _files_by_asset(files: Any) -> dict[str, str]:
    """把页面送检的文件整理成 asset_id → 本地路径。

    兼容两种入参：[{"asset_id","path","filename"}] 或 {"P04": "/abs/x.wav"}。
    只有页面/运行时能给这个映射，模型无法指定路径。
    """
    mapping: dict[str, str] = {}
    if isinstance(files, dict):
        for key, value in files.items():
            aid, path = str(key).strip().upper(), str(value or "").strip()
            if aid and path:
                mapping.setdefault(aid, path)
        return mapping
    for item in files or []:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("asset_id", "")).strip().upper()
        path = str(item.get("path", "") or "").strip()
        if aid and path:
            mapping.setdefault(aid, path)
    return mapping


def _normalize_ids(asset_ids: Any) -> list[str]:
    """去空白、转大写、去重保序。模型偶尔会重复给同一个编号。"""
    if isinstance(asset_ids, str):
        asset_ids = [asset_ids]
    ids: list[str] = []
    for raw in asset_ids or []:
        aid = str(raw).strip().upper()
        if aid and aid not in ids:
            ids.append(aid)
    return ids


def detect_assets_audio(
    asset_ids: list[str] | str | None = None,
    files: Any = None,
    mode: str | None = None,
) -> ToolResult:
    """批量检测音频（包装 A 的 analyze_audio）。

    参数
    ----
    asset_ids : list[str]
        要检测的设备编号，如 ["P01", "P04"]。**这是模型唯一能指定的东西**：
        路径由运行时通过 files 注入，模型不接触文件系统。
    files : list[dict] | dict
        页面已送检的文件，用于把编号解析成路径；real 模式下缺路径的设备进 missing。
    mode : "mock" | "real"
        mock 生成同构模拟结果（不需要真实文件）；real 逐文件调用 A。

    返回（data）
    -------------
    requested_asset_ids : 本次请求的编号（去重后）
    missing_asset_ids   : 请求了但没有可用音频文件的编号（real 模式才会非空）
    results             : A 的返回列表，字段沿用 A 的 schema（invalid_input 也照实放在这里）
    count / abnormal    : 便于页面与摘要使用的计数
    """
    mode = mode or settings.app_mode
    requested = _normalize_ids(asset_ids)
    is_mock = mode == "mock"

    if not requested:
        return ToolResult("detect_assets_audio", False, is_mock, message="未指定要检测的设备编号")

    known = set(scene_asset_ids())
    unknown = [aid for aid in requested if aid not in known]
    if unknown:
        return ToolResult(
            "detect_assets_audio", False, is_mock,
            message=(
                f"未知设备编号：{'、'.join(unknown)}。"
                f"本厂区登记的设备只有 {'、'.join(sorted(known))}，不会为不存在的设备编造结果。"
            ),
        )

    if is_mock:
        results = []
        for aid in requested:
            filename = f"{aid}_sim.wav"
            rng = random.Random(f"{aid}|{filename}")
            results.append(_mock_item(aid, filename, rng))
        return ToolResult(
            "detect_assets_audio", True, True,
            _package(requested, [], results),
        )

    try:
        analyze = audio_analyzer()
    except ModuleUnavailable as exc:
        return ToolResult("detect_assets_audio", False, False, message=str(exc))

    paths = _files_by_asset(files)
    missing = [aid for aid in requested if aid not in paths]
    targets = [(aid, paths[aid]) for aid in requested if aid in paths]
    if not targets:
        return ToolResult(
            "detect_assets_audio", False, False,
            _package(requested, missing, []),
            message=(
                "请求的设备都没有对应的音频文件，未执行检测。"
                "请先在页面上传 WAV 并为文件指定设备编号。"
            ),
        )

    results, errors = [], []
    for aid, path in targets:
        try:
            results.append(analyze(path, aid))
        except Exception as exc:  # 模型缺失/依赖不兼容/解码库异常：系统级错误
            errors.append(f"{aid}：{type(exc).__name__}: {exc}")

    if errors:
        # 一个文件出现系统级异常即整批失败：不拿部分结果冒充完整结论
        return ToolResult(
            "detect_assets_audio", False, False,
            _package(requested, missing, results),
            message="A 模块调用失败 → " + "；".join(errors),
        )

    return ToolResult(
        "detect_assets_audio", True, False,
        _package(requested, missing, results),
    )


def _package(requested: list[str], missing: list[str], results: list[dict]) -> dict:
    return {
        "requested_asset_ids": list(requested),
        "missing_asset_ids": list(missing),
        "results": list(results),
        "count": len(results),
        "abnormal": _count_abnormal(results),
    }


def _count_abnormal(results: list[dict]) -> int:
    return sum(1 for r in results if r.get("status") == "suspected_anomaly")


# ---------------------------------------------------------------------------
# 工具 2：巡检规划（对接 D）
# ---------------------------------------------------------------------------

def plan_asset_inspection(
    detection_results: list[dict] | None = None,
    closed_edges: list[str] | None = None,
    start_node: str = "S",
    input_is_mock: bool = False,
) -> ToolResult:
    """按 A 的检测结果规划巡检顺序与路网路线。

    参数
    ----
    detection_results : list[dict]
        A 返回的结果列表（必须有 asset_id 和 status）。**不能为空**：
        规划只认检测结果，不按设备清单猜目标。
    closed_edges : list[str] | None
        本次临时关闭的通道编号（E01~E13）。
    start_node : str
        起点节点，默认 "S"。
    input_is_mock : bool
        入参是否来自模拟检测，用于页面如实标注。
    """
    if not detection_results:
        return ToolResult(
            "plan_asset_inspection", False, input_is_mock,
            message="尚无检测结果：巡检规划必须基于音频检测结果，请先执行批量检测",
        )

    bad = [r for r in detection_results if not isinstance(r, dict) or not r.get("asset_id")]
    if bad:
        return ToolResult(
            "plan_asset_inspection", False, input_is_mock,
            message="检测结果格式不合法：每条记录必须包含 asset_id",
        )

    try:
        plan = route_planner()
    except ModuleUnavailable as exc:
        return ToolResult("plan_asset_inspection", False, input_is_mock, message=str(exc))

    try:
        result = plan(
            list(detection_results),
            start_node=start_node,
            closed_edges=list(closed_edges or []),
            output_dir="runs",
        )
    except Exception as exc:  # PlanInputError / SceneDataError / PlanSystemError
        return ToolResult(
            "plan_asset_inspection", False, input_is_mock,
            message=f"D 模块调用失败（{type(exc).__name__}）：{exc}",
        )

    return ToolResult("plan_asset_inspection", True, input_is_mock, result)


# ---------------------------------------------------------------------------
# 工具 3：本地检查资料
# ---------------------------------------------------------------------------

def get_checklist(query: str = "", mode: str | None = None) -> ToolResult:
    """检索知识条目（knowledge/checklist.json）。

    query 为空返回全部；否则在编号 / 标题 / 适用条件 / 检查建议里做包含匹配。
    返回 items，字段：source_id、title、url、conditions（适用条件）、advice（检查建议）、kind。
    kind = public 表示引用公开资料，team 表示团队自拟规则。
    """
    mode = mode or settings.app_mode
    path = KNOWLEDGE_DIR / "checklist.json"
    if not path.is_file():
        return ToolResult(
            "get_checklist", False, mode == "mock",
            message=f"知识条目文件缺失：knowledge/{path.name}",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ToolResult("get_checklist", False, mode == "mock", message=f"知识条目解析失败：{exc}")
    if not isinstance(raw, list):
        return ToolResult("get_checklist", False, mode == "mock", message="知识条目格式错误：顶层应为数组")

    items = [i for i in raw if isinstance(i, dict) and i.get("source_id")]
    q = (query or "").strip().lower()
    fallback = False
    matched_terms: list[str] = []
    if q:
        # 按空白与常见分隔符切词。整串匹配会把「疑似异常 复核」当成一个词，
        # 结果一条都命不中，白退化成全量返回。
        terms = [t for t in re.split(r"[\s,，、;；/|]+", q) if len(t) >= 2]
        def _partial(term: str, text: str) -> float:
            """整词命中记 1 分；中文长词按二字切片算命中比例。

            中文没有空格，用户或模型给的「音频质量」可能对不上条目里的「音频输入质量要求」，
            单靠整串包含会漏。切片法让这种情况仍能命中，只是得分低于整词。
            """
            if term in text:
                return 1.0
            if len(term) >= 4:
                grams = {term[i : i + 2] for i in range(len(term) - 1)}
                return sum(1 for g in grams if g in text) / len(grams)
            return 0.0

        if terms:
            scored = []
            hit_terms: set[str] = set()
            for i in items:
                text = " ".join(
                    str(i.get(k, "")) for k in ("source_id", "title", "conditions", "advice")
                ).lower()
                got = {t: _partial(t, text) for t in terms}
                got = {t: s for t, s in got.items() if s > 0}
                if got:
                    scored.append((sum(got.values()), i))
                    hit_terms.update(got)
            if scored:
                # 相关度高的排前面，便于模型与用户先看到最贴近的一条
                scored.sort(key=lambda pair: -pair[0])
                items = [i for _, i in scored]
                matched_terms = sorted(hit_terms)
            else:
                # 关键词没命中时不返回空手：全量返回并标注 fallback，由页面/Agent 说明「无精确匹配」
                fallback = True
    return ToolResult(
        "get_checklist", True, mode == "mock",
        {
            "items": items,
            "count": len(items),
            "matched_terms": matched_terms,
            "sources": [i["source_id"] for i in items],
            "query": query or "",
            "fallback": fallback,
        },
    )


# ---------------------------------------------------------------------------
# 工具清单（供页面展示）
# ---------------------------------------------------------------------------

TOOL_SPECS = [
    {"工具": "detect_assets_audio", "对接": "A · analyze_audio", "用途": "批量检测音频异常", "计费": "1 次/批"},
    {"工具": "plan_asset_inspection", "对接": "D · plan_inspection", "用途": "按检测结果规划巡检路线", "计费": "1 次/调用"},
    {"工具": "get_checklist", "对接": "知识库", "用途": "检索本地检查资料", "计费": "1 次/调用"},
]
