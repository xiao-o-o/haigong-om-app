# -*- coding: utf-8 -*-
"""三家模块的进程内适配层（唯一 import 入口）。

背景：A / C / D 交付的是本地 Python 函数，不是 HTTP 服务。
因此 real 模式不再走 ApiClient，而是直接 import 三家模块。

路径约定（三家一致，都是「项目根的父目录」）：
    haigong-om-app/
        audio_module/     A：analyze_audio(path, asset_id)
        models/           A：pump_v1.joblib + model_config.json
        spatial_module/   D：plan_inspection(...)，内部调用 C 的 render_route
        scene/            C/D 共用场景表（assets/nodes/edges）

本模块只做三件事：
1. 保证 PROJECT_ROOT 在 sys.path 里（换工作目录也能 import）；
2. 惰性加载并缓存三家模块，import 失败时抛出可读的 ModuleUnavailable；
3. 读取 scene/ 表，供页面渲染下拉选项（不写死设备编号）。
"""
from __future__ import annotations

import csv
import sys
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENE_DIR = PROJECT_ROOT / "scene"
RUNS_DIR = PROJECT_ROOT / "runs"
SAMPLES_DIR = PROJECT_ROOT / "samples"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# scene 表字段（与 C/D 的约定一致）
ASSET_COLUMNS = ["asset_id", "node_id", "criticality", "name", "simulation"]
EDGE_COLUMNS = ["edge_id", "from_node", "to_node", "length_m", "enabled"]
NODE_COLUMNS = ["node_id", "x_m", "y_m"]

# 首版只接受 16 kHz 单声道 WAV（A 的约束，不做重采样）
EXPECTED_SAMPLE_RATE = 16000
SUPPORTED_SUFFIXES = (".wav",)


class ModuleUnavailable(RuntimeError):
    """模块缺失或依赖不兼容：属于系统错误，不能当成「设备正常」处理。"""


# ---------------------------------------------------------------------------
# 模块加载（惰性 + 缓存）
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def audio_analyzer():
    """返回 A 的 analyze_audio，签名 (audio_path, asset_id, output_dir="runs")。"""
    try:
        from audio_module.infer import analyze_audio
    except Exception as exc:  # ImportError / 依赖缺失 / 模型库不兼容
        raise ModuleUnavailable(f"A 声音模块无法加载：{type(exc).__name__}: {exc}") from exc
    return analyze_audio


@lru_cache(maxsize=1)
def route_planner():
    """返回 D 的 plan_inspection。

    签名 (detection_results, start_node="S", closed_edges=None, output_dir="runs")。
    """
    try:
        from spatial_module.plan import plan_inspection
    except Exception as exc:
        raise ModuleUnavailable(f"D 规划模块无法加载：{type(exc).__name__}: {exc}") from exc
    return plan_inspection


@lru_cache(maxsize=1)
def route_renderer():
    """返回 C 的 render_route 函数本身（注意：需要再调用一次才是绘图）。"""
    try:
        from spatial_module.visualize import render_route as _render
    except Exception as exc:
        raise ModuleUnavailable(f"C 绘图模块无法加载：{type(exc).__name__}: {exc}") from exc
    return _render


# ---------------------------------------------------------------------------
# 场景数据（供页面下拉，不写死编号）
# ---------------------------------------------------------------------------

def _read_table(name: str, columns: list[str]) -> list[dict]:
    path = SCENE_DIR / name
    if not path.is_file():
        raise ModuleUnavailable(f"场景表缺失：{path.relative_to(PROJECT_ROOT)}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    missing = [c for c in columns if not rows or c not in rows[0]]
    if missing:
        raise ModuleUnavailable(f"{name} 缺少必需列：{missing}")
    return rows


def list_assets() -> list[dict]:
    """设备清单：[{asset_id, node_id, criticality, name}]，按 asset_id 排序。"""
    rows = _read_table("assets.csv", ASSET_COLUMNS)
    return [
        {
            "asset_id": r["asset_id"],
            "node_id": r["node_id"],
            "criticality": r["criticality"],
            "name": r["name"],
        }
        for r in sorted(rows, key=lambda r: r["asset_id"])
    ]


def list_edges() -> list[dict]:
    """通道清单：[{edge_id, from_node, to_node, length_m}]，供「临时关闭通道」选择。"""
    rows = _read_table("edges.csv", EDGE_COLUMNS)
    return [
        {
            "edge_id": r["edge_id"],
            "from_node": r["from_node"],
            "to_node": r["to_node"],
            "length_m": r["length_m"],
        }
        for r in sorted(rows, key=lambda r: r["edge_id"])
    ]


def list_nodes() -> list[dict]:
    """节点清单（含起点 S），供「起点」下拉。"""
    rows = _read_table("nodes.csv", NODE_COLUMNS)
    return [{"node_id": r["node_id"]} for r in rows]


def asset_ids() -> list[str]:
    return [a["asset_id"] for a in list_assets()]


def node_ids() -> list[str]:
    ids = [n["node_id"] for n in list_nodes()]
    return ["S"] + sorted(i for i in ids if i != "S") if "S" in ids else sorted(ids)


def edge_ids() -> list[str]:
    return [e["edge_id"] for e in list_edges()]


def sample_files() -> list[Path]:
    """A 提供的两个可用演示录音（P01 正常 / P04 异常）。"""
    if not SAMPLES_DIR.is_dir():
        return []
    return sorted(p for p in SAMPLES_DIR.glob("*.wav"))


# ---------------------------------------------------------------------------
# 健康检查（页面「模块状态」区 + 验收自检用）
# ---------------------------------------------------------------------------

def check_modules() -> list[dict]:
    """逐项探测，返回 [{name, ok, detail}]，不抛异常。"""
    checks: list[dict] = []

    model_file = PROJECT_ROOT / "models" / "pump_v1.joblib"
    checks.append({
        "name": "A 声音模型文件",
        "ok": model_file.is_file(),
        "detail": (
            f"{model_file.name} · {model_file.stat().st_size / 1024:.0f} KB"
            if model_file.is_file() else "models/pump_v1.joblib 缺失"
        ),
    })

    try:
        audio_analyzer()
        checks.append({"name": "A 推理模块", "ok": True, "detail": "audio_module.infer.analyze_audio"})
    except ModuleUnavailable as exc:
        checks.append({"name": "A 推理模块", "ok": False, "detail": str(exc)})

    try:
        route_planner()
        route_renderer()
        checks.append({"name": "D 规划 + C 绘图", "ok": True, "detail": "spatial_module.plan / visualize"})
    except ModuleUnavailable as exc:
        checks.append({"name": "D 规划 + C 绘图", "ok": False, "detail": str(exc)})

    try:
        assets, edges = list_assets(), list_edges()
        checks.append({
            "name": "场景表",
            "ok": bool(assets and edges),
            "detail": f"{len(assets)} 台设备 · {len(edges)} 条通道",
        })
    except ModuleUnavailable as exc:
        checks.append({"name": "场景表", "ok": False, "detail": str(exc)})

    return checks
