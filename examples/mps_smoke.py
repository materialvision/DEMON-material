#!/usr/bin/env python3
"""Apple Silicon (MPS) smoke test: full cover flow, timed per stage.

Runs the same pipeline as examples/session_demo.py (prepare_source ->
encode_text -> generate -> decode) on the eager PyTorch backends with
fp32 on MPS, and prints wall time per stage plus a realtime factor.

    uv run python examples/mps_smoke.py
    MPS_SMOKE_DURATION=10 MPS_SMOKE_STEPS=4 uv run python examples/mps_smoke.py
"""

import os
import sys
import time

import soundfile as sf
import torch

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from acestep.constants import TASK_INSTRUCTIONS
from acestep.engine.session import Session
from acestep.fixtures import audio_fixture
from acestep.nodes import Audio

DURATION = float(os.environ.get("MPS_SMOKE_DURATION", "60"))
STEPS = int(os.environ.get("MPS_SMOKE_STEPS", "8"))
OUTPUT_DIR = os.path.join(project_root, "test_output", "mps_smoke")


def load_audio(path: str, duration: float) -> Audio:
    data, sr = sf.read(path, dtype="float32")
    waveform = torch.from_numpy(data.T if data.ndim > 1 else data.reshape(1, -1))
    if sr != 48000:
        import torchaudio
        waveform = torchaudio.transforms.Resample(sr, 48000)(waveform)
    waveform = waveform[:2, : int(duration * 48000)]
    return Audio(waveform=waveform, sample_rate=48000)


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"=== MPS smoke test: duration={DURATION}s steps={STEPS} ===")

    t0 = time.time()
    session = Session(
        device="auto",            # resolves to mps when no CUDA device exists
        decoder_backend="eager",  # only eager works on MPS (no TRT/inductor)
        vae_backend="eager",
        use_flash_attention=False,  # not installed on macOS; force SDPA
    )
    print(f"[1] Session ready in {time.time() - t0:.1f}s "
          f"(device={session.model.handler.device}, dtype={session.model.handler.dtype})")

    t0 = time.time()
    audio = load_audio(str(audio_fixture("inside_confusion_loop_60s_gsm.wav")), DURATION)
    source = session.prepare_source(audio)
    print(f"[2] prepare_source (VAE encode + hint extract) in {time.time() - t0:.1f}s")

    t0 = time.time()
    cond = session.encode_text(
        tags="deep house, warm bass, dreamy pads",
        instruction=TASK_INSTRUCTIONS["cover"],
        refer_latent=source.latent,
        bpm=122,
        duration=DURATION,
        key="A minor",
    )
    print(f"[3] encode_text in {time.time() - t0:.2f}s")

    t0 = time.time()
    latent = session.generate(
        conditioning=cond,
        context_latent=source.context_latent,
        source_latent=source.latent,
        seed=1528,
        steps=STEPS,
        duration=DURATION,
    )
    t_gen = time.time() - t0
    print(f"[4] generate ({STEPS} steps) in {t_gen:.1f}s "
          f"({DURATION / t_gen:.2f}x realtime)")

    t0 = time.time()
    result = session.decode(latent)
    print(f"[5] decode in {time.time() - t0:.1f}s")

    wav = result.waveform
    if wav.dim() == 3:
        wav = wav.squeeze(0)
    out = os.path.join(OUTPUT_DIR, f"cover_{int(DURATION)}s_steps{STEPS}.wav")
    sf.write(out, wav.detach().cpu().float().numpy().T, result.sample_rate)
    rms = wav.pow(2).mean().sqrt().item()
    print(f"[6] Saved {out} | peak={wav.abs().max().item():.4f} rms={rms:.4f}")
    if rms <= 1e-4:
        print("WARNING: output looks silent")
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
