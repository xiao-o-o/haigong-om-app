"""训练：对数功率频谱特征 + Isolation Forest，并以 k=5 近邻平均距离作对照。

规则（手册第五章）：
  * 仅用「正常训练组」拟合 StandardScaler、Isolation Forest 与近邻库；
  * 两种方法共用同一 128 维特征与同一训练组 scaler；
  * 阈值各自取「正常验证分数」的 95 分位；
  * 阈值冻结后才看测试结果。

用法::

    .venv/Scripts/python.exe -m audio_module.train
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

from .config import (DATASET_SOURCE, DEMO_RAW_DIR, FEATURE_DIM, IF_N_ESTIMATORS,
                     KNN_K, MANIFEST_PATH, MODEL_CONFIG_PATH, MODEL_PATH,
                     MODEL_VERSION, PROJECT_ROOT, RAW_DIR, SCHEMA_VERSION, SEED,
                     SPLITS_PATH, THRESHOLD_PERCENTILE, dump_json,
                     ensure_dirs, feature_config)
from .features import feature_matrix


class ModelUnavailableError(RuntimeError):
    """模型缺失或依赖不兼容（属于系统错误，不是 invalid_input）。"""


@dataclass
class ModelBundle:
    """推理期需要的全部对象，序列化为单个 joblib 文件。"""

    scaler: StandardScaler
    iforest: IsolationForest
    knn: NearestNeighbors
    thresholds: dict
    primary_method: str
    model_version: str = MODEL_VERSION
    schema_version: str = SCHEMA_VERSION
    meta: dict = field(default_factory=dict)

    def score(self, feature: np.ndarray, method: str | None = None) -> float:
        """返回异常分数（越大越异常），与文件名、路径无关。"""
        method = method or self.primary_method
        x = np.asarray(feature, dtype=np.float64).reshape(1, -1)
        xs = self.scaler.transform(x)
        if method == "iforest":
            return float(-self.iforest.score_samples(xs)[0])
        if method == "knn":
            dist, _ = self.knn.kneighbors(xs, n_neighbors=KNN_K)
            return float(np.mean(dist))
        raise ValueError(f"未知方法: {method}")


def _tail_ratio(scores: np.ndarray) -> float:
    """正常验证分数的尾部离散度，越小说明阈值越稳定。"""
    p50, p95, p99 = np.percentile(scores, [50, 95, 99])
    return float((p99 - p95) / (p95 - p50 + 1e-9))


def load_splits(splits_path: Path | None = None) -> pd.DataFrame:
    splits_path = Path(splits_path) if splits_path else SPLITS_PATH
    if not splits_path.exists():
        raise SystemExit(f"缺少划分文件: {splits_path}（请先运行 make_splits）")
    return pd.read_csv(splits_path)


def resolve_data_root(splits: pd.DataFrame) -> Path:
    """自动判断清单指向 data/raw 还是 data/demo_raw。"""
    first_rel = splits.iloc[0]["relative_path"]
    for root in (RAW_DIR, DEMO_RAW_DIR):
        if (root / first_rel).exists():
            return root
    raise SystemExit("找不到音频文件，请检查 relative_path 与数据目录")


def train_model(data_root: Path, splits: pd.DataFrame) -> ModelBundle:
    normal_train = splits[(splits["label"] == "normal") & (splits["split"] == "train")]
    normal_val = splits[(splits["label"] == "normal") & (splits["split"] == "val")]
    if normal_train.empty or normal_val.empty:
        raise SystemExit("正常训练组或验证组为空，无法训练")
    print(f"[1/5] 正常训练 {len(normal_train)} 条 / 正常验证 {len(normal_val)} 条")
    x_train = feature_matrix([str(data_root / p) for p in normal_train["relative_path"]])
    x_val = feature_matrix([str(data_root / p) for p in normal_val["relative_path"]])
    assert x_train.shape[1] == FEATURE_DIM

    print("[2/5] 拟合 StandardScaler（仅正常训练组）")
    scaler = StandardScaler().fit(x_train)
    xs_train, xs_val = scaler.transform(x_train), scaler.transform(x_val)

    print(f"[3/5] 训练 Isolation Forest（{IF_N_ESTIMATORS} 棵树，seed={SEED}）")
    iforest = IsolationForest(n_estimators=IF_N_ESTIMATORS, random_state=SEED,
                              n_jobs=-1).fit(xs_train)
    print(f"[4/5] 建立 k={KNN_K} 近邻库（同一正常训练组）")
    knn = NearestNeighbors(n_neighbors=KNN_K, metric="euclidean").fit(xs_train)

    s_if_val = -iforest.score_samples(xs_val)
    s_knn_val = np.mean(knn.kneighbors(xs_val, n_neighbors=KNN_K)[0], axis=1)
    thresholds = {
        "iforest": float(np.percentile(s_if_val, THRESHOLD_PERCENTILE)),
        "knn": float(np.percentile(s_knn_val, THRESHOLD_PERCENTILE)),
    }

    cand = {"iforest": _tail_ratio(s_if_val), "knn": _tail_ratio(s_knn_val)}
    primary = min(cand, key=cand.get)
    print(f"[5/5] 验证集尾部离散度 iforest={cand['iforest']:.4f} / knn={cand['knn']:.4f} "
          f"-> 上线方法 {primary}")
    meta = {
        "trained_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "seed": SEED,
        "if_n_estimators": IF_N_ESTIMATORS,
        "knn_k": KNN_K,
        "threshold_percentile": THRESHOLD_PERCENTILE,
        "thresholds": thresholds,
        "primary_method": primary,
        "validation_tail_ratio": cand,
        "selection_rule": "正常验证分数尾部离散度 (p99-p95)/(p95-p50) 更小者上线；阈值仍各取 95 分位",
        "n_normal_train": int(len(normal_train)),
        "n_normal_val": int(len(normal_val)),
        "feature_config": feature_config(),
        "dataset": DATASET_SOURCE,
        "data_root": str(data_root.relative_to(PROJECT_ROOT)),
        "versions": {"sklearn": sklearn.__version__, "numpy": np.__version__,
                     "python": __import__("sys").version.split()[0]},
    }
    return ModelBundle(scaler=scaler, iforest=iforest, knn=knn,
                       thresholds=thresholds, primary_method=primary, meta=meta)


def save_bundle(bundle: ModelBundle, path: Path = MODEL_PATH) -> None:
    """保存为纯字典 + sklearn 对象，避免 pickle 依赖脚本入口模块名。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "scaler": bundle.scaler,
        "iforest": bundle.iforest,
        "knn": bundle.knn,
        "thresholds": bundle.thresholds,
        "primary_method": bundle.primary_method,
        "model_version": bundle.model_version,
        "schema_version": bundle.schema_version,
        "meta": bundle.meta,
    }, path)
    dump_json(bundle.meta, MODEL_CONFIG_PATH)
    print(f"模型已保存: {path}")
    print(f"配置已保存: {MODEL_CONFIG_PATH}")


def load_bundle(path: Path = MODEL_PATH) -> ModelBundle:
    if not path.exists():
        raise ModelUnavailableError(
            f"模型文件缺失: {path}。请先运行 python -m audio_module.train 生成模型。")
    try:
        payload = joblib.load(path)
    except Exception as exc:  # 依赖不兼容、文件损坏
        raise ModelUnavailableError(f"模型加载失败（依赖不兼容或文件损坏）: {exc}") from exc
    if isinstance(payload, ModelBundle):
        return payload
    if not isinstance(payload, dict) or "iforest" not in payload:
        raise ModelUnavailableError("模型文件格式不符，请用当前版本重新训练")
    return ModelBundle(**payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="训练水泵异常检测模型")
    parser.add_argument("--splits", default=str(SPLITS_PATH))
    parser.add_argument("--out", default=str(MODEL_PATH))
    args = parser.parse_args()
    ensure_dirs()
    splits = load_splits(Path(args.splits))
    data_root = resolve_data_root(splits)
    bundle = train_model(data_root, splits)
    save_bundle(bundle, Path(args.out))


if __name__ == "__main__":
    main()
