"""Tests for Issue #2961 -- same-component carve-out must respect ``_is_obstacle``.

Background
----------

``RoutingGrid._relax_same_component_clearance`` (Issue #2452) clears the
``_blocked`` flag on clearance-overlap cells between two same-component pads on
different nets so the A* search can route through narrow corridors (e.g. the
crystal Y1 OSC_IN/OSC_OUT pair).

The C++ pathfinder gates the ``is_obstacle`` check inside ``if (cell.blocked)``
(see ``pathfinder.cpp:81-107, 131, 177``).  So when the carve-out clears
``blocked``, ``is_obstacle`` is **never consulted** -- the cell becomes
passable for foreign nets even though pad-metal first-touch (#2915) and
rect-aware halo first-touch (#2940) painted ``is_obstacle = True``
specifically to keep foreign-net traces out.

For fine-pitch components (< ``fine_pitch_threshold``)
``_apply_narrow_channel_halo`` (Issue #2878) re-blocks the corridor. But at
2.54 mm THT pin headers (e.g. chorus-test J2 RPi GPIO header) the predicate at
``grid.py:1746-1747`` short-circuits and the halo never runs, leaving foreign
traces free to clip neighbor pads (18 ``clearance_pad_segment`` violations
against J2 on chorus-test prior to this fix).

The fix is a single early-continue in the carve-out loop: if
``self._is_obstacle[layer_idx, gy, gx]`` is already ``True``, skip the
``_blocked = False`` clear. Own-net escape is preserved because the
pathfinder's ``different_net = cell.net != routing_net`` mask short-circuits
to False for the pad owner's own net (``cell.net == routing_net``).

This file exercises the regression with a 2.54 mm THT pin-pair scenario that
mirrors the J2 GPIO header geometry, and a 2.54 mm same-component own-net
scenario that confirms own-net escape still works.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.fixture
def jlcpcb_rules() -> DesignRules:
    """jlcpcb-tier1 design rules (chorus-test deployment that hit this bug)."""
    return DesignRules(
        trace_width=0.127,
        trace_clearance=0.127,
        grid_resolution=0.1,
        min_trace_width=0.127,
        fine_pitch_clearance=0.0635,
        fine_pitch_threshold=0.65,
    )


def _make_thtpin_header_pair(
    ref: str = "J2",
    pitch: float = 2.54,
    pad_size: float = 1.7,
    drill: float = 1.0,
    net_a: int = 1,
    net_a_name: str = "I2S_DIN",
    net_b: int = 2,
    net_b_name: str = "GPIO19",
    center_x: float = 0.0,
    center_y: float = 0.0,
) -> tuple[Pad, Pad]:
    """Create two 2.54 mm THT pin-header pads mirroring chorus-test J2 geometry.

    Two adjacent pins on the same physical header land on different nets.
    Pad metal is round 1.7 mm with 1.0 mm drill, identical to the RPi GPIO
    40-pin header footprint that triggered Issue #2961.
    """
    pad_a = Pad(
        x=center_x - pitch / 2,
        y=center_y,
        width=pad_size,
        height=pad_size,
        net=net_a,
        net_name=net_a_name,
        layer=Layer.F_CU,
        through_hole=True,
        drill=drill,
        ref=ref,
        pin="1",
    )
    pad_b = Pad(
        x=center_x + pitch / 2,
        y=center_y,
        width=pad_size,
        height=pad_size,
        net=net_b,
        net_name=net_b_name,
        layer=Layer.F_CU,
        through_hole=True,
        drill=drill,
        ref=ref,
        pin="2",
    )
    return pad_a, pad_b


class TestCarveoutRespectsObstacle:
    """Issue #2961: ``_relax_same_component_clearance`` must not clear
    ``_blocked`` on cells where ``_is_obstacle == True``.
    """

    def test_isobstacle_cells_remain_blocked_after_carveout(self, jlcpcb_rules):
        """The fix: ``is_obstacle`` cells survive the same-component carve-out.

        Pre-fix: a clearance-halo cell on pad B's net (painted
        ``is_obstacle = True`` by the rect-aware first-touch at
        ``grid.py:1319-1324``) gets ``_blocked = False`` cleared by the
        carve-out, leaving ``(blocked=False, is_obstacle=True)``.  The C++
        pathfinder skips the ``is_obstacle`` check entirely (it lives inside
        ``if (cell.blocked)``) and treats the cell as passable for ALL
        nets -- producing the J2 column-centerline clipping pattern.

        Post-fix: any cell with ``is_obstacle = True`` survives the
        carve-out with ``blocked = True``, restoring the contract.
        """
        grid = RoutingGrid(width=20.0, height=20.0, rules=jlcpcb_rules)
        pad_a, pad_b = _make_thtpin_header_pair(center_x=10.0, center_y=10.0)
        grid.add_pad(pad_a)
        grid.add_pad(pad_b)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Find at least one cell that:
        #   * is in the carve-out overlap region
        #   * carries one of the two same-component nets (otherwise the
        #     cell-net allowlist at grid.py:2041 would have rejected it)
        #   * is_obstacle == True (painted by rect-aware first-touch)
        # The fix asserts that such cells stay blocked.
        found = False
        for gy in range(grid.rows):
            for gx in range(grid.cols):
                if not grid._is_obstacle[layer_idx, gy, gx]:
                    continue
                cell_net = int(grid._net[layer_idx, gy, gx])
                if cell_net not in (pad_a.net, pad_b.net):
                    continue
                # is_obstacle == True for a same-component-net cell
                found = True
                assert grid._blocked[layer_idx, gy, gx], (
                    f"Issue #2961: cell ({gx}, {gy}) on layer {layer_idx} has "
                    f"is_obstacle=True (cell.net={cell_net}) but was unblocked "
                    f"by the same-component carve-out. The pathfinder gates "
                    f"is_obstacle inside `if (cell.blocked)` -- foreign nets "
                    f"would treat this cell as passable, producing the "
                    f"clearance_pad_segment violation cluster fixed by #2961."
                )

        assert found, (
            "Test geometry should expose at least one (is_obstacle=True, "
            "cell.net in same-component nets) cell to exercise the regression "
            "guard. If no such cells exist, the rect-aware first-touch "
            "(grid.py:1319-1324) is not painting is_obstacle on this "
            "geometry -- this guard would silently pass."
        )

    def test_foreign_net_rejected_at_pad_pair(self, jlcpcb_rules):
        """A foreign net N3 must be blocked from cells inside the J2 carve-out.

        This is the user-visible behavior: a third net trying to route through
        the column-centerline corridor between two same-component pins must
        see those cells as blocked.
        """
        grid = RoutingGrid(width=20.0, height=20.0, rules=jlcpcb_rules)
        pad_a, pad_b = _make_thtpin_header_pair(
            net_a=1, net_b=2, center_x=10.0, center_y=10.0
        )
        grid.add_pad(pad_a)
        grid.add_pad(pad_b)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        foreign_net = 99  # Not pad_a.net (1), not pad_b.net (2)

        # Probe the cells immediately adjacent to pad A's metal on the side
        # facing pad B -- these are clearance-halo cells that would have
        # `is_obstacle=True` painted by the first-touch fix.
        # Pad A is at world (8.73, 10.0), pad B is at (11.27, 10.0).
        # Sample a vertical slice just east of pad A's metal edge.
        pad_a_east_edge = pad_a.x + pad_a.width / 2  # 8.73 + 0.85 = 9.58
        probe_x = pad_a_east_edge + jlcpcb_rules.grid_resolution
        rejected_obstacle_cells = 0
        for dy in range(-5, 6):
            wy = pad_a.y + dy * jlcpcb_rules.grid_resolution
            gx, gy = grid.world_to_grid(probe_x, wy)
            if not (0 <= gx < grid.cols and 0 <= gy < grid.rows):
                continue
            if not grid._is_obstacle[layer_idx, gy, gx]:
                continue
            # Foreign net N3 must be blocked from is_obstacle cells.
            assert grid.is_blocked(gx, gy, Layer.F_CU, net=foreign_net), (
                f"Issue #2961: foreign net {foreign_net} must be rejected "
                f"from is_obstacle cell ({gx}, {gy}) inside the J2 carve-out "
                f"region. Pre-fix, the carve-out cleared _blocked here, "
                f"letting the cell silently admit any foreign net."
            )
            rejected_obstacle_cells += 1

        # Sanity: the test geometry must actually have produced at least one
        # is_obstacle cell to probe, otherwise the assertion above is vacuous.
        assert rejected_obstacle_cells > 0, (
            "Expected at least one is_obstacle clearance-halo cell adjacent "
            "to pad A's metal east edge. If zero, the rect-aware first-touch "
            "is not painting this geometry -- guard would silently pass."
        )

    def test_own_net_escape_preserved(self, jlcpcb_rules):
        """Own-net escape (#2452 / #2880 / #2908) must survive the fix.

        The early-continue only protects cells where ``is_obstacle = True``.
        For own-net traces (``trace_net == cell.net``), the pathfinder's
        ``different_net = cell.net != routing_net`` mask is False, so the
        cell stays passable regardless of ``is_obstacle``. We verify this at
        the Python-level ``is_blocked`` mirror.
        """
        grid = RoutingGrid(width=20.0, height=20.0, rules=jlcpcb_rules)
        pad_a, pad_b = _make_thtpin_header_pair(
            net_a=1, net_b=2, center_x=10.0, center_y=10.0
        )
        grid.add_pad(pad_a)
        grid.add_pad(pad_b)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Cells carrying pad_a.net stay passable for pad_a.net traces (own-net
        # escape).  We probe cells adjacent to pad A's metal that are on
        # pad_a.net.
        own_net_passable = 0
        for gy in range(grid.rows):
            for gx in range(grid.cols):
                if int(grid._net[layer_idx, gy, gx]) != pad_a.net:
                    continue
                if grid._pad_blocked[layer_idx, gy, gx]:
                    continue  # Pad metal itself -- still passable for own net
                # Non-metal cell on pad_a.net: own-net trace must reach it
                if not grid.is_blocked(gx, gy, Layer.F_CU, net=pad_a.net):
                    own_net_passable += 1

        assert own_net_passable > 0, (
            "Own-net escape regression (#2452 / #2880 / #2908): pad A's own "
            "net must remain able to traverse non-metal cells on its net. "
            "If the fix accidentally over-blocks (e.g. broadened the "
            "early-continue beyond is_obstacle), this guard catches it."
        )

    def test_third_party_cells_untouched(self, jlcpcb_rules):
        """Cells that are NOT on either same-component net are never touched
        by the carve-out (existing #2452 behavior, just re-verified).
        """
        grid = RoutingGrid(width=20.0, height=20.0, rules=jlcpcb_rules)
        # Add an "intruder" pad on a third component but a third net (N3).
        intruder = Pad(
            x=10.0,
            y=11.5,  # Just north of the J2 pair
            width=0.5,
            height=0.5,
            net=3,
            net_name="THIRD_NET",
            layer=Layer.F_CU,
            ref="C99",  # Different component
            pin="1",
        )
        grid.add_pad(intruder)

        # Capture intruder's metal blocked state BEFORE adding J2 pair.
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        intruder_gx, intruder_gy = grid.world_to_grid(intruder.x, intruder.y)
        assert grid._blocked[layer_idx, intruder_gy, intruder_gx]

        # Now add the J2 pair which triggers the carve-out.
        pad_a, pad_b = _make_thtpin_header_pair(
            net_a=1, net_b=2, center_x=10.0, center_y=10.0
        )
        grid.add_pad(pad_a)
        grid.add_pad(pad_b)

        # Intruder pad metal must STILL be blocked.  (The carve-out's
        # ``cell_net != pad.net and cell_net != other_pad.net`` allowlist
        # already excludes it, but this is a defense-in-depth check that
        # also catches accidental widening of the early-continue.)
        assert grid._blocked[layer_idx, intruder_gy, intruder_gx], (
            "Third-party pad metal must never be unblocked by same-component "
            "carve-out (existing #2452 guard)."
        )
