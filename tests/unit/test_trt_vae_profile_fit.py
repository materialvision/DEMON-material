"""TRT VAE profile-fit guard.

``_trt_vae_profile_fits`` decides whether a process-wide cached TRT VAE
engine can take a given input shape, so a caller with an eager VAE can
fall back instead of letting TRT reject the shape mid-encode (the
"120 s upload vs the live session's 60 s engine" hazard). Contract:

- True / False when the verdict is known from the cached engine's
  optimization profile,
- None when it can't be determined (engine not cached, engine slot
  empty, TRT API error) — callers treat None as "behave as before".

The engine cache is faked at the module seam; no TRT runtime needed.
"""

from __future__ import annotations

import os

import acestep.nodes.vae_nodes as vn

_PATH = "/tmp/fake_vae_encode.engine"


class _FakeEngine:
    def __init__(self, mn, opt, mx, error=None, extra_profiles=()):
        # Profile 0 plus any extra (mn, opt, mx) triples.
        self._profiles = [(mn, opt, mx), *extra_profiles]
        self._error = error

    @property
    def num_optimization_profiles(self):
        return len(self._profiles)

    def get_tensor_profile_shape(self, tensor_name, profile_index):
        if self._error is not None:
            raise self._error
        return self._profiles[profile_index]


def _install(monkeypatch, engine):
    monkeypatch.setattr(
        vn, "_trt_vae_cache",
        {os.path.abspath(_PATH): {"engine": engine}},
    )


def test_unknown_when_engine_not_cached(monkeypatch):
    monkeypatch.setattr(vn, "_trt_vae_cache", {})
    assert vn._trt_vae_profile_fits(_PATH, "audio", (1, 2, 48_000)) is None


def test_unknown_when_cache_entry_has_no_engine(monkeypatch):
    monkeypatch.setattr(
        vn, "_trt_vae_cache", {os.path.abspath(_PATH): {"engine": None}},
    )
    assert vn._trt_vae_profile_fits(_PATH, "audio", (1, 2, 48_000)) is None


def test_fits_when_shape_inside_profile(monkeypatch):
    _install(monkeypatch, _FakeEngine(
        mn=(1, 2, 48_000), opt=(1, 2, 1_440_000), mx=(1, 2, 2_880_000),
    ))
    assert vn._trt_vae_profile_fits(_PATH, "audio", (1, 2, 1_000_000)) is True
    # Boundary values are inside the profile.
    assert vn._trt_vae_profile_fits(_PATH, "audio", (1, 2, 48_000)) is True
    assert vn._trt_vae_profile_fits(_PATH, "audio", (1, 2, 2_880_000)) is True


def test_rejects_shape_outside_profile(monkeypatch):
    _install(monkeypatch, _FakeEngine(
        mn=(1, 2, 48_000), opt=(1, 2, 1_440_000), mx=(1, 2, 2_880_000),
    ))
    # The 120s-upload-vs-60s-engine case: too many samples.
    assert vn._trt_vae_profile_fits(_PATH, "audio", (1, 2, 5_760_000)) is False
    # Too few.
    assert vn._trt_vae_profile_fits(_PATH, "audio", (1, 2, 1_000)) is False
    # Wrong batch.
    assert vn._trt_vae_profile_fits(_PATH, "audio", (2, 2, 1_000_000)) is False


def test_rejects_rank_mismatch(monkeypatch):
    _install(monkeypatch, _FakeEngine(
        mn=(1, 2, 48_000), opt=(1, 2, 1_440_000), mx=(1, 2, 2_880_000),
    ))
    assert vn._trt_vae_profile_fits(_PATH, "audio", (2, 48_000)) is False


def test_any_profile_fitting_is_enough(monkeypatch):
    # Multi-profile engine: profile 0 is too small, profile 1 covers the
    # shape — the guard must not false-negative into the eager fallback
    # by only consulting profile 0.
    _install(monkeypatch, _FakeEngine(
        mn=(1, 2, 48_000), opt=(1, 2, 1_440_000), mx=(1, 2, 2_880_000),
        extra_profiles=[
            ((1, 2, 2_880_000), (1, 2, 5_760_000), (1, 2, 11_520_000)),
        ],
    ))
    assert vn._trt_vae_profile_fits(_PATH, "audio", (1, 2, 5_760_000)) is True
    # Outside every profile still rejects.
    assert vn._trt_vae_profile_fits(_PATH, "audio", (1, 2, 20_000_000)) is False


def test_unknown_on_trt_api_error(monkeypatch):
    _install(monkeypatch, _FakeEngine(
        mn=(1, 2, 48_000), opt=(1, 2, 1_440_000), mx=(1, 2, 2_880_000),
        error=RuntimeError("TRT API exploded"),
    ))
    assert vn._trt_vae_profile_fits(_PATH, "audio", (1, 2, 1_000_000)) is None
