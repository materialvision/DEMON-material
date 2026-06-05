"""ACE drain parity rail, unit tier (backend-seam plan round 3, Phase 3a).

Drains a fixed scenario battery through ``StreamPipeline`` with a
deterministic mock decoder on CPU and requires the finished latents to
be BIT-IDENTICAL to the blessed capture in ``tests/unit/data/``,
generated on the pre-ModelAdapter-seam tree (be4d954). This pins the
exact behavior the seam relocates — noise generation order and sizing,
schedule construction, batched-forward assembly (encoder padding /
concatenation order), the CFG negative pass, and per-slot integration —
so the ACEAdapter extraction is provably byte-identical without a GPU.

The GPU-side rail with real weights is ``scripts/ace_drain_parity.py``;
the end-to-end gate is the golden harness (``tests/golden``).

Regenerate the blessed capture ONLY when intentionally changing
semantics::

    .venv/Scripts/python.exe tests/unit/test_ace_adapter_parity.py --capture
"""

from __future__ import annotations

from pathlib import Path

import pytest
import torch

from acestep.engine.diffusion import DiffusionConfig, DiffusionEngine
from acestep.engine.stream import SlotCondition, SlotRequest, StreamPipeline

BLESSED = Path(__file__).parent / "data" / "ace_adapter_parity_blessed.pt"

T = 40          # latent frames
D = 64          # ACE latent channels
D_CTX = 128     # context = src ++ chunk_mask
L_ENC = 12      # encoder sequence length (primary)
L_ENC_B = 7     # shorter secondary condition (exercises padding)
ENC_DIM = 32    # stand-in for the 2048-wide text embedding


class _MockDecoder:
    """Deterministic stand-in for the ACE DiT.

    A pure function of every input the PyTorch forward path feeds it,
    constructed so a change in batching, padding, masking, timestep
    ordering, or context assembly changes the output. Returns a 1-tuple
    like the real decoder.
    """

    def __call__(
        self,
        *,
        hidden_states,
        timestep,
        timestep_r,
        attention_mask,
        encoder_hidden_states,
        encoder_attention_mask,
        context_latents,
        use_cache,
        past_key_values,
    ):
        B, T_, D_ = hidden_states.shape
        # Masked encoder summary: padding rows are zeroed by the mask,
        # so a padding-behavior change that leaks nonzero pad content
        # shifts this term; the raw (unmasked) mean catches the
        # complementary failure where mask handling changes.
        enc_masked = (
            encoder_hidden_states * encoder_attention_mask.unsqueeze(-1)
        ).sum(dim=(1, 2)).view(B, 1, 1)
        enc_raw = encoder_hidden_states.mean(dim=(1, 2)).view(B, 1, 1)
        t_term = timestep.view(B, 1, 1)
        vel = (
            0.30 * hidden_states
            + 0.05 * context_latents[..., :D_]
            + 0.07 * context_latents[..., D_:]
            + 0.013 * enc_masked
            + 0.11 * enc_raw
            - 0.40 * t_term
        )
        return (vel,)


class _FakeEngine:
    """The minimal DiffusionEngine surface StreamPipeline consumes,
    with the REAL schedule builder so schedule numerics are pinned."""

    _DENOISE_MIN = DiffusionEngine._DENOISE_MIN
    _build_timestep_schedule = DiffusionEngine._build_timestep_schedule

    def __init__(self):
        self.decoder = _MockDecoder()
        self.model = None
        self._trt_ctx = None
        self._trt_stream = None
        self._trt_engine = None
        self._trt_io_dtype = torch.float32
        self._trt_input_dtypes = {}
        self._trt_output_dtype = torch.float32
        self._compile_loops = False


def _cond_tensors(seed: int, L: int):
    g = torch.Generator().manual_seed(seed)
    enc = torch.randn(1, L, ENC_DIM, generator=g)
    mask = torch.ones(1, L)
    return enc, mask


def _request(seed: int, *, denoise: float = 1.0, L: int = L_ENC, **kw) -> SlotRequest:
    g = torch.Generator().manual_seed(seed + 7777)
    ctx = torch.randn(1, T, D_CTX, generator=g)
    enc, mask = _cond_tensors(seed, L)
    return SlotRequest(
        encoder_hidden_states=enc,
        encoder_attention_mask=mask,
        context_latents=ctx,
        seed=seed,
        denoise=denoise,
        **kw,
    )


def _source(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed + 31337)
    return torch.randn(1, T, D, generator=g)


def _drain(pipe: StreamPipeline, requests, ticks: int):
    out = []
    queue = list(requests)
    torch.manual_seed(0)  # pin global RNG for SDE renoise draws
    for _ in range(ticks):
        if queue:
            pipe.submit(queue.pop(0))
        fin = pipe.tick()
        if fin is not None:
            out.append(fin.detach().clone())
    return out


def _config(method: str = "ode", steps: int = 4) -> DiffusionConfig:
    return DiffusionConfig(
        infer_steps=steps,
        shift=3.0,
        infer_method=method,
        noise_on_cpu=True,
        dcw_enabled=False,
    )


