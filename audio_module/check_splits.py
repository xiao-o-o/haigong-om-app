"""划分核查：训练前必须通过的四项检查。

在正式训练前运行，避免「先训练再发现清单有问题」：

    python -m audio_module.check_splits

检查内容：
  1  同一 group_id 是否被拆到多个 split（数据泄漏）
  2  标签是否与官方目录一致（relative_path 中的 normal / abnormal）
  3  正常 60/20/20、异常 20/80 的比例是否在容差内
  4  设备与标签是否混杂（若某类标签只出现在部分设备，模型可能学到设备身份而非异常）
另附：文件缺失、MD5 重复的提示。

结果写入 runs/splits_check_<时间戳>.json，有 FAIL 时退出码为 1。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import (DEMO_RAW_DIR, MANIFEST_PATH, PROJECT_ROOT, RAW_DIR,
                     RUNS_DIR, SEED, SPLITS_PATH)
from .train import resolve_data_root


def _ratio(counts: dict, total: int) -> dict:
    return {k: round(v / total, 3) if total else 0.0 for k, v in counts.items()}


def main() -> None:
    if not SPLITS_PATH.exists() or not MANIFEST_PATH.exists():
        raise SystemExit("缺少 manifest.csv 或 splits.csv，请先运行 make_splits")
    splits = pd.read_csv(SPLITS_PATH)
    manifest = pd.read_csv(MANIFEST_PATH)
    data_root = resolve_data_root(splits)
    is_demo = data_root == DEMO_RAW_DIR

    results: list[dict] = []

    def check(name: str, ok: bool, detail: str, level: str = "FAIL") -> None:
        results.append({"check": name, "passed": bool(ok), "level": level, "detail": detail})

    # 1 group 泄漏
    group_splits = splits.groupby("group_id")["split"].nunique()
    leaked = group_splits[group_splits > 1]
    check("同一 group_id 只属于一个 split", leaked.empty,
          f"泄漏组 {len(leaked)} 个" if len(leaked) else "无泄漏")

    # 2 标签与目录一致
    mismatch = splits[[
        not ((("normal" in str(p).lower()) and lab == "normal")
             or (("abnormal" in str(p).lower()) and lab == "anomaly"))
        for p, lab in zip(splits["relative_path"], splits["label"])
    ]]
    check("标签与官方目录一致", mismatch.empty,
          f"不一致 {len(mismatch)} 条" if len(mismatch) else "全部一致")

    # 3 比例
    n_normal = int((splits["label"] == "normal").sum())
    n_anomaly = int((splits["label"] == "anomaly").sum())
    n_split = splits[splits["label"] == "normal"]["split"].value_counts().to_dict()
    a_split = splits[splits["label"] == "anomaly"]["split"].value_counts().to_dict()
    n_ratio, a_ratio = _ratio(n_split, n_normal), _ratio(a_split, n_anomaly)
    normal_ok = all(0.55 <= n_ratio.get(k, 0) <= 0.65 for k in ("train",)) and \
        all(0.15 <= n_ratio.get(k, 0) <= 0.25 for k in ("val", "test"))
    anomaly_ok = 0.15 <= a_ratio.get("dev", 0) <= 0.25 and 0.75 <= a_ratio.get("test", 0) <= 0.85
    check("正常 60/20/20 比例", normal_ok, f"{n_ratio}")
    check("异常 20/80 比例", anomaly_ok, f"{a_ratio}")

    # 4 设备×标签混杂
    cross = pd.crosstab(splits["device_id"], splits["label"])
    normal_devices = set(cross.index[cross.get("normal", 0) > 0]) if "normal" in cross else set()
    anomaly_devices = set(cross.index[cross.get("anomaly", 0) > 0]) if "anomaly" in cross else set()
    shared = normal_devices & anomaly_devices
    confounded = bool(normal_devices) and bool(anomaly_devices) and not shared
    check("正常与异常共享设备（避免学到设备身份）", not confounded,
          "正常与异常分别只出现在不同设备，模型可能学设备而非异常；"
          "真实 MIMII 每个 id 同时包含 normal 与 abnormal，重跑前请确认" if confounded
          else f"共享设备 {sorted(shared)}", level="WARN")

    # 附：文件缺失与重复
    missing = [p for p in splits["relative_path"] if not (data_root / p).exists()]
    check("清单文件均存在", not missing, f"缺失 {len(missing)} 个" if missing else "全部存在")
    dup = int(manifest["md5"].astype(str).duplicated().sum()) if "md5" in manifest else 0
    check("MD5 无重复", dup == 0, f"重复 {dup} 条" if dup else "无重复", level="WARN")

    # 输出
    fails = [r for r in results if not r["passed"] and r["level"] == "FAIL"]
    warns = [r for r in results if not r["passed"] and r["level"] == "WARN"]
    payload = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_root": str(data_root.relative_to(PROJECT_ROOT)),
        "is_demo_data": is_demo,
        "seed": SEED,
        "counts": {"normal": n_normal, "anomaly": n_anomaly,
                   "normal_ratio": n_ratio, "anomaly_ratio": a_ratio},
        "device_label_table": cross.to_dict(),
        "results": results,
        "fail_count": len(fails),
        "warn_count": len(warns),
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / f"splits_check_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"数据目录: {payload['data_root']}" + ("（合成演示数据）" if is_demo else ""))
    print(f"样本: 正常 {n_normal} 条 {n_ratio} / 异常 {n_anomaly} 条 {a_ratio}\n")
    print("设备 × 标签:")
    print(cross.to_string())
    print()
    for r in results:
        mark = "PASS" if r["passed"] else ("WARN" if r["level"] == "WARN" else "FAIL")
        print(f"[{mark}] {r['check']}  -- {r['detail']}")
    print(f"\nFAIL {len(fails)} / WARN {len(warns)}  记录: {out.relative_to(PROJECT_ROOT)}")
    if fails:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
