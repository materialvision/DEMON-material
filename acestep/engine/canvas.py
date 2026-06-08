"""Server-side audio canvas backing real-time input ("play into the model").

The canvas is the session's sample-exact mirror of the audio its source
latent was encoded from. It exists because the source latent alone is
not enough to support live edits:

* **Context.** Re-encoding any span needs the encoder's receptive-field
  context around it. With the mirror resident, clients ship ONLY the
  changed audio (a bar, a chunk) and the server pulls true context from
  the mirror — no client-computed margins, no buffer-coverage errors,
  and edits whose neighbors were themselves just written get encoded
  against the real post-edit surroundings by construction.
* **Musical time.** The latent grid is 25 fps; an arbitrary bar length
  (e.g. 124 BPM → 1.93533 s) is not representable as a whole number of
  latent frames, so latent-domain tiling drifts. The mirror tiles in
  the AUDIO domain at 48 kHz, where any bar is sample-exact, then
  re-encodes — no period quantization, no replicated receptive-field
  artifacts at tile junctions.
* **Overdub.** ``mix="sum"`` layers new audio over what's there, which
  has no latent-domain equivalent.

Declicking is done here, in the audio domain, where the discontinuity
actually lives: a short crossfade where written audio meets pre-existing
audio. No latent-domain crossfade is applied at commit: window encodes
read true mirror context, and the VAE's sampling noise is i.i.d. per
element (adjacent frames inside ONE encode already carry independent
draws), so a frame boundary between an old and a new encode is no more
discontinuous than any interior boundary.

Torch-only (no engines, no model): the encode is injected as a callable
so this module is unit-testable on CPU.
"""

from __future__ import annotations

from typing import Callable

import torch
from loguru import logger

SAMPLE_RATE = 48000
FRAME_RATE = 25
SAMPLES_PER_FRAME = SAMPLE_RATE // FRAME_RATE  # 1920
# The vae_encode graph builds latents in 5-frame groups; encode windows
# must START on this grid to reproduce the full-context pooling
# alignment (every validated windowed-encode parity run used 5-frame-
# aligned window starts).
POOL_FRAMES = 5

# Declick crossfade where written audio meets pre-existing canvas audio
# (replace mode only; both edges, interior to the canvas).
EDGE_FADE_SAMPLES = 240  # 5 ms @ 48 kHz

MIX_MODES = ("replace", "sum")
REPEAT_MODES = ("none", "fill")


