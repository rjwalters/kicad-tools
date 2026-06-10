"""Softstart rev B fine-pitch escape end-to-end consumer test (Issue #3371 P_FP4/P_FP5).

This is the heavyweight consumer test that closes Phase 4 of the
fine-pitch escape ladder.  It:

  1. Regenerates the softstart rev B schematic + PCB on demand (via
     the in-tree recipe ``boards/external/softstart/generate_design.py``).
  2. Drives the routing pipeline in-process with the manufacturing
     recipe (``jlcpcb-tier1``, 0.20 mm clearance, 0.30 mm trace).
  3. Asserts the pipeline produces a structured outcome -- the
     fine-pitch escape regions are detected and either (a) the routing
     converges with reach >= some baseline OR (b) the partial result
     is recorded for diagnostic purposes.

This is a slow test gated on ``KICAD_RUN_SLOW_SOFTSTART_REACH=1`` to
match the existing softstart slow-path conventions.  The headline
target from Issue #3371 AC #4 is ``>= 28/30 reach`` at the
jlcpcb-tier1 recipe; P_FP4 lands the infrastructure (adaptive radius,
in-region clearance threading, escape helper) that should bring the
routing reach up.  This test pins the floor at the pre-P_FP4 baseline
(18/30) so a future regression of the infrastructure surfaces.

**Issue #3390 timeout investigation:** Before this PR, the test
invoked ``kct route`` via ``subprocess.run`` and timed out at 660 s
because (a) ``--auto-layers`` (default ON) escalated from L=2 to L=4
on min-completion miss, adding a ~5 min L=4 pass, and (b) the rip-up
+ reroute iterations continue past the routing budget by some margin
before the negotiated router cleanly exits.  This test now drives
the autorouter directly in-process (mirroring
``test_softstart_routing_reach_regression.py``) with a strict 240 s
routing budget and ``use_negotiated=True``.  In-process bypasses
the subprocess overhead and gives precise control over the budget;
total wall is ~5 min on a modern laptop with the C++ backend.

To run locally::

    KICAD_RUN_SLOW_SOFTSTART_REACH=1 uv run pytest \\
      tests/router/test_softstart_revb_fine_pitch_escape.py -v --no-cov -s

Issue: https://github.com/rjwalters/kicad-tools/issues/3371
"""

from __future__ import annotations

import io
import logging
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "external" / "softstart"


pytestmark = pytest.mark.slow


def _slow_tests_enabled() -> bool:
    """Whether the slow softstart routing tests are enabled."""
    return os.environ.get("KICAD_RUN_SLOW_SOFTSTART_REACH") == "1"


if not _slow_tests_enabled():  # pragma: no cover - env gate
    pytestmark = [
        pytest.mark.slow,
        pytest.mark.skipif(
            True,
            reason=(
                "Slow softstart fine-pitch escape test (~10-15min).  Set "
                "KICAD_RUN_SLOW_SOFTSTART_REACH=1 to enable."
            ),
        ),
    ]


def _regenerate_softstart_pcb(output_dir: Path) -> Path:
    """Regenerate softstart rev B PCB on demand."""
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


# Mirrors the recipe's ``ROUTE_SKIP_NETS`` (generate_design.py).  Issue
# #3343 P-R1 (architect S1): VGATE / SRC_POS / SRC_NEG / BUS_LINE are
# power/heavy-current nets that get zone-pour copper, not 0.3 mm traces —
# the signal-net denominator is 26, not 30.
_SKIP_NETS = [
    "AC_LINE", "AC_NEUTRAL", "FUSED_LINE", "GND",
    "+3.3V", "VRECT",
    "SCAP_POS+", "SCAP_POS_GND", "SCAP_NEG+", "SCAP_NEG_GND",
    "ISENSE_POS",
    "VGATE", "SRC_POS", "SRC_NEG", "BUS_LINE",
]


