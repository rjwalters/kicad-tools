#!/usr/bin/env python3
"""Isolated reach measurement for the board-07 DDR data byte (Issue #4084).

Reproduces the "11-net DDR bundle alone on an empty 4-layer board"
scenario from #3438's isolation matrix so the monotonic-certificate
(Issue #4084, Phase 1) reach delta is re-runnable, not just quoted in
issue prose.

The bundle is the DDR_DATA_BYTE_0 group on board 07: nine single-ended
nets (DQ0-7 + DM0) plus the DQS_P/DQS_N diff pair, connecting a facing
QFN-48 pin column on U1 (right side, pins 25-35) to its mirror on U2
(left side, pins 1-11), at 0.8 mm pitch across a ~30 mm channel.  Both
columns declare the byte in the SAME net order, so along the row long
axis (y) the two facing columns are CO-ORIENTED, not reversed — which is
exactly what the certificate reports.

Usage:
    uv run python boards/07-matchgroup-test/ddr_bundle_isolation_repro.py
    uv run python boards/07-matchgroup-test/ddr_bundle_isolation_repro.py \
        --mode reversed

Prints, for the certificate flag OFF (identity baseline) and ON:
  * the monotonic-certificate classification (feasible? witness?),
  * the routing order the escape scheduler used,
  * the measured reach (X/11 nets routed to completion).

Pure in-process; no CLI subprocess (the flag is not exposed on ``kct
route``).  Uses the negotiated router with the C++ backend when built.

Reversed-geometry mode (Issue #5536, Epic #5511 Phase 2a)
---------------------------------------------------------
``--mode reversed`` rebuilds the SAME bundle with U2's column carrying the
byte in the opposite order -- the real board's geometry, where the
stuck-net classifier measures 28/28 facing pad pairs inverted -- and
measures reach twice:

  * with the declared swap group's rebinding NOT applied (the honest
    "before"), and
  * with it applied: the declared ``swap_group`` produces a
    crossing-minimising ``{pad -> net}`` map via the SAME pure
    ``propose_swap_assignment`` the classifier uses, applied through the
    SAME ``placement_feedback`` helpers the Phase-2a ``reorder_pins``
    applicator uses on a real board.

Both passes route the SAME pads at the SAME positions under the SAME
budget; the only difference is the pad->net binding.  The acceptance
assertion is ``reach(before) < reach(after) == 11/11``, and the script
exits non-zero when it does not hold -- a re-runnable check rather than a
quoted number.  Measured 2026-09-18 (C++ backend built, default budget):
**9/11 before, 11/11 after**, with the proposal itself reporting
``crossings 55 -> 0``.  Nothing here is inferred: with no ``swap_group``
declared on the net class the proposal is never computed and the mode
reports that it has nothing to measure.

The mode deliberately runs on ONE signal layer inside a sealed 3 mm
channel -- see ``build_isolated_router`` for the three configurations that
were measured and why the other two make a reach assertion vacuous (on the
open 4-layer board a fully reversed bundle still reaches 11/11).

Why the CO-ORIENTED harness does NOT exercise placement-delta feedback (#4468)
-----------------------------------------------------------------------------
Measured 2026-07-31 on `main` + #4468, C++ backend built: this harness
reaches **11/11 with the certificate flag both OFF and ON**.  There is no
stuck net, so the placement-delta feedback loop (#4467/#4468) has nothing
to propose here and wiring it in would be measuring a no-op.  That is what
the reversed mode above exists to fix.

That is not a contradiction with the full board stranding DDR members --
it is the harness's stated idealization showing through.  ``build_isolated_router``
places each net's two pads at the SAME ``y`` on both columns (co-oriented,
per the note above), whereas board 07's real ``U1`` right column and ``U2``
left column carry the byte in opposite order: the stuck-net classifier
measures **28/28 facing pad pairs inverted** between them on the real
board.  The isolated bundle is therefore the *already-de-reversed* geometry
-- which is exactly why it routes 11/11 -- while the full board's DDR opens
are a property of the reversal plus the surrounding foreign copper, neither
of which this harness models.

The full-board placement-delta measurement (and the negative result for the
``rotate_180`` de-reversal move) is recorded in ``README.md`` under
"Placement-delta feedback".

Runtime note: the negotiated router can spend minutes on this bundle (the
C++ pathfinder gives up on DQS_N/DQS_P and falls back to the pure-Python
A*).  ``--timeout`` / ``--per-net-timeout`` bound a run, at the cost of
lowering BOTH measured reaches -- compare figures only across runs that
used the same bounds.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the DDR pin declarations importable from the sibling generator.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kicad_tools.router.bundle_river import RowMember  # noqa: E402
from kicad_tools.router.core import Autorouter  # noqa: E402
from kicad_tools.router.layers import Layer, LayerStack  # noqa: E402
from kicad_tools.router.placement_feedback import (  # noqa: E402
    apply_router_pad_net_bindings,
    resolve_router_swap_bindings,
)
from kicad_tools.router.rules import NetClassRouting  # noqa: E402
from kicad_tools.router.swap_groups import (  # noqa: E402
    SwapAssignment,
    propose_swap_assignment,
)

PITCH = 0.8
SWAP_GROUP = "DDR_BYTE0"
BOARD_W, BOARD_H = 80.0, 40.0
# Vertical breathing room left between the outermost pad and a channel wall:
# the widest DDR trace is 0.15mm with 0.10mm clearance, so 0.4mm clears the
# wall's own halo without giving a crossing anywhere to escape to.
CHANNEL_MARGIN = 0.4
# Reversed-mode channel width.  Short on purpose: a 30mm channel leaves room
# to resolve the crossings, which is exactly what must NOT be available for
# the reach assertion to mean anything (see build_isolated_router).
REVERSED_CHANNEL_MM = 3.0
# Default budget for the reversed acceptance run (the CLI flags override it).
DEFAULT_REVERSED_TIMEOUT = 120.0
DEFAULT_REVERSED_PER_NET_TIMEOUT = 20.0

# The DDR byte's row order on both facing columns (U1 pins 25-35 == U2
# pins 1-11), matching generate_pcb.py's pin_nets declarations.  DQS_P /
# DQS_N are the interleaved diff pair.
ROW_NETS = [
    "DQ0",
    "DQ1",
    "DQ2",
    "DQ3",
    "DM0",
    "DQS_P",
    "DQS_N",
    "DQ4",
    "DQ5",
    "DQ6",
    "DQ7",
]


def build_isolated_router(
    *,
    enable_certificate: bool,
    channel_mm: float = 30.0,
    reversed_secondary: bool = False,
    declare_swap_group: bool = False,
    single_layer: bool = False,
    channel_walls: bool = False,
) -> tuple[Autorouter, list[int]]:
    """Build a router with ONLY the 11-net DDR byte on an empty board.

    U1's facing column sits at x=20, U2's mirror at x=20+channel.  By
    default each net connects one pad on each column at the same y
    (CO-ORIENTED), exactly as board 07 declares them.

    ``reversed_secondary`` (Issue #5536) instead binds U2's column in the
    OPPOSITE order -- U2 pin ``1 + i`` carries ``ROW_NETS[-1 - i]`` -- which
    is the real board's relationship between U1's right column and U2's left
    column (28/28 facing pad pairs inverted).  Pad POSITIONS are identical in
    both modes; only the pad->net binding differs, which is precisely the
    degree of freedom a declared swap group re-assigns.

    ``declare_swap_group`` puts ``swap_group=SWAP_GROUP`` on the net class, so
    the members are DECLARED swappable.  Without it nothing is swappable:
    membership is never inferred (see ``NetClassRouting.swap_group``).

    ``single_layer`` pins routing to F.Cu (the #715 ``allowed_layers`` hard
    constraint) and ``channel_walls`` seals the byte lane between two
    full-width keepouts, leaving a corridor exactly as tall as the pad column
    (+/-0.4 mm).  Together with a SHORT ``channel_mm`` those two are what make
    a reversal a REACH question instead of a via-count one.  Measured on this
    harness (2026-09-18, C++ backend built):

    * 4-layer stack, 30 mm channel, no walls -- the router answers a fully
      reversed bundle by diving to the back layer and still reaches 11/11
      (the co-oriented control reached 10/11 in the same 240s/30s budget).
      The reversal costs vias and length there, not nets, so a reach-based
      assertion on that geometry is vacuous.
    * one signal layer, 30 mm channel -- the reversal costs reach (9/11 vs a
      10/11 co-oriented control) but the bundle's own ceiling is 10/11: the
      coupled DQS_P/DQS_N strobe pair never lands on a single layer over that
      span, in EITHER ordering.
    * one signal layer, sealed 3 mm channel (the reversed mode's default) --
      the 55 forced crossings have neither a layer nor a way around to escape
      to, and the co-oriented control is clean: 11/11 in 3.2s.  That is the
      regime the monotone certificate (#4084) describes, and the one where
      the swap's reach delta is real.
    """
    cls = NetClassRouting(
        name="DDR_DATA_BYTE_0",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        length_critical=True,
        length_match_group="DDR_DATA_BYTE_0",
        length_match_reference=None,
        length_match_tolerance_mm=0.1,
        swap_group=SWAP_GROUP if declare_swap_group else None,
    )
    net_class_map: dict[str, NetClassRouting] = {}
    router = Autorouter(
        width=BOARD_W,
        height=BOARD_H,
        net_class_map=net_class_map,
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
    )
    router.enable_monotone_certificate_order = enable_certificate
    if single_layer:
        router.rules.allowed_layers = ["F.Cu"]

    u1_x = 20.0
    u2_x = u1_x + channel_mm
    centre_y = 20.0
    base_y = centre_y - (len(ROW_NETS) - 1) * PITCH / 2.0

    net_ids: list[int] = []
    for i, name in enumerate(ROW_NETS):
        net_id = i + 1
        net_ids.append(net_id)
        y = base_y + i * PITCH
        router.add_component(
            "U1",
            [{"number": str(25 + i), "x": u1_x, "y": y, "net": net_id, "net_name": name}],
        )
        net_class_map[name] = cls
    for i in range(len(ROW_NETS)):
        y = base_y + i * PITCH
        row = len(ROW_NETS) - 1 - i if reversed_secondary else i
        router.add_component(
            "U2",
            [
                {
                    "number": str(1 + i),
                    "x": u2_x,
                    "y": y,
                    "net": row + 1,
                    "net_name": ROW_NETS[row],
                }
            ],
        )
    router.net_class_map = net_class_map
    if channel_walls:
        top_y = base_y + (len(ROW_NETS) - 1) * PITCH
        low_h = base_y - CHANNEL_MARGIN
        high_h = BOARD_H - (top_y + CHANNEL_MARGIN)
        # (x, y, w, h, layer) with (x, y) the obstacle CENTRE.
        router._repro_walls = [
            (BOARD_W / 2.0, low_h / 2.0, BOARD_W, low_h, Layer.F_CU),
            (BOARD_W / 2.0, top_y + CHANNEL_MARGIN + high_h / 2.0, BOARD_W, high_h, Layer.F_CU),
        ]
        install_channel_walls(router)
    return router, net_ids


def install_channel_walls(router: Autorouter) -> None:
    """(Re-)install the corridor keepouts recorded on ``router``.

    Must be re-run after any grid reset: ``Autorouter._reset_for_new_trial``
    rebuilds the grid from the live pads, fixed fills and edge keepout, but
    NOT from obstacles registered through ``add_obstacle`` -- so a reset
    silently reopens the channel.
    """
    for x, y, width, height, layer in getattr(router, "_repro_walls", ()):
        router.add_obstacle(x, y, width, height, layer)


def _facing_rows(router: Autorouter) -> tuple[list[RowMember], list[RowMember]]:
    """Project U1/U2 pads onto their shared long axis (y) as row members."""
    rows: dict[str, list[RowMember]] = {"U1": [], "U2": []}
    for pad in router.all_pads:
        if pad.ref in rows:
            rows[pad.ref].append(RowMember(net_id=pad.net, net_name=pad.net_name, projection=pad.y))
    return rows["U1"], rows["U2"]


def propose_swap(router: Autorouter) -> tuple[SwapAssignment, dict[str, str]] | None:
    """Compute the declared swap group's ``{pad_number: net_name}`` proposal.

    DECLARED-ONLY, exactly like the classifier: the group is the set of nets
    whose class carries ``swap_group == SWAP_GROUP``.  With no declaration
    (``build_isolated_router(declare_swap_group=False)``) this returns
    ``None`` and nothing can be applied.  The assignment itself comes from
    the same pure :func:`propose_swap_assignment` the stuck-net classifier
    calls, so this harness measures the production proposal, not a bespoke
    permutation.
    """
    group_nets = {
        name
        for name, net_class in router.net_class_map.items()
        if net_class.swap_group == SWAP_GROUP
    }
    if len(group_nets) < 2:
        return None
    primary, secondary = _facing_rows(router)
    assignment = propose_swap_assignment(primary, secondary, group_nets)
    if assignment is None or not assignment.net_rebinding:
        return None
    pad_map = {
        pad.pin: assignment.net_rebinding[pad.net_name]
        for pad in router.all_pads
        if pad.ref == "U2" and pad.net_name in assignment.net_rebinding
    }
    return (assignment, pad_map) if pad_map else None


def apply_swap(router: Autorouter, pad_map: dict[str, str]) -> int:
    """Apply a proposal's pad map to the router, via the Phase-2a helpers.

    These are the SAME functions
    :meth:`~kicad_tools.router.placement_feedback.PlacementDeltaFeedbackLoop._apply_delta_to_router_pads`
    uses for a ``reorder_pins`` delta on a real board (Issue #5536), so a
    reach measured here is a measurement of the shipped applicator's router
    side, not of harness-local code.
    """
    bindings = resolve_router_swap_bindings(router, "U2", pad_map)
    if not bindings:
        raise SystemExit("swap pad_map did not resolve against the router's pads")
    apply_router_pad_net_bindings(router, bindings)
    # Re-stamp the grid so its per-cell net ownership follows the rebinding --
    # the same ``_reset_for_new_trial`` the feedback loop reaches through
    # ``_clear_routes`` before every re-route.  Skipping it measures 1/11
    # instead of 11/11: every net would be routed at pads the grid still
    # attributes to the net that used to own them.
    router._reset_for_new_trial()
    install_channel_walls(router)
    return len(bindings)


def route_and_count(
    router: Autorouter,
    net_ids: list[int],
    *,
    timeout: float | None = None,
    per_net_timeout: float | None = None,
) -> int:
    """Route the bundle and return how many of ``net_ids`` reached completion."""
    routes = router.route_all_negotiated(seed=42, timeout=timeout, per_net_timeout=per_net_timeout)
    # A net is "reached" when it has at least one non-escape (full) route.
    routed_nets = {
        r.net
        for r in routes
        if getattr(r, "net", None) is not None and not getattr(r, "is_escape", False)
    }
    return len(routed_nets & set(net_ids))


def _print_certificates(router: Autorouter, net_ids: list[int]) -> None:
    """Show the ordering + certificate verdict the escape scheduler computed."""
    ordered = router._apply_byte_lane_inner_priority(list(net_ids))
    print(f"routing order (net ids): {ordered}")
    for grp, cert in router._last_monotone_certificates.items():
        print(
            f"certificate[{grp}]: feasible={cert.feasible} "
            f"mirrored={cert.mirrored} inversions={cert.inversion_count}"
        )
        if not cert.feasible:
            pairs = ", ".join(f"({p.net_a},{p.net_b})" for p in cert.witness)
            print(f"  witness (forced crossings): {pairs}")


def measure_reach(
    *,
    enable_certificate: bool,
    timeout: float | None = None,
    per_net_timeout: float | None = None,
) -> int:
    router, net_ids = build_isolated_router(enable_certificate=enable_certificate)

    label = "ON (certificate)" if enable_certificate else "OFF (identity baseline)"
    print(f"\n=== enable_monotone_certificate_order = {label} ===")
    _print_certificates(router, net_ids)

    reached = route_and_count(router, net_ids, timeout=timeout, per_net_timeout=per_net_timeout)
    print(f"reach: {reached}/{len(net_ids)} nets routed")
    return reached


def measure_reversed_reach(
    *,
    timeout: float | None = None,
    per_net_timeout: float | None = None,
    single_layer: bool = True,
) -> int:
    """Reversed-geometry acceptance measurement (Issue #5536).

    Same pads, same positions, same router settings -- the only difference
    between the two measurements is whether the declared swap group's
    pad->net rebinding has been applied.  Returns a process exit code: 0 when
    the swap strictly improves reach AND lands 11/11, 1 otherwise.
    """
    total = len(ROW_NETS)
    if timeout is None and per_net_timeout is None:
        # Equal, finite budget for BOTH passes.  The swapped bundle routes in
        # well under a second; the budget exists only so the unroutable
        # "before" pass stops grinding on nets that have nowhere to go.
        timeout, per_net_timeout = DEFAULT_REVERSED_TIMEOUT, DEFAULT_REVERSED_PER_NET_TIMEOUT
    print(f"budget: timeout={timeout}s per_net_timeout={per_net_timeout}s (identical both passes)")
    build = {
        "enable_certificate": True,
        "reversed_secondary": True,
        "declare_swap_group": True,
        "single_layer": single_layer,
        "channel_walls": single_layer,
        "channel_mm": REVERSED_CHANNEL_MM if single_layer else 30.0,
    }
    print(
        "\n=== reversed geometry: swap NOT applied (before) ==="
        + (
            f"\n(sealed {REVERSED_CHANNEL_MM}mm channel on ONE signal layer -- a crossing has"
            "\n neither a layer nor a way around to escape to)"
            if single_layer
            else "\n(full 4-layer stack, open board -- the reversal costs vias, not reach)"
        )
    )
    router, net_ids = build_isolated_router(**build)
    _print_certificates(router, net_ids)
    proposal = propose_swap(router)
    if proposal is None:
        print("no declared-swap-group proposal -- nothing to measure")
        return 1
    assignment, pad_map = proposal
    print(
        f"declared swap group {SWAP_GROUP}: {len(pad_map)} pad(s) re-bound on U2, "
        f"crossings {assignment.crossings_before} -> {assignment.crossings_after}"
    )
    for pad in sorted(pad_map, key=int):
        print(f"  pad {pad} -> {pad_map[pad]}")
    before = route_and_count(router, net_ids, timeout=timeout, per_net_timeout=per_net_timeout)
    print(f"reach: {before}/{total} nets routed")

    print("\n=== reversed geometry: swap APPLIED (after) ===")
    router, net_ids = build_isolated_router(**build)
    rebound = apply_swap(router, pad_map)
    print(f"applied the proposal to {rebound} router pad(s)")
    _print_certificates(router, net_ids)
    after = route_and_count(router, net_ids, timeout=timeout, per_net_timeout=per_net_timeout)
    print(f"reach: {after}/{total} nets routed")

    ok = before < after == total
    print(
        f"\nacceptance: before {before}/{total} < after {after}/{total} == {total}/{total}"
        f" -> {'PASS' if ok else 'FAIL'}"
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mode",
        choices=("certificate", "reversed", "all"),
        default="all",
        help=(
            "certificate: the #4084 co-oriented measurement only; "
            "reversed: the #5536 reversed-geometry swap acceptance only; "
            "all (default): both"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="wall-clock budget per routing pass (default: unbounded)",
    )
    parser.add_argument(
        "--per-net-timeout",
        type=float,
        default=None,
        help="per-net budget (default: unbounded; bounding it lowers every reach)",
    )
    parser.add_argument(
        "--multi-layer",
        action="store_true",
        help=(
            "reversed mode: route on the full 4-layer stack instead of pinning "
            "the bundle to F.Cu (the reversal then costs vias, not reach)"
        ),
    )
    args = parser.parse_args(argv)

    print("Board-07 DDR data byte isolation reach measurement (Issue #4084)")
    print(f"bundle: {len(ROW_NETS)} nets, 0.8mm pitch, facing QFN-48 columns")
    bounds = {"timeout": args.timeout, "per_net_timeout": args.per_net_timeout}
    if args.mode in ("certificate", "all"):
        measure_reach(enable_certificate=False, **bounds)
        measure_reach(enable_certificate=True, **bounds)
    if args.mode in ("reversed", "all"):
        return measure_reversed_reach(single_layer=not args.multi_layer, **bounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
