# -*- coding: utf-8 -*-
"""requests 封装的轻量 API 客户端。

红线（实施手册第十二章）：
- 只用 requests，不引重型 SDK；
- 超时 / 连接失败 / 非 2xx / 非法 JSON 一律显式抛 ApiError；
- 超时后绝不编造检测或巡检结果，由上层如实展示错误。
"""
from __future__ import annotations

from dataclasses import dataclass

import requests


class ApiError(RuntimeError):
    """接口调用失败（超时 / 连接失败 / HTTP 错误 / 返回体非法）。"""


@dataclass
class ApiResponse:
    status_code: int
    data: dict


class ApiClient:
    """极简 HTTP 客户端：连接超时 + 读取超时分开控制。"""

    def __init__(
        self,
        base_url: str,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = (connect_timeout, read_timeout)
        self._session = requests.Session()

    def get(self, path: str, params: dict | None = None) -> ApiResponse:
        return self._request("GET", path, params=params)

    def post(self, path: str, json: dict | None = None) -> ApiResponse:
        return self._request("POST", path, json=json)

    def health(self) -> bool:
        """连通性自检：GET /health。成功返回 True，失败抛 ApiError。"""
        self.get("/health")
        return True

    def _request(self, method: str, path: str, **kwargs) -> ApiResponse:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.Timeout as exc:
            raise ApiError(f"接口超时（读取上限 {self.timeout[1]:.0f}s）：{url}") from exc
        except requests.ConnectionError as exc:
            raise ApiError(f"无法连接接口：{url}") from exc
        except requests.RequestException as exc:
            raise ApiError(f"请求异常：{exc}") from exc

        if resp.status_code >= 400:
            raise ApiError(f"接口返回 HTTP {resp.status_code}：{resp.text[:200]}")
        try:
            return ApiResponse(resp.status_code, resp.json())
        except ValueError as exc:
            raise ApiError("接口返回的不是合法 JSON") from exc
