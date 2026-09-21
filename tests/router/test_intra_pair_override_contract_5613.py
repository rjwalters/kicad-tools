"""Issue #5613 -- the within-pair clearance override must RELAX, never tighten.

Background
----------

Issue #2559 / Epic #2556 Phase 1C threaded a ``partner_net`` +
``intra_pair_clearance`` pair through the C++ A* search and through
``Grid3D::validate_route`` so the two legs of a differential pair are not
rejected for sitting at the **tighter** within-pair distance their coupling
requires.  ``cpp/src/grid.cpp`` states that contract in as many words:

    The partner branch is active when ``partner_net`` is a real net id
    (>= 0) and ``intra_pair_clearance`` is a *tighter* (non-negative)
    override.

Nothing on the Python side enforced the "tighter" half.  ``cpp_backend``
handed ``NetClassRouting.effective_intra_pair_clearance()`` straight to the
C++ side at three call sites (the search radius, ``set_search_partner_clearance``
and ``_validate_route_clearance``), and that accessor reports the pair's
*coupling gap* -- a number the impedance resolver writes from the field
solver, not a DRC floor.

When a class declares a differential target but no authored
``intra_pair_clearance``, ``apply_impedance_driven_sizing`` takes its
unconstrained branch: it solves the width from ``Z0 / 2`` and *then* solves
the gap for the differential target, which lands in the essentially-uncoupled
regime.  That function's own comment names the pathology ("an ~8mm gap [that
makes] its second net unroutable").  Passed through unchecked, the coupling
gap became a hard floor pointing the wrong way: on the board-07 DDR byte the
validator demanded **8.425 mm** between ``DQS_P`` and ``DQS_N``, whose pads
are 0.8 mm apart, so every candidate route for ``DQS_N`` was rejected.

That mis-signed floor was latent until PR #5165 (Issue #5004) removed the
pitch-only same-component carve-out.  With the carve-out gone the pad seed /
goal region for a 0.5 mm pad carrying a 0.375 mm impedance-sized trace erodes
to a single grid cell, so the "reject the goal cell and resume" recovery that
used to work its way past the bogus rejection has nothing left to land on and
the C++ A* reports "open set exhausted" immediately.  Bisection pinned the
reach regression to PR #5165; the defect this module guards is the
mis-signed floor that made recovery necessary in the first place.

What is asserted
----------------

The fixture is arithmetic-only.  Two parallel same-layer segments of width
0.2 mm sit ``dy`` apart, so their edge-to-edge gap is exactly ``dy - 0.2``:

  * ``dy = 0.35`` -> 0.15 mm gap: BELOW the 0.2 mm default clearance, ABOVE
    a genuine 0.075 mm within-pair override.
  * ``dy = 0.80`` -> 0.60 mm gap: comfortably above the default clearance.

Layer 1 unit-tests ``CppPathfinder._resolve_intra_pair_override`` directly.
Layer 2 drives the whole ``_validate_route_clearance`` path so the assertion
covers the wiring, not just the predicate.
"""

from __future__ import annotations

import dataclasses

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

native = pytest.mark.skipif(
    not is_cpp_available(), reason="native backend unavailable; run kct build-native"
)

TRACE_WIDTH = 0.2
DEFAULT_CLEARANCE = 0.2
#: A genuine within-pair relaxation -- matches the shipped high-speed class.
TIGHT_OVERRIDE = 0.075
#: The uncoupled-regime gap the impedance resolver produces for a 100R
#: differential target with no authored ``intra_pair_clearance``.
UNCOUPLED_GAP = 8.425

P_NET, N_NET = 1, 2
P_Y = 5.0
#: Centre-to-centre offsets; edge-to-edge gap is ``dy - TRACE_WIDTH``.
DY_INSIDE_DEFAULT = 0.35  # 0.15mm gap -- needs the override to pass
DY_CLEARS_DEFAULT = 0.80  # 0.60mm gap -- passes on the plain default


def _net_class(intra: float | None) -> NetClassRouting:
    return NetClassRouting(
        name="DIFF",
        priority=1,
        trace_width=TRACE_WIDTH,
        clearance=DEFAULT_CLEARANCE,
        intra_pair_clearance=intra,
        diffpair_partner="DP_N",
    )


def _make_grid() -> RoutingGrid:
    rules = DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=DEFAULT_CLEARANCE,
        via_drill=0.1,
        via_diameter=TRACE_WIDTH,
        via_clearance=DEFAULT_CLEARANCE,
        grid_resolution=0.1,
    )
    return RoutingGrid(12.0, 12.0, rules=rules, layer_stack=LayerStack.two_layer())


def _segment(net: int, y: float) -> Segment:
    return Segment(2.0, y, 8.0, y, TRACE_WIDTH, Layer.F_CU, net)