class SourceCanvas:
    """Sample-exact audio mirror of a session's source latent.

    ``waveform`` is ``[C<=2, S]`` float32 (any device); it is trimmed to
    pool alignment (the same trim the session applies before encoding,
    so the mirror and the latent cover identical samples). All writes
    mutate the mirror in place; the caller re-encodes dirty spans via
    :meth:`stage_frames` and commits them into the live latent.

    Thread model: writes are NOT internally locked. The owning session
    serializes writes (single dispatch thread) and guards mirror
    replacement (source swap) with its own lock + epoch.
    """

    def __init__(self, waveform: torch.Tensor):
        if waveform.dim() != 2:
            raise ValueError(
                f"canvas waveform must be [C, S]; got {tuple(waveform.shape)}"
            )
        wf = waveform[:2].to(torch.float32)
        pool_samples = SAMPLES_PER_FRAME * POOL_FRAMES
        s = wf.shape[-1] - (wf.shape[-1] % pool_samples)
        if s <= 0:
            raise ValueError(
                f"canvas waveform too short: {wf.shape[-1]} samples "
                f"(< one {pool_samples}-sample pool group)"
            )
        self.wf = wf[:, :s].contiguous()

    @property
    def samples(self) -> int:
        return int(self.wf.shape[-1])

    @property
    def frames(self) -> int:
        return self.samples // SAMPLES_PER_FRAME

    @property
    def duration_s(self) -> float:
        return self.samples / SAMPLE_RATE

    # ---- Writes ----------------------------------------------------------

    def write(
        self,
        waveform: torch.Tensor,
        *,
        at_s: float = 0.0,
        mix: str = "replace",
        repeat: str = "none",
    ) -> tuple[int, int]:
        """Write ``waveform`` onto the mirror at time ``at_s``.

        * ``mix="replace"`` overwrites (with a 5 ms declick crossfade
          where the new audio meets pre-existing audio); ``mix="sum"``
          adds on top (overdub), clamped to [-1, 1].
        * ``repeat="none"`` writes once; ``repeat="fill"`` treats the
          buffer as one period of a loop and lays it across the WHOLE
          canvas, phase-anchored so the period starts at ``at_s``. The
          tiling is sample-exact (audio domain) — any period length
          works, not just multiples of the latent frame.

        Returns the dirty latent-frame span ``[f0, f1)`` the caller must
        re-encode. Audio past the canvas end is trimmed (logged), never
        wrapped: the canvas is a finite song, not a ring.
        """
        if mix not in MIX_MODES:
            raise ValueError(f"mix must be one of {MIX_MODES}; got {mix!r}")
        if repeat not in REPEAT_MODES:
            raise ValueError(
                f"repeat must be one of {REPEAT_MODES}; got {repeat!r}"
            )
        if waveform.dim() != 2:
            raise ValueError(
                f"write waveform must be [C, S]; got {tuple(waveform.shape)}"
            )
        if at_s < 0:
            raise ValueError(f"at_s must be >= 0; got {at_s}")
        a = int(round(at_s * SAMPLE_RATE))
        if a >= self.samples:
            raise ValueError(
                f"at_s={at_s:.3f}s is past the canvas end "
                f"({self.duration_s:.3f}s)"
            )
        unit = waveform[:2].to(self.wf.device, torch.float32)
        if unit.shape[0] == 1:
            unit = unit.expand(self.wf.shape[0], -1)
        n = int(unit.shape[-1])
        if n <= 0:
            raise ValueError("write waveform is empty")

        if repeat == "fill":
            # Build the full tiled track in the audio domain, phase-
            # anchored at ``a``, then lay it down as one canvas-wide
            # write (so there are no per-copy seams and no edges left
            # to declick).
            reps = -(-(self.samples + a) // n) + 1
            tiled = unit.repeat(1, reps)
            start = (-a) % n
            self._splice(tiled[:, start:start + self.samples], 0, mix)
            return (0, self.frames)

        b = min(a + n, self.samples)
        if a + n > self.samples:
            logger.warning(
                "canvas_write_trimmed at_s={:.3f} dropped_s={:.3f}",
                at_s, (a + n - self.samples) / SAMPLE_RATE,
            )
        self._splice(unit[:, :b - a], a, mix)
        f0 = a // SAMPLES_PER_FRAME
        f1 = min(self.frames, -(-b // SAMPLES_PER_FRAME))  # ceil
        return (f0, f1)

    def _splice(self, unit: torch.Tensor, a: int, mix: str) -> None:
        """Land ``unit`` at sample ``a`` (already trimmed to fit)."""
        b = a + int(unit.shape[-1])
        if mix == "sum":
            self.wf[:, a:b] = (self.wf[:, a:b] + unit).clamp_(-1.0, 1.0)
            return
        fade = min(EDGE_FADE_SAMPLES, (b - a) // 2)
        head = tail = None
        if fade > 0:
            ramp = torch.linspace(
                0.0, 1.0, fade, device=self.wf.device, dtype=self.wf.dtype,
            ).unsqueeze(0)
            if a > 0:
                head = self.wf[:, a:a + fade] * (1 - ramp) + unit[:, :fade] * ramp
            if b < self.samples:
                tail = unit[:, -fade:] * (1 - ramp) + self.wf[:, b - fade:b] * ramp
        self.wf[:, a:b] = unit
        if head is not None:
            self.wf[:, a:a + fade] = head
        if tail is not None:
            self.wf[:, b - fade:b] = tail

    # ---- Staged re-encode ------------------------------------------------

    def stage_frames(
        self,
        f0: int,
        f1: int,
        encode_window_fn: Callable[[torch.Tensor], torch.Tensor],
        *,
        win_frames: int,
        margin_frames: int,
    ) -> torch.Tensor:
        """Re-encode latent frames ``[f0, f1)`` from the mirror.

        Tiles fixed ``win_frames`` encode windows across the span, each
        keeping its center and discarding ``margin_frames`` of
        receptive-field margin per side; every window reads true context
        from the mirror, clamped only at the canvas's hard edges (where
        boundary-clamped windows are exact by construction — the song
        genuinely starts/ends there). Window starts are kept on the
        5-frame pool grid so the windowed encode reproduces the
        full-context pooling alignment.

        ``encode_window_fn`` maps a ``[1, C, win_frames*1920]`` audio
        segment to ``[1, D, win_frames]`` latents. Returns the staged
        block ``[1, D, f1-f0]`` — nothing is written anywhere; the
        caller commits it into the live latent under its own lock.
        """
        frames = self.frames
        if not (0 <= f0 < f1 <= frames):
            raise ValueError(
                f"stage span [{f0},{f1}) out of bounds for {frames} frames"
            )
        if frames < win_frames:
            raise ValueError(
                f"canvas ({frames} frames) shorter than the encode window "
                f"({win_frames} frames); use a whole-canvas encode instead"
            )
        last_w0 = frames - win_frames  # pool-aligned: both terms are
        block: torch.Tensor | None = None
        k0 = f0
        while k0 < f1:
            w0 = min(max(k0 - margin_frames, 0), last_w0)
            w0 = (w0 // POOL_FRAMES) * POOL_FRAMES
            # Keep through the window's trailing margin only at the
            # canvas's last window, where no further context exists.
            k1 = f1 if w0 >= last_w0 else min(f1, w0 + win_frames - margin_frames)
            seg = self.wf[
                :, w0 * SAMPLES_PER_FRAME:(w0 + win_frames) * SAMPLES_PER_FRAME
            ].unsqueeze(0)
            lat = encode_window_fn(seg)  # [1, D, win_frames]
            piece = lat[:, :, k0 - w0:k1 - w0]
            if block is None:
                block = torch.empty(
                    (piece.shape[0], piece.shape[1], f1 - f0),
                    device=piece.device, dtype=piece.dtype,
                )
            block[:, :, k0 - f0:k1 - f0] = piece
            k0 = k1
        return block
