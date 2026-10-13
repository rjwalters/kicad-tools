"""Fine-pitch neighbour entry-lane soft cost (Issue #6382).

On board 05 the grid router's wide PHASE_C escape left U2.9 straight, turned
45 degrees about 0.4 mm past the tip and crossed U2.10's and U2.11's
straight-out lanes 1.0 and 1.7 mm from their tips, stranding GND_SENSE and VM.
``pad_lanes`` charges foreign nets a soft per-cell cost inside a lane that runs
``LANE_LENGTH_MM`` past the tip and tiles the column across (each lane reaches
the midpoint of the gap to its neighbour), via the existing #3143
``PadChannelBudget`` primitive.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import CppPathfinder, is_cpp_available, router_cpp
from kicad_tools.router.fine_pitch_escape import detect_fine_pitch_regions
from kicad_tools.router.pad_lanes import (
    DISABLE_ENV,
    LANE_LENGTH_MM,
    LANE_MAX_GAP_FILL_MM,
    LANE_PENALTY_FACTOR,
    VIA_DISC_ENV,
    build_pad_lane_budgets,
    compute_pad_lanes,
    lane_grid_rect,
    lane_half_across,
    pad_lane,
    pad_lanes_disabled,
    via_disc_enabled,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

requires_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not available")

# Board 05 U2 geometry: 1.575 x 0.4 mm pads at 0.65 mm pitch.
PITCH = 0.65
PAD_LONG = 1.575
PAD_SHORT = 0.4
LEFT_X = 10.0
RIGHT_X = 15.0
Y0 = 10.0
LEFT_TIP = LEFT_X - PAD_LONG / 2


@pytest.fixture(autouse=True)
def _clear_lane_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (DISABLE_ENV, VIA_DISC_ENV, "KCT_PAD_LANE_LENGTH_MM", "KCT_PAD_LANE_PENALTY"):
        monkeypatch.delenv(name, raising=False)


def _column_pads(rows: int = 4) -> list[Pad]:
    pads: list[Pad] = []
    for i in range(rows):
        y = Y0 + i * PITCH
        pads.append(
            Pad(
                x=LEFT_X,
                y=y,
                width=PAD_LONG,
                height=PAD_SHORT,
                net=i + 1,
                net_name=f"N{i + 1}",
                ref="U1",
                pin=str(i + 1),
            )
        )
        pads.append(
            Pad(
                x=RIGHT_X,
                y=y,
                width=PAD_LONG,
                height=PAD_SHORT,
                net=10 + i,
                net_name=f"R{i}",
                ref="U1",
                pin=str(2 * rows - i),
            )
        )
    return pads


class _Grid:
    def __init__(self, resolution: float, cols: int = 1000, rows: int = 1000) -> None:
        self.resolution = resolution
        self.origin_x = 0.0
        self.origin_y = 0.0
        self.cols = cols
        self.rows = rows


# ---------------------------------------------------------------------------
# Lane geometry
# ---------------------------------------------------------------------------


def test_left_column_lane_starts_at_tip_and_extends_away_from_body() -> None:
    lanes = {lane.pad_key: lane for lane in compute_pad_lanes(_column_pads())}
    assert len(lanes) == 8
    for i in range(4):
        lane = lanes[("U1", str(i + 1))]
        y = Y0 + i * PITCH
        assert lane.axis == "x"
        assert lane.direction == -1
        assert lane.x2 == pytest.approx(LEFT_TIP)
        assert lane.x1 == pytest.approx(LEFT_TIP - LANE_LENGTH_MM)
        # One pitch wide: the pad's short side plus half of each inter-pad
        # gap, so adjacent lanes tile the column (not widened by trace width
        # or clearance).
        assert lane.y2 - lane.y1 == pytest.approx(PITCH)
        assert (lane.y1 + lane.y2) / 2 == pytest.approx(y)
        assert lane.net == i + 1


def test_right_column_lane_points_outward() -> None:
    lanes = {lane.pad_key: lane for lane in compute_pad_lanes(_column_pads())}
    lane = lanes[("U1", "8")]
    assert lane.direction == 1
    assert lane.x1 == pytest.approx(RIGHT_X + PAD_LONG / 2)
    assert lane.x2 == pytest.approx(RIGHT_X + PAD_LONG / 2 + LANE_LENGTH_MM)
    assert lane.net == 10


def test_vertical_pad_lane_runs_along_y() -> None:
    top = Pad(x=5.0, y=2.0, width=0.3, height=1.2, net=7, net_name="T", ref="U9", pin="1")
    lane = pad_lane(top, body_center=(5.0, 5.0), length=1.5)
    assert lane is not None
    assert lane.axis == "y"
    assert lane.direction == -1
    assert lane.y2 == pytest.approx(1.4)
    assert lane.y1 == pytest.approx(-0.1)
    assert lane.x2 - lane.x1 == pytest.approx(0.3)


@pytest.mark.parametrize(
    "overrides",
    [
        {"net": 0},  # NC pad
        {"width": 0.5, "height": 0.5},  # square pad: no long axis
        {"steiner_point": True},  # virtual pad
        {"escape_terminal": True},  # escape-terminal pad
        {"rotation": 45.0},  # non-cardinal
        {"through_hole": True},
    ],
)
def test_no_lane_for_ineligible_pads(overrides: dict) -> None:
    kwargs = {
        "x": LEFT_X,
        "y": Y0,
        "width": PAD_LONG,
        "height": PAD_SHORT,
        "net": 3,
        "net_name": "N",
        "ref": "U1",
        "pin": "1",
    }
    kwargs.update(overrides)
    assert pad_lane(Pad(**kwargs), body_center=(12.5, Y0)) is None


def test_no_lane_for_pad_straddling_body_centre() -> None:
    exposed = Pad(x=12.5, y=Y0, width=3.0, height=2.0, net=5, net_name="GND", ref="U1", pin="EP")
    assert pad_lane(exposed, body_center=(12.5, Y0)) is None


def test_pad_wide_lane_for_isolated_pad_and_capped_gap_fill() -> None:
    lone = Pad(
        x=LEFT_X, y=Y0, width=PAD_LONG, height=PAD_SHORT, net=1, net_name="N", ref="U1", pin="1"
    )
    assert lane_half_across(lone, [lone]) == pytest.approx(PAD_SHORT / 2)
    far = Pad(
        x=LEFT_X,
        y=Y0 + 5.0,
        width=PAD_LONG,
        height=PAD_SHORT,
        net=2,
        net_name="M",
        ref="U1",
        pin="2",
    )
    assert lane_half_across(lone, [lone, far]) == pytest.approx(
        PAD_SHORT / 2 + LANE_MAX_GAP_FILL_MM
    )
    # A pad in another column (different x) is not a same-column neighbour.
    other_col = Pad(
        x=RIGHT_X,
        y=Y0 + 0.1,
        width=PAD_LONG,
        height=PAD_SHORT,
        net=3,
        net_name="O",
        ref="U1",
        pin="3",
    )
    assert lane_half_across(lone, [lone, other_col]) == pytest.approx(PAD_SHORT / 2)


def test_adjacent_lanes_tile_without_overlap_on_grid() -> None:
    """Adjacent lanes share a boundary and claim disjoint, contiguous cell rows.

    The inter-pad gap is covered (a via centred in it is charged), and each
    pad's own centreline row lies only in its own lane, so a straight escape
    never pays for the next lane.
    """
    left = [lane for lane in compute_pad_lanes(_column_pads()) if lane.direction == -1]
    left.sort(key=lambda lane: lane.y1)
    for a, b in zip(left, left[1:], strict=False):
        assert a.y2 == pytest.approx(b.y1)
    for res in (0.05, 0.065, 0.1):
        grid = _Grid(res)
        rects = sorted(r for r in (lane_grid_rect(lane, grid) for lane in left) if r is not None)
        assert len(rects) == len(left)
        rows = sorted((r[1], r[3]) for r in rects)
        for (_, hi), (lo, _) in zip(rows, rows[1:], strict=False):
            assert hi + 1 == lo, f"lanes overlap or leave a gap at resolution {res}"
        for lane, (_, gy1, _, gy2) in zip(left, sorted(rects, key=lambda r: r[1]), strict=True):
            centre_row = round(((lane.y1 + lane.y2) / 2) / res)
            assert gy1 <= centre_row <= gy2


# ---------------------------------------------------------------------------
# Budgets and merge with #3143
# ---------------------------------------------------------------------------


def _region_for(pads: list[Pad]):
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15)
    regions = detect_fine_pitch_regions(pads, rules)
    assert regions, "fixture must qualify as a fine-pitch region"
    return regions


@requires_cpp
def test_budgets_carry_source_net_layer_and_penalty() -> None:
    pads = _column_pads()
    budgets = build_pad_lane_budgets(_Grid(0.065), pads, _region_for(pads), cost_straight=1.0)
    assert len(budgets) == 8
    assert {b.source_net for b in budgets} == {1, 2, 3, 4, 10, 11, 12, 13}
    for b in budgets:
        assert b.layer == -1
        assert b.overflow_penalty == pytest.approx(LANE_PENALTY_FACTOR)


@requires_cpp
def test_budgets_empty_without_regions_or_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    pads = _column_pads()
    assert build_pad_lane_budgets(_Grid(0.065), pads, [], cost_straight=1.0) == []
    monkeypatch.setenv(DISABLE_ENV, "1")
    assert pad_lanes_disabled()
    assert build_pad_lane_budgets(_Grid(0.065), pads, _region_for(pads), cost_straight=1.0) == []


def _budget(net: int, gx: int = 0):
    b = router_cpp.PadChannelBudget()
    b.gx1, b.gy1, b.gx2, b.gy2 = gx, 0, gx + 1, 1
    b.layer = -1
    b.overflow_penalty = 0.5
    b.source_net = net
    return b


@requires_cpp
def test_lane_and_channel_budgets_coexist() -> None:
    pf = CppPathfinder.__new__(CppPathfinder)
    pf._pad_channel_budgets = []
    pf._pad_lane_budgets = []
    channel = [_budget(1), _budget(2)]
    lanes = [_budget(2, gx=5), _budget(3, gx=7)]

    pf.set_pad_channel_budgets(channel)
    pf.set_pad_lane_budgets(lanes)
    assert pf._filter_pad_channel_budgets_for_net(99) == channel + lanes
    # Setting the #3143 list again does not drop the lanes ...
    pf.set_pad_channel_budgets([channel[0]])
    assert pf._filter_pad_channel_budgets_for_net(99) == [channel[0]] + lanes
    # ... and clearing the lanes does not drop the #3143 list.
    pf.set_pad_lane_budgets(None)
    assert pf._filter_pad_channel_budgets_for_net(99) == [channel[0]]
    # The own-net filter applies to both lists.
    pf.set_pad_channel_budgets(channel)
    pf.set_pad_lane_budgets(lanes)
    assert [b.source_net for b in pf._filter_pad_channel_budgets_for_net(2)] == [1, 3]


# ---------------------------------------------------------------------------
# Synthetic routing fixture
# ---------------------------------------------------------------------------


def _build_router(
    wide_target: tuple[float, float], *, wall: bool = False
) -> tuple[Autorouter, float]:
    """Two-column package: WIDE (0.4 mm) on pin 1, its neighbour NB on pin 2.

    NB's target sits straight out, 1.7 mm past its tip (inside its lane).
    """
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.15,
        grid_resolution=0.05,
    )
    router = Autorouter(
        width=20,
        height=20,
        rules=rules,
        net_class_map={"WIDE": NetClassRouting(name="WIDE", trace_width=0.4)},
    )
    names = {1: "WIDE", 2: "NB", 3: "N3", 4: "N4"}
    pins = []
    for i in range(4):
        y = Y0 + i * PITCH
        pins.append(
            {
                "number": str(i + 1),
                "x": LEFT_X,
                "y": y,
                "width": PAD_LONG,
                "height": PAD_SHORT,
                "net": i + 1,
                "net_name": names[i + 1],
            }
        )
        pins.append(
            {
                "number": str(8 - i),
                "x": RIGHT_X,
                "y": y,
                "width": PAD_LONG,
                "height": PAD_SHORT,
                "net": 0,
                "net_name": "",
            }
        )
    router.add_component("U1", pins)
    nb_y = Y0 + PITCH
    router.add_component(
        "D1",
        [
            {
                "number": "1",
                "x": wide_target[0],
                "y": wide_target[1],
                "width": 0.6,
                "height": 0.6,
                "net": 1,
                "net_name": "WIDE",
            }
        ],
    )
    router.add_component(
        "D2",
        [
            {
                "number": "1",
                "x": 7.5,
                "y": nb_y,
                "width": 0.6,
                "height": 0.6,
                "net": 2,
                "net_name": "NB",
            }
        ],
    )
    if wall:
        # Through-hole keepout covering x in [4.0, 7.2]: every path to the
        # target must cross NB's row inside its lane (which ends at 7.2125).
        router.add_component(
            "K1",
            [
                {
                    "number": "1",
                    "x": 5.6,
                    "y": 11.0,
                    "width": 3.2,
                    "height": 6.0,
                    "net": 0,
                    "net_name": "",
                    "through_hole": True,
                }
            ],
        )
    router.grid.set_fine_pitch_regions(
        detect_fine_pitch_regions(list(router.pads.values()), router.rules)
    )
    return router, nb_y


def _lane_crossings(routes, y: float) -> list[float]:
    """Distances from the left tip at which ``routes`` cross the row ``y``."""
    out: list[float] = []
    for route in routes:
        for s in route.segments:
            if s.y1 != s.y2 and (s.y1 - y) * (s.y2 - y) <= 0:
                x = s.x1 + (y - s.y1) / (s.y2 - s.y1) * (s.x2 - s.x1)
                if x < LEFT_TIP:
                    out.append(LEFT_TIP - x)
        for v in route.vias:
            if abs(v.y - y) <= PAD_SHORT / 2 + v.diameter / 2 and v.x < LEFT_TIP:
                out.append(LEFT_TIP - v.x)
    return out


def _route_pair(monkeypatch: pytest.MonkeyPatch, *, disabled: bool):
    if disabled:
        monkeypatch.setenv(DISABLE_ENV, "1")
    router, nb_y = _build_router((4.0, 13.5))
    installed = router._install_pad_lane_budgets()
    wide = router.route_net(1)
    nb = router.route_net(2)
    return installed, wide, nb, nb_y


@requires_cpp
def test_wide_net_crosses_neighbour_lane_beyond_lane_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With lanes, WIDE turns past NB's lane and NB routes straight out.

    Fails on unfixed main: there (equivalently, under the escape hatch, see
    the next test) WIDE crosses NB's row 0.81 mm from the tip and NB needs
    two vias to get past it.
    """
    installed, wide, nb, nb_y = _route_pair(monkeypatch, disabled=False)
    assert installed == 4
    assert wide, "WIDE must route"
    crossings = _lane_crossings(wide, nb_y)
    assert crossings, "WIDE must cross NB's row to reach its target"
    assert min(crossings) >= LANE_LENGTH_MM, crossings
    assert nb, "NB must route"
    assert sum(len(r.vias) for r in nb) == 0
    for route in nb:
        for s in route.segments:
            assert s.y1 == pytest.approx(nb_y) and s.y2 == pytest.approx(nb_y)