def _pad(net: int, net_name: str, y: float, pin: str) -> Pad:
    return Pad(
        x=2.0,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=net_name,
        ref="U1",
        pin=pin,
    )


def _pathfinder(grid: RoutingGrid, partner_y: float) -> CppPathfinder:
    """Build a pathfinder whose stored copper already carries the N leg."""
    grid.routes.append(Route(net=N_NET, net_name="DP_N", segments=[_segment(N_NET, partner_y)]))
    pf = CppPathfinder(CppGrid.from_routing_grid(grid), grid.rules)
    pf.set_net_name_to_id({"DP_P": P_NET, "DP_N": N_NET})
    return pf


def _validate(intra: float | None, dy: float) -> object:
    """Validate a P-leg route ``dy`` from an already-stored N leg."""
    grid = _make_grid()
    pf = _pathfinder(grid, P_Y + dy)
    route = Route(net=P_NET, net_name="DP_P", segments=[_segment(P_NET, P_Y)])
    return pf._validate_route_clearance(
        route,
        _pad(P_NET, "DP_P", P_Y, "1"),
        _pad(P_NET, "DP_P", P_Y, "2"),
        trace_radius_cells=1,
        net_class=_net_class(intra),
    )


# ---------------------------------------------------------------------------
# Layer 0: the fixture really produces the gaps the asserts assume
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dy,expected_gap", [(DY_INSIDE_DEFAULT, 0.15), (DY_CLEARS_DEFAULT, 0.60)])
def test_fixture_gap_arithmetic(dy: float, expected_gap: float) -> None:
    assert dy - TRACE_WIDTH == pytest.approx(expected_gap)


# ---------------------------------------------------------------------------
# Layer 1: the predicate
# ---------------------------------------------------------------------------


@native
def test_override_engaged_only_when_it_relaxes() -> None:
    """A tighter gap is honoured; an uncoupled-regime gap is refused."""
    pf = CppPathfinder(CppGrid.from_routing_grid(_make_grid()), _make_grid().rules)

    # Genuine relaxation -> passed through unchanged (pre-#5613 behaviour).
    assert pf._resolve_intra_pair_override(
        _net_class(TIGHT_OVERRIDE), DEFAULT_CLEARANCE
    ) == pytest.approx(TIGHT_OVERRIDE)

    # THE BUG: a coupling gap wider than the default is not a clearance
    # floor.  Refuse it so the caller leaves ``partner_net`` dormant.
    assert pf._resolve_intra_pair_override(_net_class(UNCOUPLED_GAP), DEFAULT_CLEARANCE) is None

    # Exactly equal stays ENGAGED: passing it through and going dormant both
    # make the partner's required clearance the default, so keeping it keeps
    # boards that author the two to the same value (board 03's 0.15mm USB
    # sidecar vs the 0.15mm HighSpeed class) on their historical path.
    assert pf._resolve_intra_pair_override(
        _net_class(DEFAULT_CLEARANCE), DEFAULT_CLEARANCE
    ) == pytest.approx(DEFAULT_CLEARANCE)


@native
def test_override_dormant_without_a_declared_partner() -> None:
    pf = CppPathfinder(CppGrid.from_routing_grid(_make_grid()), _make_grid().rules)
    solo = dataclasses.replace(_net_class(TIGHT_OVERRIDE), diffpair_partner=None)
    assert pf._resolve_intra_pair_override(solo, DEFAULT_CLEARANCE) is None
    assert pf._resolve_intra_pair_override(None, DEFAULT_CLEARANCE) is None


# ---------------------------------------------------------------------------
# Layer 2: THE BUG, through the real validator
# ---------------------------------------------------------------------------


@native
def test_uncoupled_gap_does_not_reject_a_well_separated_pair() -> None:
    """0.60mm of copper gap must pass, whatever the coupling target says.

    Pre-#5613 the 8.425mm "override" became the required clearance for
    partner-net copper, so this route was rejected and the caller burned
    its whole resume budget re-entering the same goal.
    """
    assert _validate(UNCOUPLED_GAP, DY_CLEARS_DEFAULT) is None


@native
def test_tight_override_still_relaxes_a_sub_default_pair() -> None:
    """The #2559 relaxation is untouched: 0.15mm passes under a 0.075 floor."""
    assert _validate(TIGHT_OVERRIDE, DY_INSIDE_DEFAULT) is None


@native
def test_sub_default_pair_is_rejected_without_a_real_relaxation() -> None:
    """With no usable override the partner is a foreign net at 0.2mm."""
    violation = _validate(UNCOUPLED_GAP, DY_INSIDE_DEFAULT)
    assert violation is not None
    assert violation.kind == "seg-seg"
