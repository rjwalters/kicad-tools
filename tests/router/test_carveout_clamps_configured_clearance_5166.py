"""Issue #5166 -- the same-component carve-out must CLAMP, not skip entirely.

Background
----------

The NET-AWARE same-component clearance carve-out becomes eligible for a
component ref for exactly two structurally different reasons (see
``RoutingGrid._same_component_carveout_mode`` /
``CppPathfinder._same_component_carveout_mode``):

1.  ``_relax_same_component_clearance`` (Issue #2452) physically unblocked
    the overlap corridor between two same-component pads down to a
    ``trace_width / 2`` floor.  Nothing shrank any *clearance value* --
    ``pad.clearance_override`` is still the FULL default -- so the
    pad-clearance check genuinely has to be skipped for the corridor route
    to survive geometric acceptance.

2.  A CONFIGURED override resolved smaller than the default
    ``trace_clearance``: an explicit ``component_clearances`` entry, an
    applied ``fine_pitch_clearance`` shrink, or a net-class
    ``escape_clearance`` (Issue #1764).

Before this fix both reasons produced the same unconditional ``continue``
in ``Grid3D::validate_route`` (``cpp/src/grid.cpp``) and in the three
Python sibling call sites (``grid.py``: ``worst_segment_pad_deficit``,
``worst_via_pad_deficit``, ``validate_segment_clearance``).  For reason 2
that is a real DRC hazard: an authored 0.10mm relaxation silently accepted
*any* positive gap, so a route could pass with 0.02mm of actual copper
clearance.  #5004 / PR #5165 removed the unconfigured fine-pitch refs from
the exclusion set entirely; this module covers the narrower remaining case
where the ref legitimately reaches the carve-out.

What is asserted
----------------

The fixture is deliberately arithmetic-only so every expected value is
hand-checkable:

  * ``U1`` pad: 1.0 x 1.0mm rect at (5, 5), net 2 (``FOREIGN``).
  * Default ``trace_clearance`` 0.2mm, trace width 0.2mm (half-width 0.1).
  * A horizontal net-1 segment spanning the pad at ``y = 5 + dy`` has
    ``clearance = dy - 0.5 (pad half-height) - 0.1 (trace half-width)``.
    ``dy = 0.65`` -> 0.05mm gap;  ``dy = 0.75`` -> 0.15mm gap.
  * A net-1 via (0.2mm diameter, radius 0.1) at the same offsets gives the
    same two gaps.

Against ``component_clearances={"U1": 0.10}``:

  * 0.05mm actual gap -> REJECTED (the configured floor is enforced).  This
    is the bug: pre-#5166 it was accepted.
  * 0.15mm actual gap -> ACCEPTED.  The override is respected *at its
    smaller value* -- the fix must not degenerate into "drop the carve-out",
    which would raise the floor back to the 0.2mm default and reject this.

Against ``_relaxed_clearance_refs = {"U1"}`` (the #2452 corridor relief,
no override configured):

  * 0.05mm actual gap -> still ACCEPTED, byte-identical to pre-#5166.

Every case is asserted on BOTH validators: the pure-Python backstops in
``grid.py`` and the compiled ``Grid3D::validate_route``, plus the
``CppPathfinder._validate_route_clearance`` wrapper that owns the
``exclude_ref_hashes`` / ``clamp_ref_hashes`` split.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available, router_cpp
from kicad_tools.router.grid import RoutingGrid, _sync_pad_via_policies
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

native = pytest.mark.skipif(
    not is_cpp_available(), reason="native backend unavailable; run kct build-native"
)

# Geometry constants -- see the module docstring for the arithmetic.
PAD_X = 5.0
PAD_Y = 5.0
PAD_SIZE = 1.0
TRACE_WIDTH = 0.2
DEFAULT_CLEARANCE = 0.2
CONFIGURED_OVERRIDE = 0.10

#: ``dy`` offsets producing an exact edge-to-edge gap of 0.05 / 0.15mm.
DY_UNDERCUT = 0.65  # gap 0.05mm -- BELOW the configured 0.10mm floor
DY_CLEARS = 0.75  # gap 0.15mm -- above the configured floor, below the default

Mode = str


def _make_rules() -> DesignRules:
    return DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=DEFAULT_CLEARANCE,
        via_drill=0.1,
        via_diameter=TRACE_WIDTH,
        via_clearance=DEFAULT_CLEARANCE,
        grid_resolution=0.1,
    )


def _make_grid(mode: Mode) -> tuple[RoutingGrid, Pad]:
    """Build a one-foreign-pad grid whose carve-out mode is ``mode``.

    ``mode`` selects how ``U1`` becomes carve-out eligible:

      * ``"clamp"``  -- ``component_clearances={"U1": 0.10}`` (a configured
        override that resolves smaller than ``trace_clearance``).
      * ``"skip"``   -- ``U1`` is in ``_relaxed_clearance_refs`` (#2452
        corridor relief) with no override configured.
      * ``"none"``   -- neither; the pad keeps its full default clearance.
    """
    rules = _make_rules()
    if mode == "clamp":
        rules.component_clearances["U1"] = CONFIGURED_OVERRIDE
    grid = RoutingGrid(10.0, 10.0, rules=rules, layer_stack=LayerStack.two_layer())
    pad = Pad(
        x=PAD_X,
        y=PAD_Y,
        width=PAD_SIZE,
        height=PAD_SIZE,
        net=2,
        net_name="FOREIGN",
        ref="U1",
        pin="2",
    )
    grid.add_pad(pad)
    if mode == "skip":
        grid._relaxed_clearance_refs.add("U1")
    return grid, pad


def _segment(dy: float) -> Segment:
    """Net-1 segment spanning the pad horizontally at ``PAD_Y + dy``."""
    y = PAD_Y + dy
    return Segment(PAD_X - 0.5, y, PAD_X + 0.5, y, TRACE_WIDTH, Layer.F_CU, 1)


def _via(dy: float) -> Via:
    return Via(PAD_X, PAD_Y + dy, 0.1, TRACE_WIDTH, (Layer.F_CU, Layer.B_CU), 1)


def _cpp_segment(dy: float) -> object:
    seg = router_cpp.Segment()
    seg.x1, seg.y1 = PAD_X - 0.5, PAD_Y + dy
    seg.x2, seg.y2 = PAD_X + 0.5, PAD_Y + dy
    seg.width, seg.layer, seg.net = TRACE_WIDTH, 0, 1
    return seg


def _cpp_via(dy: float) -> object:
    via = router_cpp.Via()
    via.x, via.y = PAD_X, PAD_Y + dy
    via.drill, via.diameter = 0.1, TRACE_WIDTH
    via.layer_from, via.layer_to, via.net = 0, 1, 1
    return via


def _cpp_validate(
    grid: RoutingGrid,
    *,
    segments: list[object],
    vias: list[object],
    skip_refs: list[str],
    clamp_refs: list[str],
) -> object:
    cpp = CppGrid.from_routing_grid(grid)
    _sync_pad_via_policies(grid, cpp)
    return cpp._impl.validate_route(
        segments,
        vias,
        1,  # exclude_net -- the route's own net
        [router_cpp.fnv1a_hash(r) for r in skip_refs],
        grid.rules.trace_clearance,
        grid.rules.via_clearance,
        grid.rules.min_drill_clearance,
        -1,
        0.0,
        [router_cpp.fnv1a_hash(r) for r in clamp_refs],
    )


# ---------------------------------------------------------------------------
# Layer 0: the geometry the rest of the module relies on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dy,expected_gap", [(DY_UNDERCUT, 0.05), (DY_CLEARS, 0.15)])
def test_fixture_gap_arithmetic(dy: float, expected_gap: float) -> None:
    """The fixture really produces the 0.05 / 0.15mm gaps the asserts assume."""
    grid, _ = _make_grid("none")
    # With no carve-out and a required clearance of 0.2, the deficit is
    # exactly ``0.2 - gap`` -- which pins down ``gap`` itself.
    seg_deficit, _ = grid.worst_segment_pad_deficit(_segment(dy), exclude_net=1)
    via_deficit, _ = grid.worst_via_pad_deficit(_via(dy), exclude_net=1)
    assert seg_deficit == pytest.approx(DEFAULT_CLEARANCE - expected_gap)
    assert via_deficit == pytest.approx(DEFAULT_CLEARANCE - expected_gap)


# ---------------------------------------------------------------------------
# Layer 1: mode classification (the C++/Python boundary's shared decision)
# ---------------------------------------------------------------------------


def test_python_mode_classification() -> None:
    """``RoutingGrid._same_component_carveout_mode`` grades the two reasons."""
    grid, _ = _make_grid("clamp")
    assert (
        grid._same_component_carveout_mode("U1", CONFIGURED_OVERRIDE, DEFAULT_CLEARANCE) == "clamp"
    )

    grid, _ = _make_grid("skip")
    # Corridor relief: the resolved clearance is still the full default.
    assert grid.rules.get_clearance_for_component("U1") == DEFAULT_CLEARANCE
    assert grid._same_component_carveout_mode("U1", DEFAULT_CLEARANCE, DEFAULT_CLEARANCE) == "skip"

    grid, _ = _make_grid("none")
    assert grid._same_component_carveout_mode("U1", DEFAULT_CLEARANCE, DEFAULT_CLEARANCE) == "none"


def test_corridor_relief_wins_over_a_configured_override() -> None:
    """A ref eligible for BOTH reasons keeps the weaker (full-skip) floor.

    The #2452 corridor was physically unblocked to ``trace_width / 2``;
    clamping it to the configured override would reject the very routes
    that relaxation exists to permit, so corridor relief takes precedence.
    """
    grid, _ = _make_grid("clamp")
    grid._relaxed_clearance_refs.add("U1")
    assert (
        grid._same_component_carveout_mode("U1", CONFIGURED_OVERRIDE, DEFAULT_CLEARANCE) == "skip"
    )


def test_strict_pad_clearance_disables_both_flavours() -> None:
    grid, _ = _make_grid("clamp")
    grid.rules.strict_pad_clearance = True
    assert (
        grid._same_component_carveout_mode("U1", CONFIGURED_OVERRIDE, DEFAULT_CLEARANCE) == "none"
    )


def test_active_predicate_is_the_union_of_both_flavours() -> None:
    """``_same_component_carveout_active`` keeps its pre-#5166 contract."""
    for mode, active in (("clamp", True), ("skip", True), ("none", False)):
        grid, _ = _make_grid(mode)
        required = grid.rules.get_clearance_for_component("U1")
        assert grid._same_component_carveout_active("U1", required, DEFAULT_CLEARANCE) is active, (
            mode
        )


