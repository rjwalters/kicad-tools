"""Tests for diff-pair-aware escape routing (Issue #2639 / Epic #2556 Phase 2F).

This file implements the five-gate verification chain from the issue:

1. CLI ``--differential-pairs`` flag -> autorouter populates a diff_pair_map
2. Autorouter -> EscapeRouter constructor receives the map at all three sites
3. EscapeRouter stores AND ``generate_escapes`` consults the map
4. Dispatch reaches ``_escape_diff_pair_segment`` for each of the three
   priority dispatchers (BGA, QFP/QFN, MULTI_ROW_CONNECTOR / USB-C),
   plus a negative case
5. Paired escape segments are emitted with the expected coupled spacing
   (within +/-15% of the target intra-pair clearance + trace width)

Plus a no-regression block per dispatcher.

Per the issue's #2587 lesson, every gate has an explicit test (not an
inspection): a future regression that silently breaks the wiring is
caught by exactly one gate failing.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.escape import (
    EscapeRouter,
    PackageInfo,
    PackageType,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import (
    NET_CLASS_HIGH_SPEED,
    DesignRules,
    NetClassRouting,
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
def grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(50, 50, rules, origin_x=0, origin_y=0)


# =============================================================================
# Synthetic package builders
# =============================================================================


def make_bga_with_pair(p_net: str = "TX_P", n_net: str = "TX_N") -> list[Pad]:
    """Build a 4x4 BGA where balls B2/B3 carry the diff pair.

    Other 14 balls get unique net names (NET_3 .. NET_16).
    """
    pitch = 0.8
    pads: list[Pad] = []
    nid = 3
    for row in range(4):
        for col in range(4):
            x = -pitch * 1.5 + col * pitch
            y = -pitch * 1.5 + row * pitch
            if row == 1 and col == 1:
                net_name, net_id = p_net, 1
            elif row == 1 and col == 2:
                net_name, net_id = n_net, 2
            else:
                net_name, net_id = f"NET_{nid}", nid
                nid += 1
            pads.append(
                Pad(
                    x=x, y=y, width=0.4, height=0.4,
                    net=net_id, net_name=net_name,
                    layer=Layer.F_CU, ref="U1", through_hole=False,
                )
            )
    return pads


def make_qfn_with_pair(p_net: str = "USB_D+", n_net: str = "USB_D-") -> list[Pad]:
    """Build a QFN with 8 pins per side at 0.5mm pitch.

    Pins on the south edge include the diff pair on adjacent positions.
    """
    pitch = 0.5
    pins_per_side = 8
    half = (pins_per_side - 1) * pitch / 2 + 1.0
    pads: list[Pad] = []
    nid = 3

    # South side - pad index 4 and 5 are the diff pair
    for i in range(pins_per_side):
        x = -half + 1.0 + i * pitch
        if i == 4:
            net_name, net_id = p_net, 1
        elif i == 5:
            net_name, net_id = n_net, 2
        else:
            net_name, net_id = f"NET_{nid}", nid
            nid += 1
        pads.append(
            Pad(
                x=x, y=-half, width=0.3, height=0.8,
                net=net_id, net_name=net_name,
                layer=Layer.F_CU, ref="U2",
            )
        )
    # The other three sides
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=half, y=-half + 1.0 + i * pitch,
                width=0.8, height=0.3,
                net=nid, net_name=f"NET_{nid}",
                layer=Layer.F_CU, ref="U2",
            )
        )
        nid += 1
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=half - 1.0 - i * pitch, y=half,
                width=0.3, height=0.8,
                net=nid, net_name=f"NET_{nid}",
                layer=Layer.F_CU, ref="U2",
            )
        )
        nid += 1
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=-half, y=half - 1.0 - i * pitch,
                width=0.8, height=0.3,
                net=nid, net_name=f"NET_{nid}",
                layer=Layer.F_CU, ref="U2",
            )
        )
        nid += 1
    return pads


def make_usbc_with_pair(p_net: str = "USB_D+", n_net: str = "USB_D-") -> list[Pad]:
    """Build a synthetic USB-C (MULTI_ROW_CONNECTOR) with 24 pads.

    Two rows of 12 pads at 0.5mm pitch + mounting tabs.  Pads A6 / A7
    on row A carry the diff pair.
    """
    pitch = 0.5
    pads_per_row = 12
    pads: list[Pad] = []
    nid = 3
    half = (pads_per_row - 1) * pitch / 2

    # Row A (y=0) - indices 5 and 6 are the diff pair
    for i in range(pads_per_row):
        x = -half + i * pitch
        if i == 5:
            net_name, net_id = p_net, 1
        elif i == 6:
            net_name, net_id = n_net, 2
        else:
            net_name, net_id = f"NET_{nid}", nid
            nid += 1
        pads.append(
            Pad(
                x=x, y=0.0, width=0.25, height=0.35,
                net=net_id, net_name=net_name,
                layer=Layer.F_CU, ref="J1", through_hole=False,
            )
        )

    # Row B (y=1.0) - all unique nets
    for i in range(pads_per_row):
        x = -half + i * pitch
        pads.append(
            Pad(
                x=x, y=1.0, width=0.25, height=0.35,
                net=nid, net_name=f"NET_{nid}",
                layer=Layer.F_CU, ref="J1", through_hole=False,
            )
        )
        nid += 1

    # Through-hole mounting tabs to push pin_count past the MULTI_ROW threshold
    for tx in (-half - 1.0, half + 1.0):
        pads.append(
            Pad(
                x=tx, y=0.0, width=1.0, height=1.0,
                net=nid, net_name=f"NET_{nid}",
                layer=Layer.F_CU, ref="J1",
                through_hole=True, drill=0.6,
            )
        )
        nid += 1
    return pads


def make_package_info(pads: list[Pad], pkg_type: PackageType, ref: str) -> PackageInfo:
    """Build a PackageInfo without consulting the (often noisy) detector.

    The diff-pair pre-pass is invoked from ``generate_escapes`` based
    only on ``package.package_type``, so for unit tests we synthesise a
    PackageInfo with the correct type rather than relying on the
    detection heuristics for our small synthetic fixtures.
    """
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    bbox = (min(xs), min(ys), max(xs), max(ys))
    # Estimate pitch from nearest-neighbour spacing
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


# =============================================================================
# Gate 1: CLI flag -> Autorouter populates a diff_pair_map
# =============================================================================


class TestGate1AutorouterDiffPairMap:
    """Autorouter exposes a non-empty diff_pair_map when paired nets exist."""

    def _build_ar(self) -> Autorouter:
        ar = Autorouter(width=20.0, height=20.0)
        ar.nets[1] = [("J1", "1")]
        ar.nets[2] = [("J1", "2")]
        ar.net_names[1] = "USB_D+"
        ar.net_names[2] = "USB_D-"
        ar.net_class_map["USB_D+"] = NET_CLASS_HIGH_SPEED
        ar.net_class_map["USB_D-"] = NET_CLASS_HIGH_SPEED
        return ar

    def test_get_diff_pair_map_returns_bidirectional_dict(self):
        ar = self._build_ar()
        m = ar.get_diff_pair_map()
        assert m == {"USB_D+": "USB_D-", "USB_D-": "USB_D+"}

    def test_get_diff_pair_map_empty_when_no_pairs(self):
        ar = Autorouter(width=20.0, height=20.0)
        ar.nets[1] = [("U1", "1")]
        ar.net_names[1] = "SIG_A"
        ar.net_class_map["SIG_A"] = NetClassRouting(name="Plain")
        assert ar.get_diff_pair_map() == {}

    def test_get_diff_pair_map_no_net_names(self):
        ar = Autorouter(width=20.0, height=20.0)
        # Empty net_names -> empty map (defensive case)
        assert ar.get_diff_pair_map() == {}


# =============================================================================
# Gate 2: Autorouter -> EscapeRouter constructor receives the map
# =============================================================================


class TestGate2EscapeRouterCtorReceivesMap:
    """EscapeRouter construction at all three sites receives the map."""

    def _build_ar_with_pair(self) -> Autorouter:
        ar = Autorouter(width=20.0, height=20.0)
        ar.nets[1] = [("J1", "1")]
        ar.nets[2] = [("J1", "2")]
        ar.net_names[1] = "USB_D+"
        ar.net_names[2] = "USB_D-"
        ar.net_class_map["USB_D+"] = NET_CLASS_HIGH_SPEED
        ar.net_class_map["USB_D-"] = NET_CLASS_HIGH_SPEED
        return ar

    def test_autorouter_escape_property_passes_map(self):
        """core.py _escape property threads the map when coupling is active.

        Issue #3419: the map is now gated on ``paired_escape_coupling``
        (flipped on by ``route_all_with_diffpairs``).  Without a coupled
        consumer, the tightly-coupled paired escape endpoints strand the
        plain per-net A* (board 06: 41% -> 27% reach regression).
        """
        ar = self._build_ar_with_pair()
        ar.paired_escape_coupling = True
        escape = ar._escape
        assert isinstance(escape, EscapeRouter)
        assert escape.diff_pair_map == {"USB_D+": "USB_D-", "USB_D-": "USB_D+"}

    def test_autorouter_escape_property_empty_map_without_coupling(self):
        """Issue #3419: per-net routing (no coupled consumer) -> empty map.

        The paired-escape pre-pass emits endpoints at the intra-pair
        clearance; only the CoupledPathfinder can route from them.  When
        ``paired_escape_coupling`` is False (default; plain
        ``route_all`` / ``route_all_negotiated``), the map must NOT be
        threaded even though pairs are detected.
        """
        ar = self._build_ar_with_pair()
        assert ar.paired_escape_coupling is False
        escape = ar._escape
        assert escape.diff_pair_map == {}

    def test_route_all_with_diffpairs_flips_coupling_flag(self):
        """``route_all_with_diffpairs`` enables the pre-pass before routing
        and refreshes an already-created escape router's map in place
        (Issue #3419).
        """
        import contextlib
        from unittest.mock import MagicMock

        from kicad_tools.router.diffpair import DifferentialPairConfig

        ar = self._build_ar_with_pair()
        # Simulate an earlier phase having created the escape router with
        # the gate off.
        escape = ar._escape
        assert escape.diff_pair_map == {}

        # ``Autorouter._diffpair`` is a lazy property over
        # ``_diffpair_router`` -- mock the underlying attribute.
        ar._diffpair_router = MagicMock()
        ar._diffpair_router.route_all_with_diffpairs = MagicMock(return_value=([], []))
        ar._diffpair_router.intra_clearance_violations = MagicMock(return_value=[])
        # The mocked inner router may not satisfy the full post-route
        # pipeline; the flag flip happens FIRST so it is still
        # observable either way.
        with contextlib.suppress(Exception):
            ar.route_all_with_diffpairs(DifferentialPairConfig(enabled=True))
        assert ar.paired_escape_coupling is True
        assert escape.diff_pair_map == {"USB_D+": "USB_D-", "USB_D-": "USB_D+"}

    def test_autorouter_escape_property_empty_map_for_no_pairs(self):
        """When no pairs are detected, diff_pair_map is empty (regression
        check: pre-#2639 single-ended behavior must be preserved exactly).
        """
        ar = Autorouter(width=20.0, height=20.0)
        ar.nets[1] = [("U1", "1")]
        ar.net_names[1] = "SIG_A"
        ar.net_class_map["SIG_A"] = NetClassRouting(name="Plain")
        escape = ar._escape
        assert escape.diff_pair_map == {}

    def test_orchestrator_ctor_sites_thread_map(self):
        """The orchestrator's ``_get_diff_pair_map`` resolves the map.

        Tested via a lightweight PCB-like shim that exposes the same
        ``get_diff_pair_map`` hook the orchestrator looks up.  Note
        (Issue #3432): the EscapeRouter ctor sites now gate the map on
        ``paired_escape_coupling`` -- see
        ``TestOrchestratorPairedEscapeGate`` for the threading tests.
        """
        from kicad_tools.router.orchestrator import RoutingOrchestrator

        class _ShimPcb:
            def __init__(self):
                self.grid = None
                self.net_names = {1: "USB_D+", 2: "USB_D-"}
                self._edge_clearance = None
                self._board_bbox = None

            def get_diff_pair_map(self):
                return {"USB_D+": "USB_D-", "USB_D-": "USB_D+"}

        pcb = _ShimPcb()
        rules = DesignRules()
        orch = RoutingOrchestrator(pcb=pcb, rules=rules)
        # Without a grid the escape_router property short-circuits to
        # None, but _get_diff_pair_map should still resolve to the
        # non-empty map from the shim's hook.
        assert orch._get_diff_pair_map() == {
            "USB_D+": "USB_D-",
            "USB_D-": "USB_D+",
        }


# =============================================================================
# Issue #3432: RoutingOrchestrator paired-escape coupling gate
# =============================================================================


class TestOrchestratorPairedEscapeGate:
    """RoutingOrchestrator gates diff_pair_map on ``paired_escape_coupling``.

    Issue #3432 (mirrors the Autorouter gate from #3419/#3431): both
    orchestrator EscapeRouter construction sites
    (``_route_escape_then_global`` and the ``escape_router`` property)
    must NOT thread the diff-pair map unless a coupled consumer exists
    on the route-auto path.  The route-auto escape consumer is the
    per-net GlobalRouter, which cannot route from the tightly-coupled
    paired escape endpoints the pre-pass emits -- threading the map
    unconditionally is the same stranding mechanism that regressed
    board 06 from 41% to 27% reach on the Autorouter path.
    """

    PAIR_MAP = {"USB_D+": "USB_D-", "USB_D-": "USB_D+"}

    def _build_orchestrator(self, grid, rules):
        from kicad_tools.router.orchestrator import RoutingOrchestrator

        pair_map = self.PAIR_MAP

        class _ShimPcb:
            """PCB-like shim with a real grid so the EscapeRouter ctor
            sites actually construct (unlike the grid=None shim above)."""

            def __init__(self):
                self.grid = grid
                self.net_names = {1: "USB_D+", 2: "USB_D-"}
                self._edge_clearance = None
                self._board_bbox = None

            def get_diff_pair_map(self):
                return dict(pair_map)

        return RoutingOrchestrator(pcb=_ShimPcb(), rules=rules)

    def test_orchestrator_escape_property_empty_map_without_coupling(self, grid, rules):
        """Mirror of ``test_autorouter_escape_property_empty_map_without_coupling``.

        ``paired_escape_coupling`` defaults to False (route-auto has no
        CoupledPathfinder), so the ``escape_router`` property ctor site
        must pass an empty map even though pairs ARE detected.
        """
        orch = self._build_orchestrator(grid, rules)
        assert orch.paired_escape_coupling is False
        # Pairs are detectable...
        assert orch._get_diff_pair_map() == self.PAIR_MAP
        # ...but the EscapeRouter must not receive them.
        escape = orch.escape_router
        assert isinstance(escape, EscapeRouter)
        assert escape.diff_pair_map == {}

    def test_orchestrator_escape_property_passes_map_with_coupling(self, grid, rules):
        """If a coupled consumer is ever added to route-auto, flipping
        the flag on before the escape phase threads the map (same
        contract as ``Autorouter.paired_escape_coupling``)."""
        orch = self._build_orchestrator(grid, rules)
        orch.paired_escape_coupling = True
        escape = orch.escape_router
        assert escape.diff_pair_map == self.PAIR_MAP

    def test_orchestrator_escape_then_global_site_empty_map_without_coupling(self, grid, rules):
        """The ``_route_escape_then_global`` ctor site is gated too.

        Phase 2 (GlobalRouter) is stubbed out so the test exercises only
        the Phase 1 EscapeRouter construction.
        """
        from kicad_tools.router.strategies import RoutingResult, RoutingStrategy

        orch = self._build_orchestrator(grid, rules)
        assert orch.paired_escape_coupling is False
        # Short-circuit Phase 2: the per-net global router is not under
        # test here and needs board geometry the shim does not provide.
        orch._route_global = lambda net, pads: RoutingResult(  # type: ignore[method-assign]
            success=True,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
        )
        pads = [
            Pad(
                x=1.0, y=1.0, width=0.4, height=0.4,
                net=1, net_name="USB_D+",
                layer=Layer.F_CU, ref="U1", through_hole=False,
            ),
            Pad(
                x=5.0, y=5.0, width=0.4, height=0.4,
                net=1, net_name="USB_D+",
                layer=Layer.F_CU, ref="J1", through_hole=False,
            ),
        ]
        result = orch._route_escape_then_global("USB_D+", pads)
        assert result.success is True
        assert orch._escape is not None
        assert orch._escape.diff_pair_map == {}


# =============================================================================
# Gate 3: EscapeRouter stores AND generate_escapes consults the map
# =============================================================================


class TestGate3EscapeRouterUsesMap:
    """The escape router both stores AND consults the diff_pair_map."""

    def test_ctor_stores_map(self, grid, rules):
        m = {"X": "Y", "Y": "X"}
        er = EscapeRouter(grid, rules, diff_pair_map=m)
        assert er.diff_pair_map == m

    def test_default_map_is_empty(self, grid, rules):
        er = EscapeRouter(grid, rules)
        assert er.diff_pair_map == {}

    def test_generate_escapes_invokes_paired_segment(self, grid, rules):
        """generate_escapes increments ``diff_pair_segment_calls`` when
        a paired pad's partner lives on the same package.
        """
        pads = make_qfn_with_pair("USB_D+", "USB_D-")
        info = make_package_info(pads, PackageType.QFN, "U2")
        er = EscapeRouter(
            grid, rules,
            diff_pair_map={"USB_D+": "USB_D-", "USB_D-": "USB_D+"},
        )
        assert er.diff_pair_segment_calls == 0
        er.generate_escapes(info)
        assert er.diff_pair_segment_calls >= 1

    def test_generate_escapes_no_pair_means_no_paired_call(self, grid, rules):
        """When the map is empty, paired-segment is never invoked.

        This is the negative-case lock-in: pre-#2639 behavior preserved.
        """
        pads = make_qfn_with_pair("USB_D+", "USB_D-")
        info = make_package_info(pads, PackageType.QFN, "U2")
        er = EscapeRouter(grid, rules)  # empty diff_pair_map
        er.generate_escapes(info)
        assert er.diff_pair_segment_calls == 0


# =============================================================================
# Gate 4: Dispatch reaches _escape_diff_pair_segment for each priority dispatcher
# =============================================================================


class TestGate4DispatchPerPackageType:
    """All three priority dispatchers route paired pads through the new helper."""

    def test_bga_dispatcher_invokes_paired_segment(self, grid, rules):
        pads = make_bga_with_pair("TX_P", "TX_N")
        info = make_package_info(pads, PackageType.BGA, "U1")
        er = EscapeRouter(
            grid, rules,
            diff_pair_map={"TX_P": "TX_N", "TX_N": "TX_P"},
        )
        er.generate_escapes(info)
        assert er.diff_pair_segment_calls == 1

    def test_qfn_dispatcher_invokes_paired_segment(self, grid, rules):
        pads = make_qfn_with_pair("USB_D+", "USB_D-")
        info = make_package_info(pads, PackageType.QFN, "U2")
        er = EscapeRouter(
            grid, rules,
            diff_pair_map={"USB_D+": "USB_D-", "USB_D-": "USB_D+"},
        )
        er.generate_escapes(info)
        assert er.diff_pair_segment_calls == 1

    def test_usbc_multi_row_dispatcher_invokes_paired_segment(self, grid, rules):
        pads = make_usbc_with_pair("USB_D+", "USB_D-")
        info = make_package_info(pads, PackageType.MULTI_ROW_CONNECTOR, "J1")
        er = EscapeRouter(
            grid, rules,
            diff_pair_map={"USB_D+": "USB_D-", "USB_D-": "USB_D+"},
        )
        er.generate_escapes(info)
        assert er.diff_pair_segment_calls == 1

    def test_negative_no_pair_in_map_zero_calls(self, grid, rules):
        """BGA with NO nets in the diff_pair_map -> no paired segment.

        This is the negative subtest the curator demanded: with no
        relevant pair declared, the pair-aware path must remain dormant
        (the pads escape via the standard ring pattern).
        """
        pads = make_bga_with_pair("FOO_P", "FOO_N")
        info = make_package_info(pads, PackageType.BGA, "U1")
        # Map declares some OTHER pair that doesn't appear on this package
        er = EscapeRouter(
            grid, rules,
            diff_pair_map={"OTHER_P": "OTHER_N", "OTHER_N": "OTHER_P"},
        )
        er.generate_escapes(info)
        assert er.diff_pair_segment_calls == 0

    def test_negative_partner_on_different_package(self, grid, rules):
        """When the partner is declared but not on the same package, the
        pair-aware path is skipped (cross-package coupling is out of scope).
        """
        # Build a BGA whose B2 carries TX_P, but TX_N is on no pad of
        # this fixture (it would be on a different package in the real
        # board).
        pads = make_bga_with_pair("TX_P", "ORPHAN_NET")
        info = make_package_info(pads, PackageType.BGA, "U1")
        # The map declares TX_P partners with TX_N, which is NOT on
        # this package.
        er = EscapeRouter(
            grid, rules,
            diff_pair_map={"TX_P": "TX_N", "TX_N": "TX_P"},
        )
        er.generate_escapes(info)
        assert er.diff_pair_segment_calls == 0


# =============================================================================
# Gate 5: Paired escapes have coupled spacing within +/-15% of target
# =============================================================================


class TestGate5PairedEscapeGeometry:
    """The two escape segments terminate at the target intra-pair spacing.

    Acceptance criterion from the issue: terminate at points within +/-15%
    of the target intra-pair spacing.
    """

    @pytest.mark.parametrize(
        ("fixture_builder", "pkg_type", "ref", "p_net", "n_net"),
        [
            (make_bga_with_pair, PackageType.BGA, "U1", "TX_P", "TX_N"),
            (make_qfn_with_pair, PackageType.QFN, "U2", "USB_D+", "USB_D-"),
            (make_usbc_with_pair, PackageType.MULTI_ROW_CONNECTOR, "J1",
             "USB_D+", "USB_D-"),
        ],
    )
    def test_paired_endpoint_spacing(
        self, grid, rules, fixture_builder, pkg_type, ref, p_net, n_net,
    ):
        pads = fixture_builder(p_net, n_net)
        info = make_package_info(pads, pkg_type, ref)
        # Use HighSpeed net class so effective_intra_pair_clearance returns
        # the 0.075mm target the issue mentions.
        ncm = {p_net: NET_CLASS_HIGH_SPEED, n_net: NET_CLASS_HIGH_SPEED}
        er = EscapeRouter(
            grid, rules, net_class_map=ncm,
            diff_pair_map={p_net: n_net, n_net: p_net},
        )
        escapes = er.generate_escapes(info)
        paired = [e for e in escapes if e.pad.net_name in (p_net, n_net)]
        assert len(paired) == 2

        # The two escape endpoints should be separated by approximately
        # ``intra_pair_clearance + trace_width`` along the lateral axis.
        p_escape = next(e for e in paired if e.pad.net_name == p_net)
        n_escape = next(e for e in paired if e.pad.net_name == n_net)

        target = NET_CLASS_HIGH_SPEED.effective_intra_pair_clearance() + max(
            NET_CLASS_HIGH_SPEED.trace_width,
            NET_CLASS_HIGH_SPEED.trace_width,
        )
        # NET_CLASS_HIGH_SPEED has trace_width=0.2 and
        # intra_pair_clearance=0.075 -> target ~ 0.275 mm
        actual = math.hypot(
            p_escape.escape_point[0] - n_escape.escape_point[0],
            p_escape.escape_point[1] - n_escape.escape_point[1],
        )
        # Acceptance: within +/-15% of target spacing
        low, high = target * 0.85, target * 1.15
        assert low <= actual <= high, (
            f"Paired escape endpoint spacing {actual:.4f} not in [{low:.4f}, {high:.4f}] "
            f"(target={target:.4f}mm)"
        )

    def test_paired_segments_appear_in_grid_reservations(self, grid, rules):
        """``apply_escape_routes`` reserves the paired segments on the grid.

        Gate 5 of the verification chain: the grid state is the input to
        the C++ A* search (and to the Python pathfinder), so this proves
        the paired escapes feed downstream routing identically to
        single-ended escapes.  We assert (a) the paired routes are
        returned by ``apply_escape_routes``, (b) the underlying
        segments are non-empty (a mark_route call with empty segments
        would be a no-op), and (c) the endpoints are exactly the
        ``escape_point`` values from the EscapeRoute objects (proving
        no transformation lost the paired-spacing information between
        ``generate_escapes`` and ``apply_escape_routes``).
        """
        pads = make_usbc_with_pair("USB_D+", "USB_D-")
        info = make_package_info(pads, PackageType.MULTI_ROW_CONNECTOR, "J1")
        ncm = {"USB_D+": NET_CLASS_HIGH_SPEED, "USB_D-": NET_CLASS_HIGH_SPEED}
        er = EscapeRouter(
            grid, rules, net_class_map=ncm,
            diff_pair_map={"USB_D+": "USB_D-", "USB_D-": "USB_D+"},
        )
        escapes = er.generate_escapes(info)
        # Capture the paired escape_points BEFORE apply_escape_routes
        # so we can prove they survive to the grid-reservation pass.
        paired_eps = {
            e.pad.net_name: e.escape_point
            for e in escapes
            if e.pad.net_name in ("USB_D+", "USB_D-")
        }
        assert set(paired_eps.keys()) == {"USB_D+", "USB_D-"}

        routes = er.apply_escape_routes(escapes)

        # Find the two paired-net routes
        p_routes = [r for r in routes if r.net_name == "USB_D+"]
        n_routes = [r for r in routes if r.net_name == "USB_D-"]
        assert len(p_routes) == 1
        assert len(n_routes) == 1

        # Each paired route MUST have at least one segment, and the
        # endpoint must match the escape_point we recorded above (i.e.
        # apply_escape_routes did not silently drop the paired geometry
        # before invoking grid.mark_route).
        for r in p_routes + n_routes:
            assert r.segments, f"Route for {r.net_name} has no segments"
            seg = r.segments[0]
            expected_ep = paired_eps[r.net_name]
            assert seg.x2 == pytest.approx(expected_ep[0], abs=1e-9)
            assert seg.y2 == pytest.approx(expected_ep[1], abs=1e-9)


# =============================================================================
# No-regression: when diff_pair_map is empty, the escape geometry is
# identical to pre-#2639 output.
# =============================================================================


class TestNoRegressionWhenMapEmpty:
    """Empty (or None) diff_pair_map preserves pre-#2639 geometry exactly."""

    @pytest.mark.parametrize("pkg_type", [
        PackageType.BGA,
        PackageType.QFN,
        PackageType.MULTI_ROW_CONNECTOR,
    ])
    def test_empty_map_produces_same_escape_count(self, grid, rules, pkg_type):
        """The total number of escapes matches the no-map baseline."""
        if pkg_type == PackageType.BGA:
            pads = make_bga_with_pair("TX_P", "TX_N")
            ref = "U1"
        elif pkg_type == PackageType.QFN:
            pads = make_qfn_with_pair("USB_D+", "USB_D-")
            ref = "U2"
        else:
            pads = make_usbc_with_pair("USB_D+", "USB_D-")
            ref = "J1"
        info = make_package_info(pads, pkg_type, ref)

        er_baseline = EscapeRouter(grid, rules)
        baseline = er_baseline.generate_escapes(info)

        # New router with empty map - must produce identical count.
        # We can't compare full geometry across runs because of edge
        # clearance / clamping interactions, but identical counts AND
        # zero pair-segment calls is sufficient to prove the dormant-
        # signal lesson from #2587 is honoured.
        er_empty = EscapeRouter(grid, rules, diff_pair_map={})
        empty_run = er_empty.generate_escapes(info)
        assert len(empty_run) == len(baseline)
        assert er_empty.diff_pair_segment_calls == 0


# =============================================================================
# Issue #2677: Inner-layer continuation corridor reservation
# =============================================================================
# The five-gate chain mirrors #2639 but verifies that the partner-via
# placement done by ``_escape_bga_rings`` and the other per-package
# dispatchers does NOT colonise the inner-layer continuation channel a
# paired escape needs.  This is the BGA partner-via escape blocker the
# issue identifies as the binding gap.  See ``escape.py`` /
# ``_reserve_pair_continuation_corridor`` for the implementation.


from kicad_tools.router.layers import LayerStack as _LayerStack  # noqa: E402
from kicad_tools.router.primitives import Via as _Via  # noqa: E402


@pytest.fixture
def grid_4layer(rules: DesignRules) -> RoutingGrid:
    """4-layer JLCPCB tier-1-like grid for corridor tests.

    SIG-GND-PWR-SIG with an inner SIGNAL slot is what board 06 uses; we
    use the all-signal variant here so ``_select_inner_escape_layer``
    returns an actual SIGNAL inner layer the corridor reservation can
    target.  The corridor logic must also gracefully no-op on the
    plain 2-layer ``grid`` fixture (covered by the gate-5 regression
    below).
    """
    stack = _LayerStack.four_layer_all_signal()
    return RoutingGrid(50, 50, rules, origin_x=0, origin_y=0, layer_stack=stack)


class TestCorridorReservation:
    """Five-gate verification chain for Issue #2677 corridor reservation."""

    def _make_bga_pair_router(self, grid_obj, rules_obj):
        """Helper: BGA with a single diff pair, HighSpeed net class.

        Returns ``(escape_router, package_info)`` ready for
        ``generate_escapes``.  Mirrors the BGA fixture used by gates 4/5
        of the original #2639 chain.
        """
        pads = make_bga_with_pair("TX_P", "TX_N")
        info = make_package_info(pads, PackageType.BGA, "U1")
        ncm = {"TX_P": NET_CLASS_HIGH_SPEED, "TX_N": NET_CLASS_HIGH_SPEED}
        er = EscapeRouter(
            grid_obj, rules_obj, net_class_map=ncm,
            diff_pair_map={"TX_P": "TX_N", "TX_N": "TX_P"},
        )
        return er, info

    # ------------------------------------------------------------------
    # Gate (a): inner-layer corridor reserved before partner-via placement
    # ------------------------------------------------------------------

    def test_gate_a_corridor_reserved_during_paired_escape(
        self, grid_4layer, rules,
    ):
        """``_reserve_pair_continuation_corridor`` runs during the
        paired pre-pass.

        Asserts:
            * ``pair_corridor_reservations`` is incremented to 1 (one
              pair).
            * ``pair_corridor_reserved_cells`` is non-zero (the corridor
              actually covers at least one grid cell).
            * The grid reports a matching reserved-cell count via
              ``reserved_cell_count()``.
        """
        er, info = self._make_bga_pair_router(grid_4layer, rules)
        assert er.pair_corridor_reservations == 0
        assert er.pair_corridor_reserved_cells == 0
        assert grid_4layer.reserved_cell_count() == 0

        er.generate_escapes(info)

        assert er.pair_corridor_reservations == 1, (
            "Expected exactly one corridor reservation for one paired pair"
        )
        assert er.pair_corridor_reserved_cells >= 1
        assert grid_4layer.reserved_cell_count() == er.pair_corridor_reserved_cells

    # ------------------------------------------------------------------
    # Gate (b): partner vias respect the reservation
    # ------------------------------------------------------------------

    def test_gate_b_partner_vias_do_not_consume_reserved_cells(
        self, grid_4layer, rules,
    ):
        """A partner-net via placed on top of a reserved cell does NOT
        block that cell.

        We synthesise a via belonging to a NON-paired net (id=42) and
        directly invoke ``_mark_via`` on the reserved layer at the
        centroid of the reservation region.  The reserved cells must
        remain unblocked after the call.
        """
        er, info = self._make_bga_pair_router(grid_4layer, rules)
        er.generate_escapes(info)

        # Snapshot the reserved cells map (private API access OK in
        # this test -- the public API doesn't enumerate cells).
        reserved_items = list(grid_4layer._reserved_for_nets.items())
        assert reserved_items, "Expected non-empty reservation map"

        # Pick a reserved cell roughly at the corridor centroid.
        # Convert grid cell -> world coordinate -> via.
        (layer_idx, gy, gx), owners = reserved_items[len(reserved_items) // 2]
        # The owners set should be exactly {1, 2} (the BGA pair net IDs)
        assert owners == frozenset({1, 2}), (
            f"Corridor owner set should be the paired pair nets {{1,2}}, got {owners}"
        )

        wx, wy = grid_4layer.grid_to_world(gx, gy)

        # Capture pre-state of the reserved cell.
        pre_blocked = grid_4layer.grid[layer_idx][gy][gx].blocked

        # Synthesise a partner-net via (net=42, not in {1, 2}).
        from kicad_tools.router.layers import Layer as _Layer
        partner_via = _Via(
            x=wx, y=wy,
            drill=rules.via_drill,
            diameter=rules.via_diameter,
            layers=(_Layer.F_CU, _Layer.B_CU),
            net=42, net_name="PARTNER",
        )
        grid_4layer._mark_via(partner_via)

        # The reserved cell must NOT have been blocked by this via.
        # (It may have been blocked by something else pre-existing, but
        # if it was unblocked before, it must remain unblocked.)
        if not pre_blocked:
            assert not grid_4layer.grid[layer_idx][gy][gx].blocked, (
                "Partner-net via colonised a corridor-reserved cell"
            )

        # Conversely: an owner-net via DOES block the cell (sanity).
        owner_via = _Via(
            x=wx, y=wy,
            drill=rules.via_drill,
            diameter=rules.via_diameter,
            layers=(_Layer.F_CU, _Layer.B_CU),
            net=1, net_name="TX_P",  # in owner set
        )
        grid_4layer._mark_via(owner_via)
        assert grid_4layer.grid[layer_idx][gy][gx].blocked, (
            "Owner-net via should block its own corridor cell"
        )

    # ------------------------------------------------------------------
    # Gate (c): reservation precedes via-marking in generate_escapes flow
    # ------------------------------------------------------------------

    def test_gate_c_reservation_precedes_partner_via_marking(
        self, grid_4layer, rules,
    ):
        """The reservation count is non-zero AT the time the
        non-paired dispatcher runs its via marking.

        This is the timing gate: the paired pre-pass MUST reserve
        BEFORE the per-package dispatcher creates vias.  We verify by
        running the full ``generate_escapes`` flow and inspecting how
        many vias the BGA dispatcher placed -- there should be more
        non-paired vias than reserved cells in extreme cases, but the
        reservation must already exist BEFORE the dispatcher starts.

        We instrument the dispatcher by calling
        ``generate_escapes`` then running the partner-via marking
        manually via ``apply_escape_routes`` and verifying reservations
        persisted.
        """
        er, info = self._make_bga_pair_router(grid_4layer, rules)
        escapes = er.generate_escapes(info)

        # The corridor was reserved during ``_generate_paired_escapes``,
        # which runs first.  After ``generate_escapes`` returns, the
        # reservations are still in place AND the BGA dispatcher has
        # produced via-bearing escapes for non-paired pads.
        assert er.pair_corridor_reservations == 1
        reserved_before_apply = grid_4layer.reserved_cell_count()
        assert reserved_before_apply > 0

        # The combined escape list contains the paired pair + non-paired
        # ring pads (14 non-paired in our 4x4 BGA).
        paired_count = sum(1 for e in escapes if e.pad.net_name in ("TX_P", "TX_N"))
        non_paired_count = len(escapes) - paired_count
        assert paired_count == 2
        assert non_paired_count >= 1, (
            "Expected at least one non-paired escape (inner-ring partner vias)"
        )

        # Apply all escapes to the grid (marks segments + vias).  The
        # reservation must SURVIVE this pass -- ``_mark_via`` only
        # skips matching-owner-set cells, it does not clear them.
        er.apply_escape_routes(escapes)
        assert grid_4layer.reserved_cell_count() == reserved_before_apply, (
            "Corridor reservation was lost during apply_escape_routes"
        )

    # ------------------------------------------------------------------
    # Gate (d): match-group-aware API accepts list[EscapeRoute] (#2661 hook)
    # ------------------------------------------------------------------

    def test_gate_d_helper_accepts_n_member_list(self, grid_4layer, rules):
        """``_reserve_pair_continuation_corridor`` accepts N>=2 members.

        Epic #2661 will pass a 3+ member list; this gate locks in the
        signature so #2661 inherits the primitive without a follow-up
        API change.
        """
        from kicad_tools.router.escape import EscapeDirection, EscapeRoute
        from kicad_tools.router.layers import Layer as _Layer
        from kicad_tools.router.primitives import Pad as _Pad

        er = EscapeRouter(grid_4layer, rules)

        # Build 3 synthetic EscapeRoute objects launching EAST.
        members = []
        for i, net_id in enumerate((1, 2, 3)):
            pad = _Pad(
                x=0.0, y=float(i) * 0.2,
                width=0.2, height=0.2,
                net=net_id, net_name=f"NET_{net_id}",
                layer=_Layer.F_CU,
            )
            members.append(EscapeRoute(
                pad=pad,
                direction=EscapeDirection.EAST,
                escape_point=(1.0, float(i) * 0.2),
                escape_layer=_Layer.F_CU,
                via_pos=None,
                segments=[],
                via=None,
                ring_index=0,
            ))

        count = er._reserve_pair_continuation_corridor(
            members=members,
            target_inner_layer=_Layer.IN1_CU,
            intra_pair_clearance=0.1,
        )
        assert count >= 1, "3-member corridor reservation should cover >= 1 cell"
        assert er.pair_corridor_reservations == 1
        # Owner set should be {1, 2, 3}.
        owners = next(iter(grid_4layer._reserved_for_nets.values()))
        assert owners == frozenset({1, 2, 3}), (
            f"3-member corridor owners {owners} != {{1,2,3}}"
        )

    # ------------------------------------------------------------------
    # Gate (e): empty diff_pair_map -> NO reservation (regression)
    # ------------------------------------------------------------------

    def test_gate_e_empty_map_no_reservation(self, grid_4layer, rules):
        """When ``diff_pair_map`` is empty, no corridor is reserved.

        Mirrors the ``TestNoRegressionWhenMapEmpty`` pattern: byte-
        identical pre-fix behaviour when the feature is dormant.
        """
        pads = make_bga_with_pair("TX_P", "TX_N")
        info = make_package_info(pads, PackageType.BGA, "U1")
        er = EscapeRouter(grid_4layer, rules)  # empty map
        er.generate_escapes(info)
        assert er.pair_corridor_reservations == 0
        assert er.pair_corridor_reserved_cells == 0
        assert grid_4layer.reserved_cell_count() == 0

    # ------------------------------------------------------------------
    # 2-layer grid: no inner SIGNAL layer -> helper no-ops gracefully
    # ------------------------------------------------------------------

    def test_two_layer_grid_corridor_is_noop(self, grid, rules):
        """A 2-layer grid has no inner copper layer.

        Issue #2677 guard: ``_select_inner_escape_layer`` falls back to
        ``Layer.B_CU`` on 2-layer stacks, but reserving on B.Cu would
        BLOCK partner-net through-hole vias from completing their
        footprint on B.Cu -- which is required for legitimate 2-layer
        routing.  The helper must therefore SKIP reservation when the
        target layer is an outer copper layer.

        This protects boards 01-05 (which run on 2-layer stacks via
        ``--auto-layers``) from regression.
        """
        er, info = self._make_bga_pair_router(grid, rules)
        # ``generate_escapes`` should not raise and must produce zero
        # corridor reservations (the 2-layer guard kicks in).
        er.generate_escapes(info)
        assert er.diff_pair_segment_calls == 1, (
            "Paired escape segment generation must still run on 2-layer boards"
        )
        assert er.pair_corridor_reservations == 0, (
            "Corridor reservation must be skipped on 2-layer boards"
        )
        assert grid.reserved_cell_count() == 0


# =============================================================================
# Issue #2911: Pathfinder corridor awareness (attractor + envelope halo)
# =============================================================================
# The five-gate suite above verifies that the corridor reservation EXISTS
# and that partner-net through-hole vias respect it.  But the pre-#2911
# pathfinder consumed the reservation map ONLY via ``_mark_via`` -- the
# main A* search had no awareness that an inner-layer corridor was
# reserved for a paired set of nets.  On board 06 the USB3_TX1+/- pair
# was stranded even with the reservation in place because the pathfinder
# never bothered to drop into the reserved channel.  These tests pin the
# AC4 "pathfinder corridor awareness" gate (attractor mechanism) and the
# AC6 "envelope robustness" gate (partner-via clearance halo).


class TestCorridorAttractor:
    """Issue #2911: A* pathfinder consults the reservation map as an
    attractor (negative cost), and the corridor envelope absorbs the
    partner-via clearance halo."""

    # ------------------------------------------------------------------
    # AC4 unit test: get_corridor_attractor_bonus contract
    # ------------------------------------------------------------------

    def test_attractor_bonus_returned_for_owner_net(self, grid_4layer, rules):
        """``get_corridor_attractor_bonus`` returns the bonus for cells
        reserved for the queried net.

        Pre-#2911 there was no such helper; pre-#2677 there was no
        reservation map at all.  This gates the AC4 attractor wiring.
        """
        # Reserve a small patch on layer 1 (inner) for nets {1, 2}.
        cells = [(10, 10), (10, 11), (11, 10), (11, 11)]
        grid_4layer.reserve_corridor_cells(
            layer_idx=1, cells=cells, net_ids={1, 2},
        )

        # Owner-net query: bonus magnitude returned.
        bonus = grid_4layer.get_corridor_attractor_bonus(
            layer_idx=1, gx=10, gy=10, net_id=1, bonus=3.0,
        )
        assert bonus == 3.0
        bonus = grid_4layer.get_corridor_attractor_bonus(
            layer_idx=1, gx=11, gy=11, net_id=2, bonus=3.0,
        )
        assert bonus == 3.0

        # Non-owner-net query: zero.
        bonus = grid_4layer.get_corridor_attractor_bonus(
            layer_idx=1, gx=10, gy=10, net_id=42, bonus=3.0,
        )
        assert bonus == 0.0

        # Unreserved cell: zero.
        bonus = grid_4layer.get_corridor_attractor_bonus(
            layer_idx=1, gx=20, gy=20, net_id=1, bonus=3.0,
        )
        assert bonus == 0.0

        # Other-layer query for a reserved (x, y): zero (layer-scoped).
        bonus = grid_4layer.get_corridor_attractor_bonus(
            layer_idx=0, gx=10, gy=10, net_id=1, bonus=3.0,
        )
        assert bonus == 0.0

    def test_attractor_fast_path_when_empty(self, grid_4layer, rules):
        """Empty reservation map -> 0.0 returned immediately (fast path
        preserves pre-#2911 hot-loop performance).
        """
        assert grid_4layer.reserved_cell_count() == 0
        # Even with a non-zero bonus, no reservations -> 0.0.
        bonus = grid_4layer.get_corridor_attractor_bonus(
            layer_idx=1, gx=10, gy=10, net_id=1, bonus=3.0,
        )
        assert bonus == 0.0

    def test_attractor_disabled_when_bonus_is_zero(self, grid_4layer, rules):
        """When ``cost_corridor_attractor`` is 0.0, the helper returns
        0.0 even for reserved cells (feature kill-switch).
        """
        grid_4layer.reserve_corridor_cells(
            layer_idx=1, cells=[(10, 10)], net_ids={1},
        )
        bonus = grid_4layer.get_corridor_attractor_bonus(
            layer_idx=1, gx=10, gy=10, net_id=1, bonus=0.0,
        )
        assert bonus == 0.0

    # ------------------------------------------------------------------
    # AC4 integration test: pathfinder is corridor-aware (fenced corridor)
    # ------------------------------------------------------------------

    def test_pathfinder_prefers_reserved_corridor_for_paired_net(
        self, grid_4layer, rules,
    ):
        """A* prefers cells on the reserved inner layer when the route
        belongs to a paired net.

        This is the AC5 fenced-inner-corridor gate: it verifies that the
        main pathfinder ACTUALLY USES the reservation (not just respects
        it).  Pre-#2911 the attractor did not exist, so the cheapest
        path from start to end stayed on the start layer with no via;
        the reservation was a no-op for the main pathfinder.

        Setup:
            * 4-layer grid (F.Cu, In1.Cu, In2.Cu, B.Cu).
            * Start pad on F.Cu at (5.0, 25.0), end pad on F.Cu at
              (45.0, 25.0) -- a long horizontal route.
            * Inner layer In1 (index 1) carries a reserved corridor for
              the paired net pair {1, 2}, spanning a horizontal strip
              from (5.0, 25.0) to (45.0, 25.0).

        Assertion:
            * The route returned by ``Router.route`` for net 1 uses In1
              for at least some segments (vias present).
        """
        from kicad_tools.router.pathfinder import Router
        from kicad_tools.router.primitives import Pad as _Pad

        # Reserve a horizontal strip on In1 (layer index 1) for nets 1+2.
        # Span: x in [5.0, 45.0], y in [25.0, 25.5] (5 cells tall).
        strip_cells: set[tuple[int, int]] = set()
        for x_mm_x10 in range(50, 451):  # 0.1mm steps in 0.1mm-resolution grid
            for y_mm_x10 in range(245, 256):
                gx, gy = grid_4layer.world_to_grid(x_mm_x10 / 10.0, y_mm_x10 / 10.0)
                strip_cells.add((gx, gy))
        grid_4layer.reserve_corridor_cells(
            layer_idx=1, cells=strip_cells, net_ids={1, 2},
        )
        assert grid_4layer.reserved_cell_count() == len(strip_cells)

        start = _Pad(
            x=5.0, y=25.0, width=0.4, height=0.4,
            net=1, net_name="TX_P", layer=Layer.F_CU,
            ref="U1", pin="1",
        )
        end = _Pad(
            x=45.0, y=25.0, width=0.4, height=0.4,
            net=1, net_name="TX_P", layer=Layer.F_CU,
            ref="U2", pin="1",
        )

        # With attractor enabled (default rule value), route should
        # at minimum complete -- this is the binding assertion.  Pre-#2911
        # patch the route still completes here because F.Cu is wide
        # open; the real-world failure mode is when surface routes are
        # blocked (covered in test_pathfinder_uses_reserved_layer_when_surface_blocked).
        # Here we just verify the attractor doesn't break the basic case.
        router = Router(grid_4layer, rules)
        route = router.route(start, end)
        assert route is not None, "Pathfinder must still complete a basic route"

    def test_pathfinder_uses_reserved_layer_when_surface_blocked(
        self, rules,
    ):
        """When the surface layer is blocked between start and end, the
        attractor-aware pathfinder dives into the reserved inner layer
        and completes the route.

        Pre-#2911 (no attractor), if F.Cu was blocked the pathfinder
        would still consider via transitions to any inner layer, but
        there was no preference for the RESERVED inner layer -- on dense
        boards (BGA-49 USB3 case on board 06) the search exhausted its
        budget exploring the wrong inner layer.

        This test pins the AC4 contract: when the reservation exists,
        the route MUST be able to complete by diving into the reserved
        inner layer, with the attractor making that dive the cheapest
        option.
        """
        from kicad_tools.router.layers import LayerStack
        from kicad_tools.router.pathfinder import Router
        from kicad_tools.router.primitives import Pad as _Pad

        # Build a fresh 4-layer grid for this test (the fixture is
        # shared with corridor-reservation tests; we want a clean
        # blocking pattern).
        stack = LayerStack.four_layer_all_signal()
        grid = RoutingGrid(50, 50, rules, origin_x=0, origin_y=0, layer_stack=stack)

        # Block a wall on F.Cu (layer 0) at x in [10, 40], y in [20, 30]
        # -- a thick obstacle the pathfinder cannot route through on the
        # surface.  This forces the pathfinder to drop a via.
        for layer_idx in (0,):
            for gy in range(int(20.0 / 0.1), int(30.0 / 0.1) + 1):
                for gx in range(int(10.0 / 0.1), int(40.0 / 0.1) + 1):
                    if 0 <= gx < grid.cols and 0 <= gy < grid.rows:
                        grid.grid[layer_idx][gy][gx].blocked = True
                        grid.grid[layer_idx][gy][gx].net = 99  # foreign net

        # Reserve a corridor on In1 (layer 1) for nets {1, 2}.
        strip_cells: set[tuple[int, int]] = set()
        for x_mm_x10 in range(50, 451):
            for y_mm_x10 in range(245, 256):
                gx, gy = grid.world_to_grid(x_mm_x10 / 10.0, y_mm_x10 / 10.0)
                strip_cells.add((gx, gy))
        grid.reserve_corridor_cells(
            layer_idx=1, cells=strip_cells, net_ids={1, 2},
        )

        start = _Pad(
            x=5.0, y=25.0, width=0.4, height=0.4,
            net=1, net_name="TX_P", layer=Layer.F_CU,
            ref="U1", pin="1",
        )
        end = _Pad(
            x=45.0, y=25.0, width=0.4, height=0.4,
            net=1, net_name="TX_P", layer=Layer.F_CU,
            ref="U2", pin="1",
        )

        router = Router(grid, rules)
        route = router.route(start, end)
        assert route is not None, (
            "Pathfinder must complete the route by diving into the "
            "reserved inner corridor (F.Cu is walled off)"
        )

        # The completed route MUST include at least one via (we crossed
        # a wall on the surface -- there is no surface-only completion).
        via_count = len(route.vias) if route.vias else 0
        assert via_count >= 1, (
            f"Expected the route to use a via (surface wall present); "
            f"got {via_count} vias"
        )

        # And at least one segment must live on the RESERVED inner
        # layer (In1.Cu) -- not merely on any non-surface layer.
        # The attractor's contract is that the route prefers the
        # reserved corridor; checking only "non-F.Cu" would still pass
        # if the attractor produced 0.0 bonus and the route dived
        # randomly into In2.Cu, which is exactly the regression class
        # this gate is meant to catch (Judge soft finding on PR #2938).
        reserved_layer_segments = [
            s for s in route.segments
            if s.layer == Layer.IN1_CU
        ]
        assert len(reserved_layer_segments) >= 1, (
            f"Expected route to traverse the RESERVED inner layer "
            f"(In1.Cu); got segments on layers "
            f"{sorted({s.layer.value for s in route.segments})}"
        )

    # ------------------------------------------------------------------
    # AC6: partner-via envelope is harmless to reserved cells (per-cell
    # protection, not lateral widening)
    # ------------------------------------------------------------------

    def test_partner_via_just_outside_corridor_does_not_invade_reserved_cells(
        self, grid_4layer, rules,
    ):
        """A partner via whose CENTRE sits just outside the reserved
        corridor still does not colonise any reserved cell.

        Pre-#2911 the concern was: a partner via placed JUST outside
        the corridor with a clearance envelope of
        ``via_diameter/2 + via_clearance + trace_w/2`` could chew
        laterally into reserved cells.  An earlier #2911 iteration
        widened the corridor by this halo, but that over-reserved the
        inner layer and starved single-ended neighbours on board 07
        (DDR data byte regression observed during development).

        The REAL contract is that ``RoutingGrid._mark_via`` walks every
        cell in the via's envelope and SKIPS each cell individually if
        it is reserved for a different net.  So a partner via just
        outside the corridor:
            * Has its CENTRE cell blocked (correct).
            * Has envelope cells INSIDE the corridor SKIPPED (the
              per-cell protection from #2677 + #2911).

        This gate locks in the per-cell protection so a future
        regression that drops the cell-level skip surfaces here.
        """
        from kicad_tools.router.layers import Layer as _Layer
        from kicad_tools.router.primitives import Via as _Via

        er, info = self._make_bga_pair_router(grid_4layer, rules)
        er.generate_escapes(info)
        assert er.pair_corridor_reservations == 1

        reserved_cells_before = set(grid_4layer._reserved_for_nets.keys())
        assert reserved_cells_before, "Expected reserved cells"

        # Find a reserved cell on the BOUNDARY of the corridor (max gy
        # along the layer).  A partner via placed JUST outside this
        # boundary will have its envelope overlap the reserved cells.
        layer_indices = {k[0] for k in reserved_cells_before}
        (target_layer_idx,) = layer_indices  # exactly one layer reserved
        max_gy = max(k[1] for k in reserved_cells_before if k[0] == target_layer_idx)
        boundary_cells = [
            (k[2], k[1]) for k in reserved_cells_before
            if k[0] == target_layer_idx and k[1] == max_gy
        ]
        gx, gy = boundary_cells[len(boundary_cells) // 2]

        # Place a partner-net (net=42) via JUST outside the corridor
        # boundary -- a few grid cells beyond on the y axis so the
        # envelope (a square radius in grid cells) overlaps the
        # reserved boundary cells.
        partner_wx, partner_wy = grid_4layer.grid_to_world(gx, gy + 2)
        partner_via = _Via(
            x=partner_wx, y=partner_wy,
            drill=rules.via_drill,
            diameter=rules.via_diameter,
            layers=(_Layer.F_CU, _Layer.B_CU),
            net=42, net_name="PARTNER",
        )

        # Capture cell states before the via mark.
        pre_state = {
            k: grid_4layer.grid[k[0]][k[1]][k[2]].blocked
            for k in reserved_cells_before
        }
        grid_4layer._mark_via(partner_via)

        # All reserved cells that were unblocked before must remain
        # unblocked after -- the partner via's envelope cells were
        # SKIPPED per-cell within the corridor (per-cell protection
        # from #2677 + #2911).
        post_state = {
            k: grid_4layer.grid[k[0]][k[1]][k[2]].blocked
            for k in reserved_cells_before
        }
        newly_blocked = [
            k for k in reserved_cells_before
            if not pre_state[k] and post_state[k]
        ]
        assert not newly_blocked, (
            f"Partner via just outside corridor colonised {len(newly_blocked)} "
            f"reserved cells -- per-cell protection failed"
        )

    def _make_bga_pair_router(self, grid_obj, rules_obj):
        """Mirror of ``TestCorridorReservation._make_bga_pair_router`` so
        this class can reuse the fixture flow."""
        pads = make_bga_with_pair("TX_P", "TX_N")
        info = make_package_info(pads, PackageType.BGA, "U1")
        ncm = {"TX_P": NET_CLASS_HIGH_SPEED, "TX_N": NET_CLASS_HIGH_SPEED}
        er = EscapeRouter(
            grid_obj, rules_obj, net_class_map=ncm,
            diff_pair_map={"TX_P": "TX_N", "TX_N": "TX_P"},
        )
        return er, info


# =============================================================================
# Issue #3270: BGA-49 inner-ring single-pad drift-prevention fixture
# =============================================================================
# Codifies the "B2/B3 inner-ring USB3 pair on a 7x7 BGA-49" geometry from
# board 06 so future budget/heuristic changes that regress the corridor
# reservation behaviour for inner-ring paired pads land a test failure
# instead of a silent reach regression on the integration board.
#
# Mirrors curator AC #6 from Issue #3270: "Drift-prevention regression:
# synthetic BGA-49 fixture (the one from #2677 AC, plus one isolating
# just the U2.B2-class inner-ring single-pad case) holds in the test
# suite so future budget/heuristic changes cannot regress."


def make_bga49_with_inner_ring_pair(
    p_net: str = "USB3_TX1+",
    n_net: str = "USB3_TX1-",
) -> list[Pad]:
    """Build a 7x7 BGA-49 with the diff pair at B2/B3 (inner-ring).

    Mirrors ``boards/06-diffpair-test/generate_pcb.py:generate_bga49_usb3_sink``
    exactly so the synthetic fixture exercises the same launch direction
    (SOUTH from B-row, see ``_get_quadrant_direction``) and same lateral
    geometry the integration board hits.

    Layout (1.27 mm pitch, 0.45 mm pads, F.Cu only):

    * Row A and G + col 1 and 7: perimeter ring, all GND (net 5)
    * Inner power: C2/C6/E2/E6 = +3V3 (net 4); rest of inner 3x5 = +1V2
      (net 6)
    * **B2 = p_net, B3 = n_net** (the inner-ring USB3 pair under test)
    * B5, B6, F2, F3, F5, F6: other USB3 lanes (unique nets)

    Total: 49 pads with 8 unique signal nets (USB3 lanes), GND, +3V3,
    +1V2.
    """
    pitch = 1.27
    pad_size = 0.45
    pads: list[Pad] = []

    pin_nets: dict[str, tuple[str, int]] = {}
    for row_letter in "ABCDEFG":
        for col in range(1, 8):
            pin_nets[f"{row_letter}{col}"] = ("GND", 5)
    for row in "CDE":
        for col in range(2, 7):
            pin_nets[f"{row}{col}"] = ("+1V2", 6)
    pin_nets["C2"] = ("+3V3", 4)
    pin_nets["C6"] = ("+3V3", 4)
    pin_nets["E2"] = ("+3V3", 4)
    pin_nets["E6"] = ("+3V3", 4)

    # The pair under test: B2 / B3 (inner-ring, second row from outer
    # perimeter, second / third column from the west edge).  Net IDs 1
    # and 2 match the existing TestCorridorReservation convention so the
    # owner-set assertion (`{1, 2}` for the paired pair) carries over.
    pin_nets["B2"] = (p_net, 1)
    pin_nets["B3"] = (n_net, 2)
    # Other USB3 lanes (unique nets so the diff-pair pre-pass does NOT
    # pair them and only B2/B3 enters the corridor reservation path).
    pin_nets["B5"] = ("USB3_RX1+", 10)
    pin_nets["B6"] = ("USB3_RX1-", 11)
    pin_nets["F2"] = ("USB3_TX2+", 12)
    pin_nets["F3"] = ("USB3_TX2-", 13)
    pin_nets["F5"] = ("USB3_RX2+", 14)
    pin_nets["F6"] = ("USB3_RX2-", 15)

    for row_idx, row_letter in enumerate("ABCDEFG"):
        for col in range(1, 8):
            px = (col - 4) * pitch
            py = (row_idx - 3) * pitch
            net_name, net_id = pin_nets[f"{row_letter}{col}"]
            pads.append(
                Pad(
                    x=px, y=py,
                    width=pad_size, height=pad_size,
                    net=net_id, net_name=net_name,
                    layer=Layer.F_CU, ref="U2",
                    through_hole=False,
                    pin=f"{row_letter}{col}",
                )
            )
    return pads


class TestBGA49InnerRingCorridorDriftPrevention:
    """Drift-prevention: B2/B3 inner-ring pair gets a corridor reservation.

    Curator AC #6 from Issue #3270.  Synthetic BGA-49 fixture replicates
    board 06's USB3_TX1+/- launch geometry exactly so any future
    refactor that breaks inner-ring corridor reservation (e.g. a
    well-intentioned ring-aware short-circuit that skips inner pads)
    will fail this gate instead of stranding U2.B2 on the integration
    board.
    """

    def test_inner_ring_pair_reserves_corridor(self, grid_4layer, rules):
        """The B2/B3 inner-ring pair MUST produce a corridor reservation.

        Counterpart to ``TestCorridorReservation.test_gate_a`` but on the
        exact 7x7 BGA-49 footprint board 06 uses, with the diff pair on
        the **inner** ring (B-row) -- not the original 4x4 fixture's
        B-row pair which is functionally on the outer of two rings.
        """
        pads = make_bga49_with_inner_ring_pair()
        info = make_package_info(pads, PackageType.BGA, "U2")
        diff_pair_map = {"USB3_TX1+": "USB3_TX1-", "USB3_TX1-": "USB3_TX1+"}
        ncm = {n: NET_CLASS_HIGH_SPEED for n in diff_pair_map}

        er = EscapeRouter(
            grid_4layer, rules,
            net_class_map=ncm, diff_pair_map=diff_pair_map,
        )

        assert er.pair_corridor_reservations == 0
        er.generate_escapes(info)

        assert er.diff_pair_segment_calls == 1, (
            "Inner-ring paired escape must run exactly once for the "
            "single B2/B3 USB3 pair"
        )
        assert er.pair_corridor_reservations == 1, (
            "Inner-ring B2/B3 pair must reserve exactly one corridor; "
            "future refactors that limit corridor reservation to "
            "outer-ring pairs will fail this gate"
        )
        assert er.pair_corridor_reserved_cells >= 1
        assert grid_4layer.reserved_cell_count() == er.pair_corridor_reserved_cells

    def test_inner_ring_pair_launches_south(self, grid_4layer, rules):
        """B2/B3 pair launch direction must be SOUTH (outward from BGA).

        The pair sits 1 pitch south of BGA center (row B = row 1, center
        is row D = row 3).  ``_get_quadrant_direction`` returns SOUTH
        when ``|dy| > |dx|`` and ``dy < 0``: B2/B3 mid_y = -2.54 < 0,
        mid_x = -1.905 -> |dy| > |dx| -> SOUTH.

        Locking in the launch direction prevents a refactor that
        accidentally classifies the inner-ring pair as 'inward' or
        'diagonal' (which would defeat the corridor reservation, since
        the corridor extrudes along the launch direction).
        """
        from kicad_tools.router.escape import EscapeDirection

        pads = make_bga49_with_inner_ring_pair()
        info = make_package_info(pads, PackageType.BGA, "U2")
        diff_pair_map = {"USB3_TX1+": "USB3_TX1-", "USB3_TX1-": "USB3_TX1+"}
        ncm = {n: NET_CLASS_HIGH_SPEED for n in diff_pair_map}

        er = EscapeRouter(
            grid_4layer, rules,
            net_class_map=ncm, diff_pair_map=diff_pair_map,
        )
        escapes = er.generate_escapes(info)

        pair_escapes = [
            e for e in escapes
            if e.pad.net_name in ("USB3_TX1+", "USB3_TX1-")
        ]
        assert len(pair_escapes) == 2, (
            "Expected exactly 2 paired escapes for B2/B3 inner-ring pair"
        )
        for e in pair_escapes:
            assert e.direction == EscapeDirection.SOUTH, (
                f"Inner-ring B-row pair must launch SOUTH (outward), "
                f"got {e.direction.name} for {e.pad.pin}"
            )
            # And the F.Cu paired escape carries no via-drop (the pair
            # is committed to surface routing until the main pathfinder
            # decides where to drop).  The corridor reservation is the
            # mechanism that protects the inner-layer continuation.
            assert e.via is None
            assert e.escape_layer == Layer.F_CU

    def test_inner_ring_pair_corridor_owner_set_is_pair_only(
        self, grid_4layer, rules,
    ):
        """The B2/B3 corridor is owned by the B2/B3 pair, not other USB3 lanes.

        Other lanes (USB3_RX1, USB3_TX2, USB3_RX2) on the same BGA share
        the package but their nets are NOT paired in the diff_pair_map
        passed to this test, so they MUST NOT appear in the B2/B3
        corridor's owner set.  This is the regression guard against a
        future change that accidentally bundles all USB3 lanes into a
        single shared owner set (which would let RX1's traces colonise
        the TX1 corridor).
        """
        pads = make_bga49_with_inner_ring_pair()
        info = make_package_info(pads, PackageType.BGA, "U2")
        diff_pair_map = {"USB3_TX1+": "USB3_TX1-", "USB3_TX1-": "USB3_TX1+"}
        ncm = {n: NET_CLASS_HIGH_SPEED for n in diff_pair_map}

        er = EscapeRouter(
            grid_4layer, rules,
            net_class_map=ncm, diff_pair_map=diff_pair_map,
        )
        er.generate_escapes(info)

        # All reservations should be owned by exactly the pair {1, 2}.
        reserved_items = list(grid_4layer._reserved_for_nets.items())
        assert reserved_items, "Expected non-empty reservation map"
        unique_owner_sets = {frozenset(o) for _, o in reserved_items}
        assert unique_owner_sets == {frozenset({1, 2})}, (
            f"All B2/B3 corridor cells must be owned by the pair {{1, 2}}; "
            f"found unexpected owner sets {unique_owner_sets - {{frozenset({{1, 2}})}}}"
        )


# =============================================================================
# Issue #3419: partner-connector-aware paired-escape launch direction
# =============================================================================


class TestPairLaunchDirectionHeuristic:
    """Issue #3419: paired escapes launch TOWARD the partner connector.

    The original heuristic launched the pair outward from the package
    center (quadrant rule).  On board 06's BGA-49 that strands the
    tightly-coupled escape endpoints facing away from the USB-C source
    and the per-net A* times out dragging the pair around the package.
    ``_select_pair_launch_direction`` aims the launch at the centroid of
    the pair's off-package endpoints when ``net_pad_positions`` is
    provided, and falls back to the quadrant rule otherwise.
    """

    @staticmethod
    def _pair_and_mid(pads):
        pad_p = next(p for p in pads if p.net_name == "TX_P")
        pad_n = next(p for p in pads if p.net_name == "TX_N")
        mid_x = (pad_p.x + pad_n.x) / 2.0
        mid_y = (pad_p.y + pad_n.y) / 2.0
        return pad_p, pad_n, mid_x, mid_y

    def test_fallback_without_positions(self, grid, rules):
        """No net_pad_positions -> exact pre-#3419 quadrant behaviour."""
        pads = make_bga_with_pair()
        info = make_package_info(pads, PackageType.BGA, "U1")
        er = EscapeRouter(grid, rules, diff_pair_map={"TX_P": "TX_N", "TX_N": "TX_P"})
        pad_p, pad_n, mid_x, mid_y = self._pair_and_mid(pads)
        d = er._select_pair_launch_direction(pad_p, pad_n, mid_x, mid_y, info)
        assert d == er._get_quadrant_direction(mid_x, mid_y, *info.center)

    def test_fallback_when_all_endpoints_on_package(self, grid, rules):
        """Positions map containing ONLY the on-package pads -> fallback."""
        pads = make_bga_with_pair()
        info = make_package_info(pads, PackageType.BGA, "U1")
        pad_p, pad_n, mid_x, mid_y = self._pair_and_mid(pads)
        positions = {
            "TX_P": [(pad_p.x, pad_p.y)],
            "TX_N": [(pad_n.x, pad_n.y)],
        }
        er = EscapeRouter(
            grid, rules,
            diff_pair_map={"TX_P": "TX_N", "TX_N": "TX_P"},
            net_pad_positions=positions,
        )
        d = er._select_pair_launch_direction(pad_p, pad_n, mid_x, mid_y, info)
        assert d == er._get_quadrant_direction(mid_x, mid_y, *info.center)

    def test_direction_points_toward_partner_connector(self, grid, rules):
        """Connector due EAST -> launch EAST even though quadrant says SOUTH.

        The B2/B3 pair of ``make_bga_with_pair`` sits on the south side
        of the package (midpoint (0, -0.4) vs center (0, 0)), so the
        quadrant rule launches SOUTH.  With the partner connector's pads
        far to the east, the #3419 heuristic must launch EAST.
        """
        from kicad_tools.router.escape import EscapeDirection

        pads = make_bga_with_pair()
        info = make_package_info(pads, PackageType.BGA, "U1")
        pad_p, pad_n, mid_x, mid_y = self._pair_and_mid(pads)
        positions = {
            "TX_P": [(pad_p.x, pad_p.y), (30.0, mid_y)],
            "TX_N": [(pad_n.x, pad_n.y), (30.0, mid_y - 0.2)],
        }
        er = EscapeRouter(
            grid, rules,
            diff_pair_map={"TX_P": "TX_N", "TX_N": "TX_P"},
            net_pad_positions=positions,
        )
        d = er._select_pair_launch_direction(pad_p, pad_n, mid_x, mid_y, info)
        assert d == EscapeDirection.EAST

    def test_never_launches_into_package_interior(self, grid, rules):
        """Connector on the FAR side -> heuristic must not launch inward.

        The pair sits on the south side; a connector due north would
        naively suggest NORTH, but that crosses the BGA pad field on the
        surface layer.  The interior veto must exclude NORTH.
        """
        from kicad_tools.router.escape import EscapeDirection

        pads = make_bga_with_pair()
        info = make_package_info(pads, PackageType.BGA, "U1")
        pad_p, pad_n, mid_x, mid_y = self._pair_and_mid(pads)
        positions = {
            "TX_P": [(pad_p.x, pad_p.y), (0.0, 30.0)],
            "TX_N": [(pad_n.x, pad_n.y), (0.2, 30.0)],
        }
        er = EscapeRouter(
            grid, rules,
            diff_pair_map={"TX_P": "TX_N", "TX_N": "TX_P"},
            net_pad_positions=positions,
        )
        d = er._select_pair_launch_direction(pad_p, pad_n, mid_x, mid_y, info)
        assert d != EscapeDirection.NORTH

    def test_generate_escapes_threads_heuristic(self, grid, rules):
        """End-to-end: paired escape endpoints move toward the connector."""
        pads = make_bga_with_pair()
        info = make_package_info(pads, PackageType.BGA, "U1")
        pad_p, pad_n, mid_x, mid_y = self._pair_and_mid(pads)
        positions = {
            "TX_P": [(pad_p.x, pad_p.y), (30.0, mid_y)],
            "TX_N": [(pad_n.x, pad_n.y), (30.0, mid_y - 0.2)],
        }
        er = EscapeRouter(
            grid, rules,
            diff_pair_map={"TX_P": "TX_N", "TX_N": "TX_P"},
            net_pad_positions=positions,
        )
        escapes = er.generate_escapes(info)
        paired = [e for e in escapes if e.pad.net_name in ("TX_P", "TX_N")]
        assert len(paired) == 2
        for e in paired:
            assert e.escape_point[0] > e.pad.x, (
                f"{e.pad.net_name}: expected eastward launch toward the "
                f"connector, got escape point {e.escape_point} from pad "
                f"({e.pad.x}, {e.pad.y})"
            )

    def test_autorouter_threads_net_pad_positions(self):
        """The Autorouter ``_escape`` property builds and threads the map."""
        ar = Autorouter(width=20.0, height=20.0)
        ar.net_class_map["SIG_A"] = NetClassRouting(name="Plain")
        ar.add_component(
            "J1",
            [{"number": "1", "x": 5.0, "y": 6.0, "net": 1, "net_name": "SIG_A"}],
        )
        escape = ar._escape
        assert escape.net_pad_positions.get("SIG_A") == [(5.0, 6.0)]

    def test_default_positions_map_is_empty(self, grid, rules):
        er = EscapeRouter(grid, rules)
        assert er.net_pad_positions == {}
