"""音频读取与波形质量检查（首读与范围核查）。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import (CHANNEL_INDEX, CLIP_DURATION_S, DEMO_RAW_DIR,
                     MIN_DURATION_S, RAW_DIR, SAMPLE_RATE)


def md5_of_file(path: str | Path, chunk_mb: int = 8) -> str:
    """流式计算文件 MD5，用于查重与下载校验。"""
    md5 = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_mb * 1024 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def load_wave(path: str | Path) -> dict:
    """读取一个 WAV 并返回波形与基础质量信息。

    返回字段：
        y              波形（float32, 单通道=通道0）
        sample_rate    采样率
        channels       原始通道数
        duration_s     原始时长（秒）
        used_duration_s 实际参与特征计算的时长（秒）
        took_head_clip 是否因超过 10 秒而截取前段
    """
    data, sr = sf.read(str(path), always_2d=True, dtype="float32")
    y = data[:, CHANNEL_INDEX]
    duration = float(len(y) / sr)
    took_head = False
    max_len = int(CLIP_DURATION_S * sr)
    if len(y) > max_len:
        y = y[:max_len]
        took_head = True
    return {
        "y": y,
        "sample_rate": int(sr),
        "channels": int(data.shape[1]),
        "duration_s": duration,
        "used_duration_s": float(len(y) / sr),
        "took_head_clip": took_head,
    }


def wave_quality(y: np.ndarray, sample_rate: int) -> dict:
    """波形质量指标：坏文件、全零、非有限值在此暴露。"""
    finite = bool(np.all(np.isfinite(y)))
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    rms = float(np.sqrt(np.mean(y**2))) if y.size else 0.0
    clipped = float(np.mean(np.abs(y) >= 0.999)) if y.size else 0.0
    return {
        "is_finite": finite,
        "is_all_zero": bool(peak <= 1e-9),
        "peak": peak,
        "rms": rms,
        "peak_dbfs": float(20 * np.log10(peak + 1e-10)),
        "rms_dbfs": float(20 * np.log10(rms + 1e-10)),
        "clipped_ratio": clipped,
        "sample_rate_hz": int(sample_rate),
        "expected_sample_rate_hz": SAMPLE_RATE,
        "sample_rate_ok": bool(sample_rate == SAMPLE_RATE),
    }


def quick_inspect(path: str | Path) -> dict:
    """不读全量的快速体检：采样率、时长、通道数、是否有限。"""
    info = sf.info(str(path))
    return {
        "path": str(path),
        "sample_rate_hz": int(info.samplerate),
        "frames": int(info.frames),
        "duration_s": float(info.frames / info.samplerate),
        "channels": int(info.channels),
        "format": info.format,
        "subtype": info.subtype,
    }


def first_read_report(raw_dir=None, max_per_group: int = 2) -> Path:
    """首次读取与范围核查：每组抽样显示采样率、时长、通道数，并画首条录音的图。

    输出 runs/data_inspection_<时间戳>/：
        inspection.csv    抽样体检表
        summary.json      采样率/通道/时长范围汇总
        first_sample.png  首条录音的波形 + 对数功率频带图
    """
    import json as _json

    import pandas as pd

    from .config import DEMO_RAW_DIR, RUNS_DIR
    from .features import log_power_bands

    if raw_dir is None:
        raw_dir = RAW_DIR if any(RAW_DIR.rglob("*.wav")) else DEMO_RAW_DIR
        if raw_dir == DEMO_RAW_DIR:
            print("提示: data/raw 为空，当前核查的是合成演示数据（结果不进入正式结论）")
    raw_dir = Path(raw_dir)
    wavs = sorted(raw_dir.rglob("*.wav"))
    if not wavs:
        raise SystemExit(f"目录中没有 WAV: {raw_dir}")

    by_group: dict[tuple, list] = {}
    for p in wavs:
        key = (p.parent.parent.name, p.parent.name)  # (设备, 标签)
        by_group.setdefault(key, []).append(p)

    rows = []
    for (device, label), paths in sorted(by_group.items()):
        for p in paths[:max_per_group]:
            q = quick_inspect(p)
            q["device"] = device
            q["label"] = label
            q["relative_path"] = str(p.relative_to(raw_dir)).replace("\\", "/")
            rows.append(q)
    df = pd.DataFrame(rows)

    out_dir = RUNS_DIR / f"data_inspection_{pd.Timestamp.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / "inspection.csv", index=False, encoding="utf-8-sig")
    print(df[["device", "label", "sample_rate_hz", "duration_s", "channels"]]
          .to_string(index=False))

    summary = {
        "raw_dir": str(raw_dir),
        "n_files_total": len(wavs),
        "n_sampled": len(df),
        "groups": {f"{d}/{l}": int((df["device"] == d).mul(df["label"] == l).sum())
                   for d, l in by_group},
        "sample_rate_hz": sorted({int(v) for v in df["sample_rate_hz"]}),
        "channels": sorted({int(v) for v in df["channels"]}),
        "duration_s_min": round(float(df["duration_s"].min()), 2),
        "duration_s_max": round(float(df["duration_s"].max()), 2),
        "formats": sorted({str(v) for v in df["format"]}),
    }
    (out_dir / "summary.json").write_text(
        _json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(_json.dumps({k: summary[k] for k in
                       ("sample_rate_hz", "channels", "duration_s_min", "duration_s_max")},
                      ensure_ascii=False, indent=2))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    wave = load_wave(wavs[0])
    fig, axes = plt.subplots(2, 1, figsize=(9, 5))
    axes[0].plot(np.arange(len(wave["y"])) / wave["sample_rate"], wave["y"], lw=0.4)
    axes[0].set_xlabel("time (s)"); axes[0].set_ylabel("amp")
    axes[0].set_title(str(wavs[0].relative_to(raw_dir)) + "  (first sample)", fontsize=9)
    im = axes[1].imshow(log_power_bands(wave["y"]), aspect="auto", origin="lower", cmap="magma")
    axes[1].set_xlabel("time (s)"); axes[1].set_ylabel("band")
    fig.colorbar(im, ax=axes[1], label="log10 power")
    fig.tight_layout(); fig.savefig(out_dir / "first_sample.png", dpi=130); plt.close(fig)
    print(f"首读报告: {out_dir}")
    return out_dir


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="音频首读与范围核查")
    parser.add_argument("--raw", default=None, help="原始数据目录，默认 data/raw，缺失时用 data/demo_raw")
    parser.add_argument("--max-per-group", type=int, default=2)
    args = parser.parse_args()
    first_read_report(Path(args.raw) if args.raw else None, args.max_per_group)


if __name__ == "__main__":
    main()
