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


def _make_outline_footer(origin: tuple[float, float], size: float = 50.0) -> str:
    """Build an Edge.Cuts gr_line rectangle footer at an arbitrary origin.

    Coordinates are sheet-absolute (the KiCad file format), so a non-zero
    *origin* produces a board whose ``PCB.board_origin`` is non-zero --
    exercising the board-relative coordinate conversion in
    ``PCB._detect_board_origin()``.
    """
    x0, y0 = origin
    x1, y1 = x0 + size, y0 + size
    stroke = '(stroke (width 0.05) (type default)) (layer "Edge.Cuts")'
    return (
        f"  (gr_line (start {x0:g} {y0:g}) (end {x1:g} {y0:g}) {stroke})\n"
        f"  (gr_line (start {x1:g} {y0:g}) (end {x1:g} {y1:g}) {stroke})\n"
        f"  (gr_line (start {x1:g} {y1:g}) (end {x0:g} {y1:g}) {stroke})\n"
        f"  (gr_line (start {x0:g} {y1:g}) (end {x0:g} {y0:g}) {stroke})\n"
        ")\n"
    )


def _make_pcb(
    net_defs: list[tuple[int, str]],
    pad_nets: list[tuple[int, str]],
    zones: list[str] | None = None,
    origin: tuple[float, float] = (0.0, 0.0),
) -> str:
    """Build a minimal PCB string.

    Args:
        net_defs: (net_id, net_name) pairs for the header.
        pad_nets: (net_id, net_name) pairs for pad references inside a
            dummy footprint.
        zones: Optional list of zone S-expression strings to insert.
        origin: Sheet-absolute position of the board outline's top-left
            corner.  Non-zero values (e.g. ``(100, 100)`` like board 03)
            give the board a non-zero ``PCB.board_origin``.
    """
    parts = [_PCB_HEADER]
    parts.append('  (net 0 "")\n')
    for nid, name in net_defs:
        parts.append(f'  (net {nid} "{name}")\n')

    # Single dummy footprint with pads (placed 10mm inside the outline)
    fp_x, fp_y = origin[0] + 10, origin[1] + 10
    parts.append(f'  (footprint "TestLib:TestPkg" (layer "F.Cu") (at {fp_x:g} {fp_y:g})\n')
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

    if origin == (0.0, 0.0):
        parts.append(_PCB_FOOTER)
    else:
        parts.append(_make_outline_footer(origin))
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

    def test_all_power_board_honors_force_pour_nets(self, tmp_path: Path):
        """Issue #3092: all-power-board guard yields to caller-forced pour nets.

        Reproduces board 01 (VIN/VOUT/GND): the caller (``kct route``) passes
        ``--skip-nets GND``, declaring GND will be poured.  Without the
        force escape, the all-power guard suppresses every zone and GND
        ends up with neither traces (router skipped it) nor a zone
        (auto-pour skipped it), leaving its pads stranded in DRC.

        With ``force_pour_nets=["GND"]`` on a 2-layer board we expect a
        B.Cu GND zone PLUS a F.Cu GND mirror zone (so F.Cu-only SMD GND
        pads aren't stranded), and no VIN/VOUT zones (so the router
        routes those as signals).
        """
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

        count, names = auto_pour_if_missing(pcb_path, force_pour_nets=["GND"])

        # Two GND zones: B.Cu (initial) + F.Cu (mirror).  ``names`` is the
        # combined list returned by both passes.
        assert count == 2
        assert names == ["GND", "GND"]
        text = pcb_path.read_text()
        assert text.count("(zone") == 2
        # Both zones are on the GND net; no VIN/VOUT zones.
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("(zone"):
                assert "VIN" not in stripped
                assert "VOUT" not in stripped
        # Load the PCB and inspect zone layers/net programmatically.
        from kicad_tools.schema.pcb import PCB

        pcb_obj = PCB.load(str(pcb_path))
        assert {z.net_name for z in pcb_obj.zones} == {"GND"}
        assert {z.layer for z in pcb_obj.zones} == {"B.Cu", "F.Cu"}

    def test_all_power_board_force_pour_unknown_net_no_effect(self, tmp_path: Path):
        """force_pour_nets entries that aren't pour candidates are ignored.

        Belt-and-braces: passing a non-existent or non-power net name
        through ``force_pour_nets`` must not cause spurious zone creation
        and must not break the all-power guard for actual power nets.
        """
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "VIN"), (2, "VOUT"), (3, "GND")],
            pad_nets=[(1, "VIN"), (2, "VOUT"), (3, "GND")],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path, force_pour_nets=["DOES_NOT_EXIST"])

        # No matching pour candidate -> guard still trips, no zones.
        assert count == 0
        assert names == []

    def test_dual_side_ground_skipped_when_fcu_taken(self, tmp_path: Path):
        """Dual-side GND mirror is suppressed when F.Cu is already a power plane.

        Defends against the dual-side mirror stealing F.Cu copper from a
        legitimate power zone.  Set up: ``VCC`` already has a F.Cu zone,
        GND is in the force list.  The mirror pass must skip GND on
        F.Cu, leaving the user's power plane intact.
        """
        from kicad_tools.router.auto_pour import auto_pour_if_missing
        from kicad_tools.schema.pcb import PCB

        zone_vcc_fcu = (
            "(zone\n"
            "    (net 2)\n"
            '    (net_name "VCC")\n'
            '    (layer "F.Cu")\n'
            "    (hatch edge 0.5)\n"
            "    (connect_pads (clearance 0.25)\n"
            "    )\n"
            "    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)\n"
            "    )\n"
            "    (polygon (pts (xy 0.5 0.5) (xy 49.5 0.5) (xy 49.5 49.5) (xy 0.5 49.5))\n"
            "    )\n"
            "  )"
        )
        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "VCC")],
            pad_nets=[(1, "GND"), (2, "VCC")],
            zones=[zone_vcc_fcu],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        # Force GND -- but VCC zone occupies F.Cu, so the mirror must be skipped.
        # NB: this is a mixed power board (not all-power: only 2 nets, both
        # power-class), so the all-power guard does NOT trip; new zones get
        # created for GND but NOT for VCC (which already has one).
        count, names = auto_pour_if_missing(pcb_path, force_pour_nets=["GND"])

        # Exactly one new zone for GND on B.Cu.  No F.Cu mirror because
        # VCC owns F.Cu.
        pcb_obj = PCB.load(str(pcb_path))
        gnd_zones = [z for z in pcb_obj.zones if z.net_name == "GND"]
        vcc_zones = [z for z in pcb_obj.zones if z.net_name == "VCC"]
        assert len(gnd_zones) == 1
        assert gnd_zones[0].layer == "B.Cu"
        # VCC zone preserved on F.Cu.
        assert len(vcc_zones) == 1
        assert vcc_zones[0].layer == "F.Cu"

    def test_force_pour_nets_no_effect_on_mixed_board(self, tmp_path: Path):
        """When the board has signal nets, force_pour_nets is redundant.

        The all-power guard does NOT trip on a board with signal nets,
        so all pour candidates get zones whether or not the caller
        forces any.  ``force_pour_nets`` must not change behavior in
        the common case.
        """
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "VCC"), (3, "SDA"), (4, "SCL")],
            pad_nets=[(1, "GND"), (2, "VCC"), (3, "SDA"), (4, "SCL")],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path, force_pour_nets=["GND"])

        # Same result as without force_pour_nets on a mixed board.
        assert count == 2
        assert set(names) == {"GND", "VCC"}

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

        for x_str, _y_str in xy_matches:
            x = float(x_str)
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