def _scenarios() -> dict:
    """name -> list of finished latents. Every tensor is fp32 CPU."""
    out: dict = {}

    # 1. Plain ODE drain, full denoise (fast Euler path), varying seeds.
    pipe = StreamPipeline(_FakeEngine(), _config("ode"), pipeline_depth=2)
    out["ode_plain"] = _drain(
        pipe, [_request(s) for s in (11, 22, 33, 44)], ticks=14,
    )

    # 2. Partial denoise against a source (schedule cache + init mix).
    pipe = StreamPipeline(_FakeEngine(), _config("ode"), pipeline_depth=2)
    reqs = [
        _request(55, denoise=0.6, source_latents=_source(1)),
        _request(66, denoise=0.35, source_latents=_source(2)),
        _request(77, denoise=0.6, source_latents=_source(1)),
    ]
    out["ode_partial_denoise"] = _drain(pipe, reqs, ticks=12)

    # 3. SDE (bare re-noise) drain.
    pipe = StreamPipeline(_FakeEngine(), _config("sde"), pipeline_depth=2)
    out["sde_plain"] = _drain(
        pipe, [_request(s) for s in (10, 20, 30)], ticks=12,
    )

    # 4. SDE with a per-frame denoise curve + source.
    pipe = StreamPipeline(_FakeEngine(), _config("ode"), pipeline_depth=2)
    curve = torch.linspace(0.1, 0.9, T).view(1, T, 1)
    reqs = [
        _request(s, source_latents=_source(s), sde_denoise_curve=curve)
        for s in (12, 23)
    ]
    out["sde_curve"] = _drain(pipe, reqs, ticks=10)

    # 5. Multi-condition with mixed encoder lengths (padding + blend).
    pipe = StreamPipeline(_FakeEngine(), _config("ode"), pipeline_depth=2)
    enc_b, mask_b = _cond_tensors(901, L_ENC_B)
    weight = torch.linspace(0.0, 1.0, T)
    reqs = [
        _request(
            s,
            extra_conditions=[SlotCondition(
                encoder_hidden_states=enc_b,
                encoder_attention_mask=mask_b,
                temporal_weight=weight,
            )],
        )
        for s in (41, 52)
    ]
    out["multi_cond_padded"] = _drain(pipe, reqs, ticks=10)

    # 6. Full CFG (negative forward every step) with a guidance curve.
    pipe = StreamPipeline(_FakeEngine(), _config("ode"), pipeline_depth=2)
    neg_enc, neg_mask = _cond_tensors(777, L_ENC_B)
    gcurve = torch.full((1, T, 1), 2.5)
    reqs = [
        _request(
            s,
            neg_conditions=[SlotCondition(
                encoder_hidden_states=neg_enc,
                encoder_attention_mask=neg_mask,
            )],
            guidance_curve=gcurve,
        )
        for s in (61, 72)
    ]
    out["cfg_full"] = _drain(pipe, reqs, ticks=10)

    # 7. RCFG self (virtual negative = slot's initial noise).
    pipe = StreamPipeline(_FakeEngine(), _config("ode"), pipeline_depth=2)
    reqs = [
        _request(s, guidance_curve=gcurve, rcfg_mode="self")
        for s in (81, 92)
    ]
    out["rcfg_self"] = _drain(pipe, reqs, ticks=10)

    # 8. x0-target blend (scalar strength via shared curve) + velocity
    #    scale shared override.
    pipe = StreamPipeline(_FakeEngine(), _config("ode"), pipeline_depth=2)
    pipe.set_shared_curve("x0_target_strength", 0.4)
    pipe.set_shared_curve("velocity_scale", 1.15)
    reqs = [
        _request(s, x0_target=_source(9), source_latents=_source(9),
                 denoise=0.8)
        for s in (101, 112)
    ]
    out["x0_target_shared_curves"] = _drain(pipe, reqs, ticks=10)

    return out


def capture() -> None:
    BLESSED.parent.mkdir(parents=True, exist_ok=True)
    torch.save(_scenarios(), BLESSED)
    sizes = {k: len(v) for k, v in _scenarios().items()}
    print(f"wrote {BLESSED}: {sizes}")


@pytest.mark.skipif(not BLESSED.exists(), reason="blessed capture missing")
def test_ace_drain_bit_identical():
    blessed = torch.load(BLESSED, weights_only=True)
    live = _scenarios()
    assert set(live) == set(blessed), (
        f"scenario set drifted: {sorted(live)} vs {sorted(blessed)}"
    )
    for name, ref_list in blessed.items():
        got_list = live[name]
        assert len(got_list) == len(ref_list), (
            f"{name}: emitted {len(got_list)} latents, blessed {len(ref_list)}"
        )
        for i, (got, ref) in enumerate(zip(got_list, ref_list)):
            assert got.shape == ref.shape, f"{name}[{i}]: shape drift"
            assert torch.equal(got, ref), (
                f"{name}[{i}]: NOT bit-identical "
                f"(max_diff={(got - ref).abs().max().item():.3e})"
            )


if __name__ == "__main__":
    import sys

    if "--capture" in sys.argv:
        capture()
    else:
        print(__doc__)
