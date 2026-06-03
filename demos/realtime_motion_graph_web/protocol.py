"""WebSocket protocol shared by the client, the server, and the full demo.

Torch-free. Only depends on numpy, websockets, and zstandard.
"""

import json
import struct
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

SAMPLE_RATE = 48000
T = 1500  # 60s at 25fps latents
CROSSFADE_SECONDS = 0.025

# Binary slice header: flags, start_sample, num_samples, channels, tick_ms, dec_ms, num_gens
# flags: 0 = raw float16, 1 = zstd-compressed float16 delta
SLICE_HDR_FMT = "<BIIHffI"
SLICE_HDR_SIZE = struct.calcsize(SLICE_HDR_FMT)
SLICE_FLAG_RAW = 0
SLICE_FLAG_DELTA = 1


class RemoteBackend:
    """WebSocket connection to a remote GPU pipeline server.

    Accepts the source audio as either a torch.Tensor with shape
    ``(channels, samples)`` (via ``.numpy()``) or a plain numpy array with
    the same shape. This keeps the thin client torch-free while remaining
    compatible with the full local+remote demo.
    """

    def __init__(self, url: str, waveform, config: dict):
        from websockets.sync.client import connect as ws_connect

        print(f"[Remote] Connecting to {url}...")
        self.ws = ws_connect(url, max_size=50 * 1024 * 1024, open_timeout=30)
        self._send_lock = threading.Lock()

        print("[Remote] Sending config...")
        self.ws.send(json.dumps(config))

        if hasattr(waveform, "numpy"):
            audio_np = waveform.numpy()
        else:
            audio_np = np.asarray(waveform)
        # Normalize to (samples, channels) layout for the wire format.
        # Callers pass (channels, samples); transpose here.
        audio_np = audio_np.T
        hdr = struct.pack("<II", audio_np.shape[1], audio_np.shape[0])
        self.ws.send(hdr + audio_np.astype(np.float32).tobytes())
        print(f"[Remote] Uploaded audio ({audio_np.nbytes / 1024 / 1024:.1f} MB)")

        print("[Remote] Waiting for server init...")
        ready = json.loads(self.ws.recv())
        self.duration = ready["duration"]
        self.channels = ready["channels"]
        self.sample_rate = ready["sample_rate"]
        self.lora_count = ready.get("lora_count", 0)

        init_bytes = self.ws.recv()
        self.initial_buffer = (
            np.frombuffer(init_bytes, dtype=np.float16)
            .astype(np.float32)
            .reshape(-1, self.channels)
        )
        print(f"[Remote] Ready: {self.duration:.1f}s, {self.channels}ch")

    def send_raw(self, raw_dict: dict, playback_pos: float):
        msg = {"type": "params", "raw": raw_dict, "playback_pos": playback_pos}
        try:
            with self._send_lock:
                self.ws.send(json.dumps(msg))
        except Exception:
            pass

    def send_prompt(self, tags: str):
        try:
            with self._send_lock:
                self.ws.send(json.dumps({"type": "prompt", "tags": tags}))
        except Exception:
            pass

    def recv(self, timeout: float = 0.01):
        try:
            msg = self.ws.recv(timeout=timeout)
            if isinstance(msg, bytes) and len(msg) > SLICE_HDR_SIZE:
                hdr = struct.unpack(SLICE_HDR_FMT, msg[:SLICE_HDR_SIZE])
                flags = hdr[0]
                payload = msg[SLICE_HDR_SIZE:]
                if flags == SLICE_FLAG_DELTA:
                    import zstandard as zstd
                    payload = zstd.decompress(payload)
                audio_f16 = np.frombuffer(payload, dtype=np.float16)
                return ("audio", {
                    "flags": flags,
                    "start_sample": hdr[1],
                    "num_samples": hdr[2],
                    "channels": hdr[3],
                    "tick_ms": hdr[4],
                    "dec_ms": hdr[5],
                    "num_gens": hdr[6],
                    "audio": audio_f16.astype(np.float32).reshape(hdr[2], hdr[3]),
                })
            elif isinstance(msg, str):
                return ("json", json.loads(msg))
        except TimeoutError:
            return None
        except Exception:
            return None
        return None

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Wire-contract registry
# ---------------------------------------------------------------------------
# Single source of truth for the WebSocket control vocabulary: every command
# the client may send (client -> server) and every event the server may emit
# (server -> client). Projected into a transport-agnostic catalog served at
# ``GET /api/protocol`` and by the MCP ``describe_protocol`` tool, so a
# re-skinned / vibecoded UI -- or an MCP agent -- builds against this contract
# instead of reverse-engineering ``ws_adapter.py`` and ``web/engine/
# protocol.ts``. Mirrors the role ``acestep/streaming/knobs.py`` plays for
# the parameter surface.
#
# Authoritative, not just descriptive: ``tests/unit/test_wire_contract.py``
# AST-parses the dispatcher and asserts its handled message types are exactly
# ``COMMAND_NAMES``, and that every event the browser client handles appears
# in ``EVENT_NAMES``. So a new verb can't be added to either side without
# being registered here, or the suite fails.
#
# Out of scope here (these are framing, not part of the JSON command/event
# vocabulary): the phase-1 ``config`` handshake (the JSON config + binary PCM
# upload sent before streaming begins; see the ``SessionConfig`` TS interface)
# and the binary audio-slice stream (see ``SLICE_*`` constants above). The
# per-command ``params`` knob payload has its own manifest at ``/api/knobs``.