class TestNonZeroBoardOrigin:
    """Zone preservation/reinset on boards with a non-zero origin (#3461).

    Regression tests for the double origin-subtraction bug fixed in
    PR #3453 (issue #3410): ``Zone.polygon`` vertices are ALREADY
    board-relative after ``PCB._detect_board_origin()`` runs, but
    ``_detect_uninset_zones`` used to subtract ``pcb.board_origin`` a
    second time.  On any board whose Edge.Cuts origin is non-zero
    (e.g. board 03 at 100,100) that pushed every vertex of a
    properly-inset zone out to ~-origin, so the zone was flagged
    "insufficient edge clearance", silently deleted, and replaced
    with a conflicting auto-pour zone.

    All other zone-preservation tests in this file use boards at
    origin (0,0), where the buggy subtraction is a no-op -- so a
    re-regression would pass the entire fast suite.  These tests pin
    the non-zero-origin behavior directly.
    """

    # Board 03's convention: outline top-left corner at sheet (100, 100).
    ORIGIN = (100.0, 100.0)

    def test_preserves_inset_zone_at_nonzero_origin(self, tmp_path: Path):
        """A properly-inset hand-tuned zone on an offset board is preserved.

        The zone polygon below is written in sheet-absolute coordinates
        (as KiCad stores it): inset 0.3mm from the 100..150 outline.
        After load it is board-relative (0.3..49.7).  With the
        double-subtraction bug, _detect_uninset_zones saw vertices at
        ~(-99.7..-50.3), flagged the zone as un-inset, and deleted it.
        """
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        zone_gnd_inset = (
            '(zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5) '
            "(connect_pads (clearance 0.25)) "
            "(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) "
            "(polygon (pts (xy 100.3 100.3) (xy 149.7 100.3) "
            "(xy 149.7 149.7) (xy 100.3 149.7))))"
        )
        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "SDA")],
            pad_nets=[(1, "GND"), (2, "SDA")],
            zones=[zone_gnd_inset],
            origin=self.ORIGIN,
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path, edge_clearance=0.3)

        # GND already has a properly-inset zone: nothing to create.
        assert count == 0, (
            f"Expected the inset GND zone to be preserved (count=0), "
            f"got count={count} names={names} -- the zone was likely "
            f"flagged as un-inset due to origin mishandling"
        )
        assert names == []

        # The original hand-tuned polygon must survive verbatim.
        text = pcb_path.read_text()
        assert "(xy 100.3 100.3)" in text, (
            "Hand-tuned zone polygon was removed/regenerated despite "
            "having proper edge clearance on a non-zero-origin board"
        )
        assert "(xy 149.7 149.7)" in text

    def test_reinsets_uninset_zone_at_nonzero_origin(self, tmp_path: Path):
        """A genuinely un-inset zone on an offset board is still detected.

        Companion case: detection must not be broken in the other
        direction either (e.g. by skipping the check entirely on
        offset boards).  A zone at the exact 100..150 board edge must
        be removed and regenerated with the 0.3mm inset.
        """
        import re

        from kicad_tools.router.auto_pour import auto_pour_if_missing

        zone_gnd_at_edge = (
            '(zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5) '
            "(connect_pads (clearance 0.25)) "
            "(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) "
            "(polygon (pts (xy 100 100) (xy 150 100) "
            "(xy 150 150) (xy 100 150))))"
        )
        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "SDA")],
            pad_nets=[(1, "GND"), (2, "SDA")],
            zones=[zone_gnd_at_edge],
            origin=self.ORIGIN,
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path, edge_clearance=0.3)

        assert count == 1
        assert "GND" in names

        text = pcb_path.read_text()
        # Exactly one zone block: the un-inset original was removed.
        zone_count = len(re.findall(r"\(zone\b", text))
        assert zone_count == 1, f"Expected exactly 1 zone block after reinset, got {zone_count}"
        # Every polygon vertex must be inset 0.3mm from the board edge
        # in whatever coordinate space the generator wrote (board 0..50
        # plus optional 100,100 sheet offset both satisfy the modular
        # check below by validating against the matching edge box).
        xy_matches = re.findall(r"\(xy\s+([\d.e+-]+)\s+([\d.e+-]+)\)", text)
        assert len(xy_matches) > 0, "No zone polygon coordinates found"
        xs = [float(x) for x, _ in xy_matches]
        ys = [float(y) for _, y in xy_matches]
        # Determine which space the writer used from the data itself.
        x_base = 100.0 if min(xs) > 50.0 else 0.0
        y_base = 100.0 if min(ys) > 50.0 else 0.0
        for x in xs:
            assert x >= x_base + 0.3 - 0.01, f"X coord {x} too close to left edge"
            assert x <= x_base + 49.7 + 0.01, f"X coord {x} too close to right edge"
        for y in ys:
            assert y >= y_base + 0.3 - 0.01, f"Y coord {y} too close to top edge"
            assert y <= y_base + 49.7 + 0.01, f"Y coord {y} too close to bottom edge"


