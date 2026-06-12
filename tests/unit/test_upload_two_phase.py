"""Two-phase upload persistence.

Phase 1 (``persist_user_upload_packet`` with empty stems): source WAV +
full sidecar only — the fast ``upload_ok`` ack path. Phase 2
(``persist_user_upload_stems``): backfills stem WAVs + per-stem sidecars
and re-derives the metadata manifest. The phase-2 writer must refuse to
write when the track dir was wiped mid-rip (session-end wipe racing a
long separation — recreating a wiped track would leak it to the pod's
next renter).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import torch

from acestep.paths import user_uploads_dir
from acestep.track_assets import sidecar_paths, track_dir
from acestep.user_uploads import (
    persist_user_upload_packet,
    persist_user_upload_stems,
)

_SR = 48_000


def _source():
    return SimpleNamespace(
        latent=SimpleNamespace(tensor=torch.zeros((1, 8, 4), dtype=torch.float32)),
        context_latent=SimpleNamespace(tensor=torch.zeros((1, 8, 4), dtype=torch.float32)),
    )


def _waveform():
    return torch.zeros((2, _SR), dtype=torch.float32)


def _stems():
    return {
        "vocals": torch.ones((2, _SR), dtype=torch.float32),
        "instruments": torch.full((2, _SR), 0.5),
    }


def _persist_phase1(name: str):
    return persist_user_upload_packet(
        name,
        waveform=_waveform(),
        stems={},
        sources={"full": _source()},
        sample_rate=_SR,
        checkpoint="acestep-v15-turbo",
        bpm=120,
        key="C major",
        time_signature="4",
    )


def _persist_phase2(name: str) -> bool:
    return persist_user_upload_stems(
        name,
        waveform=_waveform(),
        stems=_stems(),
        sources={"vocals": _source(), "instruments": _source()},
        sample_rate=_SR,
        checkpoint="acestep-v15-turbo",
        bpm=120,
        key="C major",
        time_signature="4",
    )


def test_phase1_persists_source_without_stem_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ACESTEP_MODELS_DIR", str(tmp_path))
    packet = _persist_phase1("song.wav")

    root = user_uploads_dir()
    tdir = track_dir(root, "song.wav")
    assert packet.samples == _SR
    assert tdir.is_dir()
    assert (tdir / "track.json").is_file()
    full_json, full_sf = sidecar_paths(root, "song.wav", "full")
    assert full_json.is_file() and full_sf.is_file()
    # Phase 1 writes NO stem assets — they arrive with the background rip.
    assert not (tdir / "stems").exists()
    for mode in ("vocals", "instruments"):
        stem_json, stem_sf = sidecar_paths(root, "song.wav", mode)
        assert not stem_json.exists() and not stem_sf.exists()


def test_phase2_backfills_stems_and_sidecars(tmp_path, monkeypatch):
    monkeypatch.setenv("ACESTEP_MODELS_DIR", str(tmp_path))
    _persist_phase1("song.wav")

    assert _persist_phase2("song.wav") is True

    root = user_uploads_dir()
    tdir = track_dir(root, "song.wav")
    assert (tdir / "stems" / "vocals.wav").is_file()
    assert (tdir / "stems" / "instruments.wav").is_file()
    for mode in ("vocals", "instruments"):
        stem_json, stem_sf = sidecar_paths(root, "song.wav", mode)
        assert stem_json.is_file() and stem_sf.is_file()
        meta = json.loads(stem_json.read_text(encoding="utf-8"))
        assert meta.get("samples") == _SR
    # Metadata manifest re-derived after the stem files landed.
    assert (tdir / "track.json").is_file()


def test_phase2_refuses_to_write_after_track_wipe(tmp_path, monkeypatch):
    monkeypatch.setenv("ACESTEP_MODELS_DIR", str(tmp_path))
    # Track was never persisted (or was wiped mid-rip): phase 2 must
    # decline, not recreate the directory.
    assert _persist_phase2("wiped.wav") is False

    root = user_uploads_dir()
    assert not track_dir(root, "wiped.wav").exists()


def test_phase2_honors_wipe_racing_the_writes(tmp_path, monkeypatch):
    # TOCTOU: the is_dir() pre-check passes, then the session-end wipe
    # runs while the stem WAVs/sidecars are being written. The post-write
    # sentinel (phase-1 source WAV, which phase 2 never recreates) must
    # catch it: everything just written is deleted again, nothing leaks
    # to the pod's next renter.
    import shutil

    import acestep.user_uploads as uploads_mod

    monkeypatch.setenv("ACESTEP_MODELS_DIR", str(tmp_path))
    _persist_phase1("raced.wav")
    root = user_uploads_dir()
    tdir = track_dir(root, "raced.wav")

    real_write_stem_wavs = uploads_mod.write_stem_wavs

    def _write_then_wipe(*args, **kwargs):
        real_write_stem_wavs(*args, **kwargs)
        shutil.rmtree(tdir)  # the wipe lands mid-phase-2

    monkeypatch.setattr(uploads_mod, "write_stem_wavs", _write_then_wipe)

    assert _persist_phase2("raced.wav") is False
    assert not tdir.exists()
