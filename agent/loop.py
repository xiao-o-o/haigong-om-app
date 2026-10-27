# -*- coding: utf-8 -*-
"""单 Agent 主循环 v0.3（可切换决策器：LLM / 规则版）。

预算硬约束（来自实施手册，不得放宽）：
- 单任务最多 6 轮决策；
- 单任务最多 12 次工具执行；
- 批量检测无论文件数量计 1 次。

v0.3 相比 v0.2：
1. 决策器抽到 agent/planner.py，LLM 与规则版共用同一执行循环；
2. LLM 输出必须过白名单校验（工具名、设备编号、通道编号、节点、顺序），
   校验不过就回退规则版，并在执行轨迹里写明原因，不静默切换；
3. AgentResult 增加 planner / planner_note，页面可显示本次用的是哪一版；
4. 汇总文案仍只用工具真实返回字段，LLM 不参与结论生成。

循环、预算、汇总逻辑与决策器解耦：换模型只动 planner.py。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent.planner import make_plan
from agent.tools import ToolResult, detect_assets_audio, get_checklist, plan_asset_inspection
from core import llm, runlog

MAX_ROUNDS = 6
MAX_TOOL_CALLS = 12

PLANNER_LABELS = {
    "auto": "自动（LLM 优先，失败回退规则版）",
    "llm": "仅 LLM（失败即报错）",
    "rule": "仅规则版",
}


@dataclass
class Step:
    round_no: int
    tool: str
    args: dict
    ok: bool
    is_mock: bool
    summary: str
    duration_ms: int = 0
    status: str = "ok"          # ok / failed / invalid_input
    error: str = ""


@dataclass
class AgentResult:
    answer: str
    steps: list[Step] = field(default_factory=list)
    rounds_used: int = 0
    calls_used: int = 0
    budget_exhausted: bool = False
    detections: list[dict] = field(default_factory=list)
    detections_attempted: bool = False
    plan: dict | None = None
    plan_attempted: bool = False
    plan_start_node: str = "S"
    plan_closed_edges: list[str] = field(default_factory=list)
    mission: dict | None = None
    mission_attempted: bool = False
    planner: str = "rule"
    planner_note: str = ""
    run_id: str = ""
    log_path: str = ""


def _dispatch(tool_name: str, args: dict, mode: str | None, state: dict) -> ToolResult:
    if tool_name == "detect_assets_audio":
        # 模型只给编号；路径来自页面送检文件（state["files"]），不经模型。
        # 有活动任务时，任何一次新检测都不能悄悄套到旧任务上。
        if state.get("mission"):
            state["mission_stale"] = True
        tr = detect_assets_audio(
            asset_ids=args.get("asset_ids") or [],
            files=state.get("files") or [],
            mode=mode,
        )
        if tr.ok:
            state["detections"] = tr.data["results"]
            state["detections_is_mock"] = tr.is_mock
        return tr

    if tool_name == "plan_asset_inspection":
        detections = state.get("detections") or []
        return plan_asset_inspection(
            detection_results=detections,
            closed_edges=args.get("closed_edges"),
            start_node=args.get("start_node"),
            input_is_mock=bool(state.get("detections_is_mock", mode == "mock")),
            mission=state.get("mission"),
            mission_action=args.get("mission_action") or "auto",
            mission_stale=bool(state.get("mission_stale")),
            source_signature=str(state.get("detection_signature") or ""),
        )

    if tool_name == "get_checklist":
        return get_checklist(**args, mode=mode)

    return ToolResult(tool_name, False, False, message=f"未知工具：{tool_name}")


def _summarize(tr: ToolResult) -> str:
    if not tr.ok:
        return f"调用失败：{tr.message}"

    if tr.tool == "detect_assets_audio":
        d = tr.data or {}
        results = d.get("results") or []
        abnormal = d.get("abnormal", 0)
        missing = d.get("missing_asset_ids") or []
        none_score = [i for i in results if i.get("score") is None]
        tail = f"，{len(none_score)} 个文件输入不合法" if none_score else ""
        lack = f"，缺音频 {len(missing)} 台（{'、'.join(missing)}）" if missing else ""
        return f"检测 {len(results)} 台，疑似异常 {abnormal} 台{tail}{lack}"

    if tr.tool == "plan_asset_inspection":
        d = tr.data or {}
        if d.get("mode") == "mission":
            mission = d.get("mission") or {}
            return (
                f"任务 {mission.get('status', '?')}，当前位置 {mission.get('current_node', '?')}，"
                f"已走 {mission.get('walked_distance_m', '?')} m，"
                f"待检 {'、'.join(mission.get('pending_assets') or []) or '无'}，"
                f"剩余 {mission.get('remaining_distance_m', '?')} m，"
                f"不可达 {len(mission.get('unreachable_assets') or [])} 台"
            )
        order = "→".join(d.get("visit_order") or []) or "无"
        return (
            f"规划状态 {d.get('status', '?')}，访问顺序 {order}，"
            f"总距离 {d.get('total_distance_m', '?')} m，"
            f"不可达 {len(d.get('unreachable_asset_ids') or [])} 台"
        )

    if tr.tool == "get_checklist":
        d = tr.data or {}
        items = d.get("items") or []
        if not items:
            return "未命中检查资料"
        head = "；".join(f"{i.get('source_id')} {i.get('title')}" for i in items[:3])
        more = f" 等 {len(items)} 条" if len(items) > 3 else ""
        tail = "（关键词无精确匹配，已返回全部条目）" if d.get("fallback") else ""
        return f"命中 {len(items)} 条：{head}{more}{tail}"

    return "完成"


def _compose_answer(steps: list[Step]) -> str:
    lines = []
    for s in steps:
        tag = "（模拟数据）" if s.is_mock else "（真实数据）"
        lines.append(f"- 第 {s.round_no} 步 {s.tool}{tag}：{s.summary}")
    if any(s.is_mock for s in steps):
        lines.append("")
        lines.append("注意：以上包含模拟数据，仅用于流程演示，不可作为正式结论。")
    return "\n".join(lines)


NO_PLAN_HINT = (
    "没理解你的任务，可以这样说：\n"
    "- 「检测 P01 和 P04 的声音」→ 调用批量检测（需先在页面选好音频）\n"
    "- 「把检测结果做成巡检计划」→ 调用巡检规划\n"
    "- 「关闭 E04 后重新规划巡检路线」→ 关闭通道再规划\n"
    "- 「查一下检查标准」→ 检索本地资料"
)


def run_agent(
    question: str,
    mode: str | None = None,
    context: dict | None = None,
    planner: str = "auto",
) -> AgentResult:
    """执行单次 Agent 任务，返回结论 + 执行轨迹 + 预算消耗。

    context 可包含：
        files              : [{"asset_id": "P04", "path": "...", "filename": "..."}] 页面已送检文件
        detections         : 页面最近一次检测结果，供「直接规划」场景使用
        detections_is_mock : 上述结果是否来自模拟
    planner: auto（默认，LLM 优先失败回退） / llm（仅 LLM） / rule（仅规则版）
    """
    context = dict(context or {})
    context.setdefault("mode", mode or "mock")
    context.setdefault("start_node", "S")
    context.setdefault("closed_edges", [])
    state: dict = {
        "detections": list(context.get("detections") or []),
        "detections_is_mock": bool(context.get("detections_is_mock", False)),
        "files": list(context.get("files") or []),
        "start_node": context.get("start_node") or "S",
        "closed_edges": list(context.get("closed_edges") or []),
        "mission": context.get("mission") if isinstance(context.get("mission"), dict) else None,
        "mission_stale": bool(context.get("mission_stale", False)),
        "detection_signature": str(context.get("detection_signature") or ""),
    }

    result = AgentResult(answer="")
    started_at = runlog.now_iso()
    run_id = runlog.new_run_id()

    def _finish(res: AgentResult) -> AgentResult:
        """统一出口：给每次运行编号并落日志，所有 return 都走这里。"""
        res.run_id = run_id
        res.log_path = runlog.write_run_log({
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": runlog.now_iso(),
            "mode": mode or "mock",
            "planner_requested": planner,
            "planner_used": res.planner,
            "planner_note": res.planner_note,
            "question": question,
            "rounds": res.rounds_used,
            "tool_calls": res.calls_used,
            "budget_exhausted": res.budget_exhausted,
            "plan_start_node": res.plan_start_node,
            "plan_closed_edges": res.plan_closed_edges,
            "mission_id": (res.mission or {}).get("mission_id"),
            "mission_status": (res.mission or {}).get("status"),
            "steps": [
                {
                    "round": s.round_no,
                    "tool": s.tool,
                    "arguments": runlog.digest_args(s.args),
                    "status": s.status,
                    "result_summary": s.summary,
                    "duration_ms": s.duration_ms,
                    "error": s.error,
                }
                for s in res.steps
            ],
            "answer": res.answer,
        })
        return res

    try:
        outcome = make_plan(question, context, max_steps=MAX_ROUNDS, planner=planner)
    except llm.LLMError as exc:
        # 仅在 planner="llm"（强制 LLM）时走到这里：如实报错，不偷偷换规则版
        result.answer = f"【失败】LLM 决策器调用失败：{exc}。已按设置停止，未回退规则版。"
        result.planner = "llm"
        result.planner_note = str(exc)
        result.rounds_used = 1
        return _finish(result)

    result.planner = outcome.source
    result.planner_note = outcome.note
    plans = outcome.steps

    # 只说了「规划」但没有任何检测结果可复用 → 直接给出可执行提示，不臆造目标。
    # 有活动任务时允许只凭任务快照重规划剩余目标。
    if (
        plans
        and plans[0][0] == "plan_asset_inspection"
        and not state["detections"]
        and not state["mission"]
    ):
        result.answer = (
            "还没有可用的检测结果。巡检规划只按 A 的检测结果确定目标，"
            "不会凭设备清单猜测。请先选择音频并执行检测，再说「做成巡检计划」。"
        )
        result.rounds_used = 1
        return _finish(result)

    if not plans:
        result.answer = NO_PLAN_HINT
        result.rounds_used = 1
        return _finish(result)

    for round_no, (tool_name, args) in enumerate(plans, start=1):
        if round_no > MAX_ROUNDS or result.calls_used >= MAX_TOOL_CALLS:
            result.budget_exhausted = True
            break
        result.rounds_used = round_no

        t0 = time.perf_counter()
        tr = _dispatch(tool_name, args, mode, state)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        result.calls_used += 1

        status = "ok" if tr.ok else "failed"
        if tr.ok and tool_name == "detect_assets_audio":
            rows = (tr.data or {}).get("results") or []
            if any(r.get("status") == "invalid_input" for r in rows):
                status = "invalid_input"

        result.steps.append(
            Step(
                round_no, tool_name, args, tr.ok, tr.is_mock, _summarize(tr),
                duration_ms=duration_ms,
                status=status,
                error="" if tr.ok else tr.message,
            )
        )

        if tool_name == "detect_assets_audio":
            result.detections_attempted = True
            if tr.ok:
                result.detections = state["detections"]

        if tool_name == "plan_asset_inspection":
            result.plan_attempted = True
            if state.get("mission") or (args.get("mission_action") == "create"):
                result.mission_attempted = True
            if tr.ok:
                payload = tr.data if isinstance(tr.data, dict) else {}
                if payload.get("mode") == "mission":
                    result.mission = payload.get("mission")
                    result.plan = payload.get("plan")
                    mission_state = result.mission or {}
                    result.plan_start_node = str(
                        mission_state.get("current_node") or "S"
                    )
                    result.plan_closed_edges = list(
                        mission_state.get("closed_edges") or []
                    )
                    state["mission"] = result.mission
                else:
                    result.plan = tr.data
                    result.plan_start_node = (
                        (tr.data or {}).get("start_node")
                        or args.get("start_node")
                        or "S"
                    )
                    result.plan_closed_edges = list(
                        (tr.data or {}).get("closed_edges")
                        if "closed_edges" in (tr.data or {})
                        else (args.get("closed_edges") or [])
                    )

        if not tr.ok:
            result.answer = (
                f"【失败】第 {round_no} 步 {tool_name} 调用失败：{str(tr.message).rstrip('。')}。"
                "已停止后续步骤，不会用模拟数据兜底或编造结果。"
            )
            if not (result.detections_attempted and result.detections):
                result.detections = []
            return _finish(result)

    if result.detections_attempted:
        result.detections = state["detections"]
    body = _compose_answer(result.steps)
    if result.budget_exhausted:
        result.answer = "【部分完成】已达单任务预算上限，提前停止。\n" + body
    else:
        result.answer = body
    return _finish(result)