class TestErcMarkerNetExclusion:
    """Tests for the ERC-marker (PWR_FLAG) exclusion filter (#2592).

    KiCad's netlister emits ``PWR_FLAG`` (and user-named flag variants)
    as ordinary net names, even though those symbols have no electrical
    connection -- they exist purely to silence the *Input Power pin not
    driven by any Output Power pin* ERC error.  The name-based classifier
    in ``router/net_class.py`` matches them as ``NetClass.POWER`` because
    they start with ``PWR``, so without a dedicated filter they would
    end up as poured zones that starve legitimate power rails of copper.
    """

    def test_pwr_flag_excluded_from_pour_nets(self, tmp_path: Path):
        """``PWR_FLAG`` must not produce a zone even when classified POWER."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "+3.3V"), (2, "GND"), (3, "PWR_FLAG"), (4, "SIG1")],
            pad_nets=[(1, "+3.3V"), (2, "GND"), (3, "PWR_FLAG"), (4, "SIG1")],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path)

        # Exactly two zones: +3.3V and GND.  PWR_FLAG must not appear.
        assert count == 2
        assert set(names) == {"+3.3V", "GND"}
        assert "PWR_FLAG" not in names

        # And the file must not contain a PWR_FLAG zone definition.
        text = pcb_path.read_text()
        assert '(net_name "PWR_FLAG")' not in text
        assert '(net "PWR_FLAG")' not in text

    def test_user_named_flag_variant_excluded(self, tmp_path: Path):
        """User-named flag nets (e.g., ``+3V3_FLAG``) are also filtered."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "+3.3V"), (2, "GND"), (3, "+3V3_FLAG"), (4, "SIG1")],
            pad_nets=[(1, "+3.3V"), (2, "GND"), (3, "+3V3_FLAG"), (4, "SIG1")],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path)

        # +3V3_FLAG must not be poured even though it would otherwise
        # match both the POWER pattern and the per-rail naming idiom.
        assert "+3V3_FLAG" not in names
        assert set(names) == {"+3.3V", "GND"}
        assert count == 2

    def test_pwr_flag_only_does_not_count_as_power_only_board(self, tmp_path: Path):
        """A board whose only power-classified net is ``PWR_FLAG`` runs normally.

        Without the filter, the all-power-board guard would treat the
        board as having one power net (``PWR_FLAG``) and one signal net
        (``SIG1``), and produce a spurious zone for ``PWR_FLAG``.  With
        the filter, ``PWR_FLAG`` is invisible to the pour stage entirely:
        no zones are created and the function returns cleanly without
        triggering the all-power early return.
        """
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb(
            net_defs=[(1, "PWR_FLAG"), (2, "SIG1")],
            pad_nets=[(1, "PWR_FLAG"), (2, "SIG1")],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path)

        # No zones -- no real pour nets -- and no all-power guard hit.
        assert count == 0
        assert names == []
        text = pcb_path.read_text()
        assert "(zone" not in text

    def test_pwr_flag_classifier_unchanged(self):
        """``classify_from_name`` may still tag PWR_FLAG as POWER.

        The fix lives in the *pour-net selection* layer, not the
        classifier.  Other consumers (e.g., trace-width selection in
        ``apply_net_class_rules``) should keep getting a POWER answer
        for legacy reasons; the auto-pour filter is what excludes the
        net from zones.  This test pins down that design decision so a
        future refactor does not silently move the carve-out.
        """
        from kicad_tools.router.net_class import NetClass, classify_from_name

        # The classifier is allowed to return POWER here -- this is the
        # *expected* behaviour given the ``^PWR`` pattern.  What matters
        # is that auto_pour_if_missing filters it out (covered above).
        result = classify_from_name("PWR_FLAG")
        assert result == NetClass.POWER

    def test_is_erc_marker_net_helper(self):
        """The internal helper recognises canonical and variant spellings."""
        from kicad_tools.router.auto_pour import _is_erc_marker_net

        # Canonical ERC markers
        assert _is_erc_marker_net("PWR_FLAG")
        assert _is_erc_marker_net("+3V3_FLAG")
        assert _is_erc_marker_net("VBUS_FLAG")
        assert _is_erc_marker_net("#FLG01")
        assert _is_erc_marker_net("#FLG")

        # Real net names that must NOT be treated as ERC markers
        assert not _is_erc_marker_net("PWR_5V")  # legitimate per-rail name
        assert not _is_erc_marker_net("+3.3V")
        assert not _is_erc_marker_net("GND")
        assert not _is_erc_marker_net("SIG1")
        assert not _is_erc_marker_net("FLAG_OUT")  # _FLAG anchored at end only


