"""Tests for cross-package diff-pair corridor reservation (Issue #4086).

Background
----------

Epic #2556 Phase 2F (issues #2639, #2677) added paired escape + corridor
reservation for diff pairs whose BOTH halves land on the same package
(``EscapeRouter._generate_paired_escapes`` ->
``_escape_diff_pair_segment`` + ``_reserve_pair_continuation_corridor``).
Cross-package pairs -- partner pin on a DIFFERENT package (driver ->
connector, or driver -> receiver on opposite board sides) -- were
explicitly skipped: both legs escaped single-ended and the only coupling
downstream was ``CoupledPathfinder`` free-searching the joint state space.

Issue #4086 (Phase 1) closes that gap behind a flag (default OFF):
``EscapeRouter._reserve_cross_package_pair_corridor`` reserves a SOFT
(attractor-only) continuation corridor from each leg's escape launch point
toward the off-package partner, resolved via the board-wide
``net_pad_positions`` map, so the coupled search (via the #4080 attractor)
has geometry to follow.

These tests pin the Phase-1 contract:

1. Detection fires ONLY when the flag is on AND ``net_pad_positions``
   resolves an off-package partner endpoint.
2. Flag OFF (default) => byte-identical no-op (no reservation, cell count
   unchanged); the leg still escapes single-ended.
3. ``net_pad_positions`` absent / partner unresolvable => no-op even with
   the flag ON (fallback-safety, mirrors ``_select_pair_launch_direction``).
4. The reservation is SOFT (only the attractor applies -- foreign copper
   is NOT fenced out) and owned by the pair's net-id set only (never a
   third net).
5. Intra-package pairs are unaffected -- they still take the existing
   paired-escape path, not the cross-package path.

The fixtures are synthetic two-package layouts on an otherwise-empty grid;
no board file is required (board 00-07 fixtures have no cross-package diff
pair today).
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.escape import (
    EscapeRouter,
    PackageInfo,
    PackageType,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import (
    NET_CLASS_HIGH_SPEED,
    DesignRules,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


@pytest.fixture
def grid_4layer(rules: DesignRules) -> RoutingGrid:
    """4-layer all-signal grid so an inner SIGNAL layer exists for the
    corridor (the intra-package corridor uses the same stack-up)."""
    stack = LayerStack.four_layer_all_signal()
    return RoutingGrid(60, 60, rules, origin_x=0, origin_y=0, layer_stack=stack)


@pytest.fixture
def grid_2layer(rules: DesignRules) -> RoutingGrid:
    """2-layer grid -- no inner signal layer, so the corridor must no-op."""
    return RoutingGrid(60, 60, rules, origin_x=0, origin_y=0)


# =============================================================================
# Synthetic two-package fixture builders
# =============================================================================

DRIVER_REF = "U1"
CONNECTOR_REF = "J1"
P_NET = "HDMI_D0_P"
N_NET = "HDMI_D0_N"
P_ID = 1
N_ID = 2


def make_driver_qfn(
    *,
    p_net: str = P_NET,
    n_net: str = N_NET,
    p_id: int = P_ID,
    n_id: int = N_ID,
) -> list[Pad]:
    """QFN-8 driver whose SOUTH edge carries ONE half of a diff pair.

    The partner half is intentionally NOT on this package -- it lives on
    the connector (see ``make_connector_pads``).  The other 7 pins get
    unique single-ended nets.
    """
    pitch = 0.5
    pins_per_side = 8
    half = (pins_per_side - 1) * pitch / 2 + 1.0
    pads: list[Pad] = []
    nid = 100

    # South edge (y = -half): index 4 carries the P half; index 5 carries
    # a DIFFERENT single-ended net (the N half is off-package).
    for i in range(pins_per_side):
        x = -half + 1.0 + i * pitch
        if i == 4:
            net_name, net_id = p_net, p_id
        else:
            net_name, net_id = f"U1_NET_{nid}", nid
            nid += 1
        pads.append(
            Pad(
                x=x,
                y=-half,
                width=0.3,
                height=0.8,
                net=net_id,
                net_name=net_name,
                layer=Layer.F_CU,
                ref=DRIVER_REF,
            )
        )
    return pads


def make_connector_pads(
    *,
    n_net: str = N_NET,
    n_id: int = N_ID,
    origin_y: float = 20.0,
) -> list[Pad]:
    """A connector footprint placed well AWAY from the driver.

    Carries the N half of the diff pair (its partner is on the driver).
    We only need it to populate the board-wide ``net_pad_positions`` map;
    it is NOT the package passed to ``generate_escapes`` in these tests.
    """
    return [
        Pad(
            x=0.0,
            y=origin_y,
            width=0.3,
            height=0.8,
            net=n_id,
            net_name=n_net,
            layer=Layer.F_CU,
            ref=CONNECTOR_REF,
        ),
    ]


def make_package_info(pads: list[Pad], pkg_type: PackageType, ref: str) -> PackageInfo:
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    bbox = (min(xs), min(ys), max(xs), max(ys))
    pitches = []
    for i, a in enumerate(pads):
        for b in pads[i + 1 :]:
            d = math.hypot(a.x - b.x, a.y - b.y)
            if d > 0:
                pitches.append(d)
    pitch = min(pitches) if pitches else 0.5
    return PackageInfo(
        ref=ref,
        package_type=pkg_type,
        center=(cx, cy),
        pads=pads,
        pin_count=len(pads),
        pin_pitch=pitch,
        bounding_box=bbox,
        is_dense=True,
    )


def build_board_wide_maps(
    driver_pads: list[Pad], connector_pads: list[Pad]
) -> tuple[dict[str, list[tuple[float, float]]], dict[str, int]]:
    """Build the board-wide net_pad_positions + net_name_to_id maps the
    real ``Autorouter._escape`` property assembles from ``self.pads``."""
    net_pad_positions: dict[str, list[tuple[float, float]]] = {}
    net_name_to_id: dict[str, int] = {}
    for pad in [*driver_pads, *connector_pads]:
        if pad.net_name:
            net_pad_positions.setdefault(pad.net_name, []).append((pad.x, pad.y))
            if pad.net is not None and pad.net_name not in net_name_to_id:
                net_name_to_id[pad.net_name] = int(pad.net)
    return net_pad_positions, net_name_to_id


def make_router(
    grid: RoutingGrid,
    rules: DesignRules,
    *,
    enable_flag: bool,
    with_board_maps: bool = True,
) -> tuple[EscapeRouter, PackageInfo]:
    """Assemble an EscapeRouter + the driver PackageInfo for a
    cross-package HDMI_D0 pair (P on driver, N on connector)."""
    driver_pads = make_driver_qfn()
    connector_pads = make_connector_pads()
    info = make_package_info(driver_pads, PackageType.QFN, DRIVER_REF)
    ncm = {P_NET: NET_CLASS_HIGH_SPEED, N_NET: NET_CLASS_HIGH_SPEED}

    if with_board_maps:
        net_pad_positions, net_name_to_id = build_board_wide_maps(driver_pads, connector_pads)
    else:
        net_pad_positions, net_name_to_id = {}, {}

    er = EscapeRouter(
        grid,
        rules,
        net_class_map=ncm,
        diff_pair_map={P_NET: N_NET, N_NET: P_NET},
        net_pad_positions=net_pad_positions,
        net_name_to_id=net_name_to_id,
        enable_cross_package_pair_corridor=enable_flag,
    )
    return er, info


# =============================================================================
# Contract 1: fires only when flag ON and partner resolvable
# =============================================================================


class TestCrossPackageDetection:
    def test_flag_on_reserves_corridor(self, grid_4layer, rules):
        """Flag ON + resolvable off-package partner => a soft corridor is
        reserved for the cross-package leg."""
        er, info = make_router(grid_4layer, rules, enable_flag=True)
        assert er.cross_package_pair_corridor_reservations == 0
        assert grid_4layer.reserved_cell_count() == 0

        er.generate_escapes(info)

        assert er.cross_package_pair_corridor_reservations == 1, (
            "Expected exactly one cross-package corridor reservation "
            "(one cross-package leg on the driver package)"
        )
        assert er.cross_package_pair_corridor_reserved_cells >= 1
        assert grid_4layer.reserved_cell_count() >= 1

    def test_flag_off_is_noop(self, grid_4layer, rules):
        """Flag OFF (default) => byte-identical no-op: no reservation, no
        reserved cells; the leg still escapes single-ended."""
        er, info = make_router(grid_4layer, rules, enable_flag=False)

        er.generate_escapes(info)

        # Flag OFF => the cross-package branch is a no-op ``continue``:
        # no reservation, no reserved cells.  The cross-package leg is NOT
        # removed from the single-ended dispatcher's input (it was never
        # added to ``paired_pad_keys``), so escape dispatch is unchanged.
        assert er.cross_package_pair_corridor_reservations == 0
        assert er.cross_package_pair_corridor_reserved_cells == 0
        assert grid_4layer.reserved_cell_count() == 0

    def test_flag_off_vs_on_dispatch_identical(self, grid_4layer, rules):
        """The cross-package leg escapes single-ended IDENTICALLY whether
        the flag is on or off -- Phase 1 only adds a corridor reservation,
        it never changes the leg's own escape geometry."""
        er_off, info = make_router(grid_4layer, rules, enable_flag=False)
        esc_off = er_off.generate_escapes(info)

        grid2 = RoutingGrid(
            60, 60, rules, origin_x=0, origin_y=0, layer_stack=LayerStack.four_layer_all_signal()
        )
        er_on, info_on = make_router(grid2, rules, enable_flag=True)
        esc_on = er_on.generate_escapes(info_on)

        # Same number of escape routes and same escape points (the leg's
        # own geometry is untouched by the corridor reservation).
        assert len(esc_off) == len(esc_on)
        pts_off = sorted(round(c, 4) for e in esc_off for c in e.escape_point)
        pts_on = sorted(round(c, 4) for e in esc_on for c in e.escape_point)
        assert pts_off == pts_on, (
            "Cross-package leg escape geometry must be identical flag on vs off"
        )

    def test_no_board_maps_is_noop_even_with_flag_on(self, grid_4layer, rules):
        """Flag ON but ``net_pad_positions`` absent => no-op (partner
        endpoint unresolvable).  Mirrors the fallback-safety contract on
        ``_select_pair_launch_direction``."""
        er, info = make_router(grid_4layer, rules, enable_flag=True, with_board_maps=False)

        er.generate_escapes(info)

        assert er.cross_package_pair_corridor_reservations == 0
        assert er.cross_package_pair_corridor_reserved_cells == 0
        assert grid_4layer.reserved_cell_count() == 0

    def test_two_layer_grid_is_noop(self, grid_2layer, rules):
        """No inner signal layer (2-layer stack) => corridor no-op even
        with the flag on and the partner resolvable."""
        er, info = make_router(grid_2layer, rules, enable_flag=True)

        er.generate_escapes(info)

        assert er.cross_package_pair_corridor_reservations == 0
        assert grid_2layer.reserved_cell_count() == 0


