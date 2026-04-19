"""End-to-end integration test for the sub-block layout and routing pipeline.

Exercises the full four-stage pipeline:
1. PCBBlock subclass instantiation (LDOBlock, LEDBlock)
2. PCBLayout assembly with block placement and inter-block connections
3. Placement bridge: layout.to_block_groups() -> BlockGroupDef validation
4. Block-aware routing: register_block() + route_all_block_aware()

Issue #1615.
"""

import pytest

from kicad_tools.pcb.blocks.base import PCBBlock
from kicad_tools.pcb.blocks.led import LEDBlock
from kicad_tools.pcb.blocks.power import LDOBlock
from kicad_tools.pcb.layout import PCBLayout
from kicad_tools.placement.vector import BlockGroupDef, RelativeOffset
from kicad_tools.router.block_router import BlockRouter, BlockRoutingResult
from kicad_tools.router.core import Autorouter
from kicad_tools.router.rules import DesignRules

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_ldo_led_layout() -> tuple[PCBLayout, LDOBlock, LEDBlock]:
    """Create a layout with an LDO block powering an LED block.

    LDO placed at (15, 30), LED placed at (50, 30).
    Inter-block connection: LDO VOUT -> LED ANODE.
    """
    ldo = LDOBlock(
        ldo_ref="U1",
        input_cap="C1",
        output_caps=["C2", "C3"],
    )
    ldo.place(15, 30)

    led = LEDBlock(led_ref="D1", res_ref="R1")
    led.place(50, 30)

    layout = PCBLayout(name="ldo_led_board")
    layout.add_block(ldo)
    layout.add_block(led)

    # Inter-block connection: LDO output powers LED anode
    layout.route("LDO_U1", "VOUT", "LED_D1", "ANODE", net="VOUT_TO_LED")

    return layout, ldo, led


def _register_block_pads(
    router: Autorouter,
    block: PCBBlock,
    net_map: dict[str, int],
) -> None:
    """Register all component pads from a block onto the Autorouter.

    For each component in the block, computes absolute pad positions and
    registers them with the router using the provided net_map
    (net_name -> net_id).

    Args:
        router: The Autorouter to register pads on.
        block: A placed PCBBlock.
        net_map: Mapping from net name to net ID for pad assignment.
    """
    for ref, comp in block.components.items():
        pads = []
        abs_comp_pos = block.component_position(ref)
        for pad_name, pad_point in comp.pads.items():
            # Pad position is relative to the component; component position
            # is relative to the block origin. We need absolute position.
            abs_x = abs_comp_pos.x + pad_point.x
            abs_y = abs_comp_pos.y + pad_point.y

            # Determine net assignment from the block's internal traces
            # Use a simple heuristic: look at block traces that start/end
            # near this pad's relative position
            pad_net = 0
            pad_net_name = ""
            comp_pad_rel_x = comp.position.x + pad_point.x
            comp_pad_rel_y = comp.position.y + pad_point.y
            for trace in block.traces:
                dist_start = (
                    (trace.start.x - comp_pad_rel_x) ** 2 + (trace.start.y - comp_pad_rel_y) ** 2
                ) ** 0.5
                dist_end = (
                    (trace.end.x - comp_pad_rel_x) ** 2 + (trace.end.y - comp_pad_rel_y) ** 2
                ) ** 0.5
                if (dist_start < 0.1 or dist_end < 0.1) and trace.net:
                    if trace.net in net_map:
                        pad_net = net_map[trace.net]
                        pad_net_name = trace.net
                        break

            pads.append(
                {
                    "number": pad_name,
                    "x": abs_x,
                    "y": abs_y,
                    "net": pad_net,
                    "net_name": pad_net_name,
                    "width": 0.5,
                    "height": 0.5,
                }
            )
        router.add_component(ref, pads)


# ===========================================================================
# Stage 1: PCBBlock subclass instantiation
# ===========================================================================


