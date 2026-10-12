"""Regression tests for Issue #6375: the C++ A* and the post-route validator
must agree on the pad geometry next to the routing net's OWN pad.

After #6370 (#6315) board 04 spent ~134 s of a 143 s route in pure-Python A*
fallbacks, because the C++ search kept returning candidates that
``validate_route`` rejected for ``seg-pad`` gaps of 0.100 mm (BOOT0 vs U2.43)
against a 0.15 mm rule.  Instrumenting every rejected candidate on boards 04
and 03 classified all of them the same way -- not the reconstruct_path tails
(the start tail cleared at 0.20 mm), and not a step the #6315 exact pad bypass
approved:

* The offending steps all sat on cells of the routing net's OWN pad copper or
  clearance halo (``cell.net == net``, ``pad_cell``), which both A* loops admit
  with no clearance check at all (the #5599 strict kernel is evidence-gated).
* The neighbour that the step crowded is a net-0 pad (NC, or a pour net the
  router maps to net 0), so the cell is never marked as a shared halo.
* Before #6370 the net-0 neighbour's clearance cells further along the escape
  blocked the corridor, so the unchecked own-pad steps never led anywhere.
  #6370 resolved those cells exactly, the corridor opened, and the unchecked
  steps reached the validator.

The fix holds every planar step touching the routing net's own static pad
cells to the validator's own seg-vs-pad verdict (``Grid3D::segment_pad_clear``
is now the single implementation behind both ``validate_route`` and the A*
gate), with the same carve-outs and the narrowest emittable width.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.layers import Layer
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available; run `kct build-native`",
)

# Board 04 U2 (LQFP-48): 0.3 x 1.475 mm roundrect pads at 0.5 mm pitch, 0.2 mm
# traces, 0.15 mm clearance, 0.05 mm grid; the pad row sits 0.0125 mm off grid.
PITCH = 0.5
PAD_SHORT = 0.3
PAD_LONG = 1.475
TRACE = 0.2
CLEARANCE = 0.15
RESOLUTION = 0.05
ROW = 5.0
ROW_OFFSET = 0.0125


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=TRACE,
        trace_clearance=CLEARANCE,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=CLEARANCE,
        grid_resolution=RESOLUTION,
    )


def _u2_pads(rotated: bool) -> list[dict]:
    """NC, SIG, NC, NC -- the signal pin's right neighbour is NC (net 0).

    ``rotated`` turns the row 90 degrees: horizontal pads stacked in Y.
    """
    out = []
    for i, (offset, net, name) in enumerate(
        [(-PITCH, 0, ""), (0.0, 1, "SIG"), (PITCH, 0, ""), (2 * PITCH, 0, "")]
    ):
        if rotated:
            x, y, w, h = ROW + ROW_OFFSET, ROW + offset, PAD_LONG, PAD_SHORT
        else:
            x, y, w, h = ROW + offset, ROW + ROW_OFFSET, PAD_SHORT, PAD_LONG
        out.append(
            {
                "number": str(i + 1),
                "x": x,
                "y": y,
                "width": w,
                "height": h,
                "net": net,
                "net_name": name,
                "layer": Layer.F_CU,
            }
        )
    return out


def _autorouter(rotated: bool, target: tuple[float, float]):
    from kicad_tools.router.core import Autorouter

    ar = Autorouter(width=12.0, height=12.0, rules=_rules())
    ar.add_component("U2", _u2_pads(rotated))
    ar.add_component(
        "J2",
        [
            {
                "number": "1",
                "x": target[0],
                "y": target[1],
                "width": 1.0,
                "height": 1.0,
                "net": 1,
                "net_name": "SIG",
                "layer": Layer.F_CU,
            }
        ],
    )
    if ar.backend_info.get("active") != "cpp":
        pytest.skip("Autorouter did not select the C++ backend")
    return ar


def _pad_gap(pad, seg) -> float:
    """Edge-to-edge gap between a segment and an axis-aligned rectangular pad."""
    hw, hh = pad.width / 2, pad.height / 2
    best = math.inf
    for i in range(401):
        t = i / 400
        px = seg.x1 + (seg.x2 - seg.x1) * t
        py = seg.y1 + (seg.y2 - seg.y1) * t
        best = min(best, math.hypot(max(abs(px - pad.x) - hw, 0.0), max(abs(py - pad.y) - hh, 0.0)))
    return best - seg.width / 2


# (rotated, target): straight past the NC neighbour's tip, the mirror image,
# the 90-degree-rotated row, and a steep diagonal approach.  On the pre-fix
# build each of the first four took 3 seg-pad rejections against the NC pad.
CASES = [
    pytest.param(False, (7.5, 2.5), id="vertical-up-right"),
    pytest.param(False, (7.5, 7.5), id="vertical-down-right"),
    pytest.param(True, (7.5, 7.5), id="rotated-down-right"),
    pytest.param(True, (2.5, 7.5), id="rotated-down-left"),
    pytest.param(False, (6.0, 2.0), id="vertical-steep-diagonal"),
]


@requires_cpp
@pytest.mark.parametrize(("rotated", "target"), CASES)
def test_candidate_passes_validation_on_the_first_attempt(rotated, target) -> None:
    """AC: the native candidate validates on the FIRST attempt (no resume)."""
    ar = _autorouter(rotated, target)
    assert ar.route_net(1), "SIG did not route"
    stats = ar.backend_info.get("fallback_stats", {})
    diagnostics = stats.get("resume_diagnostics") or {}
    assert "SIG" not in diagnostics, (
        f"the C++ candidate was rejected by post-route validation: {diagnostics.get('SIG')}"
    )
    assert not stats.get("fallback_count"), "fell back to the Python A*"

    pads = [p for p in ar.pads.values() if p.net != 1]
    for route in ar.routes:
        for seg in route.segments:
            for pad in pads:
                gap = _pad_gap(pad, seg)
                assert gap >= CLEARANCE - 1e-3, (
                    f"segment ({seg.x1:.4f},{seg.y1:.4f})-({seg.x2:.4f},{seg.y2:.4f}) is "
                    f"{gap:.4f} mm from {pad.ref}.{pad.pin}"
                )


def _attached_pathfinder(rotated: bool):
    """A ``CppPathfinder`` over a grid holding the U2 row (pads only)."""
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.primitives import Pad

    rules = _rules()
    grid = RoutingGrid(width=12.0, height=12.0, rules=rules, layer_stack=LayerStack.two_layer())
    cpp_grid = CppGrid.from_routing_grid(grid)
    pads = []
    for spec in _u2_pads(rotated):
        pad = Pad(
            x=spec["x"],
            y=spec["y"],
            width=spec["width"],
            height=spec["height"],
            net=spec["net"],
            net_name=spec["net_name"],
            layer=Layer.F_CU,
            ref="U2",
            pin=spec["number"],
        )
        grid.add_pad(pad, pin_pitch=PITCH)
        pads.append(pad)
    pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    return grid, cpp_grid, pathfinder, pads


def _validator_verdict(cpp_grid, ax, ay, bx, by, net=1, exclude=(), clamp=()) -> bool:
    from kicad_tools.router.cpp_backend import router_cpp

    seg = router_cpp.Segment()
    seg.x1, seg.y1, seg.x2, seg.y2 = ax, ay, bx, by
    seg.width, seg.layer, seg.net = TRACE, 0, net
    result = cpp_grid._impl.validate_route(
        [seg],
        [],
        net,
        list(exclude),
        CLEARANCE,
        CLEARANCE,
        CLEARANCE,
        -1,
        0.0,
        list(clamp),
        CLEARANCE,
    )
    return bool(result.valid)


def _steps_near_the_signal_tip(grid, rotated: bool):
    """Every planar grid step within ~0.6 mm of the signal pad's tip toward
    the NC neighbour -- including the board 04 BOOT0 diagonal and column."""
    sig_x, sig_y = (ROW + ROW_OFFSET, ROW) if rotated else (ROW, ROW + ROW_OFFSET)
    res = grid.resolution
    gx0 = round((sig_x - grid.origin_x) / res)
    gy0 = round((sig_y - grid.origin_y) / res)
    reach = round(0.6 / res) + round(PAD_LONG / 2 / res)
    steps = []
    for dgy in range(-reach, reach + 1):
        for dgx in range(-reach, reach + 1):
            gx, gy = gx0 + dgx, gy0 + dgy
            for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
                steps.append((gx, gy, gx + dx, gy + dy))
    return steps


@requires_cpp
@pytest.mark.parametrize("rotated", [False, True], ids=["vertical", "rotated"])
def test_own_pad_step_gate_matches_validate_route(rotated: bool) -> None:
    """AC: for every step at the boundary, the search-side verdict equals
    ``validate_route``'s verdict on the same segment."""
    grid, cpp_grid, pathfinder, _ = _attached_pathfinder(rotated)
    impl = pathfinder._impl
    impl.set_search_own_pad_gate([], [], TRACE)
    disagreements = []
    rejected = 0
    for gx1, gy1, gx2, gy2 in _steps_near_the_signal_tip(grid, rotated):
        ax, ay = cpp_grid._impl.grid_to_world(gx1, gy1)
        bx, by = cpp_grid._impl.grid_to_world(gx2, gy2)
        search = bool(impl.own_pad_step_clear(gx1, gy1, gx2, gy2, 0, 1, TRACE))
        validator = _validator_verdict(cpp_grid, ax, ay, bx, by)
        rejected += not validator
        if search != validator:
            disagreements.append(((ax, ay, bx, by), search, validator))
    assert rejected, "the window must contain steps the validator rejects"
    assert not disagreements, disagreements[:5]