@native
def test_cpp_backend_mode_classification_mirrors_python() -> None:
    """``CppPathfinder._same_component_carveout_mode`` agrees with the grid."""
    for mode in ("clamp", "skip", "none"):
        grid, _ = _make_grid(mode)
        pf = CppPathfinder(CppGrid.from_routing_grid(grid), grid.rules)
        assert pf._same_component_carveout_mode(grid, "U1") == mode
        # And the legacy boolean wrapper still answers the union question.
        assert pf._same_component_carveout_eligible(grid, "U1") is (mode != "none")


# ---------------------------------------------------------------------------
# Layer 2: THE BUG -- a configured override must be a floor, not a free pass
# ---------------------------------------------------------------------------


def test_python_segment_backstop_enforces_configured_floor() -> None:
    """0.05mm against an authored 0.10mm floor is a violation (#5166)."""
    grid, pad = _make_grid("clamp")
    deficit, loc = grid.worst_segment_pad_deficit(
        _segment(DY_UNDERCUT), exclude_net=1, exclude_refs={"U1"}
    )
    assert deficit == pytest.approx(CONFIGURED_OVERRIDE - 0.05)
    assert loc == (pad.x, pad.y)


def test_python_segment_backstop_respects_the_smaller_value() -> None:
    """0.15mm passes: the override is honoured AT 0.10mm, not raised to 0.2."""
    grid, _ = _make_grid("clamp")
    deficit, loc = grid.worst_segment_pad_deficit(
        _segment(DY_CLEARS), exclude_net=1, exclude_refs={"U1"}
    )
    assert deficit == 0.0
    assert loc is None


