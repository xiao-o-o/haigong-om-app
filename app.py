# -*- coding: utf-8 -*-
"""海工智维 · 单页应用主入口（v0.3：接入 A / C / D 真实模块）

B 部分（应用与 Agent）职责：把 A 的声音模型、D 的巡检算法、C 的场景绘图
接成一个能跑、能点、能演示的单页应用。

结构约束（沿用手册，不放宽）：
- 唯一主入口：单页 + 页内分区（st.tabs），不建 pages/ 多页目录；
- 三个工具、一个 Agent，预算 6 轮 / 12 次工具调用；
- 显著标明数据来源：模拟数据不得被当成正式结论。

真实模式的数据流（进程内调用，不走 HTTP）：
    上传 WAV → A.analyze_audio(path, asset_id) → 检测结果列表
             → D.plan_inspection(结果列表, closed_edges=...) → 路线 + 地图 PNG
             → D 内部调用 C.render_route 出图

本地运行：
    .venv\\Scripts\\streamlit run app.py
"""
from __future__ import annotations

import csv
import io
import json
import sys
import uuid
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.loop import (  # noqa: E402
    MAX_ROUNDS,
    MAX_TOOL_CALLS,
    PLANNER_LABELS,
    run_agent,
)
from core.llm import current_config as llm_config  # noqa: E402
from core import runlog  # noqa: E402
from core import cd_state  # noqa: E402
from core.cd_cases import (  # noqa: E402
    CaseFileError,
    case_detection_results,
    load_cases,
)
from agent.tools import (  # noqa: E402
    TOOL_SPECS,
    detect_assets_audio,
    get_checklist,
    plan_asset_inspection,
)
from core.local_modules import (  # noqa: E402
    EXPECTED_SAMPLE_RATE,
    PROJECT_ROOT,
    asset_ids,
    audio_analyzer,
    check_modules,
    comparison_planner,
    edge_ids,
    list_assets,
    list_edges,
    node_ids,
    sample_files,
)
from spatial_module.mission import (  # noqa: E402
    COMPLETION_NOTE,
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_NO_TARGETS,
    MissionError,
    confirm_asset,
    create_mission,
    mission_is_stale,
    mission_to_json,
    replan_mission,
    step_forward,
)

UPLOAD_DIR = PROJECT_ROOT / "runs" / "uploads"

st.set_page_config(page_title="海工智维 · 设备运维助手", page_icon="🔧", layout="wide")

from core.report import STATUS_ZH, build_markdown_report  # noqa: E402  报告排版与状态译名

# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------
st.session_state.setdefault("mode", "mock")
st.session_state.setdefault("planner", "auto")
st.session_state.setdefault("agent_usage", {"rounds": 0, "calls": 0})
st.session_state.setdefault("detect_items", [])       # 最近一次送检的 [{asset_id, path, filename}]
st.session_state.setdefault("detections", [])          # 最近一次检测结果（A schema）
st.session_state.setdefault("detections_is_mock", False)
st.session_state.setdefault("detection_input_signature", None)  # 当前检测结果对应的输入指纹
st.session_state.setdefault("detect_error", "")
st.session_state.setdefault("detect_notice", "")
st.session_state.setdefault("detect_run_id", "")
st.session_state.setdefault("detect_log_path", "")
st.session_state.setdefault("plan_result", None)
st.session_state.setdefault("plan_is_mock", False)
st.session_state.setdefault("plan_context", None)      # 生成路线时的起点/关闭通道
st.session_state.setdefault("plan_start_node", "S")
st.session_state.setdefault("plan_closed_edges", [])
st.session_state.setdefault("plan_used_start_node", "S")
st.session_state.setdefault("plan_used_closed_edges", [])
st.session_state.setdefault("plan_error", "")
st.session_state.setdefault("plan_notice", "")
st.session_state.setdefault("last_agent", None)      # 最近一次 Agent 结果（含轨迹与日志路径）
st.session_state.setdefault("cd_case_result", None)   # 模拟案例对照，与真实检测/规划分开保存
st.session_state.setdefault("cd_case_id", "")
st.session_state.setdefault("cd_case_error", "")
st.session_state.setdefault("mission", None)          # 当前会话的巡检执行任务
st.session_state.setdefault("mission_error", "")
st.session_state.setdefault("mission_notice", "")
st.session_state.setdefault("mission_closed_edges", [])
st.session_state.setdefault("_current_input_signature", "")
st.session_state.setdefault("_pending_plan_start_node", None)
st.session_state.setdefault("_pending_plan_closed_edges", None)
st.session_state.setdefault("_pending_mission_closed_edges", None)

# Agent 在页面之后执行，不能直接改已经创建过的 widget 状态。
# 先把要同步的值放这里，下一次 rerun 在 widget 创建前应用。
if st.session_state.get("_pending_plan_start_node") is not None:
    pending_start = st.session_state.pop("_pending_plan_start_node")
    if pending_start in node_ids():
        st.session_state["plan_start_node"] = pending_start
if st.session_state.get("_pending_plan_closed_edges") is not None:
    pending_closed = st.session_state.pop("_pending_plan_closed_edges")
    valid_edges = set(edge_ids())
    st.session_state["plan_closed_edges"] = [
        edge for edge in pending_closed if edge in valid_edges
    ]
if st.session_state.get("_pending_mission_closed_edges") is not None:
    pending_mission_closed = st.session_state.pop("_pending_mission_closed_edges")
    valid_edges = set(edge_ids())
    st.session_state["mission_closed_edges"] = [
        edge for edge in pending_mission_closed if edge in valid_edges
    ]

