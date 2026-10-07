"""Grid-engine enforcement of board-file keepout rule areas (Issue #6008).

Before #6008 only the lattice engine read ``(zone ... (keepout ...))`` rule
areas; the default grid engine routed straight through them.  These tests pin
the grid behaviour on BOTH backends (pure Python and C++):

* a ``(tracks not_allowed)`` wall the only path must cross leaves the net
  unrouted instead of crossing it;
* a tracks-only wall on one layer is detoured through the other layer;
* a ``(vias not_allowed)`` area blocks vias but leaves tracks free;
* ``spatial_keepouts`` per-class filters narrow an area exactly as on the
  lattice engine (one shared parse, ``_lattice_keepout_projection``);
* the unrouted-cause classifier (#5944) names the rule area as the blocker;
* ``kct route`` no longer warns that the grid engine ignores rule areas.

Boards are synthetic S-expression strings placed away from the sheet origin,
so the board-relative -> sheet-absolute polygon shift is exercised.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.layers import Layer
from kicad_tools.router.rules import DesignRules

BACKENDS = [
    pytest.param(True, id="python"),
    pytest.param(
        False,
        id="cpp",
        marks=pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built"),
    ),
]

# Board outline (sheet coordinates).
BX0, BY0, BX1, BY1 = 100.0, 100.0, 130.0, 116.0
# The wall every left->right route has to cross.
WALL_X0, WALL_X1 = 113.0, 117.0


def _pad(number: str, x: float, y: float, net: int, net_name: str) -> str:
    return (
        f'(pad "{number}" smd rect (at {x} {y}) (size 0.6 0.6) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{net_name}"))'
    )


def _fp(ref: str, uid: int, x: float, y: float, pads: str) -> str:
    return f"""  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    {pads}
  )
"""


def _keepout(
    name: str,
    poly: list[tuple[float, float]],
    *,
    layers: str = '"F.Cu" "B.Cu"',
    tracks: str = "not_allowed",
    vias: str = "not_allowed",
    uid: int = 1,
) -> str:
    pts = " ".join(f"(xy {x} {y})" for x, y in poly)
    return f"""  (zone
    (net 0)
    (net_name "")
    (name "{name}")
    (layers {layers})
    (uuid "cccccccc-0000-0000-0000-0000000000{uid:02d}")
    (hatch edge 0.5)
    (keepout (tracks {tracks}) (vias {vias}) (pads allowed) (copperpour allowed))
    (polygon (pts {pts}))
  )
"""


def _wall(**kwargs: object) -> str:
    """Full-height strip between the two pads."""
    poly = [(WALL_X0, BY0 - 1), (WALL_X1, BY0 - 1), (WALL_X1, BY1 + 1), (WALL_X0, BY1 + 1)]
    return _keepout("wall", poly, **kwargs)  # type: ignore[arg-type]


def _board(extra: str = "", *, two_nets: bool = False) -> str:
    parts = [
        _fp("R1", 10, 104.0, 108.0, _pad("1", 0, 0, 1, "/SIG")),
        _fp("R2", 11, 126.0, 108.0, _pad("1", 0, 0, 1, "/SIG")),
    ]
    nets = '  (net 0 "")\n  (net 1 "/SIG")\n'
    if two_nets:
        parts = [
            _fp("R1", 10, 104.0, 103.0, _pad("1", 0, 0, 1, "/HV_A")),
            _fp("R2", 11, 126.0, 103.0, _pad("1", 0, 0, 1, "/HV_A")),
            _fp("R3", 12, 104.0, 113.0, _pad("1", 0, 0, 2, "/HV_B")),
            _fp("R4", 13, 126.0, 113.0, _pad("1", 0, 0, 2, "/HV_B")),
        ]
        nets = '  (net 0 "")\n  (net 1 "/HV_A")\n  (net 2 "/HV_B")\n'
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
{nets}  (gr_rect (start {BX0} {BY0}) (end {BX1} {BY1})
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{"".join(parts)}{extra})
"""


def _rules() -> DesignRules:
    return DesignRules(
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
    )


def _load(tmp_path: Path, text: str, force_python: bool):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(text)
    router, _net_map = load_pcb_for_routing(
        str(pcb),
        rules=_rules(),
        use_pcb_rules=False,
        validate_drc=False,
        force_python=force_python,
    )
    return router


def _route(router) -> list:
    router.route_all(suppress_no_timeout_warning=True)
    return list(router.routes)


def _segments(routes, layer: Layer | None = None):
    for route in routes:
        for seg in route.segments:
            if layer is None or seg.layer == layer:
                yield seg


def _vias(routes):
    for route in routes:
        yield from route.vias


