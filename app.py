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
    list_assets,
    list_edges,
    node_ids,
    sample_files,
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
st.session_state.setdefault("plan_result", None)
st.session_state.setdefault("plan_is_mock", False)
st.session_state.setdefault("detect_run_id", "")
st.session_state.setdefault("detect_log_path", "")
st.session_state.setdefault("last_agent", None)      # 最近一次 Agent 结果（含轨迹与日志路径）


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
    if res.plan:
        st.caption("巡检计划已写入；切到「③ 巡检规划」可查看优先级、路线与地图。")


def _render_plan(d: dict, is_mock: bool, start_node: str = "S") -> None:
    status = d.get("status")
    st.markdown(
        f"**规划状态：** `{status}` · {STATUS_ZH.get(status, '')}　"
        f"**总距离：** {d.get('total_distance_m')} m　"
        f"**起点：** {start_node}"
    )
    if is_mock:
        st.caption("数据来源：模拟检测结果 + D 的真实规划算法（路线与距离按真实场景表计算）")
    else:
        st.caption("数据来源：A 的真实检测结果 + D 的真实规划算法")

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


# ---------------------------------------------------------------------------
# 侧边栏
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("数据来源")
    mode_label = st.radio(
        "运行模式",
        options=["模拟数据（mock）", "真实模型（real）"],
        index=0 if st.session_state.mode == "mock" else 1,
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
        "audio_module/  models/  samples/     A 交付（原样，未改动）\n"
        "spatial_module/  scene/              D + C 交付（原样，未改动）\n"
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
                    {"asset_id": asset, "path": "", "filename": f.name, "_upload": f}
                )

    if st.button("开始检测", type="primary", disabled=not pending_items):
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

        run_id = runlog.new_run_id()
        started_at = runlog.now_iso()
        with st.spinner(f"检测中（{len(items)} 个文件，计 1 次工具调用）……"):
            result = detect_assets_audio(
                asset_ids=[it["asset_id"] for it in items],
                files=items,
                mode=st.session_state.mode,
            )
        finished_at = runlog.now_iso()

        st.session_state.detect_items = items
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
            st.session_state.plan_result = None
            if result.data["missing_asset_ids"]:
                st.warning(
                    "以下设备没有对应音频，本次未检测（不会当作正常）："
                    + "、".join(result.data["missing_asset_ids"])
                )
        else:
            st.error(f"检测失败：{result.message}")
            st.caption("失败时不会用模拟数据兜底。请检查音频规格或模块状态。")

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
                    start_node=st.session_state.get("plan_start_node", "S"),
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
    edge_options = [f"{e['edge_id']}（{e['from_node']}→{e['to_node']}）" for e in edges]

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
            closed = st.multiselect(
                "临时关闭通道（可选）",
                edge_options,
                help="关闭后通行能力降级或目标不可达，可用于演示 partial（部分达成）。",
            )
        with col2:
            start_node = st.selectbox("起点节点", node_ids(), index=0)

        closed_ids = [c.split("（")[0] for c in closed]

        if st.button("生成巡检计划", type="primary"):
            with st.spinner("规划中（含路线枚举与绘图）……"):
                result = plan_asset_inspection(
                    detection_results=rows,
                    closed_edges=closed_ids,
                    start_node=start_node,
                    input_is_mock=st.session_state.detections_is_mock,
                )
            if result.ok:
                st.session_state.plan_result = result.data
                st.session_state.plan_is_mock = result.is_mock
                st.session_state.plan_start_node = start_node
            else:
                st.session_state.plan_result = None
                st.error(f"规划失败：{result.message}")

        if st.session_state.plan_result:
            st.divider()
            _render_plan(
                st.session_state.plan_result,
                st.session_state.plan_is_mock,
                st.session_state.get("plan_start_node", "S"),
            )

            with st.expander("规划结果原始 JSON"):
                st.json(st.session_state.plan_result)

        st.divider()
        if st.button("只看场景地图（不做规划）"):
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
    )
    if st.button("交给 Agent", type="primary", disabled=not question.strip()):
        context = {
            "files": files_ctx,
            "detections": st.session_state.detections,
            "detections_is_mock": st.session_state.detections_is_mock,
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

        if result.detections:
            st.session_state.detections = result.detections
            st.session_state.detections_is_mock = any(
                s.is_mock for s in result.steps if s.tool == "detect_assets_audio"
            )

        if result.plan:
            # 真正写回状态，否则「③ 巡检规划」看不到 Agent 生成的路线
            st.session_state.plan_result = result.plan
            st.session_state.plan_is_mock = st.session_state.detections_is_mock
            st.session_state.plan_start_node = result.plan_start_node

        st.session_state.last_agent = result

    # 轨迹与结论从 Session State 渲染：页面重跑后仍可见，且只在本会话内存在
    if st.session_state.last_agent is not None:
        _render_agent_result(st.session_state.last_agent)
    else:
        st.caption("还没有执行过任务。")

st.divider()
st.caption("海工智维 · 声学B ｜ 应用与 Agent ｜ A: audio_module · C: spatial_module.visualize · D: spatial_module.plan")
