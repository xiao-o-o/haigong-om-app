# -*- coding: utf-8 -*-
"""Agent 决策器：LLM 规划 + 严格校验 + 规则版回退。

分工边界：
- LLM 只决定「调用哪些工具、传什么参数」，不许输出任何检测分数、路线距离或结论；
- 工具返回什么，页面就显示什么；校验失败即视为模型没规划对，回退规则版；
- 回退必须留痕（PlanOutcome.source / note），执行轨迹里能看见用的是哪一版决策器。

校验清单（_validate_plan）：
1. 顶层必须是 {"steps": [...]}，步数 1..max_steps；
2. 工具名必须在白名单内，未知工具直接判失败（不做「猜你想用哪个」）；
3. detect_assets_audio 的 items 只能引用页面已送检的文件，LLM 不得自己造路径；
4. plan_asset_inspection 的 closed_edges 只能是场景表里真实存在的 E01~E13；
5. start_node 必须是场景里真实节点；
6. 同任务先检测后规划，顺序被强制修正。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from core import llm
from core.local_modules import asset_ids, edge_ids, list_assets, node_ids

ALLOWED_TOOLS = ("detect_assets_audio", "plan_asset_inspection", "get_checklist")

TOOL_DOCS = [
    {
        "name": "detect_assets_audio",
        "purpose": "批量检测设备音频是否疑似异常，调用 A 的声音模型",
        "args": {
            "asset_ids": "字符串数组，元素为设备编号，如 [\"P01\", \"P04\"]；只能取上下文 assets 里列出的编号，不能指定文件路径",
        },
        "returns": "requested_asset_ids / missing_asset_ids / results（每条含 score 与 status：normal / suspected_anomaly / invalid_input）",
    },
    {
        "name": "plan_asset_inspection",
        "purpose": "按检测结果排巡检优先级与路网路线，调用 D 的规划算法",
        "args": {
            "closed_edges": "数组，元素为通道编号如 \"E04\"；不传表示沿用当前状态，空数组表示全部开放",
            "start_node": "起点节点；有活动任务时必须等于任务的 current_node，不能把人员瞬移回 S",
            "mission_action": "可选 auto / replan / create；有活动任务时通常用 auto 或 replan，create 会被拒绝",
        },
        "returns": "静态规划返回 status / priority / visit_order / total_distance_m；活动任务返回 mission + 剩余 plan",
        "prerequisite": "新建静态规划需要检测结果；活动任务重规划使用任务快照和当前位置",
    },
    {
        "name": "get_checklist",
        "purpose": "检索本地检查标准与资料",
        "args": {"query": "关键词字符串，可为空字符串表示全部"},
        "returns": "命中的资料条目",
    },
]


def _system_prompt() -> str:
    return (
        "你是海工设备运维助手的任务规划器。你的唯一职责是把用户的一句话任务翻译成工具调用序列，"
        "不回答业务问题、不生成任何数据、不猜测设备状态。\n\n"
        f"可用工具：\n{json.dumps(TOOL_DOCS, ensure_ascii=False, indent=2)}\n\n"
        "输出要求：只输出 JSON，格式为\n"
        '{"reason": "一句话说明规划依据", "steps": [{"tool": "工具名", "args": {...}}]}\n'
        "- steps 最多 3 步，按执行顺序排列；\n"
        "- 需要检测又需要规划时，detect 必须排在 plan 前面；\n"
        "- 只调必要的工具，不要为了显得完整而调用无关工具；\n"
        "- 没有可用文件就不要规划 detect；没有检测结果就不要规划 plan；\n"
        "- 参数只能用上下文给出的编号，不得编造设备编号、通道编号或文件路径；\n"
        "- plan 的 start_node 和 closed_edges 若用户没有明确改变，必须沿用 "
        "current_plan_state，不要自行重置为 S 或空数组；\n"
        "- 有 active_mission 时，重规划只能从任务的 current_node 出发，"
        "不能修改当前位置、已完成设备或已走距离；\n"
        "- 有 active_mission 时不要用 create 替换任务；是否新建由页面确认；\n"
        "- 如果任务与三个工具都无关，返回 {\"steps\": []}。"
    )


def _user_prompt(question: str, context: dict) -> str:
    files = [
        {"asset_id": str(f.get("asset_id", "")), "filename": str(f.get("filename") or "")}
        for f in (context.get("files") or [])
    ]
    detections = context.get("detections") or []
    mission = context.get("mission") if isinstance(context.get("mission"), dict) else None
    if mission:
        current_node = str(mission.get("current_node") or "S")
        mission_closed = list(mission.get("closed_edges") or [])
        mission_summary = {
            "mission_id": mission.get("mission_id"),
            "status": mission.get("status"),
            "source_kind": (mission.get("source") or {}).get("kind"),
            "current_node": current_node,
            "pending_assets": list(mission.get("pending_assets") or []),
            "completed_assets": list(mission.get("completed_assets") or []),
            "unreachable_assets": list(mission.get("unreachable_assets") or []),
            "closed_edges": mission_closed,
            "walked_distance_m": mission.get("walked_distance_m"),
            "remaining_distance_m": mission.get("remaining_distance_m"),
        }
    else:
        current_node = str(context.get("start_node") or "S")
        mission_closed = list(context.get("closed_edges") or [])
        mission_summary = None

    ctx = {
        "assets": [
            {"asset_id": a["asset_id"], "name": a.get("name"), "criticality": a.get("criticality")}
            for a in list_assets()
        ],
        "nodes": node_ids(),
        "edges": edge_ids(),
        "available_files": files,
        "existing_detections": {
            "count": len(detections),
            "is_mock": bool(context.get("detections_is_mock")),
        },
        "active_mission": mission_summary,
        "current_plan_state": {
            "start_node": current_node,
            "closed_edges": mission_closed,
        },
        "runtime_mode": context.get("mode", "mock"),
    }
    return (
        f"上下文：\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
        f"用户任务：{question}"
    )


@dataclass
class PlanOutcome:
    steps: list[tuple[str, dict]] = field(default_factory=list)
    source: str = "rule"          # llm / rule / rule_fallback
    note: str = ""                # 用哪一版、为什么回退
    raw: dict | None = None       # LLM 原始输出，便于排查


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _validate_plan(raw: dict, context: dict, max_steps: int) -> tuple[list[tuple[str, dict]], str]:
    """把 LLM 输出校验成可执行的工具序列。返回 (steps, error)。"""
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list):
        return [], "缺少 steps 数组"
    if len(steps_raw) > max_steps:
        return [], f"步骤数 {len(steps_raw)} 超过上限 {max_steps}"
    if not steps_raw:
        return [], ""  # 合法但为空：任务与工具无关

    valid_edges = {e.upper() for e in edge_ids()}
    valid_nodes = {n.upper() for n in node_ids()}
    allowed_assets = {str(a).upper() for a in asset_ids()}

    steps: list[tuple[str, dict]] = []
    seen: set[str] = set()

    for i, item in enumerate(steps_raw, start=1):
        if not isinstance(item, dict):
            return [], f"第 {i} 步不是对象"
        tool = item.get("tool")
        if tool not in ALLOWED_TOOLS:
            return [], f"第 {i} 步工具名不在白名单：{tool!r}"
        if tool in seen:
            return [], f"第 {i} 步重复调用 {tool}（单任务同工具只允许一次）"
        seen.add(tool)

        args_in = item.get("args") or {}
        if not isinstance(args_in, dict):
            return [], f"第 {i} 步 args 不是对象"
        args: dict = {}

        if tool == "detect_assets_audio":
            ids_raw = args_in.get("asset_ids")
            if not isinstance(ids_raw, list):
                return [], "detect_assets_audio 缺少 asset_ids 数组"
            picked, unknown = [], []
            for raw_id in ids_raw:
                aid = str(raw_id).strip().upper()
                if aid not in allowed_assets:
                    # 场景表里没有的编号一律拒绝：不允许模型凭空造设备
                    unknown.append(aid or "空")
                elif aid not in picked:
                    picked.append(aid)
            if unknown:
                return [], "detect 引用了不存在的设备编号：" + "、".join(unknown)
            if not picked:
                return [], "detect 的 asset_ids 为空"
            # 只透传编号；路径由 tools 从页面送检文件里解析，模型碰不到文件系统
            args["asset_ids"] = picked

        elif tool == "plan_asset_inspection":
            # 用户没有明确给参数时，沿用当前状态；有活动任务时以任务当前位置/封路为准。
            mission = context.get("mission") if isinstance(context.get("mission"), dict) else None
            default_start = (
                str(mission.get("current_node") or "S")
                if mission
                else str(context.get("start_node") or "S")
            )
            default_closed = list(
                mission.get("closed_edges") or []
                if mission
                else context.get("closed_edges") or []
            )
            if "closed_edges" in args_in and args_in.get("closed_edges") is not None:
                closed_raw = args_in.get("closed_edges")
            else:
                closed_raw = default_closed
            if not isinstance(closed_raw, list):
                return [], "closed_edges 必须是数组"
            closed = []
            for c in closed_raw:
                cid = str(c).strip().upper()
                if cid not in valid_edges:
                    return [], f"closed_edges 含不存在的通道：{c}"
                if cid not in closed:
                    closed.append(cid)
            start_raw = args_in.get("start_node")
            start = str(start_raw or default_start).strip().upper()
            if start not in valid_nodes:
                return [], f"start_node 不存在于场景：{start}"
            action = str(args_in.get("mission_action") or "auto").strip().lower()
            if action not in {"auto", "replan", "create"}:
                return [], f"mission_action 不合法：{action}"
            args["closed_edges"] = closed
            args["start_node"] = start
            args["mission_action"] = action

        elif tool == "get_checklist":
            args["query"] = str(args_in.get("query") or "")

        steps.append((tool, args))

    # 顺序修正：detect 必须早于 plan
    order = {"detect_assets_audio": 0, "plan_asset_inspection": 1, "get_checklist": 2}
    steps.sort(key=lambda s: order[s[0]])
    return steps, ""


# ---------------------------------------------------------------------------
# 规则版（回退用，逻辑与 v0.2 一致）
# ---------------------------------------------------------------------------

_ASSET_MENTION = re.compile(r"(?<![A-Za-z0-9])P\d{2}(?![0-9])", re.IGNORECASE)
_EDGE_MENTION = re.compile(r"(?<![A-Za-z0-9])E\d{2}(?![0-9])", re.IGNORECASE)
_START_PATTERNS = (
    re.compile(r"从\s*([A-Za-z][A-Za-z0-9]*)\s*出发"),
    re.compile(
        r"从\s*([A-Za-z][A-Za-z0-9]*)\s*(?:开始|重新规划|重新|规划|走|前往)"
    ),
    re.compile(
        r"起点\s*(?:节点)?\s*(?:设为|改为|改到|定在|放在|是|为)?\s*([A-Za-z][A-Za-z0-9]*)"
    ),
)
_OPEN_PHRASES = (
    "取消关闭", "解除关闭", "重新开放", "恢复开放", "恢复通行", "打开", "开放", "放行"
)
_CLOSE_WORDS = ("关闭", "封闭", "封路", "禁止通行", "不通")


def _mentioned_start(question: str) -> str:
    """从“从J01出发 / 起点改为J01”里取起点；没提到返回空串。"""
    for pattern in _START_PATTERNS:
        match = pattern.search(question)
        if match:
            return match.group(1).strip().upper()
    return ""


def _apply_edge_changes(question: str, current_closed: list[str]) -> tuple[list[str], list[str]]:
    """按自然语言增删关闭通道。

    返回 (本次提到的通道, 处理后的关闭通道)。没有提通道时保持当前状态。
    支持“关闭E04”“重新开放E11”“取消关闭E11”等写法。
    """
    current = {str(edge).strip().upper() for edge in current_closed if str(edge).strip()}
    matches = list(_EDGE_MENTION.finditer(question))
    mentioned = sorted({m.group(0).upper() for m in matches})
    if not matches:
        return [], sorted(current)

    explicit_open: set[str] = set()
    explicit_close: set[str] = set()
    for match in matches:
        edge_id = match.group(0).upper()
        prefix = question[max(0, match.start() - 12):match.start()]
        if any(phrase in prefix for phrase in ("取消关闭", "解除关闭", "重新开放", "恢复开放", "恢复通行")):
            explicit_open.add(edge_id)
            continue

        open_at = max((prefix.rfind(word) for word in _OPEN_PHRASES), default=-1)
        close_at = max((prefix.rfind(word) for word in _CLOSE_WORDS), default=-1)
        if open_at > close_at:
            explicit_open.add(edge_id)
        elif close_at >= 0:
            explicit_close.add(edge_id)

    if not explicit_open and not explicit_close:
        # 关键词在通道编号后面（如“E04关闭”）时，按全句意图处理。
        has_open = any(word in question for word in _OPEN_PHRASES)
        has_close = any(word in question for word in _CLOSE_WORDS)
        if has_open and not has_close:
            explicit_open.update(mentioned)
        elif has_close and not has_open:
            explicit_close.update(mentioned)

    for edge_id in explicit_open:
        current.discard(edge_id)
    for edge_id in explicit_close:
        current.add(edge_id)
    return mentioned, sorted(current)


def rule_plan(question: str, context: dict) -> list[tuple[str, dict]]:
    file_pat = re.compile(r"[\w\u4e00-\u9fff-]+\.wav", re.IGNORECASE)

    pool = [str(f.get("asset_id", "")).upper() for f in (context.get("files") or []) if f.get("asset_id")]
    known = [a.upper() for a in asset_ids()]
    named_assets = sorted({m.group(0).upper() for m in _ASSET_MENTION.finditer(question)})

    # 点了不存在的设备编号（如 P99）：直接把编号交给工具，由工具如实报错，
    # 不静默忽略、也不替换成别的设备。
    mentioned = sorted({m.group(0).upper() for m in _ASSET_MENTION.finditer(question)})
    unknown_mentioned = [a for a in mentioned if a not in known]
    if unknown_mentioned:
        return [("detect_assets_audio", {"asset_ids": unknown_mentioned})]

    # 取检测目标的优先级：点名设备 > 页面送检文件 > 全场景（模拟模式无文件也能出演示分数）
    if named_assets:
        detect_ids = [a for a in named_assets if a in known]
    elif pool:
        detect_ids = []
        for a in pool:
            if a in known and a not in detect_ids:
                detect_ids.append(a)
    else:
        detect_ids = list(known)

    want_detect = any(k in question for k in ("检测", "音频", "声音", "异响")) or bool(file_pat.search(question))
    # 已有检测结果、又没点名新设备时，不重复检测：通道变化只需重规划
    if context.get("detections") and not named_assets:
        want_detect = False

    mission = context.get("mission") if isinstance(context.get("mission"), dict) else None
    current_start = (
        str(mission.get("current_node") or "S")
        if mission
        else str(context.get("start_node") or "S")
    ).strip().upper()
    current_closed = [
        str(e).strip().upper()
        for e in (
            mission.get("closed_edges") if mission else context.get("closed_edges")
        ) or []
    ]
    named_start = _mentioned_start(question)
    start_node = named_start or current_start
    _, next_closed = _apply_edge_changes(question, current_closed)
    edge_changed = sorted(next_closed) != sorted(current_closed)

    create_words = ("新建", "创建", "新任务", "替换任务", "开始新")
    wants_create = any(word in question for word in create_words)
    want_plan = any(k in question for k in ("巡检", "规划", "路线", "计划")) or wants_create
    if edge_changed and (context.get("detections") or mission):
        # “关闭E04”“重新开放E11”本身就意味着按新通道状态重算路线
        want_plan = True
    want_doc = any(
        k in question
        for k in ("检查资料", "资料", "清单", "标准", "规范", "复核", "流程", "要求", "注意事项")
    )

    if wants_create:
        mission_action = "create"
    elif mission:
        mission_action = "replan"
    else:
        mission_action = "auto"

    plans: list[tuple[str, dict]] = []
    if want_detect and detect_ids:
        plans.append(("detect_assets_audio", {"asset_ids": detect_ids}))
    if want_plan:
        plans.append((
            "plan_asset_inspection",
            {
                "closed_edges": next_closed,
                "start_node": start_node,
                "mission_action": mission_action,
            },
        ))
    if want_doc:
        doc_query = next(
            (k for k in ("复核", "清单", "规范", "标准", "资料", "流程", "要求", "注意事项") if k in question),
            "",
        )
        plans.append(("get_checklist", {"query": doc_query}))
    return plans


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def make_plan(
    question: str,
    context: dict,
    max_steps: int,
    planner: str = "auto",
) -> PlanOutcome:
    """planner: auto（LLM 优先，失败回退） / llm（强制 LLM） / rule（只用规则版）。"""
    if planner == "rule":
        return PlanOutcome(rule_plan(question, context), source="rule", note="按设置使用规则版决策器")

    cfg = llm.current_config()
    if not cfg.configured:
        if planner == "llm":
            # 「仅 LLM」是显式要求用模型：没配置就如实报不可用，不偷偷换成规则版
            raise llm.LLMError("LLM 未配置：" + "、".join(cfg.missing) + "，Agent 的 LLM 模式不可用")
        steps = rule_plan(question, context)
        return PlanOutcome(
            steps, source="rule",
            note="规则版（未配置 LLM：" + "、".join(cfg.missing) + "）",
        )

    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _user_prompt(question, context)},
    ]
    try:
        raw = llm.chat_json(messages, cfg)
    except llm.LLMError as exc:
        if planner == "llm":
            raise
        return PlanOutcome(
            rule_plan(question, context), source="rule_fallback",
            note=f"规则版（LLM 调用失败回退：{exc}）",
        )

    steps, error = _validate_plan(raw, context, max_steps)
    if error:
        if planner == "llm":
            raise llm.LLMError(f"LLM 规划未通过校验：{error}")
        return PlanOutcome(
            rule_plan(question, context), source="rule_fallback", raw=raw,
            note=f"规则版（LLM 规划被校验拒绝：{error}）",
        )

    reason = str(raw.get("reason") or "").strip()
    note = f"LLM 规划（{cfg.model}）" + (f"：{reason}" if reason else "")
    return PlanOutcome(steps, source="llm", note=note, raw=raw)
