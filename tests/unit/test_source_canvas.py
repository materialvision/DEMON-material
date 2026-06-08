"""Unit tests for the real-time-input audio canvas.

``acestep.engine.canvas.SourceCanvas`` is deliberately torch-only (the
encode is injected), so the write/tile/mix sample math and the staged
window tiling run here on CPU with a fake encoder — no GPU, no TRT, no
model.
"""

import pytest
import torch

from acestep.engine.canvas import (
    EDGE_FADE_SAMPLES,
    POOL_FRAMES,
    SAMPLE_RATE,
    SAMPLES_PER_FRAME,
    SourceCanvas,
)

POOL_SAMPLES = SAMPLES_PER_FRAME * POOL_FRAMES

# Stage-window geometry mirroring the production constants
# (acestep.paths.WINDOWED_VAE_ENCODE_*): 125-frame window, 10-frame
# receptive-field margin.
WIN = 125
MARGIN = 10


def _canvas(pools: int = 30) -> SourceCanvas:
    """A small deterministic canvas: 30 pools = 6 s = 150 frames."""
    n = pools * POOL_SAMPLES
    wf = torch.arange(n, dtype=torch.float32).remainder(997) / 997.0
    return SourceCanvas(torch.stack([wf, -wf]))


# ---- construction ---------------------------------------------------------


def test_init_trims_to_pool_alignment():
    wf = torch.zeros(2, 3 * POOL_SAMPLES + 1234)
    c = SourceCanvas(wf)
    assert c.samples == 3 * POOL_SAMPLES
    assert c.frames == 3 * POOL_FRAMES
    assert c.frames * SAMPLES_PER_FRAME == c.samples


def test_init_rejects_too_short_and_bad_rank():
    with pytest.raises(ValueError):
        SourceCanvas(torch.zeros(2, POOL_SAMPLES - 1))
    with pytest.raises(ValueError):
        SourceCanvas(torch.zeros(2, 3, POOL_SAMPLES))


# ---- write: placement / spans ---------------------------------------------


