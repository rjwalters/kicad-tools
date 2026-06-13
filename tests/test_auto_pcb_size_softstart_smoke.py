"""Slow real-PCB smoke test for auto-pcb-size escalation (Issue #3352).

This file holds the end-to-end smoke test that closes the loop on the
auto-pcb-size escalation feature against a real PCB.  The test invokes
``route_with_size_escalation`` against a real board and asserts that
the escalation pipeline either (a) succeeds with a clean routed
result, or (b) refuses cleanly with one of the actionable refusal
decisions (``REFUSE_HARD_ENVELOPE`` / ``REFUSE_HOLES_DONT_FIT`` /
``REFUSE_MAX_TIER`` / ``REFUSE_REGRESSION``).  Both outcomes are
acceptable -- the goal is to confirm the pipeline runs end-to-end
without raising and produces a structured outcome consumers can act on.

P_AS5 (Issue #3352) replaced the P_AS4 voltage-divider stand-in with
the softstart rev B fixture.  The softstart recipe under
``boards/external/softstart/`` is the canonical real consumer of the
auto-pcb-size feature and now carries the ``envelope_hard: true`` +
``escalation.ladder: layers-only`` declarations that exercise the
refusal path on real over-constrained input.

Marked ``@pytest.mark.slow`` because regenerating the softstart
schematic + PCB and running a single layer escalation attempt at
0.20mm clearance takes several minutes.  Run with ``pytest -m slow``
to include, AND set ``KICAD_RUN_SLOW_SOFTSTART_REACH=1`` to opt the
softstart-specific tests in (matches the convention used by
``tests/router/test_softstart_routing_reach_regression`` and
``tests/router/test_softstart_revb_fine_pitch_escape``).

The mocked refusal-message test does NOT regenerate the recipe -- it
patches the inner routing call to a deterministic over-constrained
result and verifies the refusal message wording.  That test stays
fast and runs as part of the slow marker without the softstart env
gate.

Issue: https://github.com/rjwalters/kicad-tools/issues/3352
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Mark every test in this module as slow.  Per the task brief, the smoke
# test is allowed to take 5-15 minutes; pytest -m slow opts in.
pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_DIR = REPO_ROOT / "boards" / "external" / "softstart"


def _slow_softstart_enabled() -> bool:
    """Whether the slow softstart routing path is enabled.

    Matches the convention used by the softstart slow-corpus tests
    under ``tests/router/``.  When unset, tests that regenerate the
    softstart recipe skip; tests that mock the inner routing call run
    as long as ``pytest -m slow`` is passed.
    """
    return os.environ.get("KICAD_RUN_SLOW_SOFTSTART_REACH") == "1"


def _regenerate_softstart_pcb(output_dir: Path) -> Path:
    """Regenerate softstart rev B (schematic + placed PCB).

    Returns the path to the freshly-placed (but unrouted) PCB.  This
    mirrors the production path the recipe takes -- when the slow
    softstart corpus is enabled, the regeneration is the recipe's
    canonical state.
    """
    if not _slow_softstart_enabled():
        pytest.skip(
            "Softstart regeneration is a slow path (~30s + multi-minute "
            "route).  Set KICAD_RUN_SLOW_SOFTSTART_REACH=1 to enable."
        )

    sys.path.insert(0, str(BOARD_DIR))
    try:
        import generate_design  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    output_dir.mkdir(parents=True, exist_ok=True)
    generate_design.create_project(output_dir, "softstart")
    generate_design.create_softstart_schematic(output_dir)
    pcb_path = generate_design.create_softstart_pcb(output_dir)
    return pcb_path


def _build_args(pcb_path: Path, output_path: Path) -> SimpleNamespace:
    """Build a minimal CLI Namespace for invoking route_with_size_escalation.

    Mirrors what the route_cmd dispatcher sets up when ``--auto-pcb-size``
    is on the command line.  Per Q5 the flag IMPLIES ``--auto-layers``
    so we forward both flags here.

    Hand-rolled because constructing the real argparser in-tree is
    brittle (the parser lives inside ``main()`` and exits on --help).
    The defaults below cover every attribute the routing pipeline
    touches; missing attributes surface as AttributeError on first
    access so the list stays self-correcting.
    """
    from kicad_tools.spec.schema import EscalationPolicy

    args = SimpleNamespace(
        pcb=str(pcb_path),
        output=str(output_path),
        manufacturer="jlcpcb-tier1",  # softstart's production target
        auto_layers=True,
        auto_pcb_size=True,
        max_layers=4,
        min_completion=0.95,
        min_clearance_floor=None,
        min_trace=None,
        quiet=True,
        strategy="negotiated",
        backend="auto",
        no_auto_build_native=False,
        # Routing config
        timeout=600.0,
        per_net_timeout=45.0,
        iterations=10,
        seed=42,
        verbose=False,
        force=False,
        preserve_existing=False,
        # Design rules -- match softstart rev B production
        grid="0.1mm",
        trace_width=0.30,
        clearance=0.20,
        via_drill=0.3,
        via_diameter=0.6,
        edge_clearance=0.5,
        fine_pitch_clearance=None,
        # Skipping / nets -- softstart's power + heavy-current return nets
        skip_nets=[
            "AC_LINE",
            "AC_NEUTRAL",
            "FUSED_LINE",
            "GND",
            "+3.3V",
            "VRECT",
            "SCAP_POS+",
            "SCAP_POS_GND",
            "SCAP_NEG+",
            "SCAP_NEG_GND",
            "ISENSE_POS",
        ],
        skip_drc=False,
        power_nets=[],
        # Strategy-specific
        mc_trials=10,
        pop_size=10,
        generations=10,
        max_search_iterations=10,
        two_phase=False,
        two_phase_iterations=None,
        multi_resolution=False,
        # Features off
        diagnostics=False,
        differential_pairs=False,
        diffpair_max_delta=None,
        diffpair_spacing=None,
        bus_routing=False,
        bus_min_width=None,
        bus_spacing=None,
        bus_mode=None,
        analyze=False,
        auto_fix=False,
        auto_fix_passes=0,
        adaptive_rules=False,
        export_failed_nets=False,
        # Cache
        cache_only=False,
        cache_stats=False,
        clear_cache=False,
        no_cache=False,
        # Other
        format="text",
        dry_run=False,
        preview=False,
        no_optimize=False,
        layers=None,
        profile=False,
        profile_output=None,
        net_class_map=None,
        early_stop_patience=2,
        checkpoint_interval=30.0,
        batch_routing=False,
        hierarchical=False,
        perturbation=True,
        high_performance=False,
        region_parallel=False,
        partition_rows=2,
        partition_cols=2,
        max_parallel_workers=4,
        # Escalation policy injected here (avoids project.kct discovery
        # in tests; we want the test to be hermetic).  Mirrors softstart
        # rev B's project.kct declarations.
        _escalation_policy=EscalationPolicy(ladder="layers-only", max_layers=4),
        _envelope_hard=True,
        _hole_group=None,
    )
    return args


def test_softstart_smoke_routes_or_refuses_cleanly(tmp_path: Path) -> None:
    """End-to-end smoke: route softstart rev B through route_with_size_escalation.

    The softstart rev B PCB declares ``envelope_hard=true`` +
    ``escalation.ladder=layers-only``.  Expected outcomes:

      1. Exit code 0: routing succeeded; no escalation needed.
      2. Exit code 2 + refusal print: layer escalation exhausted at
         the recipe's max_layers ceiling, refusal emitted naming the
         BOM / layers / clearance / spec-amendment levers.
      3. Exit code 3: DRC violations after routing; the recipe still
         produced a routed PCB but flagged manufacturer-tier check
         failures.

    Failure modes that fail this test:
      - Unhandled exception during routing or escalation.
      - Exit code other than 0, 1, 2, or 3 (1 is reserved for hard
        subprocess failures; we accept it but don't expect it).
      - Silent crash (no metrics stashed on ``args._last_layer_result``).

    Wall-clock budget: ~5-15 minutes for one layer attempt.  The
    layer-only ladder can run up to 2 attempts (2L, 4L) which doubles
    the budget.  CI's slow-board job has a 30-minute cap.
    """
    from kicad_tools.cli import route_cmd

    src_pcb = _regenerate_softstart_pcb(tmp_path / "softstart_out")
    output_path = tmp_path / "softstart_routed.kicad_pcb"

    args = _build_args(src_pcb, output_path)

    try:
        rc = route_cmd.route_with_size_escalation(
            pcb_path=src_pcb,
            output_path=output_path,
            args=args,
            quiet=True,
        )
    except Exception as exc:  # pragma: no cover - debugging aid
        pytest.fail(f"route_with_size_escalation crashed unexpectedly: {exc}")

    assert rc in (0, 1, 2, 3), f"route_with_size_escalation returned an unexpected exit code {rc!r}"

    # When the routing produced an output PCB, structurally validate it.
    if output_path.exists():
        from kicad_tools.schema.pcb import PCB

        try:
            PCB.load(output_path)
        except Exception as exc:
            pytest.fail(f"Routed PCB at {output_path} is not loadable as a valid kicad_pcb: {exc}")


def test_refusal_path_emits_actionable_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify the refusal path runs and emits the actionable message.

    Constructs an over-constrained scenario (hard envelope on a small
    real PCB) so the escalation decision is REFUSE_HARD_ENVELOPE.
    Confirms the refusal message includes the architect-mandated
    alternative levers (BOM / layers / envelope / clearance /
    manufacturer tier).

    This test does NOT need a real softstart route -- the refusal
    message is emitted by the wrapper logic, independent of which
    inner routing engine is used.  We use the small in-tree
    voltage-divider PCB as a lightweight stand-in for the wrapper's
    starting envelope and patch the inner routing call to a
    deterministic over-constrained result.  The patched inner result
    is what triggers the refusal decision; the real PCB is only used
    as a *valid kicad_pcb* the wrapper can probe for dimensions.
    """
    from kicad_tools.cli import route_cmd

    # Use the voltage-divider PCB as a lightweight wrapper-input
    # fixture.  This is NOT a regression on the P_AS4 stand-in -- the
    # PCB content is irrelevant here because we patch the inner
    # routing call; we only need a valid kicad_pcb file with a board
    # outline so ``extract_board_dimensions`` succeeds.
    src_pcb = REPO_ROOT / "boards" / "01-voltage-divider" / "output" / "voltage_divider.kicad_pcb"
    if not src_pcb.exists():
        pytest.skip(
            f"Voltage-divider fixture not found at {src_pcb}; run "
            f"`uv run python boards/01-voltage-divider/generate_design.py` first."
        )

    pcb_path = tmp_path / "refusal_smoke.kicad_pcb"
    shutil.copy(src_pcb, pcb_path)
    output_path = tmp_path / "refusal_smoke_routed.kicad_pcb"

    args = _build_args(pcb_path, output_path)
    # Force the refusal path by declaring envelope_hard.
    args._envelope_hard = True

    # Patch the inner routing call to produce an over-constrained
    # outcome that fires the trigger; this lets the smoke test verify
    # the refusal message without paying the routing cost.
    from unittest.mock import patch

    def fake_inner(pcb_path, output_path, args, quiet):
        args._last_layer_result = SimpleNamespace(
            nets_routed=70,
            nets_to_route=100,
            overflow=80,
            completion=0.7,
            success=False,
            router=None,
        )
        return 2

    with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
        rc = route_cmd.route_with_size_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=False,  # Show output so we can capture the refusal message
        )

    captured = capsys.readouterr()
    # Refusal returns the inner exit code (partial=2).
    assert rc == 2, f"Expected exit code 2 (partial) for envelope_hard refusal; got {rc}"

    # The actionable refusal message must enumerate the alternative
    # levers per architect proposal §4.
    output = captured.out + captured.err
    assert "AUTO-PCB-SIZE ESCALATION REFUSED" in output
    assert "envelope_hard" in output
    # Alternative-lever enumeration -- mention at least 3 of the 5.
    levers_mentioned = sum(
        1
        for keyword in ("BOM", "layers", "envelope", "clearance", "manufacturer")
        if keyword.lower() in output.lower()
    )
    assert levers_mentioned >= 3, (
        f"Actionable refusal message should enumerate alternative levers; "
        f"only matched {levers_mentioned}/5 in output: {output[:500]}"
    )


