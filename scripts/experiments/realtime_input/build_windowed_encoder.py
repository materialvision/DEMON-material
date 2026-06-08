"""Build + validate the fixed 5-second windowed VAE *encode* engine.

Mirrors the windowed decode engine rationale (acestep/paths.py:640): a
fixed small profile reserves a small TRT activation workspace vs the
ranged 5-60s engine. Margin sizing comes from the measured encoder
receptive field (~7 frames; 10 frames / 0.4s = floor-exact -- see
encoder_receptive_field.py).

Why 5s and not 1s (measured 2026-06-07): sub-5s encode shapes are a
trap on this TRT/5090 combo. At builder opt level 1 (the documented
workaround level for the 5-60s graph) they emit a broken Myelin
fusion -- static profiles access-violate at create_execution_context,
dynamic ones at the first execute_async_v3. Level 0 builds run but
its kernels are ~7x slower per sample (6.4ms per 1s window; batching
b8/b16 does NOT amortize it -- ~5ms/window, compute-bound). A fixed
5s window sits inside the shape range where level 1 is proven, runs
one window in ~4.4ms, and with 0.4s margins each side yields 4.2s of
usable center per call.

Validates the whole live-input scheme:
  1. Build vae_encode_fp16_5s_fixed from the existing encoder ONNX.
  2. TRT activation reservation: fixed 5s vs the 5-60s ranged engine.
  3. Per-call latency (5s window).
  4. Startup tiling: 30s loop as 8 windowed calls -> assembled
     latent, wall time, parity vs the full-context encode.
  5. Live bar update: one 5s call re-encoding 8 pool groups (1.6s),
     spliced; latency and parity of the spliced region.

Parity is compared on the moments MEAN (deterministic), not sampled
latents, so the comparison is not masked by the mean+std*randn
sampling (rms ~0.022) of the production encode path.

Run:
    .venv/Scripts/python.exe scripts/experiments/realtime_input/build_windowed_encoder.py
    .venv/Scripts/python.exe scripts/experiments/realtime_input/build_windowed_encoder.py --skip-build
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import time

faulthandler.enable()
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = next(
    p for p in (_HERE, *_HERE.parents) if (p / "pyproject.toml").exists()
)
# Force OUR repo to the front (sibling ACE-Step shadows `acestep`).
while str(_REPO_ROOT) in sys.path:
    sys.path.remove(str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT))

FIXTURE = "low_fi_Gm_loop_60s_gnm.wav"
SR = 48000
FRAME = 1920            # samples per latent frame (25 fps)
WIN_FRAMES = 125        # 5s engine window (sub-5s = Myelin trap, see above)
MARGIN_FRAMES = 10      # measured encoder RF: 10 frames = floor-exact
POOL_FRAMES = 5         # semantic pool group (splice granularity)
KEEP_FRAMES = WIN_FRAMES - 2 * MARGIN_FRAMES  # 105 fr = 4.2s per call
ENGINE_NAME = "vae_encode_fp16_5s_fixed"


def _stats(xs):
    xs = sorted(xs)
    n = len(xs)
    return {
        "mean": round(sum(xs) / n, 3),
        "min": round(xs[0], 3),
        "p50": round(xs[n // 2], 3),
        "max": round(xs[-1], 3),
        "n": n,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--loop-duration", type=float, default=30.0)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--latency-iters", type=int, default=50)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import torch

    from acestep.paths import trt_engines_dir
    from acestep.streaming.source import _load_known_fixture_waveform

    trt_dir = Path(trt_engines_dir())
    onnx_path = trt_dir / "_onnx_vae" / "vae_encode" / "vae_encode.onnx"
    engine_path = trt_dir / ENGINE_NAME / f"{ENGINE_NAME}.engine"
    full_engine = trt_dir / "vae_encode_fp16_60s" / "vae_encode_fp16_60s.engine"
    assert onnx_path.exists(), f"encoder ONNX missing: {onnx_path}"
    assert full_engine.exists(), f"ranged engine missing: {full_engine}"

    report: dict = {"engine": str(engine_path), "notes": []}

    # ------------------------------------------------------------------
    # 1) Build (pure TRT, no torch export -- ONNX already on disk).
    # ------------------------------------------------------------------
    if args.skip_build and engine_path.exists():
        print(f"[build] skipped, using existing {engine_path}", flush=True)
        report["build_s"] = None
    else:
        from acestep.engine.trt.vae_export import (
            VAETRTBuildConfig,
            build_vae_encode_engine,
        )

        win_samples = WIN_FRAMES * FRAME  # 240000 = 5s
        cfg = VAETRTBuildConfig(
            workspace_gb=8.0,
            encode_min_samples=win_samples,
            encode_opt_samples=win_samples,
            encode_max_samples=win_samples,
            # Default level 1: 5s is inside the shape range where the
            # level-1 build is proven on the 5090 (the ranged 5-60s
            # engine runs it daily). Do NOT shrink the window below 5s
            # -- see the module docstring for the sub-5s Myelin trap.
        )
        print(f"[build] {ENGINE_NAME} (fixed {win_samples} samples) ...",
              flush=True)
        t0 = time.time()
        build_vae_encode_engine(onnx_path, engine_path, config=cfg)
        report["build_s"] = round(time.time() - t0, 1)
        print(f"[build] done in {report['build_s']}s", flush=True)
    report["engine_mb"] = round(engine_path.stat().st_size / 1e6, 1)

    # ------------------------------------------------------------------
    # Runtime helpers: deterministic mean-encode via either engine.
    # ------------------------------------------------------------------
    from acestep.nodes.vae_nodes import _get_trt_vae, _get_trt_stream

    device = torch.device("cuda")
    torch.zeros(1, device=device)  # init CUDA context before baselines

    def trt_encode_mean(audio_bct: torch.Tensor, path: str) -> torch.Tensor:
        """Encode [B,2,S] -> moments mean [B,64,T] (no sampling)."""
        entry = _get_trt_vae(path, device)
        ctx = entry["context"]
        stream = _get_trt_stream()
        dtypes = entry["tensor_dtypes"]
        inp = audio_bct.to(
            device=device, dtype=dtypes.get("audio", torch.float32)
        ).contiguous()
        if not ctx.set_input_shape("audio", tuple(inp.shape)):
            raise RuntimeError(f"rejected input shape {tuple(inp.shape)}")
        if not ctx.set_tensor_address("audio", inp.data_ptr()):
            raise RuntimeError("rejected input address")
        if ctx.infer_shapes():
            raise RuntimeError("shapes insufficiently specified")
        out_shape = tuple(ctx.get_tensor_shape("moments"))
        buf = torch.empty(
            out_shape, dtype=dtypes.get("moments", torch.float32),
            device=device)
        if not ctx.set_tensor_address("moments", buf.data_ptr()):
            raise RuntimeError("rejected output address")
        if not ctx.execute_async_v3(stream.ptr):
            raise RuntimeError("execute failed")
        stream.synchronize()
        mean, _logvar = buf.float().chunk(2, dim=1)
        return mean  # [B, 64, T]

    def free_gb() -> float:
        free, _total = torch.cuda.mem_get_info()
        return free / 2**30

    # ------------------------------------------------------------------
    # 2) Activation reservation: ranged 5-60s vs fixed 1s.
    # ------------------------------------------------------------------
    wf = _load_known_fixture_waveform(FIXTURE)
    n = int(SR * args.loop_duration)
    n -= n % (POOL_FRAMES * FRAME)
    loop = wf[:, :n].unsqueeze(0).to(device)  # [1, 2, S]
    n_frames = n // FRAME
    n_pools = n_frames // POOL_FRAMES

    before = free_gb()
    ref_mean = trt_encode_mean(loop, str(full_engine))  # also loads engine
    after = free_gb()
    report["ranged_engine_reserve_gb"] = round(before - after, 3)

    before = free_gb()
    _ = trt_encode_mean(loop[:, :, :WIN_FRAMES * FRAME], str(engine_path))
    after = free_gb()
    report["fixed_engine_reserve_gb"] = round(before - after, 3)
    print(f"[vram] context reservation: ranged 5-60s="
          f"{report['ranged_engine_reserve_gb']}GB  {ENGINE_NAME}="
          f"{report['fixed_engine_reserve_gb']}GB", flush=True)

    # ------------------------------------------------------------------
    # 3) Per-call latency (5s window).
    # ------------------------------------------------------------------
    win = loop[:, :, :WIN_FRAMES * FRAME]
    for _ in range(5):
        trt_encode_mean(win, str(engine_path))
    lat = []
    for _ in range(args.latency_iters):
        t0 = time.perf_counter()
        trt_encode_mean(win, str(engine_path))
        lat.append((time.perf_counter() - t0) * 1000.0)
    report["call_ms"] = _stats(lat)
    print(f"[latency] 5s window p50={report['call_ms']['p50']}ms", flush=True)

    # ------------------------------------------------------------------
    # 4) Startup tiling: assemble the full loop latent from windowed
    #    calls, KEEP_FRAMES of usable center per call. Windows are
    #    clamped to the loop; clamped edges are exact because the true
    #    audio boundary IS the full-encode context there.
    # ------------------------------------------------------------------
    def window_for_keep(k0: int) -> int:
        """Window start (frames) whose center covers keep [k0, k0+KEEP)."""
        return min(max(k0 - MARGIN_FRAMES, 0), n_frames - WIN_FRAMES)

    tiled = torch.empty_like(ref_mean)  # [1, 64, T]
    n_windows = 0
    t0 = time.perf_counter()
    for k0 in range(0, n_frames, KEEP_FRAMES):
        k1 = min(k0 + KEEP_FRAMES, n_frames)
        w0 = window_for_keep(k0)
        m = trt_encode_mean(
            loop[:, :, w0 * FRAME:(w0 + WIN_FRAMES) * FRAME],
            str(engine_path))
        off = k0 - w0
        tiled[:, :, k0:k1] = m[:, :, off:off + (k1 - k0)]
        n_windows += 1
    tiling_ms = (time.perf_counter() - t0) * 1000.0
    report["startup_tiling_ms"] = round(tiling_ms, 1)
    report["startup_windows"] = n_windows

    diff = (tiled - ref_mean).pow(2).mean(dim=1).sqrt().squeeze(0)  # [T]
    cos = torch.nn.functional.cosine_similarity(
        tiled.transpose(1, 2), ref_mean.transpose(1, 2), dim=-1).squeeze(0)
    report["tiling_parity"] = {
        "latent_rms": round(ref_mean.pow(2).mean().sqrt().item(), 4),
        "rms_mean": round(diff.mean().item(), 5),
        "rms_max": round(diff.max().item(), 5),
        "cos_min_frame": round(cos.min().item(), 6),
    }
    print(f"[tiling] {n_windows} windows in {tiling_ms:.0f}ms  "
          f"parity vs full-context mean: rms_mean="
          f"{report['tiling_parity']['rms_mean']} rms_max="
          f"{report['tiling_parity']['rms_max']} cos_min="
          f"{report['tiling_parity']['cos_min_frame']}", flush=True)

    # ------------------------------------------------------------------
    # 5) Live bar update: ONE 5s call centered on a changed 1.6s bar
    #    (8 pool groups), splice the bar's frames; latency + parity.
    # ------------------------------------------------------------------
    f_lo, f_hi = 300, 340  # 12.0s..13.6s, pool-aligned
    w0 = min(max((f_lo + f_hi) // 2 - WIN_FRAMES // 2, 0),
             n_frames - WIN_FRAMES)
    spliced = tiled.clone()
    for _ in range(3):
        trt_encode_mean(
            loop[:, :, w0 * FRAME:(w0 + WIN_FRAMES) * FRAME],
            str(engine_path))
    lat = []
    for _ in range(20):
        t0 = time.perf_counter()
        m = trt_encode_mean(
            loop[:, :, w0 * FRAME:(w0 + WIN_FRAMES) * FRAME],
            str(engine_path))
        lat.append((time.perf_counter() - t0) * 1000.0)
    off = f_lo - w0
    spliced[:, :, f_lo:f_hi] = m[:, :, off:off + (f_hi - f_lo)]
    bar_diff = (spliced[:, :, f_lo:f_hi] - ref_mean[:, :, f_lo:f_hi]) \
        .pow(2).mean(dim=1).sqrt()
    report["bar_update"] = {
        "span_s": (f_hi - f_lo) / 25.0,
        "ms": _stats(lat),
        "rms_max": round(bar_diff.max().item(), 5),
    }
    print(f"[bar] {report['bar_update']['span_s']}s bar re-encoded+spliced "
          f"in one call, p50={report['bar_update']['ms']['p50']}ms  "
          f"rms_max={report['bar_update']['rms_max']}", flush=True)

    out = Path(args.out) if args.out else (
        _REPO_ROOT / "runs" / "realtime-input"
        / f"windowed-encoder-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[report] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