def test_write_replace_is_sample_exact_and_reports_dirty_frames():
    c = _canvas()
    before = c.wf.clone()
    n = 2 * SAMPLES_PER_FRAME + 7          # deliberately not frame-aligned
    a = 3 * SAMPLES_PER_FRAME + 1234       # deliberately mid-frame
    unit = torch.ones(2, n) * 0.5

    f0, f1 = c.write(unit, at_s=a / SAMPLE_RATE)

    assert f0 == a // SAMPLES_PER_FRAME
    assert f1 == -(-(a + n) // SAMPLES_PER_FRAME)
    # Interior of the write (past the declick fades) is exactly the unit.
    interior = c.wf[:, a + EDGE_FADE_SAMPLES:a + n - EDGE_FADE_SAMPLES]
    assert torch.equal(interior, unit[:, EDGE_FADE_SAMPLES:n - EDGE_FADE_SAMPLES])
    # Outside the write, untouched.
    assert torch.equal(c.wf[:, :a], before[:, :a])
    assert torch.equal(c.wf[:, a + n:], before[:, a + n:])


def test_write_replace_declick_blends_edges():
    c = _canvas()
    old_head = c.wf[:, 10000:10000 + EDGE_FADE_SAMPLES].clone()
    unit = torch.full((2, 48000), 0.25)
    c.write(unit, at_s=10000 / SAMPLE_RATE)
    # First written sample is (almost) the old audio, the fade end is
    # (almost) the new audio.
    assert torch.allclose(c.wf[:, 10000], old_head[:, 0], atol=1e-6)
    ramp_end = c.wf[:, 10000 + EDGE_FADE_SAMPLES - 1]
    assert torch.allclose(
        ramp_end, torch.full((2,), 0.25), atol=2 * float(old_head.abs().max()) / EDGE_FADE_SAMPLES + 1e-3,
    )


def test_write_at_canvas_start_and_end_skips_outer_fades():
    c = _canvas()
    unit = torch.full((2, c.samples), 0.125)
    c.write(unit, at_s=0.0)
    # Spans the whole canvas: no pre-existing neighbor on either side,
    # so the write is exact everywhere.
    assert torch.equal(c.wf, unit)


def test_write_sum_overdubs_and_clamps():
    c = _canvas()
    base = c.wf[:, 5000:5000 + 1000].clone()
    unit = torch.full((2, 1000), 0.9)
    c.write(unit, at_s=5000 / SAMPLE_RATE, mix="sum")
    assert torch.equal(
        c.wf[:, 5000:6000], (base + unit).clamp(-1.0, 1.0),
    )


def test_write_past_end_is_trimmed_never_wrapped():
    c = _canvas()
    a = c.samples - 1000
    unit = torch.full((2, 5000), 0.5)
    f0, f1 = c.write(unit, at_s=a / SAMPLE_RATE)
    assert f1 == c.frames
    # The trimmed tail landed; nothing wrapped to the canvas head.
    assert torch.equal(
        c.wf[:, a + EDGE_FADE_SAMPLES:],
        unit[:, EDGE_FADE_SAMPLES:1000],
    )
    assert not torch.allclose(c.wf[:, :100], torch.full((2, 100), 0.5))


def test_write_rejects_bad_args():
    c = _canvas()
    unit = torch.zeros(2, 1000)
    with pytest.raises(ValueError):
        c.write(unit, at_s=-0.1)
    with pytest.raises(ValueError):
        c.write(unit, at_s=c.duration_s + 1.0)
    with pytest.raises(ValueError):
        c.write(unit, mix="multiply")
    with pytest.raises(ValueError):
        c.write(unit, repeat="forever")
    with pytest.raises(ValueError):
        c.write(torch.zeros(2, 0))
    with pytest.raises(ValueError):
        c.write(torch.zeros(2, 3, 5))


# ---- write: fill (audio-domain tiling) -------------------------------------


def test_fill_tiles_sample_exactly_at_awkward_period():
    """The case latent-domain tiling cannot represent: a 124 BPM,
    16-step bar (92,896 samples = 1.93533 s) is not a whole number of
    latent frames, but audio-domain tiling lays it down sample-exactly
    with zero period drift across the canvas."""
    c = _canvas()
    n = 92896
    unit = (torch.arange(n, dtype=torch.float32).remainder(101) / 101.0 - 0.5)
    unit = torch.stack([unit, unit * 0.5])

    f0, f1 = c.write(unit, at_s=0.0, repeat="fill")

    assert (f0, f1) == (0, c.frames)
    idx = torch.arange(c.samples) % n
    assert torch.equal(c.wf, unit[:, idx])


def test_fill_phase_anchors_at_at_s():
    c = _canvas()
    n = 48000 + 7
    unit = torch.stack([
        torch.arange(n, dtype=torch.float32) / n,
        torch.zeros(n),
    ])
    a = 70000
    c.write(unit, at_s=a / SAMPLE_RATE, repeat="fill")
    # unit[0] lands at every a + k*n; positions before a wrap backward.
    idx = (torch.arange(c.samples) - a) % n
    assert torch.equal(c.wf, unit[:, idx])


def test_fill_sum_overdubs_the_tiled_track():
    c = _canvas()
    before = c.wf.clone()
    n = 12345
    unit = torch.full((2, n), 0.25)
    c.write(unit, at_s=0.0, repeat="fill", mix="sum")
    assert torch.equal(c.wf, (before + 0.25).clamp(-1.0, 1.0))


# ---- stage_frames: window tiling -------------------------------------------


class _FakeEncoder:
    """Encodes a window into 'latents' that carry the absolute frame
    index, so block assembly is verifiable; records window starts."""

    def __init__(self, samples: int):
        self.samples = samples
        self.window_starts: list = []

    def __call__(self, seg: torch.Tensor) -> torch.Tensor:
        assert seg.dim() == 3 and seg.shape[0] == 1
        n_frames = seg.shape[-1] // SAMPLES_PER_FRAME
        assert n_frames == WIN, f"window must be {WIN} frames, got {n_frames}"
        # Recover the absolute window start from the segment content
        # (the canvas fixture is strictly increasing by construction in
        # this test's canvas; see test usage).
        w0 = int(round(float(seg[0, 0, 0]) * self.samples)) // SAMPLES_PER_FRAME
        self.window_starts.append(w0)
        frames = torch.arange(w0, w0 + WIN, dtype=torch.float32)
        return frames.expand(1, 3, WIN).clone()


def _ramp_canvas(pools: int = 40) -> SourceCanvas:
    """Canvas whose sample i holds i/N — lets the fake encoder recover
    absolute positions from content alone."""
    n = pools * POOL_SAMPLES
    ramp = torch.arange(n, dtype=torch.float32) / n
    return SourceCanvas(torch.stack([ramp, ramp]))


@pytest.mark.parametrize("span", [
    (0, 5),                  # head of canvas (window clamps to 0)
    (37, 61),                # interior, unaligned span
    (60, 175),               # spans more than one keep region
    (190, 200),              # tail of canvas (window clamps to last)
    (0, 200),                # whole canvas
])
def test_stage_frames_assembles_absolute_frames(span):
    c = _ramp_canvas(40)  # 200 frames
    f0, f1 = span
    enc = _FakeEncoder(c.samples)
    block = c.stage_frames(
        f0, f1, enc, win_frames=WIN, margin_frames=MARGIN,
    )
    assert block.shape == (1, 3, f1 - f0)
    # Every staged frame is exactly the absolute frame it claims to be:
    # piece offsets, window clamping, and assembly are all correct.
    expected = torch.arange(f0, f1, dtype=torch.float32).expand(1, 3, -1)
    assert torch.equal(block, expected)
    for w0 in enc.window_starts:
        assert w0 % POOL_FRAMES == 0, "window starts must stay pool-aligned"
        assert 0 <= w0 <= c.frames - WIN


def test_stage_frames_keeps_margin_except_at_canvas_edges():
    c = _ramp_canvas(40)
    enc = _FakeEncoder(c.samples)
    c.stage_frames(50, 160, enc, win_frames=WIN, margin_frames=MARGIN)
    # Interior windows: the kept region starts at least MARGIN (minus
    # pool rounding) into the window.
    for w0 in enc.window_starts:
        if 0 < w0 < c.frames - WIN:
            assert 50 - w0 >= MARGIN - (POOL_FRAMES - 1) or w0 == 0


def test_stage_frames_validates_span_and_canvas_size():
    c = _ramp_canvas(40)
    enc = _FakeEncoder(c.samples)
    with pytest.raises(ValueError):
        c.stage_frames(10, 10, enc, win_frames=WIN, margin_frames=MARGIN)
    with pytest.raises(ValueError):
        c.stage_frames(0, c.frames + 1, enc, win_frames=WIN, margin_frames=MARGIN)
    small = SourceCanvas(torch.zeros(2, 20 * POOL_SAMPLES))  # 100 frames < WIN
    with pytest.raises(ValueError):
        small.stage_frames(0, 5, enc, win_frames=WIN, margin_frames=MARGIN)
