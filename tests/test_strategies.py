"""Tests for design strategies — focused on MCUCoreStrategy crystal placement.

These tests exercise the XTAL-pin-side introspection added in #2923 so
that ``MCUCoreStrategy.compute_placements`` places a crystal on whichever
package edge actually carries the XTAL/OSC pads. The previous behaviour
hardcoded 270 deg ("above MCU") regardless of package geometry, which
broke routing whenever the MCU's XTAL pins were not on the north edge of
the package (e.g. TQFP-32 XTAL pins on the west edge).

The tests build synthetic MCU footprints (no real KiCad library files
needed) and assert that the crystal lands on the correct cardinal edge
under each (XTAL-edge x footprint-rotation) combination, and that the
fallback to 270 deg still triggers when no XTAL pads are present.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pytest

from kicad_tools.design.strategies import _XTAL_NET_PATTERN, MCUCoreStrategy

# ---------------------------------------------------------------------------
# Minimal mock objects
#
# We mock just enough of the Pad / Footprint / PCB surface to drive
# MCUCoreStrategy._xtal_edge_angle. The real schema classes carry many
# fields the strategy never touches (sexp_node, board_origin, layers, ...)
# so a focused mock is safer and faster than constructing real instances.
# ---------------------------------------------------------------------------


@dataclass
class MockPad:
    number: str
    position: tuple[float, float]
    net_name: str = ""


@dataclass
class MockFootprint:
    reference: str
    position: tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0
    pads: list[MockPad] = field(default_factory=list)


@dataclass
class MockPCB:
    footprints: list[MockFootprint] = field(default_factory=list)


def _make_tqfp32_mcu(
    xtal_edge: str,
    rotation: float = 0.0,
    *,
    xtal_net_names: tuple[str, str] = ("XTAL1", "XTAL2"),
    ref: str = "U1",
    half_size: float = 4.5,
) -> MockFootprint:
    """Build a synthetic TQFP-32-ish MCU footprint.

    The TQFP-32 package is a square with 8 pins per edge. We model just
    enough pads to make a centroid: two XTAL pads on the requested edge
    plus four "filler" pads with VCC/GND/IO nets on the other three
    edges (to verify the centroid logic ignores non-XTAL nets).

    Args:
        xtal_edge: One of {"N", "S", "E", "W"} — the *intended* package
            edge that carries the XTAL pads (before any footprint
            rotation is applied). North/South use screen-Y convention
            (north = negative Y in the local frame).
        rotation: Footprint rotation in degrees, applied at the package
            level. Positive CCW (KiCad convention).
        xtal_net_names: Net names for the two XTAL pads. Vary this in
            tests to exercise alternate naming (OSC_IN/OSC_OUT etc.).
        ref: Reference designator.
        half_size: Half the package side length (mm). XTAL pads sit
            exactly on the edge at +/- half_size.
    """
    edge_offsets = {
        "N": [(-1.0, -half_size), (1.0, -half_size)],
        "S": [(-1.0, half_size), (1.0, half_size)],
        "E": [(half_size, -1.0), (half_size, 1.0)],
        "W": [(-half_size, -1.0), (-half_size, 1.0)],
    }
    if xtal_edge not in edge_offsets:
        raise ValueError(f"Unknown xtal_edge: {xtal_edge}")

    pads: list[MockPad] = []
    for i, off in enumerate(edge_offsets[xtal_edge]):
        pads.append(MockPad(number=str(i + 1), position=off, net_name=xtal_net_names[i]))

    # Add some non-XTAL "noise" pads on the other three edges. These
    # must not influence the centroid (we filter by net-name pattern).
    noise_edges = {"N", "S", "E", "W"} - {xtal_edge}
    noise_nets = ["VCC", "GND", "PA0", "PA1"]
    for i, noise_edge in enumerate(noise_edges):
        ox, oy = edge_offsets[noise_edge][0]
        pads.append(
            MockPad(
                number=str(10 + i),
                position=(ox, oy),
                net_name=noise_nets[i % len(noise_nets)],
            )
        )

    return MockFootprint(reference=ref, position=(0.0, 0.0), rotation=rotation, pads=pads)


def _make_pcb_with_mcu(mcu: MockFootprint) -> MockPCB:
    return MockPCB(footprints=[mcu])


# ---------------------------------------------------------------------------
# Direct tests of _xtal_edge_angle
# ---------------------------------------------------------------------------


class TestXTALEdgeAngleDetection:
    """Per-edge / per-rotation introspection of XTAL pads."""

    @pytest.mark.parametrize(
        "xtal_edge,expected_angle",
        [
            ("N", 270.0),  # north => angle 270 (screen-Y: -y)
            ("S", 90.0),  # south => angle 90 (+y)
            ("E", 0.0),  # east  => angle 0 (+x)
            ("W", 180.0),  # west  => angle 180 (-x)
        ],
    )
    def test_xtal_edge_no_rotation(self, xtal_edge: str, expected_angle: float):
        """Each cardinal XTAL-pad edge produces the matching angle."""
        mcu = _make_tqfp32_mcu(xtal_edge=xtal_edge, rotation=0.0)
        pcb = _make_pcb_with_mcu(mcu)
        angle = MCUCoreStrategy._xtal_edge_angle(pcb, "U1")
        assert angle == expected_angle

    @pytest.mark.parametrize(
        "xtal_edge,rotation,expected_angle",
        [
            # North XTAL pads (-y locally) rotated 90 deg CCW =>
            # rotated to +x (east). Confirms rotation-awareness.
            ("N", 90.0, 0.0),
            # West XTAL pads (-x locally) rotated 90 deg CCW => -y (north).
            ("W", 90.0, 270.0),
            # West rotated 180 deg => east.
            ("W", 180.0, 0.0),
            # East rotated 270 deg => north (-y).
            ("E", 270.0, 270.0),
            # West rotated 90 deg CW (= -90 / 270 deg CCW) => south (+y).
            ("W", 270.0, 90.0),
        ],
    )
    def test_xtal_edge_with_rotation(self, xtal_edge: str, rotation: float, expected_angle: float):
        """Footprint rotation moves XTAL pads to a different world-frame edge.

        TQFP-32 packages have XTAL on the west edge. If the MCU is
        rotated 90 deg CCW, the XTAL pads end up on the south edge in the
        world frame. The placer must follow.
        """
        mcu = _make_tqfp32_mcu(xtal_edge=xtal_edge, rotation=rotation)
        pcb = _make_pcb_with_mcu(mcu)
        angle = MCUCoreStrategy._xtal_edge_angle(pcb, "U1")
        assert angle == expected_angle

    def test_osc_in_out_naming(self):
        """STM32-style OSC_IN/OSC_OUT pads are detected, not just XTAL*."""
        mcu = _make_tqfp32_mcu(
            xtal_edge="W",
            rotation=0.0,
            xtal_net_names=("OSC_IN", "OSC_OUT"),
        )
        pcb = _make_pcb_with_mcu(mcu)
        angle = MCUCoreStrategy._xtal_edge_angle(pcb, "U1")
        assert angle == 180.0

    def test_osc32_naming(self):
        """STM32-style OSC32_IN/OSC32_OUT (LSE) pads are detected."""
        mcu = _make_tqfp32_mcu(
            xtal_edge="E",
            rotation=0.0,
            xtal_net_names=("OSC32_IN", "OSC32_OUT"),
        )
        pcb = _make_pcb_with_mcu(mcu)
        angle = MCUCoreStrategy._xtal_edge_angle(pcb, "U1")
        assert angle == 0.0

    def test_hierarchical_net_names(self):
        """Hierarchical net paths like ``mcu/XTAL1`` are also detected."""
        mcu = _make_tqfp32_mcu(
            xtal_edge="N",
            rotation=0.0,
            xtal_net_names=("mcu/XTAL1", "mcu/XTAL2"),
        )
        pcb = _make_pcb_with_mcu(mcu)
        angle = MCUCoreStrategy._xtal_edge_angle(pcb, "U1")
        assert angle == 270.0

    def test_no_xtal_pads_returns_none(self):
        """Footprint without any XTAL/OSC-named pads returns None (fallback)."""
        mcu = MockFootprint(
            reference="U1",
            position=(0.0, 0.0),
            rotation=0.0,
            pads=[
                MockPad(number="1", position=(-4.5, 0.0), net_name="VCC"),
                MockPad(number="2", position=(4.5, 0.0), net_name="GND"),
                MockPad(number="3", position=(0.0, -4.5), net_name="PA0"),
            ],
        )
        pcb = _make_pcb_with_mcu(mcu)
        angle = MCUCoreStrategy._xtal_edge_angle(pcb, "U1")
        assert angle is None

    def test_missing_mcu_returns_none(self):
        """When the MCU reference can't be found in the PCB, returns None."""
        pcb = MockPCB(footprints=[])
        angle = MCUCoreStrategy._xtal_edge_angle(pcb, "U99")
        assert angle is None

    def test_missing_pads_returns_none(self):
        """Footprint with no pads at all returns None."""
        mcu = MockFootprint(reference="U1", pads=[])
        pcb = _make_pcb_with_mcu(mcu)
        angle = MCUCoreStrategy._xtal_edge_angle(pcb, "U1")
        assert angle is None

    def test_pcb_without_footprints_attr(self):
        """A PCB-like object without a ``footprints`` attribute is tolerated."""

        class BarePCB:
            pass

        angle = MCUCoreStrategy._xtal_edge_angle(BarePCB(), "U1")
        assert angle is None

    def test_xtal_pad_with_empty_net_name_ignored(self):
        """Pads with empty net_name are ignored even if numbered like XTAL pins."""
        mcu = MockFootprint(
            reference="U1",
            pads=[
                MockPad(number="1", position=(-4.5, 0.0), net_name=""),
                MockPad(number="2", position=(4.5, 0.0), net_name=""),
            ],
        )
        pcb = _make_pcb_with_mcu(mcu)
        angle = MCUCoreStrategy._xtal_edge_angle(pcb, "U1")
        assert angle is None

    def test_centroid_at_origin_returns_none(self):
        """If XTAL pads' centroid lands on the MCU centre, fall back to default.

        This catches degenerate inputs like a single XTAL pad placed at
        (0, 0) or two pads exactly symmetric about the centre.
        """
        mcu = MockFootprint(
            reference="U1",
            pads=[
                MockPad(number="1", position=(-2.0, 0.0), net_name="XTAL1"),
                MockPad(number="2", position=(2.0, 0.0), net_name="XTAL2"),
            ],
        )
        pcb = _make_pcb_with_mcu(mcu)
        angle = MCUCoreStrategy._xtal_edge_angle(pcb, "U1")
        assert angle is None