def _crosses_band(seg, x0: float, x1: float, half: float) -> bool:
    """Copper of ``seg`` (half-width ``half``) overlaps the x band ``[x0, x1]``."""
    lo, hi = min(seg.x1, seg.x2) - half, max(seg.x1, seg.x2) + half
    return hi > x0 and lo < x1


def _is_cpp(router) -> bool:
    return type(router.router).__name__ == "CppPathfinder"


# ---------------------------------------------------------------------------
# Rasterisation / grid primitives
# ---------------------------------------------------------------------------


def test_rasterise_polygon_covers_area_cells(tmp_path: Path) -> None:
    from kicad_tools.router.rule_area_grid import rasterise_polygon

    router = _load(tmp_path, _board(), force_python=True)
    grid = router.grid
    poly = [(110.0, 105.0), (112.0, 105.0), (112.0, 107.0), (110.0, 107.0)]
    gx0, gy0, mask = rasterise_polygon(grid, poly)
    inside = grid.world_to_grid(111.0, 106.0)
    outside = grid.world_to_grid(113.0, 106.0)
    assert mask[inside[1] - gy0, inside[0] - gx0]
    h, w = mask.shape
    ox, oy = outside[0] - gx0, outside[1] - gy0
    assert not (0 <= ox < w and 0 <= oy < h and mask[oy, ox])
    # 2 mm square at 0.1 mm: 21 x 21 nodes inside / on the boundary.
    assert mask.sum() == pytest.approx(21 * 21, abs=4 * 21)


@pytest.mark.parametrize("force_python", BACKENDS)
def test_track_area_becomes_static_cells_via_area_does_not(
    tmp_path: Path, force_python: bool
) -> None:
    text = _board(
        _keepout(
            "tracks",
            [(108.0, 104.0), (110.0, 104.0), (110.0, 106.0), (108.0, 106.0)],
            vias="allowed",
            uid=1,
        )
        + _keepout(
            "vias",
            [(118.0, 110.0), (120.0, 110.0), (120.0, 112.0), (118.0, 112.0)],
            tracks="allowed",
            uid=2,
        )
    )
    router = _load(tmp_path, text, force_python)
    assert router._install_grid_rule_area_keepouts() == 2
    grid = router.grid
    tx, ty = grid.world_to_grid(109.0, 105.0)
    vx, vy = grid.world_to_grid(119.0, 111.0)
    for layer in (0, 1):
        assert grid._blocked[layer, ty, tx]
        assert grid._net[layer, ty, tx] == 0
        # The via-only area leaves the occupancy planes untouched.
        assert not grid._blocked[layer, vy, vx]
    assert grid.rule_area_via_blocked(vx, vy, 1, 4)
    assert not grid.rule_area_via_blocked(tx, ty, 1, 4)
    assert not grid.rule_area_trace_blocked(vx, vy, 0, 1, 2)
    # Idempotent: a second install registers nothing new.
    assert router._install_grid_rule_area_keepouts() == 2
    if not force_python:
        cpp = grid._cpp_grid._impl
        assert cpp.rule_area_keepout_count() == 1  # only the via-only area
        assert cpp.rule_area_via_blocked(vx, vy, 1, 4)
        assert not cpp.rule_area_via_blocked(tx, ty, 1, 4)
        assert cpp.at(tx, ty, 0).blocked


# ---------------------------------------------------------------------------
# Routing behaviour, both backends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force_python", BACKENDS)
def test_unrouted_without_keepout_routes_baseline(tmp_path: Path, force_python: bool) -> None:
    router = _load(tmp_path, _board(), force_python)
    routes = _route(router)
    assert routes, "baseline board must route"
    assert any(_crosses_band(s, WALL_X0, WALL_X1, 0.1) for s in _segments(routes))


@pytest.mark.parametrize("force_python", BACKENDS)
def test_full_wall_blocks_route_instead_of_crossing(tmp_path: Path, force_python: bool) -> None:
    router = _load(tmp_path, _board(_wall()), force_python)
    assert _is_cpp(router) is (not force_python)
    routes = _route(router)
    for seg in _segments(routes):
        assert not _crosses_band(seg, WALL_X0, WALL_X1, seg.width / 2), (
            f"segment crosses the tracks-not-allowed wall: {seg}"
        )
    for via in _vias(routes):
        assert not (WALL_X0 - via.diameter / 2 < via.x < WALL_X1 + via.diameter / 2)
    assert not any(r.net == 1 and r.segments for r in routes if _spans(r))


def _spans(route) -> bool:
    xs = [p for s in route.segments for p in (s.x1, s.x2)]
    return bool(xs) and min(xs) < WALL_X0 and max(xs) > WALL_X1


