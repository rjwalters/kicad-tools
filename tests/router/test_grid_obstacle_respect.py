"""Tests for Issue #2915 / #2920 — isolated pad cells must be marked
``is_obstacle = True`` on first touch.

Background
----------

``RoutingGrid._add_pad_unsafe`` populates the per-cell ``is_obstacle``
flag that the pathfinder consults in its negotiated-mode loophole:

::

    obstacle_blocks = self._blocked & self._is_obstacle & different_net
    static_blocks   = self._blocked & ~self._is_obstacle & different_net &
                      (self._usage_count == 0)
    base_blocked    = obstacle_blocks | static_blocks

The ``static_blocks`` branch releases a cell as soon as one trace touches
it (``_usage_count > 0``). That is fine for soft pad clearance halos but
it MUST NEVER apply to actual pad copper. Pre-fix, ``_add_pad_unsafe``
only flipped ``is_obstacle = True`` for pad-metal cells on the SECOND
pad-touch path (``elif cell.net != pad.net``). Isolated pads (TO-220
2.54 mm pitch, RPi GPIO 2.54 mm pitch, audio jacks, 0402 caps) had no
neighbour-envelope overlap → cells were first-touched with
``is_obstacle = False`` → ``static_blocks`` admitted foreign-net traces
to pad metal, producing trace-through-pad DRC violations on chorus-test
(#2915, 215 violations) and board-05 TO-220 H-bridge (#2920, 252+
violations).

The fix sets ``is_obstacle = True`` unconditionally on first touch for
pad-metal cells. Same-net escape (#2880 / #2908) is preserved because
the pathfinder's ``different_net`` mask is False for cells owned by the
routing net.

This test file exercises the fix with mixed pad geometries (THT
rectangular, SMD rectangular, SMD round-equivalent, oval) that all hit
the isolated-pad pattern.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.fixture
def jlcpcb_rules() -> DesignRules:
    """jlcpcb-tier1 design rules (matching the chorus-test / board-05
    deployment that hit this bug).
    """
    return DesignRules(
        trace_width=0.127,
        trace_clearance=0.127,
        grid_resolution=0.1,
        min_trace_width=0.127,
        fine_pitch_clearance=0.0635,
        fine_pitch_threshold=0.65,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    """Build a 40x40 mm grid centred on the origin."""
    return RoutingGrid(
        width=40.0,
        height=40.0,
        rules=rules,
        origin_x=-20.0,
        origin_y=-20.0,
    )


def _is_metal_cell(grid: RoutingGrid, pad: Pad) -> tuple[int, int]:
    """Return grid coordinates of the cell at the pad centre.

    The center cell is guaranteed to be inside the metal area; we use it
    as the canonical probe location.
    """
    return grid.world_to_grid(pad.x, pad.y)


class TestIsolatedPadObstacleMarking:
    """Issue #2915 / #2920: pad metal must be ``is_obstacle = True`` on
    first touch, regardless of pad shape.

    Each test adds a single isolated pad (no neighbour-envelope overlap)
    and asserts that:

    1. The pad-centre cell is ``pad_blocked = True``.
    2. The pad-centre cell is ``is_obstacle = True`` (the bug fix).
    3. A foreign-net query through ``is_blocked`` rejects the cell.
    """

    def test_isolated_tht_rect_pad_is_obstacle(self, jlcpcb_rules):
        """TO-220-style THT rectangular pad: 1.8 mm x 1.8 mm with drill.

        Mirrors the board-05 PHASE_B / PHASE_C TO-220 MOSFETs (Q4/Q6)
        from Issue #2920. With 2.54 mm pitch and 0.127 mm clearance,
        adjacent envelopes do not overlap — every cell is first-touch.
        """
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=0.0,
            y=0.0,
            width=1.8,
            height=1.8,
            net=5,
            net_name="PHASE_B",
            layer=Layer.F_CU,
            through_hole=True,
            drill=1.0,
            ref="Q4",
            pin="2",
        )
        grid.add_pad(pad)

        gx, gy = _is_metal_cell(grid, pad)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]

        assert cell.pad_blocked is True, "TO-220 THT pad center must be pad_blocked"
        assert cell.is_obstacle is True, (
            "Issue #2915/#2920: isolated TO-220 THT pad metal cell must be "
            "is_obstacle=True on first touch (was False pre-fix)"
        )

        # A foreign-net trace probe must see the cell as blocked.
        assert grid.is_blocked(gx, gy, Layer.F_CU, net=42), (
            "Foreign-net trace must be blocked from isolated TO-220 pad metal"
        )

    def test_isolated_smd_rect_pad_is_obstacle(self, jlcpcb_rules):
        """0805-style SMD rectangular pad: 1.25 mm x 1.7 mm."""
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=2.0,
            y=2.0,
            width=1.25,
            height=1.7,
            net=7,
            net_name="VBUS",
            layer=Layer.F_CU,
            ref="C10",
            pin="1",
        )
        grid.add_pad(pad)

        gx, gy = _is_metal_cell(grid, pad)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]

        assert cell.pad_blocked is True
        assert cell.is_obstacle is True, (
            "Issue #2915: isolated SMD-rect pad metal cell must be is_obstacle=True"
        )
        assert grid.is_blocked(gx, gy, Layer.F_CU, net=42)

    def test_isolated_smd_round_pad_is_obstacle(self, jlcpcb_rules):
        """SMD round-equivalent pad (square aspect, 0.5 mm).

        ``RoutingGrid`` treats round pads as their bounding rectangle for
        grid-blocking purposes; this exercises the equal-axis case.
        """
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=-3.0,
            y=-3.0,
            width=0.5,
            height=0.5,
            net=11,
            net_name="MISO",
            layer=Layer.F_CU,
            ref="J5",
            pin="3",
        )
        grid.add_pad(pad)

        gx, gy = _is_metal_cell(grid, pad)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]

        assert cell.pad_blocked is True
        assert cell.is_obstacle is True, (
            "Issue #2915: isolated SMD-round pad metal cell must be is_obstacle=True"
        )
        assert grid.is_blocked(gx, gy, Layer.F_CU, net=42)

    def test_isolated_oval_tht_pad_is_obstacle(self, jlcpcb_rules):
        """Oval THT pad (audio-jack style, 2.0 mm x 3.0 mm with drill).

        Oval THT pads use their bounding rectangle for grid-blocking.
        """
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=5.0,
            y=-5.0,
            width=2.0,
            height=3.0,
            net=13,
            net_name="AUDIO_L",
            layer=Layer.F_CU,
            through_hole=True,
            drill=1.2,
            ref="J3",
            pin="1",
        )
        grid.add_pad(pad)

        gx, gy = _is_metal_cell(grid, pad)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][gy][gx]

        assert cell.pad_blocked is True
        assert cell.is_obstacle is True, (
            "Issue #2915: isolated oval THT pad metal cell must be is_obstacle=True"
        )
        assert grid.is_blocked(gx, gy, Layer.F_CU, net=42)

    def test_own_net_can_still_traverse_isolated_pad_metal(self, jlcpcb_rules):
        """Same-net escape (#2880 / #2908) is preserved.

        Setting ``is_obstacle = True`` for pad metal must NOT block the
        pad's own net from reaching its pin. The pathfinder's
        ``different_net = cell.net != routing_net`` mask is False for
        own-net cells, so they stay passable.
        """
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=0.0,
            y=0.0,
            width=1.8,
            height=1.8,
            net=5,
            net_name="PHASE_B",
            layer=Layer.F_CU,
            through_hole=True,
            drill=1.0,
            ref="Q4",
            pin="2",
        )
        grid.add_pad(pad)

        gx, gy = _is_metal_cell(grid, pad)

        # A trace on the pad's own net must NOT be blocked from the pad
        # centre -- it has to be able to terminate there.
        assert grid.is_blocked(gx, gy, Layer.F_CU, net=5) is False, (
            "Own-net trace must remain passable through its own pad metal "
            "(same-net escape preserved -- #2880 / #2908 regression guard)"
        )


class TestPathfinderRejectsPadMetalForForeignNets:
    """Issue #2915 / #2920: the pathfinder's negotiated-mode
    ``static_blocks`` loophole must NOT release pad metal once a trace
    has touched it.

    We can't easily run a full A* in a unit test, but we can directly
    exercise ``compute_expanded_blocked`` which produces the bitmap the
    pathfinder consults.  Cells flagged ``is_obstacle = True`` survive
    the ``static_blocks`` release; cells flagged only ``blocked = True``
    do not.

    The post-fix invariant: for every isolated pad, the pad-centre cell
    is reported as blocked by ``compute_expanded_blocked`` for any
    foreign net.
    """

    @pytest.mark.parametrize(
        "pad_kwargs,description",
        [
            (
                dict(
                    width=1.8,
                    height=1.8,
                    through_hole=True,
                    drill=1.0,
                ),
                "THT rectangular (TO-220)",
            ),
            (
                dict(width=1.25, height=1.7),
                "SMD rectangular (0805)",
            ),
            (
                dict(width=0.5, height=0.5),
                "SMD round-equivalent (0402)",
            ),
            (
                dict(
                    width=2.0,
                    height=3.0,
                    through_hole=True,
                    drill=1.2,
                ),
                "Oval THT (audio jack)",
            ),
        ],
    )
    def test_foreign_net_blocked_in_negotiated_mode(
        self, jlcpcb_rules, pad_kwargs, description
    ):
        """Each isolated pad shape: ``compute_expanded_blocked`` (the
        negotiated-mode bitmap) blocks foreign nets at the pad centre.
        """
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=0.0,
            y=0.0,
            net=5,
            net_name="SIG",
            layer=Layer.F_CU,
            ref="REF",
            pin="1",
            **pad_kwargs,
        )
        grid.add_pad(pad)

        gx, gy = _is_metal_cell(grid, pad)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Foreign net 42, negotiated mode (allow_sharing=True).
        # Radius=0 isolates the per-cell decision from dilation effects.
        blocked = grid.compute_expanded_blocked(
            radius=0, net=42, allow_sharing=True
        )

        assert bool(blocked[layer_idx, gy, gx]) is True, (
            f"Issue #2915/#2920: {description} pad metal cell must block "
            f"foreign net in negotiated mode (was passable pre-fix because "
            f"is_obstacle=False let the static_blocks branch release it "
            f"on usage_count>0)"
        )

    def test_own_net_passable_in_negotiated_mode(self, jlcpcb_rules):
        """Same-net regression guard for negotiated mode (#2880 / #2908).

        The pad's own-net escape must still be permitted in negotiated
        mode -- ``different_net`` is False for own-net cells, so they
        are NOT in ``base_blocked`` regardless of ``is_obstacle``.
        """
        grid = _make_grid(jlcpcb_rules)
        pad = Pad(
            x=0.0,
            y=0.0,
            width=1.8,
            height=1.8,
            net=5,
            net_name="PHASE_B",
            layer=Layer.F_CU,
            through_hole=True,
            drill=1.0,
            ref="Q4",
            pin="2",
        )
        grid.add_pad(pad)

        gx, gy = _is_metal_cell(grid, pad)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Own net 5, negotiated mode.
        blocked = grid.compute_expanded_blocked(
            radius=0, net=5, allow_sharing=True
        )

        assert bool(blocked[layer_idx, gy, gx]) is False, (
            "Own-net trace must remain passable through its own pad metal "
            "in negotiated mode (same-net escape -- #2880 / #2908)"
        )


class TestTO220HBridgeFixture:
    """Issue #2920: synthetic TO-220 H-bridge fixture mirroring board-05.

    Three pads at 2.54 mm pitch (TO-220-3 footprint), 1.8 x 1.8 mm
    rectangular metal with 1.0 mm drill. None of the pad envelopes
    overlap at the chosen pitch (half-extent 0.9 + 0.127 mm clearance
    = 1.027 mm < 1.27 mm pitch midpoint), so every cell is first-touch
    when the pads are added in sequence.
    """

    def test_to220_three_pads_all_obstacle_on_first_touch(self, jlcpcb_rules):
        """All three pads of a TO-220-3 footprint produce pad-metal
        cells with ``is_obstacle = True`` on first touch.

        Pre-fix: any pad-centre cell whose neighbour envelope did not
        already paint it would land in the ``cell.net == 0`` first-touch
        branch and stay ``is_obstacle = False``.
        """
        grid = _make_grid(jlcpcb_rules)

        pads = [
            Pad(
                x=0.0,
                y=-2.54,
                width=1.8,
                height=1.8,
                net=10,
                net_name="GATE",
                layer=Layer.F_CU,
                through_hole=True,
                drill=1.0,
                ref="Q4",
                pin="1",
            ),
            Pad(
                x=0.0,
                y=0.0,
                width=1.8,
                height=1.8,
                net=11,
                net_name="DRAIN",
                layer=Layer.F_CU,
                through_hole=True,
                drill=1.0,
                ref="Q4",
                pin="2",
            ),
            Pad(
                x=0.0,
                y=2.54,
                width=1.8,
                height=1.8,
                net=12,
                net_name="SOURCE",
                layer=Layer.F_CU,
                through_hole=True,
                drill=1.0,
                ref="Q4",
                pin="3",
            ),
        ]

        for pad in pads:
            grid.add_pad(pad)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        for pad in pads:
            gx, gy = grid.world_to_grid(pad.x, pad.y)
            cell = grid.grid[layer_idx][gy][gx]
            assert cell.pad_blocked is True, (
                f"TO-220 pad {pad.ref}.{pad.pin} (net={pad.net}) center "
                f"must be pad_blocked"
            )
            assert cell.is_obstacle is True, (
                f"Issue #2920: TO-220 pad {pad.ref}.{pad.pin} (net={pad.net}) "
                f"center must be is_obstacle=True on first touch -- this is "
                f"the exact failure mode of board-05 PHASE_B clipping Q4/Q6 "
                f"PHASE_C pads"
            )

    def test_foreign_net_blocked_across_all_to220_pads(self, jlcpcb_rules):
        """A foreign net (e.g. PHASE_B trace probing Q6's PHASE_C pads)
        must be blocked at every TO-220 pad centre in negotiated mode.

        This is the regression invariant for #2920: PHASE_B → Q4/Q6
        PHASE_C cluster drops to zero violations once pad metal is
        unconditionally ``is_obstacle = True``.
        """
        grid = _make_grid(jlcpcb_rules)

        pads = [
            Pad(
                x=0.0,
                y=-2.54,
                width=1.8,
                height=1.8,
                net=10,
                net_name="GATE",
                layer=Layer.F_CU,
                through_hole=True,
                drill=1.0,
                ref="Q6",
                pin="1",
            ),
            Pad(
                x=0.0,
                y=0.0,
                width=1.8,
                height=1.8,
                net=11,
                net_name="PHASE_C",
                layer=Layer.F_CU,
                through_hole=True,
                drill=1.0,
                ref="Q6",
                pin="2",
            ),
            Pad(
                x=0.0,
                y=2.54,
                width=1.8,
                height=1.8,
                net=12,
                net_name="SOURCE",
                layer=Layer.F_CU,
                through_hole=True,
                drill=1.0,
                ref="Q6",
                pin="3",
            ),
        ]
        for pad in pads:
            grid.add_pad(pad)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        # PHASE_B trace (foreign net 99) probing Q6.
        blocked = grid.compute_expanded_blocked(
            radius=0, net=99, allow_sharing=True
        )

        for pad in pads:
            gx, gy = grid.world_to_grid(pad.x, pad.y)
            assert bool(blocked[layer_idx, gy, gx]) is True, (
                f"Issue #2920: foreign net (PHASE_B analogue, net=99) must "
                f"be blocked at Q6.{pad.pin} (net={pad.net}) in negotiated "
                f"mode -- pre-fix, the static_blocks branch released this "
                f"cell as soon as one trace touched it"
            )