PROTOCOL_VERSION = 1


@dataclass
class FieldSpec:
    """One field of a command/event JSON payload."""
    name: str
    type: str                 # str | float | int | bool | dict | list | enum
    required: bool = False
    default: Any = None
    options: tuple = ()        # allowed values for enum fields
    description: str = ""


@dataclass
class CommandSpec:
    """One client -> server message type."""
    name: str
    fields: tuple = ()
    binary: bool = False           # a trailing binary audio frame follows
    binary_optional: bool = False  # binary present only in some variants
    origin_sensitive: bool = False  # PRIMARY applies; EXTERNAL (MCP) echoes
    description: str = ""


@dataclass
class EventSpec:
    """One server -> client message type."""
    name: str
    fields: tuple = ()
    binary_follow: bool = False    # one or more binary frames follow the JSON
    description: str = ""


# Reusable framing note for the two binary upload commands.
_PCM_FRAME = (
    "JSON header followed by one binary PCM frame: ``<II`` (channels, samples) "
    "then interleaved float32 at 48 kHz."
)

COMMANDS: tuple = (
    CommandSpec(
        "params",
        fields=(
            FieldSpec("raw", "dict", required=True,
                      description="Knob name -> value map. The payload schema "
                                  "is the separate /api/knobs manifest; values "
                                  "are clamped/validated server-side."),
            FieldSpec("playback_pos", "float", default=0.0,
                      description="Playhead position in SECONDS (not a 0..1 "
                                  "ratio); used for time-keyed curve sampling."),
        ),
        origin_sensitive=True,
        description="Continuous parameter update; sent every tick (~125 Hz) "
                    "carrying the full knob dict.",
    ),
    CommandSpec(
        "loop_band",
        fields=(
            FieldSpec("start_sec", "float",
                      description="Loop start in seconds; null/degenerate clears."),
            FieldSpec("end_sec", "float",
                      description="Loop end in seconds; null/degenerate clears."),
        ),
        origin_sensitive=True,
        description="Arm / move / clear the playback loop band.",
    ),
    CommandSpec(
        "prompt",
        fields=(
            FieldSpec("tags", "str", required=True,
                      description="Prompt A (wire text; enabled-LoRA triggers "
                                  "are prepended client-side)."),
            FieldSpec("tags_b", "str",
                      description="Optional prompt B, cached for A/B blend."),
            FieldSpec("key", "str", description='Musical key, e.g. "C major".'),
            FieldSpec("time_signature", "str",
                      description='Meter numerator, e.g. "3"/"4"/"6".'),
        ),
        origin_sensitive=True,
        description="Re-encode the live prompt (text-encoder pass).",
    ),
    CommandSpec(
        "set_prompt_blend",
        fields=(FieldSpec("value", "float", required=True, default=0.0,
                          description="0.0 = A, 1.0 = B. Clamped to [0,1]."),),
        origin_sensitive=True,
        description="Lerp cached prompt A/B conditioning. Cheap; no encode pass.",
    ),
    CommandSpec(
        "set_interp_method",
        fields=(
            FieldSpec("path", "enum", required=True,
                      options=("prompt", "timbre", "structure", "feedback"),
                      description="Which live blend to retune."),
            FieldSpec("method", "enum", required=True,
                      options=("slerp", "linear"),
                      description="Interpolation curve."),
        ),
        origin_sensitive=True,
        description="Switch a blend path between slerp (norm-preserving) and "
                    "linear.",
    ),
    CommandSpec(
        "set_depth",
        fields=(FieldSpec("value", "int", required=True,
                          description="Target ring depth; clamped to "
                                      "[1, max_pipeline_depth]."),),
        origin_sensitive=True,
        description="Live pipeline_depth retune. Echoed back as depth_applied.",
    ),
    CommandSpec(
        "enable_lora",
        fields=(
            FieldSpec("id", "str", required=True,
                      description="LoRA id/stem (see /api/loras)."),
            FieldSpec("strength", "float",
                      description="Target strength the refit lands at."),
        ),
        origin_sensitive=True,
        description="Enable a LoRA; allocates a lora_str_<id> knob.",
    ),
    CommandSpec(
        "disable_lora",
        fields=(FieldSpec("id", "str", required=True),),
        origin_sensitive=True,
        description="Disable a LoRA and drop its lora_str_<id> knob.",
    ),
    CommandSpec(
        "set_timbre_strength",
        fields=(FieldSpec("value", "float", required=True, default=1.0,
                          description="1.0 = full reference, 0.0 = silence "
                                      "baseline. Clamped to [0,1]."),),
        origin_sensitive=True,
        description="Blend the cached (silence, full) timbre conditioning pair.",
    ),
    CommandSpec(
        "set_timbre_source",
        fields=(FieldSpec("name", "str", default="timbre",
                          description="Label echoed back in timbre_set."),),
        binary=True,
        origin_sensitive=True,
        description="Upload audio as the timbre reference. " + _PCM_FRAME +
                    " Acked by timbre_set / timbre_failed.",
    ),
    CommandSpec(
        "set_timbre_fixture",
        fields=(FieldSpec("name", "str", required=True,
                          description="Fixture name (see /api/fixtures)."),),
        origin_sensitive=True,
        description="Use a server-side fixture as the timbre reference (no "
                    "upload round-trip).",
    ),
    CommandSpec(
        "clear_timbre_source",
        origin_sensitive=True,
        description="Drop the timbre override; fall back to self-timbre. Acked "
                    "by timbre_cleared.",
    ),
    CommandSpec(
        "set_structure_source",
        fields=(FieldSpec("name", "str", default="structure",
                          description="Label echoed back in structure_set."),),
        binary=True,
        origin_sensitive=True,
        description="Upload audio as the structure (semantic-hint) reference. "
                    + _PCM_FRAME + " Acked by structure_set / structure_failed.",
    ),
    CommandSpec(
        "set_structure_fixture",
        fields=(FieldSpec("name", "str", required=True,
                          description="Fixture name (see /api/fixtures)."),),
        origin_sensitive=True,
        description="Use a server-side fixture as the structure reference (no "
                    "upload round-trip).",
    ),
    CommandSpec(
        "clear_structure_source",
        origin_sensitive=True,
        description="Drop the structure override; restore the source's own "
                    "context_latent. Acked by structure_cleared.",
    ),
    CommandSpec(
        "swap_source",
        fields=(
            FieldSpec("tags", "str", description="Optional new prompt A."),
            FieldSpec("key", "str"),
            FieldSpec("time_signature", "str"),
            FieldSpec("fixture_name", "str",
                      description="Source label; for server-side loads, the "
                                  "fixture/upload name to read off the pod's "
                                  "disk."),
            FieldSpec("stem_source_mode", "enum",
                      options=("full", "vocals", "instruments"),
                      description="For uploads: which model-ripped stem feeds "
                                  "inference."),
            FieldSpec("use_server_source", "bool",
                      description="When true, the server loads the named source "
                                  "off its own disk and NO binary frame is "
                                  "sent."),
        ),
        binary=True,
        binary_optional=True,
        origin_sensitive=True,
        description="Replace the playback source in-flight. A binary PCM frame "
                    "follows UNLESS use_server_source is set. Acked by "
                    "swap_ready (+ binary buffer) / swap_failed.",
    ),
)