def test_python_via_backstop_enforces_configured_floor() -> None:
    grid, pad = _make_grid("clamp")
    deficit, loc = grid.worst_via_pad_deficit(_via(DY_UNDERCUT), exclude_net=1, exclude_refs={"U1"})
    assert deficit == pytest.approx(CONFIGURED_OVERRIDE - 0.05)
    assert loc == (pad.x, pad.y)


def test_python_via_backstop_respects_the_smaller_value() -> None:
    grid, _ = _make_grid("clamp")
    deficit, loc = grid.worst_via_pad_deficit(_via(DY_CLEARS), exclude_net=1, exclude_refs={"U1"})
    assert deficit == 0.0
    assert loc is None


def test_python_validate_segment_clearance_enforces_configured_floor() -> None:
    """The third Python call site (``validate_segment_clearance``) too."""
    grid, pad = _make_grid("clamp")
    valid, _, loc = grid.validate_segment_clearance(
        _segment(DY_UNDERCUT), exclude_net=1, exclude_refs={"U1"}
    )
    assert valid is False
    assert loc == (pad.x, pad.y)

    valid, _, loc = grid.validate_segment_clearance(
        _segment(DY_CLEARS), exclude_net=1, exclude_refs={"U1"}
    )
    assert valid is True
    assert loc is None


