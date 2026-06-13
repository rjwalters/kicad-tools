"""Issue #2946: long-axis nudge for in-pad via clearance rescue.

Verifies the ``EscapeRouter._select_in_pad_via_position`` helper from
PR for #2946:

* When the dead-centre candidate clears its foreign-net neighbors, no
  nudge is attempted (the function returns the original pad center and
  ``nudged=False``).  This preserves PR #2945 behavior on the easy case.

* When dead-centre violates clearance to a neighbor and a long-axis
  offset rescues the placement, the helper returns the offset center
  with ``nudged=True`` and the result lies entirely inside the pad's
  copper rectangle (the stencil-safety constraint).

* When no offset can rescue the placement (dense foreign copper on
  every side), the helper falls back to dead-centre with
  ``nudged=False`` so the caller emits the PR #2945 diagnostic warning
  and proceeds with a defer-to-DRC via.

The defect this guards against is the board-04 OSC_OUT in-pad rescue:
a 0.45mm via placed dead-centre on an LQFP 0.5mm-pitch pin sat 0.05mm
from the adjacent foreign-net pin pads (OSC_IN / NRST), producing
DRC errors at jlcpcb-tier1's 0.127mm clearance rule.  Nudging along
the pad's long axis -- well inside the SMT stencil aperture -- moves
the via clear while preserving the in-pad escape strategy that lets
the LQFP perimeter route at all.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.escape import (
    EscapeRouter,
    PackageInfo,
    PackageType,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# Use a central anchor far from any grid edge.  ``_can_place_via``'s
# bounds check is ``0 <= x <= grid.width`` (world-coord against grid
# extent, assuming origin=(0,0)); we therefore place the pad cluster
# safely interior so all candidate offsets stay inside the grid.
ANCHOR_X = 10.0
ANCHOR_Y = 10.0


def _make_rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=LayerStack.two_layer(),
    )


def _make_package(pads: list[Pad], ref: str = "U1") -> PackageInfo:
    """Wrap pads in a minimal PackageInfo so the nudge code path engages.

    ``_select_in_pad_via_position`` short-circuits to dead-centre when
    ``package is None`` (no foreign-net pad context).  We supply a
    minimal-but-valid PackageInfo so the nudge logic runs.
    """
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


class TestSelectInPadViaPositionDeadCentrePasses:
    """When dead-centre clears every foreign pad, no nudge is attempted."""

    def test_isolated_pad_returns_dead_centre(self):
        """A pad with no foreign neighbors keeps the dead-centre via."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        target = Pad(
            x=ANCHOR_X,
            y=ANCHOR_Y,
            width=1.4,
            height=0.3,
            net=5,
            net_name="OSC_OUT",
            ref="U2",
            pin="5",
            layer=Layer.F_CU,
        )
        package = _make_package([target])

        via_x, via_y, nudged = router._select_in_pad_via_position(
            pad=target,
            via_diameter=0.6,
            min_annular=0.05,
            effective_clearance=0.15,
            package=package,
        )
        assert nudged is False
        assert via_x == pytest.approx(ANCHOR_X, abs=1e-6)
        assert via_y == pytest.approx(ANCHOR_Y, abs=1e-6)

    def test_far_foreign_neighbor_returns_dead_centre(self):
        """When the only foreign neighbor is well outside the clearance
        envelope, dead-centre is accepted without a nudge."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        target = Pad(
            x=ANCHOR_X,
            y=ANCHOR_Y,
            width=1.4,
            height=0.3,
            net=5,
            net_name="OSC_OUT",
            ref="U2",
            pin="5",
            layer=Layer.F_CU,
        )
        # Foreign square pad far from target -- 3mm along X, well past
        # any clearance envelope.
        neighbor = Pad(
            x=ANCHOR_X + 3.0,
            y=ANCHOR_Y,
            width=0.3,
            height=0.3,
            net=9,
            net_name="NRST",
            ref="U2",
            pin="9",
            layer=Layer.F_CU,
        )
        package = _make_package([target, neighbor])

        via_x, via_y, nudged = router._select_in_pad_via_position(
            pad=target,
            via_diameter=0.6,
            min_annular=0.05,
            effective_clearance=0.15,
            package=package,
        )
        assert nudged is False
        assert via_x == pytest.approx(ANCHOR_X, abs=1e-6)
        assert via_y == pytest.approx(ANCHOR_Y, abs=1e-6)

    def test_none_package_returns_dead_centre(self):
        """``package=None`` disables the nudge rescue (no neighbor context)."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        target = Pad(
            x=ANCHOR_X,
            y=ANCHOR_Y,
            width=1.4,
            height=0.3,
            net=5,
            net_name="OSC_OUT",
            ref="U2",
            pin="5",
            layer=Layer.F_CU,
        )
        via_x, via_y, nudged = router._select_in_pad_via_position(
            pad=target,
            via_diameter=0.6,
            min_annular=0.05,
            effective_clearance=0.15,
            package=None,
        )
        assert nudged is False
        assert via_x == pytest.approx(ANCHOR_X, abs=1e-6)
        assert via_y == pytest.approx(ANCHOR_Y, abs=1e-6)


