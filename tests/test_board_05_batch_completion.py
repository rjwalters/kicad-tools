"""Regression guard: board-05 design.py runs a BATCH completion pass (#4476).

Issue #4476: board-05's step 6b only ran the **solo** rescue loop
(:func:`kicad_tools.router.partial_rescue.rescue_partial_nets`), which
reroutes each residual net ALONE against every other net's preserved,
immutable copper.  In the saturated U3 south sense band that is blocked by
non-rippable copper by construction, so a fresh regen logged
``Rescue ISENSE_A+: FAILED (no output produced)`` for the whole stranded
cohort (ISENSE_A+/A-/B+/B-/C-, PWM_BH, PWM_CH).

The recipe now layers the shared BATCH pass
(:func:`kicad_tools.router.partial_rescue.complete_unfinished_nets`, which
shells ``kct route --complete`` on the lattice engine) on top of the solo
loop, so the unfinished cohort can still negotiate with itself while the
finished nets stay protected.

These are cheap static (AST) guards -- KiCad-independent, no routing -- that
pin the wiring and its ordering.  The routing outcome itself is asserted by
the ``board-05-routing-regression`` CI job
(``scripts/ci/check_board_05_blocking.py --max-blocking 11``).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DESIGN_PY = REPO_ROOT / "boards" / "05-bldc-motor-controller" / "design.py"


def _design_tree() -> ast.Module:
    return ast.parse(DESIGN_PY.read_text())


def _main_fn(tree: ast.Module) -> ast.FunctionDef:
    return next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "main"
    )


def _first_call_lines(fn: ast.FunctionDef) -> dict[str, int]:
    lines: dict[str, int] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            lines.setdefault(node.func.id, node.lineno)
    return lines


def test_design_imports_the_shared_batch_completion() -> None:
    """The batch pass must come from the shared library, not a recipe copy."""
    tree = _design_tree()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == (
            "kicad_tools.router.partial_rescue"
        ):
            imported.update(alias.name for alias in node.names)

    assert "complete_unfinished_nets" in imported, (
        "design.py must import complete_unfinished_nets from "
        "kicad_tools.router.partial_rescue (Issue #4476) -- the no-stub / "
        "no-short rollback guarantee lives in the shared function and must "
        "not be re-implemented in the recipe."
    )


def test_design_defines_completion_wrapper() -> None:
    tree = _design_tree()
    funcs = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "complete_unfinished_nets" in funcs, (
        "design.py must define a complete_unfinished_nets() wrapper carrying "
        "this board's rescue knobs (Issue #4476)."
    )


def test_main_runs_batch_completion_after_solo_rescue_and_before_stitch() -> None:
    """Ordering is load-bearing.

    The batch pass must see whatever the solo loop could close (so it runs
    after ``rescue_partial_nets``) and its copper must be present before the
    pour stitching / zone fill / DRC tail (so it runs before ``stitch_pcb``).
    """
    lines = _first_call_lines(_main_fn(_design_tree()))

    assert "complete_unfinished_nets" in lines, (
        "main() must call complete_unfinished_nets() as part of step 6b (Issue #4476)."
    )
    assert "rescue_partial_nets" in lines
    assert "stitch_pcb" in lines
    assert lines["rescue_partial_nets"] < lines["complete_unfinished_nets"] < lines["stitch_pcb"], (
        "complete_unfinished_nets() must run AFTER rescue_partial_nets() and "
        "BEFORE stitch_pcb(). Current order:\n"
        f"  rescue_partial_nets      @ line {lines['rescue_partial_nets']}\n"
        f"  complete_unfinished_nets @ line {lines['complete_unfinished_nets']}\n"
        f"  stitch_pcb               @ line {lines['stitch_pcb']}"
    )


def test_completion_budget_fits_the_ci_wall_clock_envelope() -> None:
    """The completion phase must stay well inside the 90-min CI job budget.

    A fresh board-05 regen already spends ~45-55 min on the main pass plus
    the solo rescue loop, so the batch phase's worst case
    (``_COMPLETION_MAX_PASSES * _COMPLETION_PASS_TIMEOUT_S``) is capped at 15
    minutes of routing.  Raising it needs a CI-measured wall-clock, not a
    guess (#3880/#3894 history).
    """
    tree = _design_tree()
    consts: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value.value, int):
                    consts[target.id] = node.value.value

    assert "_COMPLETION_MAX_PASSES" in consts
    assert "_COMPLETION_PASS_TIMEOUT_S" in consts
    worst_case_s = consts["_COMPLETION_MAX_PASSES"] * consts["_COMPLETION_PASS_TIMEOUT_S"]
    assert 0 < worst_case_s <= 900, (
        f"Batch completion worst case is {worst_case_s}s; the board-05 CI job "
        "budget (90 min total, ~45-55 min already spent) does not have room "
        "for more than ~15 min of completion routing."
    )


def test_design_does_not_enable_escape_corridor_reservation() -> None:
    """#4519 gates the corridor-reservation opt-in; #4476 must not sneak it in.

    ``--escape-corridor-reservation`` regressed this board's blocking gate
    (11 -> 13) when it was opted in during PR #4509, so it stays OFF until
    #4519 lands measured-safe selectivity.
    """
    text = DESIGN_PY.read_text()
    assert "--escape-corridor-reservation" not in text
    assert "escape_corridor_reservation" not in text