@pytest.mark.parametrize("force_python", BACKENDS)
def test_front_only_track_wall_detours_through_back_layer(
    tmp_path: Path, force_python: bool
) -> None:
    router = _load(tmp_path, _board(_wall(layers='"F.Cu"', vias="allowed")), force_python)
    routes = _route(router)
    assert routes, "the back layer is open -- the net must route"
    assert list(_vias(routes)), "crossing a front-only wall needs vias"
    for seg in _segments(routes, Layer.F_CU):
        assert not _crosses_band(seg, WALL_X0, WALL_X1, seg.width / 2), seg
    assert any(_crosses_band(s, WALL_X0, WALL_X1, 0.1) for s in _segments(routes, Layer.B_CU))


@pytest.mark.parametrize("force_python", BACKENDS)
def test_via_only_area_blocks_vias_not_tracks(tmp_path: Path, force_python: bool) -> None:
    # Front-only track wall forces two vias; a via-only area covers the
    # 5 mm left of the wall on both layers, so the left via must land west of
    # it -- while B.Cu copper is free to run through the via-only area.
    via_band = (108.0, 113.0)
    extra = _wall(layers='"F.Cu"', vias="allowed") + _keepout(
        "no-vias",
        [
            (via_band[0], BY0 - 1),
            (via_band[1], BY0 - 1),
            (via_band[1], BY1 + 1),
            (via_band[0], BY1 + 1),
        ],
        tracks="allowed",
        uid=2,
    )
    router = _load(tmp_path, _board(extra), force_python)
    routes = _route(router)
    vias = list(_vias(routes))
    assert vias, "net must still route (vias west of the via-only band)"
    for via in vias:
        r = via.diameter / 2
        assert not (via_band[0] - r < via.x < via_band[1] + r), f"via inside via-only area: {via}"
    assert any(_crosses_band(s, via_band[0], via_band[1], 0.1) for s in _segments(routes)), (
        "tracks must still be allowed through a via-only area"
    )


@pytest.mark.parametrize("force_python", BACKENDS)
def test_spatial_keepouts_filters_match_lattice_semantics(
    tmp_path: Path, force_python: bool
) -> None:
    from kicad_tools.router.rules import NetClassRouting

    a_limit, b_limit = 105.8, 110.2
    guards = _keepout(
        "hv-a-corridor-guard", [(BX0, a_limit), (BX1, a_limit), (BX1, BY1), (BX0, BY1)], uid=1
    ) + _keepout(
        "hv-b-corridor-guard", [(BX0, BY0), (BX1, BY0), (BX1, b_limit), (BX0, b_limit)], uid=2
    )
    router = _load(tmp_path, _board(guards, two_nets=True), force_python)
    router.net_class_map["/HV_A"] = NetClassRouting(name="HV_A")
    router.net_class_map["/HV_B"] = NetClassRouting(name="HV_B")
    router._spatial_keepout_filters = {
        "hv-a-corridor-guard": {"only_classes": ["HV_A"]},
        "hv-b-corridor-guard": {"only_classes": ["HV_B"]},
    }
    routes = _route(router)
    nets = {r.net for r in routes if r.segments}
    assert nets == {1, 2}, f"both banks must route, got {nets}"
    areas = router.grid._rule_area_keepouts
    assert all(a.filtered and not a.static_tracks for a in areas)
    for seg in _segments(routes):
        if seg.net == 1:
            assert max(seg.y1, seg.y2) + seg.width / 2 <= a_limit + 1e-6, seg
        elif seg.net == 2:
            assert min(seg.y1, seg.y2) - seg.width / 2 >= b_limit - 1e-6, seg


@pytest.mark.parametrize("force_python", BACKENDS)
@pytest.mark.parametrize(
    ("spatial_filter", "may_cross"),
    [
        ({"only_classes": ["SIG"]}, False),
        ({"only_classes": ["OTHER"]}, True),
        ({"except_classes": ["SIG"]}, True),
        ({"except_classes": ["OTHER"]}, False),
    ],
    ids=["only-own", "only-other", "except-own", "except-other"],
)
def test_filtered_wall_applies_only_to_its_classes(
    tmp_path: Path, force_python: bool, spatial_filter: dict, may_cross: bool
) -> None:
    """A net-filtered wall (net-aware mask, not static cells) blocks exactly
    the nets the ``spatial_keepouts`` filter says it governs."""
    from kicad_tools.router.rules import NetClassRouting

    router = _load(tmp_path, _board(_wall()), force_python)
    router.net_class_map["/SIG"] = NetClassRouting(name="SIG")
    router.net_class_map["/UNUSED"] = NetClassRouting(name="OTHER")
    router._spatial_keepout_filters = {"wall": spatial_filter}
    routes = _route(router)
    (area,) = router.grid._rule_area_keepouts
    # ``except_classes: [OTHER]`` exempts no real net, so it degenerates to
    # the all-nets rule (static cells); the other three are net-aware masks.
    assert area.applies_to(1) is not may_cross
    crossed = any(_crosses_band(s, WALL_X0, WALL_X1, s.width / 2) for s in _segments(routes))
    assert crossed is may_cross


