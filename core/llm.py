# -*- coding: utf-8 -*-
"""LLM 客户端（OpenAI 兼容协议）。

用途单一：只给 Agent 的决策器用——把自然语言任务翻译成工具调用序列。
它不生成任何检测结果、规划结果或检查结论，业务数据一律来自 A / D / 知识库。

红线：
- 密钥只从环境变量或 Streamlit Secrets 读取，不写进代码、不打印到页面；
- 未配置 / 超时 / 返回不可解析 / 越权工具，一律抛 LLMError，由调用方回退规则版决策器；
- 回退时必须在执行轨迹里写明原因，不能悄悄换掉。

配置（环境变量或 .streamlit/secrets.toml 同名键）：
    LLM_BASE_URL   https://api.deepseek.com/v1   （务必带版本段，代码只在其后拼 /chat/completions）
    LLM_API_KEY    sk-...
    LLM_MODEL      deepseek-v4-flash              （旧名 deepseek-chat 已于 2026-07-24 停用，会 404）
    LLM_TIMEOUT    30                             （秒，可选）
    LLM_TEMPERATURE 0                             （可选，决策任务建议 0）
    LLM_THINKING   auto                           （可选：auto 对 deepseek 关闭思考 / 0 关 / 1 开）

旧名 LLM_API_BASE 仍可识别（等价于 LLM_BASE_URL），避免已部署环境改配置。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import requests


class LLMError(RuntimeError):
    """LLM 调用或输出解析失败。调用方据此回退规则版决策器。"""


@dataclass(frozen=True)
class LLMConfig:
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 30.0
    temperature: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_base and self.api_key and self.model)

    @property
    def endpoint(self) -> str:
        return self.api_base.rstrip("/") + "/chat/completions"

    @property
    def key_hint(self) -> str:
        """给页面看的部分遮蔽，不输出完整密钥。"""
        if not self.api_key:
            return "未设置"
        return f"{self.api_key[:4]}…{self.api_key[-4:]}" if len(self.api_key) > 8 else "已设置"

    @property
    def missing(self) -> list[str]:
        out = []
        if not self.api_base:
            out.append("LLM_BASE_URL")
        if not self.api_key:
            out.append("LLM_API_KEY")
        if not self.model:
            out.append("LLM_MODEL")
        return out


_SECRETS_FILE = Path(__file__).resolve().parents[1] / ".streamlit" / "secrets.toml"


@lru_cache(maxsize=8)
def _load_secrets(_mtime: float) -> dict:
    """按文件 mtime 缓存解析结果：改了 secrets.toml 不必重启进程即可重读。"""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        return {}
    try:
        with _SECRETS_FILE.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _from_secrets_file(name: str) -> str:
    """直接读 .streamlit/secrets.toml。

    不用 st.secrets，原因有两个：
    1. st.secrets 只在 `streamlit run` 进程里可用，脱离 Streamlit 跑脚本（自检、单测）读不到；
    2. Streamlit 首次读取 secrets 时会把文件里的键**写进 os.environ**，从而覆盖外部环境变量，
       导致「同一进程里前后两次读到的配置不一样」。纯标准库读文件没有这个副作用。
    """
    try:
        mtime = _SECRETS_FILE.stat().st_mtime
    except OSError:
        return ""
    value = _load_secrets(mtime).get(name)
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _setting(name: str, default: str = "") -> str:
    """取值优先级：显式设置的环境变量 > secrets.toml > st.secrets > 默认值。

    「显式」的判定：环境变量与文件值不同。两者相同说明那个环境变量是 Streamlit 从
    secrets.toml 同步过来的，不算用户覆盖。这样既支持用环境变量临时换模型/换密钥，
    又不会因为文件里留了空串就把真实环境变量冲掉。
    """
    env_value = (os.environ.get(name) or "").strip()
    file_value = _from_secrets_file(name)

    if env_value and env_value != file_value:
        return env_value
    if file_value:
        return file_value
    if env_value:
        return env_value

    try:  # 兜底：secrets 由平台注入、且未落到本地文件的场景
        import streamlit as st

        if name in st.secrets:
            value = str(st.secrets[name]).strip()
            if value:
                return value
    except Exception:
        pass
    return default


def _base_url() -> str:
    """接口地址：新名 LLM_BASE_URL 优先，兼容旧名 LLM_API_BASE。"""
    return _setting("LLM_BASE_URL") or _setting("LLM_API_BASE")


def current_config() -> LLMConfig:
    return LLMConfig(
        api_base=_base_url(),
        api_key=_setting("LLM_API_KEY"),
        model=_setting("LLM_MODEL"),
        timeout=float(_setting("LLM_TIMEOUT", "30")),
        temperature=float(_setting("LLM_TEMPERATURE", "0")),
    )


def _extract_json(text: str) -> dict:
    """从模型输出里取出 JSON 对象，容忍 ```json 围栏和前后废话。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回空内容")

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("模型输出不是 JSON")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型输出 JSON 解析失败：{exc}") from exc

    if not isinstance(data, dict):
        raise LLMError("模型输出的 JSON 顶层不是对象")
    return data


def _thinking_fields(cfg: LLMConfig) -> dict:
    """按模型家族生成「要不要先思考」的字段。

    背景：DeepSeek V4 和 Qwen3.5+ 都默认开启思考模式（先输出思维链再给答案）。
    本项目的决策器只需要吐一段 JSON，思考纯属多等几秒、多付输出 token，
    而且思考模式下 temperature 会被忽略（我们要的就是 temperature=0 的稳定性）。
    两家字段名不一样，所以按模型名前缀分派；不认识的服务商一个字段都不发。
    """
    raw = _setting("LLM_THINKING", "auto").strip().lower()
    if raw in {"1", "on", "true", "yes", "enabled"}:
        want = True
    elif raw in {"0", "off", "false", "no", "disabled"}:
        want = False
    else:  # auto：已知默认开思考的家族就显式关闭，其余不管
        want = None

    model = cfg.model.lower()
    if "deepseek" in model:
        return {"thinking": {"type": "disabled" if want is not True else "enabled"}}
    if "qwen" in model:
        return {"enable_thinking": bool(want) if want is not None else False}
    return {}


def chat_json(messages: list[dict], config: LLMConfig | None = None) -> dict:
    """发一轮对话，返回解析后的 JSON 对象。任何异常统一抛 LLMError。"""
    cfg = config or current_config()
    if not cfg.configured:
        raise LLMError("LLM 未配置：" + "、".join(cfg.missing))

    payload: dict = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "stream": False,
    }
    think_fields = _thinking_fields(cfg)
    payload.update(think_fields)

    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }

    def _post(body: dict) -> requests.Response:
        return requests.post(
            cfg.endpoint,
            headers=headers,
            json=body,
            timeout=(10, cfg.timeout),
        )

    body_full = {**payload, "response_format": {"type": "json_object"}}
    body_no_think = {k: v for k, v in body_full.items() if k not in think_fields}
    body_plain = dict(payload)

    try:
        resp = _post(body_full)
        if resp.status_code == 400 and think_fields:
            # 少数兼容实现不认思考开关字段：去掉它，但保留 JSON 模式
            resp = _post(body_no_think)
        if resp.status_code == 400:
            # 再退一步：连 response_format 也不发，靠提示词约束输出 JSON
            resp = _post(body_plain)
    except requests.Timeout as exc:
        raise LLMError(f"LLM 请求超时（{cfg.timeout:g}s）") from exc
    except requests.RequestException as exc:
        raise LLMError(f"LLM 请求失败：{type(exc).__name__}") from exc

    if resp.status_code != 200:
        detail = ""
        try:
            body = resp.json()
            detail = (body.get("error") or {}).get("message") or body.get("message") or ""
        except Exception:
            detail = resp.text[:200]
        raise LLMError(f"LLM 返回 HTTP {resp.status_code}{'：' + detail if detail else ''}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError("LLM 响应结构异常（缺少 choices[0].message.content）") from exc

    return _extract_json(content)
