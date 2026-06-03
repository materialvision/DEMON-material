"""Knob bank definitions + transport-agnostic knob state.

Torch-free, acestep-free. Pure dataclasses + constants.

The ``cc`` MIDI-controller-number field is intentionally absent from
``KnobDef`` because hardware MIDI binding is a transport concern that
the streaming session does not need to know about. The browser-side
MIDI store (``web/store/useMidiStore.ts``) owns its own CC mapping for
the operator's hardware controller.
"""

import threading
from dataclasses import dataclass
from typing import Any, Optional


# Manifest schema version. Bump when the knob contract changes shape in a
# way a frontend/re-skin/agent must notice (knob added/removed/retyped,
# bounds semantics changed). Served alongside the catalog at /api/knobs and
# by the MCP list_knobs tool so a consumer can detect a stale build.
KNOB_SCHEMA_VERSION = 1


# Channel groups / keystones used by the server-side pipeline for
# channel-guided generation. Shared here so the client can display them.
CHANNEL_GROUPS = [
    ("ch_g0", 0, 7),   ("ch_g1", 8, 15),  ("ch_g2", 16, 23),
    ("ch_g3", 24, 31),  ("ch_g4", 32, 39),  ("ch_g5", 40, 47),
    ("ch_g6", 48, 55),  ("ch_g7", 56, 63),
]
KEYSTONE_CHANNELS = [
    ("ch13", 13), ("ch14", 14), ("ch19", 19),
    ("ch23", 23), ("ch29", 29), ("ch56", 56),
]


@dataclass
class KnobDef:
    """KnobState value type. Only ``default`` is read by KnobState; the
    rest is metadata carried over from the registry (:func:`knob_specs`)
    for the discovery surfaces that project it."""
    default: float = 0.0
    sensitivity: float = 2.0
    max_val: float = 1.0
    min_val: Optional[float] = None
    description: str = ""


@dataclass
class KnobBank:
    name: str
    knobs: dict


@dataclass
class KnobSpec:
    """One operator knob, fully self-describing.

    This is the single source of truth for the knob universe. The
    KnobState banks (:func:`build_banks`) and the transport-agnostic
    catalog / manifest (:func:`knob_catalog`, served at ``/api/knobs``
    and by the MCP ``list_knobs`` tool) are both projections of it. Add a
    knob here and every backend surface — plus any frontend that consumes
    the manifest — picks it up automatically.

    ``bank`` knobs are KnobState-backed: continuous params the streaming
    runner reads via ``get_param``. Non-``bank`` knobs ride the same
    ``params`` channel but are consumed straight from the raw wire dict
    (e.g. ``raw.get("guidance_scale")``) and are never stored in
    KnobState — they still belong in the schema so every frontend can
    render and validate them.
    """
    name: str
    default: Any = 0.0
    min_val: Optional[float] = None
    max_val: float = 1.0
    sensitivity: float = 2.0
    group: str = "core"
    type: str = "float"            # "float" | "int" | "enum" | "bool"
    options: tuple = ()             # allowed values for enum / bool
    description: str = ""
    bank: bool = True


# Registry group -> KnobState bank name, in display order. Only ``bank``
# specs land in a KnobState bank; their ``group`` must be one of these.
_BANKS = (("core", "Core"), ("groups", "Groups"), ("keystones", "Keystones"))


