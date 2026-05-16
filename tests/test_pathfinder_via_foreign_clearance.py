"""Tests for Issue #2947: pathfinder via foreign-net world-coord clearance.

The bug: ``Router._check_via_placement_cached`` consulted only the
coarse-grid obstacle map via ``_is_via_blocked``, so a via that lands on
a "free" grid cell could still violate world-coord clearance against a
foreign-net pad / committed track (most visibly: board-04 BOOT0 vias
overlapping SWDIO/SWCLK by 0.1-0.2 mm).

The fix: after the per-layer grid check passes, call
``via_clearance.point_clear_of_copper`` against the foreign-net context
pushed by :meth:`Autorouter._update_router_via_foreign_context`.  When
no foreign context is set (default / pre-#2947), behavior is unchanged.
"""

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules


def _make_router() -> tuple[Router, RoutingGrid]:
    """Build a small two-layer router with a known-good origin."""
    stack = LayerStack.two_layer()
    rules = DesignRules(grid_resolution=0.5)
    grid = RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        layer_stack=stack,
    )
    return Router(grid, rules), grid


class TestForeignContextBaseline:
    """Without foreign context, behavior matches pre-#2947 (the existing
    via_placement_regression tests already cover the grid-only path)."""

    def test_no_foreign_context_allows_clear_cell(self):
        """A clear cell with no foreign context returns True."""
        router, grid = _make_router()
        # Pick a cell in the interior with no obstacles.
        can_place = router._check_via_placement_cached(
            gx=20, gy=20, net=1, allow_sharing=False
        )
        assert can_place

    def test_setter_default_args_empty(self):
        """Calling the setter with no args clears stale context."""
        router, _ = _make_router()
        # Pre-populate with a foreign pad to ensure a later clear works.
        bad_pad = Pad(x=10.0, y=10.0, width=1.0, height=1.0, net=99, net_name="X")
        router.set_via_foreign_context(foreign_pads=[bad_pad])
        assert router._foreign_pad_tuples  # populated

        router.set_via_foreign_context()  # clear
        assert router._foreign_pad_tuples == []
        assert router._foreign_track_adapters == []


class TestForeignPadRejection:
    """A via that passes the grid check but encroaches on a foreign-net
    pad's clearance envelope must be rejected."""

    def test_foreign_pad_within_clearance_blocks(self):
        """Foreign pad close enough to violate via clearance -> rejected."""
        router, grid = _make_router()
        # Pick a cell whose world-coord position will be near (but not
        # touching) a foreign-net pad.  With rules.via_clearance=0.2,
        # via_diameter=0.7 (radius=0.35) and pad effective_radius=
        # max(w,h)/2=0.5, the threshold is 0.35 + 0.5 + 0.2 = 1.05 mm.
        gx, gy = 20, 20
        wx, wy = grid.grid_to_world(gx, gy)

        # Place a foreign-net pad 0.6 mm from the via center -- well
        # inside the 1.05 mm threshold.
        foreign_pad = Pad(
            x=wx + 0.6, y=wy,
            width=1.0, height=1.0,
            net=99, net_name="FOREIGN",
        )
        router.set_via_foreign_context(foreign_pads=[foreign_pad])

        # Without foreign-context, the grid check would pass; the
        # world-coord check must now reject.
        can_place = router._check_via_placement_cached(
            gx=gx, gy=gy, net=1, allow_sharing=False
        )
        assert not can_place, (
            "Via must be rejected: foreign-net pad at 0.6 mm violates "
            "0.35 (via_r) + 0.5 (pad_r) + 0.2 (clear) = 1.05 mm threshold"
        )

    def test_same_net_pad_does_not_block(self):
        """A pad on the via's own net is filtered out (no rejection)."""
        router, grid = _make_router()
        gx, gy = 20, 20
        wx, wy = grid.grid_to_world(gx, gy)

        same_net_pad = Pad(
            x=wx + 0.6, y=wy,
            width=1.0, height=1.0,
            net=1, net_name="SAME",
        )
        router.set_via_foreign_context(foreign_pads=[same_net_pad])

        can_place = router._check_via_placement_cached(
            gx=gx, gy=gy, net=1, allow_sharing=False
        )
        assert can_place, "Same-net pad must not trigger foreign-net rejection"

    def test_distant_foreign_pad_does_not_block(self):
        """Foreign pad far outside the clearance envelope -> allowed."""
        router, grid = _make_router()
        gx, gy = 20, 20
        wx, wy = grid.grid_to_world(gx, gy)

        far_pad = Pad(
            x=wx + 5.0, y=wy,  # 5 mm clear -- way beyond 1.05 mm
            width=1.0, height=1.0,
            net=99, net_name="FAR",
        )
        router.set_via_foreign_context(foreign_pads=[far_pad])

        can_place = router._check_via_placement_cached(
            gx=gx, gy=gy, net=1, allow_sharing=False
        )
        assert can_place