def _stage_upload(uploaded_file) -> str:
    """把上传内容落盘到 runs/uploads/，返回绝对路径。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(uploaded_file.name).name
    target = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{src}"
    target.write_bytes(uploaded_file.getbuffer())
    return str(target)


def _abs_asset_path(rel_or_abs: str) -> Path:
    path = Path(rel_or_abs)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _clear_plan_state(message: str = "", notice: bool = True) -> None:
    """清掉旧路线，但保留仍然有效的检测结果。"""
    st.session_state.plan_result = None
    st.session_state.plan_is_mock = False
    st.session_state.plan_context = None
    st.session_state.plan_error = ""
    if notice and message:
        st.session_state.plan_notice = message


def _clear_detection_state(message: str = "", notice: bool = True) -> None:
    """清掉检测结果及其派生的路线、任务回答。"""
    st.session_state.detections = []
    st.session_state.detections_is_mock = False
    st.session_state.detection_input_signature = None
    st.session_state.detect_items = []
    st.session_state.detect_error = ""
    st.session_state.detect_run_id = ""
    st.session_state.detect_log_path = ""
    st.session_state.last_agent = None
    _clear_plan_state("", notice=False)
    if notice and message:
        st.session_state.detect_notice = message


def _pending_signature(source: str, mode: str, items: list[dict]) -> str:
    return cd_state.build_detection_signature(mode, source, items)


# B：在页面各 tab 渲染之前就让旧路线失效，避免报告区先拿到上一帧的旧计划。
if st.session_state.plan_result and not cd_state.same_plan_context(
    st.session_state.plan_context,
    st.session_state.get("plan_start_node", "S"),
    st.session_state.get("plan_closed_edges") or [],
):
    _clear_plan_state(
        "起点或关闭通道已变化：旧路线已清除，检测结果仍保留，请重新规划。"
    )


def _to_csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def _detect_table(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append(
            {
                "设备编号": r.get("asset_id"),
                "文件": (r.get("quality") or {}).get("filename", "—"),
                "判定": STATUS_ZH.get(r.get("status"), r.get("status")),
                "分数": r.get("score") if r.get("score") is not None else "—",
                "阈值": r.get("threshold", "—"),
                "说明": "；".join(r.get("notes") or []),
            }
        )
    return out


def _render_agent_result(res) -> None:
    """渲染一次 Agent 结果：决策器来源、轨迹（六字段）、结论、日志下载。"""
    src_label = {
        "llm": "🟢 LLM 决策器",
        "rule": "⚪ 规则版决策器",
        "rule_fallback": "🟡 规则版（LLM 回退）",
    }.get(res.planner, res.planner)
    st.caption(f"**本次决策器：** {src_label} ｜ {res.planner_note}")
    if res.run_id:
        st.caption(
            f"运行编号：`{res.run_id}`"
            + (f" ｜ 日志：`{res.log_path}`" if res.log_path else " ｜ 日志写入失败（不影响结果）")
        )

    with st.expander("执行轨迹 · step / tool / arguments / result_summary / status / duration_ms", expanded=True):
        if not res.steps:
            st.caption("本次没有执行工具调用。")
        for s in res.steps:
            tag = "模拟" if s.is_mock else "真实"
            st.markdown(
                f"**第 {s.round_no} 轮 · `{s.tool}`**（{tag}）  \n"
                f"status：`{s.status}` ｜ duration_ms：{s.duration_ms}  \n"
                f"result_summary：{s.summary}"
            )
            st.code(json.dumps(runlog.digest_args(s.args), ensure_ascii=False, indent=2), language="json")
            if s.error:
                st.error(f"错误：{s.error}")
        usage = f"本次消耗：{res.rounds_used} 轮 / {res.calls_used} 次工具调用"
        if res.budget_exhausted:
            usage += "（已达上限，提前停止）"
        st.caption(usage)

    if res.answer.startswith("【失败】"):
        st.error(res.answer)
    else:
        st.success(res.answer)

    if res.log_path:
        log_file = PROJECT_ROOT / res.log_path
        if log_file.is_file():
            st.download_button(
                "下载本次运行日志（JSON）",
                log_file.read_bytes(),
                file_name=log_file.name,
                mime="application/json",
                key=f"log_dl_{res.run_id}",
            )

    if res.detections:
        st.caption("检测结果已同步到「② 音频检测」与「③ 巡检规划」。")
    if res.mission:
        st.caption(
            f"巡检任务已更新（{res.mission.get('mission_id')}）；"
            "切到「③ 巡检规划」可继续执行。"
        )
    elif res.plan:
        st.caption("巡检计划已写入；切到「③ 巡检规划」可查看优先级、路线与地图。")


def _render_plan(d: dict, is_mock: bool, start_node: str = "S") -> None:
    status = d.get("status")
    actual_start = d.get("start_node") or start_node
    actual_closed = d.get("closed_edges") or []
    st.markdown(
        f"**规划状态：** `{status}` · {STATUS_ZH.get(status, '')}　"
        f"**总距离：** {d.get('total_distance_m')} m　"
        f"**起点：** {actual_start}　"
        f"**关闭通道：** {'、'.join(actual_closed) if actual_closed else '无'}"
    )
    if is_mock:
        st.caption("数据来源：模拟检测结果 + D 的真实规划算法（路线与距离按真实场景表计算）")
    else:
        st.caption("数据来源：A 模型推理结果 + D 的真实规划算法（当前模型按合成演示口径）")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**优先级顺序**（按 criticality，不等于访问顺序）")
        priority = d.get("priority") or []
        if priority:
            st.table(
                [
                    {
                        "设备": p.get("asset_id") if isinstance(p, dict) else p,
                        "重要等级": p.get("criticality") if isinstance(p, dict) else "—",
                    }
                    for p in priority
                ]
            )
        else:
            st.write("无目标")
        st.markdown("**实际访问顺序**（满足优先级约束的最短路）")
        order = d.get("visit_order") or []
        st.write(" → ".join(str(o) for o in order) if order else "无目标")
    with col2:
        st.markdown("**不可达目标**")
        unreachable = d.get("unreachable_asset_ids") or []
        st.write("、".join(unreachable) if unreachable else "无")
        st.markdown("**完整路线节点**")
        st.code(" → ".join(d.get("route_nodes") or []) or "—", language="text")

    if d.get("notes"):
        st.caption("说明：" + "；".join(d["notes"]))

    map_file = d.get("map_file")
    if map_file:
        png = _abs_asset_path(map_file)
        if png.is_file():
            st.image(str(png), caption=f"路线图：{map_file}", width="stretch")
        else:
            st.warning(f"规划返回了地图路径但文件不存在：{map_file}")


def _render_cd_case_section() -> None:
    """规划案例对照：模拟输入，和 A 的录音检测结果完全分开保存。"""
    st.subheader("规划案例对照（模拟输入）")
    st.caption(
        "这里用团队手工设定的设备状态，只比较两种检查顺序。"
        "它不会覆盖上面的录音检测结果，也不会写进录音检测报告。"
    )
    try:
        bundle = load_cases()
    except CaseFileError as exc:
        st.error(f"案例文件读取失败：{exc}")
        return

    st.info(bundle["disclaimer"])

    labels = {
        f"{case['case_id']} · {case['name']}": case for case in bundle["cases"]
    }
    selected_label = st.selectbox(
        "选择案例",
        list(labels),
        key="cd_case_select",
    )
    case = labels[selected_label]
    if st.session_state.cd_case_id != case["case_id"]:
        st.session_state.cd_case_result = None
        st.session_state.cd_case_error = ""

    st.markdown("**案例条件（团队手工设定）**")
    target_text = "、".join(
        f"{target['asset_id']}（{STATUS_ZH.get(target['status'], target['status'])}）"
        for target in case["targets"]
    )
    st.write(
        f"- 起点：{case['start_node']}；关闭通道："
        f"{'、'.join(case['closed_edges']) if case['closed_edges'] else '无'}"
    )
    st.write(f"- 检查目标：{target_text}")
    if case.get("description"):
        st.caption(case["description"])
    st.caption("目标状态只用于规划演示，不包含模型分数，不能用于评价声音检测准确率。")

    if st.button("运行案例对照", type="primary", key="run_cd_case"):
        st.session_state.cd_case_error = ""
        st.session_state.cd_case_result = None
        try:
            compare = comparison_planner()
            result = compare(
                case_detection_results(case),
                start_node=case["start_node"],
                closed_edges=case["closed_edges"],
                output_dir="runs/cd_cases",
            )
        except Exception as exc:
            st.session_state.cd_case_error = (
                f"案例对照计算失败（{type(exc).__name__}）：{exc}"
            )
        else:
            if result.get("status") == "invalid_input":
                st.session_state.cd_case_error = (
                    "案例对照失败：D 返回 invalid_input。"
                    + "；".join(result.get("notes") or [])
                )
            else:
                st.session_state.cd_case_result = {
                    "case": case,
                    "result": result,
                    "run_id": runlog.new_run_id(),
                    "ran_at": runlog.now_iso(),
                }
                st.session_state.cd_case_id = case["case_id"]

    if st.session_state.cd_case_error:
        st.error(st.session_state.cd_case_error)

    entry = st.session_state.cd_case_result
    if not entry or (entry.get("case") or {}).get("case_id") != case["case_id"]:
        st.caption("选择案例后点击「运行案例对照」，页面会实际调用 D 规划并生成三张图。")
        return

    result = entry["result"]
    baseline = result.get("baseline") or {}
    optimized = result.get("optimized") or {}
    comparison = result.get("comparison") or {}
    unreachable = result.get("unreachable_asset_ids") or []

    st.success(f"案例 {case['case_id']} 计算完成（模拟输入，不是录音检测结果）")
    col_fixed, col_best = st.columns(2)
    with col_fixed:
        st.markdown("**固定顺序（基线）**")
        st.metric("总距离", f"{baseline.get('total_distance_m')} m")
        st.write("访问顺序：" + (" → ".join(baseline.get("visit_order") or []) or "无"))
        st.code(
            " → ".join(baseline.get("route_nodes") or []) or "—",
            language="text",
        )
    with col_best:
        st.markdown("**优化方案**")
        st.metric("总距离", f"{optimized.get('total_distance_m')} m")
        st.write("访问顺序：" + (" → ".join(optimized.get("visit_order") or []) or "无"))
        st.code(
            " → ".join(optimized.get("route_nodes") or []) or "—",
            language="text",
        )

    st.write(f"**不可达设备：** {'、'.join(unreachable) if unreachable else '无'}")
    st.write(
        f"**距离差：** {comparison.get('distance_difference_m')} m　"
        f"**改善比例：** {comparison.get('improvement_text')}"
    )

    explanation = comparison.get("summary") or ""
    if result.get("status") == "partial":
        explanation += (
            f" {'、'.join(unreachable)} 不可达，保留在不可达清单中，"
            "没有进入访问顺序；可达目标继续安排。"
        )
    if comparison.get("same_order"):
        explanation += " 这个案例里两种顺序相同，所以距离相同。"
    st.info(explanation.strip() or "本案例没有产生可比较的路线。")

    map_files = result.get("map_files") or {}
    map_specs = [
        ("优化方案路线图", "optimized_route"),
        ("固定顺序路线图", "fixed_route"),
        ("距离对比图", "comparison_chart"),
    ]
    for caption, key in map_specs:
        rel = map_files.get(key)
        if not rel:
            continue
        png = _abs_asset_path(rel)
        if png.is_file():
            st.image(str(png), caption=f"{caption}：{rel}", width="stretch")
        else:
            st.warning(f"{caption}路径存在但文件不存在：{rel}")

    with st.expander("运行记录（开发用）"):
        st.caption(
            "函数：spatial_module.plan.compare_inspection_plans("
            "detection_results, start_node, closed_edges, output_dir)；"
            "绘图由 C 的 render_route 与 render_plan_comparison 完成。"
        )
        st.code(
            "compare_inspection_plans(\n"
            f"    detection_results=case_detection_results({case['case_id']!r}),\n"
            f"    start_node={case['start_node']!r},\n"
            f"    closed_edges={case['closed_edges']!r},\n"
            "    output_dir='runs/cd_cases',\n"
            ")",
            language="python",
        )
        st.caption(f"运行编号：{entry.get('run_id')} ｜ 时间：{entry.get('ran_at')}")
        timing = result.get("timing") or {}
        timing_rows = [
            {"阶段": "计算（含建网、枚举、基线）", "耗时 ms": timing.get("calculation_total_ms")},
            {"阶段": "其中：优化顺序枚举", "耗时 ms": timing.get("optimization_ms")},
            {"阶段": "其中：固定基线", "耗时 ms": timing.get("baseline_calculation_ms")},
            {"阶段": "绘图合计", "耗时 ms": timing.get("drawing_total_ms")},
            {"阶段": "其中：优化路线图", "耗时 ms": timing.get("optimized_map_ms")},
            {"阶段": "其中：固定路线图", "耗时 ms": timing.get("baseline_map_ms")},
            {"阶段": "其中：对比图", "耗时 ms": timing.get("comparison_chart_ms")},
        ]
        st.table([row for row in timing_rows if row["耗时 ms"] is not None])
        st.caption(
            (timing.get("note") or "")
            + " 这些是单次运行值，不能当作可靠的性能基准。"
        )
        st.json(result)


def _mission_status_text(status: str) -> str:
    return {
        STATUS_ACTIVE: "进行中",
        STATUS_BLOCKED: "受阻（有目标不可达）",
        STATUS_COMPLETED: "巡检已完成",
        STATUS_NO_TARGETS: "无需巡检",
    }.get(status, status)


def _mission_device_text(assets: list[str]) -> str:
    return "、".join(assets) if assets else "无"


def _mission_create_form(
    rows: list[dict],
    start_node: str,
    closed_ids: list[str],
    edge_options: list[str],
    edge_labels: dict[str, str],
    *,
    has_mission: bool,
    mission: dict | None,
) -> None:
    """新建任务表单；有任务时放在“替换”折叠区，必须再次确认。"""
    try:
        bundle = load_cases()
    except CaseFileError as exc:
        st.error(f"模拟案例读取失败：{exc}")
        return

    detection_label = "当前录音检测结果"
    case_labels = {
        f"{case['case_id']} · {case['name']}（手工模拟）": case
        for case in bundle["cases"]
    }
    options = [detection_label] + list(case_labels)
    selected = st.selectbox("任务来源", options, key="mission_source_select")

    if selected == detection_label:
        source_kind = "detection"
        source_rows = list(rows)
        source_label = (
            f"当前录音检测结果（{'模拟数据' if st.session_state.detections_is_mock else 'A 模型推理'}）"
        )
        source_signature = st.session_state.get("_current_input_signature", "")
        source_note = "来自当前有效录音检测结果；检测输入变化后任务会失效，需要重新创建。"
        is_mock = bool(st.session_state.detections_is_mock)
        task_start = start_node
        task_closed = list(closed_ids)
        case_id = ""
        if not source_rows:
            st.warning("当前没有检测结果，不能创建录音来源的任务。")
        else:
            st.caption(
                f"使用上方当前起点 {start_node} 和关闭通道 "
                f"{'、'.join(closed_ids) if closed_ids else '无'}；"
                f"检测结果 {len(source_rows)} 条。"
            )
    else:
        case = case_labels[selected]
        source_kind = "simulation_case"
        source_rows = case_detection_results(case)
        source_label = f"{case['case_id']} · {case['name']}（手工模拟案例）"
        source_signature = ""
        source_note = bundle["disclaimer"]
        is_mock = True
        task_start = case["start_node"]
        task_closed = list(case["closed_edges"])
        case_id = case["case_id"]
        st.info(bundle["disclaimer"])
        st.caption(
            f"案例起点 {task_start}；关闭通道 "
            f"{'、'.join(task_closed) if task_closed else '无'}；"
            f"目标 {'、'.join(t['asset_id'] for t in case['targets'])}。"
        )

    confirmed = True
    if has_mission:
        confirmed = st.checkbox(
            "我确认结束/替换当前任务，用上面的来源新建任务",
            key=f"mission_replace_confirm_{(mission or {}).get('mission_id', 'none')}",
        )
        st.warning("新建会替换当前会话中的任务状态；当前任务的导出请先下载。")

    can_create = bool(source_rows) and confirmed
    if st.button(
        "创建并开始巡检任务",
        type="primary",
        disabled=not can_create,
        key="mission_create",
    ):
        try:
            new_mission = create_mission(
                source_rows,
                source_kind=source_kind,
                source_label=source_label,
                is_mock=is_mock,
                source_signature=source_signature,
                case_id=case_id,
                source_note=source_note,
                start_node=task_start,
                closed_edges=task_closed,
                output_dir="runs/missions",
            )
        except MissionError as exc:
            st.session_state.mission_error = f"创建巡检任务失败：{exc}"
        except Exception as exc:
            st.session_state.mission_error = (
                f"创建巡检任务失败（{type(exc).__name__}）：{exc}"
            )
        else:
            st.session_state.mission = new_mission
            st.session_state.mission_error = ""
            st.session_state.mission_notice = (
                f"任务 {new_mission['mission_id']} 已创建："
                f"当前位置 {new_mission['current_node']}，"
                f"待检 {_mission_device_text(new_mission['pending_assets'])}。"
            )
            st.session_state._pending_mission_closed_edges = list(
                new_mission["closed_edges"]
            )
            st.rerun()


def _render_mission_execution(
    rows: list[dict],
    start_node: str,
    closed_ids: list[str],
    edge_options: list[str],
    edge_labels: dict[str, str],
) -> None:
    st.subheader("巡检执行（模拟）")
    st.caption(
        "记录当前位置、已完成设备、待检设备和已走距离。"
        "前进只能走相邻的开放通道；到达设备节点后由人员确认，不自动完成。"
    )

    mission = st.session_state.mission
    if st.session_state.mission_notice:
        st.info(st.session_state.mission_notice)
    if st.session_state.mission_error:
        st.error(st.session_state.mission_error)

    if not isinstance(mission, dict):
        st.caption("当前没有巡检任务。可以从有效录音检测结果或 C1/C2/C3 手工模拟案例创建。")
        _mission_create_form(
            rows,
            start_node,
            closed_ids,
            edge_options,
            edge_labels,
            has_mission=False,
            mission=None,
        )
        return

    source = mission.get("source") or {}
    stale = mission_is_stale(
        mission,
        st.session_state.get("_current_input_signature", ""),
        cd_state.detection_result_signature(rows),
    )
    status = mission.get("status")
    current = mission.get("current_node")
    walked = float(mission.get("walked_distance_m") or 0.0)
    remaining = float(mission.get("remaining_distance_m") or 0.0)
    expected = mission.get("expected_total_distance_m")
    pending = list(mission.get("pending_assets") or [])
    completed = list(mission.get("completed_assets") or [])
    unreachable = list(mission.get("unreachable_assets") or [])
    closed = list(mission.get("closed_edges") or [])
    plan = mission.get("remaining_plan") or {}

    st.markdown(
        f"**任务状态：** `{status}` · {_mission_status_text(status)}　"
        f"**任务编号：** `{mission.get('mission_id')}`"
    )
    st.caption(
        f"来源：{source.get('label')} ｜ "
        f"{'模拟输入（不是 A 的录音检测结果）' if source.get('is_mock') else 'A 模型推理结果'}"
    )
    if source.get("source_note"):
        st.caption(source["source_note"])

    if stale:
        st.error(
            "关联录音检测输入已变化：当前任务已失效，不能继续使用新检测数据执行。"
            "请先导出记录，再在下方新建任务。"
        )

    invalid_notes = source.get("invalid_input_notes") or []
    if invalid_notes:
        st.warning("无效录音需要补录（没有被当成正常）：" + "；".join(invalid_notes))

    if status == STATUS_COMPLETED:
        st.success(
            "巡检已完成：所有任务目标都已由人员确认检查完成。"
            "这不代表故障已维修或设备恢复正常。"
        )
    elif status == STATUS_NO_TARGETS:
        st.info("无需巡检：本次输入没有疑似异常目标。")
    elif status == STATUS_BLOCKED:
        st.error(
            "任务受阻：待检目标全部不可达，剩余可行行走距离为 0，"
            "但任务没有完成。可以恢复通道后重新规划。"
        )
    elif unreachable:
        st.warning(
            "任务尚未完成：还有不可达目标。下面的剩余距离只覆盖当前可达目标，"
            "不能当作完成全部任务所需的总里程。"
        )
    else:
        st.info("任务进行中：到达设备节点后请人工确认检查完成。")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("当前位置", current or "—")
    col2.metric("累计已走", f"{walked:g} m")
    col3.metric(
        "当前可达目标剩余距离" if unreachable else "剩余距离",
        f"{remaining:g} m",
    )
    col4.metric(
        "预计总里程",
        f"{float(expected):g} m" if expected is not None else "不可用",
    )

    st.markdown(f"**待检设备（含不可达）：** {_mission_device_text(pending)}")
    st.markdown(f"**已完成设备：** {_mission_device_text(completed)}")
    st.markdown(f"**不可达设备：** {_mission_device_text(unreachable)}")
    st.markdown(f"**关闭通道：** {_mission_device_text(closed)}")
    route = list(plan.get("route_nodes") or [])
    st.markdown("**当前剩余路线：**")
    st.code(" → ".join(route) if route else "—", language="text")

    map_file = plan.get("map_file")
    if map_file:
        png = _abs_asset_path(map_file)
        if png.is_file():
            st.image(str(png), caption=f"当前剩余路线图：{map_file}", width="stretch")
        else:
            st.warning(f"剩余路线图路径存在但文件不存在：{map_file}")

    actions_disabled = stale or status in (STATUS_COMPLETED, STATUS_NO_TARGETS)
    asset_nodes = {item["asset_id"]: item["node_id"] for item in list_assets()}
    here = [asset_id for asset_id in pending if asset_nodes.get(asset_id) == current]

    if not actions_disabled:
        if st.button(
            "沿当前路线前进一步",
            key="mission_step",
            disabled=status != STATUS_ACTIVE or len(route) < 2 or bool(here),
            help="当前节点有待确认设备时，必须先确认检查完成才能继续走。",
        ):
            try:
                new_mission = step_forward(mission)
            except MissionError as exc:
                st.session_state.mission_error = f"前进一步失败：{exc}"
            else:
                asset_nodes = {
                    item["asset_id"]: item["node_id"] for item in list_assets()
                }
                arrived = [
                    asset_id
                    for asset_id in new_mission["pending_assets"]
                    if asset_nodes.get(asset_id) == new_mission["current_node"]
                ]
                notice = (
                    f"已走到 {new_mission['current_node']}，"
                    f"累计已走 {new_mission['walked_distance_m']:g} m。"
                )
                if arrived:
                    notice += f" 这里是 {'、'.join(arrived)} 的节点，请确认检查完成。"
                st.session_state.mission = new_mission
                st.session_state.mission_error = ""
                st.session_state.mission_notice = notice
                st.rerun()

        if here:
            confirm_target = st.selectbox(
                "当前节点可确认的设备",
                here,
                key="mission_confirm_asset",
            )
            if st.button(
                "确认当前设备检查完成",
                key="mission_confirm",
                disabled=stale,
            ):
                try:
                    new_mission = confirm_asset(
                        mission,
                        confirm_target,
                        output_dir="runs/missions",
                    )
                except MissionError as exc:
                    st.session_state.mission_error = f"确认检查失败：{exc}"
                else:
                    st.session_state.mission = new_mission
                    st.session_state.mission_error = ""
                    st.session_state.mission_notice = (
                        f"已确认 {confirm_target} 检查完成；"
                        f"累计已走 {new_mission['walked_distance_m']:g} m，"
                        f"待检 {_mission_device_text(new_mission['pending_assets'])}。"
                    )
                    st.rerun()
        else:
            st.caption("当前节点没有可确认的设备。")

        st.markdown("**任务封路状态**")
        mission_closed = st.multiselect(
            "当前任务关闭通道",
            edge_options,
            format_func=lambda edge_id: edge_labels.get(edge_id, edge_id),
            key="mission_closed_edges",
            help="只影响当前巡检任务；应用后会从当前位置重算剩余路线。",
        )
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button(
                "应用封路并重新规划",
                key="mission_replan",
                disabled=stale or status == STATUS_COMPLETED,
            ):
                try:
                    new_mission = replan_mission(
                        mission,
                        closed_edges=mission_closed,
                        output_dir="runs/missions",
                    )
                except MissionError as exc:
                    st.session_state.mission_error = f"任务重规划失败：{exc}"
                else:
                    st.session_state.mission = new_mission
                    st.session_state.mission_error = ""
                    st.session_state.mission_notice = (
                        f"已从 {new_mission['current_node']} 重规划："
                        f"{_mission_status_text(new_mission['status'])}，"
                        f"剩余 {new_mission['remaining_distance_m']:g} m，"
                        f"已走仍为 {new_mission['walked_distance_m']:g} m。"
                    )
                    st.rerun()
        with col_b:
            if st.button(
                "按当前封路重算（不移动）",
                key="mission_replan_same",
                disabled=stale or status == STATUS_COMPLETED,
            ):
                try:
                    new_mission = replan_mission(
                        mission,
                        closed_edges=closed,
                        output_dir="runs/missions",
                    )
                except MissionError as exc:
                    st.session_state.mission_error = f"任务重规划失败：{exc}"
                else:
                    st.session_state.mission = new_mission
                    st.session_state.mission_error = ""
                    st.session_state.mission_notice = (
                        f"已重新计算剩余路线，已走距离仍为 "
                        f"{new_mission['walked_distance_m']:g} m。"
                    )
                    st.rerun()

    st.caption(COMPLETION_NOTE)

    col_export, col_clear = st.columns(2)
    with col_export:
        st.download_button(
            "导出任务 JSON",
            mission_to_json(mission).encode("utf-8"),
            file_name=f"mission_{mission.get('mission_id')}.json",
            mime="application/json",
            key="mission_download",
        )
    with col_clear:
        if st.button(
            "清除当前任务（不影响检测结果）",
            key="mission_clear",
        ):
            st.session_state.mission = None
            st.session_state.mission_error = ""
            st.session_state.mission_notice = "当前任务已从本会话清除；检测结果未改变。"
            st.session_state._pending_mission_closed_edges = []
            st.rerun()

    with st.expander("任务原始 JSON（开发用）"):
        st.json(mission)

    st.divider()
    with st.expander("新建巡检任务（会替换当前任务）", expanded=False):
        _mission_create_form(
            rows,
            start_node,
            closed_ids,
            edge_options,
            edge_labels,
            has_mission=True,
            mission=mission,
        )


# ---------------------------------------------------------------------------
# 侧边栏
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("数据来源")
    mode_label = st.radio(
        "运行模式",
        options=["模拟数据（mock）", "真实模型（real）"],
        index=0 if st.session_state.mode == "mock" else 1,
        key="run_mode",
        help="real 模式在进程内调用 A 的 pump_v1 模型与 D 的规划算法；mock 只模拟检测分数，规划仍按真实场景计算。",
    )
    st.session_state.mode = "mock" if mode_label.startswith("模拟") else "real"

    st.divider()
    st.header("Agent 决策器")
    _cfg = llm_config()
    # 未配置 LLM 时不提供 LLM 相关选项：那些模式必然失败，留着只会让人误点
    _planner_keys = list(PLANNER_LABELS) if _cfg.configured else ["rule"]
    if st.session_state.planner not in _planner_keys:
        st.session_state.planner = "rule" if not _cfg.configured else "auto"
    _planner_label = st.radio(
        "决策器",
        options=[PLANNER_LABELS[k] for k in _planner_keys],
        index=_planner_keys.index(st.session_state.planner),
        help="LLM 只负责把任务翻译成工具调用序列；检测与规划结果始终来自 A / D 模块。",
    )
    st.session_state.planner = _planner_keys[[PLANNER_LABELS[k] for k in _planner_keys].index(_planner_label)]

    if _cfg.configured:
        st.caption(f"✅ LLM 已配置：`{_cfg.model}`（密钥 {_cfg.key_hint}）")
        st.caption(f"接口 {_cfg.api_base} ｜ 超时 {_cfg.timeout:g}s")
    else:
        st.caption("⚠️ LLM 未配置 → **Agent 的 LLM 模式不可用**，当前只能跑规则版决策器")
        st.caption("需要设置：" + "、".join(_cfg.missing))
        st.caption("配好后跑 `check_llm.py` 自检，通过再重启应用。")

    st.divider()
    st.header("Agent 预算（硬约束）")
    st.caption(f"决策轮次上限 {MAX_ROUNDS} ｜ 工具执行上限 {MAX_TOOL_CALLS}")
    usage = st.session_state.agent_usage
    st.progress(min(usage["rounds"] / MAX_ROUNDS, 1.0))
    st.caption(f"累计轮次 {usage['rounds']} / {MAX_ROUNDS}")
    st.progress(min(usage["calls"] / MAX_TOOL_CALLS, 1.0))
    st.caption(f"累计工具调用 {usage['calls']} / {MAX_TOOL_CALLS}")
    if st.button("重置计数"):
        st.session_state.agent_usage = {"rounds": 0, "calls": 0}
        st.rerun()

    st.divider()
    st.header("模块状态")
    for chk in check_modules():
        st.caption(("✅ " if chk["ok"] else "❌ ") + f"{chk['name']}：{chk['detail']}")

# ---------------------------------------------------------------------------
# 页头
# ---------------------------------------------------------------------------
st.title("海工智维 · 设备运维助手")
st.caption("声学B（应用与 Agent）｜ 单页主入口 · 页内五区 ｜ 模型 A · 规划 D · 绘图 C")

if st.session_state.mode == "mock":
    st.markdown(
        '<div style="border:2px solid #d93025;background:#fdecea;color:#a50e0e;'
        "padding:10px 16px;border-radius:8px;font-size:15px;font-weight:600;\">"
        "⚠️ 模拟模式：检测分数为本地演示数据，未调用 A 的模型。"
        "巡检路线由 D 的真实算法基于这些模拟结果计算，整页内容不得作为正式结论引用"
        "</div>",
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        '<div style="border:2px solid #188038;background:#e6f4ea;color:#0d652d;'
        "padding:10px 16px;border-radius:8px;font-size:15px;font-weight:600;\">"
        "真实模型模式：调用 A 的 pump_v1 与 D 的规划算法；调用失败会直接报错，"
        "绝不用模拟数据兜底、绝不编造结果"
        "</div>",
        unsafe_allow_html=True,
    )

tab0, tab1, tab2, tab3, tab4 = st.tabs(
    ["① 总览", "② 音频检测", "③ 巡检规划", "④ 检查资料", "⑤ Agent 助手"]
)

# ---------------------------------------------------------------------------
# ① 总览
# ---------------------------------------------------------------------------
with tab0:
    st.subheader("模块状态（实测，不是声明）")
    st.caption("逐项探测模型文件、A 推理模块、D 规划 + C 绘图、场景表；任一项失败会在页面直接显示原因。")
    checks = check_modules()
    st.table(
        [
            {"检查项": c["name"], "结果": "通过" if c["ok"] else "失败", "详情": c["detail"]}
            for c in checks
        ]
    )
    if st.button("重新探测"):
        audio_analyzer.cache_clear()
        st.rerun()

    st.caption(
        "来源口径：当前随包模型与两条演示录音按团队「合成演示」说明，"
        "不能据此声称已经完成 MIMII 真实数据训练；MIMII 仅作为计划采用的公开数据集参考。"
        "本页的规划案例是团队手工设定的模拟输入，不是录音检测结果。"
    )

    st.subheader("三个工具（不多不少）")
    st.table(TOOL_SPECS)

    st.subheader("决策器（负责决定调哪个工具，不参与出结果）")
    st.caption(
        "LLM 只把自然语言任务翻译成工具调用序列；检测分数、路线、距离全部来自 A / D 模块。"
        "LLM 输出要过白名单校验（工具名、设备编号、通道编号、节点、顺序），"
        "校验不过一律回退规则版，并在执行轨迹里写明原因。"
    )
    st.table(
        [
            {"决策器": "自动（默认）", "行为": "LLM 优先；未配置 / 超时 / 校验失败 → 回退规则版"},
            {"决策器": "仅 LLM", "行为": "不做任何回退，失败直接报错停下"},
            {"决策器": "仅规则版", "行为": "不调用外部模型，纯关键词解析，可离线演示"},
        ]
    )
    _cur = llm_config()
    if _cur.configured:
        st.caption(
            f"✅ 当前接的是 `{_cur.model}`（{_cur.api_base}），密钥 {_cur.key_hint}，"
            f"超时 {_cur.timeout:g}s"
        )
    else:
        st.info(
            "当前未配置 LLM，Agent 走规则版决策器，功能完整、可离线演示。"
            "要接真实模型：在 .streamlit/secrets.toml 里填 LLM_API_KEY，"
            "再跑 check_llm.py 自检，通过后重启本应用。"
        )

    st.subheader("数据流")
    st.code(
        "上传 WAV ──► A.analyze_audio(path, asset_id) ──► 检测结果列表（schema 1.0）\n"
        "                                                     │\n"
        "                                                     ▼\n"
        "        D.plan_inspection(detection_results, closed_edges) ──► 路线 + 距离\n"
        "                                                     │\n"
        "                                                     ▼\n"
        "                              C.render_route(scene, route_nodes) ──► 地图 PNG",
        language="text",
    )

    st.subheader("代码结构")
    st.code(
        "app.py                 单页应用主入口（本文件）\n"
        "agent/loop.py          单 Agent 主循环（6 轮 / 12 次预算 + 工具依赖链）\n"
        "agent/tools.py         三个工具（detect / plan / checklist）\n"
        "core/local_modules.py  三家模块的唯一 import 入口（A / C / D）\n"
        "audio_module/  models/  samples/     A 交付（本轮未改动）\n"
        "spatial_module/  scene/              C/D（兼容扩展，公开入口保留）\n"
        "spatial_module/mission.py 巡检执行任务（行走/确认/重规划/导出）\n"
        "core/cd_state.py      输入指纹、检测结果指纹与规划上下文比较\n"
        "core/cd_cases.py      模拟规划案例读取\n"
        "examples/             案例 JSON + 运行脚本\n"
        "check_cd.py           第一轮检查\n"
        "check_cd_mission.py   第二轮巡检执行检查\n"
        "knowledge/              本地检查资料",
        language="text",
    )

    st.subheader("里程碑")
    st.table(
        [
            {"里程碑": "空应用上线", "状态": "已完成（已发布公网链接）", "负责": "B"},
            {"里程碑": "单页主入口与页面框架", "状态": "已完成（单页五区）", "负责": "B"},
            {"里程碑": "A / C / D 模块交付", "状态": "已完成（三家自检全过）", "负责": "A / C / D"},
            {"里程碑": "端到端联调", "状态": "已完成（A→D→C 链路通）", "负责": "B"},
            {"里程碑": "Agent 决策器接入 LLM", "状态": "已完成（校验 + 回退）", "负责": "B"},
            {"里程碑": "整体验收", "状态": "待完成", "负责": "全员"},
            {"里程碑": "代码冻结", "状态": "待完成", "负责": "全员"},
        ]
    )

# ---------------------------------------------------------------------------
# ② 音频检测
# ---------------------------------------------------------------------------
with tab1:
    st.subheader("设备音频批量检测")
    st.caption(
        f"首版只接受 {EXPECTED_SAMPLE_RATE} Hz 单声道 WAV，时长 ≥ 1 秒，不做重采样。"
        "上传仅用于检测，不会触发任何训练；批量检测无论文件数量，Agent 侧只计 1 次工具调用。"
    )

    source = st.radio(
        "音频来源",
        [
            "内置演示录音（P01 正常 / P04 异常）",
            "全场景模拟（P01–P06，仅模拟模式）",
            "上传文件",
        ],
        horizontal=True,
        key="audio_source",
    )

    pending_items: list[dict] = []
    blocked_reason = ""

    if source.startswith("内置"):
        samples = sample_files()
        if not samples:
            st.warning("samples/ 目录下没有演示录音。")
        else:
            pending_items = [
                {
                    "asset_id": p.stem.split("_")[0],
                    "path": str(p),
                    "filename": p.name,
                }
                for p in samples
            ]
            st.table([{"设备编号": it["asset_id"], "文件": it["filename"]} for it in pending_items])
    elif source.startswith("全场景"):
        if st.session_state.mode == "real":
            blocked_reason = "全场景模拟只提供演示分数；真实模型模式需要实际音频文件，请切到模拟模式或改用上传。"
            st.warning(blocked_reason)
        pending_items = [
            {"asset_id": a, "path": "", "filename": f"{a}_sim.wav"} for a in asset_ids()
        ]
        st.table(
            [
                {"设备编号": a, "重要等级": c, "说明": "无音频，仅生成演示分数"}
                for a, c in [(x["asset_id"], x["criticality"]) for x in list_assets()]
            ]
        )
    else:
        uploaded = st.file_uploader(
            "上传音频文件（可多选）", type=["wav"], accept_multiple_files=True
        )
        if uploaded:
            options = asset_ids()
            st.caption("为每个文件指定设备编号（A 不从文件名推断编号）：")
            for idx, f in enumerate(uploaded):
                stem = Path(f.name).stem.split("_")[0].upper()
                default = stem if stem in options else options[0]
                col1, col2 = st.columns([1, 2])
                with col1:
                    asset = st.selectbox(
                        f"设备编号（{f.name}）",
                        options,
                        index=options.index(default),
                        key=f"asset_pick_{idx}_{f.name}",
                    )
                with col2:
                    st.caption(f"文件大小 {len(f.getbuffer()) / 1024:.1f} KB")
                pending_items.append(
                    {
                        "asset_id": asset,
                        "path": "",
                        "filename": f.name,
                        "content_sha256": cd_state.sha256_bytes(bytes(f.getbuffer())),
                        "_upload": f,
                    }
                )

    current_input_signature = _pending_signature(
        source, st.session_state.mode, pending_items
    )
    st.session_state._current_input_signature = current_input_signature

    # A：来源 / 运行模式 / 设备映射 / 文件内容任一变化，旧检测、旧路线、旧回答都失效。
    # 同名文件也靠内容哈希识别，不能只看文件名。
    if (
        (
            st.session_state.detections
            or st.session_state.plan_result
            or st.session_state.last_agent
            or st.session_state.detect_error
        )
        and st.session_state.detection_input_signature != current_input_signature
    ):
        _clear_detection_state(
            "音频来源、设备映射、文件内容或运行模式已变化："
            "旧检测结果、旧路线和旧任务回答已清除，请重新检测。"
        )
        st.session_state.plan_notice = ""
        st.session_state.plan_error = ""

    if st.session_state.detect_notice:
        st.info(st.session_state.detect_notice)
    if st.session_state.detect_error:
        st.error(st.session_state.detect_error)
        st.caption("旧检测结果没有继续显示；请按错误提示修正输入后重新检测。")

    if st.button(
        "开始检测", type="primary", disabled=not pending_items, key="run_detect"
    ):
        items = []
        for it in pending_items:
            if it.get("_upload") is not None:
                items.append(
                    {
                        "asset_id": it["asset_id"],
                        "path": _stage_upload(it["_upload"]),
                        "filename": it["filename"],
                    }
                )
            else:
                items.append(
                    {"asset_id": it["asset_id"], "path": it["path"], "filename": it["filename"]}
                )

        # 先清旧结果再调用：即使本次失败，也不会让上一次的检测/路线继续假装是本轮结果。
        _clear_detection_state("", notice=False)
        st.session_state.detect_notice = ""
        st.session_state.plan_notice = ""
        st.session_state.detect_items = items
        st.session_state.detection_input_signature = current_input_signature

        run_id = runlog.new_run_id()
        started_at = runlog.now_iso()
        with st.spinner(f"检测中（{len(items)} 个文件，计 1 次工具调用）……"):
            result = detect_assets_audio(
                asset_ids=[it["asset_id"] for it in items],
                files=items,
                mode=st.session_state.mode,
            )
        finished_at = runlog.now_iso()

        st.session_state.detect_run_id = run_id
        log_payload = {
            "run_id": run_id,
            "kind": "detect",
            "started_at": started_at,
            "finished_at": finished_at,
            "mode": st.session_state.mode,
            "tool": "detect_assets_audio",
            "arguments": {"asset_ids": [it["asset_id"] for it in items]},
            "ok": result.ok,
            "status": "ok" if result.ok else "failed",
            "error": "" if result.ok else result.message,
        }
        if isinstance(result.data, dict):
            log_payload.update({
                "requested_asset_ids": result.data.get("requested_asset_ids"),
                "missing_asset_ids": result.data.get("missing_asset_ids"),
                "abnormal": result.data.get("abnormal"),
            })
        st.session_state.detect_log_path = runlog.write_run_log(log_payload)
        if result.ok:
            st.session_state.detections = result.data["results"]
            st.session_state.detections_is_mock = result.is_mock
            st.session_state.detect_error = ""
            st.session_state.plan_notice = ""
            if result.data["missing_asset_ids"]:
                st.warning(
                    "以下设备没有对应音频，本次未检测（不会当作正常）："
                    + "、".join(result.data["missing_asset_ids"])
                )
        else:
            st.session_state.detections = []
            st.session_state.detections_is_mock = False
            st.session_state.detect_error = f"检测失败：{result.message}"
            st.error(st.session_state.detect_error)
            st.caption("旧检测结果已清除；失败时不会用模拟数据兜底。")

    # 展示最近一次结果（rerun 后仍保留）
    rows = st.session_state.detections
    if rows:
        st.divider()
        st.markdown(
            f"**检测结果**（{'模拟数据' if st.session_state.detections_is_mock else 'A 的真实推理'}）"
            f" · 共 {len(rows)} 条 · 疑似异常 "
            f"{sum(1 for r in rows if r.get('status') == 'suspected_anomaly')} 条"
        )
        st.table(_detect_table(rows))

        invalid = [r for r in rows if r.get("status") == "invalid_input"]
        if invalid:
            st.warning(
                "以下文件输入不合法，未参与判定（不会算作「正常」）："
                + "；".join(
                    f"{r['asset_id']}（{(r.get('quality') or {}).get('filename', '—')}）"
                    for r in invalid
                )
            )

        if st.session_state.detect_run_id:
            st.caption(
                f"本次运行编号：`{st.session_state.detect_run_id}`"
                + (f" ｜ 日志：`{st.session_state.detect_log_path}`" if st.session_state.detect_log_path else "")
            )

        col_csv, col_md = st.columns(2)
        with col_csv:
            st.download_button(
                "导出检测结果 CSV",
                _to_csv_bytes(_detect_table(rows)),
                file_name=f"detect_{st.session_state.detect_run_id or 'result'}.csv",
                mime="text/csv",
            )
        with col_md:
            st.download_button(
                "下载完整报告（Markdown）",
                build_markdown_report(
                    rows,
                    st.session_state.detections_is_mock,
                    st.session_state.detect_run_id,
                    st.session_state.detect_log_path,
                    plan=st.session_state.plan_result,
                    plan_is_mock=st.session_state.plan_is_mock,
                    start_node=st.session_state.get("plan_used_start_node", "S"),
                    closed_edges=list(
                        st.session_state.get("plan_used_closed_edges") or []
                    ),
                    expected_sample_rate=EXPECTED_SAMPLE_RATE,
                ).encode("utf-8"),
                file_name=f"report_{st.session_state.detect_run_id or 'result'}.md",
                mime="text/markdown",
            )

        spectrums = [r for r in rows if r.get("plot_file")]
        if spectrums:
            st.markdown("**频谱图**")
            cols = st.columns(min(len(spectrums), 3))
            for i, r in enumerate(spectrums):
                png = _abs_asset_path(r["plot_file"])
                if png.is_file():
                    cols[i % len(cols)].image(str(png), caption=f"{r['asset_id']} · score={r.get('score')}")
        st.caption("分数是模型给出的相对异常程度，不是故障概率，也不判断具体损坏部件。")

# ---------------------------------------------------------------------------
# ③ 巡检规划
# ---------------------------------------------------------------------------
with tab2:
    st.subheader("巡检规划（按检测结果排路线）")
    rows = st.session_state.detections
    edges = list_edges()
    edge_labels = {
        e["edge_id"]: f"{e['edge_id']}（{e['from_node']}→{e['to_node']}）"
        for e in edges
    }
    edge_options = [e["edge_id"] for e in edges]

    if not rows:
        st.warning("尚无检测结果。巡检规划只按 A 的检测结果确定目标，请先在「② 音频检测」执行一次检测。")
    else:
        st.caption(
            f"当前检测结果 {len(rows)} 条（"
            f"{'模拟数据' if st.session_state.detections_is_mock else '真实推理'}），"
            "疑似异常的设备才会成为巡检目标。"
        )

    col1, col2 = st.columns(2)
    with col1:
        closed_ids = st.multiselect(
            "临时关闭通道（可选）",
            edge_options,
            format_func=lambda edge_id: edge_labels.get(edge_id, edge_id),
            key="plan_closed_edges",
            help="关闭后通行能力降级或目标不可达，可用于演示 partial（部分达成）。",
        )
    with col2:
        start_node = st.selectbox(
            "起点节点",
            node_ids(),
            key="plan_start_node",
        )

    # E：起点/关闭通道变化时，检测结果保留，但旧路线立即失效。
    if st.session_state.plan_result and not cd_state.same_plan_context(
        st.session_state.plan_context, start_node, closed_ids
    ):
        _clear_plan_state(
            "起点或关闭通道已变化：旧路线已清除，检测结果仍保留，请重新规划。"
        )
    if st.session_state.plan_notice:
        st.info(st.session_state.plan_notice)
    if st.session_state.plan_error:
        st.error(st.session_state.plan_error)

    if st.button(
        "生成巡检计划", type="primary", disabled=not rows, key="run_plan"
    ):
        # 先清旧路线：规划失败时不能继续显示上一次的结果。
        _clear_plan_state("", notice=False)
        st.session_state.plan_notice = ""
        st.session_state.plan_error = ""
        with st.spinner("规划中（含路线枚举与绘图）……"):
            result = plan_asset_inspection(
                detection_results=rows,
                closed_edges=closed_ids,
                start_node=start_node,
                input_is_mock=st.session_state.detections_is_mock,
            )
        if (
            result.ok
            and isinstance(result.data, dict)
            and result.data.get("status") != "invalid_input"
        ):
            st.session_state.plan_result = result.data
            st.session_state.plan_is_mock = result.is_mock
            actual_start = result.data.get("start_node") or start_node
            actual_closed = list(
                result.data.get("closed_edges")
                if "closed_edges" in result.data
                else closed_ids
            )
            st.session_state.plan_used_start_node = actual_start
            st.session_state.plan_used_closed_edges = actual_closed
            st.session_state.plan_context = cd_state.plan_context(
                actual_start, actual_closed
            )
            st.session_state.plan_error = ""
        else:
            st.session_state.plan_result = None
            message = (
                "D 返回 invalid_input" if result.ok else result.message
            )
            st.session_state.plan_error = f"规划失败：{message}"
            st.error(st.session_state.plan_error)

    if st.session_state.plan_result:
        st.divider()
        _render_plan(
            st.session_state.plan_result,
            st.session_state.plan_is_mock,
            st.session_state.get("plan_used_start_node", "S"),
        )

        baseline = st.session_state.plan_result.get("baseline") or {}
        if baseline:
            with st.expander("同条件固定顺序基线（D 内部计算，供对照）"):
                st.write(
                    "访问顺序："
                    + (" → ".join(baseline.get("visit_order") or []) or "无")
                )
                st.write(f"总距离：{baseline.get('total_distance_m')} m")
                st.caption(
                    "固定基线按重要等级降序、同等级按设备编号升序；"
                    "与上面的优化方案使用同一起点、同一批可达目标和同样的开放通道。"
                )

        with st.expander("规划结果原始 JSON"):
            st.json(st.session_state.plan_result)

    st.divider()
    if st.button("只看场景地图（不做规划）", key="preview_scene"):
        from core.local_modules import ModuleUnavailable, route_renderer

        try:
            rel = route_renderer()(
                scene_dir="scene",
                route_nodes=[start_node],
                output_path=f"runs/preview/scene_{uuid.uuid4().hex[:8]}.png",
                closed_edges=closed_ids,
            )
            png = _abs_asset_path(rel)
            if png.is_file():
                st.image(str(png), caption=f"场景地图：{rel}", width="stretch")
            else:
                st.warning(f"绘图返回路径但文件不存在：{rel}")
        except ModuleUnavailable as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(f"C 绘图失败（{type(exc).__name__}）：{exc}")

    st.divider()
    _render_mission_execution(
        rows,
        start_node,
        closed_ids,
        edge_options,
        edge_labels,
    )

    st.divider()
    _render_cd_case_section()

# ---------------------------------------------------------------------------
# ④ 检查资料
# ---------------------------------------------------------------------------
with tab3:
    st.subheader("本地检查资料")
    st.caption(
        "数据源为 knowledge/checklist.json，字段固定：编号 / 标题 / 来源链接 / 适用条件 / 检查建议。"
        "kind=public 为公开资料引用，kind=team 为团队自拟规则（已在条目上标注）。"
    )
    keyword = st.text_input("检索关键词（留空显示全部）", value="")
    result = get_checklist(query=keyword.strip(), mode=st.session_state.mode)
    if not result.ok:
        st.error(f"知识库读取失败：{result.message}")
        items = []
    else:
        items = result.data["items"]
    if not items:
        st.warning("没有匹配的检查条目。")
    else:
        st.table(
            [
                {
                    "编号": it.get("source_id", ""),
                    "标题": it.get("title", ""),
                    "来源": "公开资料" if it.get("kind") == "public" else "团队设定",
                    "适用条件": it.get("conditions", ""),
                    "检查建议": it.get("advice", ""),
                    "链接": it.get("url") or "—",
                }
                for it in items
            ]
        )
        st.caption("团队设定条目为自拟规则，不代表行业标准；公开条目的链接需人工核对一次再引用。")

# ---------------------------------------------------------------------------
# ⑤ Agent 助手
# ---------------------------------------------------------------------------
with tab4:
    st.subheader("Agent 助手（单 Agent · 三个工具）")
    st.caption(
        "工具：detect_assets_audio（调 A）｜ plan_asset_inspection（调 D）｜ get_checklist（查本地资料）。"
        f"单任务上限 {MAX_ROUNDS} 轮决策 / {MAX_TOOL_CALLS} 次工具执行。"
    )

    files_ctx = st.session_state.detect_items
    if files_ctx:
        st.caption("Agent 可直接使用页面上已送检的音频：" + "、".join(
            f"{i['asset_id']}({i['filename']})" for i in files_ctx
        ))
    else:
        st.caption("提示：先在「② 音频检测」选定音频，Agent 才能执行检测任务。")

    question = st.text_input(
        "描述任务",
        placeholder="例：检测 P01 和 P04，然后关闭 E04 出一条巡检路线",
        key="agent_question",
    )
    if st.button(
        "交给 Agent",
        type="primary",
        disabled=not question.strip(),
        key="run_agent",
    ):
        agent_input_signature = st.session_state.get("_current_input_signature", "")
        active_mission = (
            st.session_state.mission
            if isinstance(st.session_state.mission, dict)
            else None
        )
        agent_mission_stale = mission_is_stale(
            active_mission,
            agent_input_signature,
            cd_state.detection_result_signature(st.session_state.detections),
        )
        context = {
            "files": files_ctx,
            "detections": st.session_state.detections,
            "detections_is_mock": st.session_state.detections_is_mock,
            "start_node": (
                active_mission.get("current_node")
                if active_mission
                else st.session_state.get("plan_start_node", "S")
            ),
            "closed_edges": list(
                active_mission.get("closed_edges") or []
                if active_mission
                else st.session_state.get("plan_closed_edges") or []
            ),
            "mission": active_mission,
            "mission_stale": agent_mission_stale,
            "detection_signature": agent_input_signature,
        }
        with st.spinner("Agent 执行中……"):
            result = run_agent(
                question.strip(),
                mode=st.session_state.mode,
                context=context,
                planner=st.session_state.planner,
            )

        st.session_state.agent_usage["rounds"] += result.rounds_used
        st.session_state.agent_usage["calls"] += result.calls_used

        detect_steps = [
            step for step in result.steps if step.tool == "detect_assets_audio"
        ]
        plan_steps = [
            step for step in result.steps if step.tool == "plan_asset_inspection"
        ]

        # 新任务检测失败：旧检测、旧路线、旧回答不能继续冒充本次结果。
        if result.detections_attempted and not result.detections:
            _clear_detection_state("", notice=False)
            st.session_state.detect_items = list(files_ctx)
            st.session_state.detection_input_signature = agent_input_signature
            st.session_state.detect_error = (
                "Agent 检测失败：旧检测结果与旧路线已清除，请查看上方失败原因。"
            )
            st.session_state.plan_notice = ""
            st.session_state.plan_error = ""
        elif result.detections:
            st.session_state.detections = result.detections
            st.session_state.detections_is_mock = any(
                step.is_mock for step in detect_steps
            )
            st.session_state.detection_input_signature = agent_input_signature
            st.session_state.detect_error = ""
            st.session_state.detect_notice = ""
            st.session_state.detect_run_id = result.run_id
            st.session_state.detect_log_path = result.log_path
            _clear_plan_state("", notice=False)
            st.session_state.plan_notice = ""

        # 巡检执行任务与第一轮静态规划分开写回。
        if result.mission_attempted:
            if result.mission:
                st.session_state.mission = result.mission
                st.session_state.mission_error = ""
                action = (
                    (plan_steps[-1].args or {}).get("mission_action")
                    if plan_steps
                    else "auto"
                )
                st.session_state.mission_notice = (
                    f"Agent 已{'新建' if action == 'create' else '重规划'}任务 "
                    f"{result.mission.get('mission_id')}："
                    f"当前位置 {result.mission.get('current_node')}，"
                    f"已走 {result.mission.get('walked_distance_m')} m，"
                    f"待检 {_mission_device_text(result.mission.get('pending_assets') or [])}。"
                )
                st.session_state._pending_mission_closed_edges = list(
                    result.mission.get("closed_edges") or []
                )
            else:
                failure = (
                    plan_steps[-1].error
                    if plan_steps and plan_steps[-1].error
                    else result.answer
                )
                st.session_state.mission_error = f"Agent 任务操作失败：{failure}"
            # 活动任务不会覆盖第一轮的静态路线和模拟案例结果。
        elif result.plan_attempted:
            # 新任务规划失败：旧路线必须清掉；检测结果若本次有效则保留。
            if result.plan:
                st.session_state.plan_result = result.plan
                plan_is_mock = (
                    plan_steps[-1].is_mock
                    if plan_steps
                    else st.session_state.detections_is_mock
                )
                st.session_state.plan_is_mock = plan_is_mock
                actual_start = (
                    result.plan.get("start_node")
                    or result.plan_start_node
                    or st.session_state.get("plan_start_node", "S")
                )
                actual_closed = list(
                    result.plan.get("closed_edges")
                    if "closed_edges" in result.plan
                    else result.plan_closed_edges
                )
                st.session_state.plan_used_start_node = actual_start
                st.session_state.plan_used_closed_edges = actual_closed
                st.session_state.plan_context = cd_state.plan_context(
                    actual_start, actual_closed
                )
                st.session_state.plan_error = ""
                st.session_state.plan_notice = ""
                if (
                    st.session_state.get("plan_start_node", "S") != actual_start
                    or sorted(st.session_state.get("plan_closed_edges") or [])
                    != sorted(actual_closed)
                ):
                    st.session_state._pending_plan_start_node = actual_start
                    st.session_state._pending_plan_closed_edges = actual_closed
            else:
                _clear_plan_state("", notice=False)
                st.session_state.plan_error = (
                    "Agent 规划失败：旧路线已清除，检测结果仍保留；"
                    "请查看上方失败原因后重新规划。"
                )
                st.session_state.plan_notice = ""

        st.session_state.last_agent = result
        if (
            result.detections_attempted
            or result.plan_attempted
            or result.mission_attempted
        ):
            # Agent 在页面下方执行；rerun 后检测区、规划区和任务区都拿到同一份新状态。
            st.rerun()

    # 轨迹与结论从 Session State 渲染：页面重跑后仍可见，且只在本会话内存在
    if st.session_state.last_agent is not None:
        _render_agent_result(st.session_state.last_agent)
    else:
        st.caption("还没有执行过任务。")

st.divider()
st.caption(
    "海工智维 · 声学B ｜ 应用与 Agent ｜ "
    "A: audio_module · C: spatial_module.visualize · D: spatial_module.plan ｜ "
    "C/D 补丁 CD-R2-20261025"
)