class TestSelectInPadViaPositionNudgeSucceeds:
    """When dead-centre fails but a long-axis offset rescues it."""

    def test_nudge_chosen_against_axial_neighbor(self):
        """A small foreign pad sits *along* the target pad's long axis,
        too close for the dead-centre via to clear but far enough that a
        modest long-axis offset (toward the opposite end of the pad)
        recovers the clearance.

        Geometry:
        - Target pad at the anchor, width=1.4 (long X) x height=0.3 (short Y).
        - Foreign pad 0.55mm further along +X: width=0.3 x height=0.3 ->
          effective radius (using max(w,h)/2 from ``_can_place_via``) = 0.15.
        - Via diameter 0.6 (radius 0.3), clearance 0.15.
        - Required centre-to-centre distance: 0.3 + 0.15 + 0.15 = 0.60mm.
        - Dead-centre distance: 0.55mm -> violates by 0.05mm.  Nudging
          the via -0.10mm along X (away from the foreign pad) yields
          0.65mm centre-to-centre, which clears.

        Stencil-safety budget:
        - max_offset = (1.4 - 0.6)/2 - 0.05 = 0.35mm.
        - Chosen offset -0.10mm is well inside the budget; the via
          barrel + annular ring stays entirely inside the pad's copper
          rectangle.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        target = Pad(
            x=ANCHOR_X,
            y=ANCHOR_Y,
            width=1.4,
            height=0.3,
            net=5,
            net_name="OSC_OUT",
            ref="U2",
            pin="5",
            layer=Layer.F_CU,
        )
        # Foreign pad too close along +X to clear dead-centre.
        neighbor = Pad(
            x=ANCHOR_X + 0.55,
            y=ANCHOR_Y,
            width=0.3,
            height=0.3,
            net=9,
            net_name="NRST",
            ref="U2",
            pin="9",
            layer=Layer.F_CU,
        )
        package = _make_package([target, neighbor])

        # First confirm the dead-centre placement would actually fail
        # (i.e. our fixture exercises the nudge path).
        assert not router._can_place_via(
            x=target.x,
            y=target.y,
            net=target.net,
            foreign_pads=[neighbor],
            clearance=0.15,
            via_diameter=0.6,
        )

        via_x, via_y, nudged = router._select_in_pad_via_position(
            pad=target,
            via_diameter=0.6,
            min_annular=0.05,
            effective_clearance=0.15,
            package=package,
        )

        # The nudge succeeded.
        assert nudged is True

        # The chosen via offset is AWAY from the foreign pad along the
        # long axis (X), within the stencil-safety budget.
        assert via_y == pytest.approx(ANCHOR_Y, abs=1e-6)
        offset = via_x - ANCHOR_X
        assert offset < 0.0  # away from neighbor at +0.55
        # Pad-copper containment: via radius + annular must stay inside
        # the pad rectangle along the long axis.
        max_offset = (target.width - 0.6) / 2 - 0.05  # 0.35
        assert abs(offset) <= max_offset + 1e-9

        # And the chosen placement actually clears the neighbor.
        assert router._can_place_via(
            x=via_x,
            y=via_y,
            net=target.net,
            foreign_pads=[neighbor],
            clearance=0.15,
            via_diameter=0.6,
        )

    def test_nudge_picks_smallest_magnitude_first(self):
        """The nudge iteration should pick the smallest passing offset
        (the search visits ``[+s, -s, +2s, -2s, ...]`` with s=0.05mm).

        With the neighbor at +0.55mm offset, the smallest passing offset
        is -0.05mm: at that distance the via center sits 0.60mm from
        the neighbor center, exactly the minimum required distance
        (``via_radius + pad_radius + clearance = 0.30 + 0.15 + 0.15``).
        ``point_clear_of_copper`` accepts equality (``dist < min_dist``
        is strictly false at the boundary), so -0.05mm is the smallest
        passing magnitude.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        target = Pad(
            x=ANCHOR_X,
            y=ANCHOR_Y,
            width=1.4,
            height=0.3,
            net=5,
            net_name="OSC_OUT",
            ref="U2",
            pin="5",
            layer=Layer.F_CU,
        )
        neighbor = Pad(
            x=ANCHOR_X + 0.55,
            y=ANCHOR_Y,
            width=0.3,
            height=0.3,
            net=9,
            net_name="NRST",
            ref="U2",
            pin="9",
            layer=Layer.F_CU,
        )
        package = _make_package([target, neighbor])

        via_x, via_y, nudged = router._select_in_pad_via_position(
            pad=target,
            via_diameter=0.6,
            min_annular=0.05,
            effective_clearance=0.15,
            package=package,
        )
        assert nudged is True
        # Smallest passing offset is -0.05mm.  Allow 0.001mm slack for
        # floating-point arithmetic.  The chosen offset must be smaller
        # in magnitude than the budget (0.35mm) -- this guards against
        # a regression to "pick the maximum offset" or other ordering
        # bugs.
        offset = via_x - ANCHOR_X
        assert offset == pytest.approx(-0.05, abs=0.001)

    def test_nudge_long_axis_is_y_for_tall_pad(self):
        """When the pad's long axis is Y (height > width), the nudge
        proceeds along Y, not X.  The function must detect orientation
        from the pad's width/height ratio.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        # Tall pad: long axis = Y.
        target = Pad(
            x=ANCHOR_X,
            y=ANCHOR_Y,
            width=0.3,
            height=1.4,
            net=5,
            net_name="OSC_OUT",
            ref="U2",
            pin="5",
            layer=Layer.F_CU,
        )
        # Foreign pad along +Y -- the long axis direction.
        neighbor = Pad(
            x=ANCHOR_X,
            y=ANCHOR_Y + 0.55,
            width=0.3,
            height=0.3,
            net=9,
            net_name="NRST",
            ref="U2",
            pin="9",
            layer=Layer.F_CU,
        )
        package = _make_package([target, neighbor])

        via_x, via_y, nudged = router._select_in_pad_via_position(
            pad=target,
            via_diameter=0.6,
            min_annular=0.05,
            effective_clearance=0.15,
            package=package,
        )
        assert nudged is True
        # Nudge is along Y (away from the +Y neighbor).
        assert via_x == pytest.approx(ANCHOR_X, abs=1e-6)
        assert (via_y - ANCHOR_Y) < 0.0


class TestSelectInPadViaPositionFallback:
    """When no offset can rescue the placement, fall back to dead-centre."""

    def test_no_room_to_nudge_returns_dead_centre(self):
        """A near-square pad has a vanishing long-axis stencil-safety
        budget: ``(long_dim - via_diameter)/2 - min_annular`` is
        non-positive.  The helper must fall back to dead-centre with
        ``nudged=False`` so the caller emits the diagnostic warning.

        ``larger_dim`` is the parent function's gate, not this helper's,
        so it doesn't run in this isolated test path -- we observe the
        helper's own ``max_offset <= 0`` fallback directly.
        """
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)

        # Pad just barely larger than via_diameter on the long axis so
        # max_offset = (0.65 - 0.6)/2 - 0.05 = -0.025 (no room).  We
        # co-locate a foreign pad close enough to force dead-centre to
        # fail clearance so the helper enters the nudge path and then
        # exits via the ``max_offset <= 0`` early-return.
        target = Pad(
            x=ANCHOR_X,
            y=ANCHOR_Y,
            width=0.65,
            height=0.65,
            net=5,
            net_name="A",
            ref="U2",
            pin="5",
            layer=Layer.F_CU,
        )
        # Foreign pad too close to clear dead-centre via.
        neighbor = Pad(
            x=ANCHOR_X + 0.50,
            y=ANCHOR_Y,
            width=0.3,
            height=0.3,
            net=9,
            net_name="B",
            ref="U2",
            pin="9",
            layer=Layer.F_CU,
        )
        package = _make_package([target, neighbor])

        via_x, via_y, nudged = router._select_in_pad_via_position(
            pad=target,
            via_diameter=0.6,
            min_annular=0.05,
            effective_clearance=0.15,
            package=package,
        )
        # Fall back to dead-centre with ``nudged=False`` so the caller
        # emits the PR #2945 diagnostic warning and proceeds.
        assert nudged is False
        assert via_x == pytest.approx(ANCHOR_X, abs=1e-6)
        assert via_y == pytest.approx(ANCHOR_Y, abs=1e-6)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
