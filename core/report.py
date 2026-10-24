# -*- coding: utf-8 -*-
"""报告生成：把工具返回值整理成可下载的 Markdown 报告。

只做排版，不做任何判定、不补任何数字：
- 分数、阈值、状态、距离、访问顺序全部原样取自 A / D 的返回；
- 「模拟与适用条件」「人工复核建议」单独成节，避免模拟分数被读成诊断结论；
- 独立成模块而不是写在页面里，是为了能脱离 Streamlit 单独测试。
"""
from __future__ import annotations

from datetime import datetime

STATUS_ZH = {
    "normal": "正常",
    "suspected_anomaly": "疑似异常",
    "invalid_input": "输入不合法",
    "ok": "规划成功",
    "partial": "部分达成",
    "no_targets": "无需巡检",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build_markdown_report(
    rows: list[dict],
    is_mock: bool,
    run_id: str = "",
    log_path: str = "",
    plan: dict | None = None,
    plan_is_mock: bool = False,
    start_node: str = "S",
    closed_edges: list[str] | None = None,
    expected_sample_rate: int = 16000,
) -> str:
    """返回 Markdown 文本。五个小节：结果 / 巡检计划 / 模拟条件 / 人工复核 / 日志。"""
    model_versions = sorted({str(r.get("model_version") or "—") for r in rows})
    lines = [
        "# 海工智维 · 设备检测报告",
        "",
        f"- 运行编号：`{run_id or '—'}`",
        f"- 生成时间：{_now()}",
        f"- 数据来源：{'模拟数据（未调用 A 模型）' if is_mock else 'A 声音模型真实推理'}",
        f"- 模型版本：{'、'.join(model_versions)}",
        f"- 音频输入：{expected_sample_rate} Hz 单声道 WAV，时长 ≥ 1 秒（未做重采样）",
        "",
        "## 一、检测结果",
        "",
        "| 设备编号 | 文件 | 判定 | 分数 | 阈值 | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            "| {aid} | {fn} | {st} | {sc} | {th} | {note} |".format(
                aid=r.get("asset_id", "—"),
                fn=(r.get("quality") or {}).get("filename", "—"),
                st=STATUS_ZH.get(r.get("status"), r.get("status") or "—"),
                sc=r.get("score") if r.get("score") is not None else "—",
                th=r.get("threshold", "—"),
                note="；".join(r.get("notes") or []) or "—",
            )
        )

    abnormal = [r for r in rows if r.get("status") == "suspected_anomaly"]
    invalid = [r for r in rows if r.get("status") == "invalid_input"]
    lines += [
        "",
        f"合计 {len(rows)} 台：疑似异常 {len(abnormal)} 台，输入不合法 {len(invalid)} 台。",
    ]

    if plan:
        lines += [
            "",
            "## 二、巡检计划",
            "",
            f"- 数据来源：{'模拟检测结果' if plan_is_mock else '真实检测结果'}（规划算法来自 D 模块）",
            f"- 规划状态：{STATUS_ZH.get(plan.get('status'), plan.get('status') or '—')}",
            f"- 起点节点：{start_node}",
            f"- 临时关闭通道：{'、'.join(closed_edges or []) or '无'}",
            f"- 访问顺序：{' → '.join(plan.get('visit_order') or []) or '无'}",
            f"- 总距离：{plan.get('total_distance_m', '—')} m",
            f"- 不可达设备：{'、'.join(plan.get('unreachable_asset_ids') or []) or '无'}",
        ]
        if plan.get("map_file"):
            lines.append(f"- 路线图：`{plan['map_file']}`")

    lines += ["", "## 三、模拟与适用条件", ""]
    if is_mock:
        lines.append("- 本报告分数为模拟数据，仅用于流程演示，**不得作为正式巡检结论**。")
    lines.append("- 分数是模型给出的相对异常程度，不是故障概率，也不判断具体损坏部件。")
    lines.append("- 模型基于公开工业数据（MIMII 水泵）训练，迁移到其他机型或安装方式时结论可能不成立。")
    lines.append("- 场景为模拟厂区，路线与距离按 scene/ 场景表计算，不代表真实厂区布局。")

    lines += ["", "## 四、人工复核建议", ""]
    if abnormal:
        lines.append(
            "- 疑似异常设备："
            + "、".join(str(r.get("asset_id")) for r in abnormal)
            + "。建议先人工复听并复测一次，再决定是否安排检修。"
        )
    if invalid:
        lines.append(
            "- 输入不合法设备："
            + "、".join(str(r.get("asset_id")) for r in invalid)
            + "。需重新采集后送检，**不得按正常处理**。"
        )
    if not abnormal and not invalid:
        lines.append("- 本次未发现疑似异常；仍建议按常规周期巡检，单次录音不能代表设备长期状态。")
    lines.append("- 最终结论由人工复核给出，系统只提供排序与提示。")

    if log_path:
        lines += ["", "## 五、运行日志", "", f"- `{log_path}`"]
    return "\n".join(lines) + "\n"