@native
def test_cpp_segment_validator_enforces_configured_floor() -> None:
    """``Grid3D::validate_route``'s segment-vs-pad branch clamps (#5166)."""
    grid, pad = _make_grid("clamp")
    result = _cpp_validate(
        grid,
        segments=[_cpp_segment(DY_UNDERCUT)],
        vias=[],
        skip_refs=[],
        clamp_refs=["U1"],
    )
    assert result.valid is False
    assert result.violation_type == 1  # seg-pad
    assert (result.violation_x, result.violation_y) == (pad.x, pad.y)


@native
def test_cpp_segment_validator_respects_the_smaller_value() -> None:
    grid, _ = _make_grid("clamp")
    result = _cpp_validate(
        grid,
        segments=[_cpp_segment(DY_CLEARS)],
        vias=[],
        skip_refs=[],
        clamp_refs=["U1"],
    )
    assert result.valid is True


@native
def test_cpp_via_validator_enforces_configured_floor() -> None:
    """``Grid3D::validate_route``'s via-vs-pad branch clamps (#5166)."""
    grid, pad = _make_grid("clamp")
    result = _cpp_validate(
        grid,
        segments=[],
        vias=[_cpp_via(DY_UNDERCUT)],
        skip_refs=[],
        clamp_refs=["U1"],
    )
    assert result.valid is False
    assert result.violation_type == 8  # via-pad
    assert (result.violation_x, result.violation_y) == (pad.x, pad.y)


@native
def test_cpp_via_validator_respects_the_smaller_value() -> None:
    grid, _ = _make_grid("clamp")
    result = _cpp_validate(
        grid,
        segments=[],
        vias=[_cpp_via(DY_CLEARS)],
        skip_refs=[],
        clamp_refs=["U1"],
    )
    assert result.valid is True


@native
def test_cpp_empty_clamp_set_reproduces_pre_5166_full_skip() -> None:
    """The new parameter is opt-in: the old call shape keeps the old answer.

    Passing ``U1`` in ``exclude_ref_hashes`` with an EMPTY clamp set is
    exactly what every pre-#5166 caller did, and still accepts the 0.05mm
    undercut.  This pins the defaults-preserve contract that protects the
    #2452 corridor-relief path.
    """
    grid, _ = _make_grid("clamp")
    for segments, vias in (([_cpp_segment(DY_UNDERCUT)], []), ([], [_cpp_via(DY_UNDERCUT)])):
        result = _cpp_validate(grid, segments=segments, vias=vias, skip_refs=["U1"], clamp_refs=[])
        assert result.valid is True