# ---------------------------------------------------------------------------
# End-to-end: compute_placements integration
# ---------------------------------------------------------------------------


class TestMCUCoreStrategyCrystalPlacement:
    """End-to-end integration via ``compute_placements``."""

    @pytest.mark.parametrize(
        "xtal_edge,expected_dx,expected_dy",
        [
            # Crystal sits 5mm from the MCU centre on the XTAL-pad edge.
            ("N", 0.0, -5.0),
            ("S", 0.0, 5.0),
            ("E", 5.0, 0.0),
            ("W", -5.0, 0.0),
        ],
    )
    def test_crystal_placed_on_xtal_edge(
        self, xtal_edge: str, expected_dx: float, expected_dy: float
    ):
        """Crystal lands on the MCU edge whose pads carry XTAL nets."""
        mcu = _make_tqfp32_mcu(xtal_edge=xtal_edge, rotation=0.0)
        pcb = _make_pcb_with_mcu(mcu)
        strategy = MCUCoreStrategy()

        anchor_pos = (50.0, 30.0)
        placements = strategy.compute_placements(
            components=["U1", "Y1", "C1", "C2"],
            anchor="U1",
            anchor_position=anchor_pos,
            pcb=pcb,
        )

        assert "Y1" in placements
        crystal = placements["Y1"]
        assert math.isclose(crystal.x - anchor_pos[0], expected_dx, abs_tol=1e-6)
        assert math.isclose(crystal.y - anchor_pos[1], expected_dy, abs_tol=1e-6)

    def test_crystal_follows_mcu_rotation(self):
        """Rotating the MCU 90 deg CCW moves XTAL pads from west to north.

        TQFP-32 XTAL pads are natively on the west edge (local -x). The
        standard 2D rotation matrix R(theta) with theta = +90 deg maps
        the vector (-1, 0) to (0, -1). In screen-Y conventions that's
        "north" (angle 270 deg in our 0=east, 90=south, 180=west,
        270=north scheme). The crystal must follow.
        """
        mcu = _make_tqfp32_mcu(xtal_edge="W", rotation=90.0)
        pcb = _make_pcb_with_mcu(mcu)
        strategy = MCUCoreStrategy()

        anchor_pos = (50.0, 30.0)
        placements = strategy.compute_placements(
            components=["U1", "Y1"],
            anchor="U1",
            anchor_position=anchor_pos,
            pcb=pcb,
        )

        crystal = placements["Y1"]
        dx = crystal.x - anchor_pos[0]
        dy = crystal.y - anchor_pos[1]
        # Distance is 5.0 mm and the crystal lies on a cardinal axis.
        assert math.isclose(math.hypot(dx, dy), 5.0, abs_tol=1e-6)
        # West edge rotated 90 deg CCW => north edge (angle 270 deg).
        # Position vector = 5 * (cos(270), sin(270)) = (0, -5).
        assert math.isclose(dx, 0.0, abs_tol=1e-6)
        assert math.isclose(dy, -5.0, abs_tol=1e-6)

    def test_crystal_fallback_when_no_xtal_pads(self):
        """When MCU has no XTAL pads, fall back to 270 deg (legacy behaviour).

        This preserves backwards compatibility for callers (existing
        boards, FPGAs without crystals, MCUs whose pads simply aren't
        net-named yet, etc.). No warning is emitted — silent fallback.
        """
        mcu_no_xtal = MockFootprint(
            reference="U1",
            pads=[
                MockPad(number="1", position=(-4.5, 0.0), net_name="VCC"),
                MockPad(number="2", position=(4.5, 0.0), net_name="GND"),
            ],
        )
        pcb = _make_pcb_with_mcu(mcu_no_xtal)
        strategy = MCUCoreStrategy()

        anchor_pos = (50.0, 30.0)
        placements = strategy.compute_placements(
            components=["U1", "Y1"],
            anchor="U1",
            anchor_position=anchor_pos,
            pcb=pcb,
        )

        crystal = placements["Y1"]
        # Legacy 270 deg = above MCU (screen-Y: -y) at distance 5mm.
        assert math.isclose(crystal.x - anchor_pos[0], 0.0, abs_tol=1e-6)
        assert math.isclose(crystal.y - anchor_pos[1], -5.0, abs_tol=1e-6)

    def test_crystal_fallback_when_no_pcb_context(self):
        """When pcb has no footprints, fall back to 270 deg gracefully.

        This is the path taken by the original ``MockPCB`` style stubs
        in ``tests/test_design.py`` and the public API contract for
        callers who don't populate footprints before computing.
        """
        empty_pcb = MockPCB(footprints=[])
        strategy = MCUCoreStrategy()

        anchor_pos = (50.0, 30.0)
        placements = strategy.compute_placements(
            components=["U1", "Y1"],
            anchor="U1",
            anchor_position=anchor_pos,
            pcb=empty_pcb,
        )

        crystal = placements["Y1"]
        assert math.isclose(crystal.x - anchor_pos[0], 0.0, abs_tol=1e-6)
        assert math.isclose(crystal.y - anchor_pos[1], -5.0, abs_tol=1e-6)

    def test_load_caps_straddle_crystal_perpendicular_to_mcu_vector(self):
        """Load caps sit perpendicular to the MCU-to-crystal vector.

        For an east-edge XTAL, the MCU-to-crystal direction is +x, so
        the two caps should straddle the crystal along the Y axis (with
        a small inward nudge along -x toward the MCU).
        """
        mcu = _make_tqfp32_mcu(xtal_edge="E", rotation=0.0)
        pcb = _make_pcb_with_mcu(mcu)
        strategy = MCUCoreStrategy()

        anchor_pos = (50.0, 30.0)
        # Classification: Y1 comes before any "C" -> the first two
        # caps after the crystal become load caps. We pass C0, C1 as
        # the load caps.
        placements = strategy.compute_placements(
            components=["U1", "Y1", "C0", "C1"],
            anchor="U1",
            anchor_position=anchor_pos,
            pcb=pcb,
        )

        crystal = placements["Y1"]
        c0 = placements["C0"]
        c1 = placements["C1"]

        # Both load caps share the same x: crystal.x - 1.5 (nudged back
        # toward the MCU along -x, the inward direction relative to
        # the east-edge crystal).
        assert math.isclose(c0.x, crystal.x - 1.5, abs_tol=1e-6)
        assert math.isclose(c1.x, crystal.x - 1.5, abs_tol=1e-6)
        # They straddle the crystal along Y: one at +1.5, the other -1.5.
        ys = sorted([c0.y, c1.y])
        assert math.isclose(ys[0], crystal.y - 1.5, abs_tol=1e-6)
        assert math.isclose(ys[1], crystal.y + 1.5, abs_tol=1e-6)

    def test_load_caps_preserve_legacy_geometry_in_fallback(self):
        """When the placer falls back to 270 deg, load caps match legacy code.

        Regression guard: the pre-fix load-cap layout was caps at
        ``(crystal_x ± 1.5, crystal_y + 1.5)``. Confirm the new
        perpendicular-axis code reproduces that exact placement when
        the crystal is in the legacy 270 deg position.
        """
        mcu_no_xtal = MockFootprint(reference="U1", pads=[])
        pcb = _make_pcb_with_mcu(mcu_no_xtal)
        strategy = MCUCoreStrategy()

        anchor_pos = (50.0, 30.0)
        placements = strategy.compute_placements(
            components=["U1", "Y1", "C0", "C1"],
            anchor="U1",
            anchor_position=anchor_pos,
            pcb=pcb,
        )

        crystal = placements["Y1"]
        c0 = placements["C0"]
        c1 = placements["C1"]

        # Same y for both caps (legacy): crystal.y + 1.5 (south of
        # crystal, which in screen-Y means "back toward the MCU" when
        # the crystal is on the 270 deg north side).
        assert math.isclose(c0.y, crystal.y + 1.5, abs_tol=1e-6)
        assert math.isclose(c1.y, crystal.y + 1.5, abs_tol=1e-6)
        # X coords straddle: ± 1.5.
        xs = sorted([c0.x, c1.x])
        assert math.isclose(xs[0], crystal.x - 1.5, abs_tol=1e-6)
        assert math.isclose(xs[1], crystal.x + 1.5, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# Net-name pattern unit tests
# ---------------------------------------------------------------------------


class TestXTALNetPattern:
    """Sanity-check the regex used to identify XTAL/OSC pad nets."""

    @pytest.mark.parametrize(
        "name",
        [
            "XTAL",
            "XTAL1",
            "XTAL2",
            "XTALIN",
            "XTALOUT",
            "OSC_IN",
            "OSC_OUT",
            "OSC32_IN",
            "OSC32_OUT",
            "OSC1",
            "OSC2",
            "xtal1",  # case-insensitive
            "/XTAL1",  # leading slash (hierarchical net)
            "mcu/XTAL2",  # hierarchical prefix
        ],
    )
    def test_matches_xtal_names(self, name: str):
        assert _XTAL_NET_PATTERN.search(name)

    @pytest.mark.parametrize(
        "name",
        [
            "VCC",
            "GND",
            "SCK",
            "MOSI",
            "MISO",
            "FAST_GPIO",
            "NET_42",
            "LED_OUT",
            "RESET",
            # "OSCILLOSCOPE_PROBE",  # would falsely match "OSC" prefix —
            # but our pattern requires a word boundary or non-alphanumeric
            # *after* the OSC token, so "OSCILLO..." should NOT match.
            "OSCILLOSCOPE_PROBE",
        ],
    )
    def test_skips_non_xtal_names(self, name: str):
        assert not _XTAL_NET_PATTERN.search(name)
