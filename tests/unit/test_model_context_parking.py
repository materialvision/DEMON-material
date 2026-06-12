"""Park/restore semantics of ModelContext VRAM management.

Covers the three placement protocols the VRAM-pressure work added:

- ``vram_parked()``: temporary eviction (Mel-Band separation window) —
  exception-safe restore, idempotent nesting, restore-error policy.
- ``offload_eager_to_cpu()`` / ``ensure_eager_on_device()``: persistent
  eviction between uploads, with per-module lazy restore through
  ``_load_model_context``.
- ``_placement_lock``: eager-module consumers block while the models
  are parked instead of running GPU inputs against CPU weights.

No GPU needed: device moves are simulated by overriding the two
primitives every placement path funnels through
(``_recursive_to_device`` / ``_module_on_device``), so the tests
exercise the real orchestration, lock, and error handling.
"""

from __future__ import annotations

import threading

import pytest
import torch

from acestep.engine.model_context import ModelContext


class _FakeModule:
    """Stands in for an eager nn.Module; placement is a plain attribute."""

    def __init__(self, device_type: str = "cuda"):
        self.fake_device = device_type


class _FakeLatent:
    """Stands in for the silence-latent tensor (park path calls .cpu())."""

    def __init__(self, device_type: str):
        self.device = torch.device(device_type)

    def cpu(self) -> "_FakeLatent":
        return _FakeLatent("cpu")


class _Ctx(ModelContext):
    """ModelContext with checkpoint loading bypassed and device moves
    simulated. Everything else (locks, park bookkeeping, restore-error
    policy) is the production code."""

    def __init__(self, *, offload_to_cpu=False, device="cuda", silence=None):
        # Deliberately do NOT call super().__init__ (loads checkpoints).
        self._placement_lock = threading.RLock()
        self.offload_to_cpu = offload_to_cpu
        self.offload_dit_to_cpu = False
        self._offload_text_encoder = False
        self.device = device
        self.dtype = torch.float32
        self.model = _FakeModule()
        self.vae = _FakeModule()
        self.text_encoder = _FakeModule()
        self.silence_latent = silence
        self.moves: list[tuple[str, str]] = []
        self.fail_restore = False
        self.silence_restores = 0

    def _name_of(self, module) -> str:
        for name in ("model", "vae", "text_encoder"):
            if getattr(self, name, None) is module:
                return name
        return "?"

    def _recursive_to_device(self, model, device, dtype=None):
        target = torch.device(device).type
        if self.fail_restore and target != "cpu":
            raise RuntimeError("simulated restore failure")
        self.moves.append((self._name_of(model), target))
        model.fake_device = target

    def _module_on_device(self, module, device) -> bool:
        return module.fake_device == device.type

    def _module_fully_on_device(self, module, device) -> bool:
        # Fakes have a single placement, so partial residency can't
        # occur; mirror the any-param predicate.
        return module.fake_device == device.type

    def _get_vae_dtype(self):
        return torch.float32

    def _ensure_silence_latent_on_device(self):
        self.silence_restores += 1
        self.silence_latent = _FakeLatent("cuda")

    def devices(self) -> dict[str, str]:
        return {
            name: getattr(self, name).fake_device
            for name in ("model", "vae", "text_encoder")
        }


# ---------------------------------------------------------------------------
# vram_parked: temporary park/restore
# ---------------------------------------------------------------------------


def test_vram_parked_parks_all_resident_modules_and_restores():
    ctx = _Ctx()
    with ctx.vram_parked() as parked:
        assert parked == ["model", "vae", "text_encoder"]
        assert set(ctx.devices().values()) == {"cpu"}
    assert set(ctx.devices().values()) == {"cuda"}


def test_vram_parked_noop_on_cpu_context():
    ctx = _Ctx(device="cpu")
    with ctx.vram_parked() as parked:
        assert parked == []
    assert ctx.moves == []


def test_vram_parked_only_restores_what_it_parked():
    ctx = _Ctx()
    ctx.vae.fake_device = "cpu"  # already offloaded by someone else
    with ctx.vram_parked() as parked:
        assert "vae" not in parked
    # The restore must not promote a module the park didn't move.
    assert ctx.vae.fake_device == "cpu"
    assert ctx.model.fake_device == "cuda"


def test_vram_parked_includes_silence_latent():
    ctx = _Ctx(silence=_FakeLatent("cuda"))
    with ctx.vram_parked() as parked:
        assert "silence_latent" in parked
        assert ctx.silence_latent.device.type == "cpu"
    assert ctx.silence_restores == 1
    assert ctx.silence_latent.device.type == "cuda"


def test_vram_parked_restores_on_body_exception():
    ctx = _Ctx()
    with pytest.raises(ValueError, match="boom"):
        with ctx.vram_parked():
            raise ValueError("boom")
    assert set(ctx.devices().values()) == {"cuda"}


def test_vram_parked_surfaces_restore_failure():
    ctx = _Ctx()
    with pytest.raises(RuntimeError, match="simulated restore failure"):
        with ctx.vram_parked():
            ctx.fail_restore = True
    # Modules are stranded on CPU — but the failure was loud, not silent.
    assert set(ctx.devices().values()) == {"cpu"}


def test_vram_parked_restore_failure_does_not_mask_body_exception():
    ctx = _Ctx()
    with pytest.raises(ValueError, match="body wins"):
        with ctx.vram_parked():
            ctx.fail_restore = True
            raise ValueError("body wins")