EVENTS: tuple = (
    EventSpec(
        "ready",
        fields=(
            FieldSpec("duration", "float", required=True),
            FieldSpec("channels", "int", required=True),
            FieldSpec("sample_rate", "int", required=True),
            FieldSpec("lora_catalog", "list"),
            FieldSpec("lora_dir", "str"),
            FieldSpec("bpm", "float"),
            FieldSpec("key", "str"),
            FieldSpec("time_signature", "str"),
            FieldSpec("checkpoint", "str"),
            FieldSpec("checkpoint_scale", "str"),
            FieldSpec("pipeline_depth", "int"),
            FieldSpec("max_pipeline_depth", "int"),
        ),
        binary_follow=True,
        description="First JSON after the upload handshake, followed by the "
                    "binary float16 initial buffer.",
    ),
    EventSpec(
        "error",
        fields=(
            FieldSpec("code", "str"),
            FieldSpec("message", "str"),
            FieldSpec("build_command", "str"),
        ),
        description="Structured init failure (sent during the handshake).",
    ),
    EventSpec(
        "params_update",
        fields=(FieldSpec("params", "dict", required=True,
                          description="Applied params + runtime telemetry "
                                      "(num_gens, tick_ms, dec_ms)."),),
        description="Periodic echo of the live param/telemetry snapshot.",
    ),
    EventSpec(
        "params_echo",
        fields=(FieldSpec("raw", "dict", required=True),),
        description="MCP-driven knob change echoed so the browser mirrors it; "
                    "the browser's own param updates do not echo.",
    ),
    EventSpec(
        "prompt_blend_echo",
        fields=(FieldSpec("value", "float", required=True),),
        description="MCP-driven prompt-blend echo (the dedicated blend slider "
                    "rides its own channel, not params).",
    ),
    EventSpec(
        "prompt_applied",
        fields=(FieldSpec("tags", "str"),),
        description="Ack that a prompt re-encode completed.",
    ),
    EventSpec(
        "lora_catalog",
        fields=(FieldSpec("catalog", "list", required=True),),
        description="Refreshed LoRA catalog after enable/disable.",
    ),
    EventSpec(
        "swap_ready",
        fields=(
            FieldSpec("duration", "float", required=True),
            FieldSpec("channels", "int", required=True),
            FieldSpec("bpm", "float"),
            FieldSpec("key", "str"),
            FieldSpec("time_signature", "str"),
            FieldSpec("fixture_name", "str"),
        ),
        binary_follow=True,
        description="Swap accepted; a binary float16 replacement buffer follows "
                    "the JSON.",
    ),
    EventSpec(
        "swap_failed",
        fields=(FieldSpec("error", "str"),),
        description="Swap rejected (decode/load failure).",
    ),
    EventSpec(
        "stem_assets",
        fields=(
            FieldSpec("fixture_name", "str", required=True),
            FieldSpec("sample_rate", "int", required=True),
            FieldSpec("channels", "int", required=True),
            FieldSpec("frames", "int", required=True),
            FieldSpec("stems", "list", required=True,
                      description='Ordered subset of ("vocals","instruments").'),
            FieldSpec("source_mode", "enum",
                      options=("full", "vocals", "instruments")),
        ),
        binary_follow=True,
        description="Stem-overlay header; one binary float16 buffer per listed "
                    "stem follows, in order.",
    ),
    EventSpec(
        "stem_failed",
        fields=(FieldSpec("fixture_name", "str"), FieldSpec("error", "str")),
        description="Stem extraction/overlay failure.",
    ),
    EventSpec(
        "depth_applied",
        fields=(FieldSpec("value", "int", required=True,
                          description="The clamped applied depth."),),
        description="Ack for set_depth.",
    ),
    EventSpec(
        "timbre_set",
        fields=(FieldSpec("name", "str", required=True),
                FieldSpec("duration", "float", required=True)),
        description="Ack for set_timbre_source / set_timbre_fixture.",
    ),
    EventSpec("timbre_cleared", description="Ack for clear_timbre_source."),
    EventSpec(
        "timbre_failed",
        fields=(FieldSpec("error", "str"),),
        description="Failure ack for any set_timbre_* path.",
    ),
    EventSpec(
        "structure_set",
        fields=(FieldSpec("name", "str", required=True),
                FieldSpec("duration", "float", required=True)),
        description="Ack for set_structure_source / set_structure_fixture.",
    ),
    EventSpec("structure_cleared",
              description="Ack for clear_structure_source."),
    EventSpec(
        "structure_failed",
        fields=(FieldSpec("error", "str"),),
        description="Failure ack for any set_structure_* path, and the notice "
                    'emitted when a swap drops a structure override ("dropped '
                    'after swap: ...").',
    ),
)

