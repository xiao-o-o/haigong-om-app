"""D 模块：巡检优先级、路网巡检顺序与不可达处理。

公开接口只有 plan_inspection，签名保持为：

    plan_inspection(detection_results, start_node="S",
                    closed_edges=None, output_dir="runs") -> dict

规划规则：
1. 只把 status="suspected_anomaly" 的设备作为检查目标；
2. priority 按 criticality 从高到低排列，同等级按 asset_id 升序，
   仅表示重要等级约束，不等同于实际 visit_order；
3. visit_order 在满足“重要等级不升高”的全部排列中枚举，
   选择沿可用路网累计距离最短者；同距离时按资产编号元组字典序稳定选择；
4. 距离使用 networkx 以 length_m 为权重的最短路径，不使用直线距离或边数；
5. 从 start_node 出发，不强制回到起点；
6. 不可达目标不进入 visit_order；没有实际位移时 route_nodes 保留 [start_node]；
7. 图像由 C 的 spatial_module.visualize.render_route 生成，失败时抛 PlanSystemError。

本模块只读取 scene/ 下三张表，不改写场景，不推断具体故障。
"""
from __future__ import annotations

import itertools
import math
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import networkx as nx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spatial_module.visualize import (  # noqa: E402
    render_plan_comparison,
    render_route,
)

SCHEMA_VERSION = "1.0"
STATUS_OK = "ok"
STATUS_NO_TARGETS = "no_targets"
STATUS_PARTIAL = "partial"
STATUS_INVALID = "invalid_input"
VALID_DETECTION_STATUSES = {"normal", "suspected_anomaly", "invalid_input"}
CRITICALITY_VALUES = {"1", "2", "3"}
ASSET_COLUMNS = ["asset_id", "node_id", "criticality", "name", "simulation"]
NODE_COLUMNS = ["node_id", "x_m", "y_m"]
EDGE_COLUMNS = ["edge_id", "from_node", "to_node", "length_m", "enabled"]
DISTANCE_TOL = 1e-9


class SceneDataError(ValueError):
    """场景表缺失或不符合约定。"""


class PlanInputError(ValueError):
    """调用方传入的检测结果、起点或关闭通道不合法。"""


class PlanSystemError(RuntimeError):
    """绘图、输出目录等系统性问题；不能伪装成成功。"""


@dataclass
class Scene:
    assets: dict[str, dict[str, Any]]
    nodes: dict[str, tuple[float, float]]
    edges: dict[str, dict[str, Any]]
    graph: nx.Graph


@dataclass
class DetectionSummary:
    targets: list[str]
    invalid_notes: list[str]
    provided: list[str]


