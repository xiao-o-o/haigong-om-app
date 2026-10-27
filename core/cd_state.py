# -*- coding: utf-8 -*-
"""C/D 联调用的输入状态指纹（不依赖 Streamlit，便于命令行检查）。

解决的问题：
- 同名上传文件内容变了，文件名一样，旧检测结果不能继续沿用；
- 同一批设备换了录音、换了设备映射、换了运行模式，旧检测也要失效；
- 起点或关闭通道变了，检测结果仍然有效，但旧路线必须失效。

这里只做“指纹/比较”，不读写 Streamlit 会话状态。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


def sha256_bytes(data: bytes) -> str:
    """返回字节内容的 SHA-256。"""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """按块读取文件并返回 SHA-256；文件不可读时返回空串。"""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _item_token(item: dict[str, Any], index: int) -> dict[str, str]:
    """把一条送检输入归一化成可比较的字段，不保存真实路径。"""
    asset_id = str(item.get("asset_id") or "").strip().upper()
    filename = str(item.get("filename") or "").strip()
    content_hash = str(item.get("content_sha256") or "").strip()
    if not content_hash and item.get("path"):
        content_hash = sha256_file(str(item["path"]))
    return {
        "order": str(index),
        "asset_id": asset_id,
        "filename": filename,
        "content_sha256": content_hash,
    }


def build_detection_signature(
    mode: str,
    source: str,
    items: Iterable[dict[str, Any]] | None,
) -> str:
    """构造检测输入指纹。

    mode   : mock / real
    source : 页面音频来源（内置 / 全场景 / 上传），也用于 Agent 上下文
    items  : [{asset_id, filename, path 或 content_sha256}]

    指纹包含运行模式、来源、设备映射、文件名和文件内容哈希，因此：
    - 同名文件内容变化 → 指纹变化；
    - 设备编号重新指定 → 指纹变化；
    - 内置录音换文件/被替换 → 指纹变化；
    - mock/real 切换 → 指纹变化。
    """
    tokens = [
        _item_token(item, index)
        for index, item in enumerate(items or [])
        if isinstance(item, dict)
    ]
    payload = {
        "mode": str(mode or "").strip().lower(),
        "source": str(source or "").strip(),
        "items": tokens,
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detection_result_signature(rows: Iterable[dict[str, Any]] | None) -> str:
    """检测结果集指纹：设备、状态、分数、阈值。

    用于判断“任务创建时的检测快照”是否仍对应当前显示的结果。
    同一批文件换了检测目标或结果变化时，任务应提示失效。
    """
    tokens: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        asset_id = str(row.get("asset_id") or "").strip().upper()
        if not asset_id:
            continue
        tokens.append(
            {
                "asset_id": asset_id,
                "status": str(row.get("status") or "").strip(),
                "score": str(row.get("score")),
                "threshold": str(row.get("threshold")),
            }
        )
    tokens.sort(key=lambda item: item["asset_id"])
    text = json.dumps(tokens, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_context(start_node: str | None, closed_edges: Iterable[str] | None) -> dict[str, Any]:
    """把规划参数归一化成可比较的字典。"""
    return {
        "start_node": str(start_node or "").strip().upper(),
        "closed_edges": sorted(
            {str(edge).strip().upper() for edge in (closed_edges or []) if str(edge).strip()}
        ),
    }


def same_plan_context(
    saved: dict[str, Any] | None,
    start_node: str | None,
    closed_edges: Iterable[str] | None,
) -> bool:
    """比较已保存的规划上下文和当前页面参数是否一致。"""
    if not isinstance(saved, dict):
        return False
    return saved == plan_context(start_node, closed_edges)
