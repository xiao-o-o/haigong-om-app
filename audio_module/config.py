"""声学模块统一配置（字段名统一英文，说明用中文）。

所有路径都是相对项目根的相对路径，代码中不出现个人电脑绝对路径。
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------- 路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"            # MIMII 原始解压目录，保持官方结构与标签
DEMO_RAW_DIR = DATA_DIR / "demo_raw"  # 合成演示数据，仅用于链路自检，不进入正式结论
META_DIR = DATA_DIR / "meta"          # manifest.csv / splits.csv
MODEL_DIR = PROJECT_ROOT / "models"
RUNS_DIR = PROJECT_ROOT / "runs"

MANIFEST_PATH = META_DIR / "manifest.csv"
SPLITS_PATH = META_DIR / "splits.csv"
MODEL_PATH = MODEL_DIR / "pump_v1.joblib"
MODEL_CONFIG_PATH = MODEL_DIR / "model_config.json"

# ---------------------------------------------------------------- 数据规格
SAMPLE_RATE = 16000          # 官方录制规格 16 kHz
CHANNEL_INDEX = 0            # 首版固定通道 0
CLIP_DURATION_S = 10.0       # 首版按约 10 秒录音处理；更长录音取前 10 秒并提示
MIN_DURATION_S = 1.0         # 短于该时长视为 invalid_input

# ---------------------------------------------------------------- 特征
N_FFT = 1024
HOP_LENGTH = 512
N_BANDS = 64                 # 对数功率频谱合并 64 个频带
FEATURE_DIM = 128            # 每带均值 + 标准差
FMIN_HZ = 20.0
FMAX_HZ = 8000.0             # 16 kHz 的奈奎斯特频率
EPS = 1e-10

# ---------------------------------------------------------------- 模型
SEED = 42
IF_N_ESTIMATORS = 200
KNN_K = 5
THRESHOLD_PERCENTILE = 95    # 正常验证分数的 95 分位作为初版阈值
MODEL_VERSION = "pump_v1"
SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------- 数据来源记录
DATASET_SOURCE = {
    "name": "MIMII public 1.0 (pump)",
    "version": "1.0",
    "url": "https://zenodo.org/records/3384388",
    "archive": "6_dB_pump.zip",
    "archive_md5": "a09ba6060c10fc09cd4c8770213b0b9f",
    "license": "CC BY-SA 4.0",
    "subset": "pump / id_00 起的正常与异常录音（+6 dB 噪声条件）",
    "task": "水泵声音二分类异常复核（normal / suspected_anomaly）",
    "limits": "公开工业录音，非海工现场采集；分数不是故障概率；仅取 +6 dB 单一噪声条件",
}

# ---------------------------------------------------------------- 工具函数
def ensure_dirs() -> None:
    """确保运行所需目录存在。"""
    for path in (RAW_DIR, DEMO_RAW_DIR, META_DIR, MODEL_DIR, RUNS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def feature_config() -> dict:
    """写入模型文件与配置文件的特征参数，便于复现。"""
    return {
        "sample_rate": SAMPLE_RATE,
        "channel_index": CHANNEL_INDEX,
        "clip_duration_s": CLIP_DURATION_S,
        "n_fft": N_FFT,
        "hop_length": HOP_LENGTH,
        "n_bands": N_BANDS,
        "feature_dim": FEATURE_DIM,
        "fmin_hz": FMIN_HZ,
        "fmax_hz": FMAX_HZ,
        "band_aggregation": "每带对数功率的时间均值 + 标准差",
    }


def dump_json(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
