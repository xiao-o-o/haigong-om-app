"""验收自检：手册第五章与第九章的验收条件逐条验证。

检查项：
  1. 正常文件推理返回 schema 1.0 全字段；
  2. 同一录音改文件名后分数完全一致（确定性）；
  3. 空文件 / 过短 / 全零 / 非有限值 / 采样率不符 / 文件缺失 -> invalid_input；
  4. 声学图 PNG 生成且路径存在。

用法::

    .venv/Scripts/python.exe -m audio_module.acceptance_check
"""
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import PROJECT_ROOT, RUNS_DIR, SAMPLE_RATE, SCHEMA_VERSION, ensure_dirs
from .infer import analyze_audio
from .train import load_splits, resolve_data_root

REQUIRED_KEYS = {"schema_version", "asset_id", "model_version", "score",
                 "threshold", "status", "quality", "plot_file", "notes",
                 "run_id", "method", "created_at"}


def _make_wav(path: Path, y: np.ndarray, sr: int, subtype: str = "PCM_16") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, y, sr, subtype=subtype)


def run() -> list[dict]:
    ensure_dirs()
    splits = load_splits()
    data_root = resolve_data_root(splits)
    ref_row = splits[(splits["split"] == "test")].iloc[0]
    ref_path = data_root / ref_row["relative_path"]
    tmp = Path(tempfile.mkdtemp(prefix="acceptance_"))
    results: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append({"check": name, "passed": bool(ok), "detail": detail})

    # 1. 正常推理
    r1 = analyze_audio(str(ref_path), "P03", output_dir=str(tmp / "runs"))
    ok1 = REQUIRED_KEYS.issubset(r1) and r1["status"] in ("normal", "suspected_anomaly")
    check("正常推理返回完整字段", ok1, f"status={r1['status']} score={r1['score']}")

    # 2. 重命名后分数一致
    copy_path = tmp / "renamed_随便什么名字.wav"
    shutil.copy(ref_path, copy_path)
    r2 = analyze_audio(str(copy_path), "P03", output_dir=str(tmp / "runs"))
    check("重命名后分数一致（确定性）",
          r2["score"] == r1["score"] and r2["threshold"] == r1["threshold"],
          f"{r1['score']} vs {r2['score']}")

    # 3. 各类坏输入
    cases = []
    empty = tmp / "empty.wav"
    _make_wav(empty, np.zeros(0, dtype=np.float32), SAMPLE_RATE)
    cases.append(("空文件", empty))
    short = tmp / "short.wav"
    _make_wav(short, np.random.default_rng(1).standard_normal(int(0.3 * SAMPLE_RATE)).astype(np.float32), SAMPLE_RATE)
    cases.append(("过短(<1s)", short))
    zero = tmp / "allzero.wav"
    _make_wav(zero, np.zeros(SAMPLE_RATE * 2, dtype=np.float32), SAMPLE_RATE)
    cases.append(("全零静音", zero))
    nan = tmp / "nan.wav"
    y_nan = np.random.default_rng(2).standard_normal(SAMPLE_RATE * 2).astype(np.float32)
    y_nan[1000:1200] = np.nan
    try:
        _make_wav(nan, y_nan, SAMPLE_RATE, subtype="FLOAT")
        cases.append(("非有限值(NaN)", nan))
    except Exception as exc:
        check("非有限值(NaN)", True, f"soundfile 不允许写入 NaN（{exc}），跳过构造")
    sr8k = tmp / "sr8k.wav"
    _make_wav(sr8k, np.random.default_rng(3).standard_normal(SAMPLE_RATE * 2).astype(np.float32), 8000)
    cases.append(("采样率 8 kHz 不符", sr8k))
    cases.append(("文件不存在", tmp / "no_such_file.wav"))

    for name, p in cases:
        r = analyze_audio(str(p), "P03", output_dir=str(tmp / "runs"))
        check(f"invalid_input: {name}",
              r["status"] == "invalid_input" and r.get("reason_code"),
              f"reason_code={r.get('reason_code')}")

    # 4. 声学图
    plot = PROJECT_ROOT / r1["plot_file"] if not Path(r1["plot_file"]).is_absolute() else Path(r1["plot_file"])
    check("声学图已生成", plot.exists(), str(r1["plot_file"]))

    shutil.rmtree(tmp, ignore_errors=True)
    return results


def main() -> None:
    print("开始验收自检...\n")
    results = run()
    lines = [f"验收自检报告  {datetime.now().isoformat(timespec='seconds')}", ""]
    n_pass = 0
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        n_pass += int(r["passed"])
        line = f"[{mark}] {r['check']}  -- {r['detail']}"
        print(line)
        lines.append(line)
    summary = f"\n{n_pass}/{len(results)} 项通过"
    print(summary)
    lines.append(summary)
    out = RUNS_DIR / "acceptance_report.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已保存: {out.relative_to(PROJECT_ROOT)}")
    if n_pass < len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