def knob_specs(sde: bool, loras=None) -> list:
    """The complete operator-knob registry for a session.

    Parameterized by ``sde`` (SDE vs ODE core knobs) and the enabled
    ``loras`` (each id gets a ``lora_str_<id>`` strength knob, replacing
    the old positional ``lora_str_1`` naming so toggling catalog entries
    doesn't shuffle knob identities). ``loras`` accepts an int for
    back-compat (treated as ``slot1..slotN``). See :class:`KnobSpec`.
    """
    if isinstance(loras, int):
        lora_ids = [f"slot{i}" for i in range(1, loras + 1)]
    else:
        lora_ids = list(loras or [])

    specs: list = []

    # --- Core bank knobs (KnobState-backed) ---
    if sde:
        specs.append(KnobSpec(
            "sde_amp", sensitivity=2.0,
            description="SDE diffusion amplitude (replaces denoise in SDE mode)",
        ))
    else:
        specs.append(KnobSpec(
            "denoise", sensitivity=2.0, description="ODE denoise strength",
        ))
    specs.append(KnobSpec(
        "seed", max_val=float(0xFFFFFFFF), sensitivity=0.5, type="int",
        description="Stream seed (uint32 integer; passed to torch.manual_seed)",
    ))
    specs.append(KnobSpec(
        "feedback", sensitivity=2.0, description="Feedback amount",
    ))
    # Delay-tap depth for the feedback knob. 1 == blend with the most
    # recent finished latent; N>1 reaches N ticks back for an echo / ghost
    # effect. Integer-valued; capped at 8 (StreamPipeline ring ceiling).
    specs.append(KnobSpec(
        "feedback_depth", default=1.0, min_val=1.0, max_val=8.0,
        sensitivity=4.0, type="int",
        description="Feedback delay-tap depth in ticks (1 = last, N = N ticks back)",
    ))
    # Flow shift flows verbatim into the diffusion solver; useful ~[1, 6].
    specs.append(KnobSpec(
        "shift", default=3.5, min_val=1.0, max_val=6.0, sensitivity=1.0,
        description="Flow shift (timing/curve shape). Passed verbatim to the diffusion solver.",
    ))
    if sde:
        specs.append(KnobSpec(
            "periodicity", max_val=12.5, sensitivity=2.0,
            description="SDE periodicity",
        ))
    for lid in lora_ids:
        specs.append(KnobSpec(
            f"lora_str_{lid}", max_val=2.0, sensitivity=2.0,
            description=f"Strength for LoRA {lid!r}",
        ))
    specs.append(KnobSpec(
        "hint_strength", default=1.0, sensitivity=2.0,
        description="Structure (semantic hint) blend strength",
    ))
    # Scalar source-lock target blend. Runner reads via get_param("x0_target")
    # and pushes it in as the x0_target_strength shared curve.
    specs.append(KnobSpec(
        "x0_target", max_val=1.0, sensitivity=2.0,
        description=(
            "Scalar blend toward the x0 source-lock target (second half of "
            "the schedule). Requires a source latent."
        ),
    ))

    # --- Channel-group / keystone bank knobs (KnobState-backed) ---
    for (name, _start, _end) in CHANNEL_GROUPS:
        specs.append(KnobSpec(
            name, default=1.0, max_val=3.0, sensitivity=1.5, group="groups",
            description=f"Channel-group amplifier {name}",
        ))
    for (name, _ch) in KEYSTONE_CHANNELS:
        specs.append(KnobSpec(
            name, default=1.0, max_val=3.0, sensitivity=1.5, group="keystones",
            description=f"Keystone channel amplifier {name}",
        ))

    # --- Raw-param knobs (NOT KnobState-backed) ---
    # Step count, guidance, and the DCW corrector ride the params channel
    # but the runner consumes them via raw.get(...), never storing them in
    # KnobState. They live in the schema so the catalog/manifest is complete
    # and every frontend can render them.
    specs.append(KnobSpec(
        "steps_override", default=8.0, min_val=1.0, max_val=16.0, type="int",
        bank=False,
        description="Diffusion step count. Lower = lower quality, higher = more latency. Changing rebuilds the StreamPipeline.",
    ))
    specs.append(KnobSpec(
        "guidance_scale", default=1.0, min_val=1.0, max_val=15.0,
        group="guidance", bank=False,
        description="RCFG guidance scale. Only applied when rcfg_mode != 'off'.",
    ))
    specs.append(KnobSpec(
        "cfg_rescale", default=0.0, max_val=1.0, group="guidance", bank=False,
        description="APG norm rescale toward vt_pos magnitude. Only applied when rcfg_mode != 'off'.",
    ))
    specs.append(KnobSpec(
        "rcfg_mode", default="off", group="guidance", type="enum",
        options=("off", "self", "initialize", "full"), bank=False,
        description="Residual-CFG mode. String-valued; set via the set_rcfg_mode tool, not set_knob.",
    ))
    specs.append(KnobSpec(
        "dcw_enabled", default=True, group="dcw", type="bool",
        options=(False, True), bank=False,
        description="Enable the DCW wavelet-domain corrector.",
    ))
    specs.append(KnobSpec(
        "dcw_mode", default="double", group="dcw", type="enum",
        options=("low", "high", "double", "pix"), bank=False,
        description="DCW correction band.",
    ))
    specs.append(KnobSpec(
        "dcw_wavelet", default="haar", group="dcw", type="enum",
        options=("haar", "db4", "sym8", "db8"), bank=False,
        description="DCW wavelet basis.",
    ))
    specs.append(KnobSpec(
        "dcw_scaler", default=0.05, max_val=0.5, group="dcw", bank=False,
        description="DCW low-band push strength.",
    ))
    specs.append(KnobSpec(
        "dcw_high_scaler", default=0.02, max_val=0.5, group="dcw", bank=False,
        description="DCW high-band push strength.",
    ))
    specs.append(KnobSpec(
        "dcw_mult_blend", default=0.0, max_val=1.0, group="dcw", bank=False,
        description="DCW multiplicative-blend fader (advanced; 0 = upstream behavior).",
    ))
    specs.append(KnobSpec(
        "dcw_mag_phase", default=0.0, max_val=1.0, group="dcw", bank=False,
        description="DCW magnitude+phase fader (advanced; 0 = upstream behavior).",
    ))
    specs.append(KnobSpec(
        "dcw_soft_thresh", default=0.0, max_val=0.3, group="dcw", bank=False,
        description="DCW soft-threshold sparsity (advanced; 0 = upstream behavior).",
    ))
    return specs


