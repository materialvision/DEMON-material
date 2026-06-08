"""End-to-end live-input simulation: continuous in + continuous out.

The simplest honest closed loop for the sequencer scenario. Drives the
real streaming pipeline (Session.stream -> tick -> windowed decode)
flat out, splicing each generation into one advancing output WAV so the
result is listenable. Partway through, it simulates a sequencer edit:
re-encode a changed input and splice it into the live source latent
(handle.tick re-reads handle.source.latent every call, so an in-place
mutation propagates with zero integration code), then refresh the
structure/context latent.

Measures the two things the component benches couldn't:
  1. Smoothness: realtime factor with gen + windowed decode + periodic
     re-encode all contending on one GPU, sustained. RT factor =
     audio-advanced-per-gen / per-gen wall (>1 = headroom).
  2. Edit-to-audible latency: watch a fixed latent region; the edit
     emerges when that region's content jumps. Reports ticks and ms
     from edit submission to emergence (the diffusion-pipeline pickup),
     plus the encode+splice cost on top.

Two re-encode modes:
  --mode full     (default) swap whole source via session.encode path
                  (~25ms TRT) -- conservative upper bound, dead simple.
  --mode windowed re-encode a 5s region with vae_encode_fp16_5s_fixed
                  and splice only the changed frames (~4.5ms).

Run:
    .venv/Scripts/python.exe scripts/experiments/realtime_input/e2e_live_input.py
    .venv/Scripts/python.exe scripts/experiments/realtime_input/e2e_live_input.py --mode windowed --depth 4
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = next(
    p for p in (_HERE, *_HERE.parents) if (p / "pyproject.toml").exists()
)
while str(_REPO_ROOT) in sys.path:
    sys.path.remove(str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT))

FIXTURE = "low_fi_Gm_loop_60s_gnm.wav"
PROMPT = "lofi hip hop, mellow, instrumental"
SR = 48000
FRAME = 1920
POOL = FRAME * 5
WIN_FRAMES = 125            # 5s windowed-encode engine
MARGIN_FRAMES = 10
WINDOWED_ENGINE = "vae_encode_fp16_5s_fixed"

# Watch this latent region for the edit to emerge (mid-loop, pool-aligned).
WATCH_F0, WATCH_F1 = 300, 340


def _stats(xs):
    xs = sorted(xs)
    n = len(xs)
    return {"mean": round(sum(xs) / n, 2), "p50": round(xs[n // 2], 2),
            "min": round(xs[0], 2), "max": round(xs[-1], 2), "n": n}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--checkpoint", default="acestep-v15-turbo")
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--denoise", type=float, default=0.7)
    ap.add_argument("--loop-duration", type=float, default=30.0)
    ap.add_argument("--vae-window", type=float, default=1.0,
                    help="windowed decode span (s); 0 = full decode")
    ap.add_argument("--mode", choices=("full", "windowed"), default="full")
    ap.add_argument("--total-ticks", type=int, default=160)
    ap.add_argument("--edit-tick", type=int, default=80)
    ap.add_argument("--reencode-every", type=int, default=0,
                    help="0 = single edit at --edit-tick; N>=1 = re-encode "
                         "the source every N ticks (continuous input; "
                         "N=1 is maximum encode throughput)")
    ap.add_argument("--slice-duration", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=1528)
    ap.add_argument("--out-wav", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import torch
    import numpy as np
    import soundfile as sf

    torch.set_grad_enabled(False)
    torch._dynamo.config.disable = True

    from acestep.constants import TASK_INSTRUCTIONS
    from acestep.engine.session import Session, PreparedSource
    from acestep.nodes.types import Audio, Latent
    from acestep.paths import (
        checkpoints_dir, select_trt_engines, trt_engine_path,
    )
    from acestep.fixtures import audio_fixture

    def load_slice(start_s: float, dur_s: float) -> Audio:
        data, sr = sf.read(str(audio_fixture(FIXTURE)), dtype="float32")
        wf = torch.from_numpy(data.T if data.ndim > 1 else data.reshape(1, -1))
        wf = wf[:2]
        s0 = int(start_s * SR)
        s1 = s0 + int(dur_s * SR)
        wf = wf[:, s0:s1]
        rem = wf.shape[-1] % POOL
        if rem:
            wf = wf[:, :wf.shape[-1] - rem]
        return Audio(waveform=wf, sample_rate=SR)

    # Input A = first 30s. The "edited" input must be the SAME length as
    # the source latent (the fixture is <60s, so a [30,60]s slice would be
    # short and the in-place splice would shape-mismatch). Time-reverse A:
    # guaranteed same length, musically very different -> clear emergence.
    audio_a = load_slice(0.0, args.loop_duration)
    audio_b = Audio(
        waveform=torch.flip(audio_a.waveform, dims=[-1]).contiguous(),
        sample_rate=SR,
    )

    base = select_trt_engines(duration_s=60.0)
    trt_engines = {
        "vae_encode": base["vae_encode"],
        "vae_decode": base["vae_decode"],
        "decoder": str(trt_engine_path("decoder_mixed_refit_b8_60s")),
    }

    def vram(tag: str) -> dict:
        free, total = torch.cuda.mem_get_info()
        snap = {
            "tag": tag,
            "free_gb": round(free / 2**30, 3),
            "total_gb": round(total / 2**30, 3),
            "used_gb": round((total - free) / 2**30, 3),
            "torch_alloc_gb": round(torch.cuda.memory_allocated() / 2**30, 3),
            "torch_reserved_gb": round(torch.cuda.memory_reserved() / 2**30, 3),
            "torch_peak_alloc_gb": round(
                torch.cuda.max_memory_allocated() / 2**30, 3),
        }
        print(f"  [vram:{tag}] used={snap['used_gb']}/{snap['total_gb']}GB "
              f"free={snap['free_gb']}GB torch_reserved="
              f"{snap['torch_reserved_gb']}GB peak_alloc="
              f"{snap['torch_peak_alloc_gb']}GB", flush=True)
        return snap

    print(f"[setup] depth={args.depth} mode={args.mode} "
          f"vae_window={args.vae_window} loop={args.loop_duration}s", flush=True)
    torch.cuda.reset_peak_memory_stats()
    vram_log = [vram("pre_session")]
    session = Session(
        project_root=str(checkpoints_dir()),
        config_path=args.checkpoint,
        decoder_backend="tensorrt",
        vae_backend="tensorrt",
        trt_engines=trt_engines,
        vae_window=args.vae_window,
    )

    report: dict = {
        "checkpoint": args.checkpoint, "depth": args.depth,
        "mode": args.mode, "vae_window": args.vae_window,
        "denoise": args.denoise, "loop_duration_s": args.loop_duration,
    }

    # Windowed-encode helper (mode=windowed): one 5s call via the fixed
    # engine, mean-sampled, spliced into the source latent.
    win_engine_path = None
    if args.mode == "windowed":
        p = trt_engine_path(WINDOWED_ENGINE)
        if not Path(str(p)).exists():
            print(f"[setup] {WINDOWED_ENGINE} missing; falling back to full",
                  flush=True)
            args.mode = "full"
            report["mode"] = "full"
            report.setdefault("notes", []).append(
                f"{WINDOWED_ENGINE} missing, used full re-encode")
        else:
            win_engine_path = str(p)

    try:
        source = session.prepare_source(audio_a)
        T = source.latent.tensor.shape[1]
        n_frames = T
        dtype = source.latent.tensor.dtype
        device = source.latent.tensor.device
        print(f"  source T={T} ({T/25:.1f}s)", flush=True)

        cond = session.encode_text(
            tags=PROMPT, lyrics="[Instrumental]",
            instruction=TASK_INSTRUCTIONS["cover"],
            refer_latent=source.latent, bpm=120, duration=args.loop_duration,
            key="G minor",
        )
        stream = session.stream(
            source=source, conditioning=cond,
            steps=args.steps, shift=3.0, pipeline_depth=args.depth,
        )
        vram_log.append(vram("after_session_and_source"))
        # pipeline builds lazily on first tick; read effective depth then.
        report["effective_depth"] = None

        # Pre-encode the edited input so the edit cost we time is purely
        # encode+splice, not disk/resample.
        from acestep.nodes.vae_nodes import (
            _get_trt_vae, _get_trt_stream, VAEEncodeAudio,
        )

        def windowed_splice_edit() -> float:
            """Re-encode a 5s region of audio_b and splice center frames
            into the live source latent. Returns elapsed ms."""
            entry = _get_trt_vae(win_engine_path, device)
            ctx = entry["context"]
            pgstrm = _get_trt_stream()
            dts = entry["tensor_dtypes"]
            wb = audio_b.waveform
            # center the 5s window on the watched region
            mid = (WATCH_F0 + WATCH_F1) // 2
            w0 = min(max(mid - WIN_FRAMES // 2, 0), n_frames - WIN_FRAMES)
            seg = wb[:, w0 * FRAME:(w0 + WIN_FRAMES) * FRAME].unsqueeze(0)
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            inp = seg.to(device=device,
                         dtype=dts.get("audio", torch.float32)).contiguous()
            ctx.set_input_shape("audio", tuple(inp.shape))
            ctx.set_tensor_address("audio", inp.data_ptr())
            ctx.infer_shapes()
            buf = torch.empty(tuple(ctx.get_tensor_shape("moments")),
                              dtype=dts.get("moments", torch.float32),
                              device=device)
            ctx.set_tensor_address("moments", buf.data_ptr())
            ctx.execute_async_v3(pgstrm.ptr)
            pgstrm.synchronize()
            mean, logvar = buf.float().chunk(2, dim=1)
            lat = (mean + torch.exp(0.5 * logvar)
                   * torch.randn_like(mean)).transpose(1, 2).to(dtype)  # [1,W,D]
            off = WATCH_F0 - w0
            k = WATCH_F1 - WATCH_F0
            src = stream.source.latent.tensor
            src[:, WATCH_F0:WATCH_F1, :] = lat[:, off:off + k, :]
            # structure ref derives from the latent -> refresh it
            stream.context_latent = session.extract_hints(
                stream.source.latent)
            torch.cuda.synchronize()
            return (time.perf_counter() - t0) * 1000.0

        def full_swap_edit() -> float:
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            new_lat = session.encode_audio(audio_b)         # ~25ms TRT
            new_ctx = session.extract_hints(new_lat)        # ~5ms
            stream.source.latent.tensor[:] = new_lat.tensor
            stream.context_latent = new_ctx
            torch.cuda.synchronize()
            return (time.perf_counter() - t0) * 1000.0

        # ----------------------------------------------------------------
        # Continuous loop.
        # ----------------------------------------------------------------
        slice_samples = int(args.slice_duration * SR)
        playback_start = 5.0
        play_off = int(playback_start * SR)
        out_chunks = []
        tick_ms, dec_ms, enc_ms = [], [], []
        wall_ms = []            # encode(if any) + tick + decode, per gen
        edit_ms = None
        edit_submit_tick = None
        watch_ref = None        # emerged watched-region at edit time
        emerge_tick = None
        num_done = 0
        continuous = args.reencode_every > 0
        min_free_gb = float("inf")   # tightest free-memory point during run
        encode_loaded_snap = None

        def do_encode() -> float:
            return (windowed_splice_edit() if args.mode == "windowed"
                    else full_swap_edit())

        if continuous:
            print(f"[run] {args.total_ticks} ticks, CONTINUOUS re-encode "
                  f"every {args.reencode_every} tick(s) ({args.mode})",
                  flush=True)
        else:
            print(f"[run] {args.total_ticks} ticks, single edit at tick "
                  f"{args.edit_tick} ({args.mode})", flush=True)

        for tick in range(args.total_ticks):
            this_enc = 0.0
            # Simulate live input: continuous cadence, or one edit.
            if continuous:
                if tick > 0 and tick % args.reencode_every == 0:
                    this_enc = do_encode()
                    enc_ms.append(this_enc)
                    if encode_loaded_snap is None:
                        # First encode loads the encode-engine context
                        # alongside the resident decoder/decode contexts:
                        # the VRAM-contention moment.
                        encode_loaded_snap = vram("encode_engine_loaded")
            elif tick == args.edit_tick:
                this_enc = edit_ms = do_encode()
                edit_submit_tick = tick
                encode_loaded_snap = vram("encode_engine_loaded")
                print(f"  >> EDIT at tick {tick}: re-encode+splice "
                      f"({args.mode}) {this_enc:.1f}ms", flush=True)

            torch.cuda.synchronize()
            t0 = time.perf_counter()
            result = stream.tick(denoise=args.denoise, seed=args.seed)
            torch.cuda.synchronize()
            tms = (time.perf_counter() - t0) * 1000.0

            if result is None:
                tick_ms.append(tms)
                continue
            tick_ms.append(tms)
            wall_ms.append(this_enc + tms)  # decode added below
            lat_t = result.tensor

            # Edit-emergence detection on the watched region.
            region = lat_t[:, WATCH_F0:WATCH_F1, :].float()
            if edit_submit_tick is not None and watch_ref is None:
                watch_ref = region.clone()  # baseline at first emerge post-edit
            elif edit_submit_tick is not None and emerge_tick is None:
                mse = (region - watch_ref).pow(2).mean().item()
                if mse > 0.05:  # well above sampling noise
                    emerge_tick = tick

            # Windowed decode + splice into the advancing output.
            start = play_off + num_done * slice_samples
            d0 = time.perf_counter()
            if args.vae_window > 0:
                audio_out = session.decode(result, t_start=start / SR)
                wav = audio_out.waveform.detach().cpu().float().squeeze(0)
                win0 = audio_out.start_sample
            else:
                audio_out = session.decode(result)
                wav = audio_out.waveform.detach().cpu().float().squeeze(0)
                win0 = 0
            torch.cuda.synchronize()
            d = (time.perf_counter() - d0) * 1000.0
            dec_ms.append(d)
            wall_ms[-1] += d

            ls = start - win0
            le = ls + slice_samples
            if 0 <= ls and le <= wav.shape[1]:
                out_chunks.append(wav[:, ls:le])
            else:
                chunk = torch.zeros(wav.shape[0], slice_samples)
                avail = min(slice_samples, wav.shape[1] - max(ls, 0))
                if avail > 0 and ls >= 0:
                    chunk[:, :avail] = wav[:, ls:ls + avail]
                out_chunks.append(chunk)
            num_done += 1

            free_now = torch.cuda.mem_get_info()[0] / 2**30
            if free_now < min_free_gb:
                min_free_gb = free_now
            if report["effective_depth"] is None:
                try:
                    report["effective_depth"] = stream.pipeline._depth
                except Exception:
                    pass

            if num_done % 20 == 0:
                print(f"  #{num_done:3d} tick={tms:5.1f}ms "
                      f"dec={dec_ms[-1]:5.1f}ms free={free_now:.2f}GB "
                      f"(play {start/SR:.1f}s)", flush=True)

        # ----------------------------------------------------------------
        # Accounting.
        # ----------------------------------------------------------------
        report["tick_ms"] = _stats(tick_ms)
        report["decode_ms"] = _stats(dec_ms)
        report["encode_ms"] = _stats(enc_ms) if enc_ms else None
        # per-gen wall = encode(when it fired) + tick + decode. In
        # continuous N=1 mode every gen carries an encode; the p50 is the
        # honest "can we keep up" number.
        report["per_gen_ms"] = _stats(wall_ms)
        report["reencode_every"] = args.reencode_every
        # Each generation advances playback by slice_duration of audio.
        report["realtime_factor"] = round(
            args.slice_duration * 1000.0 / report["per_gen_ms"]["p50"], 2)
        report["realtime_factor_worst"] = round(
            args.slice_duration * 1000.0 / report["per_gen_ms"]["max"], 2)
        report["edit_encode_splice_ms"] = round(edit_ms, 1) if edit_ms else None

        # VRAM / fit accounting.
        end_snap = vram("end_of_run")
        vram_log.append(end_snap)
        if encode_loaded_snap is not None:
            vram_log.append(encode_loaded_snap)
        report["vram"] = vram_log
        report["vram_peak_torch_alloc_gb"] = round(
            torch.cuda.max_memory_allocated() / 2**30, 3)
        report["vram_min_free_gb"] = round(min_free_gb, 3)
        report["vram_total_gb"] = end_snap["total_gb"]
        # "Fits without contention" = the tightest free-memory point stays
        # comfortably positive AND torch's reserved pool never had to be
        # released/regrown mid-run (no allocator thrash). We approximate
        # the margin; a negative/near-zero floor would mean contention.
        report["fits"] = bool(min_free_gb > 1.0)  # >1GB headroom at floor
        if emerge_tick is not None:
            ticks_to_emerge = emerge_tick - edit_submit_tick
            report["edit_emergence"] = {
                "edit_tick": edit_submit_tick,
                "emerge_tick": emerge_tick,
                "ticks": ticks_to_emerge,
                "pipeline_ms": round(ticks_to_emerge * report["tick_ms"]["p50"], 1),
                "total_ms": round(
                    (edit_ms or 0) + ticks_to_emerge * report["tick_ms"]["p50"], 1),
            }
        else:
            report["edit_emergence"] = None

        enc_p50 = report["encode_ms"]["p50"] if report["encode_ms"] else 0.0
        print(f"\n[result] per-gen p50={report['per_gen_ms']['p50']}ms "
              f"(enc {enc_p50} + tick {report['tick_ms']['p50']} + "
              f"dec {report['decode_ms']['p50']})  "
              f"realtime_factor={report['realtime_factor']}x "
              f"(worst {report['realtime_factor_worst']}x)", flush=True)
        print(f"[vram] depth={report['effective_depth'] or args.depth} "
              f"peak_torch_alloc={report['vram_peak_torch_alloc_gb']}GB  "
              f"min_free={report['vram_min_free_gb']}GB / "
              f"{report['vram_total_gb']}GB  "
              f"FITS={report['fits']}", flush=True)
        if report["edit_emergence"]:
            e = report["edit_emergence"]
            print(f"[result] edit->audible: encode+splice "
                  f"{report['edit_encode_splice_ms']}ms + pipeline "
                  f"{e['pipeline_ms']}ms ({e['ticks']} ticks) = "
                  f"{e['total_ms']}ms", flush=True)
        else:
            print("[result] edit never detected in watched region "
                  "(check denoise/region)", flush=True)

        # Save the listenable output.
        out_wav = (Path(args.out_wav) if args.out_wav else
                   _REPO_ROOT / "runs" / "realtime-input"
                   / f"e2e-{args.mode}-d{args.depth}-"
                   f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.wav")
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        full = torch.cat(out_chunks, dim=1)
        sf.write(str(out_wav), full.numpy().T, SR, format="WAV")
        report["out_wav"] = str(out_wav)
        report["out_duration_s"] = round(full.shape[1] / SR, 1)
        print(f"[save] {out_wav} ({report['out_duration_s']}s, edit audible "
              f"at {(play_off + (emerge_tick or 0) * slice_samples)/SR:.1f}s)"
              if emerge_tick else f"[save] {out_wav}", flush=True)
    finally:
        try:
            stream.close()
        except Exception:
            pass
        session.close()

    out = (Path(args.out) if args.out else
           _REPO_ROOT / "runs" / "realtime-input"
           / f"e2e-{args.mode}-d{args.depth}-"
           f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[report] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
