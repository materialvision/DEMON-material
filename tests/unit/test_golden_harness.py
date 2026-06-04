"""GPU-free correctness net for the golden harness itself.

Runs the harness client against a synthetic in-process WebSocket server
that speaks the wire protocol (ready + initial buffer + slices + swap),
so the slice parsing, binary routing, buffer mirroring, canonical-stream
hashing, and tier-1/tier-2 comparison logic are all exercised by plain
``pytest tests/unit``. No torch, no GPU, no pod.
"""

import json
import struct
import threading

import numpy as np
import pytest

from demos.realtime_motion_graph_web.protocol import (
    SLICE_FLAG_DELTA,
    SLICE_FLAG_RAW,
    SLICE_HDR_FMT,
)
from tests.golden.client import GoldenClient
from tests.golden.compare import audio_metrics

CHANNELS = 2
SR = 48000


def _slice_frame(flags: int, start: int, audio_f32: np.ndarray,
                 tick_ms: float = 8.0, dec_ms: float = 11.0,
                 num_gens: int = 1) -> bytes:
    payload = audio_f32.astype(np.float16).tobytes()
    if flags == SLICE_FLAG_DELTA:
        import zstandard as zstd
        payload = zstd.compress(payload)
    hdr = struct.pack(SLICE_HDR_FMT, flags, start, audio_f32.shape[0],
                      CHANNELS, tick_ms, dec_ms, num_gens)
    return hdr + payload


class _FakeServer:
    """Minimal scripted server: config handshake, ready + initial
    buffer, two slices (one RAW, one zstd DELTA), then a swap_ready +
    replacement buffer when the client sends swap_source."""

    def __init__(self):
        rng = np.random.default_rng(7)
        self.initial = rng.standard_normal((SR, CHANNELS)).astype(
            np.float32) * 0.1
        self.swap_buf = rng.standard_normal((SR, CHANNELS)).astype(
            np.float32) * 0.1
        self.raw_slice = rng.standard_normal((1000, CHANNELS)).astype(
            np.float32) * 0.1
        self.delta_slice = rng.standard_normal((1000, CHANNELS)).astype(
            np.float32) * 0.01
        self.received: list = []

    def handler(self, ws):
        cfg = json.loads(ws.recv())
        self.received.append(cfg)
        ws.send(json.dumps({
            "type": "ready", "duration": 1.0, "channels": CHANNELS,
            "sample_rate": SR, "lora_catalog": [], "pipeline_depth": 4,
        }))
        ws.send(self.initial.astype(np.float16).tobytes())
        ws.send(_slice_frame(SLICE_FLAG_RAW, 0, self.raw_slice))
        ws.send(_slice_frame(SLICE_FLAG_DELTA, 500, self.delta_slice))
        # Serve until the client swaps, then ack + close.
        while True:
            msg = ws.recv(timeout=5)
            data = json.loads(msg)
            self.received.append(data)
            if data.get("type") == "swap_source":
                ws.send(json.dumps({
                    "type": "swap_ready", "duration": 1.0,
                    "sample_rate": SR, "channels": CHANNELS,
                    "bpm": None, "key": None, "time_signature": None,
                    "fixture_name": data.get("fixture_name"),
                }))
                ws.send(self.swap_buf.astype(np.float16).tobytes())
                ws.send(_slice_frame(SLICE_FLAG_RAW, 0, self.raw_slice))
                return


@pytest.fixture()
def fake_session(tmp_path):
    from websockets.sync.server import serve

    srv = _FakeServer()
    server = serve(srv.handler, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, f"ws://127.0.0.1:{port}"
    finally:
        server.shutdown()


def test_client_mirrors_protocol(fake_session, tmp_path):
    srv, url = fake_session
    from tests.golden.client import Recorder

    client = GoldenClient(url, recorder=Recorder(tmp_path / "rec"))
    try:
        client.send_config({"fixture_name": "x", "use_server_fixture": True})
        ready = client.wait_ready(timeout=10)
        assert ready["channels"] == CHANNELS
        # Initial buffer mirrored (through the f16 wire round-trip).
        f16 = srv.initial.astype(np.float16).astype(np.float32)
        np.testing.assert_array_equal(client.buffer, f16)

        # Two slices: RAW overwrites, DELTA adds.
        while len(client.slices) < 2:
            assert client.pump(timeout=5), "expected 2 slices"
        raw16 = srv.raw_slice.astype(np.float16).astype(np.float32)
        delta16 = srv.delta_slice.astype(np.float16).astype(np.float32)
        np.testing.assert_array_equal(client.buffer[:500], raw16[:500])
        np.testing.assert_allclose(
            client.buffer[500:1000],
            raw16[500:] + delta16[:500], rtol=0, atol=1e-6)
        assert client.slices[1].flags == SLICE_FLAG_DELTA
        assert client.gen_samples == 2000
        # Overlapping slices merged into one coverage interval.
        assert client.coverage == [[0, 1500]]
        assert client.covered_run_from(100) == (100, 1500)
        assert client.covered_run_from(1500) is None

        # Swap: replacement buffer routed as swap_buffer, not a slice;
        # coverage re-anchors on the new source.
        client.send_swap_to_fixture("other.wav")
        while len(client.slices) < 3:
            assert client.pump(timeout=5), "expected post-swap slice"
        swap16 = srv.swap_buf.astype(np.float16).astype(np.float32)
        np.testing.assert_array_equal(client.buffer[1000:], swap16[1000:])
        assert client.coverage == [[0, 1000]]
        assert any(e.get("type") == "swap_ready"
                   for _, e in client.events)
    finally:
        client.close()

    # Transcript recorded both directions with blob roles.
    lines = [json.loads(ln) for ln in
             (tmp_path / "rec" / "transcript.jsonl")
             .read_text(encoding="utf-8").splitlines()]
    roles = [e.get("role") for e in lines if e["kind"] == "bin"]
    assert roles == ["initial", "slice", "slice", "swap_buffer", "slice"]
    assert any(e["dir"] == "send" and e["kind"] == "json"
               and e["data"].get("type") == "swap_source" for e in lines)


def test_audio_metrics_identity_and_perturbation():
    rng = np.random.default_rng(11)
    ref = rng.standard_normal((SR * 2, CHANNELS)).astype(np.float32) * 0.1
    same = audio_metrics(ref, ref.copy())
    assert same["mel_l2"] == 0.0
    assert same["rms_db_diff"] == 0.0
    assert same["win_cos_min"] == 1.0

    # A gross perturbation must move every metric away from identity.
    bad = ref.copy()
    bad[SR // 2:SR] = 0.0  # half a second of dropout
    diff = audio_metrics(ref, bad)
    assert diff["mel_l2"] > 0.5
    assert diff["win_cos_min"] < 0.9
