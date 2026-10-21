"""Structural guards for single-tail-only ``kct route`` flags (#7377 step 2).

``--stitch-power-planes`` and ``--export-failed-nets`` must be read only inside
their shared helpers, so a new tail cannot re-grow a private copy that drifts.
Behavioural coverage (flags acting on the default path) lives in
``test_route_entry_path_goldens.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

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


# --- Entry-path warnings (#7657) ---------------------------------------------
#
# ``--progressive-clearance`` / ``--bus-routing`` are honoured only by the
# single-attempt path.  The default ``--auto-layers`` entry is covered by
# ``test_route_entry_path_goldens.py``; these cover the two wrapper entries,
# ``--auto-pcb-size`` and ``--auto-mfr-tier``.  The wrappers are stubbed: only
# the warning raised before dispatch is under test, not any routing.

FIXTURE_PCB = Path(__file__).parent / "fixtures" / "route_entry_paths" / "routable.kicad_pcb"


def _run_entry(wrapper: str, entry_flag: str, extra: list[str], tmp_path: Path, capsys) -> str:
    from unittest import mock

    from kicad_tools.cli import route_cmd

    argv = [str(FIXTURE_PCB), "-o", str(tmp_path / "out.kicad_pcb"), "--quiet", entry_flag, *extra]
    with mock.patch.object(route_cmd, wrapper, return_value=0) as stub:
        route_cmd.main(argv)
    stub.assert_called_once()
    return capsys.readouterr().err


@pytest.mark.parametrize(
    ("wrapper", "entry_flag"),
    [
        ("route_with_size_escalation", "--auto-pcb-size"),
        ("route_with_mfr_tier_escalation", "--auto-mfr-tier"),
    ],
)
def test_wrapper_entry_warns_once_for_strategy_flags(wrapper, entry_flag, tmp_path, capsys) -> None:
    err = _run_entry(
        wrapper, entry_flag, ["--progressive-clearance", "--bus-routing"], tmp_path, capsys
    )
    assert err.count("ignored: the escalation ladders") == 1
    assert "--progressive-clearance, --bus-routing ignored" in err


@pytest.mark.parametrize(
    ("wrapper", "entry_flag"),
    [
        ("route_with_size_escalation", "--auto-pcb-size"),
        ("route_with_mfr_tier_escalation", "--auto-mfr-tier"),
    ],
)
def test_wrapper_entry_silent_without_strategy_flags(wrapper, entry_flag, tmp_path, capsys) -> None:
    err = _run_entry(wrapper, entry_flag, [], tmp_path, capsys)
    assert "ignored: the escalation ladders" not in err