def _route_softstart_in_process(
    pcb_path: Path,
    *,
    routing_timeout: float = 240.0,
    per_net_timeout: float = 30.0,
    layer_stack: object | None = None,
) -> tuple[int, int, str]:
    """Drive softstart rev B routing in-process (Issue #3390 timeout fix).

    Replaces the previous ``subprocess.run(kct route)`` invocation
    that timed out at 660 s wall.  In-process gives precise control
    over the routing budget and lets us strictly bound the test
    duration while still exercising the full ``route_with_escape``
    pipeline (subgrid prepass + dense-package escape + negotiated
    main routing).

    Args:
        pcb_path: Input PCB.
        routing_timeout: Overall budget for the main routing phase.
        per_net_timeout: Per-A* timeout in seconds.
        layer_stack: Optional explicit ``LayerStack`` for routing.  When
            ``None`` the PCB's declared layer count is used (2L for the
            softstart unrouted PCB fixture).  Issue #3401 ships the
            ``starting_layers=4`` recipe field; this test surfaces the
            L=4 measurement by passing
            ``LayerStack.four_layer_sig_gnd_pwr_sig()`` explicitly so the
            in-process measurement matches what ``kct route`` does at
            ``--starting-layers 4`` (route_cmd.py picks the plane-aware
            stack first when escalating to L=4).

    Returns:
        Tuple ``(nets_routed, total_nets, captured_log)`` where
        ``nets_routed`` is the count of fully-connected pad-to-pad
        nets, ``total_nets`` is the count of nets routing attempted
        on, and ``captured_log`` is the full ``kicad_tools.router.*``
        INFO log buffer (used by callers that assert on log content).
    """
    os.environ.setdefault("PYTHONHASHSEED", "0")

    # Capture router log lines for callers that assert on them
    # (e.g. fine-pitch regions detected, SOP rescue, U1 LQFP rescue).
    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(name)s:%(levelname)s:%(message)s"))
    router_logger = logging.getLogger("kicad_tools")
    prev_level = router_logger.level
    router_logger.setLevel(logging.INFO)
    router_logger.addHandler(handler)

    try:
        from kicad_tools.router import DesignRules, load_pcb_for_routing

        rules = DesignRules(
            trace_width=0.30,
            trace_clearance=0.20,
            via_diameter=0.6,
            via_drill=0.3,
            min_trace_width=0.127,
            manufacturer="jlcpcb-tier1",
        )

        load_kwargs: dict[str, object] = {
            "skip_nets": _SKIP_NETS,
            "rules": rules,
        }
        if layer_stack is not None:
            load_kwargs["layer_stack"] = layer_stack

        router, _ = load_pcb_for_routing(str(pcb_path), **load_kwargs)
        router.rules.manufacturer = "jlcpcb-tier1"

        router.route_with_escape(
            use_negotiated=True,
            per_net_timeout=per_net_timeout,
            timeout=routing_timeout,
        )

        stats = router.get_statistics()
        # ``nets_routed`` from compute_routing_statistics counts only
        # fully-connected pad-to-pad nets (post PR #3389 / Issue #3199).
        nets_routed = int(stats["nets_routed"])
        # Total signal nets the router attempted (excludes skip_nets).
        total_nets = len(router.nets)

        return nets_routed, total_nets, log_buf.getvalue()
    finally:
        router_logger.removeHandler(handler)
        router_logger.setLevel(prev_level)


