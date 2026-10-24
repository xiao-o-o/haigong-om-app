"""audio_module: 海工智维水泵声音异常检测模块（声学A 负责）。

为避免 `python -m audio_module.train` 触发「模块已导入」告警，
这里只做惰性导入：`from audio_module import analyze_audio` 仍然可用。
"""
from .config import MODEL_VERSION, SCHEMA_VERSION

__all__ = ["analyze_audio", "ModelBundle", "ModelUnavailableError",
           "MODEL_VERSION", "SCHEMA_VERSION"]


def __getattr__(name: str):
    if name == "analyze_audio":
        from .infer import analyze_audio
        return analyze_audio
    if name in ("ModelBundle", "ModelUnavailableError"):
        from . import train
        return getattr(train, name)
    raise AttributeError(name)
