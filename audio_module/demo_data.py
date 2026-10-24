"""合成演示数据生成器（仅用于链路自检，不进入正式性能结论）。

模拟一台水泵的 16 kHz / 10 秒录音：
    正常 = 基频 + 谐波族 + 缓慢幅度起伏 + 宽带底噪
    异常 = 正常 + 周期性金属敲击（2~4 kHz 衰减振荡）+ 特定频带能量抬升 + 底噪升高
目录结构模仿 MIMII：6_dB_pump/pump/id_xx/{normal,abnormal}/00000000.wav
"""
from __future__ import annotations

import numpy as np
import soundfile as sf

from .config import DEMO_RAW_DIR, SAMPLE_RATE

DURATION_S = 10.0
N_SAMPLES = int(SAMPLE_RATE * DURATION_S)

DEVICE_PLAN = {
    "id_00": {"normal": 12, "abnormal": 6},
    "id_02": {"normal": 12, "abnormal": 6},
    "id_04": {"normal": 8, "abnormal": 6},
    "id_06": {"normal": 8, "abnormal": 6},
}


def _pump_normal(rng: np.random.Generator) -> np.ndarray:
    t = np.arange(N_SAMPLES) / SAMPLE_RATE
    f0 = 49.0 + rng.normal(0, 0.8)                       # 转速轻微波动
    y = np.zeros(N_SAMPLES)
    for k in range(1, 13):                               # 谐波族
        amp = 0.5 / k ** 1.2
        fk = f0 * k + rng.normal(0, 0.05)
        phase = rng.uniform(0, 2 * np.pi)
        y += amp * np.sin(2 * np.pi * fk * t + phase)
    wobble = 1.0 + 0.05 * np.sin(2 * np.pi * rng.uniform(0.2, 0.6) * t)  # 缓慢起伏
    noise = rng.normal(0, 0.012, N_SAMPLES)              # 宽带底噪
    y = y * wobble + noise
    return (0.6 * y / max(np.max(np.abs(y)), 1e-9)).astype(np.float32)  # 归一化到 -0.5~0.5，避免削波


def _pump_anomaly(rng: np.random.Generator) -> np.ndarray:
    y = _pump_normal(rng).astype(np.float64)
    t = np.arange(N_SAMPLES) / SAMPLE_RATE
    f0 = 49.0  # 仅用于异常调制的载波，与正常基频附近一致
    burst_f = rng.uniform(2200, 4000)                    # 敲击固有频率
    click_t = rng.uniform(0, 0.35)
    click_period = rng.uniform(0.55, 0.95)
    click_train = np.zeros(N_SAMPLES)
    while click_t < DURATION_S:
        idx0 = int(click_t * SAMPLE_RATE)
        dur = min(int(0.03 * SAMPLE_RATE), N_SAMPLES - idx0)
        if dur <= 0:
            break
        tt = np.arange(dur) / SAMPLE_RATE
        click_train[idx0:idx0 + dur] += np.exp(-tt * 140) * np.sin(2 * np.pi * burst_f * tt)
        click_t += click_period
    resonance = 0.12 * np.sin(2 * np.pi * rng.uniform(1400, 1800) * t)   # 特定频带抬升
    noise = rng.normal(0, 0.025, N_SAMPLES)              # 底噪升高
    y = y + 0.5 * click_train + 0.25 * resonance * (1 + 0.3 * np.sin(2 * np.pi * f0 * t)) + noise
    y = y / max(np.max(np.abs(y)), 1e-9) * 0.6               # 归一化，保留异常相对能量差异
    return y.astype(np.float32)


def generate(root=DEMO_RAW_DIR) -> dict:
    """生成演示数据集，返回统计信息。同名文件直接覆盖，不做删除操作。"""
    base = root / "6_dB_pump" / "pump"
    rng = np.random.default_rng(42)
    stats = {}
    for device, plan in DEVICE_PLAN.items():
        for label, count in plan.items():
            out_dir = base / device / label
            out_dir.mkdir(parents=True, exist_ok=True)
            for i in range(count):
                name = f"{str(i).zfill(8)}.wav"
                y = _pump_anomaly(rng) if label == "abnormal" else _pump_normal(rng)
                sf.write(out_dir / name, y, SAMPLE_RATE, subtype="PCM_16")
            stats[f"{device}/{label}"] = count
    print("合成演示数据已生成（seed=42，16 kHz × 10 s）:")
    for k, v in stats.items():
        print(f"  {k}: {v} 条")
    print(f"目录: {base}")
    return stats


if __name__ == "__main__":
    generate()