COMMAND_NAMES = frozenset(c.name for c in COMMANDS)
EVENT_NAMES = frozenset(e.name for e in EVENTS)


def _field_payload(f: "FieldSpec") -> dict:
    out: dict = {"type": f.type, "required": f.required}
    if f.default is not None:
        out["default"] = f.default
    if f.options:
        out["options"] = list(f.options)
    if f.description:
        out["description"] = f.description
    return out


def command_catalog() -> dict:
    """Project COMMANDS into a transport-agnostic ``name -> spec`` catalog."""
    return {
        c.name: {
            "fields": {f.name: _field_payload(f) for f in c.fields},
            "binary": c.binary,
            "binary_optional": c.binary_optional,
            "origin_sensitive": c.origin_sensitive,
            "description": c.description,
        }
        for c in COMMANDS
    }


def event_catalog() -> dict:
    """Project EVENTS into a transport-agnostic ``name -> spec`` catalog."""
    return {
        e.name: {
            "fields": {f.name: _field_payload(f) for f in e.fields},
            "binary_follow": e.binary_follow,
            "description": e.description,
        }
        for e in EVENTS
    }


def wire_contract() -> dict:
    """Full self-describing WS contract: ``{version, commands, events}``.

    Backs ``GET /api/protocol`` and the MCP ``describe_protocol`` tool. A
    consumer can build (and validate) every control message and decode every
    server event from this alone, modulo the binary framing documented in
    each entry and the ``/api/knobs`` payload for ``params``.
    """
    return {
        "version": PROTOCOL_VERSION,
        "commands": command_catalog(),
        "events": event_catalog(),
    }
