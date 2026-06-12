"""Mel-Band separator VRAM policy + pending-stems registry.

Covers the env-knob parsing (`DEMON_MELBAND_VRAM_PARK`,
`DEMON_MELBAND_VRAM_RESERVE_GB`, `DEMON_MELBAND_RAM_CACHE`), the
park decision in ``should_park_for_melband``, and the registry the
two-phase upload path uses to coordinate background stem rips with
swaps. CPU-only: VRAM telemetry is monkeypatched at the module seam.
"""

from __future__ import annotations

import threading

import torch

import acestep.streaming.stems as stems_mod
from acestep.streaming.stems import (
    DEFAULT_MELBAND_PARK_MODE,
    DEFAULT_MELBAND_VRAM_RESERVE_GB,
    MELBAND_RAM_CACHE_ENV,
    MELBAND_VRAM_PARK_ENV,
    MELBAND_VRAM_RESERVE_ENV,
    finish_stems_pending,
    mark_stems_pending,
    melband_park_mode,
    melband_vram_reserve_gb,
    should_park_for_melband,
    stems_pending,
    wait_for_pending_stems,
)


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def test_park_mode_defaults_to_always(monkeypatch):
    monkeypatch.delenv(MELBAND_VRAM_PARK_ENV, raising=False)
    assert melband_park_mode() == "always"
    assert DEFAULT_MELBAND_PARK_MODE == "always"


def test_park_mode_accepts_documented_values(monkeypatch):
    for mode in ("always", "auto", "never"):
        monkeypatch.setenv(MELBAND_VRAM_PARK_ENV, mode.upper())
        assert melband_park_mode() == mode


def test_park_mode_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(MELBAND_VRAM_PARK_ENV, "sometimes")
    assert melband_park_mode() == DEFAULT_MELBAND_PARK_MODE


def test_reserve_gb_default_and_overrides(monkeypatch):
    monkeypatch.delenv(MELBAND_VRAM_RESERVE_ENV, raising=False)
    assert melband_vram_reserve_gb() == DEFAULT_MELBAND_VRAM_RESERVE_GB
    monkeypatch.setenv(MELBAND_VRAM_RESERVE_ENV, "8.5")
    assert melband_vram_reserve_gb() == 8.5
    monkeypatch.setenv(MELBAND_VRAM_RESERVE_ENV, "-3")
    assert melband_vram_reserve_gb() == 0.0  # clamped, never negative
    monkeypatch.setenv(MELBAND_VRAM_RESERVE_ENV, "not-a-number")
    assert melband_vram_reserve_gb() == DEFAULT_MELBAND_VRAM_RESERVE_GB


def test_ram_cache_enabled_by_default_and_disable_spellings(monkeypatch):
    monkeypatch.delenv(MELBAND_RAM_CACHE_ENV, raising=False)
    assert stems_mod._melband_ram_cache_enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv(MELBAND_RAM_CACHE_ENV, off)
        assert stems_mod._melband_ram_cache_enabled() is False
    monkeypatch.setenv(MELBAND_RAM_CACHE_ENV, "1")
    assert stems_mod._melband_ram_cache_enabled() is True


# ---------------------------------------------------------------------------
# should_park_for_melband
# ---------------------------------------------------------------------------


def _telemetry(available_gb: float):
    return {
        "free_gb": available_gb,
        "total_gb": 24.0,
        "allocated_gb": 24.0 - available_gb,
        "reserved_gb": 24.0 - available_gb,
        "available_gb": available_gb,
    }


def test_should_park_false_on_non_cuda_device(monkeypatch):
    park, _, _ = should_park_for_melband(torch.device("cpu"))
    assert park is False


def test_should_park_never_mode_disables_parking(monkeypatch):
    monkeypatch.setenv(MELBAND_VRAM_PARK_ENV, "never")
    park, _, _ = should_park_for_melband(torch.device("cuda"))
    assert park is False


def test_should_park_always_mode_parks_regardless_of_headroom(monkeypatch):
    monkeypatch.delenv(MELBAND_VRAM_PARK_ENV, raising=False)
    monkeypatch.setattr(
        stems_mod, "get_vram_telemetry", lambda device: _telemetry(20.0),
    )
    park, available_gb, _ = should_park_for_melband(torch.device("cuda"))
    assert park is True
    assert available_gb == 20.0


def test_should_park_auto_mode_compares_against_reserve(monkeypatch):
    monkeypatch.setenv(MELBAND_VRAM_PARK_ENV, "auto")
    monkeypatch.setenv(MELBAND_VRAM_RESERVE_ENV, "6.0")

    monkeypatch.setattr(
        stems_mod, "get_vram_telemetry", lambda device: _telemetry(4.0),
    )
    park, _, reserve = should_park_for_melband(torch.device("cuda"))
    assert park is True
    assert reserve == 6.0

    monkeypatch.setattr(
        stems_mod, "get_vram_telemetry", lambda device: _telemetry(9.0),
    )
    park, _, _ = should_park_for_melband(torch.device("cuda"))
    assert park is False


def test_should_park_auto_mode_without_telemetry_does_not_park(monkeypatch):
    monkeypatch.setenv(MELBAND_VRAM_PARK_ENV, "auto")
    monkeypatch.setattr(stems_mod, "get_vram_telemetry", lambda device: None)
    park, available_gb, _ = should_park_for_melband(torch.device("cuda"))
    assert park is False
    assert available_gb == 0.0


# ---------------------------------------------------------------------------
# Pending-stems registry
# ---------------------------------------------------------------------------


def test_pending_registry_round_trip():
    name = "test-pending-roundtrip"
    assert stems_pending(name) is False
    mark_stems_pending(name)
    try:
        assert stems_pending(name) is True
    finally:
        finish_stems_pending(name)
    assert stems_pending(name) is False


def test_pending_registry_rejects_non_string_names():
    assert stems_pending(None) is False
    assert stems_pending(42) is False
    # And waiting on a non-name never blocks.
    assert wait_for_pending_stems(None, timeout=0.01) is True


def test_wait_returns_immediately_when_nothing_pending():
    assert wait_for_pending_stems("never-marked", timeout=0.01) is True


def test_wait_times_out_while_rip_in_flight():
    name = "test-pending-timeout"
    mark_stems_pending(name)
    try:
        assert wait_for_pending_stems(name, timeout=0.05) is False
    finally:
        finish_stems_pending(name)


def test_finish_unblocks_concurrent_waiter():
    name = "test-pending-unblock"
    mark_stems_pending(name)
    results: list[bool] = []

    waiter = threading.Thread(
        target=lambda: results.append(wait_for_pending_stems(name, timeout=5.0)),
    )
    waiter.start()
    finish_stems_pending(name)
    waiter.join(timeout=5.0)
    assert results == [True]
    assert stems_pending(name) is False


def test_finish_is_safe_for_unknown_names():
    finish_stems_pending("never-marked-either")  # must not raise


def test_finish_unblocks_waiter_on_failure_path_too():
    # finish_stems_pending is called on BOTH success and failure; the
    # waiter re-checks the disk cache afterwards. The registry must not
    # distinguish — a failed rip still unblocks.
    name = "test-pending-failure"
    mark_stems_pending(name)
    finish_stems_pending(name)  # simulates the except-path call
    assert wait_for_pending_stems(name, timeout=0.01) is True