@requires_cpp
def test_escape_hatch_reproduces_main_crossing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``KCT_DISABLE_PAD_LANES=1`` installs nothing and restores main's escape."""
    installed, wide, nb, nb_y = _route_pair(monkeypatch, disabled=True)
    assert installed == 0
    assert wide and nb
    assert min(_lane_crossings(wide, nb_y)) < LANE_LENGTH_MM
    assert sum(len(r.vias) for r in nb) > 0


@requires_cpp
def test_never_stricter_when_only_path_crosses_lane() -> None:
    """A soft cost: when every legal path crosses the lane, WIDE still routes."""
    router, nb_y = _build_router((8.0, 12.6), wall=True)
    assert router._install_pad_lane_budgets() == 4
    wide = router.route_net(1)
    assert wide, "WIDE must still route through the lane"
    assert min(_lane_crossings(wide, nb_y)) < LANE_LENGTH_MM


# ---------------------------------------------------------------------------
# Wiring into route_all_negotiated / escape hatch
# ---------------------------------------------------------------------------


@requires_cpp
def test_route_all_negotiated_installs_lanes_and_keeps_channel_budgets() -> None:
    router, _ = _build_router((4.0, 13.5))
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    channel = [_budget(99)]
    pathfinder.set_pad_channel_budgets(channel)
    router.route_all_negotiated(max_iterations=1, timeout=60)
    assert len(pathfinder._pad_lane_budgets) == 4
    assert pathfinder._pad_channel_budgets == channel


