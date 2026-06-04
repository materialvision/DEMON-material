"""Golden regression: each scenario's generated audio must match the
canonical reference captured from the baseline build (see refs_store).

A scenario with no manifest entry skips with an explicit reason, so the
suite degrades to "nothing compared" rather than false green when refs
haven't been captured yet.
"""

import json

import pytest

from .compare import compare_bundles
from .refs_store import load_manifest, ref_dir
from .scenarios import SCENARIOS

_NAMES = [s.name for s in SCENARIOS]


@pytest.mark.parametrize("name", _NAMES)
def test_matches_canonical_reference(name, scenario_runs):
    ref = ref_dir(name)
    if ref is None:
        pytest.skip(f"no canonical reference captured for '{name}' yet "
                    f"(refs.json has no entry: see refs_store pack)")

    result, run_dir = scenario_runs(name)
    if result["status"] == "skipped":
        pytest.skip(f"{name}: {result.get('reason')}")
    assert result["status"] == "ok", (
        f"{name} run failed before comparison: "
        f"{result.get('error', result['status'])}")

    entry = load_manifest()["bundles"].get(name) or {}
    report = compare_bundles(ref, run_dir, entry.get("thresholds"))
    (run_dir / "compare.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")

    if report["identical"]:
        return  # tier-1: byte-identical to the baseline (lucky, not owed:
        # generation is playhead-paced + depth-refined, see scenarios.py)

    assert report["passed"], (
        f"{name} diverged from canonical reference (tier 2):\n"
        + "\n".join(f"  - {f}" for f in report["failures"])
        + f"\n  metrics: {report['metrics']}"
        + f"\n  run bundle kept at: {run_dir}")
