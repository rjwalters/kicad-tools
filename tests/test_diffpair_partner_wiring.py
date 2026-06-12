"""Tests for diff-pair partner-net activation wiring.

Issue #2587 / Epic #2556 Phase 1C-cont: This file exercises the activation
path that PR #2586 (Phase 1C) left dormant.  Specifically:

1. ``CppPathfinder.set_net_name_to_id`` / ``_resolve_partner_net_id``
   mirror the Python ``Router`` API.
2. ``Autorouter._prepare_routing()`` populates the reverse map on the
   underlying pathfinder before routing begins.
3. Diff-pair detection runs during ``_prepare_routing()`` and mutates
   per-net ``NetClassRouting`` copies (NEVER the shared singletons).
4. ``find_blocking_nets`` (both backends) excludes the diff-pair partner
   from the rip-up candidate set.
5. Backward-compatibility: when no diff pair is present, behavior is
   bit-for-bit identical to pre-#2559.

These tests do not depend on the C++ ``.so`` being current -- the
``CppPathfinder`` instantiation is gated by ``_CPP_AVAILABLE`` and skipped
gracefully when the binding mismatches the source tree.  The Python-side
wiring tests run unconditionally.
"""

from __future__ import annotations

import dataclasses

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import (
    _CPP_AVAILABLE,
    CppGrid,
    CppPathfinder,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import (
    NET_CLASS_HIGH_SPEED,
    DesignRules,
    NetClassRouting,
)

# =============================================================================
# Python pathfinder set_net_name_to_id / _resolve_partner_net_id
# =============================================================================


class TestPythonPathfinderPartnerResolution:
    """``Router.set_net_name_to_id`` and ``_resolve_partner_net_id``."""

    def _make_router(self, net_class_map=None):
        """Build a minimal Router with a small grid for unit testing."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)
        return Router(grid=grid, rules=rules, net_class_map=net_class_map or {})

    def test_set_net_name_to_id_stores_mapping(self):
        router = self._make_router()
        router.set_net_name_to_id({"USB_D+": 1, "USB_D-": 2})
        assert router._net_name_to_id == {"USB_D+": 1, "USB_D-": 2}

    def test_set_net_name_to_id_is_idempotent(self):
        router = self._make_router()
        router.set_net_name_to_id({"A": 1})
        router.set_net_name_to_id({"A": 1, "B": 2})
        assert router._net_name_to_id == {"A": 1, "B": 2}

    def test_set_net_name_to_id_empty_disables_partner(self):
        router = self._make_router()
        router.set_net_name_to_id({"A": 1})
        router.set_net_name_to_id({})
        assert router._net_name_to_id == {}

    def test_set_net_name_to_id_takes_a_copy(self):
        router = self._make_router()
        src = {"A": 1}
        router.set_net_name_to_id(src)
        src["B"] = 2  # outside mutation should not leak in
        assert router._net_name_to_id == {"A": 1}

    def test_resolve_partner_returns_none_when_no_class(self):
        router = self._make_router(net_class_map={})
        router.set_net_name_to_id({"X": 1, "Y": 2})
        assert router._resolve_partner_net_id("X") is None

    def test_resolve_partner_returns_none_when_no_partner(self):
        nc = NetClassRouting(name="Plain")  # no diffpair_partner
        router = self._make_router(net_class_map={"X": nc})
        router.set_net_name_to_id({"X": 1, "Y": 2})
        assert router._resolve_partner_net_id("X") is None

    def test_resolve_partner_returns_none_when_partner_unknown(self):
        nc = NetClassRouting(name="Plain", diffpair_partner="MISSING")
        router = self._make_router(net_class_map={"X": nc})
        router.set_net_name_to_id({"X": 1, "Y": 2})  # MISSING not in map
        assert router._resolve_partner_net_id("X") is None

    def test_resolve_partner_returns_id_when_resolvable(self):
        nc = NetClassRouting(name="Diff", diffpair_partner="Y")
        router = self._make_router(net_class_map={"X": nc})
        router.set_net_name_to_id({"X": 1, "Y": 2})
        assert router._resolve_partner_net_id("X") == 2

    def test_resolve_partner_returns_id_for_both_halves(self):
        """When both halves declare each other, both resolve correctly."""
        x_class = NetClassRouting(name="Diff", diffpair_partner="Y")
        y_class = NetClassRouting(name="Diff", diffpair_partner="X")
        router = self._make_router(net_class_map={"X": x_class, "Y": y_class})
        router.set_net_name_to_id({"X": 1, "Y": 2})
        assert router._resolve_partner_net_id("X") == 2
        assert router._resolve_partner_net_id("Y") == 1


# =============================================================================
# CppPathfinder set_net_name_to_id / _resolve_partner_net_id
# =============================================================================


@pytest.mark.skipif(not _CPP_AVAILABLE, reason="C++ router backend unavailable")
class TestCppPathfinderPartnerResolution:
    """Mirror of the Python tests for ``CppPathfinder``."""

    def _make_cpp_pathfinder(self, net_class_map=None):
        rules = DesignRules()
        py_grid = RoutingGrid(10.0, 10.0, rules)
        cpp_grid = CppGrid.from_routing_grid(py_grid)
        return CppPathfinder(
            grid=cpp_grid,
            rules=rules,
            net_class_map=net_class_map or {},
        )

    def test_setter_method_exists(self):
        pf = self._make_cpp_pathfinder()
        assert hasattr(pf, "set_net_name_to_id")
        assert hasattr(pf, "_resolve_partner_net_id")

    def test_set_net_name_to_id_stores_mapping(self):
        pf = self._make_cpp_pathfinder()
        pf.set_net_name_to_id({"USB_D+": 1, "USB_D-": 2})
        assert pf._net_name_to_id == {"USB_D+": 1, "USB_D-": 2}

    def test_set_net_name_to_id_is_idempotent(self):
        pf = self._make_cpp_pathfinder()
        pf.set_net_name_to_id({"A": 1})
        pf.set_net_name_to_id({"A": 1, "B": 2})
        assert pf._net_name_to_id == {"A": 1, "B": 2}

    def test_set_net_name_to_id_empty_disables_partner(self):
        pf = self._make_cpp_pathfinder()
        pf.set_net_name_to_id({"A": 1})
        pf.set_net_name_to_id({})
        assert pf._net_name_to_id == {}

    def test_resolve_partner_returns_none_when_no_class(self):
        pf = self._make_cpp_pathfinder(net_class_map={})
        pf.set_net_name_to_id({"X": 1, "Y": 2})
        assert pf._resolve_partner_net_id("X") is None

    def test_resolve_partner_returns_none_when_no_partner(self):
        nc = NetClassRouting(name="Plain")
        pf = self._make_cpp_pathfinder(net_class_map={"X": nc})
        pf.set_net_name_to_id({"X": 1, "Y": 2})
        assert pf._resolve_partner_net_id("X") is None

    def test_resolve_partner_returns_none_when_partner_unknown(self):
        nc = NetClassRouting(name="Plain", diffpair_partner="MISSING")
        pf = self._make_cpp_pathfinder(net_class_map={"X": nc})
        pf.set_net_name_to_id({"X": 1, "Y": 2})
        assert pf._resolve_partner_net_id("X") is None

    def test_resolve_partner_returns_id_when_resolvable(self):
        nc = NetClassRouting(name="Diff", diffpair_partner="Y")
        pf = self._make_cpp_pathfinder(net_class_map={"X": nc})
        pf.set_net_name_to_id({"X": 1, "Y": 2})
        assert pf._resolve_partner_net_id("X") == 2

    def test_backward_compat_default_is_empty(self):
        """A freshly-constructed CppPathfinder has no partner map."""
        pf = self._make_cpp_pathfinder()
        assert pf._net_name_to_id == {}
        assert pf._resolve_partner_net_id("anything") is None


# =============================================================================
# Autorouter._prepare_routing
# =============================================================================


class TestAutorouterPrepareRouting:
    """``Autorouter._prepare_routing`` builds the reverse map and propagates
    diff-pair declarations from suffix detection."""

    def _build_autorouter_with_usb_pair(self):
        """Create an autorouter, add USB_D+ and USB_D- pads with the
        ``HighSpeed`` net class, and return it ready for routing.

        The pads share a component (``J1``) but are on different pins so
        they are recognized as distinct nets by ``add_component``.
        """
        ar = Autorouter(width=20.0, height=20.0)
        # Manually register the high-speed nets without going through
        # add_component so the test stays focused on the wiring path.
        ar.nets[1] = [("J1", "1")]
        ar.nets[2] = [("J1", "2")]
        ar.net_names[1] = "USB_D+"
        ar.net_names[2] = "USB_D-"
        ar.net_class_map["USB_D+"] = NET_CLASS_HIGH_SPEED
        ar.net_class_map["USB_D-"] = NET_CLASS_HIGH_SPEED
        return ar

    def test_prepare_routing_populates_reverse_map(self):
        ar = self._build_autorouter_with_usb_pair()
        ar._prepare_routing()
        assert hasattr(ar.router, "_net_name_to_id")
        assert ar.router._net_name_to_id == {"USB_D+": 1, "USB_D-": 2}

    def test_prepare_routing_sets_diffpair_partner_on_per_net_copies(self):
        """Suffix detection finds USB_D+/USB_D- and sets diffpair_partner
        on a per-net copy so the shared NET_CLASS_HIGH_SPEED singleton is
        NOT mutated cross-call.
        """
        # Sanity check: predefined singleton has no partner initially
        assert NET_CLASS_HIGH_SPEED.diffpair_partner is None

        ar = self._build_autorouter_with_usb_pair()
        # Before _prepare_routing: both nets reference the same singleton
        assert ar.net_class_map["USB_D+"] is NET_CLASS_HIGH_SPEED
        assert ar.net_class_map["USB_D-"] is NET_CLASS_HIGH_SPEED

        ar._prepare_routing()

        # After _prepare_routing: each net has its OWN NetClassRouting
        # with the partner set; the shared singleton is unchanged.
        assert NET_CLASS_HIGH_SPEED.diffpair_partner is None
        assert ar.net_class_map["USB_D+"] is not NET_CLASS_HIGH_SPEED
        assert ar.net_class_map["USB_D-"] is not NET_CLASS_HIGH_SPEED
        # Naming convention from diff-pair detection: positive (+) gets
        # the negative (-) as its partner.
        assert ar.net_class_map["USB_D+"].diffpair_partner == "USB_D-"
        assert ar.net_class_map["USB_D-"].diffpair_partner == "USB_D+"
        # The intra_pair_clearance is preserved through dataclasses.replace.
        assert ar.net_class_map["USB_D+"].intra_pair_clearance == 0.075

    def test_prepare_routing_resolves_partner_through_router(self):
        """After _prepare_routing, the router can resolve partners by name."""
        ar = self._build_autorouter_with_usb_pair()
        ar._prepare_routing()
        # The router (whether Python or C++) now has both the map AND the
        # per-net class with diffpair_partner set, so the partner-id
        # resolution returns a real integer.
        assert ar.router._resolve_partner_net_id("USB_D+") == 2
        assert ar.router._resolve_partner_net_id("USB_D-") == 1

    def test_prepare_routing_idempotent(self):
        """Multiple calls to _prepare_routing leave net_class_map stable."""
        ar = self._build_autorouter_with_usb_pair()
        ar._prepare_routing()
        ar._prepare_routing()
        second_plus = ar.net_class_map["USB_D+"]
        # Either the same dataclass-replaced object is reused OR an
        # equivalent new one is produced; either way the partner stays
        # set and the singleton remains untouched.
        assert second_plus.diffpair_partner == "USB_D-"
        assert NET_CLASS_HIGH_SPEED.diffpair_partner is None

    def test_prepare_routing_noop_when_no_pairs(self):
        """With no diff-pair nets, _prepare_routing still builds the
        reverse map but leaves net_class_map untouched."""
        ar = Autorouter(width=20.0, height=20.0)
        ar.nets[1] = [("J1", "1")]
        ar.net_names[1] = "SIG_A"
        ar.net_class_map["SIG_A"] = NetClassRouting(name="Plain")

        original_class = ar.net_class_map["SIG_A"]
        ar._prepare_routing()

        # Reverse map populated
        assert ar.router._net_name_to_id == {"SIG_A": 1}
        # Net class untouched (still the original instance, no partner)
        assert ar.net_class_map["SIG_A"] is original_class
        assert ar.net_class_map["SIG_A"].diffpair_partner is None
        # Partner resolution returns None (the dormant signal)
        assert ar.router._resolve_partner_net_id("SIG_A") is None

    def test_prepare_routing_skips_empty_net_names(self):
        """Net id 0 with empty name does not pollute the reverse map."""
        ar = Autorouter(width=20.0, height=20.0)
        ar.net_names[0] = ""
        ar.net_names[1] = "REAL_NET"
        ar.nets[1] = [("U1", "1")]
        ar.net_class_map["REAL_NET"] = NetClassRouting(name="Plain")
        ar._prepare_routing()
        assert "" not in ar.router._net_name_to_id
        assert ar.router._net_name_to_id == {"REAL_NET": 1}

    def test_prepare_routing_partner_annotation_does_not_pollute_class(self):
        """Issue #3455 regression: a board-recipe partner annotation on
        USB_D+ (net-name-keyed, shared class 'HighSpeed') must not fan
        out to USB_CC1/USB_CC2 through the class-name-keyed synth lookup,
        and must not clobber USB_D-'s true partner.
        """
        ar = Autorouter(width=20.0, height=20.0)
        for nid, name in enumerate(["USB_D+", "USB_D-", "USB_CC1", "USB_CC2"], start=1):
            ar.nets[nid] = [("J1", str(nid))]
            ar.net_names[nid] = name
            ar.net_class_map[name] = NET_CLASS_HIGH_SPEED

        # Board-03-recipe style annotation: per-net copy for USB_D+.
        ar.net_class_map["USB_D+"] = dataclasses.replace(
            NET_CLASS_HIGH_SPEED, diffpair_partner="USB_D-"
        )

        ar._prepare_routing()

        assert ar.net_class_map["USB_D+"].diffpair_partner == "USB_D-"
        assert ar.net_class_map["USB_D-"].diffpair_partner == "USB_D+"
        # CC nets are single-ended configuration channels -- no partner,
        # and no relaxed intra-pair clearance applied between unrelated
        # nets.
        assert ar.net_class_map["USB_CC1"].diffpair_partner is None
        assert ar.net_class_map["USB_CC2"].diffpair_partner is None
        # Shared singleton untouched.
        assert NET_CLASS_HIGH_SPEED.diffpair_partner is None


# =============================================================================
# find_blocking_nets partner exclusion
# =============================================================================


class TestFindBlockingNetsPartnerExclusion:
    """Both backends exclude the diff-pair partner from blocker set."""

    def _make_router_with_pair(self):
        """Create a Python Router whose source net is paired with net 2."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)
        x_class = NetClassRouting(name="Diff", diffpair_partner="Y")
        router = Router(grid=grid, rules=rules, net_class_map={"X": x_class})
        router.set_net_name_to_id({"X": 1, "Y": 2})
        return router, grid

    def _make_pad(self, x: float, y: float, net: int, net_name: str) -> Pad:
        return Pad(
            x=x,
            y=y,
            width=0.5,
            height=0.5,
            net=net,
            net_name=net_name,
            layer=Layer.F_CU,
            ref="R1" if net == 1 else "R2",
            pin="1",
        )

    def test_find_blocking_nets_excludes_partner_python(self):
        """A direct-path Bresenham scan that crosses a partner trace
        does NOT report the partner net id as a blocker."""
        router, grid = self._make_router_with_pair()

        # Manually mark a cell on the direct path as blocked by net 2 (the
        # partner) and net 3 (an unrelated foreign net).  find_blocking_nets
        # should return {3} only, not {2, 3}.
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        mid_gx, mid_gy = grid.world_to_grid(5.0, 5.0)
        cell_partner = grid.grid[layer_idx][mid_gy][mid_gx]
        cell_partner.blocked = True
        cell_partner.net = 2
        cell_partner.usage_count = 1

        offset_gx, offset_gy = grid.world_to_grid(5.0, 6.0)
        cell_other = grid.grid[layer_idx][offset_gy][offset_gx]
        cell_other.blocked = True
        cell_other.net = 3
        cell_other.usage_count = 1

        # Construct pads at (1,5) and (9,5) so the Bresenham line passes
        # straight through (5,5).  The partner trace at (5,5) is excluded.
        start_pad = self._make_pad(1.0, 5.0, net=1, net_name="X")
        end_pad = self._make_pad(9.0, 5.0, net=1, net_name="X")

        blocking = router.find_blocking_nets(start_pad, end_pad)
        assert 2 not in blocking, "partner net should be excluded"
        # Net 3 may or may not be reached depending on the exact
        # Bresenham step; we only assert the partner exclusion contract.

    def test_find_blocking_nets_returns_partner_when_unpaired(self):
        """When no partner is configured, the same blocker IS reported.

        This is the dormant-signal contract: pre-Phase-1C behavior is
        preserved when ``_net_name_to_id`` is empty (or the source net
        has no ``diffpair_partner``).
        """
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)
        # No partner declared on the net class.
        router = Router(
            grid=grid,
            rules=rules,
            net_class_map={"X": NetClassRouting(name="Plain")},
        )
        # Empty map -- no partner can resolve.
        router.set_net_name_to_id({})

        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        mid_gx, mid_gy = grid.world_to_grid(5.0, 5.0)
        cell_blocker = grid.grid[layer_idx][mid_gy][mid_gx]
        cell_blocker.blocked = True
        cell_blocker.net = 2  # "partner" id, but not declared as such
        cell_blocker.usage_count = 1

        start_pad = self._make_pad(1.0, 5.0, net=1, net_name="X")
        end_pad = self._make_pad(9.0, 5.0, net=1, net_name="X")

        blocking = router.find_blocking_nets(start_pad, end_pad)
        assert 2 in blocking, "without partner declaration, net 2 is a regular blocker"