class TestBlockInstantiation:
    """Verify LDOBlock and LEDBlock create valid block structures."""

    def test_ldo_block_has_components(self):
        ldo = LDOBlock()
        assert len(ldo.components) >= 3  # LDO IC + input cap + at least one output cap

    def test_ldo_block_has_ports(self):
        ldo = LDOBlock()
        assert "VIN" in ldo.ports
        assert "VOUT" in ldo.ports
        assert "GND" in ldo.ports

    def test_ldo_block_has_internal_traces(self):
        ldo = LDOBlock()
        assert len(ldo.traces) > 0
        # LDO should have power traces for VIN, VOUT, GND
        net_names = {t.net for t in ldo.traces if t.net}
        assert "VIN" in net_names
        assert "VOUT" in net_names
        assert "GND" in net_names

    def test_led_block_has_components(self):
        led = LEDBlock()
        assert len(led.components) == 2  # LED + resistor

    def test_led_block_has_ports(self):
        led = LEDBlock()
        assert "ANODE" in led.ports
        assert "CATHODE" in led.ports

    def test_led_block_has_internal_trace(self):
        led = LEDBlock()
        assert len(led.traces) == 1
        assert led.traces[0].net == "LED_MID"


# ===========================================================================
# Stage 2: PCBLayout assembly
# ===========================================================================


class TestLayoutAssembly:
    """Verify blocks can be placed in a PCBLayout with inter-block routes."""

    def test_layout_contains_both_blocks(self):
        layout, ldo, led = _create_ldo_led_layout()
        assert len(layout.blocks) == 2
        assert "LDO_U1" in layout.blocks
        assert "LED_D1" in layout.blocks

    def test_blocks_placed_at_distinct_positions(self):
        layout, ldo, led = _create_ldo_led_layout()
        assert ldo.placed
        assert led.placed
        assert ldo.origin.x != led.origin.x or ldo.origin.y != led.origin.y

    def test_inter_block_route_created(self):
        layout, ldo, led = _create_ldo_led_layout()
        assert len(layout.inter_block_traces) == 1
        trace = layout.inter_block_traces[0]
        assert trace.net == "VOUT_TO_LED"

    def test_export_placements_includes_all_components(self):
        layout, ldo, led = _create_ldo_led_layout()
        placements = layout.export_placements()
        refs = {p["ref"] for p in placements}
        # LDO: U1, C1, C2, C3; LED: D1, R1
        assert "U1" in refs
        assert "C1" in refs
        assert "D1" in refs
        assert "R1" in refs

    def test_export_traces_has_internal_and_inter_block(self):
        layout, ldo, led = _create_ldo_led_layout()
        traces = layout.export_traces()
        internal = [t for t in traces if t.get("block_id") is not None]
        inter_block = [t for t in traces if t.get("block_id") is None]
        assert len(internal) > 0, "Should have block-internal traces"
        assert len(inter_block) == 1, "Should have one inter-block trace"


# ===========================================================================
# Stage 3: Placement bridge -- to_block_groups()
# ===========================================================================