@requires_cpp
def test_escape_hatch_clears_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    router, _ = _build_router((4.0, 13.5))
    assert router._install_pad_lane_budgets() == 4
    monkeypatch.setenv(DISABLE_ENV, "1")
    assert router._install_pad_lane_budgets() == 0
    assert router.router._pad_lane_budgets == []


def test_python_backend_installs_nothing() -> None:
    router = Autorouter(width=20, height=20, force_python=True)
    assert router._install_pad_lane_budgets() == 0


# ---------------------------------------------------------------------------
# Via-disc charging (Issue #6382 follow-up, BUILD_VERSION 53)
# ---------------------------------------------------------------------------


ENTRY_POINTS = ("route", "route_resumable")


def _via_xs(
    disc: bool, *, band_penalty: float = 50.0, entry: str = "route_resumable"
) -> list[float]:
    """Route layer 0 -> layer 1 past a layer-1 band; return the via x(s).

    The band covers one grid column on layer 1 only, spanning the whole
    board height, so the route can only dodge it by switching layers on the
    near side (centre outside the band) or crossing on layer 0 and dropping
    after it.  A via dropped on the cell right after the band has its centre
    outside the band but its disc inside it.
    """
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.layers import LayerStack

    rules = DesignRules(
        trace_width=0.15,
        trace_clearance=0.1,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(width=10.0, height=10.0, rules=rules, layer_stack=LayerStack.two_layer())
    from kicad_tools.router.cpp_backend import CppGrid

    cgrid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(cgrid, rules, diagonal_routing=True)
    pf.set_routable_layers(cgrid.get_routable_indices())

    bx, _ = grid.world_to_grid(5.0, 5.0)
    budget = router_cpp.PadChannelBudget()
    budget.gx1, budget.gx2 = bx, bx
    budget.gy1, budget.gy2 = 0, grid.rows - 1
    budget.layer = 1
    budget.overflow_penalty = band_penalty
    budget.source_net = 99
    budget.charge_via_disc = disc

    # ``entry`` selects the one-shot ``route()`` or the resumable twin; each
    # has its own via-landing site that must use the disc-aware table.
    result = getattr(pf._impl, entry)(2.0, 5.0, 0, 8.0, 5.0, 1, 1, pad_channel_budgets=[budget])
    pf._impl.clear_search_state()
    assert result.success
    return [v.x for v in result.vias]


def test_via_disc_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-off: unset or ``0`` keeps centre-cell charging (main behaviour)."""
    assert via_disc_enabled() is False
    monkeypatch.setenv(VIA_DISC_ENV, "0")
    assert via_disc_enabled() is False
    monkeypatch.setenv(VIA_DISC_ENV, "1")
    assert via_disc_enabled() is True


@requires_cpp
def test_lane_budgets_flag_via_disc_only_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    pads = _column_pads()
    regions = _region_for(pads)
    budgets = build_pad_lane_budgets(_Grid(0.065), pads, regions, cost_straight=1.0)
    assert budgets and not any(b.charge_via_disc for b in budgets)
    monkeypatch.setenv(VIA_DISC_ENV, "1")
    budgets = build_pad_lane_budgets(_Grid(0.065), pads, regions, cost_straight=1.0)
    assert budgets and all(b.charge_via_disc for b in budgets)


@requires_cpp
def test_default_budget_does_not_charge_via_disc() -> None:
    assert router_cpp.PadChannelBudget().charge_via_disc is False


@requires_cpp
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_via_disc_overlapping_lane_is_charged(entry: str) -> None:
    """A via centred just outside a band, with its disc inside, pays for it."""
    centre_only = _via_xs(False, entry=entry)
    disc = _via_xs(True, entry=entry)
    assert len(centre_only) == 1 and len(disc) == 1
    # The disc-aware cost must keep the via's disc off the band.
    for x in disc:
        assert abs(x - 5.0) > 0.3 + 0.05 - 1e-6
    assert centre_only != disc, "via-disc charging must change the via site"


@requires_cpp
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_via_disc_never_stricter_when_band_is_unavoidable(entry: str) -> None:
    """The cost is soft: a huge penalty on every layer-1 cell still routes."""
    assert _via_xs(True, band_penalty=1000.0, entry=entry)