# =============================================================================
# Backward compatibility: dormant-signal contract
# =============================================================================


class TestDormantSignalBackwardCompat:
    """When no diff-pair declarations exist anywhere, behavior is
    bit-for-bit identical to pre-#2559 / pre-#2587."""

    def test_router_with_empty_map_has_no_partner(self):
        """The setter accepts an empty dict and partner resolution returns
        None for every net."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid=grid, rules=rules, net_class_map={})
        router.set_net_name_to_id({})
        assert router._resolve_partner_net_id("anything") is None

    def test_autorouter_route_all_safe_without_pairs(self):
        """``_prepare_routing`` does not raise when there are no nets."""
        ar = Autorouter(width=20.0, height=20.0)
        # No pads, no nets -- the helper should still run cleanly.
        ar._prepare_routing()
        assert ar.router._net_name_to_id == {}

    def test_high_speed_singleton_intact_after_detection(self):
        """The shared NET_CLASS_HIGH_SPEED singleton is NEVER mutated by
        the wiring -- only per-net copies receive the partner."""
        baseline_partner = NET_CLASS_HIGH_SPEED.diffpair_partner
        baseline_clearance = NET_CLASS_HIGH_SPEED.intra_pair_clearance

        ar = Autorouter(width=20.0, height=20.0)
        ar.nets[1] = [("J1", "1")]
        ar.nets[2] = [("J1", "2")]
        ar.net_names[1] = "USB_D+"
        ar.net_names[2] = "USB_D-"
        ar.net_class_map["USB_D+"] = NET_CLASS_HIGH_SPEED
        ar.net_class_map["USB_D-"] = NET_CLASS_HIGH_SPEED
        ar._prepare_routing()

        # Critical: the singleton must be untouched so it remains usable
        # by every other board.
        assert NET_CLASS_HIGH_SPEED.diffpair_partner == baseline_partner
        assert NET_CLASS_HIGH_SPEED.intra_pair_clearance == baseline_clearance


# =============================================================================
# Integration: route_all triggers _prepare_routing
# =============================================================================


class TestRouteAllTriggersPrepareRouting:
    """End-to-end check: ``route_all()`` actually calls ``_prepare_routing``
    so the partner map is populated when production code calls the router.
    """

    def test_route_all_populates_partner_map(self):
        ar = Autorouter(width=20.0, height=20.0)
        ar.nets[1] = [("J1", "1")]
        ar.nets[2] = [("J1", "2")]
        ar.net_names[1] = "USB_D+"
        ar.net_names[2] = "USB_D-"
        ar.net_class_map["USB_D+"] = NET_CLASS_HIGH_SPEED
        ar.net_class_map["USB_D-"] = NET_CLASS_HIGH_SPEED

        # No real pads added -> route_all should return [] without crashing
        # but still run _prepare_routing.
        ar.route_all()
        assert ar.router._net_name_to_id == {"USB_D+": 1, "USB_D-": 2}
        assert ar.net_class_map["USB_D+"].diffpair_partner == "USB_D-"
