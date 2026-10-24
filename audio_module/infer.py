"""推理：对外唯一声音接口。

    from audio_module.infer import analyze_audio
    result = analyze_audio("xxx.wav", "P03", output_dir="runs")

返回（schema 1.0）：schema_version、asset_id、model_version、score、threshold、
status、quality、plot_file、notes、run_id、method、created_at。

status 限定 normal / suspected_anomaly / invalid_input。
模型缺失或依赖不兼容属于系统错误，抛 ModelUnavailableError，不伪装成 invalid_input。
"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import (CHANNEL_INDEX, CLIP_DURATION_S, MIN_DURATION_S,
                     MODEL_PATH, MODEL_VERSION, PROJECT_ROOT, RUNS_DIR,
                     SAMPLE_RATE, SCHEMA_VERSION)
from .features import features_from_wave, log_power_bands
from .inspect_data import load_wave, md5_of_file, wave_quality
from .train import ModelBundle, ModelUnavailableError, load_bundle

_BUNDLE: ModelBundle | None = None
_BUNDLE_MTIME: float | None = None

ASSET_ID_HINT = "asset_id 应为 P01~P06 之一（模拟设备编号）"


def _get_bundle(path: Path = MODEL_PATH) -> ModelBundle:
    """进程内缓存模型；文件更新后自动重载。"""
    global _BUNDLE, _BUNDLE_MTIME
    if not path.exists():
        raise ModelUnavailableError(
            f"模型文件缺失: {path}。请先运行 python -m audio_module.train。")
    mtime = path.stat().st_mtime
    if _BUNDLE is None or _BUNDLE_MTIME != mtime:
        _BUNDLE = load_bundle(path)
        _BUNDLE_MTIME = mtime
    return _BUNDLE


def _invalid(asset_id: str, bundle: ModelBundle | None, reason_code: str,
             message: str, quality: dict | None = None,
             run_id: str | None = None) -> dict:
    """构造 invalid_input 返回体。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "asset_id": asset_id,
        "model_version": bundle.model_version if bundle else MODEL_VERSION,
        "score": None,
        "threshold": (bundle.thresholds[bundle.primary_method] if bundle else None),
        "status": "invalid_input",
        "reason_code": reason_code,
        "quality": quality or {},
        "plot_file": None,
        "notes": [message],
        "run_id": run_id,
        "method": bundle.primary_method if bundle else None,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _plot(y: np.ndarray, sr: int, out_png: Path, title: str) -> str:
    """声学图：波形 + 对数功率频带图，返回相对项目根的路径。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_png.parent.mkdir(parents=True, exist_ok=True)
    band_map = log_power_bands(y)
    fig, axes = plt.subplots(2, 1, figsize=(9, 5.2), gridspec_kw={"height_ratios": [1, 2]})
    t = np.arange(len(y)) / sr
    axes[0].plot(t, y, linewidth=0.5, color="#2b6cb0")
    axes[0].set_xlabel("time (s)")
    axes[0].set_ylabel("amplitude")
    axes[0].set_title(title, fontsize=10)
    axes[0].grid(alpha=0.3)

    im = axes[1].imshow(band_map, aspect="auto", origin="lower", cmap="magma",
                        extent=[0, len(y) / sr, 0, band_map.shape[0]])
    axes[1].set_xlabel("time (s)")
    axes[1].set_ylabel("band index (mel, 20-8000 Hz)")
    fig.colorbar(im, ax=axes[1], label="log10 power")
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)
    try:
        return str(out_png.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(out_png).replace("\\", "/")


def analyze_audio(audio_path: str, asset_id: str, output_dir: str = "runs") -> dict:
    """分析一段水泵录音，返回 JSON 可序列化字典。"""
    bundle = _get_bundle()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    path = Path(audio_path)

    # ---------- 1. 可读性 ----------
    if not path.exists():
        return _invalid(asset_id, bundle, "file_not_found",
                        f"文件不存在: {path.name}", run_id=run_id)
    if not str(asset_id).strip():
        return _invalid(asset_id, bundle, "missing_asset_id", "缺少 asset_id", run_id=run_id)
    try:
        info = sf.info(str(path))
        wave = load_wave(path)
    except Exception as exc:
        return _invalid(asset_id, bundle, "decode_failed",
                        f"无法解码该音频（需为可解码的 WAV 文件）: {exc}", run_id=run_id)

    quality = wave_quality(wave["y"], wave["sample_rate"])
    quality.update({
        "channels": wave["channels"],
        "duration_s": round(wave["duration_s"], 3),
        "used_duration_s": round(wave["used_duration_s"], 3),
        "took_head_clip": wave["took_head_clip"],
        "channel_used": CHANNEL_INDEX,
        "filename": path.name,
        "md5": md5_of_file(path),
    })

    # ---------- 2. 输入合法性 ----------
    if wave["sample_rate"] != SAMPLE_RATE:
        return _invalid(asset_id, bundle, "sample_rate_mismatch",
                        f"采样率 {wave['sample_rate']} Hz 不符，首版只接受 {SAMPLE_RATE} Hz WAV",
                        quality, run_id)
    if len(wave["y"]) == 0 or path.stat().st_size <= 44:
        return _invalid(asset_id, bundle, "empty_file", "空文件：没有可用的音频采样数据",
                        quality, run_id)
    if wave["duration_s"] < MIN_DURATION_S:
        return _invalid(asset_id, bundle, "too_short",
                        f"录音过短（{wave['duration_s']:.2f} s），至少需要 {MIN_DURATION_S} s",
                        quality, run_id)
    if not quality["is_finite"]:
        return _invalid(asset_id, bundle, "non_finite",
                        "录音含 NaN/Inf 等非有限数值，无法计算特征", quality, run_id)
    if quality["is_all_zero"]:
        return _invalid(asset_id, bundle, "all_zero", "录音为全零（静音/空文件），无有效声音",
                        quality, run_id)

    # ---------- 3. 打分 ----------
    feat = features_from_wave(wave["y"])
    score = bundle.score(feat)
    threshold = float(bundle.thresholds[bundle.primary_method])
    status = "suspected_anomaly" if score >= threshold else "normal"

    notes = []
    if wave["took_head_clip"]:
        notes.append(f"录音长于 {CLIP_DURATION_S:.0f} 秒，仅使用前 {CLIP_DURATION_S:.0f} 秒")
    if quality["clipped_ratio"] > 0.01:
        notes.append(f"存在削波（{quality['clipped_ratio']*100:.1f}% 采样接近满幅），分数可信度下降")
    if quality["rms_dbfs"] < -55:
        notes.append("录音电平偏低，可能是远场或静音段，建议复核录音条件")
    notes.append("分数为模型相对异常程度，不是故障概率，也不判断具体损坏部件")

    # ---------- 4. 声学图 ----------
    run_dir = Path(output_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_ROOT / run_dir
    run_dir = run_dir / run_id
    png = run_dir / f"{asset_id}_spectrogram.png"
    plot_file = _plot(wave["y"], wave["sample_rate"], png,
                      f"{asset_id} | score={score:.4f} | thr={threshold:.4f} | {status}")

    result = {
        "schema_version": SCHEMA_VERSION,
        "asset_id": asset_id,
        "model_version": bundle.model_version,
        "score": round(float(score), 4),
        "threshold": round(threshold, 4),
        "status": status,
        "quality": {k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in quality.items()},
        "plot_file": plot_file,
        "notes": notes,
        "run_id": run_id,
        "method": bundle.primary_method,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    # 落盘单次结果，便于追溯（runs/<run_id>/result.json）
    (run_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="单文件声音异常推理")
    parser.add_argument("audio_path")
    parser.add_argument("asset_id", nargs="?", default="P01")
    parser.add_argument("--output-dir", default="runs")
    args = parser.parse_args()
    print(json.dumps(analyze_audio(args.audio_path, args.asset_id, args.output_dir),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