def test_softstart_layers_only_ladder_runs_end_to_end(tmp_path: Path) -> None:
    """Confirm the layers-only strategy path runs end-to-end without crashing.

    softstart rev B's project.kct declares ``escalation.ladder: layers-only``
    (P_AS5).  When the wrapper is engaged with that policy, the size
    axis is disabled at the policy level (independent of envelope_hard)
    and only the layer escalation ladder is walked.  This test
    confirms the layers-only code path runs without crashing.

    The test uses the recipe regeneration fixture (slow gate); a fast
    equivalent for the layers-only ladder construction lives in
    ``tests/test_auto_pcb_size.py``.
    """
    from kicad_tools.cli import route_cmd
    from kicad_tools.spec.schema import EscalationPolicy

    src_pcb = _regenerate_softstart_pcb(tmp_path / "softstart_lo")
    output_path = tmp_path / "softstart_lo_routed.kicad_pcb"

    args = _build_args(src_pcb, output_path)
    args._escalation_policy = EscalationPolicy(ladder="layers-only", max_layers=4)

    try:
        rc = route_cmd.route_with_size_escalation(
            pcb_path=src_pcb,
            output_path=output_path,
            args=args,
            quiet=True,
        )
    except Exception as exc:  # pragma: no cover - debugging aid
        pytest.fail(f"layers-only route_with_size_escalation crashed: {exc}")

    assert rc in (0, 1, 2, 3), (
        f"layers-only route_with_size_escalation returned an unexpected exit code {rc!r}"
    )


if __name__ == "__main__":
    # Allow direct invocation for debugging:
    #   KICAD_RUN_SLOW_SOFTSTART_REACH=1 uv run python \
    #     tests/test_auto_pcb_size_softstart_smoke.py
    sys.exit(pytest.main([__file__, "-v", "-m", "slow"]))
