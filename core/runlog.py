# -*- coding: utf-8 -*-
"""运行日志：每次任务落一份 JSON 到 runs/<run_id>/run_log.json。

必存字段（实施手册要求）：run_id、应用版本、时间、工具参数摘要、真实结果与错误。
红线：密钥、Authorization 头一律不写盘——所有内容先过 redact()。

日志是排查与验收材料，不参与业务判定：写盘失败不影响任务本身，
只在返回值里带一个空的 log_path，由页面提示，而不是抛异常打断流程。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

APP_VERSION = "0.6.0"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = PROJECT_ROOT / "runs"

# 这些键的值不落盘（大小写不敏感，覆盖常见写法）
_SECRET_KEYS = {
    "api_key", "apikey", "authorization", "auth", "token", "access_token",
    "password", "secret", "llm_api_key", "llm_base_url_key",
}
# 文本里像密钥的串也要打码（例如异常信息里带出了完整 key）
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
)


def new_run_id() -> str:
    """每次运行独立：时间戳 + 6 位随机，便于日志排序且不撞号。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def redact(obj):
    """递归脱敏：键名命中黑名单直接替换，字符串里的密钥样式打码。"""
    if isinstance(obj, dict):
        return {
            str(k): ("***" if str(k).lower() in _SECRET_KEYS else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        text = obj
        for pat in _SECRET_PATTERNS:
            text = pat.sub("***", text)
        return text
    if isinstance(obj, Path):
        return str(obj)
    return obj


def digest_args(args: dict, max_list: int = 5) -> dict:
    """参数摘要：长数组只留前几项 + 总数，日志不至于被一屏编号撑满。"""
    out: dict = {}
    for key, value in (args or {}).items():
        if isinstance(value, list):
            head = [redact(v) for v in value[:max_list]]
            out[key] = head + ([f"…共 {len(value)} 项"] if len(value) > max_list else [])
        elif isinstance(value, str) and len(value) > 120:
            out[key] = redact(value[:120] + "…")
        else:
            out[key] = redact(value)
    return out


def write_run_log(record: dict) -> str:
    """写盘并返回相对项目根的路径；失败返回空串（不影响主流程）。"""
    try:
        run_id = str(record.get("run_id") or new_run_id())
        out_dir = RUNS_DIR / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "run_log.json"
        payload = redact({"app_version": APP_VERSION, **record, "run_id": run_id})
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path.relative_to(PROJECT_ROOT).as_posix()
    except Exception:
        return ""
