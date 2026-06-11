"""Does a higher builder optimization level dodge the sub-5s encode
Myelin crash? Sweep opt levels at the 1s static encode shape.

Context (build_windowed_encoder.py): sub-5s encode shapes emit a broken
Myelin fusion at opt level 1 on TRT 10.16 / 5090 -- static profiles
access-violate at create_execution_context, and level 0 builds run but
are ~7x slower. The level-1 default itself was chosen to dodge an
*earlier* large-shape segfault, so the sub-5s crash could be a
different fusion that a higher level happens to avoid. If level 2 or 3
both loads AND runs fast at 1s, a true 1s encode engine is on the
table.

Each level is built + loaded + executed in a SEPARATE subprocess, so a
hard access violation (which kills the process) is caught and recorded
rather than taking the whole sweep down.

Run:
    .venv/Scripts/python.exe scripts/experiments/realtime_input/opt_level_sweep.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = next(
    p for p in (_HERE, *_HERE.parents) if (p / "pyproject.toml").exists()
)

# Child: build a 1s static encode engine at a given opt level, then try
# to load + execute it. Prints a single JSON line. A native crash kills
# the child with a nonzero/!0 returncode and no JSON -> parent records
# "CRASH".
CHILD = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, r"{repo}")
LEVEL = {LEVEL}
import torch
from acestep.engine.trt.vae_export import VAETRTBuildConfig, build_vae_encode_engine
from acestep.paths import trt_engines_dir
from acestep.nodes.vae_nodes import _get_trt_vae, _get_trt_stream

trt_dir = Path(trt_engines_dir())
onnx = trt_dir / "_onnx_vae" / "vae_encode" / "vae_encode.onnx"
name = f"_sweep_vae_encode_1s_o{{LEVEL}}"
ep = trt_dir / name / f"{{name}}.engine"
cfg = VAETRTBuildConfig(
    workspace_gb=8.0,
    encode_min_samples=48000, encode_opt_samples=48000, encode_max_samples=48000,
    builder_optimization_level=LEVEL,
)
t0 = time.time()
build_vae_encode_engine(onnx, ep, config=cfg)
build_s = time.time() - t0
sys.stderr.write(f"BUILT level {{LEVEL}} in {{build_s:.1f}}s\n"); sys.stderr.flush()

device = torch.device("cuda")
torch.zeros(1, device=device)
entry = _get_trt_vae(str(ep), device)   # <-- static crash point
ctx = entry["context"]; stream = _get_trt_stream()
dt = entry["tensor_dtypes"]

def enc(x):
    ctx.set_input_shape("audio", tuple(x.shape))
    ctx.set_tensor_address("audio", x.data_ptr()); ctx.infer_shapes()
    buf = torch.empty(tuple(ctx.get_tensor_shape("moments")),
                      dtype=dt.get("moments", torch.float32), device=device)
    ctx.set_tensor_address("moments", buf.data_ptr())
    ok = ctx.execute_async_v3(stream.ptr); stream.synchronize()   # <-- dyn crash point
    return buf, ok

x = torch.randn(1, 2, 48000, device=device)
_b, ok = enc(x)
for _ in range(5): enc(x)
lat = []
for _ in range(50):
    t1 = time.perf_counter(); enc(x); lat.append((time.perf_counter()-t1)*1000)
lat.sort()
print(json.dumps({{
    "level": LEVEL, "status": "OK", "build_s": round(build_s, 1),
    "exec_ok": bool(ok), "p50_ms": round(lat[25], 3),
    "engine": str(ep),
}}))
'''


def main() -> int:
    rows = []
    for level in (0, 2, 3):
        print(f"[sweep] building+testing 1s encode at opt level {level} ...",
              flush=True)
        proc = subprocess.run(
            [sys.executable, "-c", CHILD.format(repo=str(_REPO_ROOT), LEVEL=level)],
            capture_output=True, text=True,
        )
        line = next(
            (ln for ln in reversed(proc.stdout.splitlines())
             if ln.strip().startswith("{")),
            None,
        )
        if line:
            row = json.loads(line)
        else:
            tail = (proc.stderr.strip().splitlines() or ["<no stderr>"])[-3:]
            row = {
                "level": level,
                "status": "CRASH",
                "returncode": proc.returncode,
                "stderr_tail": tail,
            }
        rows.append(row)
        if row["status"] == "OK":
            print(f"  level {level}: OK  build={row['build_s']}s  "
                  f"p50={row['p50_ms']}ms", flush=True)
        else:
            print(f"  level {level}: CRASH (rc={row['returncode']})  "
                  f"{row['stderr_tail'][-1]}", flush=True)

    out = (_REPO_ROOT / "runs" / "realtime-input"
           / f"opt-level-sweep-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"shape": "1s static encode", "rows": rows},
                              indent=2), encoding="utf-8")
    print(f"[report] {out}")
    # Reference: level 1 at this shape access-violates (prior run).
    print("[note] level 1 at 1s static = CRASH (established earlier); "
          "level 0 = OK but ~6.4ms/call (slow kernels)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