class TestForeignTrackRejection:
    """The board-04 BOOT0 cluster: a via that passes the grid check but
    overlaps a committed foreign-net track segment."""

    def test_foreign_track_within_clearance_blocks(self):
        """Foreign track segment 0.15 mm from via center -> rejected."""
        router, grid = _make_router()
        gx, gy = 20, 20
        wx, wy = grid.grid_to_world(gx, gy)

        # A horizontal track on the same Y, 0.15 mm above the via.
        # via_r=0.35 + seg_w/2=0.125 + clear=0.2 = 0.675 mm threshold;
        # 0.15 mm is well inside.  Models the BOOT0 vs SWDIO/SWCLK
        # geometry from the board-04 cluster (-0.204 mm overlap).
        foreign_track = Segment(
            x1=wx - 2.0, y1=wy + 0.15,
            x2=wx + 2.0, y2=wy + 0.15,
            width=0.25, layer=Layer.B_CU, net=99, net_name="BOOT0_FOE",
        )
        router.set_via_foreign_context(foreign_tracks=[foreign_track])

        can_place = router._check_via_placement_cached(
            gx=gx, gy=gy, net=1, allow_sharing=False
        )
        assert not can_place, (
            "Via must be rejected: foreign-net track at 0.15 mm violates "
            "0.35 (via_r) + 0.125 (seg_w/2) + 0.2 (clear) = 0.675 mm threshold"
        )


class TestCacheInvariants:
    """Cache invariant: setting foreign context must invalidate cached
    results (mirrors the ``add_routed_segments`` / ``clear_via_cache``
    pattern)."""

    def test_setter_clears_via_cache(self):
        """``set_via_foreign_context`` clears stale cache entries."""
        router, grid = _make_router()

        # Prime the cache with a positive result.
        can_place_1 = router._check_via_placement_cached(
            gx=20, gy=20, net=1, allow_sharing=False
        )
        assert can_place_1
        assert (20, 20, 1, router._via_half_cells) in router._via_cache

        # Push a foreign context that should now block.
        wx, wy = grid.grid_to_world(20, 20)
        blocker = Pad(
            x=wx, y=wy,  # dead-center -> definite overlap
            width=1.0, height=1.0,
            net=99, net_name="X",
        )
        router.set_via_foreign_context(foreign_pads=[blocker])

        # Cache must have been cleared.
        assert (20, 20, 1, router._via_half_cells) not in router._via_cache

        # Re-check: now blocked.
        can_place_2 = router._check_via_placement_cached(
            gx=20, gy=20, net=1, allow_sharing=False
        )
        assert not can_place_2

    def test_cache_key_is_net_keyed(self):
        """Per-#2947 acceptance: cache key includes net so cross-net
        positives can't go stale.  (Documents existing invariant.)"""
        router, _ = _make_router()
        # Two different nets at the same (gx, gy) -> separate cache entries.
        router._check_via_placement_cached(20, 20, net=1, allow_sharing=False)
        router._check_via_placement_cached(20, 20, net=2, allow_sharing=False)
        keys = list(router._via_cache.keys())
        nets = {k[2] for k in keys}
        assert {1, 2}.issubset(nets), (
            f"Cache must discriminate by net for #2947 safety: {keys}"
        )