def build_banks(sde: bool, loras=None) -> list:
    """Build the KnobState banks driving the streaming pipeline.

    Derived from :func:`knob_specs` (the single registry): the ``bank``
    knobs, grouped into the Core / Groups / Keystones banks the runner
    expects. Non-``bank`` schema knobs (steps_override, guidance, DCW)
    are intentionally excluded — the runner reads those from the raw wire
    dict, not KnobState.
    """
    group_to_bank = {g: bank_name for g, bank_name in _BANKS}
    grouped = {bank_name: {} for _g, bank_name in _BANKS}
    for spec in knob_specs(sde, loras):
        if not spec.bank:
            continue
        bank_name = group_to_bank.get(spec.group, "Core")
        grouped[bank_name][spec.name] = KnobDef(
            default=spec.default, sensitivity=spec.sensitivity,
            max_val=spec.max_val, min_val=spec.min_val,
            description=spec.description,
        )
    return [KnobBank(name=bank_name, knobs=grouped[bank_name])
            for _g, bank_name in _BANKS]


def knob_catalog(sde: bool, loras=None) -> dict:
    """Project the full registry into a transport-agnostic catalog:
    ``name -> {type, default, min?, max, group, options?, description?,
    bank}``. Backs both the MCP ``list_knobs`` tool and the HTTP
    ``/api/knobs`` manifest, so every frontend builds against one
    backend-owned contract instead of re-declaring the knob set.
    """
    out: dict = {}
    for spec in knob_specs(sde, loras):
        entry: dict = {
            "type": spec.type,
            "default": spec.default,
            "group": spec.group,
            "bank": spec.bank,
        }
        # min/max are only meaningful for the numeric types; enum/bool
        # knobs carry their valid set in ``options`` instead.
        if spec.type in ("float", "int"):
            entry["max"] = spec.max_val
            if spec.min_val is not None:
                entry["min"] = spec.min_val
        if spec.options:
            entry["options"] = list(spec.options)
        if spec.description:
            entry["description"] = spec.description
        out[spec.name] = entry
    return out