# ---------------------------------------------------------------------------
# Layer 3: the #2452 corridor-relief case must be UNTOUCHED
# ---------------------------------------------------------------------------


def test_python_corridor_relief_still_skips_segment_check() -> None:
    """#2452 relief keeps its ``trace_width / 2`` floor, not the override."""
    grid, _ = _make_grid("skip")
    deficit, loc = grid.worst_segment_pad_deficit(
        _segment(DY_UNDERCUT), exclude_net=1, exclude_refs={"U1"}
    )
    assert deficit == 0.0
    assert loc is None

    valid, _, loc = grid.validate_segment_clearance(
        _segment(DY_UNDERCUT), exclude_net=1, exclude_refs={"U1"}
    )
    assert valid is True
    assert loc is None


def test_python_corridor_relief_still_skips_via_check() -> None:
    grid, _ = _make_grid("skip")
    deficit, loc = grid.worst_via_pad_deficit(_via(DY_UNDERCUT), exclude_net=1, exclude_refs={"U1"})
    assert deficit == 0.0
    assert loc is None


@native
def test_cpp_corridor_relief_still_skips_both_quadrants() -> None:
    grid, _ = _make_grid("skip")
    for segments, vias in (([_cpp_segment(DY_UNDERCUT)], []), ([], [_cpp_via(DY_UNDERCUT)])):
        result = _cpp_validate(grid, segments=segments, vias=vias, skip_refs=["U1"], clamp_refs=[])
        assert result.valid is True


# ---------------------------------------------------------------------------
# Layer 4: end-to-end through the wrapper that owns the set split
# ---------------------------------------------------------------------------


def _route(dy: float, *, with_via: bool) -> Route:
    route = Route(1, "SIGNAL")
    if with_via:
        route.vias.append(_via(dy))
    else:
        route.segments.append(_segment(dy))
    return route


def _own_pad() -> Pad:
    """The route's own ``U1`` pad -- the carve-out's ``exclude_refs`` source."""
    return Pad(
        x=PAD_X,
        y=PAD_Y - 2.0,
        width=PAD_SIZE,
        height=PAD_SIZE,
        net=1,
        net_name="SIGNAL",
        ref="U1",
        pin="1",
    )


@native
@pytest.mark.parametrize("with_via", [False, True])
def test_validate_route_clearance_rejects_undercut_configured_override(with_via: bool) -> None:
    """End-to-end: ``_validate_route_clearance`` routes ``U1`` into the clamp set.

    This is the acceptance path the autorouter actually calls, so it proves
    the ``exclude_ref_hashes`` / ``clamp_ref_hashes`` split is wired all the
    way through ``bindings.cpp``.
    """
    grid, pad = _make_grid("clamp")
    own = _own_pad()
    grid.add_pad(own)
    pf = CppPathfinder(CppGrid.from_routing_grid(grid), grid.rules)
    assert pf._validate_route_clearance(_route(DY_UNDERCUT, with_via=with_via), own, own, 1) == (
        pad.x,
        pad.y,
    )
    # The same override still ACCEPTS a route that honours it at 0.10mm.
    assert pf._validate_route_clearance(_route(DY_CLEARS, with_via=with_via), own, own, 1) is None


@native
@pytest.mark.parametrize("with_via", [False, True])
def test_validate_route_clearance_keeps_corridor_relief_passing(with_via: bool) -> None:
    """End-to-end #2452 regression: corridor relief still accepts 0.05mm."""
    grid, _ = _make_grid("skip")
    own = _own_pad()
    grid.add_pad(own)
    pf = CppPathfinder(CppGrid.from_routing_grid(grid), grid.rules)
    assert pf._validate_route_clearance(_route(DY_UNDERCUT, with_via=with_via), own, own, 1) is None
