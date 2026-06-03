"""Drift guard for the WebSocket wire-contract registry.

The registry in ``demos/realtime_motion_graph_web/protocol.py`` is the single
source of truth for the command/event vocabulary served at ``/api/protocol``
and by the MCP ``describe_protocol`` tool. These tests fail if the ws_adapter
dispatcher or the browser protocol client grows a message type the registry
doesn't declare (or vice versa) — so a new verb cannot be added on either side
without registering it in the contract.

Pure source parsing (AST for the Python dispatcher, regex for the TS client):
no torch, no GPU, no running server.
"""

import ast
import re
from pathlib import Path

from demos.realtime_motion_graph_web.protocol import (
    COMMAND_NAMES,
    EVENT_NAMES,
    command_catalog,
    event_catalog,
    wire_contract,
)

_DEMO = (
    Path(__file__).resolve().parents[2]
    / "demos"
    / "realtime_motion_graph_web"
)


def _dispatcher_command_types() -> set:
    """Every ``mtype == "<x>"`` literal in ws_adapter's dispatcher.

    AST-parsed from source so we never import ws_adapter (which pulls the
    torch-heavy engine). Matches the ``if/elif mtype == "..."`` chain that
    routes each client message to a session call.
    """
    src = (_DEMO / "ws_adapter.py").read_text(encoding="utf-8")
    found = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "mtype"):
            continue
        for comp in node.comparators:
            if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                found.add(comp.value)
    return found


def _client_handled_events() -> set:
    """Every ``msg.type === "<x>"`` the browser protocol client handles."""
    src = (_DEMO / "web" / "engine" / "protocol.ts").read_text(encoding="utf-8")
    return set(re.findall(r'msg\.type === "([^"]+)"', src))


def test_commands_match_dispatcher_exactly():
    # Every command the registry declares must be handled by the dispatcher,
    # and every type the dispatcher handles must be in the registry.
    assert _dispatcher_command_types() == set(COMMAND_NAMES)


def test_client_events_are_declared():
    handled = _client_handled_events()
    assert handled, "regex found no msg.type === handlers — pattern drifted?"
    missing = handled - set(EVENT_NAMES)
    assert not missing, (
        f"events handled by the browser client but absent from the wire "
        f"contract registry: {sorted(missing)}"
    )


def test_catalog_projection_shapes():
    wc = wire_contract()
    assert wc["version"] >= 1
    assert set(wc["commands"]) == set(COMMAND_NAMES)
    assert set(wc["events"]) == set(EVENT_NAMES)

    cmds = command_catalog()
    # required-field flag and binary framing survive the projection
    assert cmds["prompt"]["fields"]["tags"]["required"] is True
    assert cmds["set_timbre_source"]["binary"] is True
    assert cmds["swap_source"]["binary_optional"] is True
    assert cmds["params"]["origin_sensitive"] is True

    evts = event_catalog()
    assert evts["ready"]["binary_follow"] is True
    assert evts["timbre_cleared"]["fields"] == {}