def coerce_knob_values(raw: dict, specs_by_name: dict) -> tuple:
    """Validate a raw wire dict against the knob registry.

    The single enforcement point for the knob contract, reused by every
    transport so validation can't drift from :func:`knob_specs`. Returns
    ``(clean, errors)``:

    * ``clean`` is a NEW dict safe to apply. Every schema key is coerced:
      ``float``/``int`` are parsed and clamped to ``[min, max]`` (ints
      rounded; absent ``min`` floors at 0), and ``enum``/``bool`` values
      are checked against ``options``. Keys absent from ``specs_by_name``
      (curve specs, ``lora_blend``, the ``playback_pos`` clock, telemetry)
      pass through untouched — the registry validates only what it owns.
    * ``errors`` lists every value that had to be clamped or dropped, as
      human-readable strings.

    Hot-path callers (the 125 Hz params channel) apply ``clean`` and ignore
    ``errors`` — silently clamping never breaks the stream. Discrete callers
    (the MCP tools) raise when ``errors`` is non-empty so the agent gets
    feedback. Out-of-range numerics are clamped into ``clean`` *and* recorded
    in ``errors``; invalid enum/bool values are dropped from ``clean`` (so the
    prior KnobState value is preserved) *and* recorded. This function never
    raises.
    """
    clean: dict = {}
    errors: list = []
    for name, val in raw.items():
        spec = specs_by_name.get(name)
        if spec is None:
            clean[name] = val  # not registry-owned: pass through verbatim
            continue
        if spec.type in ("float", "int"):
            try:
                num = float(val)
            except (TypeError, ValueError):
                errors.append(f"{name}: {val!r} is not a number")
                continue  # drop → KnobState keeps its prior value
            lo = spec.min_val if spec.min_val is not None else 0.0
            hi = spec.max_val
            if num < lo or num > hi:
                errors.append(
                    f"{name}: {num} out of range [{lo}, {hi}] (clamped)"
                )
                num = min(max(num, lo), hi)
            if spec.type == "int":
                num = float(int(round(num)))
            clean[name] = num
        elif spec.type in ("enum", "bool"):
            if spec.options and val not in spec.options:
                errors.append(
                    f"{name}: {val!r} not one of {list(spec.options)}"
                )
                continue  # drop invalid enum/bool → prior value preserved
            clean[name] = val
        else:
            clean[name] = val
    return clean, errors


class KnobState:
    """Transport-agnostic knob state.

    Drop-in replacement for the demo's old ``VirtualMidiKnobs`` class.
    Values come from whatever source the transport adapter wires up
    (WebSocket client params, an MCP command, a VST plugin parameter
    update). The streaming runner reads via ``get_param`` /
    ``get_all_values`` and never knows or cares about the transport.
    """

    def __init__(self, banks):
        self._banks = banks
        self._active_bank = 0
        self._values = {}
        self._all_knobs = {}
        for bank in banks:
            for name, k in bank.knobs.items():
                if name not in self._values:
                    self._values[name] = k.default
                self._all_knobs[name] = k
        self._lock = threading.Lock()

    def update(self, raw: dict):
        """Bulk-update values from a client raw dict."""
        with self._lock:
            self._values.update(raw)

    def add_knob(self, name, knob_def):
        """Register a new knob after construction (used when the client
        enables a LoRA at runtime and we need a ``lora_str_<id>`` slot)."""
        with self._lock:
            if name not in self._values:
                self._values[name] = knob_def.default
            self._all_knobs[name] = knob_def

    def remove_knob(self, name):
        with self._lock:
            self._values.pop(name, None)
            self._all_knobs.pop(name, None)

    def get(self, name: str) -> float:
        with self._lock:
            return self._values.get(name, 0.0)

    def get_all(self) -> dict:
        with self._lock:
            bank = self._banks[self._active_bank]
            return {name: self._values[name] for name in bank.knobs}

    def get_all_values(self) -> dict:
        with self._lock:
            return dict(self._values)

    def get_param(self, name: str) -> float:
        with self._lock:
            return self._values.get(name, 0.0)

    def all_knob_defs(self) -> dict:
        return dict(self._all_knobs)

    @property
    def active_bank_index(self) -> int:
        return self._active_bank

    @property
    def active_bank(self):
        return self._banks[self._active_bank]

    def release(self):
        pass
