# Golden / latency harness

Black-box regression suite for the realtime streaming stack. It drives a
**live server** over the WebSocket wire protocol (never imports server or
app internals), records every frame, and compares the generated audio
against canonical references captured from a baseline build.

Because it is black-box at the protocol boundary, the same suite measures
any server build — including running a checkout of one branch against a
pod serving another. That is the intended workflow for de-risking large
refactors: capture baselines from the pre-refactor build, then point the
identical suite at the refactored build.

## Pieces

| File | Role |
|------|------|
| `client.py` | Recording WS client (slices, swaps, LoRA, transcripts) |
| `scenarios.py` | Declarative scenario table — add regression cases here |
| `runner.py` | Executes scenarios → result bundles (audio + transcript + timing) |
| `compare.py` | Tier-1 byte identity, tier-2 tolerance metrics |
| `refs_store.py` | Reference bundles on the HF dataset, sha256-pinned manifest |
| `refs.json` | The manifest (committed; bundles themselves live off-repo) |
| `test_golden.py` | Audio matches the canonical reference |
| `test_latency.py` | Coarse latency ceilings + per-build report artifact |

## Running

On a local GPU box the suite is self-contained: it spawns the server
itself on a free port and tears it down afterwards.

```bash
python -m tests.golden.refs_store fetch   # pull canonical refs
pytest tests/golden                       # local GPU (auto-spawn)
pytest tests/golden --pod-url ws://POD:1318   # or a remote pod
```

`DEMON_TEST_ACCEL=eager|compile|tensorrt` overrides the spawned
server's backend (default: the server's own default, tensorrt).
Without a pod URL and without CUDA everything skips, so the suite is
inert in CPU-only environments (`pytest tests/unit` is unaffected).

## Capturing new baselines

Only from a build you intend to be the reference (normally `main`), on
the standard pod class:

```bash
# variance probe FIRST: prints the same-build noise floor and the
# suggested per-scenario thresholds for this hardware
python -m tests.golden.runner --pod-url ws://POD:1318 \
    --scenario all --repeat 3

python -m tests.golden.runner --pod-url ws://POD:1318 \
    --scenario all --out runs/baseline
# paste the suggested thresholds into refs.json after packing
python -m tests.golden.refs_store pack --runs runs/baseline
python -m tests.golden.refs_store upload          # HF write token
git add tests/golden/refs.json                    # commit the manifest
```

Thresholds come from the probe's measured noise floor (x3 safety
margin), never from guessing: above the same-build noise, well below
audible breakage.

## Comparison policy

The canonical artifact is a **position-aligned region of the song
buffer**: `[anchor + warmup_skip_s, + canonical_s]`, where the anchor is
where slice coverage started (session start, or the swap point). Slice
segmentation is scheduling-dependent, so only position space is
comparable at all; the warm-up skip and the settle margin trim the
highest-variance stretches.

**Do not expect bit-exactness.** Generation is playhead-paced and
windows are re-emitted as they refine through the pipeline depth, so
wire-level audio carries small timing-coupled variance even between
back-to-back runs on one GPU (measured on a 5090: ~5% of samples, max
abs diff ~0.03, perceptually identical). Engine-level seed determinism
is covered separately by `tests/test_stream.py`. Accordingly:

1. **Tier 1 — identity** is a short-circuit when hashes happen to
   match, never a requirement.
2. **Tier 2 — calibrated tolerance** is the actual contract: log-mel
   distance, RMS level shift, and worst per-second spectral cosine
   against thresholds derived from the measured same-build noise floor.
   `runner --repeat N` prints both the observed floor and suggested
   thresholds (floor x3); put those in `refs.json`.

Actions fire when the **generation frontier** crosses their song
position (not the playhead), so the effect lands inside the compared
region on any machine regardless of its realtime factor.

When driving a remote pod, set `DEMON_SERVER_GPU` to the pod's card so
the captured env (and the identity gate) reflects the server GPU rather
than the harness box's.

## Latency reports

`test_latency.py` asserts only coarse, env-overridable ceilings (the
kind a chunk-latency architectural regression would trip) and writes the
full percentile detail to `runs/latency-reports/latency-<scenario>.json`
for diffing between builds. Treat the diff, not the pass/fail, as the
interesting output.

## Transcripts

Every run records `transcript.jsonl` + `blobs/` — the full wire session
with timestamps. These are the replay inputs for the browser-client
regression tests (driving the real web `RemoteBackend` against recorded
server traffic without a GPU). Keep recording on for baseline captures;
`--no-blobs` exists for quick timing-only runs.

## Adding a scenario

Add one `Scenario` to `scenarios.py`. Keep actions frontier-relative,
leave `deterministic` to the no-actions default, then capture + pack a
reference for it from the baseline build. The drift cost of a scenario
is one entry in `refs.json`; the coverage gain is a whole wire-path.
