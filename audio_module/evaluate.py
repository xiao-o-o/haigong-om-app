"""评估：用冻结的模型与阈值在测试组上出总体指标。

输出（runs/eval_<时间戳>/）：
    metrics.json             两方法的 AUC、混淆矩阵、误报率、漏报率等
    predictions_iforest.csv  逐样本分数（上线方法）
    predictions_knn.csv      逐样本分数（对照方法）
    score_distribution.png   正常/异常分数分布对比
    roc_curves.png           两方法 ROC 曲线

用法::

    .venv/Scripts/python.exe -m audio_module.evaluate
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.metrics import roc_auc_score, roc_curve

from .config import MODEL_PATH, PROJECT_ROOT, RUNS_DIR, SCHEMA_VERSION
from .features import feature_matrix
from .train import KNN_K, load_bundle, load_splits, resolve_data_root


def _scores(bundle, method: str, xs: np.ndarray) -> np.ndarray:
    if method == "iforest":
        return -bundle.iforest.score_samples(xs)
    dist, _ = bundle.knn.kneighbors(xs, n_neighbors=KNN_K)
    return np.mean(dist, axis=1)


def _confusion(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    tp = int(((y_true == 1) & (y_pred == 1)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    tn = int(((y_true == 0) & (y_pred == 0)).sum())
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    fpr = fp / (fp + tn) if (fp + tn) else None
    fnr = fn / (fn + tp) if (fn + tp) else None
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "false_positive_rate": None if fpr is None else round(fpr, 4),
            "false_negative_rate": None if fnr is None else round(fnr, 4)}


def evaluate(splits_path: Path | None = None, model_path: Path = MODEL_PATH) -> Path:
    bundle = load_bundle(model_path)
    splits = load_splits(splits_path or None)
    data_root = resolve_data_root(splits)

    test = splits[splits["split"] == "test"]
    dev = splits[splits["split"] == "dev"]
    y_true = (test["label"] == "anomaly").astype(int).to_numpy()
    n_normal, n_anomaly = int((y_true == 0).sum()), int((y_true == 1).sum())
    if n_normal == 0 or n_anomaly == 0:
        raise SystemExit("测试组必须同时包含正常与异常样本才能评估")

    print(f"测试组: 正常 {n_normal} 条 / 异常 {n_anomaly} 条（阈值冻结，来自正常验证 95 分位）")
    feats = feature_matrix([str(data_root / p) for p in test["relative_path"]])
    xs = bundle.scaler.transform(feats)

    out_dir = RUNS_DIR / f"eval_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    metrics: dict = {
        "schema_version": SCHEMA_VERSION,
        "model_version": bundle.model_version,
        "primary_method": bundle.primary_method,
        "thresholds": bundle.thresholds,
        "test_counts": {"normal": n_normal, "anomaly": n_anomaly},
        "test_split_definition": "异常 80% 最终测试 / 20% 开发观察（见 splits.csv）",
        "n_dev_anomaly": int(len(dev)),
        "methods": {},
    }

    plt.figure(figsize=(6.4, 4.4))
    for method in ("iforest", "knn"):
        scores = _scores(bundle, method, xs)
        thr = bundle.thresholds[method]
        y_pred = (scores >= thr).astype(int)
        auc = float(roc_auc_score(y_true, scores))
        cm = _confusion(y_true, y_pred)
        metrics["methods"][method] = {
            "auc": round(auc, 4), "threshold": round(thr, 4),
            "confusion_matrix": cm,
            "accuracy": round(float((y_pred == y_true).mean()), 4),
            "is_primary": method == bundle.primary_method,
        }
        pred = pd.DataFrame({
            "sample_id": test["sample_id"], "relative_path": test["relative_path"],
            "device_id": test["device_id"], "label": test["label"],
            "score": np.round(scores, 4), "threshold": round(thr, 4),
            "pred_status": np.where(y_pred == 1, "suspected_anomaly", "normal"),
            "correct": y_pred == y_true,
        })
        pred.to_csv(out_dir / f"predictions_{method}.csv", index=False, encoding="utf-8-sig")

        plt.hist(scores[y_true == 0], bins=30, alpha=0.55, label=f"{method} normal")
        plt.hist(scores[y_true == 1], bins=30, alpha=0.55, label=f"{method} anomaly")
        plt.axvline(thr, linestyle="--", linewidth=1, label=f"{method} thr={thr:.3f}")

        fpr, tpr, _ = roc_curve(y_true, scores)
        plt.figure(figsize=(4.6, 4.2))
        plt.plot(fpr, tpr, label=f"{method} AUC={auc:.3f}")
        plt.plot([0, 1], [0, 1], "k--", linewidth=0.8)
        plt.xlabel("FPR"); plt.ylabel("TPR"); plt.title("ROC"); plt.legend()
        plt.grid(alpha=0.3); plt.tight_layout()
        plt.savefig(out_dir / f"roc_{method}.png", dpi=130); plt.close()

    plt.xlabel("anomaly score"); plt.ylabel("count")
    plt.title("Score distribution (test split)"); plt.legend(); plt.grid(alpha=0.3)
    plt.tight_layout(); plt.savefig(out_dir / "score_distribution.png", dpi=130); plt.close()

    from .config import dump_json
    dump_json(metrics, out_dir / "metrics.json")
    print(f"评估完成，结果目录: {out_dir.relative_to(PROJECT_ROOT)}")
    for method, m in metrics["methods"].items():
        cm = m["confusion_matrix"]
        print(f"  [{method}] AUC={m['auc']} thr={m['threshold']} "
              f"FPR={cm['false_positive_rate']} FNR={cm['false_negative_rate']}")
    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="测试组评估")
    parser.add_argument("--splits", default=None)
    parser.add_argument("--model", default=str(MODEL_PATH))
    args = parser.parse_args()
    evaluate(Path(args.splits) if args.splits else None, Path(args.model))


if __name__ == "__main__":
    main()
