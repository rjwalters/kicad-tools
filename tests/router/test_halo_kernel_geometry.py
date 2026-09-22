"""The route-copper halo is the clearance kernel's geometry (Epic #5509, Phase 3a).

Issue #5660 switched consumer groups 1 and 2 -- ``RoutingGrid._mark_segment`` /
``_mark_via`` and their C++ siblings ``Grid3D::mark_segment`` /
``Grid3D::mark_via`` -- off the Chebyshev **square** halo they had stamped
since the router's first commit and onto
``kicad_tools.router.clearance_kernel``'s exact dilation.

Two properties are gated here, and they are deliberately different in kind:

1. **Shape** -- the halo cell set is exactly what the kernel says it is, and a
   strict subset of the square it replaces.  The reach (``radius_cells``) is
   untouched, so this can only remove over-blocking, never add under-blocking:
   every grid-quantisation safety margin (#1666, #1692, #1797) survives.
2. **#5410** -- the DQ3 candidate cell, ``(5, 4)`` cells from DQS_N's via, is
   no longer inside DQS_N's halo.  Chebyshev distance 5 put it *inside* the
   six-cell square; Euclidean distance ``sqrt(41) = 6.40`` puts it outside the
   six-cell disc.  The copper is legal on both counts (0.213 mm copper against
   0.20, 0.513 mm drill against 0.50) and kicad-cli agrees.

The #5410 assertions each pin the **contrast**, not just the outcome: they
assert the square would have covered the cell as well as that the kernel halo
does not.  Without that, a later change that shrank the reach for an unrelated
reason would keep these green while quietly retiring the evidence.

**What this does not claim.**  The conformance suite's group 1/2 rows still
flag the ``issue5410-dqs-n-halo-vs-legal-via`` fixture, and that is not a
contradiction: the oracle adapter's rejection rule dilates the *candidate* as
well as the existing copper, so two six-cell discs 6.40 cells apart still
touch.  What changed is the halo itself, which is what the search consults.
``tests/conformance/test_named_fixtures.py``'s
``FIXTURE_QUANTISATION_LEDGER`` records the residual and explains why closing
it belongs to the refinement pass (groups 4/5), not to halo geometry.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from kicad_tools.router.clearance_kernel import CLEARANCE_EPSILON_MM, KVia, copper_gap
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.grid import RoutingGrid, halo_mask, halo_offsets
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built")

# #5410's verbatim geometry: a 0.6/0.3 DQS_N via, a DQ3 candidate 0.213 mm of
# copper away, on the 0.127 mm grid that produced the six-cell square halo.
ISSUE_5410_RESOLUTION_MM = 0.127
ISSUE_5410_DQS_N = (143.777, 123.800)
ISSUE_5410_DQ3 = (143.142, 123.292)
ISSUE_5410_VIA_DIAMETER_MM = 0.6
ISSUE_5410_VIA_DRILL_MM = 0.3


def _square_offsets(radius_cells: int) -> set[tuple[int, int]]:
    """The Chebyshev square this phase replaced, for contrast assertions."""
    r = max(0, radius_cells)
    return {(dx, dy) for dy in range(-r, r + 1) for dx in range(-r, r + 1)}


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("radius", [0, 1, 2, 3, 5, 6, 9])
def test_halo_membership_is_the_kernels_own_verdict(radius: int) -> None:
    """Every cell in (and out of) the halo is decided by ``copper_gap``.

    Re-derived here from the kernel rather than from ``dx*dx + dy*dy <= r*r``:
    the point of the phase is that there is one geometry, so the test must
    ask the same source the implementation does.
    """
    centre = KVia(x=0.0, y=0.0, diameter=0.0, drill=0.0)
    got = set(halo_offsets(radius))
    for dx, dy in _square_offsets(radius):
        gap = copper_gap(centre, KVia(x=float(dx), y=float(dy), diameter=0.0, drill=0.0))
        assert ((dx, dy) in got) is (gap <= radius + CLEARANCE_EPSILON_MM), (
            f"halo membership of {(dx, dy)} at radius {radius} disagrees with the kernel"
        )


@pytest.mark.parametrize("radius", [0, 1, 2, 3, 5, 6, 9])
def test_halo_is_a_strict_subset_of_the_square_it_replaced(radius: int) -> None:
    """Shape-only change: same reach, no cell the square did not already mark.

    This is the safety argument for the whole phase in one assertion.  A halo
    that is a subset of the previous one cannot under-block anything that was
    blocked before, so no clearance margin is lost -- only the ``sqrt(2)``
    diagonal excess a circumscribing square carries.
    """
    got = set(halo_offsets(radius))
    square = _square_offsets(radius)
    assert got <= square
    assert (0, 0) in got, "the halo must always contain the copper's own cell"
    if radius >= 2:
        assert got < square, f"radius {radius} should drop the square's corners"
        assert (radius, radius) not in got


@pytest.mark.parametrize("radius", [0, 1, 2, 3, 6])
def test_halo_mask_and_halo_offsets_describe_the_same_cells(radius: int) -> None:
    """The vectorised window path cannot drift from the scalar one.

    ``_mark_segment`` picks between a per-cell walk over :func:`halo_offsets`
    and a NumPy slice masked by :func:`halo_mask`; a divergence between them
    would make the marked set depend on which backend happened to be active.
    """
    r = max(0, radius)
    mask = halo_mask(radius)
    assert mask.shape == (2 * r + 1, 2 * r + 1)
    from_mask = {
        (dx, dy) for dy in range(-r, r + 1) for dx in range(-r, r + 1) if bool(mask[dy + r, dx + r])
    }
    assert from_mask == set(halo_offsets(radius))
    assert not mask.flags.writeable, "the cached mask is shared; it must not be mutable"


@pytest.mark.parametrize("radius", [0, 1, 2, 3, 5, 6, 7, 9, 12])
def test_the_read_side_dilation_set_is_unchanged(radius: int) -> None:
    """Epic #5509 scope guard #2, for the two read-side callers, as a test.

    ``RoutingGrid._get_disc_kernel`` (feeding ``_dilate_blocked`` and
    ``Router._dilate_mask_disc``) and ``_get_clearance_mask`` were already
    Euclidean discs -- #3229 moved the first off a square years ago -- and
    #5660 repointed both at :func:`halo_mask` so the read and write sides stop
    being two independently written inequalities.  That is a *refactor*, and
    the only way it stays one is if the cell set is provably identical to the
    ``dx**2 + dy**2 <= r**2`` form it replaced.

    It is, and not by luck: on the integer lattice ``hypot(dx, dy)`` can only
    land within the kernel's 1e-4 epsilon of an integer ``r`` by being exactly
    ``r``, so the epsilon can never admit a cell the squared form excludes.
    A* cost terms and radii are untouched either way (scope guard #2).
    """
    r = max(0, radius)
    yy, xx = np.ogrid[-r : r + 1, -r : r + 1]
    previous = (xx * xx + yy * yy) <= r * r
    assert np.array_equal(halo_mask(radius), previous)


# ---------------------------------------------------------------------------
# #5410 -- the legal via the square halo refused
# ---------------------------------------------------------------------------


def _issue_5410_rules() -> DesignRules:
    return DesignRules(
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=ISSUE_5410_VIA_DRILL_MM,
        via_diameter=ISSUE_5410_VIA_DIAMETER_MM,
        via_clearance=0.20,
        min_hole_to_hole=0.50,
        grid_resolution=ISSUE_5410_RESOLUTION_MM,
    )


def test_issue5410_geometry_is_legal_before_any_grid_is_involved() -> None:
    """The kernel's own reading of the pair, so the rest of the file has a baseline."""
    a = KVia(
        x=ISSUE_5410_DQS_N[0],
        y=ISSUE_5410_DQS_N[1],
        diameter=ISSUE_5410_VIA_DIAMETER_MM,
        drill=ISSUE_5410_VIA_DRILL_MM,
    )
    b = KVia(
        x=ISSUE_5410_DQ3[0],
        y=ISSUE_5410_DQ3[1],
        diameter=ISSUE_5410_VIA_DIAMETER_MM,
        drill=ISSUE_5410_VIA_DRILL_MM,
    )
    assert copper_gap(a, b) == pytest.approx(0.213, abs=0.001)
    assert copper_gap(a, b) > 0.20


def test_issue5410_dq3_candidate_is_outside_the_python_via_halo() -> None:
    """Group 1: the Python grid no longer blocks the cell A* needs for DQ3.

    This is the search-time half of #5410 -- ``_mark_via`` marking DQS_N's
    halo is what made ``is_blocked_for_net`` refuse the DQ3 candidate cell,
    after which A* burned 1,000,000 expansions and called the net unroutable.
    """
    rules = _issue_5410_rules()
    grid = RoutingGrid(
        160.0,
        140.0,
        rules,
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    existing = Via(
        x=ISSUE_5410_DQS_N[0],
        y=ISSUE_5410_DQS_N[1],
        drill=ISSUE_5410_VIA_DRILL_MM,
        diameter=ISSUE_5410_VIA_DIAMETER_MM,
        layers=(Layer.F_CU, Layer.B_CU),
        net=1,
        net_name="DQS_N",
    )
    grid.mark_route(Route(net=1, net_name="DQS_N", segments=[], vias=[existing]))

    egx, egy = grid.world_to_grid(*ISSUE_5410_DQS_N)
    cgx, cgy = grid.world_to_grid(*ISSUE_5410_DQ3)
    offset = (cgx - egx, cgy - egy)
    assert offset == (-5, -4), "the fixture's cell offset drifted; re-derive the contrast below"

    # The halo radius ``_mark_via`` used is unchanged by this phase.
    radius = (
        int(
            (ISSUE_5410_VIA_DIAMETER_MM / 2 + rules.via_clearance + rules.trace_width / 2)
            / rules.grid_resolution
        )
        + 1
    )
    assert offset in _square_offsets(radius), (
        "the Chebyshev square really did swallow this candidate -- without that "
        "the assertion below would not be evidence of anything"
    )
    assert math.dist((0, 0), offset) > radius
    assert offset not in set(halo_offsets(radius))

    assert not grid.is_blocked_for_net(cgx, cgy, 0, 2), (
        "the DQ3 candidate cell is still blocked by DQS_N's halo (#5410)"
    )


@requires_cpp
def test_issue5410_dq3_candidate_is_outside_the_cpp_via_halo() -> None:
    """Group 2: the C++ marking agrees, cell for cell.

    The C++ backend receives its radius from Python
    (``cpp_backend.CppPathfinder.route``), so the contrast is drawn against
    that call site's ``ceil`` form rather than the Python grid's ``int + 1``.
    """
    from kicad_tools.router import router_cpp  # type: ignore[attr-defined]

    rules = _issue_5410_rules()
    grid = router_cpp.Grid3D(
        int(160.0 / ISSUE_5410_RESOLUTION_MM) + 1,
        int(140.0 / ISSUE_5410_RESOLUTION_MM) + 1,
        4,
        ISSUE_5410_RESOLUTION_MM,
        0.0,
        0.0,
    )
    radius = max(
        1,
        math.ceil(
            (ISSUE_5410_VIA_DIAMETER_MM / 2 + rules.via_clearance) / ISSUE_5410_RESOLUTION_MM
        ),
    )
    egx, egy = grid.world_to_grid(*ISSUE_5410_DQS_N)
    grid.mark_via(egx, egy, 1, radius)

    cgx, cgy = grid.world_to_grid(*ISSUE_5410_DQ3)
    cell = grid.at(cgx, cgy, 0)
    assert not cell.blocked, "the DQ3 candidate cell is still blocked by DQS_N's C++ halo"


# ---------------------------------------------------------------------------
# Python <-> C++ parity, and mark/unmark symmetry
# ---------------------------------------------------------------------------


@requires_cpp
@pytest.mark.parametrize("radius", [2, 3, 5, 7])
def test_python_and_cpp_via_halos_mark_identical_cells(radius: int) -> None:
    """One geometry, two ports: the halos must be the same cell set.

    Both sides query the kernel in *grid-cell* units precisely so the answer
    cannot depend on a ``float`` resolution rounding differently in C++ than
    in Python.  This asserts that intent rather than trusting it.
    """
    from kicad_tools.router import router_cpp  # type: ignore[attr-defined]

    grid = router_cpp.Grid3D(80, 80, 2, 0.1, 0.0, 0.0)
    grid.mark_via(40, 40, 7, radius)

    expected = {(40 + dx, 40 + dy) for dx, dy in halo_offsets(radius)}
    got = {
        (gx, gy)
        for gy in range(40 - radius - 2, 40 + radius + 3)
        for gx in range(40 - radius - 2, 40 + radius + 3)
        if grid.at(gx, gy, 0).blocked
    }
    assert got == expected


def test_unmark_releases_exactly_what_mark_claimed() -> None:
    """Rip-up is the halo's inverse, or committed copper leaks blocked cells.

    ``_mark_segment`` and ``_unmark_segment`` derive their cell set from the
    same :func:`halo_offsets` call; if they ever diverged, a ripped-up route
    would leave a rind of permanently blocked cells behind.
    """
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(
        20.0, 20.0, rules, origin_x=0.0, origin_y=0.0, layer_stack=LayerStack.two_layer()
    )
    route = Route(
        net=3,
        net_name="SIG",
        segments=[
            Segment(x1=4.0, y1=4.0, x2=12.0, y2=9.0, width=0.2, layer=Layer.F_CU, net=3),
        ],
        vias=[],
    )
    before = grid._blocked.copy()
    grid.mark_route(route)
    assert grid._blocked.sum() > before.sum(), "the segment marked nothing"
    grid.unmark_route(route)
    assert (grid._blocked == before).all(), "rip-up left blocked cells behind"