def test_vram_parked_nesting_is_idempotent():
    ctx = _Ctx()
    with ctx.vram_parked() as outer:
        assert outer == ["model", "vae", "text_encoder"]
        with ctx.vram_parked() as inner:
            assert inner == []  # nothing left to park
            assert set(ctx.devices().values()) == {"cpu"}
        # Inner exit must not restore the outer park.
        assert set(ctx.devices().values()) == {"cpu"}
    assert set(ctx.devices().values()) == {"cuda"}


# ---------------------------------------------------------------------------
# Placement lock: consumers wait for the restore
# ---------------------------------------------------------------------------


def test_load_model_context_blocks_while_parked():
    ctx = _Ctx()
    in_park = threading.Event()
    release_park = threading.Event()
    order: list[str] = []

    def parker():
        with ctx.vram_parked():
            in_park.set()
            assert release_park.wait(timeout=5)
            order.append("park_exit")

    def consumer():
        with ctx._load_model_context("model"):
            order.append("consumer_ran")
            assert ctx.model.fake_device == "cuda"

    park_thread = threading.Thread(target=parker)
    park_thread.start()
    assert in_park.wait(timeout=5)

    consumer_thread = threading.Thread(target=consumer)
    consumer_thread.start()
    consumer_thread.join(timeout=0.3)
    # The consumer must be blocked on the placement lock, not running
    # GPU inputs against CPU weights.
    assert consumer_thread.is_alive()

    release_park.set()
    consumer_thread.join(timeout=5)
    park_thread.join(timeout=5)
    assert not consumer_thread.is_alive()
    assert order == ["park_exit", "consumer_ran"]


def test_ensure_silence_latent_respects_placement_lock():
    # cond_nodes calls _ensure_silence_latent_on_device directly (it is
    # NOT routed through _load_model_context), so the method itself must
    # take the placement lock — otherwise a conditioning op racing a
    # vram_parked() can move the silence latent mid-park.
    ctx = object.__new__(ModelContext)
    ctx._placement_lock = threading.RLock()
    ctx.device = "cpu"
    ctx.dtype = torch.float32
    ctx.offload_to_cpu = False
    ctx.offload_dit_to_cpu = False
    ctx._offload_text_encoder = False
    ctx.model = ctx.vae = ctx.text_encoder = None
    ctx.silence_latent = torch.zeros((1, 4, 2), dtype=torch.float32)

    in_park = threading.Event()
    release_park = threading.Event()
    done = threading.Event()

    def parker():
        with ctx.vram_parked():  # holds the placement lock for the body
            in_park.set()
            assert release_park.wait(timeout=5)

    def consumer():
        ctx._ensure_silence_latent_on_device()
        done.set()

    park_thread = threading.Thread(target=parker)
    park_thread.start()
    assert in_park.wait(timeout=5)

    consumer_thread = threading.Thread(target=consumer)
    consumer_thread.start()
    consumer_thread.join(timeout=0.3)
    assert not done.is_set()  # blocked on the placement lock

    release_park.set()
    consumer_thread.join(timeout=5)
    park_thread.join(timeout=5)
    assert done.is_set()


def test_load_model_context_reentrant_for_nested_consumers():
    ctx = _Ctx()
    with ctx._load_model_context("model"):
        with ctx._load_model_context("vae"):  # must not deadlock
            pass


# ---------------------------------------------------------------------------
# Persistent offload + lazy restore
# ---------------------------------------------------------------------------


def test_offload_eager_to_cpu_is_persistent():
    ctx = _Ctx()
    parked = ctx.offload_eager_to_cpu()
    assert parked == ["model", "vae", "text_encoder"]
    # Unlike vram_parked, nothing restores on its own.
    assert set(ctx.devices().values()) == {"cpu"}


def test_load_model_context_lazily_restores_only_touched_module():
    ctx = _Ctx()
    ctx.offload_eager_to_cpu()
    with ctx._load_model_context("vae"):
        assert ctx.vae.fake_device == "cuda"
        assert ctx.model.fake_device == "cpu"
        assert ctx.text_encoder.fake_device == "cpu"
    # Resident mode: the touched module STAYS on the device afterwards.
    assert ctx.vae.fake_device == "cuda"
    assert ctx.model.fake_device == "cpu"


def test_ensure_eager_on_device_restores_everything_once():
    ctx = _Ctx()
    ctx.offload_eager_to_cpu()
    restored = ctx.ensure_eager_on_device()
    assert restored == ["model", "vae", "text_encoder"]
    assert set(ctx.devices().values()) == {"cuda"}
    assert ctx.ensure_eager_on_device() == []  # idempotent


def test_ensure_eager_on_device_noop_in_offload_mode():
    ctx = _Ctx(offload_to_cpu=True)
    assert ctx.ensure_eager_on_device() == []
    assert ctx.moves == []


def test_offload_eager_to_cpu_noop_on_cpu_context():
    ctx = _Ctx(device="cpu")
    assert ctx.offload_eager_to_cpu() == []
    assert ctx.moves == []


def test_partial_residency_predicates_disagree_by_design():
    # A partially moved module (e.g. after a failed restore) must count
    # as resident for the PARK decision (it holds VRAM — evict it) but
    # NOT for the restore-skip decision (half a model on GPU is not a
    # usable model). Exercises the real ModelContext predicates.
    from types import SimpleNamespace

    ctx = object.__new__(ModelContext)
    dev = torch.device("cuda")

    def _mixed_params():
        return iter([
            SimpleNamespace(device=torch.device("cuda")),
            SimpleNamespace(device=torch.device("cpu")),
        ])

    mixed = SimpleNamespace(parameters=_mixed_params)
    assert ctx._module_on_device(mixed, dev) is True
    assert ctx._module_fully_on_device(mixed, dev) is False