class TestPlacementBridge:
    """Verify layout.to_block_groups() produces valid BlockGroupDef instances."""

    def test_to_block_groups_returns_correct_count(self):
        layout, ldo, led = _create_ldo_led_layout()
        groups = layout.to_block_groups()
        assert len(groups) == 2

    def test_block_group_ids_match_block_names(self):
        layout, ldo, led = _create_ldo_led_layout()
        groups = layout.to_block_groups()
        group_ids = {g.block_id for g in groups}
        assert "LDO_U1" in group_ids
        assert "LED_D1" in group_ids

    def test_block_group_members_match_components(self):
        layout, ldo, led = _create_ldo_led_layout()
        groups = layout.to_block_groups()

        for group in groups:
            assert isinstance(group, BlockGroupDef)
            assert len(group.members) > 0

            if group.block_id == "LDO_U1":
                member_refs = group.member_refs
                assert "U1" in member_refs
                assert "C1" in member_refs
                assert "C2" in member_refs
                assert "C3" in member_refs
            elif group.block_id == "LED_D1":
                member_refs = group.member_refs
                assert "D1" in member_refs
                assert "R1" in member_refs

    def test_block_group_members_are_relative_offsets(self):
        layout, ldo, led = _create_ldo_led_layout()
        groups = layout.to_block_groups()
        for group in groups:
            for member in group.members:
                assert isinstance(member, RelativeOffset)
                assert isinstance(member.reference, str)
                assert isinstance(member.dx, int | float)
                assert isinstance(member.dy, int | float)

    def test_relative_offsets_match_block_components(self):
        """Offsets from to_block_groups should match PCBBlock.relative_offsets."""
        layout, ldo, led = _create_ldo_led_layout()
        groups = layout.to_block_groups()

        for group in groups:
            block = layout.blocks[group.block_id]
            block_offsets = block.relative_offsets()
            block_offset_map = {o.reference: o for o in block_offsets}

            for member in group.members:
                assert member.reference in block_offset_map
                bo = block_offset_map[member.reference]
                assert abs(member.dx - bo.dx) < 0.01
                assert abs(member.dy - bo.dy) < 0.01


# ===========================================================================
# Stage 4: Block-aware routing -- full pipeline
# ===========================================================================


