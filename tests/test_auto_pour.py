"""Tests for auto_pour_if_missing() helper.

Verifies that copper pour zones are automatically created for
power-classified nets when the PCB has no existing zones, while
respecting the board-level guard (all-power boards are left alone).
"""

from pathlib import Path

import pytest

# Minimal PCB skeleton for testing.  Footprints contain pads with net
# references; the zone generator reads net definitions from the header.
_PCB_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
"""

_PCB_FOOTER = """\
  (gr_line (start 0 0) (end 50 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 50 0) (end 50 50) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 50 50) (end 0 50) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 0 50) (end 0 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
)
"""


def _make_pcb(
    net_defs: list[tuple[int, str]],
    pad_nets: list[tuple[int, str]],
    zones: list[str] | None = None,
) -> str:
    """Build a minimal PCB string.

    Args:
        net_defs: (net_id, net_name) pairs for the header.
        pad_nets: (net_id, net_name) pairs for pad references inside a
            dummy footprint.
        zones: Optional list of zone S-expression strings to insert.
    """
    parts = [_PCB_HEADER]
    parts.append('  (net 0 "")\n')
    for nid, name in net_defs:
        parts.append(f'  (net {nid} "{name}")\n')

    # Single dummy footprint with pads
    parts.append('  (footprint "TestLib:TestPkg" (layer "F.Cu") (at 10 10)\n')
    for idx, (nid, name) in enumerate(pad_nets):
        x_off = idx * 2.0
        parts.append(
            f'    (pad "{idx + 1}" smd roundrect (at {x_off} 0) '
            f'(size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") '
            f'(roundrect_rratio 0.25) (net {nid} "{name}"))\n'
        )
    parts.append("  )\n")

    if zones:
        for z in zones:
            parts.append(f"  {z}\n")

    parts.append(_PCB_FOOTER)
    return "".join(parts)


class TestAutoPourIfMissing:
    """Unit tests for auto_pour_if_missing."""

    def test_creates_zones_for_power_nets_with_signals(self, tmp_path: Path):
        """Zones are created when power nets coexist with signal nets."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "VCC"), (3, "SDA"), (4, "SCL")],
            pad_nets=[
                (1, "GND"),
                (2, "VCC"),
                (3, "SDA"),
                (4, "SCL"),
            ],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path)

        assert count == 2
        assert set(names) == {"GND", "VCC"}
        # Verify zones actually written to file
        text = pcb_path.read_text()
        assert "(zone" in text

    def test_skips_all_power_board(self, tmp_path: Path):
        """No zones created when every net is power/ground (board 01 guard)."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "VIN"), (2, "VOUT"), (3, "GND")],
            pad_nets=[
                (1, "VIN"),
                (1, "VIN"),
                (2, "VOUT"),
                (2, "VOUT"),
                (2, "VOUT"),
                (3, "GND"),
                (3, "GND"),
                (3, "GND"),
            ],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path)

        assert count == 0
        assert names == []
        text = pcb_path.read_text()
        assert "(zone" not in text

    def test_idempotent_second_call(self, tmp_path: Path):
        """Calling twice produces the same result -- second call is a no-op."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "SDA")],
            pad_nets=[(1, "GND"), (2, "SDA")],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count1, names1 = auto_pour_if_missing(pcb_path)
        assert count1 == 1
        assert names1 == ["GND"]

        count2, names2 = auto_pour_if_missing(pcb_path)
        assert count2 == 0
        assert names2 == []

    def test_skips_nets_with_existing_zones(self, tmp_path: Path):
        """Nets that already have zones are not re-created."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        zone_gnd = (
            '(zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5) '
            "(connect_pads (clearance 0.25)) "
            "(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) "
            "(polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50))))"
        )
        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "VCC"), (3, "SDA")],
            pad_nets=[(1, "GND"), (2, "VCC"), (3, "SDA")],
            zones=[zone_gnd],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path)

        # Only VCC should get a new zone; GND already has one
        assert count == 1
        assert names == ["VCC"]

    def test_no_power_nets(self, tmp_path: Path):
        """No zones created when there are no power-classified nets."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "SDA"), (2, "SCL")],
            pad_nets=[(1, "SDA"), (2, "SCL")],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path)

        assert count == 0
        assert names == []

    def test_nonexistent_file_raises(self, tmp_path: Path):
        """Raises FileNotFoundError for a missing PCB file."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        with pytest.raises(FileNotFoundError):
            auto_pour_if_missing(tmp_path / "missing.kicad_pcb")

    def test_empty_nets(self, tmp_path: Path):
        """PCB with no nets returns 0 zones."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(net_defs=[], pad_nets=[])
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path)

        assert count == 0
        assert names == []

    def test_edge_clearance_insets_zone_boundary(self, tmp_path: Path):
        """Zone boundary is inset from board edge when edge_clearance is set.

        Uses the pure-Python rect fallback so shapely is not required.
        """
        import re

        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "SDA")],
            pad_nets=[(1, "GND"), (2, "SDA")],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path, edge_clearance=0.3)

        assert count == 1
        assert names == ["GND"]

        # Parse zone polygon coordinates from the written file
        text = pcb_path.read_text()
        # Extract xy coordinates from the zone polygon
        xy_matches = re.findall(r"\(xy\s+([\d.e+-]+)\s+([\d.e+-]+)\)", text)
        assert len(xy_matches) > 0, "No zone polygon coordinates found"

        # Board edge is 0..50 in both X and Y (from _PCB_FOOTER).
        # With 0.3mm edge clearance, all zone coords should be
        # at least 0.3mm inward from the edges.
        for x_str, y_str in xy_matches:
            x, y = float(x_str), float(y_str)
            assert x >= 0.3 - 0.01, f"X coord {x} too close to left edge (expected >= 0.3)"
            assert x <= 49.7 + 0.01, f"X coord {x} too close to right edge (expected <= 49.7)"
            assert y >= 0.3 - 0.01, f"Y coord {y} too close to top edge (expected >= 0.3)"
            assert y <= 49.7 + 0.01, f"Y coord {y} too close to bottom edge (expected <= 49.7)"

    def test_no_edge_clearance_uses_exact_outline(self, tmp_path: Path):
        """Without edge_clearance, zone boundary matches board edge exactly."""
        import re

        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "SDA")],
            pad_nets=[(1, "GND"), (2, "SDA")],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, _names = auto_pour_if_missing(pcb_path)
        assert count == 1

        text = pcb_path.read_text()
        # Zone polygon should include coordinates at or very near the
        # board edge (0 and 50).
        xy_matches = re.findall(r"\(xy\s+([\d.e+-]+)\s+([\d.e+-]+)\)", text)
        xs = [float(x) for x, _ in xy_matches]
        ys = [float(y) for _, y in xy_matches]

        # At least one coordinate should be at (or very near) each edge
        assert min(xs) <= 0.01, "Expected coordinate near left edge (x=0)"
        assert max(xs) >= 49.99, "Expected coordinate near right edge (x=50)"
        assert min(ys) <= 0.01, "Expected coordinate near top edge (y=0)"
        assert max(ys) >= 49.99, "Expected coordinate near bottom edge (y=50)"

    def test_reinsets_existing_uninset_zones(self, tmp_path: Path):
        """Existing zones at board edge are removed and recreated with inset.

        When edge_clearance is specified and an existing zone's boundary
        matches the board edge (no inset), auto_pour should remove it and
        regenerate with proper inset.
        """
        import re

        from kicad_tools.router.auto_pour import auto_pour_if_missing

        # Create a zone at the exact board edge (0..50)
        zone_gnd = (
            '(zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5) '
            "(connect_pads (clearance 0.25)) "
            "(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) "
            "(polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50))))"
        )
        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "SDA")],
            pad_nets=[(1, "GND"), (2, "SDA")],
            zones=[zone_gnd],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path, edge_clearance=0.3)

        # GND zone should be regenerated (removed + recreated)
        assert count == 1
        assert "GND" in names

        # Verify new zone boundary is inset from the board edge
        text = pcb_path.read_text()
        xy_matches = re.findall(r"\(xy\s+([\d.e+-]+)\s+([\d.e+-]+)\)", text)
        assert len(xy_matches) > 0, "No zone polygon coordinates found"

        for x_str, y_str in xy_matches:
            x, y = float(x_str), float(y_str)
            assert x >= 0.3 - 0.01, f"X coord {x} too close to left edge (expected >= 0.3)"
            assert x <= 49.7 + 0.01, f"X coord {x} too close to right edge (expected <= 49.7)"

    def test_reinsets_multiline_uninset_zone(self, tmp_path: Path):
        """Multi-line KiCad zone blocks at the board edge are removed and reinset.

        Regression test for #2462: ``_remove_zones_for_nets`` previously
        ran a per-line regex which could not match KiCad's actual writer
        output, where ``(zone``, ``(net …)``, ``(net_name …)``,
        ``(layer …)`` and ``(polygon …)`` each sit on their own line.
        The bug caused the un-inset zone to remain in the file while a
        second inset zone was appended, producing duplicate zones and
        ``edge_clearance_zone`` DRC violations.
        """
        import re

        from kicad_tools.router.auto_pour import auto_pour_if_missing

        # Multi-line zone literal that mirrors KiCad's actual writer
        # output (each sub-node on its own indented line).  The polygon
        # sits at the exact board edge (0..50) so it should be detected
        # as un-inset and regenerated.
        zone_gnd_multiline = (
            "(zone\n"
            "    (net 1)\n"
            '    (net_name "GND")\n'
            '    (layer "B.Cu")\n'
            "    (hatch edge 0.5)\n"
            "    (connect_pads (clearance 0.25)\n"
            "    )\n"
            "    (min_thickness 0.25)\n"
            "    (filled_areas_thickness no)\n"
            "    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)\n"
            "    )\n"
            "    (polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50))\n"
            "    )\n"
            "  )"
        )

        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "SDA")],
            pad_nets=[(1, "GND"), (2, "SDA")],
            zones=[zone_gnd_multiline],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path, edge_clearance=0.3)

        # Exactly one new zone should be created for GND (un-inset zone
        # removed, fresh inset zone added).
        assert count == 1
        assert "GND" in names

        text = pcb_path.read_text()

        # Exactly one ``(zone`` block should remain in the file, not
        # two -- the bug previously left the un-inset original in place.
        zone_count = len(re.findall(r"\(zone\b", text))
        assert zone_count == 1, (
            f"Expected exactly 1 zone block after reinset, got "
            f"{zone_count}.  File contents:\n{text}"
        )

        # And every polygon vertex must be inset by edge_clearance.
        xy_matches = re.findall(r"\(xy\s+([\d.e+-]+)\s+([\d.e+-]+)\)", text)
        assert len(xy_matches) > 0, "No zone polygon coordinates found"
        for x_str, y_str in xy_matches:
            x, y = float(x_str), float(y_str)
            assert x >= 0.3 - 0.01, f"X coord {x} too close to left edge (expected >= 0.3)"
            assert x <= 49.7 + 0.01, f"X coord {x} too close to right edge (expected <= 49.7)"
            assert y >= 0.3 - 0.01, f"Y coord {y} too close to top edge (expected >= 0.3)"
            assert y <= 49.7 + 0.01, f"Y coord {y} too close to bottom edge (expected <= 49.7)"

    def test_reinset_preserves_other_nets_zones(self, tmp_path: Path):
        """Reinset removes only the un-inset net's zone, not others'.

        Edge case for #2462: when a file contains a mix of un-inset and
        already-inset zones for different nets, only the un-inset one
        should be regenerated; the inset zone for the other net must be
        left untouched.
        """
        import re

        from kicad_tools.router.auto_pour import auto_pour_if_missing

        # GND zone at the exact board edge (un-inset, multi-line)
        zone_gnd_uninset = (
            "(zone\n"
            "    (net 1)\n"
            '    (net_name "GND")\n'
            '    (layer "B.Cu")\n'
            "    (hatch edge 0.5)\n"
            "    (connect_pads (clearance 0.25)\n"
            "    )\n"
            "    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)\n"
            "    )\n"
            "    (polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50))\n"
            "    )\n"
            "  )"
        )
        # VCC zone already inset by 0.3mm (should be preserved)
        zone_vcc_inset = (
            "(zone\n"
            "    (net 2)\n"
            '    (net_name "VCC")\n'
            '    (layer "F.Cu")\n'
            "    (hatch edge 0.5)\n"
            "    (connect_pads (clearance 0.25)\n"
            "    )\n"
            "    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)\n"
            "    )\n"
            "    (polygon (pts (xy 0.3 0.3) (xy 49.7 0.3) "
            "(xy 49.7 49.7) (xy 0.3 49.7))\n"
            "    )\n"
            "  )"
        )

        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "VCC"), (3, "SDA")],
            pad_nets=[(1, "GND"), (2, "VCC"), (3, "SDA")],
            zones=[zone_gnd_uninset, zone_vcc_inset],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path, edge_clearance=0.3)

        # Only GND should be regenerated; VCC's existing inset zone
        # stays in place.
        assert count == 1
        assert names == ["GND"]

        text = pcb_path.read_text()
        # Two zones total: regenerated GND + preserved VCC.
        zone_count = len(re.findall(r"\(zone\b", text))
        assert zone_count == 2, (
            f"Expected exactly 2 zone blocks (GND regenerated, VCC "
            f"preserved), got {zone_count}.  File contents:\n{text}"
        )
        # VCC's original inset polygon must still be present verbatim.
        assert "(xy 0.3 0.3)" in text
        assert "(xy 49.7 0.3)" in text
