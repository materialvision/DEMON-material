"""Real-time audio-input encode probe.

Scenario: a sequencer feeds DEMON a live ~30s loop and we re-encode the
input latent continuously (or every bar, or on change). This probe
answers the three open questions with measurements on the real
production paths:

  1. Component costs at the loop length: VAE encode (the input latent),
     semantic extract (the structure reference, incremental on top of
     the latent), and conditioning re-encode (the timbre reference's
     ``cf`` half; ``cs`` is latent-independent and cacheable).
  2. Headroom: VRAM with the VAE encoder held resident, sustained
     encode rate flat out, and what hammering encodes does to the
     generation loop (and vice versa) when both run concurrently.
  3. Cadence inputs: encode latency vs window duration (every-bar
     windows down to 1s), plus the encode sampling-noise floor
     (latent = mean + std*randn, so identical audio never re-encodes
     identically -- change detection must look at PCM or means).

Run:
    .venv/Scripts/python.exe scripts/experiments/realtime_input/encode_probe.py
    .venv/Scripts/python.exe scripts/experiments/realtime_input/encode_probe.py --accel eager --vae-accel eager
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
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
PROMPT = "lofi hip hop, mellow, instrumental"
POOL = 9600  # 1 latent frame (1920 samples) * 5-frame semantic grouping


def _stats(xs):
    xs = sorted(xs)
    n = len(xs)
    return {
        "mean": round(sum(xs) / n, 2),
        "min": round(xs[0], 2),
        "p50": round(xs[n // 2], 2),
        "max": round(xs[-1], 2),
        "n": n,
    }


def _vram(tag):
    import torch

    free, total = torch.cuda.mem_get_info()
    snap = {
        "tag": tag,
        "free_gb": round(free / 2**30, 2),
        "total_gb": round(total / 2**30, 2),
        "torch_alloc_gb": round(torch.cuda.memory_allocated() / 2**30, 2),
        "torch_reserved_gb": round(torch.cuda.memory_reserved() / 2**30, 2),
        "torch_peak_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
    }
    print(f"  [vram:{tag}] free={snap['free_gb']}GB "
          f"alloc={snap['torch_alloc_gb']}GB peak={snap['torch_peak_gb']}GB",
          flush=True)
    return snap


def _timed(fn, iters, warmup=1):
    import torch

    for _ in range(warmup):
        fn()
    torch.cuda.synchronize()
    out = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        torch.cuda.synchronize()
        out.append((time.perf_counter() - t0) * 1000.0)
    return _stats(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--checkpoint", default="acestep-v15-turbo")
    ap.add_argument("--accel", default="tensorrt",
                    choices=("tensorrt", "eager", "compile"),
                    help="decoder (DiT) backend for the generation loop")
    ap.add_argument("--vae-accel", default="tensorrt",
                    choices=("tensorrt", "eager"),
                    help="VAE backend; tensorrt falls back to eager if the "
                         "engine profile rejects the loop length")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--loop-duration", type=float, default=30.0,
                    help="the live-input loop length in seconds")
    ap.add_argument("--durations", default="1,2,5,10,15,30,60",
                    help="encode-window durations (s) for the scaling sweep")
    ap.add_argument("--iters", type=int, default=5)
    ap.add_argument("--sustained-iters", type=int, default=20)
    ap.add_argument("--measure-ticks", type=int, default=200)
    ap.add_argument("--concurrent-ticks", type=int, default=300)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import torch

    from acestep.nodes.types import Audio, Latent
    from acestep.nodes.semantic_nodes import SemanticExtract
    from acestep.nodes.vae_nodes import (
        VAEEncodeAudio,
        _find_best_vae_engine,
        _trt_available,
    )
    from acestep.streaming import registry
    from acestep.streaming.config import SessionConfig
    from acestep.streaming.encode import encode_cond_pair
    from acestep.streaming.generator_backend import TickContext
    from acestep.streaming.session import StreamingSession
    from acestep.streaming.source import (
        SAMPLE_RATE,
        _load_known_fixture_waveform,
    )

    report: dict = {
        "checkpoint": args.checkpoint,
        "accel": args.accel,
        "vae_accel": args.vae_accel,
        "steps": args.steps,
        "depth": args.depth,
        "loop_duration_s": args.loop_duration,
        "vram": [],
        "notes": [],
    }

    waveform = _load_known_fixture_waveform(FIXTURE)  # [<=2, N] f32 @ 48k

    def slice_s(dur_s: float) -> torch.Tensor:
        # Clamp to the fixture (the "60s" fixture is slightly under 60s)
        # and pool-align.
        n = min(int(SAMPLE_RATE * dur_s), waveform.shape[-1])
        n -= n % POOL
        return waveform[:, :n].contiguous()

    loop_wf = slice_s(args.loop_duration)
    loop_audio = Audio(waveform=loop_wf, sample_rate=SAMPLE_RATE)

    # ------------------------------------------------------------------
    # Phase 0: session at the loop length (the live-input scenario:
    # the session's source IS the 30s loop, not a 60s song).
    # ------------------------------------------------------------------
    print(f"[setup] session: checkpoint={args.checkpoint} "
          f"decoder={args.accel} vae={args.vae_accel} "
          f"loop={args.loop_duration}s", flush=True)
    vae_accel = args.vae_accel
    cfg = SessionConfig.from_dict({
        "prompt": PROMPT, "steps": args.steps, "depth": args.depth,
        "sde": False, "lora": False,
    })

    def create(vb):
        return StreamingSession.create(
            audio=Audio(waveform=loop_wf.clone(), sample_rate=SAMPLE_RATE),
            config=cfg,
            checkpoint=args.checkpoint,
            decoder_backend=args.accel,
            vae_backend=vb,
            session_id=registry.new_session_id(),
        )

    try:
        streaming = create(vae_accel)
    except Exception as exc:
        if vae_accel != "tensorrt":
            raise
        note = (f"session create with vae_backend=tensorrt failed at "
                f"{args.loop_duration}s ({type(exc).__name__}: {exc}); "
                f"retrying with eager VAE")
        print(f"[setup] {note}", flush=True)
        report["notes"].append(note)
        vae_accel = "eager"
        streaming = create(vae_accel)
    report["vae_accel_effective"] = vae_accel

    try:
        try:
            streaming.audio_eng.stop()
        except Exception:
            pass
        session = streaming.session
        handler = session.handler
        report["offload_to_cpu"] = bool(getattr(handler, "offload_to_cpu", False))
        trt_encode_engine = (
            _find_best_vae_engine("vae_encode") if _trt_available() else None
        )
        report["trt_vae_encode_engine"] = trt_encode_engine
        # In a TRT-VAE session the torch VAE is never instantiated
        # (handler.vae is None) -- there is no eager encoder to pin.
        eager_available = getattr(handler, "vae", None) is not None
        report["eager_vae_available"] = eager_available
        if not eager_available:
            report["notes"].append(
                "torch VAE not instantiated (TRT-VAE session); resident-eager "
                "measurements skipped -- rerun with --vae-accel eager for those")
        print(f"[setup] offload_to_cpu={report['offload_to_cpu']} "
              f"eager_vae={eager_available} "
              f"trt_vae_encode={trt_encode_engine}", flush=True)

        torch.cuda.reset_peak_memory_stats()
        report["vram"].append(_vram("after_session_create"))

        # --------------------------------------------------------------
        # Phase 1: encode latency vs window duration.
        #   node path  = production VAEEncodeAudio (TRT if cached, else
        #                eager incl. any load/offload tax)
        #   resident   = eager VAE pinned on GPU, direct encode
        # --------------------------------------------------------------
        durations = [float(d) for d in args.durations.split(",")]
        encode_rows = []
        latents_by_dur: dict[float, Latent] = {}
        print("[phase1] encode latency vs duration", flush=True)
        for dur in durations:
            wf = slice_s(dur)
            dur_actual = wf.shape[-1] / 48000.0
            audio = Audio(waveform=wf, sample_rate=SAMPLE_RATE)
            row: dict = {"duration_s": round(dur_actual, 2),
                         "samples": wf.shape[-1],
                         "latent_frames": wf.shape[-1] // 1920}

            try:
                node_out: dict = {}

                def node_call():
                    node_out["latent"] = VAEEncodeAudio().execute(
                        vae=session.vae, audio=audio,
                    )["latent"]

                row["node_ms"] = _timed(node_call, args.iters)
                latents_by_dur[dur] = node_out["latent"]
            except RuntimeError as exc:
                # Typical: TRT engine profile rejects this window length.
                row["node_error"] = f"{exc}"
                print(f"  dur={dur:>5.1f}s node path FAILED: {exc}",
                      flush=True)

            if eager_available:
                wf3 = wf.unsqueeze(0)  # [1, C, S]
                with handler._load_model_context("vae"):
                    resident_out: dict = {}

                    def resident_call():
                        resident_out["lat"] = (
                            handler._encode_audio_to_latents(wf3))

                    row["resident_eager_ms"] = _timed(
                        resident_call, args.iters)
                if dur not in latents_by_dur:
                    latents_by_dur[dur] = Latent(tensor=resident_out["lat"])
                row["resident_rtf"] = round(
                    dur_actual * 1000.0 / row["resident_eager_ms"]["p50"], 1)

            node_p50 = row.get("node_ms", {}).get("p50")
            if node_p50 is not None:
                row["node_rtf"] = round(dur_actual * 1000.0 / node_p50, 1)
            res_p50 = row.get("resident_eager_ms", {}).get("p50")
            print(f"  dur={dur:>5.1f}s "
                  f"node_p50={node_p50 if node_p50 is not None else 'n/a':>8} ms"
                  f" ({row.get('node_rtf', 'n/a')}x RT)  "
                  f"resident_p50={res_p50 if res_p50 is not None else 'n/a':>8} ms"
                  f" ({row.get('resident_rtf', 'n/a')}x RT)", flush=True)
            encode_rows.append(row)
        report["encode_vs_duration"] = encode_rows
        report["vram"].append(_vram("after_encode_sweep"))

        # --------------------------------------------------------------
        # Phase 2: load/offload tax (what "resident" buys over the
        # default offloading path), only meaningful when offloading.
        # --------------------------------------------------------------
        if report["offload_to_cpu"] and eager_available:
            print("[phase2] VAE load/offload tax", flush=True)
            t0 = time.perf_counter()
            ctx = handler._load_model_context("vae")
            ctx.__enter__()
            torch.cuda.synchronize()
            load_ms = (time.perf_counter() - t0) * 1000.0
            t0 = time.perf_counter()
            ctx.__exit__(None, None, None)
            offload_ms = (time.perf_counter() - t0) * 1000.0
            report["vae_load_ms"] = round(load_ms, 1)
            report["vae_offload_ms"] = round(offload_ms, 1)
            print(f"  load={load_ms:.0f}ms offload={offload_ms:.0f}ms",
                  flush=True)

        # --------------------------------------------------------------
        # Phase 3: structure reference = semantic extract, incremental
        # on an already-encoded latent (tokenize + detokenize).
        # --------------------------------------------------------------
        print("[phase3] semantic extract (structure ref) vs duration",
              flush=True)
        semantic_rows = []
        for dur in durations:
            if dur not in latents_by_dur:
                continue  # no path produced a latent at this window length
            lat = latents_by_dur[dur]

            def sem_call():
                SemanticExtract().execute(model=session.model, latent=lat)

            st = _timed(sem_call, args.iters)
            semantic_rows.append({"duration_s": dur, "ms": st})
            print(f"  dur={dur:>5.1f}s p50={st['p50']:>8} ms", flush=True)
        report["semantic_vs_duration"] = semantic_rows

        # --------------------------------------------------------------
        # Phase 4: timbre reference = conditioning re-encode against the
        # loop latent. cs (refer=None) is latent-independent/cacheable;
        # cf is the part a live re-encode must repeat.
        # --------------------------------------------------------------
        print("[phase4] conditioning encode (timbre ref)", flush=True)

        def encode_loop_once() -> Latent:
            return VAEEncodeAudio().execute(
                vae=session.vae, audio=loop_audio,
            )["latent"]

        loop_latent = latents_by_dur.get(args.loop_duration)
        if loop_latent is None:
            loop_latent = encode_loop_once()
        state = streaming.state
        bpm = int(getattr(state, "bpm", None) or 120)
        key = getattr(state, "key", None) or "C major"
        tsig = getattr(state, "time_signature", None) or "4"

        from acestep.constants import TASK_INSTRUCTIONS

        def cs_call():
            session.encode_text(
                tags=PROMPT, lyrics="[Instrumental]",
                instruction=TASK_INSTRUCTIONS["cover"], refer_latent=None,
                bpm=bpm, duration=args.loop_duration, key=key,
                time_signature=tsig,
            )

        def cf_call():
            session.encode_text(
                tags=PROMPT, lyrics="[Instrumental]",
                instruction=TASK_INSTRUCTIONS["cover"],
                refer_latent=loop_latent,
                bpm=bpm, duration=args.loop_duration, key=key,
                time_signature=tsig,
            )

        def pair_call():
            encode_cond_pair(session, PROMPT, loop_latent, bpm,
                             args.loop_duration, key, tsig)

        report["cond_cs_ms"] = _timed(cs_call, args.iters)
        report["cond_cf_ms"] = _timed(cf_call, args.iters)
        report["cond_pair_ms"] = _timed(pair_call, args.iters)
        print(f"  cs(cacheable) p50={report['cond_cs_ms']['p50']} ms  "
              f"cf(per-reencode) p50={report['cond_cf_ms']['p50']} ms  "
              f"pair p50={report['cond_pair_ms']['p50']} ms", flush=True)

        # --------------------------------------------------------------
        # Phase 5: encode sampling-noise floor. The VAE encode SAMPLES
        # (mean + std*randn), so identical audio never produces an
        # identical latent. Quantifies why latent-space change detection
        # needs a threshold above this floor (or must compare means/PCM).
        # --------------------------------------------------------------
        print("[phase5] encode sampling-noise floor (identical audio x2)",
              flush=True)
        la = encode_loop_once().tensor.float()
        lb = encode_loop_once().tensor.float()
        cos = torch.nn.functional.cosine_similarity(
            la.flatten(1), lb.flatten(1), dim=1
        ).item()
        report["resample_cos"] = round(cos, 6)
        report["resample_max_abs_diff"] = round(
            (la - lb).abs().max().item(), 4)
        report["resample_rms_diff"] = round(
            (la - lb).pow(2).mean().sqrt().item(), 4)
        print(f"  cos={report['resample_cos']} "
              f"max|diff|={report['resample_max_abs_diff']} "
              f"rms={report['resample_rms_diff']}", flush=True)
        del la, lb

        # --------------------------------------------------------------
        # Phase 6: sustained flat-out encode rate at the loop length
        # (resident eager), nothing else running.
        # --------------------------------------------------------------
        print("[phase6] sustained encode rate (production path, flat out)",
              flush=True)
        torch.cuda.reset_peak_memory_stats()
        report["vram"].append(_vram("before_sustained"))
        encode_loop_once()  # warm
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        for _ in range(args.sustained_iters):
            encode_loop_once()
        torch.cuda.synchronize()
        wall = time.perf_counter() - t0
        report["sustained_encodes_per_s"] = round(
            args.sustained_iters / wall, 2)
        report["vram"].append(_vram("after_sustained"))
        print(f"  {report['sustained_encodes_per_s']} encodes/s "
              f"({args.loop_duration}s window)", flush=True)

        # --------------------------------------------------------------
        # Phase 7: concurrency. Baseline generation loop, then the same
        # loop with a background thread hammering resident encodes on a
        # side CUDA stream (what a live-input feed would do).
        # --------------------------------------------------------------
        print("[phase7] generation loop vs concurrent encode", flush=True)
        backend = streaming.backend
        ctx = TickContext(
            playhead_s=0.0,
            buffer_duration_s=streaming.state.duration
            or args.loop_duration,
        )
        knobs = backend.read_knobs()
        knobs["denoise"] = 1.0

        def run_ticks(n):
            tick_ms = []
            gens = 0
            t0 = time.perf_counter()
            for _ in range(n):
                t1 = time.perf_counter()
                fresh = backend.produce(knobs, ctx, "generate")
                tick_ms.append((time.perf_counter() - t1) * 1000.0)
                gens += int(fresh)
            wall = time.perf_counter() - t0
            return {
                "gens_per_s": round(gens / wall, 3),
                "ticks_per_s": round(n / wall, 2),
                "tick_ms": _stats(tick_ms),
            }

        for _ in range(2 * (args.steps + args.depth) + 4):  # warm the ring
            backend.produce(knobs, ctx, "generate")

        report["gen_baseline"] = run_ticks(args.measure_ticks)
        print(f"  baseline: gens/s={report['gen_baseline']['gens_per_s']} "
              f"tick_p50={report['gen_baseline']['tick_ms']['p50']}ms",
              flush=True)

        stop = threading.Event()
        enc_ms: list[float] = []
        enc_err: list[str] = []

        def encode_worker():
            # Production node path: TRT encode runs on the VAE module's
            # own stream (separate from the DiT engine's); eager runs on
            # the default stream. Either way this is what a live-input
            # feed thread would actually call.
            try:
                encode_loop_once()  # warm
                torch.cuda.synchronize()
                # No device-wide sync in the loop: the node call is
                # already synchronous (TRT syncs its own stream; eager
                # round-trips the latent through CPU). A full
                # synchronize() here would absorb in-flight DiT ticks
                # into the encode timing.
                while not stop.is_set():
                    t0 = time.perf_counter()
                    encode_loop_once()
                    enc_ms.append((time.perf_counter() - t0) * 1000.0)
            except Exception as exc:  # surface, don't hang the probe
                enc_err.append(f"{type(exc).__name__}: {exc}")

        torch.cuda.reset_peak_memory_stats()
        worker = threading.Thread(target=encode_worker, daemon=True)
        worker.start()
        time.sleep(1.0)  # let the worker reach steady state
        report["gen_with_encode"] = run_ticks(args.concurrent_ticks)
        stop.set()
        worker.join(timeout=60)
        report["vram"].append(_vram("concurrent_peak"))
        if enc_err:
            report["encode_worker_error"] = enc_err[0]
            print(f"  encode worker FAILED: {enc_err[0]}", flush=True)
        report["concurrent_encode_ms"] = _stats(enc_ms) if enc_ms else None
        if enc_ms:
            window = report["gen_with_encode"]["ticks_per_s"]
            report["concurrent_encodes_per_s"] = round(
                len(enc_ms) / (args.concurrent_ticks / window), 2)
        print(f"  with encode: gens/s="
              f"{report['gen_with_encode']['gens_per_s']} "
              f"tick_p50={report['gen_with_encode']['tick_ms']['p50']}ms  "
              f"encode_p50="
              f"{report['concurrent_encode_ms']['p50'] if enc_ms else 'n/a'}"
              f"ms ({len(enc_ms)} encodes)", flush=True)

        # --------------------------------------------------------------
        # Phase 7b: same flat-out contention, but with the per-call
        # torch.cuda.empty_cache() in _trt_vae_encode no-opped, to
        # attribute how much of the slowdown is the allocator flush vs
        # the encode compute itself. Probe-local patch only.
        # --------------------------------------------------------------
        print("[phase7b] flat-out encode, empty_cache no-opped", flush=True)
        real_empty_cache = torch.cuda.empty_cache
        torch.cuda.empty_cache = lambda: None
        try:
            stop = threading.Event()
            enc2_ms: list[float] = []

            def encode_worker2():
                try:
                    encode_loop_once()
                    torch.cuda.synchronize()
                    while not stop.is_set():
                        t0 = time.perf_counter()
                        encode_loop_once()
                        enc2_ms.append((time.perf_counter() - t0) * 1000.0)
                except Exception as exc:
                    enc_err.append(f"no_empty_cache: {type(exc).__name__}: {exc}")

            worker = threading.Thread(target=encode_worker2, daemon=True)
            worker.start()
            time.sleep(1.0)
            report["gen_with_encode_no_empty_cache"] = run_ticks(
                args.concurrent_ticks)
            stop.set()
            worker.join(timeout=60)
        finally:
            torch.cuda.empty_cache = real_empty_cache
        report["concurrent_encode_no_empty_cache_ms"] = (
            _stats(enc2_ms) if enc2_ms else None)
        g = report["gen_with_encode_no_empty_cache"]
        print(f"  no empty_cache: gens/s={g['gens_per_s']} "
              f"tick_p50={g['tick_ms']['p50']}ms  encode_p50="
              f"{report['concurrent_encode_no_empty_cache_ms']['p50'] if enc2_ms else 'n/a'}"
              f"ms ({len(enc2_ms)} encodes)", flush=True)

        # --------------------------------------------------------------
        # Phase 8: paced concurrent encode -- the realistic cadences.
        # Re-encode once per bar and once per beat at the detected BPM
        # and measure what each costs the generation loop.
        # --------------------------------------------------------------
        bar_s = 4.0 * 60.0 / bpm
        report["bar_s"] = round(bar_s, 3)
        report["paced"] = []
        for label, interval in (("per_bar", bar_s), ("per_beat", bar_s / 4)):
            print(f"[phase8] paced encode: {label} (every {interval:.2f}s)",
                  flush=True)
            stop = threading.Event()
            paced_ms: list[float] = []

            def paced_worker():
                try:
                    encode_loop_once()  # warm
                    torch.cuda.synchronize()
                    next_t = time.perf_counter()
                    while not stop.is_set():
                        next_t += interval
                        wait = next_t - time.perf_counter()
                        if wait > 0 and stop.wait(wait):
                            break
                        t0 = time.perf_counter()
                        encode_loop_once()
                        paced_ms.append(
                            (time.perf_counter() - t0) * 1000.0)
                except Exception as exc:
                    enc_err.append(f"{label}: {type(exc).__name__}: {exc}")

            worker = threading.Thread(target=paced_worker, daemon=True)
            worker.start()
            time.sleep(1.0)
            stats = run_ticks(args.concurrent_ticks)
            stop.set()
            worker.join(timeout=60)
            row = {"label": label, "interval_s": round(interval, 3),
                   "gen": stats,
                   "encode_ms": _stats(paced_ms) if paced_ms else None,
                   "n_encodes": len(paced_ms)}
            report["paced"].append(row)
            print(f"  {label}: gens/s={stats['gens_per_s']} "
                  f"tick_p50={stats['tick_ms']['p50']}ms  encode_p50="
                  f"{row['encode_ms']['p50'] if paced_ms else 'n/a'}ms "
                  f"({len(paced_ms)} encodes)", flush=True)

    finally:
        streaming.close()

    out = Path(args.out) if args.out else (
        _REPO_ROOT / "runs" / "realtime-input"
        / (f"encode-dit_{args.accel}-vae_{report['vae_accel_effective']}-"
           f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[report] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
