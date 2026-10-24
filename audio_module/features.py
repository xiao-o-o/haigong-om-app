"""特征提取：对数功率频谱 -> 64 频带 -> 128 维（每带均值 + 标准差）。

首版固定参数（见 config）：
    16 kHz 输入、通道 0、STFT 窗长 1024、步长 512、Hann 窗、功率谱取以 10 为底的对数。
频带合并使用 64 个 mel 间隔的三角滤波器组（内置实现，不额外依赖 librosa）。
"""
from __future__ import annotations

import numpy as np

from .config import (EPS, FEATURE_DIM, FMAX_HZ, FMIN_HZ, HOP_LENGTH,
                     N_BANDS, N_FFT, SAMPLE_RATE)
from .inspect_data import load_wave


def _hz_to_mel(f: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(f, dtype=np.float64) / 700.0)


def _mel_to_hz(m: np.ndarray | float) -> np.ndarray | float:
    return 700.0 * (10.0 ** (np.asarray(m, dtype=np.float64) / 2595.0) - 1.0)


def mel_filterbank(n_bands: int = N_BANDS, n_fft: int = N_FFT,
                   sr: int = SAMPLE_RATE,
                   fmin: float = FMIN_HZ, fmax: float = FMAX_HZ) -> np.ndarray:
    """返回 (n_bands, n_fft//2 + 1) 的归一化三角滤波器矩阵。"""
    n_bins = n_fft // 2 + 1
    mel_pts = np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_bands + 2)
    hz_pts = _mel_to_hz(mel_pts)
    bin_pts = np.floor((n_fft + 1) * hz_pts / sr).astype(int)
    fb = np.zeros((n_bands, n_bins), dtype=np.float64)
    for i in range(n_bands):
        left, center, right = bin_pts[i], bin_pts[i + 1], bin_pts[i + 2]
        if center == left:
            center = left + 1
        if right == center:
            right = center + 1
        up = np.arange(left, center)
        if up.size:
            fb[i, up] = (up - left) / (center - left)
        down = np.arange(center, right)
        if down.size:
            fb[i, down] = (right - down) / (right - center)
    row_sums = fb.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    return fb / row_sums


_FRAMES_CACHE: dict = {}


def _frame_signal(y: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """按 (n_fft, hop) 分帧，末尾不足处补零，保证确定性。"""
    n = len(y)
    if n == 0:
        return np.zeros((0, n_fft))
    n_frames = 1 + max(0, (n - 1)) // hop if n < n_fft else 1 + (n - n_fft) // hop
    pad = (n_frames - 1) * hop + n_fft - n
    if pad > 0:
        y = np.concatenate([y, np.zeros(pad, dtype=y.dtype)])
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    return y[idx]


def log_power_bands(y: np.ndarray) -> np.ndarray:
    """单条波形 -> (n_bands, T) 的对数功率频带图（时间 x 频带）。"""
    win = np.hanning(N_FFT + 1)[:-1]  # 周期 Hann 窗
    frames = _frame_signal(y.astype(np.float64), N_FFT, HOP_LENGTH)
    spec = np.fft.rfft(frames * win[None, :], n=N_FFT, axis=1)
    power = np.abs(spec) ** 2
    log_power = np.log10(power + EPS)
    fb = mel_filterbank()
    return fb @ log_power.T  # (n_bands, T)


def features_from_wave(y: np.ndarray) -> np.ndarray:
    """单条波形 -> 128 维特征向量。"""
    band_map = log_power_bands(y)
    mean_t = band_map.mean(axis=1)
    std_t = band_map.std(axis=1, ddof=0)
    feat = np.concatenate([mean_t, std_t])
    assert feat.shape[0] == FEATURE_DIM
    return feat


def features_from_file(path: str) -> np.ndarray:
    """读取文件并提取 128 维特征（通道 0，超过 10 秒取前 10 秒）。"""
    wave = load_wave(path)
    return features_from_wave(wave["y"])


def feature_matrix(paths: list[str], progress_every: int = 200) -> np.ndarray:
    """批量提取特征，返回 (N, 128) 矩阵。"""
    feats = []
    for i, p in enumerate(paths, 1):
        feats.append(features_from_file(p))
        if progress_every and i % progress_every == 0:
            print(f"    特征提取进度: {i}/{len(paths)}")
    return np.asarray(feats, dtype=np.float64)
