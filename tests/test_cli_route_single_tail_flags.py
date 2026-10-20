"""Structural guards for single-tail-only ``kct route`` flags (#7377 step 2).

``--stitch-power-planes`` and ``--export-failed-nets`` must be read only inside
their shared helpers, so a new tail cannot re-grow a private copy that drifts.
Behavioural coverage (flags acting on the default path) lives in
``test_route_entry_path_goldens.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROUTE_CMD = Path(__file__).parent.parent / "src" / "kicad_tools" / "cli" / "route_cmd.py"

#: attribute / getattr key -> the only functions allowed to read it
_ALLOWED = {
    "stitch_power_planes": {"_run_post_save_stitching"},
    "export_failed_nets": {"_export_failed_nets_if_requested"},
}


def _readers() -> dict[str, set[str]]:
    tree = ast.parse(ROUTE_CMD.read_text())
    found: dict[str, set[str]] = {k: set() for k in _ALLOWED}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            key = None
            if isinstance(node, ast.Attribute) and node.attr in _ALLOWED:
                key = node.attr
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _ALLOWED
            ):
                key = node.args[1].value
            if key:
                found[key].add(fn.name)
    return found


def test_flags_read_only_in_shared_helpers() -> None:
    for flag, readers in _readers().items():
        extra = readers - _ALLOWED[flag]
        assert not extra, f"--{flag} read outside its shared helper in: {sorted(extra)}"
        assert readers, f"--{flag} is no longer read anywhere"


def test_every_tail_calls_shared_post_save_and_verdict_helpers() -> None:
    tree = ast.parse(ROUTE_CMD.read_text())
    tails = {
        "route_with_layer_escalation",
        "route_with_rule_relaxation",
        "route_with_combined_escalation",
        "main_with_result",
    }
    calls: dict[str, set[str]] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in tails | {"_run_main_impl"}:
            calls[fn.name] = {
                n.func.id
                for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
    for name in (
        "route_with_layer_escalation",
        "route_with_rule_relaxation",
        "route_with_combined_escalation",
    ):
        assert "_run_post_save_stitching" in calls[name], name
        assert "_run_post_verdict_reports" in calls[name], name
    single = calls.get("_run_main_impl", set())
    assert "_run_post_save_stitching" in single
    assert "_export_failed_nets_if_requested" in single
