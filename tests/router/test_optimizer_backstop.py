"""Tests for the post-optimize/post-nudge re-validation backstop (Issue #4208 / Unit 3).

M4 mechanism (``src/kicad_tools/router/optimizer/collision.py``): the
``GridCollisionChecker`` fallback's ``ignore_overflow`` branch permits a
route through an already-overused foreign cell, whereas the rtree-backed
``VectorCollisionChecker`` does an unconditional exact seg-seg check that
does not.  So in an **rtree-less** environment the trace optimizer can
introduce a cross-net crossing AFTER the negotiated finalize demote
(Unit 1/2) has already run -- the pre-optimize gate never saw it.

Unit 3 adds a backstop in ``route_cmd.py`` that re-runs the Unit-2 seg-seg
finalize gate over the CURRENT committed copper (reconstructed from
``router.routes`` via ``_build_net_routes_map``), demoting any net whose
copper became a short.  These tests exercise the backstop directly by
simulating the crossing the rtree-less optimizer would have introduced,
rather than relying on a full route pipeline to land on the fragile
overflow-tolerant path.
"""

from __future__ import annotations

from kicad_tools.cli.route_cmd import _finalize_committed_copper_or_demote
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules


def _make_rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


def _make_autorouter() -> Autorouter:
    return Autorouter(
        width=20.0,
        height=20.0,
        origin_x=0.0,
        origin_y=0.0,
        rules=_make_rules(),
        layer_stack=LayerStack.two_layer(),
    )


def _seg(x1, y1, x2, y2, net):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=net)


def _route(net, segs, name):
    return Route(net=net, net_name=name, segments=segs, vias=[])


def _commit(ar: Autorouter, route: Route) -> None:
    ar.grid.mark_route(route)
    ar.grid.mark_route_usage(route)
    ar.routes.append(route)


class TestRevalidateCommittedCopper:
    """Router-level backstop: ``revalidate_committed_copper_or_demote``."""

    def test_clean_board_is_noop(self):
        """No crossing -> no demotion (the common rtree-present case)."""
        ar = _make_autorouter()
        r1 = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)], "NETA")
        r2 = _route(2, [_seg(2.0, 10.0, 12.0, 10.0, 2)], "NETB")
        for r in (r1, r2):
            _commit(ar, r)

        assert ar.revalidate_committed_copper_or_demote() == []
        assert r1 in ar.routes and r2 in ar.routes

    def test_optimizer_introduced_crossing_is_demoted(self):
        """Simulate an rtree-less optimizer crossing: two committed nets
        end up physically overlapping on the same layer.  The backstop
        re-runs the finalize gate and demotes the greedy-cover victim."""
        ar = _make_autorouter()
        # Net 1 spans the board; the optimizer "straightened" net 2 across
        # it, landing net 2's copper on top of net 1 (physical overlap).
        r1 = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)], "NETA")
        r2 = _route(2, [_seg(4.0, 5.0, 10.0, 5.0, 2)], "NETB")
        r3 = _route(3, [_seg(2.0, 12.0, 12.0, 12.0, 3)], "CLEAN")
        for r in (r1, r2, r3):
            _commit(ar, r)

        demoted = ar.revalidate_committed_copper_or_demote()
        assert demoted == [1]  # greedy cover picks the hub (lowest id on tie)
        # Demoted net's copper is removed from routes; clean net untouched.
        assert r1 not in ar.routes
        assert r2 in ar.routes and r3 in ar.routes

    def test_sub_clearance_crossing_demoted_full_threshold(self):
        """A positive-gap-but-sub-clearance short (0.106mm < 0.15mm) is
        caught -- the backstop uses the FULL DRC threshold, not just
        polygon overlap, matching the Unit-2 finalize gate."""
        ar = _make_autorouter()
        r1 = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)], "NETA")
        # dy 0.306 -> edge gap 0.106mm: positive, but sub-clearance.
        r2 = _route(2, [_seg(2.0, 5.306, 12.0, 5.306, 2)], "NETB")
        for r in (r1, r2):
            _commit(ar, r)

        demoted = ar.revalidate_committed_copper_or_demote()
        assert len(demoted) == 1
        assert demoted[0] in (1, 2)

    def test_empty_routes_is_noop(self):
        ar = _make_autorouter()
        assert ar.revalidate_committed_copper_or_demote() == []


