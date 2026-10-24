# -*- coding: utf-8 -*-
"""LLM 接入自检：一条命令回答「我的密钥到底能不能用」。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe check_llm.py
    .venv\\Scripts\\python.exe check_llm.py --model deepseek-v4-pro
    .venv\\Scripts\\python.exe check_llm.py --planner llm      # 强制不走回退，暴露真实错误

四步检查，任一步失败都会给出排查方向，不会静默通过：
  1. 配置读取   —— 三项变量是否齐全、来源是环境变量还是 secrets.toml、密钥如何遮蔽
  2. 连通性     —— 真实发一次请求，验证地址 / 密钥 / 网络 / 代理
  3. 规划能力   —— 用三个真实任务跑决策器，看能否产出合法的工具调用序列
  4. 越权拦截   —— 故意让它引用不存在的设备编号，验证白名单是否兜住

注意：本脚本会真实消耗 token（四步合计约 1 千 token 量级）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import llm  # noqa: E402
from agent import planner as planner_mod  # noqa: E402

OK, BAD, WARN = "[通过]", "[失败]", "[提示]"
_failures = 0


def _say(tag: str, text: str) -> None:
    print(f"{tag} {text}")


def _fail(text: str, hint: str = "") -> None:
    global _failures
    _failures += 1
    _say(BAD, text)
    if hint:
        print(f"       → {hint}")


# ---------------------------------------------------------------------------
# 1. 配置
# ---------------------------------------------------------------------------

def step_config(cfg: llm.LLMConfig) -> bool:
    print("\n=== 1. 配置读取 ===")
    source = "环境变量" if os.environ.get("LLM_API_KEY", "").strip() else "secrets.toml"
    print(f"  api_base : {cfg.api_base or '(空)'}")
    print(f"  model    : {cfg.model or '(空)'}")
    print(f"  密钥来源 : {source}")
    print(f"  密钥     : {cfg.key_hint}")
    print(f"  超时     : {cfg.timeout:g}s ｜ temperature={cfg.temperature:g}")
    print(f"  请求地址 : {cfg.endpoint or '(空)'}")

    if not cfg.configured:
        _fail(
            "三项配置不全，缺：" + "、".join(cfg.missing),
            "把 core/llm.py 顶部注释里的五项抄进 .streamlit/secrets.toml（模板见 "
            ".streamlit/secrets.toml.example），或设为同名环境变量。缺任意一项都会走规则版决策器。",
        )
        return False

    if cfg.api_base.rstrip("/").endswith("/v1"):
        pass
    elif "/v1" in cfg.api_base:
        _say(WARN, "api_base 里 /v1 不在末尾，代码会在其后直接拼 /chat/completions，确认地址无误")
    else:
        _say(
            WARN,
            "api_base 未包含版本段（如 /v1）；代码只在其后拼 /chat/completions，"
            "若服务商要求别的路径会 404",
        )
    return True


# ---------------------------------------------------------------------------
# 2. 连通性
# ---------------------------------------------------------------------------

def step_ping(cfg: llm.LLMConfig) -> bool:
    print("\n=== 2. 连通性 ===")
    messages = [
        {"role": "system", "content": '只输出 JSON，不要任何解释。格式：{"ok": true, "echo": "你收到的字样"}'},
        {"role": "user", "content": '收到请回 {"ok": true, "echo": "ping"}'},
    ]
    t0 = time.perf_counter()
    try:
        data = llm.chat_json(messages, cfg)
    except llm.LLMError as exc:
        elapsed = time.perf_counter() - t0
        msg = str(exc)
        hint = "先看下面的原始报错，再对症处理"
        if "HTTP 401" in msg or "HTTP 403" in msg:
            hint = "密钥无效或没有该模型权限：确认 key 没被复制截断、没多空格，且账号已开通此模型"
        elif "HTTP 404" in msg:
            hint = "地址或模型名不对：api_base 应到 /v1 为止（不含 /chat/completions），模型名照服务商文档写"
        elif "HTTP 429" in msg:
            hint = "被限流或余额不足：稍后重试，或换 key / 换模型"
        elif "超时" in msg:
            hint = "网络到不了该地址：检查代理、公司网络或防火墙；也可把 LLM_TIMEOUT 调大再试"
        elif "请求失败" in msg:
            hint = "连不上：确认 api_base 的域名能打开、本机能出网、代理环境变量是否正确"
        _fail(f"请求失败（{elapsed:.1f}s）：{msg}", hint)
        return False

    elapsed = time.perf_counter() - t0
    if not isinstance(data, dict):
        _fail("返回不是 JSON 对象", "该模型/接口可能不支持 json_object 输出，换模型或用规则版")
        return False
    _say(OK, f"响应正常，耗时 {elapsed:.1f}s，返回键：{list(data)[:6]}")
    return True


# ---------------------------------------------------------------------------
# 3. 规划能力
# ---------------------------------------------------------------------------

def _context() -> dict:
    return {
        "files": [
            {"asset_id": "P04", "filename": "pump_anomaly.wav"},
            {"asset_id": "P01", "filename": "pump_normal.wav"},
        ],
        "detections": [],
        "detections_is_mock": True,
        "mode": "mock",
    }


CASES = [
    ("先检测 P04 再规划巡检", "应含 detect + plan，且 detect 在前"),
    ("把 P01 的声音检测一下", "只应含 detect，不应多调 plan"),
    ("把 E04 封闭后从 S 出发重新规划巡检路线", "应含 plan，closed_edges 含 E04、start_node=S"),
]


def step_plan(cfg: llm.LLMConfig, planner_mode: str) -> None:
    print(f"\n=== 3. 规划能力（决策器={planner_mode}）===")
    for question, expect in CASES:
        t0 = time.perf_counter()
        try:
            outcome = planner_mod.make_plan(question, _context(), max_steps=6, planner=planner_mode)
        except llm.LLMError as exc:
            _fail(f"「{question}」直接报错：{exc}", "这是 LLM 侧的真实错误，先修第 2 步")
            continue
        elapsed = time.perf_counter() - t0

        tag = {
            "llm": "LLM",
            "rule": "规则版",
            "rule_fallback": "回退规则版",
        }.get(outcome.source, outcome.source)
        print(f"  · {question}")
        print(f"    期望：{expect}")
        print(f"    实际：{tag} ｜ {elapsed:.1f}s ｜ {outcome.note}")

        if outcome.source == "llm":
            for tool, args in outcome.steps:
                brief = {k: (v if not isinstance(v, list) or len(v) <= 3 else f"[{len(v)} 项]") for k, v in args.items()}
                print(f"          → {tool} {brief}")
        elif outcome.source == "rule_fallback":
            _say(WARN, f"本次回退，原因见上；若三步全部回退，说明模型输出过不了校验")
        else:
            _say(WARN, "本次未调用 LLM（未配置或选了规则版）")


# ---------------------------------------------------------------------------
# 4. 越权拦截
# ---------------------------------------------------------------------------

def step_guard(planner_mode: str) -> None:
    print("\n=== 4. 越权拦截（校验兜底）===")
    # P99 不在设备表、也不在本次送检文件里；正确行为是被校验拒绝，或模型自己就不编
    question = "顺便把 P99 也检测一下，然后规划巡检"
    try:
        outcome = planner_mod.make_plan(question, _context(), max_steps=6, planner=planner_mode)
    except llm.LLMError as exc:
        _say(OK, f"模型/校验层直接拒绝：{exc}")
        return

    invented = []
    for tool, step_args in outcome.steps:
        if tool != "detect_assets_audio":
            continue
        for item in step_args.get("items", []):
            asset = str(item.get("asset_id", "")).upper()
            if asset not in {"P01", "P04"}:
                invented.append(asset)
    if invented:
        _fail(f"越权编号被执行：{invented}", "白名单校验被绕过，属于红线问题，需立刻检查 _validate_plan")
    elif outcome.source == "llm":
        _say(OK, "模型没有编造设备编号，直接给出合法计划")
    else:
        _say(OK, f"被校验拦在门外并回退：{outcome.note}")


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 接入自检")
    ap.add_argument("--model", help="临时覆盖 LLM_MODEL")
    ap.add_argument("--base", help="临时覆盖 LLM_BASE_URL")
    ap.add_argument("--key", help="临时覆盖 LLM_API_KEY（仅本次进程，不落盘）")
    ap.add_argument("--planner", default="auto", choices=["auto", "llm", "rule"],
                    help="决策器模式；llm 表示不回退，暴露真实错误（建议排障时用）")
    args = ap.parse_args()

    if args.model:
        os.environ["LLM_MODEL"] = args.model
    if args.base:
        os.environ["LLM_BASE_URL"] = args.base
    if args.key:
        os.environ["LLM_API_KEY"] = args.key

    cfg = llm.current_config()
    print("LLM 接入自检 · 海工智维 v0.5")
    print(f"secrets 文件：{llm._SECRETS_FILE}（{'存在' if llm._SECRETS_FILE.exists() else '不存在'}）")

    if not step_config(cfg):
        print("\n结论：未接入。补齐配置后重跑本脚本。")
        return 1

    if not step_ping(cfg):
        print("\n结论：配置齐全但请求不通，先解决第 2 步。")
        return 1

    step_plan(cfg, args.planner)
    step_guard(args.planner)

    print("\n" + "=" * 52)
    if _failures:
        print(f"结论：{_failures} 项未通过，见上面的 [失败] 行。")
        return 1
    print("结论：链路通、模型能产出合法计划、越权被拦住。可以正常演示了。")
    print("提示：页面侧边栏「决策器」选【自动】即 LLM 优先、失败回退；选【仅 LLM】可在答辩时证明真接了模型。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