# ----------------------------------------------------------------------
# Issue #2593 -- split-ground integration tests
# ----------------------------------------------------------------------

# 4-layer skeleton with split ground (GNDA + GNDD) plus signal nets so
# the all-power guard does not trigger.
_PCB_HEADER_4L = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
"""


def _make_pcb_4l(
    net_defs: list[tuple[int, str]],
    pad_nets: list[tuple[int, str]],
) -> str:
    """Build a minimal 4-layer PCB string (GNDA / GNDD split-ground tests)."""
    parts = [_PCB_HEADER_4L]
    parts.append('  (net 0 "")\n')
    for nid, name in net_defs:
        parts.append(f'  (net {nid} "{name}")\n')

    parts.append('  (footprint "TestLib:TestPkg" (layer "F.Cu") (at 10 10)\n')
    for idx, (nid, name) in enumerate(pad_nets):
        x_off = idx * 2.0
        parts.append(
            f'    (pad "{idx + 1}" smd roundrect (at {x_off} 0) '
            f'(size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") '
            f'(roundrect_rratio 0.25) (net {nid} "{name}"))\n'
        )
    parts.append("  )\n")
    parts.append(_PCB_FOOTER)
    return "".join(parts)


class TestAutoPourSplitGround:
    """Integration tests for split-ground auto-pour (issue #2593).

    Verifies that auto_pour_if_missing produces zones on distinct copper
    layers for multiple GROUND-class nets (GNDA / GNDD) on a 4-layer
    board, so the KiCad fill engine does not silently zero out one of
    the ground domains.
    """

    def test_split_ground_zones_use_distinct_layers(self, tmp_path: Path):
        """4-layer board with GNDA + GNDD: each ground gets its own inner layer."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb_4l(
            net_defs=[
                (1, "GNDA"),
                (2, "GNDD"),
                (3, "+3.3V"),
                (4, "SDA"),
                (5, "SCL"),
            ],
            pad_nets=[
                (1, "GNDA"),
                (2, "GNDD"),
                (3, "+3.3V"),
                (4, "SDA"),
                (5, "SCL"),
            ],
        )
        pcb_path = tmp_path / "split_ground.kicad_pcb"
        pcb_path.write_text(pcb)

        count, names = auto_pour_if_missing(pcb_path)

        # Three pour zones: GNDA, GNDD, +3.3V
        assert count == 3
        assert set(names) == {"GNDA", "GNDD", "+3.3V"}

        # Inspect the saved file: each ground should have its own zone
        # on a distinct copper layer.
        from kicad_tools.schema.pcb import PCB

        pcb_obj = PCB.load(str(pcb_path))
        layers_by_net = {z.net_name: z.layer for z in pcb_obj.zones}

        assert "GNDA" in layers_by_net
        assert "GNDD" in layers_by_net
        assert "+3.3V" in layers_by_net

        # Both grounds end up on inner layers (one on In1.Cu, one on
        # In2.Cu) -- never on the same layer.
        assert layers_by_net["GNDA"] != layers_by_net["GNDD"]
        assert {layers_by_net["GNDA"], layers_by_net["GNDD"]} == {"In1.Cu", "In2.Cu"}

        # Power demoted to F.Cu because both inner layers are reserved.
        assert layers_by_net["+3.3V"] == "F.Cu"

    def test_split_ground_priorities_distinct_per_layer(self, tmp_path: Path):
        """No two ground zones share a (layer, priority) -- the exact
        condition KiCad's fill engine reports as 'will get zero copper'."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing
        from kicad_tools.schema.pcb import PCB

        pcb = _make_pcb_4l(
            net_defs=[
                (1, "GNDA"),
                (2, "GNDD"),
                (3, "SDA"),
            ],
            pad_nets=[
                (1, "GNDA"),
                (2, "GNDD"),
                (3, "SDA"),
            ],
        )
        pcb_path = tmp_path / "split_ground_pri.kicad_pcb"
        pcb_path.write_text(pcb)

        auto_pour_if_missing(pcb_path)

        pcb_obj = PCB.load(str(pcb_path))
        ground_zones = [z for z in pcb_obj.zones if z.net_name in {"GNDA", "GNDD"}]
        assert len(ground_zones) == 2

        # Distinct (layer, priority) for each ground -- this is the
        # invariant the fix guarantees.
        layer_priority = {(z.layer, z.priority) for z in ground_zones}
        assert len(layer_priority) == 2

    def test_split_ground_zone_text_has_distinct_layer_clauses(self, tmp_path: Path):
        """Both ground zones are written to the file with a distinct ``(layer ...)`` clause.

        Lighter-weight assertion that doesn't depend on the PCB schema
        layer parsing the same way -- just inspects the raw S-expression
        text to make sure two ground zone blocks each name a different
        copper layer.
        """
        import re

        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = _make_pcb_4l(
            net_defs=[(1, "GNDA"), (2, "GNDD"), (3, "SDA")],
            pad_nets=[(1, "GNDA"), (2, "GNDD"), (3, "SDA")],
        )
        pcb_path = tmp_path / "split_ground_text.kicad_pcb"
        pcb_path.write_text(pcb)

        auto_pour_if_missing(pcb_path)

        text = pcb_path.read_text()
        # Find each (zone ...) block and look for its layer + net_name.
        # The minimal generator output uses (layer "X.Cu") inside zones.
        zone_blocks = re.findall(
            r'\(zone\b.*?\(net_name\s+"([^"]+)"\).*?\(layer\s+"([^"]+)"\)',
            text,
            re.DOTALL,
        )
        layers_by_net = {n: l for n, l in zone_blocks if n in {"GNDA", "GNDD"}}
        # Both grounds must appear and be on different copper layers.
        assert layers_by_net.get("GNDA") is not None
        assert layers_by_net.get("GNDD") is not None
        assert layers_by_net["GNDA"] != layers_by_net["GNDD"]


# ----------------------------------------------------------------------
# Issue #3035 -- public API surface for auto_skip_pour_nets
# ----------------------------------------------------------------------


class TestAutoSkipPourNetsPublicAPI:
    """Smoke tests for ``auto_skip_pour_nets`` as a public symbol (#3035).

    Promoted from the leading-underscore CLI internal
    ``kicad_tools.cli.route_cmd._auto_skip_pour_nets`` so in-process
    router callers (board ``generate_design.py`` scripts) can reach it
    without importing a CLI private.  Verifies:

    * The new public path resolves and returns the expected
      ``(auto_skip, no_zone_nets)`` tuple shape.
    * The CLI-side alias still resolves to the *same* function object so
      existing ``@patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets")``
      decorators in the test suite keep targeting the live implementation.
    * Behaviour on a small fixture matches the documented contract: pour
      nets with zones land in ``auto_skip``, pour-classified nets without
      zones land in ``no_zone_nets``.
    """

    def test_public_path_and_cli_alias_are_same_function(self):
        """``auto_skip_pour_nets`` is reachable from both the public and
        legacy CLI paths and they resolve to the *same* function object.

        ``tests/test_layer_escalation.py`` and
        ``tests/test_route_auto_fix.py`` patch the CLI alias name; if
        these two stopped being the same object, those patches would
        silently no-op against the real call sites (which import the
        public symbol indirectly via the alias).
        """
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets
        from kicad_tools.router.auto_pour import auto_skip_pour_nets

        assert auto_skip_pour_nets is _auto_skip_pour_nets

    def test_power_net_without_zone_lands_in_no_zone(self, tmp_path: Path):
        """A power-classified net with no zone is returned in ``no_zone_nets``.

        Mirrors the board 01 fixture (VIN + VOUT are pour-classified by
        name but the unrouted PCB has no zones for them, so they must be
        routed as signals via ``router._pour_nets_without_zones``).
        """
        from kicad_tools.router.auto_pour import auto_skip_pour_nets

        pcb = _make_pcb(
            net_defs=[(1, "VIN"), (2, "VOUT"), (3, "GND")],
            pad_nets=[(1, "VIN"), (2, "VOUT"), (3, "GND")],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        skip_nets: list[str] = []
        auto_skip, no_zone = auto_skip_pour_nets(pcb_path, skip_nets, quiet=True)

        # No zones exist, so all pour-classified nets fall through to
        # no_zone (not auto_skip).  VIN/VOUT/GND all classify as power
        # or ground by name pattern.
        assert auto_skip == []
        assert set(no_zone) == {"VIN", "VOUT", "GND"}
        # ``skip_nets`` is left untouched because nothing was added.
        assert skip_nets == []

    def test_power_net_with_zone_lands_in_auto_skip(self, tmp_path: Path):
        """A power-classified net with a zone is appended to ``skip_nets``.

        When the PCB already has a copper zone for a pour-classified net,
        that net should be routed via the zone fill rather than as a
        signal trace -- it lands in ``auto_skip`` and gets appended to
        the caller's ``skip_nets`` list.
        """
        from kicad_tools.router.auto_pour import auto_skip_pour_nets

        zone_gnd = (
            '(zone (net 3) (net_name "GND") (layer "B.Cu") (hatch edge 0.5) '
            "(connect_pads (clearance 0.25)) "
            "(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) "
            "(polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50))))"
        )
        pcb = _make_pcb(
            net_defs=[(1, "VIN"), (2, "VOUT"), (3, "GND"), (4, "SIG1")],
            pad_nets=[(1, "VIN"), (2, "VOUT"), (3, "GND"), (4, "SIG1")],
            zones=[zone_gnd],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        skip_nets: list[str] = []
        auto_skip, no_zone = auto_skip_pour_nets(pcb_path, skip_nets, quiet=True)

        # GND has a zone -> auto_skipped (caller routes via fill).
        # VIN/VOUT have no zone -> fall through to no_zone for signal
        # routing.
        assert "GND" in auto_skip
        assert "GND" in skip_nets  # mutated in place
        assert set(no_zone) == {"VIN", "VOUT"}

    def test_existing_skip_nets_preserved(self, tmp_path: Path):
        """User-supplied ``skip_nets`` entries are not re-added or duplicated.

        The function appends to the caller's list in place; nets already
        present must not be processed (they are already skipped) and
        must not appear in the returned ``auto_skip`` either.
        """
        from kicad_tools.router.auto_pour import auto_skip_pour_nets

        zone_gnd = (
            '(zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5) '
            "(connect_pads (clearance 0.25)) "
            "(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) "
            "(polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50))))"
        )
        pcb = _make_pcb(
            net_defs=[(1, "GND"), (2, "SIG1")],
            pad_nets=[(1, "GND"), (2, "SIG1")],
            zones=[zone_gnd],
        )
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        # Pre-populate skip_nets with GND (already user-skipped)
        skip_nets: list[str] = ["GND"]
        auto_skip, no_zone = auto_skip_pour_nets(pcb_path, skip_nets, quiet=True)

        # GND is already in skip_nets, so it is NOT re-added by the
        # carve-out and does NOT appear in auto_skip.
        assert "GND" not in auto_skip
        assert skip_nets.count("GND") == 1
        assert no_zone == []
