"""Static-site serving shared by the demo backends.

Standalone no-build demos (e.g. ``demos/arp``) are plain directories of
HTML/JS/CSS served from the same port as the WebSocket backend so the
page and the WS share one origin. This module owns that serving logic so
the realtime-motion-graph server (or any future backend host) mounts a
demo with one table entry instead of growing bespoke route code per demo.

A demo opts in by dropping a ``demo.static.json`` manifest in its
directory::

    {"route": "/arp"}

:func:`discover_static_demos` scans ``demos/*/demo.static.json`` and
returns the mount table; :func:`serve_static_mounts` resolves a request
path against it. The shared demon-client browser bundle
(``packages/demon-client/dist``, see its ``build.mjs``) is mounted at
:data:`SDK_ROUTE` so every static demo loads ONE copy of the SDK /
slice-decoder worker / audio worklet instead of vendoring its own.

Depends only on the ``websockets`` library — no torch, no acestep — so
``--no-backend`` UI-only servers can import it for free.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.parse
from pathlib import Path

from websockets.datastructures import Headers
from websockets.http11 import Response

# Windows' registry can map .js to a non-JS MIME type (e.g. text/plain),
# which makes browsers refuse to evaluate <script type="module">. Force
# the correct types so static-demo ES modules load everywhere.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Where the shared demon-client browser bundle is mounted. Static demos
# reference "/sdk/demon-client.js", "/sdk/sliceDecoder.worker.js" and
# "/sdk/audio-worklet.js" against the backend origin.
SDK_ROUTE = "/sdk"

# Route prefixes owned by the backend's API/asset endpoints. A static
# demo manifest may not claim them.
RESERVED_ROUTES = ("/api", "/fixtures", "/user_uploads", "/videos", SDK_ROUTE)

_MANIFEST_NAME = "demo.static.json"

_NO_CACHE_HEADERS = [
    ("Cache-Control", "no-store, must-revalidate"),
    ("Pragma", "no-cache"),
    ("Expires", "0"),
]


def sdk_dist_dir() -> Path:
    """The committed demon-client browser bundle directory."""
    return _REPO_ROOT / "packages" / "demon-client" / "dist"


def discover_static_demos(demos_root: Path | None = None) -> dict:
    """Scan ``demos/*/demo.static.json`` and return ``{route: directory}``.

    Malformed manifests and reserved/duplicate routes raise immediately:
    a typo'd mount should fail the server at boot, not 404 mysteriously.
    """
    if demos_root is None:
        demos_root = _REPO_ROOT / "demos"
    mounts: dict = {}
    for manifest in sorted(demos_root.glob(f"*/{_MANIFEST_NAME}")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        route = data.get("route")
        if not isinstance(route, str) or not route.startswith("/") or route == "/":
            raise ValueError(f"{manifest}: route must be a non-root '/...' string")
        route = route.rstrip("/")
        if any(route == r or route.startswith(r + "/") for r in RESERVED_ROUTES):
            raise ValueError(f"{manifest}: route {route!r} collides with {RESERVED_ROUTES}")
        if route in mounts:
            raise ValueError(f"{manifest}: route {route!r} already claimed by {mounts[route]}")
        mounts[route] = manifest.parent.resolve()
    return mounts


def _file_response(target: Path) -> Response:
    try:
        body = target.read_bytes()
    except OSError as e:
        msg = f"500 {e}\n".encode()
        return Response(
            500, "Internal Server Error",
            Headers([
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(msg))),
                *_NO_CACHE_HEADERS,
            ]),
            msg,
        )
    # ES modules must be served with a JS MIME type or the browser
    # refuses to evaluate them; guess_type covers .js/.css/.html/.wav
    # (with the .js override forced at module import above).
    ctype, _ = mimetypes.guess_type(target.name)
    return Response(
        200, "OK",
        Headers([
            ("Content-Type", ctype or "application/octet-stream"),
            ("Content-Length", str(len(body))),
            *_NO_CACHE_HEADERS,
        ]),
        body,
    )


def serve_static_site(path_only: str, route: str, root: Path) -> Response | None:
    """Serve ``path_only`` from ``root`` mounted at ``route``.

    ``route`` is slash-prefixed with no trailing slash (``"/arp"``). Bare
    ``route`` 301-redirects to ``route + "/"`` (otherwise the page's
    relative asset URLs resolve against ``/`` and break); ``route + "/"``
    maps to ``index.html``; everything else is a path-escape-guarded file
    lookup inside ``root``. Returns ``None`` when the path doesn't match
    the mount or names no file, so the caller can fall through to its 404.
    """
    root = root.resolve()
    if path_only == route:
        return Response(
            301, "Moved Permanently",
            Headers([
                ("Location", route + "/"),
                ("Content-Length", "0"),
                *_NO_CACHE_HEADERS,
            ]),
            b"",
        )
    if path_only == route + "/":
        target = root / "index.html"
    elif path_only.startswith(route + "/"):
        rel = urllib.parse.unquote(path_only[len(route) + 1:])
        candidate = (root / rel).resolve()
        # Reject anything that escapes root (symlink/`..` traversal).
        target = candidate if str(candidate).startswith(str(root)) else None
    else:
        return None
    if target is None or not target.is_file():
        return None
    return _file_response(target)


def serve_static_mounts(path_only: str, mounts: dict) -> Response | None:
    """Resolve ``path_only`` against a ``{route: directory}`` table."""
    for route, root in mounts.items():
        resp = serve_static_site(path_only, route, root)
        if resp is not None:
            return resp
    return None
