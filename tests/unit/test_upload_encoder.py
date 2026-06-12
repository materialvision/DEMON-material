"""Upload-encoder lifecycle helpers in ws_adapter.

Covers the generation-stack strip (the upload path never runs the DiT
decoder — dropping it is what fits uploads inside a long-profile
session's headroom), the persistent between-uploads offload helper, and
the windowed BPM/key analysis.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import torch.nn as nn

import demos.realtime_motion_graph_web.ws_adapter as wa


# ---------------------------------------------------------------------------
# _strip_upload_encoder_generation_stack
# ---------------------------------------------------------------------------


class _FakeDiffusionEngine:
    def __init__(self, raise_on_close=False):
        self.closed = False
        self._raise = raise_on_close

    def close(self):
        self.closed = True
        if self._raise:
            raise RuntimeError("engine close failed")


def _fake_encoder_session(*, engine=None, decoder=None):
    model = nn.Module()
    model.encoder = nn.Linear(4, 4)
    model.tokenizer = nn.Linear(4, 4)
    if decoder is not None:
        model.decoder = decoder
    handler = SimpleNamespace(_diffusion_engine=engine, model=model)
    return SimpleNamespace(handler=handler), model


def test_strip_drops_decoder_and_closes_engine():
    engine = _FakeDiffusionEngine()
    decoder = nn.Linear(4, 4)  # 4*4 weights + 4 bias = 20 params
    session, model = _fake_encoder_session(engine=engine, decoder=decoder)

    dropped = wa._strip_upload_encoder_generation_stack(session)

    assert dropped == 20
    assert engine.closed is True
    assert session.handler._diffusion_engine is None
    # Decoder replaced with an empty module, not deleted: downstream
    # attribute access stays valid, the parameters are gone.
    assert isinstance(model.decoder, nn.Module)
    assert sum(p.numel() for p in model.decoder.parameters()) == 0
    # The surfaces uploads DO run are untouched.
    assert sum(p.numel() for p in model.encoder.parameters()) == 20


def test_strip_handles_missing_engine_and_decoder():
    session, _ = _fake_encoder_session(engine=None, decoder=None)
    assert wa._strip_upload_encoder_generation_stack(session) == 0


def test_strip_survives_engine_close_failure():
    engine = _FakeDiffusionEngine(raise_on_close=True)
    decoder = nn.Linear(2, 2)
    session, _ = _fake_encoder_session(engine=engine, decoder=decoder)
    dropped = wa._strip_upload_encoder_generation_stack(session)
    assert dropped == 6  # strip completed despite the close failure
    assert session.handler._diffusion_engine is None


# ---------------------------------------------------------------------------
# _offload_upload_encoder
# ---------------------------------------------------------------------------


def test_offload_upload_encoder_accepts_none():
    wa._offload_upload_encoder(None)  # must not raise


def test_offload_upload_encoder_parks_the_handler():
    parked = []
    encoder = SimpleNamespace(
        handler=SimpleNamespace(
            offload_eager_to_cpu=lambda: parked.append(True) or ["model", "vae"],
        ),
    )
    wa._offload_upload_encoder(encoder)
    assert parked == [True]


def test_offload_upload_encoder_swallows_failures():
    encoder = SimpleNamespace(
        handler=SimpleNamespace(
            offload_eager_to_cpu=lambda: (_ for _ in ()).throw(
                RuntimeError("CUDA driver gone"),
            ),
        ),
    )
    wa._offload_upload_encoder(encoder)  # logged, not raised


# ---------------------------------------------------------------------------
# Windowed analysis
# ---------------------------------------------------------------------------


def test_analysis_window_passthrough_for_short_signals():
    sr = 48_000
    mono = np.arange(int(wa.ANALYSIS_WINDOW_S * sr) - 1, dtype=np.float32)
    out = wa._analysis_window(mono, sr)
    assert out is mono  # no copy, no trim


def test_analysis_window_passthrough_at_exact_window_length():
    sr = 48_000
    mono = np.zeros(int(wa.ANALYSIS_WINDOW_S * sr), dtype=np.float32)
    assert wa._analysis_window(mono, sr) is mono


def test_analysis_window_centers_long_signals():
    sr = 1_000  # keep the array small; the math is rate-agnostic
    total = int(wa.ANALYSIS_WINDOW_S * sr) * 3
    mono = np.arange(total, dtype=np.float32)
    out = wa._analysis_window(mono, sr)
    expected_n = int(wa.ANALYSIS_WINDOW_S * sr)
    expected_start = (total - expected_n) // 2
    assert out.shape[-1] == expected_n
    assert out[0] == expected_start  # centered, not head-anchored
    assert out[-1] == expected_start + expected_n - 1


# ---------------------------------------------------------------------------
# Page-cache prewarm helper
# ---------------------------------------------------------------------------


def test_read_files_into_page_cache_counts_bytes(tmp_path):
    a = tmp_path / "a.engine"
    b = tmp_path / "b.engine"
    a.write_bytes(b"x" * 1024)
    b.write_bytes(b"y" * 2048)
    # Duplicate paths are read once.
    total = wa._read_files_into_page_cache([a, b, str(a)])
    assert total == 3072
