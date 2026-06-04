"""Compare a scenario run against its canonical reference bundle.

Two tiers:

* **Tier 1: identity.** sha256 of ``canonical.f32.raw`` matches the
  reference: the run is byte-identical, nothing else to discuss.
* **Tier 2: tolerance.** When hashes differ (different GPU, driver,
  engine build, or a nondeterministic path), score perceptual/numeric
  distance and compare against per-scenario thresholds. Thresholds live
  in the refs manifest and MUST be calibrated from observed same-code
  variance (``runner --repeat``), never guessed: see README.

Metrics are computed on the canonical generation stream (mono mixdown):

* ``mel_l2``      mean L2 distance between log-mel frames (the workhorse)
* ``rms_db_diff`` overall level shift in dB
* ``win_cos_min`` worst per-second spectral cosine (localizes a glitch
                  that a global mean would average away)
"""

import hashlib
import json
from pathlib import Path

import numpy as np

from .client import SAMPLE_RATE

# Calibration fallbacks used only when a manifest entry carries no
# thresholds yet; deliberately strict so an uncalibrated comparison
# fails loudly rather than silently passing.
DEFAULT_THRESHOLDS = {"mel_l2": 0.10, "rms_db_diff": 0.5,
                      "win_cos_min": 0.995}


def load_canonical(bundle_dir: Path) -> np.ndarray:
    raw = (Path(bundle_dir) / "canonical.f32.raw").read_bytes()
    metrics = json.loads(
        (Path(bundle_dir) / "metrics.json").read_text(encoding="utf-8"))
    channels = int(metrics.get("ready", {}).get("channels", 2))
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, channels)


def sha256_of(bundle_dir: Path) -> str:
    return hashlib.sha256(
        (Path(bundle_dir) / "canonical.f32.raw").read_bytes()).hexdigest()


def _mono(audio: np.ndarray) -> np.ndarray:
    return audio.mean(axis=1) if audio.ndim == 2 else audio


def _log_mel(mono: np.ndarray) -> np.ndarray:
    import librosa

    mel = librosa.feature.melspectrogram(
        y=mono, sr=SAMPLE_RATE, n_fft=2048, hop_length=512, n_mels=64)
    return librosa.power_to_db(mel, ref=np.max)


def audio_metrics(ref: np.ndarray, run: np.ndarray) -> dict:
    """Tier-2 distance metrics between two canonical streams."""
    n = min(ref.shape[0], run.shape[0])
    a, b = _mono(ref[:n]), _mono(run[:n])

    eps = 1e-12
    rms_db_diff = abs(
        20 * np.log10(np.sqrt(np.mean(a ** 2)) + eps)
        - 20 * np.log10(np.sqrt(np.mean(b ** 2)) + eps))

    ma, mb = _log_mel(a), _log_mel(b)
    f = min(ma.shape[1], mb.shape[1])
    # Normalize per-frame L2 by the mel dimensionality so the number is
    # scale-comparable across n_mels choices.
    mel_l2 = float(np.linalg.norm(ma[:, :f] - mb[:, :f], axis=0).mean()
                   / np.sqrt(ma.shape[0]))

    win = SAMPLE_RATE  # 1 s windows
    cos = []
    for s in range(0, n - win + 1, win):
        fa = np.abs(np.fft.rfft(a[s:s + win]))
        fb = np.abs(np.fft.rfft(b[s:s + win]))
        denom = np.linalg.norm(fa) * np.linalg.norm(fb)
        cos.append(float(np.dot(fa, fb) / denom) if denom > eps else 1.0)

    return {
        "compared_samples": int(n),
        "len_ref": int(ref.shape[0]),
        "len_run": int(run.shape[0]),
        "mel_l2": round(mel_l2, 5),
        "rms_db_diff": round(float(rms_db_diff), 4),
        "win_cos_min": round(min(cos), 6) if cos else None,
        "win_cos_mean": round(float(np.mean(cos)), 6) if cos else None,
    }


def compare_bundles(ref_dir: Path, run_dir: Path,
                    thresholds: dict | None = None) -> dict:
    """Full comparison report. ``passed`` is True on tier-1 identity or
    on every tier-2 metric landing inside its threshold."""
    report: dict = {
        "ref_sha256": sha256_of(ref_dir),
        "run_sha256": sha256_of(run_dir),
    }
    if report["ref_sha256"] == report["run_sha256"]:
        report.update(tier=1, identical=True, passed=True)
        return report

    th = dict(DEFAULT_THRESHOLDS)
    th.update(thresholds or {})
    m = audio_metrics(load_canonical(ref_dir), load_canonical(run_dir))
    failures = []
    if m["len_run"] < m["len_ref"]:
        failures.append(f"run shorter than ref ({m['len_run']} < "
                        f"{m['len_ref']} samples)")
    if m["mel_l2"] > th["mel_l2"]:
        failures.append(f"mel_l2 {m['mel_l2']} > {th['mel_l2']}")
    if m["rms_db_diff"] > th["rms_db_diff"]:
        failures.append(f"rms_db_diff {m['rms_db_diff']} > "
                        f"{th['rms_db_diff']}")
    if m["win_cos_min"] is not None and m["win_cos_min"] < th["win_cos_min"]:
        failures.append(f"win_cos_min {m['win_cos_min']} < "
                        f"{th['win_cos_min']}")
    report.update(tier=2, identical=False, metrics=m,
                  thresholds=th, failures=failures,
                  passed=not failures)
    return report
