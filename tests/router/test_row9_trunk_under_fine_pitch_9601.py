"""Row 9 of the #9592 audit: keep non-local trunks out from under fine-pitch ICs.

Issue #9622: a non-local net (no pad on the IC) whose straight-line path
crosses a fine-pitch component's body used to route straight through the
body area at zero cost, splitting the free area under the part.  The fix
is a soft per-cell surcharge (``rules.cost_under_body * cost_straight``)
on every A* step a NON-LOCAL net takes inside the body bbox of a
fine-pitch component on an outer layer (``router.body_surcharge``); nets
with a pad on the component are exempt.

The board below is the audit's minimal repro shape: a QFN-class body
(0.5mm pitch pad columns) at the centre of an empty 2-layer board and one
trunk whose straight-line path crosses the body.  With the surcharge
active the trunk detours around the body while every net still routes;
when the under-body strip is the ONLY corridor left, the trunk still
routes through it (the surcharge is additive, never blocking).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from kicad_tools.router.body_surcharge import (
    BodySurchargeRegion,
    region_covers_cell,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import CppPathfinder
from kicad_tools.router.primitives import Layer as LayerEnum

requires_cpp = pytest.mark.requires_router_cpp

BACKENDS = [
    pytest.param(True, id="python"),
    pytest.param(
        False,
        id="cpp",
        marks=requires_cpp,
    ),
]

# QFN-class body: two pad columns (0.5mm pitch) forming the pad ring.
QFN_LEFT_X = 18.0
QFN_RIGHT_X = 22.0
QFN_Y0 = 14.0
QFN_ROWS = 9
QFN_PITCH = 0.5
QFN_PAD_SIZE = 0.3
TRUNK_NET = 100
TRUNK_NET_NAME = "TRUNK"

# A second QFN-class body whose pad ring interleaves U1's (overlap class
# from the #9622 review): U2's left column (x=21.0) sits inside U1's pad
# bbox (x=18.0..22.0), so the two margin-grown surcharge regions share a
# real cell band on every pad row.
U2_LEFT_X = 21.0
U2_RIGHT_X = 25.0
# A net with a pad on U1 only: local to U1, non-local to U2 -- the
# review's "shared net" whose overlap charge was insertion-order
# dependent in the single-owner array.
SHARED_NET = 700

# The body core in world coordinates: the pad-ring bbox (no margin).  The
# surcharge REGION is one grid cell wider on every side (the halo a trunk
# would hug), but "through the body" -- the failure mode the audit
# observed -- means copper inside the pad ring itself.
BODY_X1 = QFN_LEFT_X - QFN_PAD_SIZE / 2
BODY_X2 = QFN_RIGHT_X + QFN_PAD_SIZE / 2
BODY_Y1 = QFN_Y0 - QFN_PAD_SIZE / 2
BODY_Y2 = QFN_Y0 + (QFN_ROWS - 1) * QFN_PITCH + QFN_PAD_SIZE / 2


def _qfn_board(force_python: bool) -> Autorouter:
    router = Autorouter(width=40.0, height=30.0, force_python=force_python)

    qfn_pads: list[dict] = []
    net_id = 1
    for i in range(QFN_ROWS):
        y = QFN_Y0 + i * QFN_PITCH
        qfn_pads.append(
            {
                "number": f"L{i}",
                "x": QFN_LEFT_X,
                "y": y,
                "net": net_id,
                "net_name": f"N{net_id}",
                "width": QFN_PAD_SIZE,
                "height": QFN_PAD_SIZE,
            }
        )
        net_id += 1
        qfn_pads.append(
            {
                "number": f"R{i}",
                "x": QFN_RIGHT_X,
                "y": y,
                "net": net_id,
                "net_name": f"N{net_id}",
                "width": QFN_PAD_SIZE,
                "height": QFN_PAD_SIZE,
            }
        )
        net_id += 1
    router.add_component("U1", qfn_pads)

    # The non-local trunk: straight-line path crosses the body centre.
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 2.0,
                "y": 16.0,
                "net": TRUNK_NET,
                "net_name": TRUNK_NET_NAME,
                "width": 0.6,
                "height": 0.6,
            }
        ],
    )
    router.add_component(
        "J2",
        [
            {
                "number": "1",
                "x": 38.0,
                "y": 16.0,
                "net": TRUNK_NET,
                "net_name": TRUNK_NET_NAME,
                "width": 0.6,
                "height": 0.6,
            }
        ],
    )
    return router


def _qfn_pad_columns(left_x: float, right_x: float, first_net: int, shared_row: int | None = None):
    """One QFN-class body's pad list: two 0.5mm-pitch columns.

    ``shared_row`` (0-based right-column row) carries ``SHARED_NET``
    instead of a fresh net, so that net has a pad on this component only.
    """
    pads: list[dict] = []
    net_id = first_net
    for i in range(QFN_ROWS):
        y = QFN_Y0 + i * QFN_PITCH
        pads.append(
            {
                "number": f"L{i}",
                "x": left_x,
                "y": y,
                "net": net_id,
                "net_name": f"N{net_id}",
                "width": QFN_PAD_SIZE,
                "height": QFN_PAD_SIZE,
            }
        )
        net_id += 1
        right_net = SHARED_NET if shared_row == i else net_id
        pads.append(
            {
                "number": f"R{i}",
                "x": right_x,
                "y": y,
                "net": right_net,
                "net_name": f"N{right_net}",
                "width": QFN_PAD_SIZE,
                "height": QFN_PAD_SIZE,
            }
        )
        net_id += 1
    return pads


def _overlapping_bodies_board(force_python: bool, swap_insertion_order: bool = False) -> Autorouter:
    """Two QFN-class bodies whose surcharge regions overlap.

    U1 and U2 interleave pad columns (U1 x=18/22, U2 x=21/25) so their
    pad bboxes overlap in x=21..22 and the margin-grown regions share a
    cell band on every pad row.  ``SHARED_NET`` has one pad on U1 and none
    on U2; ``TRUNK_NET`` has no pad on either body.
    """
    router = Autorouter(width=60.0, height=30.0, force_python=force_python)
    u1 = _qfn_pad_columns(QFN_LEFT_X, QFN_RIGHT_X, 1, shared_row=QFN_ROWS - 1)
    u2 = _qfn_pad_columns(U2_LEFT_X, U2_RIGHT_X, 1000)
    if swap_insertion_order:
        router.add_component("U2", u2)
        router.add_component("U1", u1)
    else:
        router.add_component("U1", u1)
        router.add_component("U2", u2)
    return router


def _python_charge_map(finder, net: int) -> dict[tuple[int, int, int], float]:
    """Summed per-cell under-body charge the Python A* adds for ``net``.

    Computed from the engine's own state (mask planes + per-net non-local
    bitmasks) exactly the way the hot loop does: one penalty per covering
    region the net is not local to, summed across planes.
    """
    planes = finder._under_body_mask_planes()
    assert planes is not None
    nonlocal_masks, penalty = finder._under_body_net_state(net)
    charge: dict[tuple[int, int, int], float] = {}
    for plane, nonlocal_mask in zip(planes, nonlocal_masks, strict=True):
        for layer, gy, gx in zip(*np.nonzero(plane), strict=True):
            cover = int(plane[layer, gy, gx]) & nonlocal_mask
            if cover:
                key = (int(layer), int(gy), int(gx))
                charge[key] = charge.get(key, 0.0) + cover.bit_count() * penalty
    return charge


def _cpp_charge_map(finder: CppPathfinder, net: int) -> dict[tuple[int, int, int], float]:
    """Summed per-cell under-body charge the C++ lookup applies for ``net``.

    Replays the C++ builder's summing semantics (``out[key] += penalty``
    per budget), multiplicities preserved.
    """
    charge: dict[tuple[int, int, int], float] = {}
    for budget in finder._under_body_budgets_for_net(net):
        for gx in range(budget.gx1, budget.gx2 + 1):
            for gy in range(budget.gy1, budget.gy2 + 1):
                key = (int(budget.layer), gy, gx)
                charge[key] = charge.get(key, 0.0) + float(budget.overflow_penalty)
    return charge


def _many_bodies_board(count: int, force_python: bool, reverse: bool = False) -> Autorouter:
    """``count`` disjoint QFN-class bodies (four 0.5mm-pitch pads each).

    Bodies tile an 8-column grid with enough spacing that their
    margin-grown regions stay disjoint; every body carries four private
    nets, so ``TRUNK_NET`` is local to none of them.
    """
    router = Autorouter(width=38.0, height=36.0, force_python=force_python)
    bodies: list[tuple[str, list[dict]]] = []
    for idx in range(count):
        col = idx % 8
        row = idx // 8
        x0 = 2.0 + col * 4.0
        y0 = 2.0 + row * 3.5
        pads: list[dict] = []
        for k, (dx, dy) in enumerate(((0.0, 0.0), (0.5, 0.0), (0.0, 0.5), (0.5, 0.5))):
            net = 2000 + idx * 4 + k
            pads.append(
                {
                    "number": f"{k + 1}",
                    "x": x0 + dx,
                    "y": y0 + dy,
                    "net": net,
                    "net_name": f"B{net}",
                    "width": QFN_PAD_SIZE,
                    "height": QFN_PAD_SIZE,
                }
            )
        bodies.append((f"U{idx + 1}", pads))
    if reverse:
        bodies.reverse()
    for ref, pads in bodies:
        router.add_component(ref, pads)
    return router


def _segment_crosses_body(x1: float, y1: float, x2: float, y2: float, step: float = 0.05) -> bool:
    """True when the segment touches the body rectangle (conservative)."""
    # Bounding-box rejection first.
    if max(x1, x2) < BODY_X1 or min(x1, x2) > BODY_X2:
        return False
    if max(y1, y2) < BODY_Y1 or min(y1, y2) > BODY_Y2:
        return False
    length = math.hypot(x2 - x1, y2 - y1)
    steps = max(2, int(length / step))
    for i in range(steps + 1):
        t = i / steps
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        if BODY_X1 <= x <= BODY_X2 and BODY_Y1 <= y <= BODY_Y2:
            return True
    return False


def _trunk_routes(routes: list) -> list:
    return [r for r in routes if r.net == TRUNK_NET and not r.is_escape]


def _trunk_body_crossings(routes: list) -> list:
    """Trunk copper (segments on any layer) intersecting the body bbox."""
    crossings = []
    for route in _trunk_routes(routes):
        for seg in route.segments:
            if _segment_crosses_body(seg.x1, seg.y1, seg.x2, seg.y2):
                crossings.append(seg)
    return crossings


# ---------------------------------------------------------------------------
# Region derivation (unit)
# ---------------------------------------------------------------------------


class TestRegionDerivation:
    def test_qfn_body_region_with_local_nets(self) -> None:
        router = _qfn_board(force_python=True)
        regions = router.grid.under_body_surcharges
        assert len(regions) == 1
        region = regions[0]
        assert region.ref == "U1"
        assert set(region.layers) == {0, 1}  # outer layers only
        qfn_nets = set(range(1, 2 * QFN_ROWS + 1))
        assert region.local_nets == qfn_nets
        assert TRUNK_NET not in region.local_nets
        # Pad bbox + one-cell margin at 0.1mm resolution.
        assert region.gx1 == pytest.approx(int(QFN_LEFT_X / 0.1) - 1)
        assert region.gx2 == pytest.approx(int(QFN_RIGHT_X / 0.1) + 1)
        assert region.gy1 == pytest.approx(int(QFN_Y0 / 0.1) - 1)
        assert region.gy2 == pytest.approx(int((QFN_Y0 + (QFN_ROWS - 1) * QFN_PITCH) / 0.1) + 1)

    def test_two_terminal_chip_never_gets_a_region(self) -> None:
        router = _qfn_board(force_python=True)
        # A 0402-class resistor: pad-to-pad spacing inside the pitch
        # threshold, but only two pads -- its body is never surcharged.
        router.add_component(
            "R1",
            [
                {
                    "number": "1",
                    "x": 30.0,
                    "y": 10.0,
                    "net": 200,
                    "net_name": "RA",
                    "width": 0.5,
                    "height": 0.5,
                },
                {
                    "number": "2",
                    "x": 31.0,
                    "y": 10.0,
                    "net": 201,
                    "net_name": "RB",
                    "width": 0.5,
                    "height": 0.5,
                },
            ],
        )
        regions = router.grid.under_body_surcharges
        assert [r.ref for r in regions] == ["U1"]

    def test_all_net0_pads_still_get_a_region(self) -> None:
        """The route-auto ``skip_nets`` shape: every U1 pad lands as net 0
        (real nets withheld as net-0 obstacles by the loader).  The region
        must survive with an EMPTY local set -- dropping it let the
        requested trunk route straight through the body at zero cost
        (review of #9635)."""
        router = _qfn_board(force_python=True)
        netless: list[dict] = []
        for pad in _qfn_pad_columns(QFN_LEFT_X, QFN_RIGHT_X, 1):
            netless.append({**pad, "net": 0, "net_name": ""})
        router.add_component("U9", netless)
        regions = router.grid.under_body_surcharges
        by_ref = {r.ref: r for r in regions}
        assert "U9" in by_ref
        assert by_ref["U9"].local_nets == frozenset()
        # A mixed component (one real net + net-0 pads) only exempts the
        # real net.
        assert by_ref["U1"].local_nets == set(range(1, 2 * QFN_ROWS + 1))

    def test_region_cache_invalidated_by_new_pads(self) -> None:
        router = _qfn_board(force_python=True)
        assert len(router.grid.under_body_surcharges) == 1
        # A second QFN-class body appears only after its pads land.
        second: list[dict] = []
        for i in range(QFN_ROWS):
            y = QFN_Y0 + i * QFN_PITCH
            second.append(
                {
                    "number": f"L{i}",
                    "x": 28.0,
                    "y": y,
                    "net": 300 + i,
                    "net_name": f"M{i}",
                    "width": QFN_PAD_SIZE,
                    "height": QFN_PAD_SIZE,
                }
            )
        router.add_component("U2", second)
        regions = router.grid.under_body_surcharges
        assert {r.ref for r in regions} == {"U1", "U2"}

    def test_disabled_cost_leaves_regions_dormant(self) -> None:
        router = _qfn_board(force_python=True)
        assert router.grid.under_body_surcharges  # regions exist
        router.rules.cost_under_body = 0.0
        planes = router.router._under_body_mask_planes()
        assert planes is None

    def test_region_covers_cell_helper(self) -> None:
        region = BodySurchargeRegion(
            ref="U1",
            gx1=10,
            gy1=10,
            gx2=20,
            gy2=20,
            layers=(0, 1),
            local_nets=frozenset({1}),
        )
        assert region_covers_cell(region, 15, 15, 0)
        assert not region_covers_cell(region, 15, 15, 2)  # inner layer
        assert not region_covers_cell(region, 21, 15, 0)
        assert not region_covers_cell(region, 15, 9, 1)


# ---------------------------------------------------------------------------
# Local-net exemption (both engines)
# ---------------------------------------------------------------------------


class TestLocalNetExemption:
    @requires_cpp
    def test_cpp_budgets_empty_for_local_net(self) -> None:
        router = _qfn_board(force_python=False)
        finder = router.router
        assert isinstance(finder, CppPathfinder)
        trunk_budgets = finder._under_body_budgets_for_net(TRUNK_NET)
        assert trunk_budgets, "non-local net must carry surcharge budgets"
        # One budget per region per outer layer.
        assert len(trunk_budgets) == 2
        assert {b.layer for b in trunk_budgets} == {0, 1}
        penalty = finder._rules.cost_under_body * finder._rules.cost_straight
        assert all(b.overflow_penalty == pytest.approx(penalty) for b in trunk_budgets)

        local_net = 1  # U1's own net
        assert finder._under_body_budgets_for_net(local_net) == []

    def test_python_state_exempts_local_net(self) -> None:
        router = _qfn_board(force_python=True)
        finder = router.router
        planes = finder._under_body_mask_planes()
        assert planes is not None and len(planes) == 1
        mask = planes[0]
        nonlocal_masks, penalty = finder._under_body_net_state(TRUNK_NET)
        assert nonlocal_masks == (1,)  # region 1's bit: the trunk is non-local
        assert penalty == pytest.approx(finder.rules.cost_under_body * finder.rules.cost_straight)
        # The body cells carry the region's bit.
        assert mask[0, int(16.0 / 0.1), int(20.0 / 0.1)] == 1
        nonlocal1, _ = finder._under_body_net_state(1)
        assert nonlocal1 == (0,)  # U1's own net is exempt

    @requires_cpp
    def test_charge_maps_identical_between_backends(self) -> None:
        """The summed per-cell under-body charge must be identical between
        the engines on a board with two OVERLAPPING body regions: the C++
        budget lookup sums overlapping budgets (``out[key] += penalty``),
        so the Python mirror must charge one penalty per covering
        non-local region (bitmask popcount), not at most one.  Asserted at
        the adapter level, comparing the full charge map -- multiplicity
        included -- not just the charged cell set."""
        py_router = _overlapping_bodies_board(force_python=True)
        cpp_router = _overlapping_bodies_board(force_python=False)

        py_finder = py_router.router
        cpp_finder = cpp_router.router
        assert isinstance(cpp_finder, CppPathfinder)

        regions = py_finder._under_body_regions()
        assert {r.ref for r in regions} == {"U1", "U2"}
        # The regions genuinely share cells (the overlap class under test):
        # their bboxes intersect in x on the shared pad rows.
        assert max(r.gx1 for r in regions) <= min(r.gx2 for r in regions)
        penalty = py_finder.rules.cost_under_body * py_finder.rules.cost_straight

        # Instance 1: a net local to neither body -- the overlap band must
        # carry the doubled charge on BOTH engines (the single-owner cell
        # map charged it once).
        py_trunk = _python_charge_map(py_finder, TRUNK_NET)
        cpp_trunk = _cpp_charge_map(cpp_finder, TRUNK_NET)
        assert py_trunk == cpp_trunk, "per-cell charge maps must agree, sums included"
        assert any(c == pytest.approx(2.0 * penalty) for c in py_trunk.values()), (
            "overlap cells must carry the doubled charge"
        )

        # Instance 2: a net with a pad on U1 only -- still charged for
        # U2's body in the overlap band, on both engines.
        py_shared = _python_charge_map(py_finder, SHARED_NET)
        cpp_shared = _cpp_charge_map(cpp_finder, SHARED_NET)
        assert py_shared == cpp_shared
        assert any(c == pytest.approx(penalty) for c in py_shared.values())

    def test_overlap_charge_is_insertion_order_independent(self) -> None:
        """The Python mirror's charge map must not depend on the order the
        components were added in (the single-owner array made the
        shared-net charge order-dependent: whichever body landed last owned
        the overlap cells)."""
        router_a = _overlapping_bodies_board(force_python=True)
        router_b = _overlapping_bodies_board(force_python=True, swap_insertion_order=True)
        for net in (TRUNK_NET, SHARED_NET):
            charge_a = _python_charge_map(router_a.router, net)
            charge_b = _python_charge_map(router_b.router, net)
            assert charge_a == charge_b, f"net {net}: charge map changed with add order"


# ---------------------------------------------------------------------------
# Boards with more fine-pitch bodies than one bitmask plane (>64)
# ---------------------------------------------------------------------------


class TestManyFinePitchBodiesNoCap:
    """Boards with more fine-pitch bodies than one int64 bitmask plane
    (review of #9635): the Python 64-region slice left body 65+ unpriced
    AND made the charged set depend on component insertion order, while
    the C++ budget adapter installs every region.  Every region must stay
    priced on both engines."""

    @pytest.mark.parametrize("count,expected_planes", [(64, 1), (65, 2)])
    def test_plane_count_at_the_64_65_boundary(self, count: int, expected_planes: int) -> None:
        router = _many_bodies_board(count, force_python=True)
        finder = router.router
        assert len(finder._under_body_regions()) == count
        planes = finder._under_body_mask_planes()
        assert planes is not None
        assert len(planes) == expected_planes
        # Cache identity on the unchanged grid: consecutive calls return
        # the SAME tuple (the truncating slice defeated the identity check
        # and rebuilt different planes on every call).
        assert finder._under_body_mask_planes() is planes

    def test_unrelated_net_charged_in_every_body_beyond_64(self) -> None:
        router = _many_bodies_board(65, force_python=True)
        finder = router.router
        penalty = finder.rules.cost_under_body * finder.rules.cost_straight
        charge = _python_charge_map(finder, TRUNK_NET)
        regions = finder._under_body_regions()
        # Every region's centre cell carries exactly one penalty for a net
        # local to no body -- including the 65th, which the old 64-region
        # slice left free in one insertion order and priced in the other.
        for region in regions:
            cx = (region.gx1 + region.gx2) // 2
            cy = (region.gy1 + region.gy2) // 2
            assert charge.get((0, cy, cx), 0.0) == pytest.approx(penalty), region.ref
        # Complete coverage: the regions are disjoint, so the charged cell
        # count is exactly the summed region area over both outer layers.
        expected_cells = sum(
            (r.gx2 - r.gx1 + 1) * (r.gy2 - r.gy1 + 1) * len(r.layers) for r in regions
        )
        assert len(charge) == expected_cells

    def test_beyond_64_charge_is_insertion_order_independent(self) -> None:
        router_a = _many_bodies_board(65, force_python=True)
        router_b = _many_bodies_board(65, force_python=True, reverse=True)
        charge_a = _python_charge_map(router_a.router, TRUNK_NET)
        charge_b = _python_charge_map(router_b.router, TRUNK_NET)
        assert charge_a == charge_b

    @requires_cpp
    def test_beyond_64_charge_matches_cpp(self) -> None:
        py_finder = _many_bodies_board(65, force_python=True).router
        cpp_finder = _many_bodies_board(65, force_python=False).router
        assert isinstance(cpp_finder, CppPathfinder)
        # The C++ adapter installs EVERY region (one budget per region per
        # outer layer) -- the parity the Python mirror must hold beyond
        # the 64th body too.
        budgets = cpp_finder._under_body_budgets_for_net(TRUNK_NET)
        assert len(budgets) == 65 * 2
        assert _python_charge_map(py_finder, TRUNK_NET) == _cpp_charge_map(cpp_finder, TRUNK_NET)


# ---------------------------------------------------------------------------
# End-to-end behaviour (both engines)
# ---------------------------------------------------------------------------


class TestTrunkAvoidsBody:
    def test_bidirectional_trunk_detours_around_body(self) -> None:
        """The bidirectional A* expansion charges the same under-body
        surcharge as the one-way search on BOTH expansion types (trace
        steps and via drops).  Review of #9635 finding 2: the expansion
        previously omitted the term, so ``route_auto`` over a long
        Manhattan distance (and the public ``--fallback-bidirectional``
        path) crossed bodies the one-way search detours around."""
        router = _qfn_board(force_python=True)
        finder = router.router
        assert not isinstance(finder, CppPathfinder)
        pads = [p for p in finder.grid.pads if p.net == TRUNK_NET]
        assert len(pads) == 2

        route = finder.route_bidirectional(pads[0], pads[1])
        assert route is not None, "the bidirectional search must route the trunk"
        assert route.segments
        for seg in route.segments:
            assert not _segment_crosses_body(seg.x1, seg.y1, seg.x2, seg.y2), (
                f"bidirectional A* put trunk copper through the body: {seg}"
            )

    @pytest.mark.parametrize("force_python", BACKENDS)
    def test_trunk_detours_around_body_and_all_nets_route(self, force_python: bool) -> None:
        router = _qfn_board(force_python=force_python)
        routes = router.route_all_negotiated(seed=42)

        trunk = _trunk_routes(routes)
        assert trunk, "the trunk net must route"

        # The audit repro's assertion, flipped: no trunk copper through the
        # body while a detour around it exists.
        crossings = _trunk_body_crossings(routes)
        assert not crossings, f"{len(crossings)} trunk segment(s) cross the QFN body bbox"

        # Still connected end to end (the detour is intact).
        for route in trunk:
            assert route.segments

    @pytest.mark.parametrize("force_python", BACKENDS)
    def test_trunk_routes_through_body_when_it_is_the_only_path(self, force_python: bool) -> None:
        """The surcharge is soft: a board whose only corridor is the
        under-body strip still routes (never strands a board)."""
        router = _qfn_board(force_python=force_python)
        # Wall the board off above and below the body so the under-body
        # corridor is the only west-east path (the audit's constrained
        # variant).  Obstacles are centre-based rectangles.
        wall_clearance_pad = 0.6
        top_y1 = BODY_Y2 + wall_clearance_pad
        router.add_obstacle(
            20.0,
            (top_y1 + 30.0) / 2,
            44.0,
            30.0 - top_y1 + 2 * wall_clearance_pad,
            LayerEnum.F_CU,
        )
        router.add_obstacle(
            20.0,
            (top_y1 + 30.0) / 2,
            44.0,
            30.0 - top_y1 + 2 * wall_clearance_pad,
            LayerEnum.B_CU,
        )
        bottom_y2 = BODY_Y1 - wall_clearance_pad
        router.add_obstacle(
            20.0,
            bottom_y2 / 2,
            44.0,
            bottom_y2 + 2 * wall_clearance_pad,
            LayerEnum.F_CU,
        )
        router.add_obstacle(
            20.0,
            bottom_y2 / 2,
            44.0,
            bottom_y2 + 2 * wall_clearance_pad,
            LayerEnum.B_CU,
        )

        routes = router.route_all_negotiated(seed=42)
        trunk = _trunk_routes(routes)
        assert trunk, "the trunk must still route through the under-body corridor"
        for route in trunk:
            assert route.segments
        crossings = _trunk_body_crossings(routes)
        assert crossings, "with no detour available the trunk legitimately uses the body"


# ---------------------------------------------------------------------------
# route-auto (RoutingOrchestrator path)
# ---------------------------------------------------------------------------


def _qfn_board_text() -> str:
    """Synthetic board file matching the in-memory repro."""
    nets = [f'(net {i} "/N{i}")' for i in range(1, 2 * QFN_ROWS + 1)]
    nets.append(f'(net {TRUNK_NET} "/{TRUNK_NET_NAME}")')

    def pad(number, x, y, net, name, size):
        return (
            f'    (pad "{number}" smd rect (at {x} {y}) (size {size} {size}) '
            f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{name}"))'
        )

    footprints = []
    qfn_pads = []
    net_id = 1
    for i in range(QFN_ROWS):
        y = QFN_Y0 + i * QFN_PITCH
        qfn_pads.append(pad(f"L{i}", QFN_LEFT_X, y, net_id, f"/N{net_id}", QFN_PAD_SIZE))
        net_id += 1
        qfn_pads.append(pad(f"R{i}", QFN_RIGHT_X, y, net_id, f"/N{net_id}", QFN_PAD_SIZE))
        net_id += 1
    footprints.append(
        '(footprint "Package_QFN:QFN-16"\n'
        '    (layer "F.Cu")\n'
        '    (uuid "00000000-0000-0000-0000-000000000001")\n'
        "    (at 0 0)\n"
        '    (property "Reference" "U1" (at 0 -3 0) (layer "F.SilkS"))\n'
        + "\n".join(qfn_pads)
        + "\n  )"
    )
    for ref, x in (("J1", 2.0), ("J2", 38.0)):
        footprints.append(
            '(footprint "Connector:PinHeader_1x01"\n'
            '    (layer "F.Cu")\n'
            f'    (uuid "00000000-0000-0000-0000-00000000000{6 if ref == "J1" else 7}")\n'
            "    (at 0 0)\n"
            f'    (property "Reference" "{ref}" (at 0 -1 0) (layer "F.SilkS"))\n'
            f"    {pad('1', x, 16.0, TRUNK_NET, f'/{TRUNK_NET_NAME}', 0.6)}\n"
            "  )"
        )

    return (
        "(kicad_pcb\n"
        "  (version 20240108)\n"
        '  (generator "test")\n'
        '  (generator_version "8.0")\n'
        "  (general (thickness 1.6))\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        "  (setup (pad_to_mask_clearance 0))\n"
        '  (net 0 "")\n'
        + "\n  ".join(nets)
        + "\n  (gr_rect (start 0.0 0.0) (end 40.0 30.0) (stroke (width 0.1) "
        '(type default)) (fill none) (layer "Edge.Cuts"))\n' + "\n  ".join(footprints) + "\n)\n"
    )


class TestRouteAutoAvoidsBody:
    def test_route_auto_trunk_avoids_body(self, tmp_path: Path, capsys) -> None:
        from kicad_tools.cli import main as kct_main

        board = tmp_path / "qfn_board.kicad_pcb"
        board.write_text(_qfn_board_text())
        out = tmp_path / "routed.kicad_pcb"

        rc = kct_main(
            [
                "route-auto",
                str(board),
                "--net",
                f"/{TRUNK_NET_NAME}",
                "--output",
                str(out),
                "--format",
                "json",
            ]
        )
        doc = json.loads(capsys.readouterr().out)
        assert rc == 0, doc
        assert doc["success"], doc

        from kicad_tools.schema.pcb import PCB

        routed = PCB.load(str(out))
        # The parsed Segment exposes ``net_number`` / ``start`` / ``end``
        # (schema/pcb.py): the earlier ``getattr(seg, "net", ...)`` filter
        # matched nothing, so the body-crossing check below never ran
        # (review of #9635).
        trunk_segments = [s for s in routed.segments if s.net_number == TRUNK_NET]
        assert trunk_segments, "the saved board must contain trunk copper"
        for seg in trunk_segments:
            assert not _segment_crosses_body(
                float(seg.start[0]), float(seg.start[1]), float(seg.end[0]), float(seg.end[1])
            ), f"route-auto put trunk copper through the body: {seg}"


# ---------------------------------------------------------------------------
# The ORIGINAL #9622 audit board (review of #9635: the replacement board
# above selects HIERARCHICAL_DIFF_PAIR, which already avoided the body; on
# the original board the CLI selected GLOBAL_WITH_REPAIR and its coarse
# corridor emitted waypoint segments straight through U1 without ever
# consulting the A* under-body cost).
# ---------------------------------------------------------------------------

# U1 body bbox in board coordinates (footprint at (20,12); net-less pad
# rows at x 17.25..22.75 on y 9.2 and 15.2).
AUDIT_BODY_BBOX = (17.25, 9.2, 22.75, 15.2)


def _audit_board_text() -> str:
    """The original audit fixture: a net-less 0.5mm-pitch QFN pad grid at
    the board centre and one TRUNK net whose B.Cu pads sit at (4,12) and
    (36,12) -- a dead horizontal shot through the middle of U1's body."""
    u1_pads = []
    for i in range(12):
        x = -2.75 + i * 0.5
        u1_pads.append(
            f'\t\t(pad "T{i}" smd rect (at {x:.2f} -2.80) (size 0.25 0.3) '
            '(layers "F.Cu" "F.Paste" "F.Mask"))'
        )
        u1_pads.append(
            f'\t\t(pad "B{i}" smd rect (at {x:.2f} 3.20) (size 0.25 0.3) '
            '(layers "F.Cu" "F.Paste" "F.Mask"))'
        )
    fp_u1 = "\n".join(u1_pads)
    return f"""(kicad_pcb
\t(version 20240108)
\t(generator "row9_audit_fixture")
\t(generator_version "8.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(36 "B.SilkS" user "B.Silkscreen")
\t\t(37 "F.SilkS" user "F.Silkscreen")
\t\t(38 "B.Mask" user)
\t\t(39 "F.Mask" user)
\t\t(44 "Edge.Cuts" user)
\t)
\t(setup (pad_to_mask_clearance 0.05))
\t(net 0 "")
\t(net 1 "TRUNK")

\t(gr_rect (start 0 0) (end 40 30) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))

\t(footprint "Audit:U1_QFN"
\t\t(layer "F.Cu")
\t\t(uuid "fp-u1")
\t\t(at 20.0 12.0)
\t\t(attr smd)
\t\t(fp_text reference "U1" (at 0 -4) (layer "F.SilkS"))
\t\t(fp_text value "QFN" (at 0 4.5) (layer "F.Fab"))
{fp_u1}
\t)

\t(footprint "Audit:TRUNK"
\t\t(layer "B.Cu")
\t\t(uuid "fp-trunk")
\t\t(at 0 0)
\t\t(attr smd)
\t\t(fp_text reference "T" (at 1 -1) (layer "B.SilkS"))
\t\t(fp_text value "trunk" (at 1 1) (layer "B.Fab"))
\t\t(pad "A" smd rect (at 4.00 12.00) (size 0.6 0.6) (layers "B.Cu" "B.Paste" "B.Mask") (net 1 "TRUNK"))
\t\t(pad "B" smd rect (at 36.00 12.00) (size 0.6 0.6) (layers "B.Cu" "B.Paste" "B.Mask") (net 1 "TRUNK"))
\t)
)
"""


def _seg_crosses_box(
    x1: float, y1: float, x2: float, y2: float, box: tuple[float, float, float, float]
) -> bool:
    """True when the segment touches ``box`` (sampled, as in the audit)."""
    bx1, by1, bx2, by2 = box
    for i in range(81):
        t = i / 80
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        if bx1 <= x <= bx2 and by1 <= y <= by2:
            return True
    return False


class TestRouteAutoOriginalAuditBoard:
    def test_route_auto_trunk_avoids_body_on_original_audit_board(
        self, tmp_path: Path, capsys
    ) -> None:
        """The corridor-materialising strategies must not accept copper the
        A* under-body cost exists to prevent: the coarse corridor search
        prices the body, so the emitted waypoints detour around it while a
        detour exists (review of #9635 finding 1)."""
        from kicad_tools.cli import main as kct_main

        board = tmp_path / "audit.kicad_pcb"
        board.write_text(_audit_board_text())
        out = tmp_path / "routed.kicad_pcb"

        rc = kct_main(
            [
                "route-auto",
                str(board),
                "--net",
                "TRUNK",
                "--output",
                str(out),
                "--format",
                "json",
            ]
        )
        doc = json.loads(capsys.readouterr().out)
        assert rc == 0, doc
        assert doc["success"], doc

        from kicad_tools.schema.pcb import PCB

        routed = PCB.load(str(out))
        # Trunk copper on BOTH outer layers is checked (the pre-fix corridor
        # wrote F.Cu segments through the body).
        trunk_segments = [s for s in routed.segments if s.net_number == 1]
        assert trunk_segments, "the saved board must contain trunk copper"
        for seg in trunk_segments:
            assert not _seg_crosses_box(
                float(seg.start[0]),
                float(seg.start[1]),
                float(seg.end[0]),
                float(seg.end[1]),
                AUDIT_BODY_BBOX,
            ), f"route-auto put trunk copper through U1's body: {seg}"
