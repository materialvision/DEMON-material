"""Swap-path gating on in-flight background stem rips.

``extract_and_select_upload_stem`` is shared by session create and the
swap path. When the upload pipeline is still ripping a track's stems on
a background thread (pending-stems registry), the gating contract is:

- mode "full": proceed WITHOUT stems — never start a duplicate
  separation; overlays arrive later via the pushed ``stem_assets``.
- mode "vocals"/"instruments": the stem IS the inference source — wait
  for the rip, then load from the disk cache.
- not pending + cache miss: inline separation runs, and it hands the
  session's own ModelContext to ``extract_upload_stems`` so the eager
  modules park while the RoFormer runs.

All collaborators are faked at the module seam; no GPU, no separator.
"""

from __future__ import annotations

from types import SimpleNamespace

import torch

import acestep.streaming.session as session_mod
from acestep.streaming.session import extract_and_select_upload_stem

_WAVEFORM = torch.zeros((2, 480), dtype=torch.float32)
_STEMS = {
    "vocals": torch.ones((2, 480), dtype=torch.float32),
    "instruments": torch.full((2, 480), 2.0),
}


def _fake_session(prepared=None):
    return SimpleNamespace(
        handler=SimpleNamespace(device="cpu", dtype=torch.float32),
        prepare_source=lambda audio: prepared,
    )


def _forbid_separation(monkeypatch):
    def _explode(**_kwargs):
        raise AssertionError("inline separation must not run")
    monkeypatch.setattr(session_mod, "extract_upload_stems", _explode)


def test_source_mode_none_is_a_passthrough(monkeypatch):
    _forbid_separation(monkeypatch)
    source = object()
    stems, error, out_source, out_wf = extract_and_select_upload_stem(
        _WAVEFORM, session=_fake_session(), source=source, source_mode=None,
    )
    assert (stems, error, out_source) == (None, None, source)
    assert out_wf is _WAVEFORM


def test_full_swap_defers_while_rip_pending(monkeypatch):
    _forbid_separation(monkeypatch)
    monkeypatch.setattr(session_mod, "audio_clip_stems", lambda *a, **k: None)
    monkeypatch.setattr(session_mod, "stems_pending", lambda name: True)
    waited = []
    monkeypatch.setattr(
        session_mod, "wait_for_pending_stems",
        lambda name, **k: waited.append(name) or True,
    )

    source = object()
    stems, error, out_source, out_wf = extract_and_select_upload_stem(
        _WAVEFORM,
        session=_fake_session(),
        source=source,
        source_mode="full",
        fixture_name="track.wav",
    )
    # Proceeds immediately, no stems, no wait, no duplicate separation.
    assert (stems, error, out_source) == (None, None, source)
    assert out_wf is _WAVEFORM
    assert waited == []


def test_stem_swap_waits_for_pending_rip_then_uses_cache(monkeypatch):
    _forbid_separation(monkeypatch)
    calls = {"clip": 0}

    def _clip_stems(name, **_kwargs):
        # Cache miss before the rip lands, hit after the wait.
        calls["clip"] += 1
        return None if calls["clip"] == 1 else _STEMS

    waited = []
    monkeypatch.setattr(session_mod, "audio_clip_stems", _clip_stems)
    monkeypatch.setattr(session_mod, "stems_pending", lambda name: True)
    monkeypatch.setattr(
        session_mod, "wait_for_pending_stems",
        lambda name, **k: waited.append(name) or True,
    )
    monkeypatch.setattr(session_mod, "_try_load_sidecar", lambda *a, **k: None)

    prepared = object()
    stems, error, out_source, out_wf = extract_and_select_upload_stem(
        _WAVEFORM,
        session=_fake_session(prepared=prepared),
        source=object(),
        source_mode="vocals",
        fixture_name="track.wav",
    )
    assert error is None
    assert waited == ["track.wav"]
    assert calls["clip"] == 2
    assert stems is _STEMS
    assert out_source is prepared  # prepared from the ripped stem
    assert torch.equal(out_wf, _STEMS["vocals"])


def test_cache_hit_skips_wait_and_separation(monkeypatch):
    _forbid_separation(monkeypatch)
    monkeypatch.setattr(session_mod, "audio_clip_stems", lambda *a, **k: _STEMS)
    monkeypatch.setattr(
        session_mod, "stems_pending",
        lambda name: (_ for _ in ()).throw(AssertionError("cache hit must short-circuit")),
    )

    stems, error, out_source, _ = extract_and_select_upload_stem(
        _WAVEFORM,
        session=_fake_session(),
        source=object(),
        source_mode="full",
        fixture_name="track.wav",
    )
    assert error is None
    assert stems is _STEMS


def test_inline_separation_parks_via_session_model_context(monkeypatch):
    seen = {}

    def _extract(**kwargs):
        seen.update(kwargs)
        return _STEMS

    session = _fake_session()
    monkeypatch.setattr(session_mod, "audio_clip_stems", lambda *a, **k: None)
    monkeypatch.setattr(session_mod, "stems_pending", lambda name: False)
    monkeypatch.setattr(session_mod, "extract_upload_stems", _extract)

    stems, error, _, _ = extract_and_select_upload_stem(
        _WAVEFORM,
        session=session,
        source=object(),
        source_mode="full",
        fixture_name="track.wav",
    )
    assert error is None
    assert stems is _STEMS
    # The inline rip must hand over the session's own ModelContext so
    # the eager modules park while the RoFormer runs.
    assert seen["model_context"] is session.handler


def test_separation_failure_maps_to_error_and_rolls_back(monkeypatch):
    def _extract(**_kwargs):
        raise RuntimeError("separator exploded")

    monkeypatch.setattr(session_mod, "audio_clip_stems", lambda *a, **k: None)
    monkeypatch.setattr(session_mod, "stems_pending", lambda name: False)
    monkeypatch.setattr(session_mod, "extract_upload_stems", _extract)

    source = object()
    stems, error, out_source, out_wf = extract_and_select_upload_stem(
        _WAVEFORM,
        session=_fake_session(),
        source=source,
        source_mode="vocals",
        fixture_name="track.wav",
    )
    assert stems is None
    assert "separator exploded" in error
    assert out_source is source  # original source preserved on failure
    assert out_wf is _WAVEFORM
