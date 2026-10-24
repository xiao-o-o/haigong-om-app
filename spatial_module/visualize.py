from pathlib import Path
import math
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D

ROOT = Path(__file__).resolve().parents[1]


def _table(folder, filename, columns):
    data = pd.read_csv(
        folder / filename,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig"
    )
    if not set(columns).issubset(data.columns):
        raise ValueError(f"{filename} 缺少必需列：{columns}")

    rows = data[columns].to_dict("records")

    if not rows or any(
        not str(v).strip()
        for row in rows
        for v in row.values()
    ):
        raise ValueError(f"{filename} 存在空表或空值")

    key = columns[0]
    if len({row[key] for row in rows}) != len(rows):
        raise ValueError(f"{filename} 存在重复的 {key}")

    return rows


def render_route(scene_dir, route_nodes, output_path, closed_edges=None):
    """按传入的节点序列画图，返回相对项目根目录的图片路径。"""
    folder = (ROOT / scene_dir).resolve()
    output = (ROOT / output_path).resolve()
    relative = output.relative_to(ROOT)

    if output.suffix.lower() != ".png":
        raise ValueError("output_path 必须以 .png 结尾")

    if not isinstance(route_nodes, (list, tuple)):
        raise ValueError("route_nodes 必须是节点编号列表")

    if closed_edges is not None and not isinstance(
        closed_edges, (list, tuple, set)
    ):
        raise ValueError("closed_edges 必须是通道编号列表")

    closed = set(closed_edges or [])

    assets = _table(
        folder, "assets.csv",
        ["asset_id", "node_id", "criticality", "name", "simulation"]
    )
    nodes = _table(
        folder, "nodes.csv",
        ["node_id", "x_m", "y_m"]
    )
    edges = _table(
        folder, "edges.csv",
        ["edge_id", "from_node", "to_node", "length_m", "enabled"]
    )

    xy = {
        row["node_id"]: (float(row["x_m"]), float(row["y_m"]))
        for row in nodes
    }

    if "S" not in xy or any(
        not math.isfinite(v)
        for point in xy.values()
        for v in point
    ):
        raise ValueError("缺少起点 S 或坐标不是有限数值")

    for asset in assets:
        if (
            asset["node_id"] not in xy
            or asset["criticality"] not in {"1", "2", "3"}
            or asset["simulation"].lower() != "true"
        ):
            raise ValueError(f"设备字段有误：{asset['asset_id']}")

    unknown = closed - {edge["edge_id"] for edge in edges}
    if unknown:
        raise ValueError(f"未知关闭通道：{sorted(unknown)}")

    pairs = set()
    available = set()

    for edge in edges:
        a, b = edge["from_node"], edge["to_node"]
        length = float(edge["length_m"])
        flag = edge["enabled"].lower()

        if (
            a not in xy or b not in xy or a == b
            or not math.isfinite(length) or length <= 0
            or flag not in {"true", "false"}
        ):
            raise ValueError(f"通道字段有误：{edge['edge_id']}")

        pair = frozenset((a, b))
        if pair in pairs:
            raise ValueError(f"重复双向通道：{edge['edge_id']}")

        pairs.add(pair)
        if flag == "true" and edge["edge_id"] not in closed:
            available.add(pair)

    if any(node not in xy for node in route_nodes):
        raise ValueError("路线包含未知节点")

    for a, b in zip(route_nodes, route_nodes[1:]):
        if frozenset((a, b)) not in available:
            raise ValueError(f"路线不能从 {a} 直接走到 {b}：无开放通道")

    fonts = {f.name for f in font_manager.fontManager.ttflist}
    font = next(
        (
            name for name in [
                "Microsoft YaHei", "SimHei",
                "Noto Sans CJK SC", "Noto Sans CJK JP"
            ]
            if name in fonts
        ),
        None
    )

    with plt.rc_context({
        "font.family": font or "DejaVu Sans",
        "axes.unicode_minus": False
    }):
        fig, ax = plt.subplots(figsize=(10, 8))

        try:
            for edge in edges:
                a = xy[edge["from_node"]]
                b = xy[edge["to_node"]]
                blocked = (
                    edge["enabled"].lower() == "false"
                    or edge["edge_id"] in closed
                )

                ax.plot(
                    [a[0], b[0]], [a[1], b[1]],
                    lw=4,
                    color="#C84545" if blocked else "#BBC8D5",
                    linestyle="--" if blocked else "-",
                    zorder=1
                )

                horizontal = a[1] == b[1]
                ax.text(
                    (a[0] + b[0]) / 2,
                    (a[1] + b[1]) / 2 + (1.5 if horizontal else 0),
                    f"{edge['edge_id']}  {float(edge['length_m']):g} m",
                    fontsize=8,
                    ha="center",
                    va="bottom" if horizontal else "center",
                    rotation=0 if horizontal else 90,
                    bbox={
                        "facecolor": "white",
                        "edgecolor": "none",
                        "pad": 1
                    },
                    zorder=7
                )

            asset_nodes = {asset["node_id"] for asset in assets}
            start = route_nodes[0] if route_nodes else "S"

            for node, (x, y) in xy.items():
                if node not in asset_nodes and node != start:
                    ax.scatter(x, y, s=35, color="#40566B", zorder=3)
                    ax.annotate(
                        node, (x, y),
                        xytext=(6, -14),
                        textcoords="offset points",
                        fontsize=9
                    )

            colors = {
                "3": "#C77B30",
                "2": "#287FA0",
                "1": "#808B96"
            }

            for asset in assets:
                x, y = xy[asset["node_id"]]
                level = asset["criticality"]

                ax.scatter(
                    x, y, s=180, marker="s",
                    color=colors[level], zorder=4
                )

                label = (
                    f"{asset['asset_id']}  重要等级 {level}"
                    if font else
                    f"{asset['asset_id']}  criticality {level}"
                )

                ax.annotate(
                    label, (x, y),
                    xytext=(0, 14 if y > 20 else -25),
                    textcoords="offset points",
                    ha="center", fontsize=10
                )
                ax.annotate(
                    asset["node_id"], (x, y),
                    xytext=(12, -3),
                    textcoords="offset points",
                    fontsize=8, color="#607080"
                )

            for a, b in zip(route_nodes, route_nodes[1:]):
                ax.annotate(
                    "", xy=xy[b], xytext=xy[a],
                    arrowprops={
                        "arrowstyle": "->",
                        "color": "#7849AD",
                        "lw": 2
                    },
                    zorder=5
                )

            ax.scatter(
                *xy[start], s=280,
                marker="*", color="#16836C", zorder=6
            )
            ax.annotate(
                f"{start} 起点" if font else f"{start} Start",
                xy[start],
                xytext=(-12, -20),
                textcoords="offset points",
                ha="right"
            )

            ax.set_title(
                "模拟车间设备与巡检通道"
                if font else "Simulated workshop scene",
                fontsize=16, pad=18
            )
            ax.set_xlabel("x（米）" if font else "x (m)")
            ax.set_ylabel("y（米）" if font else "y (m)")
            ax.set_aspect("equal")

            xs, ys = zip(*xy.values())
            ax.set_xlim(min(xs) - 10, max(xs) + 10)
            ax.set_ylim(min(ys) - 10, max(ys) + 10)
            ax.grid(alpha=0.2)

            handles = [
                Line2D(
                    [], [], color=colors[str(level)],
                    marker="s", linestyle="none",
                    label=(
                        f"重要等级 {level}"
                        if font else f"Criticality {level}"
                    )
                )
                for level in [3, 2, 1]
            ]

            handles.append(Line2D(
                [], [], color="#C84545", linestyle="--",
                label="关闭通道" if font else "Closed corridor"
            ))

            if route_nodes:
                handles.append(Line2D(
                    [], [], color="#7849AD",
                    label="传入路线" if font else "Supplied route"
                ))

            ax.legend(
                handles=handles,
                loc="upper center",
                bbox_to_anchor=(0.5, -0.12),
                ncol=3, frameon=False, fontsize=9
            )
            fig.text(
                0.5, 0.025,
                "模拟场景  坐标与重要等级均为团队设定"
                if font else
                "Simulation: positions and criticality are team-defined",
                ha="center", fontsize=9, color="#607080"
            )

            fig.subplots_adjust(bottom=0.22, top=0.91)
            output.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(output, dpi=150)

        finally:
            plt.close(fig)

    return relative.as_posix()


if __name__ == "__main__":
    print("已生成地图：", render_route("scene", [], "runs/scene_base.png"))
