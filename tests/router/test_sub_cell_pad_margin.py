"""Issue #3233 — pad halo grid representation sub-cell margin regression.

Background
----------

PR #3232 switched the trace-clearance kernel from Chebyshev to Euclidean,
correctly admitting diagonal-corner trace placements that the manufacturer
DRC also admits. That uncovered a latent gap in how
``RoutingGrid._add_pad_unsafe`` quantizes the continuous pad metal onto
the integer cell grid: ``ceil``/``floor`` on the un-inflated metal
rectangle marked only cells whose CENTER was strictly inside the
continuous metal, leaving an outer band where:

- The continuous pad copper extends past the marked metal cells by up to
  ``resolution/2``.
- The clearance halo cells include that band but ``pad_blocked`` does not.

The pad-exit relaxation at ``pathfinder.py:2713`` keys off
``cell.pad_blocked`` to distinguish "actual pad copper" from "clearance
halo" -- when a trace exits its own pad, halo-only cells are admitted as
the first step out.  A trace exiting through the sub-cell margin band
steps onto cells whose ``pad_blocked = False`` (looks like halo) but
whose extent overlaps continuous pad copper -- the trace edge brushes
real pad metal and is flagged ``clearance_pad_segment`` by the DRC.

On board 05 with ``--backend cpp`` this raised the pad-segment violation
count from 9 (pre-#3232) to 17 (post-#3232).

The fix inflates the metal-area rectangle outward by ``resolution/2``
BEFORE applying ``ceil``/``floor``, so the ``pad_blocked`` region now
covers every grid cell whose EXTENT (not just center) overlaps the
continuous metal area.

This regression test exercises a pad whose continuous metal edge falls
exactly half a cell past the adjacent integer-cell-center.  Pre-fix the
boundary cell is pad-halo only; post-fix it is correctly marked
``pad_blocked = True``.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.fixture
def jlcpcb_rules() -> DesignRules:
    """JLCPCB tier-1 design rules at the actual board-05 deployment
    configuration: 0.127 mm trace clearance + 0.127 mm grid resolution.

    These are the parameters that reproduce the band-width math in the
    issue body: ``resolution/2 = 0.0635 mm`` is the sub-cell margin.
    """
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.127,
        grid_resolution=0.127,
        min_trace_width=0.127,
        fine_pitch_clearance=0.0635,
        fine_pitch_threshold=0.65,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    """Build a 20x20 mm grid with origin at (0, 0) so cell ``gx`` maps to
    world coordinate ``gx * resolution`` -- making the boundary math
    easy to verify by hand."""
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=0.0,
        origin_y=0.0,
    )


class TestSubCellPadMargin:
    """Issue #3233: pad metal cells must cover the full continuous copper
    boundary, not just cells whose center is strictly inside it.

    The fixture places a 1.0 x 1.0 mm SMD pad at world ``(10.0, 10.0)``.
    Continuous metal edge at ``x = 10.5`` falls between grid cell
    centers at ``cell-82-center = 10.414`` and ``cell-83-center = 10.541``.

    Pre-fix:
        ``metal_gx2 = floor(10.5 / 0.127) = floor(82.677) = 82``.
        Cell-83 has ``pad_blocked = False`` -- the cell-extent
        ``[10.4775, 10.6045]`` overlaps continuous metal at
        ``[10.4775, 10.5]`` (0.0225 mm of pad copper inside the cell).
        A trace centered at cell-83 has its left edge at ``10.441`` --
        INSIDE continuous pad metal (overlap of 0.059 mm).  The pad-exit
        relaxation at ``pathfinder.py:2713`` keys off ``pad_blocked`` to
        treat this cell as halo-only and admits the placement,
        producing a ``clearance_pad_segment`` DRC violation with
        shortfall on the order of ``resolution/2``.

    Post-fix:
        ``metal_x2`` inflated by ``resolution/2 = 0.0635`` BEFORE
        ``floor``: ``metal_gx2 = floor(10.5635 / 0.127) = floor(83.18) = 83``.
        Cell-83 is now ``pad_blocked = True`` and the pad-exit
        relaxation correctly classifies it as actual pad copper,
        refusing foreign-net traces.
    """

    def test_sub_cell_margin_cell_is_pad_blocked(self, jlcpcb_rules):
        """A cell whose center is outside continuous metal but whose
        extent overlaps continuous metal must be ``pad_blocked = True``
        (Issue #3233)."""
        grid = _make_grid(jlcpcb_rules)
        # Pad at (10.0, 10.0): right edge at x = 10.5, between
        # cell-82-center (10.414) and cell-83-center (10.541).
        pad = Pad(
            x=10.0,
            y=10.0,
            width=1.0,
            height=1.0,
            net=42,
            net_name="TEST_NET",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        grid.add_pad(pad)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Probe cell-83 in the +x direction.  Its center at world
        # x = 10.541 is 0.041 mm OUTSIDE continuous metal (right edge
        # at 10.5) but its extent [10.4775, 10.6045] overlaps
        # continuous metal at [10.4775, 10.5] (0.0225 mm of pad copper
        # inside the cell).  A trace at cell-83 has its LEFT edge at
        # 10.441 -- inside continuous pad metal.
        probe_cell_x = 83
        probe_cell_y = grid.world_to_grid(0.0, pad.y)[1]
        cell = grid.grid[layer_idx][probe_cell_y][probe_cell_x]

        assert cell.blocked is True, (
            "Sub-cell margin cell sits inside the clearance halo and "
            "must be ``blocked = True``"
        )
        assert cell.pad_blocked is True, (
            "Issue #3233: a cell whose extent overlaps continuous pad "
            "metal MUST be marked ``pad_blocked = True`` even when its "
            "center sits in the outer ``resolution/2`` band. Pre-fix, "
            "the ``ceil``/``floor`` on the un-inflated metal rectangle "
            "left this band as halo-only, allowing the pad-exit "
            "relaxation (pathfinder.py:2713) to admit foreign traces "
            "into a cell whose extent overlaps continuous pad copper -- "
            "producing 17 sub-127 um ``clearance_pad_segment`` "
            "violations on board 05 with ``--backend cpp``."
        )
        assert cell.is_obstacle is True, (
            "Sub-cell margin cell is real pad copper to the DRC -- it "
            "must be ``is_obstacle = True`` so the pathfinder's "
            "negotiated-mode ``static_blocks`` loophole cannot release "
            "it to foreign-net traces after one trace touches it."
        )

    def test_cell_well_outside_metal_extent_remains_halo_only(
        self, jlcpcb_rules
    ):
        """A cell whose extent does NOT overlap continuous metal must
        remain halo-only -- the inflation by ``resolution/2`` is
        precisely one cell on each side, not unbounded.

        This is the over-shoot guard: if the fix tightened
        ``pad_blocked`` beyond continuous metal + ``resolution/2``,
        the inner clearance corridor between fine-pitch pads would
        lose passable cells and the router would regress on board 04 /
        chorus-test escape routing.
        """
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=10.0,
            y=10.0,
            width=1.0,
            height=1.0,
            net=42,
            net_name="TEST_NET",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        grid.add_pad(pad)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Probe cell-84: center at world x = 10.668, extent
        # [10.6045, 10.7315].  Continuous metal extends only to
        # x = 10.5, so cell-84's extent is fully outside metal
        # (gap of 0.1045 mm between cell extent and continuous metal).
        # Cell-84 must NOT be pad_blocked.
        probe_cell_x = 84
        probe_cell_y = grid.world_to_grid(0.0, pad.y)[1]
        cell = grid.grid[layer_idx][probe_cell_y][probe_cell_x]

        assert cell.pad_blocked is False, (
            "Issue #3233 over-shoot guard: a cell whose extent does "
            "NOT overlap continuous metal must NOT be marked "
            "``pad_blocked = True``. The fix inflates by exactly "
            "``resolution/2`` -- any wider would regress fine-pitch "
            "escape routing."
        )

    def test_symmetric_inflation_on_negative_axis(self, jlcpcb_rules):
        """The inflation applies symmetrically -- the left/bottom edge
        of continuous metal must also pull in the adjacent outer cell.

        Pad at (10.0, 10.0) has left edge at x = 9.5.  Cell-74-center
        at 9.398 (extent [9.3345, 9.4615], outside metal) and
        cell-75-center at 9.525 (extent [9.4615, 9.588], overlaps
        continuous metal at [9.5, 9.588]).
        """
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=10.0,
            y=10.0,
            width=1.0,
            height=1.0,
            net=42,
            net_name="TEST_NET",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        grid.add_pad(pad)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Cell-75 has center at world x = 9.525, extent [9.4615, 9.588].
        # Continuous metal extends from 9.5 to 10.5.  Cell-75 center
        # is OUTSIDE metal but extent overlaps in [9.5, 9.588] (0.088 mm
        # of pad copper inside the cell).
        probe_cell_x = 75
        probe_cell_y = grid.world_to_grid(0.0, pad.y)[1]
        cell = grid.grid[layer_idx][probe_cell_y][probe_cell_x]

        assert cell.pad_blocked is True, (
            "Issue #3233: sub-cell margin must close symmetrically on "
            "the negative axis. Cell-75's extent overlaps continuous "
            "metal on the left edge of the pad."
        )

    def test_y_axis_inflation(self, jlcpcb_rules):
        """The inflation applies on both x and y axes (the issue is
        2D, not 1D)."""
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=10.0,
            y=10.0,
            width=1.0,
            height=1.0,
            net=42,
            net_name="TEST_NET",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        grid.add_pad(pad)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        # Top edge at y = 10.5.  Cell at world y = 10.541 (gy=83) has
        # extent overlapping continuous metal.
        probe_cell_x = grid.world_to_grid(pad.x, 0.0)[0]
        probe_cell_y = 83
        cell = grid.grid[layer_idx][probe_cell_y][probe_cell_x]

        assert cell.pad_blocked is True, (
            "Issue #3233: sub-cell margin must close on the y axis "
            "as well -- the inflation is bidirectional in 2D."
        )

    def test_diagonal_corner_pad_blocked(self, jlcpcb_rules):
        """The diagonal corner cell -- the failure mode the Euclidean
        kernel uncovered (cited in the issue body) -- must be
        ``pad_blocked = True``.

        The cell at ``(gx=83, gy=83)`` for a pad at ``(10.0, 10.0)``
        is the diagonal-corner cell whose extent overlaps continuous
        pad copper at both the right and top edges simultaneously.
        Pre-#3232 the Chebyshev kernel rejected diagonal corner
        placements; post-#3232 the Euclidean kernel admits them, so
        this cell's ``pad_blocked`` flag must correctly classify it
        as pad copper.
        """
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=10.0,
            y=10.0,
            width=1.0,
            height=1.0,
            net=42,
            net_name="TEST_NET",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        grid.add_pad(pad)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        # Diagonal corner: (gx=83, gy=83) -- both axes in sub-cell band.
        cell = grid.grid[layer_idx][83][83]

        assert cell.pad_blocked is True, (
            "Issue #3233: diagonal-corner cell whose extent overlaps "
            "continuous metal at both x and y edges must be "
            "``pad_blocked = True``. This is the placement family the "
            "Euclidean kernel (#3232) admits and the cell-quantization "
            "gap exposes."
        )
