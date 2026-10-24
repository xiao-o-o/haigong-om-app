"""清单与划分生成：manifest.csv 与 splits.csv（先固定清单再训练）。

用法::

    .venv/Scripts/python.exe -m audio_module.make_splits [--raw <data/raw 或 data/demo_raw>]

MIMII 官方目录形如 ``pump/id_00/normal/00000000.wav``；
设备与标签均取自官方目录结构，不根据听感改标签。
"""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (MANIFEST_PATH, META_DIR, SEED, SPLITS_PATH,
                     DATASET_SOURCE)
from .inspect_data import md5_of_file, quick_inspect

ID_RE = re.compile(r"(id_\d+)")


def _group_id(rel: Path, device_id: str, label: str) -> str:
    """MIMII 每条 10 秒 WAV 即一段独立录音；同录音的切片/不同处理版本应共享 group_id。

    规则：``设备_标签_文件名主干``。不同设备即便文件名相同也不会落入同组。
    若同一录音被切成多段（如 00000001_seg1.wav），请在此剥离切片后缀，
    使同源切片共享同一个 group_id。
    """
    stem = rel.stem
    return f"{device_id}_{label}_{stem}"


def scan_raw(raw_dir: Path) -> list[dict]:
    """递归扫描 WAV，按官方目录解析设备、标签与噪声条件。"""
    records: list[dict] = []
    wav_paths = sorted(raw_dir.rglob("*.wav"))
    if not wav_paths:
        raise SystemExit(f"目录中没有找到 WAV 文件: {raw_dir}")
    for path in wav_paths:
        rel = path.relative_to(raw_dir)
        parts = [p.lower() for p in rel.parts]
        label = "anomaly" if "abnormal" in parts else "normal"
        device_m = ID_RE.search(str(rel))
        device_id = device_m.group(1) if device_m else "unknown"
        noise = "+6dB" if parts and parts[0].startswith("6_db") else (
            "-6dB" if parts and parts[0].startswith("-6") else "unknown")
        info = quick_inspect(path)
        records.append({
            "relative_path": str(rel).replace("\\", "/"),
            "original_name": path.name,
            "source": DATASET_SOURCE["name"],
            "device_id": device_id,
            "noise": noise,
            "official_label": label,
            "sample_rate_hz": info["sample_rate_hz"],
            "duration_s": round(info["duration_s"], 3),
            "channels": info["channels"],
            "md5": None,  # 稍后统一计算（较慢）
            "group_id": _group_id(rel, device_id, label),
        })
    return records


def build_manifest(raw_dir: Path, out_csv: Path, with_hash: bool = True) -> pd.DataFrame:
    """生成 manifest.csv：sample_id、relative_path、原名、来源、设备、噪声、官方标签、元数据、哈希。"""
    print(f"[1/3] 扫描原始目录: {raw_dir}")
    records = scan_raw(raw_dir)
    df = pd.DataFrame(records)
    total = len(df)
    print(f"    共 {total} 条 WAV")
    if with_hash:
        print("[2/3] 计算 MD5 哈希（用于查重，耗时较长）...")
        hashes = []
        for i, rel in enumerate(df["relative_path"], 1):
            hashes.append(md5_of_file(raw_dir / rel))
            if i % 50 == 0 or i == total:
                print(f"    哈希进度: {i}/{total}")
        df["md5"] = hashes
    else:
        df["md5"] = ""
    dup = df[df["md5"] != ""]["md5"].duplicated().sum() if with_hash else 0
    if dup:
        print(f"    注意: 发现 {dup} 个重复哈希，训练前请人工复核")
    df.insert(0, "sample_id", [f"s{str(i).zfill(6)}" for i in range(1, total + 1)])
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"[3/3] 清单已写入: {out_csv}")
    return df


def build_splits(manifest: pd.DataFrame, out_csv: Path, seed: int = SEED) -> pd.DataFrame:
    """生成 splits.csv：正常 60/20/20 训练/验证/测试；异常 20% 开发 / 80% 最终测试。

    按 group_id 划分：同一录音的切片、不同通道、不同处理版本留在同组。
    """
    rng = np.random.default_rng(seed)
    rows = []
    for label, ratio_map in (("normal", {"train": 0.6, "val": 0.2, "test": 0.2}),
                             ("anomaly", {"dev": 0.2, "test": 0.8})):
        sub = manifest[manifest["official_label"] == label]
        groups = sorted(sub["group_id"].unique())
        rng.shuffle(groups)
        n = len(groups)
        cursor = 0
        for split_name, ratio in ratio_map.items():
            if split_name == list(ratio_map)[-1]:
                take = groups[cursor:]
            else:
                take_n = int(round(n * ratio))
                take = groups[cursor:cursor + take_n]
                cursor += take_n
            part = sub[sub["group_id"].isin(take)]
            for _, r in part.iterrows():
                rows.append({
                    "sample_id": r["sample_id"],
                    "group_id": r["group_id"],
                    "split": split_name,
                    "label": label,
                    "device_id": r["device_id"],
                    "relative_path": r["relative_path"],
                    "md5": r["md5"],
                })
    splits = pd.DataFrame(rows)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    splits.to_csv(out_csv, index=False, encoding="utf-8-sig")
    counts = splits.groupby(["label", "split"]).size()
    print("划分结果（按样本数）:")
    print(counts.to_string())
    print(f"划分已写入: {out_csv}  (seed={seed})")
    return splits


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 manifest.csv 与 splits.csv")
    parser.add_argument("--raw", default=None, help="原始数据目录，默认 data/raw")
    parser.add_argument("--no-hash", action="store_true", help="跳过 MD5（快速预览用）")
    args = parser.parse_args()
    from .config import DEMO_RAW_DIR, RAW_DIR
    raw_dir = Path(args.raw) if args.raw else RAW_DIR
    if not raw_dir.exists() or not any(raw_dir.rglob("*.wav")):
        raw_dir = DEMO_RAW_DIR
        print("data/raw 为空，自动切换到合成演示数据 data/demo_raw（结果不进入正式结论）")
    manifest = build_manifest(raw_dir, MANIFEST_PATH, with_hash=not args.no_hash)
    build_splits(manifest, SPLITS_PATH)


if __name__ == "__main__":
    main()