@requires_cpp
def test_board04_boot0_escape_steps_are_refused() -> None:
    """The exact board 04 rejected BOOT0 steps, re-based onto the test row:
    a diagonal off the pad tip toward the NC neighbour, then the column at
    x = pad + 0.2 mm.  Both the gate and the validator refuse each one."""
    grid, cpp_grid, pathfinder, _ = _attached_pathfinder(False)
    impl = pathfinder._impl
    impl.set_search_own_pad_gate([], [], TRACE)
    # board 04: (148.85,84.70)->(148.90,84.65)->(148.95,84.60)->(148.95,84.55)
    # with U2.44 at (148.75, 85.3375); here U2.SIG is at (5.0, 5.0125).
    dx, dy = ROW - 148.75, (ROW + ROW_OFFSET) - 85.3375
    for x1, y1, x2, y2 in [
        (148.85, 84.70, 148.90, 84.65),
        (148.90, 84.65, 148.95, 84.60),
        (148.95, 84.60, 148.95, 84.55),
        (148.95, 84.55, 148.95, 84.50),
    ]:
        ax, ay, bx, by = x1 + dx, y1 + dy, x2 + dx, y2 + dy
        g1 = cpp_grid._impl.world_to_grid(ax, ay)
        g2 = cpp_grid._impl.world_to_grid(bx, by)
        assert not _validator_verdict(cpp_grid, ax, ay, bx, by)
        assert not impl.own_pad_step_clear(g1[0], g1[1], g2[0], g2[1], 0, 1, TRACE)