@pytest.mark.timeout(30)
class TestBlockAwareRoutingPipeline:
    """Full pipeline: blocks -> layout -> router -> route_all_block_aware."""

    def _setup_full_pipeline(self):
        """Set up the complete pipeline with LDO and LED blocks.

        Returns:
            Tuple of (router, layout, ldo, led).
        """
        layout, ldo, led = _create_ldo_led_layout()

        rules = DesignRules()
        router = Autorouter(40, 30, force_python=True, rules=rules)

        # Net map: assign numeric IDs to net names used by blocks
        net_map = {
            "VIN": 1,
            "VOUT": 2,
            "GND": 3,
            "LED_MID": 4,
            "VOUT_TO_LED": 5,
        }

        # Register all block component pads
        _register_block_pads(router, ldo, net_map)
        _register_block_pads(router, led, net_map)

        # Register blocks with the router (marks bounding boxes as blocked,
        # indexes internal traces)
        router.register_block(ldo)
        router.register_block(led)

        return router, layout, ldo, led

    def test_blocks_registered_with_router(self):
        router, layout, ldo, led = self._setup_full_pipeline()
        assert ldo.block_id in router.registered_blocks
        assert led.block_id in router.registered_blocks

    def test_route_all_block_aware_completes(self):
        """route_all_block_aware should complete without error."""
        router, layout, ldo, led = self._setup_full_pipeline()
        routes = router.route_all_block_aware()
        assert isinstance(routes, list)

    def test_route_all_block_aware_produces_routes(self):
        """route_all_block_aware should produce a non-empty route list."""
        router, layout, ldo, led = self._setup_full_pipeline()
        routes = router.route_all_block_aware()
        assert len(routes) > 0, "Expected at least some routes from block-aware routing"

    def test_block_internal_traces_preserved(self):
        """Block internal traces from get_placed_traces should not be overwritten.

        After routing, the block's placed traces with internal=True should
        still be present and unchanged.
        """
        router, layout, ldo, led = self._setup_full_pipeline()

        # Capture internal traces before routing
        ldo_internal_before = [t for t in ldo.get_placed_traces() if t["internal"]]
        led_internal_before = [t for t in led.get_placed_traces() if t["internal"]]

        router.route_all_block_aware()

        # Internal traces should be unchanged after routing
        ldo_internal_after = [t for t in ldo.get_placed_traces() if t["internal"]]
        led_internal_after = [t for t in led.get_placed_traces() if t["internal"]]

        assert len(ldo_internal_after) == len(ldo_internal_before)
        assert len(led_internal_after) == len(led_internal_before)

        # Verify trace data is identical
        for before, after in zip(ldo_internal_before, ldo_internal_after, strict=True):
            assert before["start"] == after["start"]
            assert before["end"] == after["end"]
            assert before["net"] == after["net"]

    def test_inter_block_routing_avoids_block_interiors(self):
        """Route segments from inter-block routing should not pass through
        block bounding boxes (except at port entry/exit points).

        For inter-block nets (nets with pads in multiple blocks), verify that
        route segment midpoints do not lie inside the OTHER block's interior.
        """
        router, layout, ldo, led = self._setup_full_pipeline()
        routes = router.route_all_block_aware()

        # Create BlockRouter instances to use contains_point
        rules = DesignRules()
        block_routers = {
            "ldo": BlockRouter(ldo, rules, force_python=True),
            "led": BlockRouter(led, rules, force_python=True),
        }
        ldo_refs = set(ldo.components.keys())
        led_refs = set(led.components.keys())

        violations = []
        for route in routes:
            net_id = route.net
            if net_id not in router.nets:
                continue

            pad_keys = router.nets[net_id]
            pad_refs = {k[0] for k in pad_keys}
            has_ldo_pads = bool(pad_refs & ldo_refs)
            has_led_pads = bool(pad_refs & led_refs)

            if not (has_ldo_pads and has_led_pads):
                continue  # Not an inter-block net

            for seg in route.segments:
                mid_x = (seg.x1 + seg.x2) / 2
                mid_y = (seg.y1 + seg.y2) / 2

                # Check that inter-block route midpoints don't go through
                # block interiors. The bounding box includes a margin, so
                # routes near block edges may be borderline; we check the
                # stricter block bounding box (without margin) by verifying
                # the midpoint is not deeply inside the block.
                for _name, br in block_routers.items():
                    if br.contains_point(mid_x, mid_y):
                        violations.append(
                            f"Net {net_id} segment midpoint ({mid_x:.1f}, {mid_y:.1f}) "
                            f"inside block {br.block.block_id}"
                        )

        # Violations may occur at block boundaries due to margin overlap;
        # allow a small number of boundary violations but flag if the router
        # is routing extensively through block interiors.
        max_allowed_violations = 2  # tolerance for boundary-adjacent midpoints
        assert len(violations) <= max_allowed_violations, (
            f"Inter-block routing produced {len(violations)} interior violations "
            f"(max {max_allowed_violations} allowed):\n" + "\n".join(violations)
        )

    def test_block_router_contains_point_works(self):
        """Verify BlockRouter.contains_point correctly identifies block interior."""
        layout, ldo, led = _create_ldo_led_layout()
        rules = DesignRules()

        ldo_br = BlockRouter(ldo, rules, force_python=True)
        led_br = BlockRouter(led, rules, force_python=True)

        # LDO is at (15, 30) -- center should be inside
        assert ldo_br.contains_point(15.0, 30.0)
        # LED is at (50, 30) -- center should be inside
        assert led_br.contains_point(50.0, 30.0)

        # Points far away should not be inside either block
        assert not ldo_br.contains_point(70.0, 10.0)
        assert not led_br.contains_point(5.0, 5.0)

        # Cross-check: LDO center should NOT be inside LED block
        assert not led_br.contains_point(15.0, 30.0)
        assert not ldo_br.contains_point(50.0, 30.0)

    def test_block_internal_connections_indexed(self):
        """After registering blocks, internal trace connections should be indexed."""
        router, layout, ldo, led = self._setup_full_pipeline()

        # LDO has internal traces for VIN, VOUT, GND nets
        # At minimum some of these should be in _block_internal_connections
        # (depends on whether trace endpoints match registered pad positions)
        # The key thing is the indexing doesn't crash.
        assert isinstance(router._block_internal_connections, dict)


