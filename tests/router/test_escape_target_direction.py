"""Issue #3428: target-aware in-pad inner segment direction for fine-pitch
QFP escapes.

Board 04 (STM32 LQFP-48) background: when U2.6 OSC_OUT claims an in-pad
via rescue, the legacy dispatcher emitted the inner-layer stub in the
parity-derived ``alt_dir_cw`` direction (NORTH = +y = toward U2.7 NRST),
blocking the adjacent pin's escape-via slot.  PR for #3428 makes the stub
direction target-aware:

* ``EscapeRouter._compute_target_direction`` snaps the vector from the
  rescued pad to the net's nearest OFF-package pad onto a cardinal
  ``EscapeDirection`` (deterministic tie-breaks, into-package guard).
* ``EscapeRouter._try_in_pad_escape`` accepts ``target_direction`` which
  redirects ONLY the inner stub (via placement is direction-independent).
  ``target_direction=None`` preserves legacy behaviour byte-for-byte.
* The QFP-alternating dispatcher additionally performs a POCKET-ESCAPE
  rescue: a pin whose clean surface escape points AWAY from its net
  target while an adjacent pin already holds an in-pad via is rescued
  with its own in-pad via + target-aware stub (board 04 NRST).

Test plan (from the curator-sharpened issue):
1. Snap-to-cardinal selection for targets in each quadrant + exact-tie
   cases (deterministic result).
2. ``target_direction=None`` produces an identical ``EscapeRoute`` (stub
   vector, escape_point, width) to pre-change behaviour.
3. Target pointing into the package body falls back to legacy direction
   (returns None) unless ``allow_into_package=True``.
4. Board-04-like fixture: adjacent 0.5mm-pitch pins where pin N claims
   an in-pad via and pin N+1's target lies across the package -- the
   pocket-escape rescue fires and the stub points at the target.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.escape import (
    EscapeDirection,
    EscapeRoute,
    EscapeRouter,
    PackageInfo,
    PackageType,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Via
from kicad_tools.router.rules import DesignRules

# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------

ANCHOR_X = 0.0
ANCHOR_Y = 0.0


def _make_rules(manufacturer: str | None = "jlcpcb-tier1") -> DesignRules:
    # trace_width 0.2: narrow enough that the perpendicular (even-index)
    # surface escapes in the dispatcher fixture stay clearance-clean at
    # 0.5 mm pitch (a 0.5 mm-wide escape would violate against the
    # NEIGHBOUR pads even perpendicular, short-circuiting every pin into
    # the violation-triggered rescue and hiding the pocket trigger).
    # The necking unit test passes escape_width=0.5 explicitly, so it is
    # unaffected by this value.
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.127,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        manufacturer=manufacturer,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=30.0,
        height=30.0,
        rules=rules,
        origin_x=-15.0,
        origin_y=-15.0,
        layer_stack=LayerStack.two_layer(),
    )


def _make_router(
    net_target_positions: dict[int, list[tuple[float, float, str]]] | None = None,
    manufacturer: str | None = "jlcpcb-tier1",
) -> EscapeRouter:
    rules = _make_rules(manufacturer=manufacturer)
    grid = _make_grid(rules)
    return EscapeRouter(grid, rules, net_target_positions=net_target_positions)


def _west_edge_pad(net: int = 7, x: float = ANCHOR_X, y: float = ANCHOR_Y) -> Pad:
    """A fine-pitch west-edge pad (long axis horizontal)."""
    return Pad(
        x=x,
        y=y,
        width=1.5,
        height=0.3,
        net=net,
        net_name=f"NET{net}",
        ref="U2",
        pin="7",
        layer=Layer.F_CU,
    )


def _make_package(pads: list[Pad], ref: str = "U2") -> PackageInfo:
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    return PackageInfo(
        ref=ref,
        package_type=PackageType.QFP,
        center=(sum(xs) / len(xs), sum(ys) / len(ys)),
        pads=pads,
        pin_count=len(pads),
        pin_pitch=0.5,
        bounding_box=(min(xs), min(ys), max(xs), max(ys)),
        is_dense=True,
    )


def _make_lqfp_west_signal_row(
    pads_per_edge: int = 12,
    pitch: float = 0.5,
    pad_short: float = 0.30,
    pad_long: float = 1.50,
) -> list[Pad]:
    """Four-edge LQFP-48-like fixture, all signal nets, unique per pin.

    West-edge pads are emitted bottom-to-top so the dispatcher's
    y-ascending filtered list index equals the emission index j
    (west net id = ``100 + j``).
    """
    span = (pads_per_edge - 1) * pitch
    body_size = span + 3.0 * pitch + 2.0 * pad_long
    half_body = body_size / 2
    pad_stick_out = 0.85
    pad_center_offset = half_body + pad_stick_out / 2
    half_span = span / 2

    pads: list[Pad] = []
    pin_no = 1
    # WEST edge, bottom -> top (ascending y) so filtered index == j.
    for j in range(pads_per_edge):
        pads.append(
            Pad(
                x=-pad_center_offset,
                y=-half_span + j * pitch,
                width=pad_long,
                height=pad_short,
                net=100 + j,
                net_name=f"NET{100 + j}",
                ref="U2",
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1
    # SOUTH edge
    for j in range(pads_per_edge):
        pads.append(
            Pad(
                x=-half_span + j * pitch,
                y=-pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=300 + j,
                net_name=f"NET{300 + j}",
                ref="U2",
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1
    # EAST edge
    for j in range(pads_per_edge):
        pads.append(
            Pad(
                x=pad_center_offset,
                y=-half_span + j * pitch,
                width=pad_long,
                height=pad_short,
                net=400 + j,
                net_name=f"NET{400 + j}",
                ref="U2",
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1
    # NORTH edge
    for j in range(pads_per_edge):
        pads.append(
            Pad(
                x=-half_span + j * pitch,
                y=pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=500 + j,
                net_name=f"NET{500 + j}",
                ref="U2",
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1
    return pads


# ----------------------------------------------------------------------------
# 1. Snap-to-cardinal selection
# ----------------------------------------------------------------------------


class TestComputeTargetDirection:
    """Deterministic snap-to-cardinal of the nearest off-package pad."""

    def _direction_for_target(
        self,
        tx: float,
        ty: float,
        primary: EscapeDirection = EscapeDirection.WEST,
        allow_into_package: bool = False,
    ) -> EscapeDirection | None:
        pad = _west_edge_pad(net=7)
        router = _make_router(net_target_positions={7: [(tx, ty, "Y1")]})
        package = _make_package([pad])
        return router._compute_target_direction(
            pad=pad,
            package=package,
            primary_dir=primary,
            allow_into_package=allow_into_package,
        )

    # NOTE: EscapeDirection.NORTH == (0, +1) in this module (y-down world
    # frame: toward LARGER y).  The assertions below follow that vector
    # convention, not the schematic-intuitive one.

    def test_dominant_negative_x_snaps_west(self):
        assert self._direction_for_target(-5.0, 1.0) == EscapeDirection.WEST

    def test_dominant_positive_y_snaps_north(self):
        # NORTH is along-edge for a west-edge pad (dot == 0): allowed.
        assert self._direction_for_target(-1.0, 5.0) == EscapeDirection.NORTH

    def test_dominant_negative_y_snaps_south(self):
        assert self._direction_for_target(-1.0, -5.0) == EscapeDirection.SOUTH

    def test_dominant_positive_x_is_into_package_for_west_edge(self):
        # EAST points into the package body for a west-edge pad; the
        # guard returns None so the caller keeps the legacy direction.
        assert self._direction_for_target(5.0, 1.0) is None

    def test_dominant_positive_x_allowed_with_into_package_flag(self):
        assert self._direction_for_target(5.0, 1.0, allow_into_package=True) == EscapeDirection.EAST

    def test_exact_tie_precedence_north_beats_east(self):
        # |dx| == |dy|, dx > 0, dy > 0: candidates {NORTH, EAST}; the
        # documented N > E > S > W precedence picks NORTH (along-edge,
        # passes the guard for a west-edge pad).
        assert self._direction_for_target(3.0, 3.0) == EscapeDirection.NORTH

    def test_exact_tie_precedence_east_beats_south(self):
        # dx > 0, dy < 0: candidates {SOUTH, EAST} -> EAST.  EAST is
        # into-package for the west edge, so assert via the unguarded
        # variant.
        assert (
            self._direction_for_target(3.0, -3.0, allow_into_package=True) == EscapeDirection.EAST
        )
        # With the guard active EAST is rejected -> None (NOT silently
        # SOUTH: the tie-break picks first, the guard then rejects).
        assert self._direction_for_target(3.0, -3.0) is None

    def test_exact_tie_precedence_south_beats_west(self):
        # dx < 0, dy < 0: candidates {SOUTH, WEST} -> SOUTH.
        assert self._direction_for_target(-3.0, -3.0) == EscapeDirection.SOUTH

    def test_exact_tie_precedence_north_beats_west(self):
        # dx < 0, dy > 0: candidates {NORTH, WEST} -> NORTH.
        assert self._direction_for_target(-3.0, 3.0) == EscapeDirection.NORTH

    def test_no_map_returns_none(self):
        pad = _west_edge_pad(net=7)
        router = _make_router(net_target_positions=None)
        package = _make_package([pad])
        assert (
            router._compute_target_direction(
                pad=pad, package=package, primary_dir=EscapeDirection.WEST
            )
            is None
        )

    def test_only_same_package_pads_returns_none(self):
        pad = _west_edge_pad(net=7)
        router = _make_router(net_target_positions={7: [(-5.0, 0.0, "U2")]})
        package = _make_package([pad])
        assert (
            router._compute_target_direction(
                pad=pad, package=package, primary_dir=EscapeDirection.WEST
            )
            is None
        )

    def test_nearest_candidate_wins(self):
        pad = _west_edge_pad(net=7)
        # Far candidate to the south, near candidate to the west.
        router = _make_router(net_target_positions={7: [(0.0, -10.0, "C1"), (-2.0, 0.0, "Y1")]})
        package = _make_package([pad])
        assert (
            router._compute_target_direction(
                pad=pad, package=package, primary_dir=EscapeDirection.WEST
            )
            == EscapeDirection.WEST
        )

    def test_equidistant_candidates_break_by_xy_ascending(self):
        pad = _west_edge_pad(net=7)
        # Two candidates at identical distance: (-3, 0) and (3, 0).
        # (x, y) ascending picks (-3, 0) -> WEST, deterministically.
        router = _make_router(net_target_positions={7: [(3.0, 0.0, "B"), (-3.0, 0.0, "A")]})
        package = _make_package([pad])
        assert (
            router._compute_target_direction(
                pad=pad, package=package, primary_dir=EscapeDirection.WEST
            )
            == EscapeDirection.WEST
        )


# ----------------------------------------------------------------------------
# 2 + 3. _try_in_pad_escape: legacy equivalence and target-aware stub
# ----------------------------------------------------------------------------


class TestTryInPadEscapeTargetDirection:
    """``target_direction`` redirects ONLY the inner stub."""

    def _rescue(self, target_direction: EscapeDirection | None) -> EscapeRoute | None:
        router = _make_router()
        pad = _west_edge_pad(net=7)
        package = _make_package([pad])
        return router._try_in_pad_escape(
            pad=pad,
            direction=EscapeDirection.NORTH,
            effective_clearance=0.127,
            escape_width=0.5,
            package=package,
            target_direction=target_direction,
        )

    def test_legacy_none_path_unchanged(self):
        """``target_direction=None`` reproduces the pre-#3428 geometry:
        stub along ``direction`` (NORTH = +y), dispatcher-supplied
        width, route.direction == direction."""
        route = self._rescue(None)
        assert route is not None
        assert route.via is not None and route.via.in_pad
        via_x, via_y = route.via_pos
        ex, ey = route.escape_point
        # Stub points NORTH (+y): same x, larger y.
        assert ex == pytest.approx(via_x, abs=1e-9)
        assert ey > via_y
        assert route.direction == EscapeDirection.NORTH
        # No necking on the legacy path.
        assert route.segments[0].width == pytest.approx(0.5)

    def test_target_direction_redirects_stub_only(self):
        legacy = self._rescue(None)
        redirected = self._rescue(EscapeDirection.SOUTH)
        assert legacy is not None and redirected is not None
        # Via placement is direction-independent: identical position.
        assert redirected.via_pos == legacy.via_pos
        via_x, via_y = redirected.via_pos
        ex, ey = redirected.escape_point
        # Stub now points SOUTH (-y).
        assert ex == pytest.approx(via_x, abs=1e-9)
        assert ey < via_y
        assert redirected.direction == EscapeDirection.SOUTH
        # Stub length magnitude matches the legacy offset.
        legacy_len = abs(legacy.escape_point[1] - legacy.via_pos[1])
        new_len = abs(ey - via_y)
        assert new_len == pytest.approx(legacy_len, abs=1e-9)

    def test_target_aware_stub_is_necked_to_mfr_min_trace(self):
        """Two adjacent redirected stubs at the full 0.5 mm dispatcher
        width would touch edge-to-edge at 0.5 mm pitch; the target-aware
        stub necks to the manufacturer minimum trace (PR #3079
        precedent for lateral stubs)."""
        redirected = self._rescue(EscapeDirection.SOUTH)
        assert redirected is not None
        # jlcpcb-tier1 min_trace is 0.127 mm.
        assert redirected.segments[0].width == pytest.approx(0.127)


# ----------------------------------------------------------------------------
# Adjacent in-pad via detection (pocket-escape trigger input)
# ----------------------------------------------------------------------------


class TestNeighbourClaimedInPadVia:
    def _escape_with_in_pad_via(self, pad: Pad) -> EscapeRoute:
        via = Via(
            x=pad.x,
            y=pad.y,
            drill=0.15,
            diameter=0.3,
            layers=(Layer.F_CU, Layer.B_CU),
            net=pad.net,
            net_name=pad.net_name,
            in_pad=True,
        )
        return EscapeRoute(
            pad=pad,
            direction=EscapeDirection.WEST,
            escape_point=(pad.x - 0.5, pad.y),
            escape_layer=Layer.B_CU,
            via_pos=(pad.x, pad.y),
            via=via,
        )

    def test_adjacent_in_pad_via_detected(self):
        router = _make_router()
        pad = _west_edge_pad(net=7, y=0.0)
        neighbour = _west_edge_pad(net=6, y=-0.5)
        package = _make_package([pad, neighbour])
        escapes = [self._escape_with_in_pad_via(neighbour)]
        assert router._neighbour_claimed_in_pad_via(pad, package, escapes)

    def test_next_but_one_pin_not_adjacent(self):
        router = _make_router()
        pad = _west_edge_pad(net=7, y=0.0)
        far = _west_edge_pad(net=5, y=-1.0)  # 2 x pitch away
        package = _make_package([pad, far])
        escapes = [self._escape_with_in_pad_via(far)]
        assert not router._neighbour_claimed_in_pad_via(pad, package, escapes)

    def test_surface_escape_neighbour_ignored(self):
        router = _make_router()
        pad = _west_edge_pad(net=7, y=0.0)
        neighbour = _west_edge_pad(net=6, y=-0.5)
        package = _make_package([pad, neighbour])
        surface = EscapeRoute(
            pad=neighbour,
            direction=EscapeDirection.WEST,
            escape_point=(neighbour.x - 0.7, neighbour.y),
            escape_layer=Layer.F_CU,
        )
        assert not router._neighbour_claimed_in_pad_via(pad, package, [surface])

    def test_other_component_ignored(self):
        router = _make_router()
        pad = _west_edge_pad(net=7, y=0.0)
        other = Pad(
            x=pad.x,
            y=pad.y - 0.5,
            width=1.5,
            height=0.3,
            net=6,
            net_name="NET6",
            ref="U9",
            pin="1",
            layer=Layer.F_CU,
        )
        package = _make_package([pad])
        escapes = [self._escape_with_in_pad_via(other)]
        assert not router._neighbour_claimed_in_pad_via(pad, package, escapes)


# ----------------------------------------------------------------------------
# 4. Dispatcher integration: pocket-escape rescue (board-04 NRST pattern)
# ----------------------------------------------------------------------------


class TestPocketEscapeRescue:
    """Board-04-like scenario on a synthetic LQFP: pin j=1 (odd index,
    along-edge escape) violates neighbour clearance and claims an in-pad
    via; pin j=2 (even index, clean perpendicular WEST escape) has its
    net target far EAST across the package.  Legacy behaviour strands
    such a pin in the west pocket; the pocket-escape rescue gives it an
    in-pad via with an EAST-pointing inner stub."""

    PITCH = 0.5

    def _net_positions(
        self, pads: list[Pad], target_for_net102: tuple[float, float] | None
    ) -> dict[int, list[tuple[float, float, str]]]:
        positions: dict[int, list[tuple[float, float, str]]] = {}
        for p in pads:
            positions.setdefault(p.net, []).append((p.x, p.y, p.ref))
        # Net 101 (pin j=1): off-package target to the WEST (outward).
        positions.setdefault(101, []).append((-20.0, 0.0, "C1"))
        # Net 102 (pin j=2): optional off-package target EAST.
        if target_for_net102 is not None:
            tx, ty = target_for_net102
            positions.setdefault(102, []).append((tx, ty, "J1"))
        return positions

    def _run_dispatch(self, with_east_target: bool) -> tuple[list[EscapeRoute], list[Pad]]:
        pads = _make_lqfp_west_signal_row()
        net_target_positions = self._net_positions(pads, (20.0, 0.0) if with_east_target else None)
        router = _make_router(net_target_positions=net_target_positions)
        package = router.analyze_package(pads)
        assert package.package_type in (
            PackageType.QFP,
            PackageType.TQFP,
            PackageType.QFN,
        )
        escapes = router._escape_qfp_alternating(package)
        return escapes, pads

    def test_pocket_pin_rescued_with_east_stub(self):
        escapes, pads = self._run_dispatch(with_east_target=True)
        # Pin j=1 (net 101, odd index) must hold an in-pad via -- this is
        # the standard violation-triggered rescue and the pocket trigger's
        # adjacency prerequisite.
        rescue_101 = next((e for e in escapes if e.pad.net == 101), None)
        assert rescue_101 is not None and rescue_101.via is not None
        assert getattr(rescue_101.via, "in_pad", False)

        # Pin j=2 (net 102, even index): pocket-escape rescue fires.
        rescue_102 = next((e for e in escapes if e.pad.net == 102), None)
        assert rescue_102 is not None, "pocket pin must still produce an escape"
        assert rescue_102.via is not None and getattr(rescue_102.via, "in_pad", False), (
            "pocket pin must be rescued with an in-pad via"
        )
        pad_102 = next(p for p in pads if p.net == 102)
        ex, _ey = rescue_102.escape_point
        assert ex > pad_102.x, (
            "pocket rescue stub must point EAST (toward the net target "
            f"across the package); escape_point={rescue_102.escape_point}, "
            f"pad=({pad_102.x}, {pad_102.y})"
        )
        assert rescue_102.direction == EscapeDirection.EAST

    def test_without_target_map_pocket_pin_keeps_surface_escape(self):
        """Legacy-equivalence guard at dispatcher level: without an EAST
        target entry, pin j=2's escape is the plain perpendicular WEST
        surface escape (no via)."""
        escapes, pads = self._run_dispatch(with_east_target=False)
        rescue_102 = next((e for e in escapes if e.pad.net == 102), None)
        assert rescue_102 is not None
        assert rescue_102.via is None, (
            "without target knowledge the pocket trigger must not fire; "
            "pin keeps its legacy surface escape"
        )
        pad_102 = next(p for p in pads if p.net == 102)
        assert rescue_102.escape_point[0] < pad_102.x  # WEST

    def test_target_aligned_with_escape_does_not_trigger(self):
        """A pin whose target lies WEST (same side as its escape) is not
        pocket-rescued even when its neighbour holds an in-pad via."""
        pads = _make_lqfp_west_signal_row()
        positions = self._net_positions(pads, None)
        # Net 102 target WEST: dot(target, escape) > 0 -> no trigger.
        positions.setdefault(102, []).append((-20.0, 0.0, "J1"))
        router = _make_router(net_target_positions=positions)
        package = router.analyze_package(pads)
        escapes = router._escape_qfp_alternating(package)
        rescue_102 = next((e for e in escapes if e.pad.net == 102), None)
        assert rescue_102 is not None
        assert rescue_102.via is None


# ----------------------------------------------------------------------------
# Construction-site plumbing (Autorouter + RoutingOrchestrator)
# ----------------------------------------------------------------------------


class TestNetPadPositionPlumbing:
    """Both router code paths (``kct route`` Autorouter and ``kct
    route-auto`` RoutingOrchestrator) must supply ``net_target_positions``
    to the EscapeRouter -- fixing only one is a known foot-gun."""

    def test_orchestrator_builds_map_from_autorouter_style_pads(self):
        from kicad_tools.router.orchestrator import RoutingOrchestrator

        class _PcbLike:
            pads = {
                ("U1", "1"): Pad(
                    x=1.0,
                    y=2.0,
                    width=0.3,
                    height=1.5,
                    net=5,
                    net_name="A",
                    ref="U1",
                    pin="1",
                ),
                ("C1", "1"): Pad(
                    x=4.0,
                    y=2.0,
                    width=0.5,
                    height=0.5,
                    net=5,
                    net_name="A",
                    ref="C1",
                    pin="1",
                ),
                ("U1", "2"): Pad(
                    x=1.0,
                    y=3.0,
                    width=0.3,
                    height=1.5,
                    net=0,
                    net_name="",
                    ref="U1",
                    pin="2",
                ),
            }

        rules = _make_rules()
        orch = RoutingOrchestrator(pcb=_PcbLike(), rules=rules)
        result = orch._build_net_target_positions()
        assert result == {5: [(4.0, 2.0, "C1"), (1.0, 2.0, "U1")]}

    def test_orchestrator_returns_none_for_opaque_pcb(self):
        from kicad_tools.router.orchestrator import RoutingOrchestrator

        class _Opaque:
            pass

        rules = _make_rules()
        orch = RoutingOrchestrator(pcb=_Opaque(), rules=rules)
        assert orch._build_net_target_positions() is None

    def test_autorouter_map_skips_net_zero_and_sorts(self):
        from kicad_tools.router.core import Autorouter

        rules = _make_rules(manufacturer=None)
        router = Autorouter(width=20.0, height=20.0, rules=rules)
        router.pads[("R1", "2")] = Pad(
            x=3.0,
            y=1.0,
            width=0.5,
            height=0.5,
            net=9,
            net_name="N9",
            ref="R1",
            pin="2",
        )
        router.pads[("R1", "1")] = Pad(
            x=1.0,
            y=1.0,
            width=0.5,
            height=0.5,
            net=9,
            net_name="N9",
            ref="R1",
            pin="1",
        )
        router.pads[("J1", "1")] = Pad(
            x=5.0,
            y=5.0,
            width=0.5,
            height=0.5,
            net=0,
            net_name="",
            ref="J1",
            pin="1",
        )
        result = router._build_net_target_positions()
        assert result == {9: [(1.0, 1.0, "R1"), (3.0, 1.0, "R1")]}