class TestCliBackstopHelper:
    """The shared ``route_cmd`` helper called from all four optimize+nudge
    call sites."""

    def test_helper_demotes_and_reports(self, capsys):
        ar = _make_autorouter()
        r1 = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)], "NETA")
        r2 = _route(2, [_seg(4.0, 5.0, 10.0, 5.0, 2)], "NETB")
        for r in (r1, r2):
            _commit(ar, r)

        _finalize_committed_copper_or_demote(ar, quiet=False)

        out = capsys.readouterr().out
        assert "Post-optimize backstop demoted" in out
        assert r1 not in ar.routes  # the short was demoted

    def test_helper_quiet_suppresses_output(self, capsys):
        ar = _make_autorouter()
        r1 = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)], "NETA")
        r2 = _route(2, [_seg(4.0, 5.0, 10.0, 5.0, 2)], "NETB")
        for r in (r1, r2):
            _commit(ar, r)

        _finalize_committed_copper_or_demote(ar, quiet=True)
        assert capsys.readouterr().out == ""

    def test_helper_clean_board_no_output(self, capsys):
        ar = _make_autorouter()
        r1 = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)], "NETA")
        r2 = _route(2, [_seg(2.0, 10.0, 12.0, 10.0, 2)], "NETB")
        for r in (r1, r2):
            _commit(ar, r)

        _finalize_committed_copper_or_demote(ar, quiet=False)
        # No demotion -> no backstop message.
        assert "Post-optimize backstop demoted" not in capsys.readouterr().out
        assert r1 in ar.routes and r2 in ar.routes


class TestCoupledDiffPairExemption:
    """Issue #4270: a legitimately-coupled diff pair (leg-to-leg gap = the
    per-class intra-pair clearance, deliberately below the global trace
    clearance) must NOT be demoted by the wide finalize threshold.  The
    exemption is verified, not assumed: a pair whose copper breaks its own
    intra floor is still demoted."""

    @staticmethod
    def _pair_autorouter(gap: float) -> Autorouter:
        from kicad_tools.router.rules import NetClassRouting

        ar = _make_autorouter()
        hs = NetClassRouting(
            name="HS",
            trace_width=0.2,
            clearance=0.15,
            intra_pair_clearance=0.1,
            coupled_routing=True,
        )
        ar.net_class_map["D+"] = hs
        ar.net_class_map["D-"] = hs
        ar.net_names[1] = "D+"
        ar.net_names[2] = "D-"
        # Two parallel legs 'gap' apart edge-to-edge (centreline 0.2 + gap).
        sep = 0.2 + gap
        r_p = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)], "D+")
        r_n = _route(2, [_seg(2.0, 5.0 + sep, 12.0, 5.0 + sep, 2)], "D-")
        for r in (r_p, r_n):
            _commit(ar, r)
        return ar

    def test_coupled_pair_honoring_intra_floor_is_exempt(self):
        # Edge gap 0.1 == intra_pair_clearance floor, below the global
        # 0.15 clearance: the pre-#4270 gate demoted one leg as a short.
        ar = self._pair_autorouter(gap=0.11)
        assert ar.revalidate_committed_copper_or_demote() == []
        assert len(ar.routes) == 2

    def test_pair_breaking_its_own_intra_floor_is_still_demoted(self):
        # Edge gap 0.05 < intra_pair_clearance 0.1: a REAL violation --
        # the exemption must not fire.
        ar = self._pair_autorouter(gap=0.05)
        demoted = ar.revalidate_committed_copper_or_demote()
        assert demoted, "sub-intra-floor pair must still be demoted"

    def test_pair_without_explicit_intra_clearance_is_not_exempt(self):
        from kicad_tools.router.rules import NetClassRouting

        ar = _make_autorouter()
        nc = NetClassRouting(name="HS", trace_width=0.2, clearance=0.15)
        ar.net_class_map["D+"] = nc
        ar.net_class_map["D-"] = nc
        ar.net_names[1] = "D+"
        ar.net_names[2] = "D-"
        r_p = _route(1, [_seg(2.0, 5.0, 12.0, 5.0, 1)], "D+")
        r_n = _route(2, [_seg(2.0, 5.31, 12.0, 5.31, 2)], "D-")
        for r in (r_p, r_n):
            _commit(ar, r)
        # 0.11 edge gap without a declared intra clearance: an accidental
        # suffix-detected pair never qualifies for the exemption.
        demoted = ar.revalidate_committed_copper_or_demote()
        assert demoted
