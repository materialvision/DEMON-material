"""VAE *encoder* receptive-field probe (minimal, fast).

Answers one question: how many latent frames near a window edge differ
from encoding the same audio in full context? That bounds the margin a
windowed re-encode + splice needs. (The decoder's RF was measured at
8 frames / 320ms; the encoder's was never measured.)

Method: eager VAE (near-deterministic, sampling floor ~3e-4 rms).
Encode the full 30s loop twice (floor reference), then encode a center
window [t0, t1] with symmetric margin m, compare the window's center
frames against the same frames of the full encode, sweeping m. The
m=0 per-frame profile localizes the corrupted edge frames directly.

Run:
    .venv/Scripts/python.exe scripts/experiments/realtime_input/encoder_receptive_field.py
"""

from __future__ import annotations

import argparse
import json
import sys
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
FRAME = 1920  # samples per latent frame (25 fps)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--checkpoint", default="acestep-v15-turbo")
    ap.add_argument("--loop-duration", type=float, default=30.0)
    ap.add_argument("--win-start", type=float, default=12.0)
    ap.add_argument("--win-end", type=float, default=16.8)
    ap.add_argument("--margins", default="0,0.2,0.4,0.6,0.8,1.2,1.6,2.4,3.2")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import torch

    from acestep.engine.session import Session
    from acestep.paths import checkpoints_dir
    from acestep.streaming.source import _load_known_fixture_waveform

    wf = _load_known_fixture_waveform(FIXTURE)  # [<=2, N] f32 @ 48k
    n = int(SR * args.loop_duration)
    n -= n % FRAME
    loop = wf[:, :n].unsqueeze(0)  # [1, C, S]

    print(f"[setup] eager session {args.checkpoint} ...", flush=True)
    session = Session(
        project_root=str(checkpoints_dir()),
        config_path=args.checkpoint,
    )
    try:
        handler = session.handler

        def encode(w3: torch.Tensor) -> torch.Tensor:
            with torch.no_grad():
                return handler._encode_audio_to_latents(w3).float()  # [1,T,D]

        def frame_rms(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
            # per-frame rms diff over latent dim -> [T]
            return (a - b).pow(2).mean(dim=-1).sqrt().squeeze(0)

        full_a = encode(loop)
        full_b = encode(loop)
        floor = frame_rms(full_a, full_b)
        sig_rms = full_a.pow(2).mean().sqrt().item()
        print(f"[floor] latent_rms={sig_rms:.4f}  sampling floor per-frame "
              f"rms: mean={floor.mean().item():.5f} "
              f"max={floor.max().item():.5f}", flush=True)

        t0f = int(args.win_start * 25)
        t1f = int(args.win_end * 25)
        ref = full_a[:, t0f:t1f]  # [1, W, D]
        rows = []
        m0_profile = None
        for m in (float(x) for x in args.margins.split(",")):
            mf = int(round(m * 25))
            s0 = (t0f - mf) * FRAME
            s1 = (t1f + mf) * FRAME
            assert s0 >= 0 and s1 <= n, f"margin {m}s exceeds the loop"
            lat = encode(loop[:, :, s0:s1])
            center = lat[:, mf:mf + (t1f - t0f)]
            prof = frame_rms(center, ref)  # [W]
            row = {
                "margin_s": m,
                "margin_frames": mf,
                "rms_mean": round(prof.mean().item(), 5),
                "rms_max": round(prof.max().item(), 5),
                "rms_first_frame": round(prof[0].item(), 5),
                "rms_last_frame": round(prof[-1].item(), 5),
                "cos_min_frame": round(
                    torch.nn.functional.cosine_similarity(
                        center, ref, dim=-1).min().item(), 6),
            }
            rows.append(row)
            print(f"  m={m:>4.1f}s ({mf:>3d} fr)  rms mean={row['rms_mean']:.5f} "
                  f"max={row['rms_max']:.5f}  edge first/last="
                  f"{row['rms_first_frame']:.5f}/{row['rms_last_frame']:.5f}  "
                  f"cos_min={row['cos_min_frame']}", flush=True)
            if mf == 0:
                k = 16
                m0_profile = {
                    "first_frames_rms": [round(v, 5)
                                         for v in prof[:k].tolist()],
                    "last_frames_rms": [round(v, 5)
                                        for v in prof[-k:].tolist()],
                }
                print(f"    m=0 edge profile (rms, frames 0..{k-1}): "
                      f"{m0_profile['first_frames_rms']}", flush=True)
                print(f"    m=0 edge profile (rms, last {k}):        "
                      f"{m0_profile['last_frames_rms']}", flush=True)

        report = {
            "checkpoint": args.checkpoint,
            "loop_duration_s": args.loop_duration,
            "window_s": [args.win_start, args.win_end],
            "latent_rms": round(sig_rms, 5),
            "floor_rms_mean": round(floor.mean().item(), 6),
            "floor_rms_max": round(floor.max().item(), 6),
            "margins": rows,
            "m0_edge_profile": m0_profile,
        }
        out = Path(args.out) if args.out else (
            _REPO_ROOT / "runs" / "realtime-input"
            / f"encoder-rf-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[report] {out}")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
