"""Boundary guard for the demon-client SDK (packages/demon-client/).

The SDK is the shared client package: the WebSocket session client, the
slice decoder, the audio player, and the wire-contract/knob types. Its
promise is that it has NO dependency on any host app, so any frontend
demo (in-repo or external) can import or package it. These tests keep
that promise true:

* no SDK source may import via a host app's ``@/`` alias (everything
  internal is relative; the only bare import is the ``fzstd`` dependency);
* the worklet asset shipped with the SDK and the copy the rtmg demo app
  serves from ``public/`` must stay byte-identical.

Pure source checks: no node, no bundler, no GPU.
"""

import re
from pathlib import Path

_WEB = (
    Path(__file__).resolve().parents[2]
    / "demos"
    / "realtime_motion_graph_web"
    / "web"
)
_SDK = Path(__file__).resolve().parents[2] / "packages" / "demon-client"

# Directories under the package that hold third-party or generated code,
# not SDK sources: the boundary guard must not scan them.
_SKIP_DIRS = {"node_modules", "dist"}

_IMPORT_RE = re.compile(
    r"""(?:from|import)\s+["']([^"']+)["']|require\(\s*["']([^"']+)["']\s*\)"""
)

# The SDK's only allowed bare (package) import. Everything else must be
# relative. Grows only when the SDK gains a real third-party dependency.
_ALLOWED_BARE_IMPORTS = {"fzstd"}


def _sdk_sources():
    files = sorted(
        p for p in _SDK.rglob("*.ts")
        if not (_SKIP_DIRS & set(p.relative_to(_SDK).parts))
    )
    assert files, f"no SDK sources found under {_SDK}"
    return files


def test_sdk_sources_do_not_import_the_host_app():
    offenders: dict = {}
    for path in _sdk_sources():
        src = path.read_text(encoding="utf-8")
        for m in _IMPORT_RE.finditer(src):
            spec = m.group(1) or m.group(2)
            if spec.startswith("."):
                continue  # relative: stays inside the SDK
            if spec in _ALLOWED_BARE_IMPORTS:
                continue
            offenders.setdefault(str(path.relative_to(_SDK)), []).append(spec)
    assert not offenders, (
        f"SDK sources import outside the package boundary (use relative "
        f"imports, or add a real dependency to _ALLOWED_BARE_IMPORTS): "
        f"{offenders}"
    )


def test_sdk_worklet_matches_served_copy():
    # The canonical worklet source ships with the SDK; the rtmg demo app
    # serves a copy from public/ (Next only serves static assets from
    # there). They must not drift — the worklet's message surface is part
    # of the AudioPlayer contract.
    sdk_copy = (_SDK / "assets" / "audio-worklet.js").read_text(encoding="utf-8")
    served = (_WEB / "public" / "audio-worklet.js").read_text(encoding="utf-8")
    assert sdk_copy.replace("\r\n", "\n") == served.replace("\r\n", "\n"), (
        "packages/demon-client/assets/audio-worklet.js and the rtmg demo's "
        "web/public/audio-worklet.js have drifted — edit one, copy to the "
        "other"
    )


def test_sdk_has_entrypoint_and_manifest():
    assert (_SDK / "index.ts").is_file()
    assert (_SDK / "package.json").is_file()
    assert (_SDK / "README.md").is_file()
