"""Tests for issue #2695: in-pad via escape on fine-pitch LQFP/QFP packages.

Verifies that the in-pad escape strategy (originally added for SSOP/TSSOP in
PR #2608) is also reached from the QFP/LQFP/TQFP dispatcher
(``_escape_qfp_alternating``) when:

1. The package pitch is 0.5mm or finer (``pin_pitch <= 0.55``), and
2. The manufacturer supports via-in-pad (``via_in_pad_supported=True``).

Pre-#2695 behavior (preserved when manufacturer doesn't support in-pad):
- Inner LQFP-48 pins fail surface escape and are deferred to the main
  router, where they typically remain unrouted because the package
  perimeter is fully blocked.

Post-#2695 behavior (when via_in_pad_supported=True):
- Inner LQFP-48 pins that fail surface clearance fall through to an in-pad
  via escape: a via is placed dead-centre on the pad and the escape
  segment runs from the via on an inner signal layer (In1.Cu on 4-layer
  boards, B.Cu on 2-layer boards).
"""

from __future__ import annotations

from kicad_tools.router.escape import EscapeRouter, PackageType
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


def _make_lqfp48_0p5mm(
    ref: str = "U2",
    pitch: float = 0.5,
    pad_short: float = 0.30,
    pad_long: float = 1.50,
    body_size: float | None = None,
    pad_stick_out: float = 0.85,
    pads_per_edge: int = 12,
    start_net: int = 1,
) -> list[Pad]:
    """Build a synthetic LQFP-48 footprint at 0.5mm pitch.

    Default geometry mimics ``Package_QFP:LQFP-48_7x7mm_P0.5mm``:
    - 48 pins total, 12 per edge.
    - Pitch: 0.5mm between adjacent pins along an edge.
    - Pad dimensions: ~0.30mm (short axis, along edge) x ~1.50mm (long axis,
      perpendicular -- sticks out from package body).
    - Body: 7x7mm; pad center-line sits 0.85mm outside the body edge so
      half the pad sits over the lead and half sticks out.

    Pin numbering follows the standard LQFP convention:
    - Pin 1 starts at the top of the west edge (after the corner marker)
    - Numbering proceeds CCW: west (top->bottom), south (left->right),
      east (bottom->top), north (right->left)

    Each pad has a unique net so the escape router does not group them.

    Args:
        body_size: Package body size in mm.  Defaults to a value that
            keeps corner-to-corner pad spacing >= 1.5x pitch (so the
            min-pitch detection picks up the in-edge pin pitch rather
            than a coincidentally close corner pair).
    """
    # Auto-size the body so corner pad spacing is well above in-edge pitch.
    # Each edge spans (pads_per_edge - 1) * pitch.  Body needs >= span +
    # comfortable corner gap (~3x pitch) so corner pads of adjacent
    # edges are far enough apart.
    span = (pads_per_edge - 1) * pitch
    if body_size is None:
        body_size = span + 3.0 * pitch + 2.0 * pad_long
    half_body = body_size / 2
    pad_center_offset = half_body + pad_stick_out / 2
    pads: list[Pad] = []
    pin_no = 1
    half_span = span / 2

    # WEST edge: vertical pads, sorted top->bottom.  Long axis = X
    # (perpendicular to edge), short axis = Y (along edge).
    # Pin 1 starts at the top of the west edge.
    for i in range(pads_per_edge):
        y = half_span - i * pitch  # top -> bottom
        pads.append(
            Pad(
                x=-pad_center_offset,
                y=y,
                width=pad_long,    # long axis along X (perpendicular to edge)
                height=pad_short,  # short axis along Y (along edge)
                net=start_net + pin_no - 1,
                net_name=f"NET{start_net + pin_no - 1}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # SOUTH edge: horizontal pads, sorted left->right.  Long axis = Y,
    # short axis = X.
    for i in range(pads_per_edge):
        x = -half_span + i * pitch  # left -> right
        pads.append(
            Pad(
                x=x,
                y=-pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=start_net + pin_no - 1,
                net_name=f"NET{start_net + pin_no - 1}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # EAST edge: vertical pads, sorted bottom->top.
    for i in range(pads_per_edge):
        y = -half_span + i * pitch  # bottom -> top
        pads.append(
            Pad(
                x=pad_center_offset,
                y=y,
                width=pad_long,
                height=pad_short,
                net=start_net + pin_no - 1,
                net_name=f"NET{start_net + pin_no - 1}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # NORTH edge: horizontal pads, sorted right->left.
    for i in range(pads_per_edge):
        x = half_span - i * pitch  # right -> left
        pads.append(
            Pad(
                x=x,
                y=pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=start_net + pin_no - 1,
                net_name=f"NET{start_net + pin_no - 1}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    return pads


def _make_rules(manufacturer: str | None = None) -> DesignRules:
    """Build DesignRules matching board 04 production settings."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        manufacturer=manufacturer,
    )


def _make_grid(rules: DesignRules, layer_stack: LayerStack | None = None) -> RoutingGrid:
    return RoutingGrid(
        width=30.0,
        height=30.0,
        rules=rules,
        origin_x=-15.0,
        origin_y=-15.0,
        layer_stack=layer_stack or LayerStack.four_layer_sig_sig_gnd_pwr(),
    )


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------


class TestLqfp48InPadEscape:
    """Issue #2695: 0.5mm-pitch LQFP-48 in-pad escape on capable manufacturers."""

    def test_lqfp48_classifies_as_qfp_family(self):
        """The synthetic LQFP-48 fixture should be detected as one of
        QFP/TQFP/QFN (which all route through ``_escape_qfp_alternating``).
        """
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_0p5mm()

        package_info = escape_router.analyze_package(pads)
        assert package_info.package_type in (
            PackageType.QFP, PackageType.TQFP, PackageType.QFN,
        ), (
            f"Expected QFP-family classification, got {package_info.package_type}"
        )
        assert package_info.pin_pitch <= 0.55, (
            f"Expected pin_pitch <= 0.55, got {package_info.pin_pitch}"
        )

    def test_in_pad_escape_generated_when_supported(self):
        """With manufacturer=jlcpcb-tier1, inner LQFP-48 pins that fail
        surface escape should fall through to in-pad via escape."""
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_0p5mm()

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        # We expect at least some in-pad vias since 0.5mm-pitch with
        # 0.2mm trace + 0.15mm clearance leaves no copper for parallel
        # arms of the alternating pattern.
        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert len(in_pad_vias) >= 1, (
            f"Expected at least one in-pad via for 0.5mm-pitch LQFP-48 "
            f"with via-in-pad enabled; got {len(in_pad_vias)} of {len(escapes)} "
            f"escapes."
        )

        # Every in-pad via must sit dead-centre on its pad (within 1um).
        for esc in in_pad_vias:
            assert esc.via is not None
            assert abs(esc.via.x - esc.pad.x) < 0.001
            assert abs(esc.via.y - esc.pad.y) < 0.001
            # 4-layer signal-signal-ground-power stack: inner escape lands
            # on In1.Cu (a SIGNAL layer).
            assert esc.via.layers[0] == esc.pad.layer
            assert esc.via.layers[1] == Layer.IN1_CU

    def test_no_in_pad_escape_when_unsupported(self):
        """With manufacturer=jlcpcb (no via-in-pad capability), no in-pad
        vias are produced.  Inner pins defer as before -- byte-identical
        to pre-#2695 behaviour for users who haven't opted into the
        capability+ tier."""
        rules = _make_rules(manufacturer="jlcpcb")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_0p5mm()

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert in_pad_vias == [], (
            f"Default JLCPCB profile must NOT produce in-pad vias on "
            f"LQFP-48; got {len(in_pad_vias)}."
        )

    def test_no_in_pad_escape_when_manufacturer_is_none(self):
        """With manufacturer=None (the default), no in-pad escapes."""
        rules = _make_rules(manufacturer=None)
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_0p5mm()

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert in_pad_vias == []

    def test_in_pad_escape_uses_pcbway_when_supported(self):
        """PCBWay is the other tier with ``via_in_pad_supported=True``;
        verify the rescue fires for it too."""
        rules = _make_rules(manufacturer="pcbway")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_0p5mm()

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert len(in_pad_vias) >= 1, (
            "PCBWay (also via_in_pad_supported=True) must trigger the "
            "in-pad rescue for LQFP-48 0.5mm-pitch inner pins."
        )

    def test_in_pad_skipped_at_0p65mm_pitch_qfp(self):
        """Wider-pitch QFP packages (pitch >= 0.65mm) should NOT trigger
        the in-pad rescue -- they have enough room for surface escape
        already and would just pay an unnecessary via cost."""
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        # 0.65mm pitch -- above the 0.55mm rescue threshold.
        pads = _make_lqfp48_0p5mm(pitch=0.65)

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        # At 0.65mm pitch the perpendicular-only scheme provides ample
        # room (0.65 - 0.2 - 0.15 = 0.30mm gap each side); no need for
        # via-in-pad and we should not pay for it.
        assert in_pad_vias == [], (
            f"At 0.65mm pitch the in-pad rescue should not fire (surface "
            f"escape has room); got {len(in_pad_vias)} in-pad vias."
        )

    def test_in_pad_escape_on_2layer_board(self):
        """On a 2-layer board with via-in-pad enabled, in-pad vias land on
        B.Cu (the only available alternate signal layer)."""
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules, layer_stack=LayerStack.two_layer())
        escape_router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_0p5mm()

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert len(in_pad_vias) >= 1, (
            "Expected at least one in-pad via on the 2-layer LQFP fixture."
        )
        for esc in in_pad_vias:
            assert esc.via is not None
            assert esc.via.layers[1] == Layer.B_CU

    def test_in_pad_skipped_when_pad_too_small(self):
        """Pads too small to host the via + annular ring should bail
        gracefully and leave the pin deferred (no in-pad via emitted)."""
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        # Both axes below the required dim for a 0.3mm drill + annular.
        pads = _make_lqfp48_0p5mm(pad_short=0.20, pad_long=0.30)

        package_info = escape_router.analyze_package(pads)
        escapes = escape_router.generate_escapes(package_info)

        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]
        assert in_pad_vias == [], (
            "In-pad escape should bail out gracefully when pads are too "
            "small for drill + annular ring."
        )

    def test_at_least_one_inner_pin_rescued_per_edge(self):
        """With via-in-pad enabled on the 0.5mm LQFP-48 fixture, we expect
        in-pad vias distributed across multiple edges (not all bunched on
        one).  This guards against an edge-iteration bug where only the
        first edge gets the rescue.

        The exact count depends on how many surface escapes fail clearance
        for the current pad geometry; we just require coverage on at
        least 2 of the 4 edges.
        """
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        escape_router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_0p5mm()

        package_info = escape_router.analyze_package(pads)
        center_x, center_y = package_info.center
        escapes = escape_router.generate_escapes(package_info)

        in_pad_vias = [
            e for e in escapes
            if e.via is not None and getattr(e.via, "in_pad", False)
        ]

        # Categorise each in-pad via by which edge its pad sits on.
        edges: set[str] = set()
        for esc in in_pad_vias:
            pad = esc.pad
            dx = pad.x - center_x
            dy = pad.y - center_y
            if abs(dx) > abs(dy):
                edges.add("east" if dx > 0 else "west")
            else:
                edges.add("north" if dy > 0 else "south")

        # Skip the assertion if no in-pad vias were produced at all (the
        # other tests guard the >= 1 case); here we only check
        # distribution when the rescue fires.
        if in_pad_vias:
            assert len(edges) >= 2, (
                f"In-pad rescue should cover at least 2 edges; "
                f"got {len(in_pad_vias)} vias on edges {edges}."
            )