def test_softstart_revb_fine_pitch_regions_install(tmp_path: Path) -> None:
    """Fine-pitch escape regions are installed during softstart rev B routing.

    Verifies the P_FP3 pipeline integration: ``load_pcb_for_routing``
    detects the UCC27211 SOIC-8, MCP6001, STM32 LQFP-32, etc. as
    fine-pitch escape regions and installs them on the grid.

    Issue #3390: refactored to drive routing in-process and inspect
    the installed regions directly on ``router.grid`` rather than
    grepping the CLI's ``flush_print`` log line (which is only
    emitted by ``kicad_tools.cli.route_cmd``, not the lower-level
    ``load_pcb_for_routing`` API used here).  The structural check
    is stronger than the log-line grep because it asserts on the
    actual stored regions rather than a transient print -- a refactor
    of the log format can no longer silently break this guard.
    """
    pcb_path = _regenerate_softstart_pcb(tmp_path / "softstart_fp")

    os.environ.setdefault("PYTHONHASHSEED", "0")
    from kicad_tools.router import DesignRules, load_pcb_for_routing

    rules = DesignRules(
        trace_width=0.30,
        trace_clearance=0.20,
        via_diameter=0.6,
        via_drill=0.3,
        min_trace_width=0.127,
        manufacturer="jlcpcb-tier1",
    )
    router, _ = load_pcb_for_routing(
        str(pcb_path), skip_nets=_SKIP_NETS, rules=rules,
    )

    regions = router.grid.get_fine_pitch_regions()
    assert regions, (
        "Expected ``load_pcb_for_routing`` to install fine-pitch escape "
        "regions on the softstart rev B grid (UCC27211 SOIC-8, STM32 LQFP-32, "
        "etc.).  Got: empty region list."
    )
    region_refs = sorted({r.package_ref for r in regions})
    # The fixture must surface at least U1 (LQFP-32) and one of the
    # UCC27211 SOIC-8s; missing either points to a regression in
    # ``detect_fine_pitch_regions`` (see ``fine_pitch_escape.py``).
    assert "U1" in region_refs, (
        f"Expected U1 LQFP-32 in fine-pitch region list; got {region_refs}"
    )
    soic_refs = [r for r in region_refs if r in ("U5", "U6", "U7")]
    assert soic_refs, (
        f"Expected at least one UCC27211 / LM393 SOIC-8 (U5/U6/U7) in the "
        f"fine-pitch region list; got {region_refs}"
    )


