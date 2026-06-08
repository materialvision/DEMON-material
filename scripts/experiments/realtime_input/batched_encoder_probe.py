"""Batched fixed-1s VAE encode engine: build + latency/parity probe.

Follow-up to build_windowed_encoder.py: the b1 fixed-1s engine saves
2.7GB of TRT activation reservation but is overhead-dominated per call
(~6.4ms), making 150-window startup tiling ~1s and an 8-window bar
update ~53ms. The encoder ONNX has a dynamic batch axis, so a fixed-
length batched profile amortizes the launch overhead: startup = 10
calls of b16, bar update = 1 call of b8.

Profile: audio [1..16, 2, 48000] (batch dynamic, length fixed 1s),
opt at b8 (the bar-update shape). builder_optimization_level=0 (sub-5s
encode shapes emit a broken Myelin fusion at level 1 on the 5090).

Run:
    .venv/Scripts/python.exe scripts/experiments/realtime_input/batched_encoder_probe.py
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

faulthandler.enable()

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
FRAME = 1920
WIN_FRAMES = 25
WIN_SAMPLES = WIN_FRAMES * FRAME  # 48000
MARGIN_FRAMES = 10
POOL_FRAMES = 5
ENGINE_NAME = "vae_encode_fp16_1s_b16"
MAX_BATCH = 16


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


def build_batched_engine(onnx_path: Path, engine_path: Path) -> None:
    """Like vae_export.build_vae_trt_engine but with a dynamic BATCH
    axis and fixed window length (the stock builder hardcodes batch=1).
    """
    import tensorrt as trt

    engine_path.parent.mkdir(parents=True, exist_ok=True)
    trt_logger = trt.Logger(trt.Logger.INFO)
    builder = trt.Builder(trt_logger)
    network = builder.create_network(0)
    parser = trt.OnnxParser(network, trt_logger)
    if not parser.parse_from_file(str(onnx_path.resolve())):
        for i in range(parser.num_errors):
            print("ONNX parse error:", parser.get_error(i))
        raise RuntimeError("ONNX parsing failed")

    cfg = builder.create_builder_config()
    cfg.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 8 << 30)
    cfg.set_flag(trt.BuilderFlag.FP16)
    # Sub-5s encode shapes emit a broken Myelin fusion at level 1 on
    # the 5090 (access violation at context creation or first execute);
    # level 0 is clean. See build_windowed_encoder.py.
    cfg.builder_optimization_level = 0

    profile = builder.create_optimization_profile()
    profile.set_shape(
        "audio",
        min=(1, 2, WIN_SAMPLES),
        opt=(8, 2, WIN_SAMPLES),
        max=(MAX_BATCH, 2, WIN_SAMPLES),
    )
    if cfg.add_optimization_profile(profile) < 0:
        raise RuntimeError("failed to add optimization profile")

    serialized = builder.build_serialized_network(network, cfg)
    if serialized is None:
        raise RuntimeError("TRT engine build failed")
    engine_path.write_bytes(serialized)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--loop-duration", type=float, default=30.0)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--latency-iters", type=int, default=50)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import torch

    from acestep.nodes.vae_nodes import _get_trt_vae, _get_trt_stream
    from acestep.paths import trt_engines_dir
    from acestep.streaming.source import _load_known_fixture_waveform

    trt_dir = Path(trt_engines_dir())
    onnx_path = trt_dir / "_onnx_vae" / "vae_encode" / "vae_encode.onnx"
    engine_path = trt_dir / ENGINE_NAME / f"{ENGINE_NAME}.engine"
    full_engine = trt_dir / "vae_encode_fp16_60s" / "vae_encode_fp16_60s.engine"

    report: dict = {"engine": str(engine_path)}

    if args.skip_build and engine_path.exists():
        print(f"[build] skipped, using existing {engine_path}", flush=True)
    else:
        print(f"[build] {ENGINE_NAME} (batch 1..{MAX_BATCH} x 1s) ...",
              flush=True)
        t0 = time.time()
        build_batched_engine(onnx_path, engine_path)
        report["build_s"] = round(time.time() - t0, 1)
        print(f"[build] done in {report['build_s']}s", flush=True)

    device = torch.device("cuda")
    torch.zeros(1, device=device)

    def free_gb() -> float:
        free, _total = torch.cuda.mem_get_info()
        return free / 2**30

    def trt_encode_mean(audio_bct: torch.Tensor, path: str) -> torch.Tensor:
        entry = _get_trt_vae(path, device)
        ctx = entry["context"]
        stream = _get_trt_stream()
        dtypes = entry["tensor_dtypes"]
        inp = audio_bct.to(
            device=device, dtype=dtypes.get("audio", torch.float32)
        ).contiguous()
        if not ctx.set_input_shape("audio", tuple(inp.shape)):
            raise RuntimeError(f"rejected input shape {tuple(inp.shape)}")
        ctx.set_tensor_address("audio", inp.data_ptr())
        if ctx.infer_shapes():
            raise RuntimeError("shapes insufficiently specified")
        buf = torch.empty(
            tuple(ctx.get_tensor_shape("moments")),
            dtype=dtypes.get("moments", torch.float32), device=device)
        ctx.set_tensor_address("moments", buf.data_ptr())
        if not ctx.execute_async_v3(stream.ptr):
            raise RuntimeError("execute failed")
        stream.synchronize()
        mean, _ = buf.float().chunk(2, dim=1)
        return mean  # [B, 64, T]

    # Reservation.
    before = free_gb()
    warm = torch.randn(1, 2, WIN_SAMPLES, device=device)
    trt_encode_mean(warm, str(engine_path))
    report["reserve_gb"] = round(before - free_gb(), 3)
    print(f"[vram] b16 engine context reservation: {report['reserve_gb']}GB",
          flush=True)

    # Per-call latency at b1 / b8 / b16.
    for b in (1, 8, MAX_BATCH):
        x = torch.randn(b, 2, WIN_SAMPLES, device=device)
        for _ in range(5):
            trt_encode_mean(x, str(engine_path))
        lat = []
        for _ in range(args.latency_iters):
            t0 = time.perf_counter()
            trt_encode_mean(x, str(engine_path))
            lat.append((time.perf_counter() - t0) * 1000.0)
        report[f"call_ms_b{b}"] = _stats(lat)
        print(f"[latency] b{b}: p50={report[f'call_ms_b{b}']['p50']}ms "
              f"({report[f'call_ms_b{b}']['p50'] / b:.2f}ms/window)",
              flush=True)

    # Batch-vs-b1 parity on real audio (TRT batch invariance check).
    wf = _load_known_fixture_waveform(FIXTURE)
    n = int(SR * args.loop_duration)
    n -= n % (POOL_FRAMES * FRAME)
    loop = wf[:, :n].unsqueeze(0).to(device)
    n_frames = n // FRAME
    n_pools = n_frames // POOL_FRAMES

    def window_for_pool(g: int) -> tuple[int, int]:
        f0 = g * POOL_FRAMES
        w0 = min(max(f0 - MARGIN_FRAMES, 0), n_frames - WIN_FRAMES)
        return f0, w0

    g8 = list(range(60, 68))
    batch = torch.cat([
        loop[:, :, w0 * FRAME:(w0 + WIN_FRAMES) * FRAME]
        for _f0, w0 in (window_for_pool(g) for g in g8)
    ], dim=0)  # [8, 2, WIN_SAMPLES]
    mb = trt_encode_mean(batch, str(engine_path))
    m1 = torch.cat([
        trt_encode_mean(batch[i:i + 1], str(engine_path)) for i in range(8)
    ], dim=0)
    report["batch_vs_b1_max_abs"] = round((mb - m1).abs().max().item(), 6)
    print(f"[parity] b8 vs 8x b1 max|diff|={report['batch_vs_b1_max_abs']}",
          flush=True)

    # Startup tiling with batched calls.
    ref_mean = trt_encode_mean(loop, str(full_engine))
    tiled = torch.empty_like(ref_mean)
    t0 = time.perf_counter()
    for start in range(0, n_pools, MAX_BATCH):
        gs = list(range(start, min(start + MAX_BATCH, n_pools)))
        wins = torch.cat([
            loop[:, :, w0 * FRAME:(w0 + WIN_FRAMES) * FRAME]
            for _f0, w0 in (window_for_pool(g) for g in gs)
        ], dim=0)
        m = trt_encode_mean(wins, str(engine_path))
        for i, g in enumerate(gs):
            f0, w0 = window_for_pool(g)
            off = f0 - w0
            tiled[:, :, f0:f0 + POOL_FRAMES] = \
                m[i:i + 1, :, off:off + POOL_FRAMES]
    tiling_ms = (time.perf_counter() - t0) * 1000.0
    report["startup_tiling_ms"] = round(tiling_ms, 1)
    report["startup_calls"] = (n_pools + MAX_BATCH - 1) // MAX_BATCH

    diff = (tiled - ref_mean).pow(2).mean(dim=1).sqrt()
    cos = torch.nn.functional.cosine_similarity(
        tiled.transpose(1, 2), ref_mean.transpose(1, 2), dim=-1)
    report["tiling_parity"] = {
        "rms_mean": round(diff.mean().item(), 5),
        "rms_max": round(diff.max().item(), 5),
        "cos_min_frame": round(cos.min().item(), 6),
    }
    print(f"[tiling] {report['startup_calls']} batched calls in "
          f"{tiling_ms:.0f}ms  parity: rms_max="
          f"{report['tiling_parity']['rms_max']} cos_min="
          f"{report['tiling_parity']['cos_min_frame']}", flush=True)

    # Bar update = one b8 call.
    for _ in range(3):
        trt_encode_mean(batch, str(engine_path))
    lat = []
    for _ in range(20):
        t0 = time.perf_counter()
        trt_encode_mean(batch, str(engine_path))
        lat.append((time.perf_counter() - t0) * 1000.0)
    report["bar_update_ms"] = _stats(lat)
    print(f"[bar] 8-pool update (one b8 call) p50="
          f"{report['bar_update_ms']['p50']}ms", flush=True)

    out = Path(args.out) if args.out else (
        _REPO_ROOT / "runs" / "realtime-input"
        / f"batched-encoder-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[report] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