# =============================================================================
# Contract 2: reservation is SOFT and owned by the pair only
# =============================================================================


class TestCrossPackageReservationSemantics:
    def test_owner_set_is_exactly_the_pair(self, grid_4layer, rules):
        """The corridor owner set is exactly {P_ID, N_ID} -- never a
        third (foreign) net."""
        er, info = make_router(grid_4layer, rules, enable_flag=True)
        er.generate_escapes(info)

        reserved = list(grid_4layer._reserved_for_nets.items())
        assert reserved, "Expected a non-empty reservation map"
        for _key, owners in reserved:
            assert owners == frozenset({P_ID, N_ID}), (
                f"Cross-package corridor owner set must be the pair "
                f"{{{P_ID}, {N_ID}}}, got {owners}"
            )

    def test_reservation_is_soft(self, grid_4layer, rules):
        """Every reserved cell is marked SOFT (attractor-only) -- a
        cross-package span must not hard-fence foreign copper out."""
        er, info = make_router(grid_4layer, rules, enable_flag=True)
        er.generate_escapes(info)

        reserved_keys = set(grid_4layer._reserved_for_nets.keys())
        assert reserved_keys, "Expected reserved cells"
        # A SOFT reservation records every reserved cell key in
        # ``_soft_reservations``.  A HARD reservation records none.
        assert reserved_keys <= grid_4layer._soft_reservations, (
            "Cross-package corridor cells must all be SOFT reservations"
        )

    def test_soft_corridor_does_not_fence_foreign_via(self, grid_4layer, rules):
        """A foreign-net via placed on a soft-reserved cell still claims
        it (soft = attractor-only, no keep-out fence)."""
        from kicad_tools.router.primitives import Via

        er, info = make_router(grid_4layer, rules, enable_flag=True)
        er.generate_escapes(info)

        reserved = list(grid_4layer._reserved_for_nets.items())
        (layer_idx, gy, gx), _owners = reserved[len(reserved) // 2]
        wx, wy = grid_4layer.grid_to_world(gx, gy)

        foreign_via = Via(
            x=wx,
            y=wy,
            drill=rules.via_drill,
            diameter=rules.via_diameter,
            layers=(Layer.F_CU, Layer.B_CU),
            net=999,  # not in {P_ID, N_ID}
            net_name="FOREIGN",
        )
        grid_4layer._mark_via(foreign_via)

        # SOFT => the foreign via was NOT fenced out; the cell is blocked
        # (claimed) by the foreign via.
        assert grid_4layer.grid[layer_idx][gy][gx].blocked, (
            "A soft cross-package corridor must NOT fence a foreign via out"
        )


# =============================================================================
# Contract 3: intra-package pairs are unaffected (still Phase 2F path)
# =============================================================================


class TestIntraPackageUnaffected:
    def _make_intra_pair(self) -> list[Pad]:
        """QFN-8 whose south edge carries BOTH halves of a pair
        (adjacent) -- the classic intra-package Phase 2F case."""
        pitch = 0.5
        pins_per_side = 8
        half = (pins_per_side - 1) * pitch / 2 + 1.0
        pads: list[Pad] = []
        nid = 200
        for i in range(pins_per_side):
            x = -half + 1.0 + i * pitch
            if i == 4:
                net_name, net_id = P_NET, P_ID
            elif i == 5:
                net_name, net_id = N_NET, N_ID
            else:
                net_name, net_id = f"U1_NET_{nid}", nid
                nid += 1
            pads.append(
                Pad(
                    x=x,
                    y=-half,
                    width=0.3,
                    height=0.8,
                    net=net_id,
                    net_name=net_name,
                    layer=Layer.F_CU,
                    ref=DRIVER_REF,
                )
            )
        return pads

    def test_intra_pair_uses_phase2f_not_cross_package(self, grid_4layer, rules):
        """With BOTH halves on-package, the intra-package paired-escape
        path fires (``pair_corridor_reservations``) and the cross-package
        path does NOT -- even with the cross-package flag ON."""
        pads = self._make_intra_pair()
        info = make_package_info(pads, PackageType.QFN, DRIVER_REF)
        net_pad_positions: dict[str, list[tuple[float, float]]] = {}
        net_name_to_id: dict[str, int] = {}
        for p in pads:
            if p.net_name:
                net_pad_positions.setdefault(p.net_name, []).append((p.x, p.y))
                if p.net is not None:
                    net_name_to_id.setdefault(p.net_name, int(p.net))

        er = EscapeRouter(
            grid_4layer,
            rules,
            net_class_map={P_NET: NET_CLASS_HIGH_SPEED, N_NET: NET_CLASS_HIGH_SPEED},
            diff_pair_map={P_NET: N_NET, N_NET: P_NET},
            net_pad_positions=net_pad_positions,
            net_name_to_id=net_name_to_id,
            enable_cross_package_pair_corridor=True,
        )
        er.generate_escapes(info)

        assert er.diff_pair_segment_calls == 1, (
            "Intra-package pair should take the paired-escape segment path"
        )
        assert er.pair_corridor_reservations == 1, (
            "Intra-package pair should reserve the Phase 2F continuation corridor"
        )
        assert er.cross_package_pair_corridor_reservations == 0, (
            "Intra-package pair must NOT trigger the cross-package corridor path"
        )