def test_softstart_revb_reach_floor(tmp_path: Path) -> None:
    """Softstart rev B routing reach holds at the L=4 single-attempt floor.

    The Issue #3371 AC #4 target is >= 28/30 reach.  P_FP4 lands the
    infrastructure (adaptive radius, in-region clearance threading,
    escape helper, dense-package union); P_FP5 (PR #3380) wires
    per-ref escape clearance; P_FP6 (PR #3389) wires the SOP staggered
    in-pad rescue; PR #3386 lands the U1 LQFP-32 subgrid in-pad
    rescue.

    Issue #3401 update (Jun 2026): the softstart recipe
    (``boards/external/softstart/project.kct``) now declares
    ``escalation.starting_layers=4`` (PR #3405 landed the schema field
    + ``--starting-layers`` CLI flag, this PR sets the value in the
    softstart spec).  Empirical measurement at L=4 with the plane-aware
    stack ``four_layer_sig_gnd_pwr_sig`` is 20/30 fully connected --
    a 2-net improvement over the prior 18/30 L=2 single-attempt floor.
    The all-signal stack ``four_layer_all_signal`` measured worse at
    17/30 (more verticals + denser inner layer congestion); the
    plane-aware variant is therefore the production target.

    Why not 30/30?  The remaining 10 unrouted nets are the deeper
    rescue <-> main-router coupling problem tracked by #3398
    (BLOCKED_BY_COMPONENT rip-up + adjacent-pin in-pad vias).  L=4
    on its own does not close the gap -- it buys 2 nets of headroom
    by giving the escape -> bus routing more vertical relief but
    does not unblock the SOIC-8 / LQFP-32 interactions that #3398
    targets.

    Why not 28/30?  The architect's +3 net P_FP6 estimate (Issue
    #3381 comment) assumed the SOP staggered dispatcher would run
    for UCC27211 SOIC-8 (1.27 mm pitch).  Empirically the
    ``detect_dense_packages`` dynamic threshold at 0.30 mm trace +
    0.20 mm clearance is 1.0 mm -- below the 1.27 mm UCC27211 pitch
    -- so UCC27211 is *not* in the dense package list during
    ``route_with_escape`` and the P_FP6 wiring is unreachable on
    this fixture at this recipe.  The rescue path is correct when
    invoked directly (verified by
    ``test_softstart_revb_p_fp6_dispatcher_eligible``) but the
    SOP dispatcher does not invoke it during this end-to-end route.

    Issue #3395 (Jun 2026):  investigated raising the dispatcher
    gate (broadening the dual-row fine-pitch cap in
    ``is_dense_package`` from 0.75 mm to 1.5 mm so UCC27211 SOIC-8
    qualifies).  Empirical measurement: reach REGRESSES 18/30 ->
    8/30 because the P_FP6 in-pad vias collide with the GATE/UCC
    bus routing downstream.  See
    ``test_softstart_revb_dispatcher_gap_documents_p_fp6_unreached``
    for the detailed measurement table.  The dispatcher gap is
    INTENTIONAL until #3398 (the rescue <-> main-router interaction
    fix) lands.

    Issue #3390: drives routing in-process with a strict routing
    budget.  Replaces the previous ``subprocess.run(kct route)``
    invocation that timed out at 660 s.  Issue #3401 raises the
    budget to 480 s to accommodate the L=4 main-routing phase
    (the L=4 measurement took ~492 s wall on a baseline laptop
    with the C++ backend; the floor still holds even when the
    budget cuts in slightly earlier).

    Issue #3343 P-R1..P-R4 update (Jun 2026): the signal-net
    denominator is now **26** (VGATE / SRC_POS / SRC_NEG / BUS_LINE
    moved to the skip-list — they are power/heavy-current nets that
    get zone-pour copper, see ``ROUTE_SKIP_NETS`` in the recipe).
    Measured progression at this harness (same-session A/B runs,
    ``PYTHONHASHSEED=0``):

    - baseline (pre-#3343):       19/30  (== 17/26 at the new denominator)
    - P-R1 skip-list alignment:   18/26
    - P-R2 north-face pin moves:  20/26
    - P-R3 placement micro-moves: 21/26
    - P-R4 escape fixes (SOT-23-5 column-orientation + SOT-23-class
      dense exclusion):           22/26

    The residual nets (NRST 8-pad span, V_BANK_POS_SENSE,
    V_BUS_DVDT, SWCLK in the worst run) all route fully in isolation
    and fail only through end-of-budget rip-up non-convergence —
    the #3470 rip-up-rollback signature.  Re-tighten the floor when
    #3470 lands.
    """
    pcb_path = _regenerate_softstart_pcb(tmp_path / "softstart_reach")

    # Issue #3401: drive the measurement at L=4 to mirror what the
    # softstart project.kct spec opts into via ``starting_layers=4``.
    # ``four_layer_sig_gnd_pwr_sig`` is the first stack ``kct route``
    # tries at L=4 (see route_cmd.py).
    from kicad_tools.router import LayerStack
    layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()

    routed_count, total, _ = _route_softstart_in_process(
        pcb_path,
        routing_timeout=480.0,
        per_net_timeout=30.0,
        layer_stack=layer_stack,
    )
    print(f"\nSoftstart rev B reach: {routed_count}/{total} @ L=4")

    # Issue #3343: measured 22/26 at this harness with the P-R1..P-R4
    # changes (multiple same-session runs).  Run-to-run spread on this
    # board is ±2-3, so a floor of 20 leaves 2 nets of headroom while
    # still surfacing infrastructure regressions (the pre-#3343 state
    # measured 17/26 at this denominator).  Tighten the floor once
    # #3470 (rip-up rollback) lands.
    floor = 20
    assert routed_count >= floor, (
        f"Softstart rev B reach {routed_count}/{total} below floor {floor}/{total} "
        f"(L=4 measurement, Issues #3401/#3343)."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s", "-m", "slow", "--no-cov"]))
