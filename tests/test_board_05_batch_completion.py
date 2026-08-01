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
(``scripts/ci/check_board_05_blocking.py --max-blocking 7``).

This module also guards the #3912 exit-code gate: ``main()`` must call
``evaluate_pipeline_gate(...)`` with ``route_allowance=0`` (a literal ``0``)
so a PARTIAL route makes ``design.py`` exit non-zero instead of silently
shipping a worse board (Issue #4479).
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


def test_completion_runs_exactly_one_pass() -> None:
    """The completion phase must stay inside the 90-min CI job budget.

    What bounds a ``--complete`` pass is the lattice per-link deadline
    (``--per-net-timeout`` x link count, issues #4472/#4501), not
    ``--timeout``: board-05's ~18 stranded links at 60 s each cap ONE pass at
    ~18 min of negotiation.  A fresh regen already spends ~30-40 min before
    this phase, so a second pass would not fit.  Raising the pass count needs
    a CI-measured wall-clock, not a guess (the #3880/#3894 history).
    """
    tree = _design_tree()
    consts: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value.value, int):
                    consts[target.id] = node.value.value

    assert consts.get("_COMPLETION_MAX_PASSES") == 1, (
        "board-05 runs exactly one batch-completion pass; more does not fit "
        "the 90-minute board-05-routing-regression budget (Issue #4476)."
    )
    assert 0 < consts.get("_COMPLETION_PASS_TIMEOUT_S", 0) <= 900


def test_completion_config_keeps_the_board_rescue_knobs() -> None:
    """The completion pass must reuse ``_RESCUE_CONFIG``, not re-declare knobs.

    manufacturer / seed / per-net cutoff / layer stack / micro-via-in-pad are
    load-bearing board-05 history (#3425/#3118/#3880/#3894); the completion
    config is that config plus ``--skip-drc`` (design.py runs its own DRC at
    step 9, so the subprocess's is pure CI-budget overhead).
    """
    tree = _design_tree()
    assign = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_COMPLETION_CONFIG" for t in node.targets)
    )
    source = ast.unparse(assign.value)
    assert source.startswith("replace(_RESCUE_CONFIG"), (
        "_COMPLETION_CONFIG must be derived from _RESCUE_CONFIG via "
        f"dataclasses.replace so the board's knobs cannot drift; got: {source}"
    )
    assert "--skip-drc" in source
    # No re-declaration of the load-bearing knobs.
    for knob in ("manufacturer", "seed", "per_net_timeout_s", "deterministic_budget"):
        assert f"{knob}=" not in source, (
            f"_COMPLETION_CONFIG must not override {knob} (Issue #4476 keeps "
            "board-05's rescue knobs unchanged)."
        )


def test_main_pipeline_gate_pins_route_allowance_zero() -> None:
    """The #3912 exit-code gate must keep ``route_allowance=0`` (a literal 0).

    ``main()`` calls ``evaluate_pipeline_gate(...)`` (design.py ~line 3832)
    with ``route_allowance=0`` so a PARTIAL route makes ``design.py`` exit
    non-zero rather than silently shipping a board worse than the committed
    0-blocking artifact.  Pinning the *literal* ``0`` -- not a variable that
    could be reassigned elsewhere -- turns "verify the gate holds" into a
    durable regression guard: any future edit that loosens the allowance to
    tolerate PARTIAL (a non-zero literal, or a mutable name) fails CI
    immediately (Issue #4479, #3912).
    """
    fn = _main_fn(_design_tree())
    gate_calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "evaluate_pipeline_gate"
    ]
    assert gate_calls, (
        "main() must call evaluate_pipeline_gate() -- the #3912 exit-code gate "
        "that fails design.py on a PARTIAL route (Issue #4479/#3912)."
    )
    for call in gate_calls:
        allowance = next(
            (kw.value for kw in call.keywords if kw.arg == "route_allowance"),
            None,
        )
        assert allowance is not None, (
            "evaluate_pipeline_gate(...) must pass route_allowance explicitly (Issue #4479/#3912)."
        )
        assert isinstance(allowance, ast.Constant) and allowance.value == 0, (
            "evaluate_pipeline_gate(...) must pass route_allowance=0 as a "
            "literal 0 (not a variable that could be silently loosened) so a "
            "PARTIAL route always fails the gate (Issue #4479/#3912). Got: "
            f"{ast.unparse(allowance)}"
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
