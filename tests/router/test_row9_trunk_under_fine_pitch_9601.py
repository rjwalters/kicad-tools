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
from kicad_tools.router.cpp_backend import CppPathfinder, is_cpp_available
from kicad_tools.router.primitives import Layer as LayerEnum

requires_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not available")

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
        owner = router.router._under_body_owner_array()
        assert owner is None

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
        owner = finder._under_body_owner_array()
        assert owner is not None
        local, penalty = finder._under_body_net_state(TRUNK_NET)
        assert local == frozenset()
        assert penalty == pytest.approx(finder.rules.cost_under_body * finder.rules.cost_straight)
        # The body cells carry the region id.
        assert owner[0, int(16.0 / 0.1), int(20.0 / 0.1)] == 1
        local1, _ = finder._under_body_net_state(1)
        assert local1 == {1}

    def test_charge_maps_identical_between_backends(self) -> None:
        """The C++ per-cell charge map (budget bboxes x layers x penalty)
        must equal the Python owner array's non-zero cells cell-for-cell --
        the #4511-style mirror contract, asserted at the adapter level."""
        py_router = _qfn_board(force_python=True)
        cpp_router = _qfn_board(force_python=False)

        owner = py_router.router._under_body_owner_array()
        assert owner is not None
        penalty = py_router.router.rules.cost_under_body * py_router.rules.cost_straight
        py_cells = {
            (int(layer), int(gy), int(gx)) for layer, gy, gx in zip(*np.nonzero(owner), strict=True)
        }

        cpp_finder = cpp_router.router
        assert isinstance(cpp_finder, CppPathfinder)
        cpp_cells = set()
        for budget in cpp_finder._under_body_budgets_for_net(TRUNK_NET):
            for gx in range(budget.gx1, budget.gx2 + 1):
                for gy in range(budget.gy1, budget.gy2 + 1):
                    cpp_cells.add((int(budget.layer), gy, gx))
        # (The C++ lookup clamps bboxes to the grid extent; on this board no
        # region touches an edge, so the raw bbox equals the clamped one.)

        assert py_cells == cpp_cells
        assert penalty == pytest.approx(
            cpp_finder._rules.cost_under_body * cpp_finder._rules.cost_straight
        )


# ---------------------------------------------------------------------------
# End-to-end behaviour (both engines)
# ---------------------------------------------------------------------------


class TestTrunkAvoidsBody:
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


FIXTURE = Path(__file__).parent / "fixtures" / "row9_trunk_9601_qfn_board.kicad_pcb"


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
        for seg in routed.segments:
            if getattr(seg, "net", None) != TRUNK_NET:
                continue
            assert not _segment_crosses_body(
                float(seg.x1), float(seg.y1), float(seg.x2), float(seg.y2)
            ), f"route-auto put trunk copper through the body: {seg}"