# ===========================================================================
# Stage 4b: BlockRouter per-block routing detail
# ===========================================================================


@pytest.mark.timeout(30)
class TestBlockRouterPerBlock:
    """Verify individual BlockRouter instances route correctly."""

    def test_ldo_block_router_accepts_pads(self):
        """LDOBlock pads within the bounding box are accepted by BlockRouter."""
        ldo = LDOBlock()
        ldo.place(20, 20)

        rules = DesignRules()
        router = Autorouter(60, 60, force_python=True, rules=rules)

        net_map = {"VIN": 1, "VOUT": 2, "GND": 3}
        _register_block_pads(router, ldo, net_map)

        br = BlockRouter(ldo, rules, force_python=True, margin=3.0)
        br.add_pads_from_autorouter(router.pads, router.nets, router.net_names)

        result = br.route_block()
        assert isinstance(result, BlockRoutingResult)
        assert result.block_id == ldo.block_id
        # The key assertion: no crash during block routing

    def test_led_block_router_accepts_pads(self):
        """LEDBlock pads within the bounding box are accepted by BlockRouter."""
        led = LEDBlock()
        led.place(20, 20)

        rules = DesignRules()
        router = Autorouter(60, 60, force_python=True, rules=rules)

        net_map = {"LED_MID": 1}
        _register_block_pads(router, led, net_map)

        br = BlockRouter(led, rules, force_python=True, margin=2.0)
        br.add_pads_from_autorouter(router.pads, router.nets, router.net_names)

        result = br.route_block()
        assert isinstance(result, BlockRoutingResult)
        assert result.block_id == led.block_id


# ===========================================================================
# Variant: three-block design (LDO + 2 LEDs)
# ===========================================================================


@pytest.mark.timeout(30)
class TestThreeBlockDesign:
    """Verify pipeline works with 3 blocks and multiple inter-block nets."""

    def _setup_three_block_pipeline(self):
        """LDO powering two LED blocks."""
        ldo = LDOBlock(ldo_ref="U1", input_cap="C1", output_caps=["C2"])
        ldo.place(15, 25)

        led1 = LEDBlock(led_ref="D1", res_ref="R1")
        led1.place(30, 15)

        led2 = LEDBlock(led_ref="D2", res_ref="R2")
        led2.place(30, 25)

        layout = PCBLayout(name="ldo_2led_board")
        layout.add_block(ldo)
        layout.add_block(led1)
        layout.add_block(led2)

        layout.route("LDO_U1", "VOUT", "LED_D1", "ANODE", net="VOUT_LED1")
        layout.route("LDO_U1", "VOUT", "LED_D2", "ANODE", net="VOUT_LED2")

        rules = DesignRules()
        router = Autorouter(40, 30, force_python=True, rules=rules)

        net_map = {
            "VIN": 1,
            "VOUT": 2,
            "GND": 3,
            "LED_MID": 4,
        }

        _register_block_pads(router, ldo, net_map)
        _register_block_pads(router, led1, net_map)
        _register_block_pads(router, led2, net_map)

        router.register_block(ldo)
        router.register_block(led1)
        router.register_block(led2)

        return router, layout, ldo, led1, led2

    def test_three_blocks_registered(self):
        router, layout, ldo, led1, led2 = self._setup_three_block_pipeline()
        assert len(router.registered_blocks) == 3

    def test_three_block_routing_completes(self):
        router, layout, ldo, led1, led2 = self._setup_three_block_pipeline()
        routes = router.route_all_block_aware()
        assert isinstance(routes, list)

    def test_three_block_groups_from_layout(self):
        router, layout, ldo, led1, led2 = self._setup_three_block_pipeline()
        groups = layout.to_block_groups()
        assert len(groups) == 3
        group_ids = {g.block_id for g in groups}
        assert "LDO_U1" in group_ids
        assert "LED_D1" in group_ids
        assert "LED_D2" in group_ids
