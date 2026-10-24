# -*- coding: utf-8 -*-
"""全局配置：全部通过环境变量注入。

红线：接口地址、密钥一律不写死在代码里（实施手册第十二章）。
"""
from __future__ import annotations

import os


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


class Settings:
    """运行配置。改环境变量后需重启应用生效。"""

    # 运行模式：mock=全部走本地模拟数据；real=调用真实接口
    app_mode: str = _get("APP_MODE", "mock")

    # A 的声音模型 API（音频批量检测）
    audio_api_base: str = _get("AUDIO_API_BASE", "http://127.0.0.1:8001")

    # D 的巡检算法 API
    inspect_api_base: str = _get("INSPECT_API_BASE", "http://127.0.0.1:8002")

    # HTTP 超时（秒）：连接 / 读取
    connect_timeout: float = float(_get("HTTP_CONNECT_TIMEOUT", "5"))
    read_timeout: float = float(_get("HTTP_READ_TIMEOUT", "30"))


settings = Settings()