@requires_cpp
def test_gate_honours_the_validators_same_component_carveout() -> None:
    """The gate is never stricter than the validator: with U2 in the
    validator's ``exclude_ref_hashes`` (#1764 / #3490 carve-out, which skips a
    same-component NC pad outright) the same step is clear on both sides."""
    from kicad_tools.router.cpp_backend import router_cpp

    grid, cpp_grid, pathfinder, _ = _attached_pathfinder(False)
    impl = pathfinder._impl
    u2 = router_cpp.fnv1a_hash("U2")
    ax, ay, bx, by = 5.15, 4.30, 5.20, 4.25  # crowds the NC neighbour's tip
    g1 = cpp_grid._impl.world_to_grid(ax, ay)
    g2 = cpp_grid._impl.world_to_grid(bx, by)
    impl.set_search_own_pad_gate([], [], TRACE)
    assert not _validator_verdict(cpp_grid, ax, ay, bx, by)
    assert not impl.own_pad_step_clear(g1[0], g1[1], g2[0], g2[1], 0, 1, TRACE)
    impl.set_search_own_pad_gate([u2], [], TRACE)
    assert _validator_verdict(cpp_grid, ax, ay, bx, by, exclude=[u2])
    assert impl.own_pad_step_clear(g1[0], g1[1], g2[0], g2[1], 0, 1, TRACE)


@requires_cpp
def test_gate_uses_the_narrowest_emitted_width() -> None:
    """A narrower gate width (the neck-down floor) can only relax the gate."""
    grid, cpp_grid, pathfinder, _ = _attached_pathfinder(False)
    impl = pathfinder._impl
    # Column 0.15 mm right of the signal pad centre, beside the NC pad (whose
    # metal starts at x = 5.35): a 0.2 mm trace leaves a 0.10 mm gap, a
    # 0.05 mm trace 0.175 mm.
    ax, ay, bx, by = 5.15, 4.75, 5.15, 4.70
    g1 = cpp_grid._impl.world_to_grid(ax, ay)
    g2 = cpp_grid._impl.world_to_grid(bx, by)
    impl.set_search_own_pad_gate([], [], TRACE)
    assert not impl.own_pad_step_clear(g1[0], g1[1], g2[0], g2[1], 0, 1, TRACE)
    impl.set_search_own_pad_gate([], [], 0.05)
    assert impl.own_pad_step_clear(g1[0], g1[1], g2[0], g2[1], 0, 1, TRACE)