@dataclass
class PlanData:
    scene: Scene
    start: str
    closed_edges: list[str]
    targets: list[str]
    priority: list[dict[str, Any]]
    unreachable: list[str]
    reachable: list[str]
    visit_order: list[str]
    route_nodes: list[str]
    total_distance_m: float
    notes: list[str]
    optimized_elapsed_ms: float
    baseline_order: list[str] = field(default_factory=list)
    baseline_route_nodes: list[str] = field(default_factory=list)
    baseline_total_distance_m: float = 0.0
    baseline_elapsed_ms: float = 0.0


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _read_scene_table(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.is_file():
        raise SceneDataError(f"缺少场景文件：{_display_path(path)}")

    try:
        table = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except (OSError, UnicodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        raise SceneDataError(f"无法读取 {_display_path(path)}：{exc}") from exc

    missing = [column for column in columns if column not in table.columns]
    if missing:
        raise SceneDataError(
            f"{_display_path(path)} 缺少必需列：{missing}"
        )
    if table.empty:
        raise SceneDataError(f"{_display_path(path)} 是空表")

    for column in columns:
        blank = table[column].astype(str).str.strip() == ""
        if bool(blank.any()):
            rows = (table.index[blank] + 2).tolist()
            raise SceneDataError(
                f"{_display_path(path)} 的 {column} 列存在空值（CSV 行号：{rows}）"
            )

    return table[columns].copy()


def _parse_bool(value: str, label: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized not in {"true", "false"}:
        raise SceneDataError(
            f"{label} 的布尔值必须显式写 true 或 false，实际为 {value!r}"
        )
    return normalized == "true"


def _parse_finite_float(value: str, label: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SceneDataError(f"{label} 不是有效数值：{value!r}") from exc
    if not math.isfinite(number):
        raise SceneDataError(f"{label} 必须是有限数值，实际为 {value!r}")
    if positive and number <= 0:
        raise SceneDataError(f"{label} 必须大于 0，实际为 {value!r}")
    return number


def _load_scene(scene_dir: str | Path) -> Scene:
    """读取并校验三张场景表；所有节点都保留在图中，包括孤立节点。"""
    folder = Path(scene_dir)
    if not folder.is_absolute():
        folder = ROOT / folder
    folder = folder.resolve()

    if not folder.is_dir():
        raise SceneDataError(f"场景目录不存在：{_display_path(folder)}")

    assets_table = _read_scene_table(folder / "assets.csv", ASSET_COLUMNS)
    nodes_table = _read_scene_table(folder / "nodes.csv", NODE_COLUMNS)
    edges_table = _read_scene_table(folder / "edges.csv", EDGE_COLUMNS)

    nodes: dict[str, tuple[float, float]] = {}
    for row in nodes_table.to_dict("records"):
        node_id = str(row["node_id"]).strip()
        if node_id in nodes:
            raise SceneDataError(f"nodes.csv 存在重复 node_id：{node_id}")
        x = _parse_finite_float(row["x_m"], f"节点 {node_id} 的 x_m")
        y = _parse_finite_float(row["y_m"], f"节点 {node_id} 的 y_m")
        nodes[node_id] = (x, y)

    if "S" not in nodes:
        raise SceneDataError("nodes.csv 缺少默认起点 S")

    assets: dict[str, dict[str, Any]] = {}
    for row in assets_table.to_dict("records"):
        asset_id = str(row["asset_id"]).strip()
        node_id = str(row["node_id"]).strip()
        criticality_text = str(row["criticality"]).strip()

        if asset_id in assets:
            raise SceneDataError(f"assets.csv 存在重复 asset_id：{asset_id}")
        if node_id not in nodes:
            raise SceneDataError(
                f"设备 {asset_id} 引用了不存在的节点：{node_id}"
            )
        if criticality_text not in CRITICALITY_VALUES:
            raise SceneDataError(
                f"设备 {asset_id} 的 criticality 必须为 1、2 或 3，"
                f"实际为 {criticality_text!r}"
            )

        simulation = _parse_bool(
            row["simulation"], f"设备 {asset_id} 的 simulation"
        )
        assets[asset_id] = {
            "asset_id": asset_id,
            "node_id": node_id,
            "criticality": int(criticality_text),
            "name": str(row["name"]).strip(),
            "simulation": simulation,
        }

    edges: dict[str, dict[str, Any]] = {}
    pairs: dict[frozenset[str], str] = {}
    graph = nx.Graph()
    graph.add_nodes_from(nodes)

    for row in edges_table.to_dict("records"):
        edge_id = str(row["edge_id"]).strip()
        from_node = str(row["from_node"]).strip()
        to_node = str(row["to_node"]).strip()

        if edge_id in edges:
            raise SceneDataError(f"edges.csv 存在重复 edge_id：{edge_id}")
        if from_node not in nodes or to_node not in nodes:
            missing = [
                node for node in (from_node, to_node) if node not in nodes
            ]
            raise SceneDataError(
                f"通道 {edge_id} 引用了不存在的节点："
                f"{'、'.join(sorted(set(missing)))}"
            )
        if from_node == to_node:
            raise SceneDataError(f"通道 {edge_id} 的起点和终点不能相同")

        length = _parse_finite_float(
            row["length_m"], f"通道 {edge_id} 的 length_m", positive=True
        )
        enabled = _parse_bool(row["enabled"], f"通道 {edge_id} 的 enabled")

        pair = frozenset((from_node, to_node))
        if pair in pairs:
            raise SceneDataError(
                f"通道 {edge_id} 与 {pairs[pair]} 重复连接同一对节点："
                f"{from_node} - {to_node}"
            )
        pairs[pair] = edge_id

        edges[edge_id] = {
            "edge_id": edge_id,
            "from_node": from_node,
            "to_node": to_node,
            "length_m": length,
            "enabled": enabled,
        }

        if enabled:
            graph.add_edge(
                from_node,
                to_node,
                length_m=length,
                edge_id=edge_id,
            )

    return Scene(assets=assets, nodes=nodes, edges=edges, graph=graph)


def _available_graph(scene: Scene, closed_edges: list[str]) -> nx.Graph:
    """复制启用的路网，再按 edge_id 移除临时关闭通道。"""
    graph = scene.graph.copy()
    for edge_id in closed_edges:
        edge = scene.edges[edge_id]
        if graph.has_edge(edge["from_node"], edge["to_node"]):
            graph.remove_edge(edge["from_node"], edge["to_node"])
    return graph


def _validate_route_inputs(
    start_node: Any, closed_edges: Any, scene: Scene
) -> tuple[str, list[str]]:
    if not isinstance(start_node, str) or not start_node.strip():
        raise PlanInputError("start_node 必须是非空节点编号字符串")
    if start_node not in scene.nodes:
        raise PlanInputError(
            f"未知起点 {start_node!r}；可用节点为：{', '.join(scene.nodes)}"
        )

    if closed_edges is None:
        raw_closed: list[Any] = []
    elif isinstance(closed_edges, (list, tuple, set)):
        raw_closed = list(closed_edges)
    else:
        raise PlanInputError("closed_edges 必须是通道编号列表，或使用默认值 None")

    seen: set[str] = set()
    unknown: list[str] = []
    duplicates: list[str] = []
    for value in raw_closed:
        if not isinstance(value, str) or not value.strip():
            raise PlanInputError(
                "closed_edges 中每一项都必须是 non-empty 的 edge_id 字符串"
            )
        edge_id = value.strip()
        if edge_id not in scene.edges:
            unknown.append(edge_id)
        elif edge_id in seen:
            duplicates.append(edge_id)
        else:
            seen.add(edge_id)

    if unknown:
        raise PlanInputError(
            f"未知关闭通道：{sorted(set(unknown))}；"
            "必须使用 scene/edges.csv 中真实的 edge_id"
        )
    if duplicates:
        raise PlanInputError(
            f"closed_edges 中存在重复通道：{sorted(set(duplicates))}"
        )

    return start_node, sorted(seen)


def _detection_messages(item: dict[str, Any]) -> list[str]:
    """兼容 A 的 notes（列表/字符串）和 message（字符串）。"""
    messages: list[str] = []
    for key in ("notes", "message"):
        value = item.get(key)
        if isinstance(value, (list, tuple)):
            messages.extend(
                str(part).strip()
                for part in value
                if str(part).strip()
            )
        elif isinstance(value, str) and value.strip():
            messages.append(value.strip())
    return messages


def _validate_detections(
    detection_results: Any, scene: Scene
) -> DetectionSummary:
    if not isinstance(detection_results, list):
        raise PlanInputError(
            "detection_results 必须是列表，列表元素为 A 返回的结果字典"
        )

    targets: list[str] = []
    invalid_notes: list[str] = []
    provided: list[str] = []
    seen: set[str] = set()
    errors: list[str] = []

    for index, item in enumerate(detection_results, 1):
        if not isinstance(item, dict):
            errors.append(f"第 {index} 条检测结果不是字典")
            continue

        asset_value = item.get("asset_id")
        status_value = item.get("status")

        if not isinstance(asset_value, str) or not asset_value.strip():
            errors.append(f"第 {index} 条检测结果缺少非空 asset_id")
            continue
        asset_id = asset_value.strip()

        if not isinstance(status_value, str) or not status_value.strip():
            errors.append(f"设备 {asset_id} 缺少非空 status")
            continue
        status = status_value.strip()

        if asset_id not in scene.assets:
            errors.append(f"未知设备：{asset_id}")
        if asset_id in seen:
            errors.append(f"设备 {asset_id} 出现重复检测结果，不能任意取其中一条")
        else:
            seen.add(asset_id)
            provided.append(asset_id)

        if status not in VALID_DETECTION_STATUSES:
            errors.append(
                f"设备 {asset_id} 的 status={status!r} 不受支持；"
                "只允许 normal、suspected_anomaly、invalid_input"
            )
            continue

        if status == "suspected_anomaly":
            targets.append(asset_id)
        elif status == "invalid_input":
            detail = "；".join(_detection_messages(item))
            suffix = f" A 返回提示：{detail}" if detail else ""
            invalid_notes.append(
                f"{asset_id} 的录音状态为 invalid_input，需要补录；"
                f"本次不把它当作正常设备，也不加入检查目标。{suffix}".rstrip()
            )

    if errors:
        raise PlanInputError("；".join(errors))

    return DetectionSummary(
        targets=targets,
        invalid_notes=invalid_notes,
        provided=provided,
    )


def _priority_list(targets: list[str], scene: Scene) -> list[dict[str, Any]]:
    ordered = sorted(
        targets,
        key=lambda asset_id: (
            -scene.assets[asset_id]["criticality"],
            asset_id,
        ),
    )
    priority: list[dict[str, Any]] = []
    for asset_id in ordered:
        criticality = scene.assets[asset_id]["criticality"]
        priority.append(
            {
                "asset_id": asset_id,
                "criticality": criticality,
                "reason": (
                    f"重要等级 {criticality}/3；检测状态 suspected_anomaly；"
                    "先按等级降序，同等级再按路网距离优化；"
                    "异常分数不参与排序，也不作为故障概率"
                ),
            }
        )
    return priority


def _pair_paths(
    graph: nx.Graph, start: str, targets: list[str], scene: Scene
) -> tuple[dict[tuple[str, str], list[str]], dict[tuple[str, str], float]]:
    points: list[str] = []
    for node_id in [start] + [scene.assets[asset_id]["node_id"] for asset_id in targets]:
        if node_id not in points:
            points.append(node_id)

    paths: dict[tuple[str, str], list[str]] = {}
    distances: dict[tuple[str, str], float] = {}

    for source in points:
        source_distances, source_paths = nx.single_source_dijkstra(
            graph, source, weight="length_m"
        )
        for destination in points:
            if destination not in source_distances:
                continue
            key = (source, destination)
            distances[key] = float(source_distances[destination])
            paths[key] = list(source_paths[destination])

    return paths, distances


def _route_for_order(
    order: list[str],
    start: str,
    scene: Scene,
    paths: dict[tuple[str, str], list[str]],
    distances: dict[tuple[str, str], float],
) -> tuple[list[str], float] | None:
    route = [start]
    total = 0.0
    previous = start

    for asset_id in order:
        node_id = scene.assets[asset_id]["node_id"]
        key = (previous, node_id)
        if key not in paths:
            return None
        path = paths[key]
        if path and path[0] != previous:
            return None
        if len(path) > 1:
            route.extend(path[1:])
        total += distances[key]
        previous = node_id

    return route, total


def _optimized_order(
    reachable: list[str],
    start: str,
    scene: Scene,
    paths: dict[tuple[str, str], list[str]],
    distances: dict[tuple[str, str], float],
) -> tuple[list[str], list[str], float]:
    """枚举所有满足重要等级降序的访问顺序，按总距离再按编号稳定择优。"""
    if not reachable:
        return [], [start], 0.0

    best_order: list[str] | None = None
    best_route: list[str] = [start]
    best_total = math.inf

    for candidate in itertools.permutations(reachable):
        criticalities = [
            scene.assets[asset_id]["criticality"] for asset_id in candidate
        ]
        if any(
            left < right
            for left, right in zip(criticalities, criticalities[1:])
        ):
            continue

        evaluated = _route_for_order(
            list(candidate), start, scene, paths, distances
        )
        if evaluated is None:
            continue
        route, total = evaluated

        if best_order is None:
            take = True
        elif total < best_total - DISTANCE_TOL:
            take = True
        elif math.isclose(total, best_total, rel_tol=0.0, abs_tol=DISTANCE_TOL):
            # 等距离方案使用资产编号元组字典序，保证相同输入可复现。
            take = candidate < tuple(best_order)
        else:
            take = False

        if take:
            best_order = list(candidate)
            best_route = route
            best_total = total

    if best_order is None:
        # 理论上不会发生：reachable 的目标都有从起点出发的路径。
        return [], [start], 0.0
    return best_order, best_route, best_total


def _fixed_order_baseline(
    reachable: list[str],
    start: str,
    scene: Scene,
    paths: dict[tuple[str, str], list[str]],
    distances: dict[tuple[str, str], float],
) -> tuple[list[str], list[str], float]:
    """固定编号顺序基线：重要等级降序，同等级 asset_id 升序。"""
    order = sorted(
        reachable,
        key=lambda asset_id: (
            -scene.assets[asset_id]["criticality"],
            asset_id,
        ),
    )
    evaluated = _route_for_order(order, start, scene, paths, distances)
    if evaluated is None:
        raise PlanSystemError("基线路线拼接失败：目标在可用路网中不可达")
    route, total = evaluated
    return order, route, total


def _analyse_plan(
    detection_results: Any,
    start_node: str,
    closed_edges: Any,
    scene_dir: str | Path | None = None,
) -> PlanData:
    """内部核心：校验、建网、枚举、基线和说明；不生成地图。"""
    scene = _load_scene(scene_dir or (ROOT / "scene"))
    start, closed = _validate_route_inputs(start_node, closed_edges, scene)
    detection = _validate_detections(detection_results, scene)

    targets = detection.targets
    priority = _priority_list(targets, scene)
    graph = _available_graph(scene, closed)
    paths, distances = _pair_paths(graph, start, targets, scene)

    unreachable = [
        asset_id
        for asset_id in targets
        if (start, scene.assets[asset_id]["node_id"]) not in paths
    ]
    reachable = [asset_id for asset_id in targets if asset_id not in unreachable]

    started = time.perf_counter()
    visit_order, route_nodes, total = _optimized_order(
        reachable, start, scene, paths, distances
    )
    optimized_elapsed_ms = (time.perf_counter() - started) * 1000.0

    started = time.perf_counter()
    baseline_order, baseline_route, baseline_total = _fixed_order_baseline(
        reachable, start, scene, paths, distances
    )
    baseline_elapsed_ms = (time.perf_counter() - started) * 1000.0

    notes: list[str] = [
        "priority 是重要等级约束下的优先级清单，不等同于 visit_order；"
        "visit_order 是满足约束后按路网距离优化得到的实际检查顺序。",
        "距离按可用通道 length_m 的最短路径累计，不是设备间直线距离；"
        "从起点出发，不强制返回起点。",
        "异常分数只表示模型相对异常程度，不是故障概率；"
        "本规划未据此推断具体故障。",
    ]

    if targets:
        notes.append(
            "检查顺序先满足重要等级从高到低，再在所有可行顺序中"
            "枚举总行走距离最短的方案；等距离时按资产编号稳定选择。"
        )
    else:
        notes.append(
            "没有 status=suspected_anomaly 的检查目标，本次不安排巡检。"
        )

    if closed:
        notes.append(
            f"本次临时关闭通道（按 edge_id）：{'、'.join(closed)}。"
        )

    notes.extend(detection.invalid_notes)

    missing = sorted(set(scene.assets) - set(detection.provided))
    if missing:
        notes.append(
            "本次未提供检测结果的设备："
            f"{'、'.join(missing)}；不能据此推断这些设备正常。"
        )

    for asset_id in unreachable:
        node_id = scene.assets[asset_id]["node_id"]
        closed_text = (
            f"；当前关闭通道：{'、'.join(closed)}" if closed else ""
        )
        isolated_text = (
            "；该设备节点没有可用通道"
            if graph.degree(node_id) == 0
            else ""
        )
        notes.append(
            f"{asset_id} 不可达：从 {start} 无法沿开放通道到达 {node_id}"
            f"{closed_text}{isolated_text}。"
            "该设备保留在 unreachable_asset_ids，不进入 visit_order。"
        )

    if targets and unreachable:
        notes.append(
            "状态为 partial：存在不可达的异常目标，可达目标已继续规划；"
            "不可达目标没有被误报为已检查。"
        )
        if not reachable:
            notes.append("所有异常目标均不可达，本次没有实际行走任务。")

    if not route_nodes or len(route_nodes) == 1:
        notes.append(
            f"本次没有产生实际位移，route_nodes 按统一约定保留起点 [{start}]，"
            "total_distance_m 为 0。"
        )

    return PlanData(
        scene=scene,
        start=start,
        closed_edges=closed,
        targets=targets,
        priority=priority,
        unreachable=unreachable,
        reachable=reachable,
        visit_order=visit_order,
        route_nodes=route_nodes,
        total_distance_m=float(total),
        notes=notes,
        optimized_elapsed_ms=optimized_elapsed_ms,
        baseline_order=baseline_order,
        baseline_route_nodes=baseline_route,
        baseline_total_distance_m=float(baseline_total),
        baseline_elapsed_ms=baseline_elapsed_ms,
    )


def _invalid_result(notes: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_INVALID,
        "start_node": None,
        "closed_edges": [],
        "target_asset_ids": [],
        "reachable_asset_ids": [],
        "priority": [],
        "visit_order": [],
        "route_nodes": [],
        "total_distance_m": None,
        "unreachable_asset_ids": [],
        "map_file": None,
        "baseline": None,
        "timing": None,
        "notes": [str(note) for note in notes],
    }


def _create_run_directory(output_dir: str | Path) -> Path:
    if not isinstance(output_dir, (str, Path)):
        raise PlanSystemError("output_dir 必须是项目根目录内的相对路径或绝对路径")

    base = Path(output_dir)
    if not base.is_absolute():
        base = ROOT / base
    try:
        base = base.resolve()
    except OSError as exc:
        raise PlanSystemError(f"无法解析 output_dir：{exc}") from exc

    if not base.is_relative_to(ROOT):
        raise PlanSystemError(
            "output_dir 必须位于项目根目录内，保证 map_file 可以返回相对路径"
        )

    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PlanSystemError(f"无法创建输出目录 {base}：{exc}") from exc

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = base / f"d_plan_{stamp}_{uuid.uuid4().hex[:8]}"
    try:
        run_dir.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        raise PlanSystemError(f"无法创建本次独立输出目录 {run_dir}：{exc}") from exc
    return run_dir


def _checked_png_path(returned: Any, label: str) -> Path:
    """检查绘图函数返回的路径确实位于项目内、存在且是 PNG。"""
    returned_path = Path(str(returned))
    if returned_path.is_absolute():
        try:
            returned_path = returned_path.resolve().relative_to(ROOT)
        except ValueError as exc:
            raise PlanSystemError(
                f"{label}返回了项目根目录之外的路径：{returned}"
            ) from exc
    absolute_path = (ROOT / returned_path).resolve()

    if not absolute_path.is_relative_to(ROOT):
        raise PlanSystemError(f"{label}路径越出项目根目录：{returned}")
    if absolute_path.suffix.lower() != ".png":
        raise PlanSystemError(f"{label}返回的不是 PNG：{returned}")
    if not absolute_path.is_file():
        raise PlanSystemError(f"{label}声称成功，但文件不存在：{returned}")

    try:
        signature = absolute_path.read_bytes()[:8]
    except OSError as exc:
        raise PlanSystemError(f"无法读取{label}：{exc}") from exc
    if signature != b"\x89PNG\r\n\x1a\n":
        raise PlanSystemError(f"{label}不是有效 PNG：{returned}")

    return returned_path


def _render_route_in_dir(
    data: PlanData,
    run_dir: Path,
    route_nodes: list[str],
    filename: str,
) -> str:
    relative = (run_dir / filename).relative_to(ROOT).as_posix()
    try:
        returned = render_route(
            "scene",
            route_nodes,
            relative,
            closed_edges=data.closed_edges,
        )
    except Exception as exc:
        raise PlanSystemError(
            f"路线图生成失败（{relative}）：{type(exc).__name__}: {exc}"
        ) from exc
    return _checked_png_path(returned, "路线图").as_posix()


def _render_map(data: PlanData, output_dir: str | Path) -> str:
    run_dir = _create_run_directory(output_dir)
    return _render_route_in_dir(data, run_dir, data.route_nodes, "route.png")


def _plan_inspection_for_scene(
    detection_results: Any,
    start_node: str,
    closed_edges: Any,
    output_dir: str | Path,
    scene_dir: Path,
) -> dict[str, Any]:
    """内部入口，允许测试临时场景副本；正常调用请使用 plan_inspection。"""
    try:
        data = _analyse_plan(
            detection_results,
            start_node,
            closed_edges,
            scene_dir=scene_dir,
        )
    except (SceneDataError, PlanInputError) as exc:
        return _invalid_result([str(exc)])

    if not data.targets:
        status = STATUS_NO_TARGETS
    elif data.unreachable:
        status = STATUS_PARTIAL
    else:
        status = STATUS_OK

    map_file = _render_map(data, output_dir)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "start_node": data.start,
        "closed_edges": data.closed_edges,
        "target_asset_ids": data.targets,
        "reachable_asset_ids": data.reachable,
        "priority": data.priority,
        "visit_order": data.visit_order,
        "route_nodes": data.route_nodes,
        "total_distance_m": round(float(data.total_distance_m), 6),
        "unreachable_asset_ids": data.unreachable,
        "map_file": map_file,
        "baseline": {
            "visit_order": data.baseline_order,
            "route_nodes": data.baseline_route_nodes,
            "total_distance_m": round(float(data.baseline_total_distance_m), 6),
            "calculation_ms": round(float(data.baseline_elapsed_ms), 3),
        },
        "timing": {
            "optimization_ms": round(float(data.optimized_elapsed_ms), 3),
            "baseline_calculation_ms": round(float(data.baseline_elapsed_ms), 3),
            "note": "单次本机运行耗时，不是可靠的性能基准。",
        },
        "notes": data.notes,
    }


def plan_inspection(
    detection_results,
    start_node="S",
    closed_edges=None,
    output_dir="runs",
) -> dict:
    """根据 A 的检测结果规划巡检顺序和完整路网路径。

    参数
    ----
    detection_results : list[dict]
        A 返回的检测结果列表，必须包含 asset_id 和 status。
    start_node : str
        起点节点编号，默认 "S"。
    closed_edges : list[str] | None
        本次临时关闭的 edge_id 列表；默认 None，不会共享可变列表。
    output_dir : str
        项目根目录内的输出目录；每次调用会再创建独立子目录。

    返回
    ----
    dict
        固定包含 schema_version、status、priority、visit_order、
        route_nodes、total_distance_m、unreachable_asset_ids、map_file、notes。
        兼容扩展字段：start_node、closed_edges、target_asset_ids、
        reachable_asset_ids、baseline（固定顺序基线）、timing（单次耗时说明）。
        status 为 ok / no_targets / partial / invalid_input。
        invalid_input 时 total_distance_m、map_file 为 None，其余列表为空。
        所有数值有限，可直接 json.dumps(..., allow_nan=False)。
    """
    return _plan_inspection_for_scene(
        detection_results,
        start_node,
        closed_edges,
        output_dir,
        scene_dir=ROOT / "scene",
    )


def _invalid_compare_result(notes: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_INVALID,
        "start_node": None,
        "closed_edges": [],
        "target_asset_ids": [],
        "reachable_asset_ids": [],
        "unreachable_asset_ids": [],
        "priority": [],
        "optimized": None,
        "baseline": None,
        "comparison": None,
        "map_files": {},
        "notes": [str(note) for note in notes],
        "timing": None,
    }


def _comparison_block(
    baseline_total: float,
    optimized_total: float,
    baseline_order: list[str],
    optimized_order: list[str],
) -> dict[str, Any]:
    """计算距离差和改善比例；基线为 0 时比例明确写不适用。"""
    difference = float(baseline_total) - float(optimized_total)
    baseline_zero = abs(float(baseline_total)) <= DISTANCE_TOL

    if baseline_zero:
        ratio: float | None = None
        percent: float | None = None
        ratio_text = "不适用（基线距离为0）"
    else:
        ratio = difference / float(baseline_total)
        percent = ratio * 100.0
        ratio_text = f"{percent:.2f}%"

    if abs(difference) <= DISTANCE_TOL:
        change = "same"
        summary = "两种顺序总距离相同。"
    elif difference > 0:
        change = "shorter"
        summary = f"优化方案比固定顺序少走 {difference:.6g} m。"
    else:
        change = "longer"
        summary = f"优化方案比固定顺序多走 {abs(difference):.6g} m。"

    if baseline_zero:
        summary += "基线距离为 0，改善比例不适用。"
    return {
        "distance_difference_m": round(difference, 6),
        "improvement_ratio": round(ratio, 6) if ratio is not None else None,
        "improvement_percent": round(percent, 2) if percent is not None else None,
        "improvement_text": ratio_text,
        "baseline_zero": baseline_zero,
        "distance_change": change,
        "same_order": list(baseline_order) == list(optimized_order),
        "summary": summary,
    }


def compare_inspection_plans(
    detection_results,
    start_node="S",
    closed_edges=None,
    output_dir="runs",
) -> dict:
    """在同一批输入上比较“固定顺序基线”和“距离优化方案”。

    两种方案共用起点、同一批可达目标、同样的重要等级约束、同样的开放通道，
    且都不强制返回起点。距离全部由 D 的路网最短路实际计算，不写死。

    除两张路线图外，还调用 C 的 render_plan_comparison 生成距离对比图。
    计算耗时与绘图耗时分别记录；单次耗时不能当作性能基准。
    """
    calculation_started = time.perf_counter()
    try:
        data = _analyse_plan(
            detection_results,
            start_node,
            closed_edges,
            scene_dir=ROOT / "scene",
        )
    except (SceneDataError, PlanInputError) as exc:
        return _invalid_compare_result([str(exc)])
    calculation_total_ms = (time.perf_counter() - calculation_started) * 1000.0

    if not data.targets:
        status = STATUS_NO_TARGETS
    elif data.unreachable:
        status = STATUS_PARTIAL
    else:
        status = STATUS_OK

    run_dir = _create_run_directory(output_dir)

    drawing_started = time.perf_counter()
    optimized_map = _render_route_in_dir(
        data, run_dir, data.route_nodes, "route_optimized.png"
    )
    optimized_map_ms = (time.perf_counter() - drawing_started) * 1000.0

    drawing_started = time.perf_counter()
    baseline_map = _render_route_in_dir(
        data, run_dir, data.baseline_route_nodes, "route_fixed.png"
    )
    baseline_map_ms = (time.perf_counter() - drawing_started) * 1000.0

    chart_relative = (run_dir / "distance_comparison.png").relative_to(ROOT).as_posix()
    drawing_started = time.perf_counter()
    try:
        returned_chart = render_plan_comparison(
            chart_relative,
            {
                "visit_order": data.baseline_order,
                "total_distance_m": data.baseline_total_distance_m,
            },
            {
                "visit_order": data.visit_order,
                "total_distance_m": data.total_distance_m,
            },
            title=f"固定顺序与优化方案距离对比 · {data.start} 起点",
            unreachable=data.unreachable,
        )
    except Exception as exc:
        raise PlanSystemError(
            f"距离对比图生成失败（{chart_relative}）：{type(exc).__name__}: {exc}"
        ) from exc
    comparison_chart = _checked_png_path(returned_chart, "距离对比图").as_posix()
    comparison_chart_ms = (time.perf_counter() - drawing_started) * 1000.0

    drawing_total_ms = optimized_map_ms + baseline_map_ms + comparison_chart_ms
    comparison = _comparison_block(
        data.baseline_total_distance_m,
        data.total_distance_m,
        data.baseline_order,
        data.visit_order,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "start_node": data.start,
        "closed_edges": data.closed_edges,
        "target_asset_ids": data.targets,
        "reachable_asset_ids": data.reachable,
        "unreachable_asset_ids": data.unreachable,
        "priority": data.priority,
        "optimized": {
            "visit_order": data.visit_order,
            "route_nodes": data.route_nodes,
            "total_distance_m": round(float(data.total_distance_m), 6),
            "map_file": optimized_map,
        },
        "baseline": {
            "visit_order": data.baseline_order,
            "route_nodes": data.baseline_route_nodes,
            "total_distance_m": round(float(data.baseline_total_distance_m), 6),
            "map_file": baseline_map,
        },
        "comparison": comparison,
        "map_files": {
            "optimized_route": optimized_map,
            "fixed_route": baseline_map,
            "comparison_chart": comparison_chart,
        },
        "notes": data.notes,
        "timing": {
            "calculation_total_ms": round(calculation_total_ms, 3),
            "optimization_ms": round(float(data.optimized_elapsed_ms), 3),
            "baseline_calculation_ms": round(float(data.baseline_elapsed_ms), 3),
            "drawing_total_ms": round(drawing_total_ms, 3),
            "optimized_map_ms": round(optimized_map_ms, 3),
            "baseline_map_ms": round(baseline_map_ms, 3),
            "comparison_chart_ms": round(comparison_chart_ms, 3),
            "note": (
                "计算与绘图耗时分别记录，均为单次本机运行值；"
                "受首次加载、字体缓存、磁盘与图像大小影响，不是可靠性能基准。"
            ),
        },
    }