def test_trial_reset_and_worker_payload_keep_rule_areas(tmp_path: Path) -> None:
    """A Monte Carlo trial reset rebuilds the grid, and a parallel worker has
    no source board -- neither may silently drop the rule areas."""
    router = _load(tmp_path, _board(_wall()), force_python=True)
    router._install_grid_rule_area_keepouts()
    payload = router._serialize_for_parallel()["rule_area_keepouts"]
    assert [a.name for a in payload] == ["wall"]

    router._reset_for_new_trial()
    (area,) = router.grid._rule_area_keepouts
    gx, gy = router.grid.world_to_grid(115.0, 108.0)
    assert area.static_tracks and router.grid._blocked[0, gy, gx]

    # A worker router built without a source path installs the parent's list.
    worker = _load(tmp_path, _board(), force_python=True)
    worker._pairwise_attach_zone_pcb_path = None
    worker._rule_area_keepouts_payload = list(payload)
    assert worker._install_grid_rule_area_keepouts() == 1
    assert worker.grid._blocked[0, gy, gx]


# ---------------------------------------------------------------------------
# Unrouted-cause classifier (#5944) names the rule area
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force_python", BACKENDS)
def test_blocked_classification_names_rule_area(tmp_path: Path, force_python: bool) -> None:
    from kicad_tools.router.unrouted_cause import CAUSE_BLOCKED, diagnose_unrouted

    # A closed keepout cage around R1's pad (four bars -- a rule-area polygon
    # here carries no holes): no path leaves it on any layer.
    hole = [(103.2, 107.2), (104.8, 107.2), (104.8, 108.8), (103.2, 108.8)]
    bars = "".join(
        _keepout(f"cage-{i}", poly, uid=10 + i)
        for i, poly in enumerate(
            [
                [(102.0, 106.0), (106.0, 106.0), (106.0, hole[0][1]), (102.0, hole[0][1])],
                [(102.0, hole[2][1]), (106.0, hole[2][1]), (106.0, 110.0), (102.0, 110.0)],
                [(102.0, 106.0), (hole[0][0], 106.0), (hole[0][0], 110.0), (102.0, 110.0)],
                [(hole[1][0], 106.0), (106.0, 106.0), (106.0, 110.0), (hole[1][0], 110.0)],
            ]
        )
    )
    router = _load(tmp_path, _board(bars), force_python)
    routes = _route(router)
    assert not any(r.segments for r in routes), "caged pad must not route"
    diagnosis = diagnose_unrouted(router, [1], net_names={1: "/SIG"})
    assert diagnosis is not None
    conn = diagnosis.connections[0]
    assert conn.cause == CAUSE_BLOCKED, conn.to_dict()
    names = {b.get("name") for b in conn.blockers if b.get("kind") == "keepout"}
    assert names & {"cage-0", "cage-1", "cage-2", "cage-3"}, conn.to_dict()
    assert all(b.get("source") == "rule_area" for b in conn.blockers if b.get("name"))


# ---------------------------------------------------------------------------
# CLI: the #4605 warning is gone for the grid engine
# ---------------------------------------------------------------------------


def test_grid_engine_no_longer_warns(tmp_path: Path, capsys) -> None:
    from kicad_tools.cli.route_cmd import _warn_rule_areas_other_engine

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_board(_wall()))
    _warn_rule_areas_other_engine(pcb, "grid")
    assert "does not honor" not in capsys.readouterr().err
    _warn_rule_areas_other_engine(pcb, "mesh")
    err = capsys.readouterr().err
    assert "does not honor" in err
    assert "grid and lattice" in err


def test_rule_area_grid_disc_matches_euclidean_kernel() -> None:
    import numpy as np

    from kicad_tools.router.rule_area_grid import GridRuleArea

    mask = np.zeros((1, 1), dtype=bool)
    mask[0, 0] = True
    area = GridRuleArea(
        name="",
        polygon=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
        layers=frozenset({0}),
        blocks_tracks=True,
        blocks_vias=False,
        only=None,
        exempt=frozenset(),
        gx0=10,
        gy0=10,
        mask=mask,
    )
    assert area.hits_disc(13, 14, 5)  # 3-4-5: on the disc boundary
    assert not area.hits_disc(14, 14, 5)  # sqrt(32) > 5 even though |d|_inf = 4
    assert math.isclose(area.bbox[2], 1.0)
