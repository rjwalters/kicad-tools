"""Tests for fully unconnected component detection in sch validate."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_pin_map import _snap_coord, _to_coord
from kicad_tools.cli.sch_validate import (
    ValidationIssue,
    check_fully_unconnected_components,
)


# ---------------------------------------------------------------------------
# Helpers to generate synthetic KiCad schematics
# ---------------------------------------------------------------------------


def _make_lib_symbol(
    lib_id: str,
    pins: list[tuple[str, str, str]],
) -> str:
    """Generate a lib_symbols entry.

    Args:
        lib_id: e.g. "Device:R_Small" or "IC:DAC"
        pins: list of (pin_number, pin_name, pin_type) tuples
    """
    part_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id
    pin_blocks = []
    for i, (num, name, ptype) in enumerate(pins):
        y = i * 2.54
        pin_blocks.append(
            f"""(pin {ptype} line
                    (at 0 {y:.2f} 0)
                    (length 2.54)
                    (name "{name}")
                    (number "{num}")
                )"""
        )
    pin_str = "\n".join(pin_blocks)
    return f"""(symbol "{lib_id}"
            (pin_names (offset 0.254))
            (symbol "{part_name}_0_1"
                (rectangle
                    (start -5.08 -{(len(pins) * 2.54) + 1.27:.2f})
                    (end 5.08 1.27)
                    (stroke (width 0.254))
                    (fill (type background))
                )
            )
            (symbol "{part_name}_1_1"
                {pin_str}
            )
        )"""


def _make_symbol_instance(
    ref: str,
    lib_id: str,
    pins: list[tuple[str, str, str]],
    x: float,
    y: float,
    dnp: bool = False,
    in_bom: bool = True,
    on_board: bool = True,
    rotation: float = 0,
) -> str:
    """Generate a symbol instance S-expression."""
    pin_entries = "\n".join(
        f'(pin "{num}" (uuid "pin-{ref.lower()}-{num}"))' for num, _, _ in pins
    )
    dnp_str = "yes" if dnp else "no"
    in_bom_str = "yes" if in_bom else "no"
    on_board_str = "yes" if on_board else "no"
    return f"""(symbol
        (lib_id "{lib_id}")
        (at {x} {y} {rotation})
        (unit 1)
        (in_bom {in_bom_str})
        (on_board {on_board_str})
        (dnp {dnp_str})
        (uuid "uuid-{ref.lower()}")
        (property "Reference" "{ref}"
            (at {x + 2} {y - 2} 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Value" "{lib_id.split(':')[-1]}"
            (at {x + 2} {y} 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Footprint" ""
            (at {x} {y} 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        (property "Datasheet" "~"
            (at {x} {y} 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        {pin_entries}
    )"""


def _build_schematic(
    components: list[dict],
    no_connect_positions: list[tuple[float, float]] | None = None,
    wire_jitter: float = 0.0,
) -> str:
    """Build a complete schematic string from component descriptors.

    Each component dict has:
        ref: str          - reference designator (e.g. "U1", "R1")
        lib_id: str       - library identifier (e.g. "Device:R_Small")
        pins: list        - [(pin_num, pin_name, pin_type), ...]
        pin_nets: dict    - {pin_num: net_name, ...}  (optional)
        x: float          - X position (optional, defaults based on index)
        dnp: bool         - Do Not Populate flag (optional, default False)
        in_bom: bool      - In BOM flag (optional, default True)
        on_board: bool    - On Board flag (optional, default True)

    Args:
        wire_jitter: If non-zero, shift wire start X by this amount.
            This simulates the sub-grid rounding mismatch where a wire
            endpoint rounds to a different integer coordinate than the
            pin position computed from instance_pos + lib_pin_offset.
    """
    lib_symbols = []
    symbol_instances = []
    wires = []
    labels = []
    nc_blocks = []
    seen_lib_ids: set[str] = set()

    for idx, comp in enumerate(components):
        ref = comp["ref"]
        lib_id = comp["lib_id"]
        pins = comp["pins"]
        pin_nets = comp.get("pin_nets", {})
        x = comp.get("x", 100.0 + idx * 100.0)
        y = comp.get("y", 50.0)
        dnp = comp.get("dnp", False)
        in_bom = comp.get("in_bom", True)
        on_board = comp.get("on_board", True)

        if lib_id not in seen_lib_ids:
            lib_symbols.append(_make_lib_symbol(lib_id, pins))
            seen_lib_ids.add(lib_id)

        rotation = comp.get("rotation", 0)
        symbol_instances.append(
            _make_symbol_instance(
                ref, lib_id, pins, x, y, dnp, in_bom, on_board, rotation
            )
        )

        for pin_idx, (pin_num, _, _) in enumerate(pins):
            if pin_num not in pin_nets:
                continue
            net_name = pin_nets[pin_num]
            pin_y = y - pin_idx * 2.54
            pin_x = x + wire_jitter
            label_x = pin_x + 10.0

            wires.append(
                f"""(wire
                (pts (xy {pin_x:.4f} {pin_y:.4f}) (xy {label_x:.4f} {pin_y:.4f}))
                (stroke (width 0) (type default))
                (uuid "wire-{ref.lower()}-{pin_num}")
            )"""
            )
            labels.append(
                f"""(label "{net_name}"
                (at {label_x:.2f} {pin_y:.2f} 0)
                (effects (font (size 1.27 1.27)) (justify left bottom))
                (uuid "lbl-{ref.lower()}-{pin_num}")
            )"""
            )

    # Add no-connect markers
    if no_connect_positions:
        for i, (ncx, ncy) in enumerate(no_connect_positions):
            nc_blocks.append(
                f"""(no_connect
                (at {ncx:.2f} {ncy:.2f})
                (uuid "nc-{i}")
            )"""
            )

    lib_block = "\n".join(lib_symbols)
    inst_block = "\n".join(symbol_instances)
    wire_block = "\n".join(wires)
    label_block = "\n".join(labels)
    nc_block = "\n".join(nc_blocks)

    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-unconnected-uuid")
    (paper "A4")
    (lib_symbols
        {lib_block}
    )
    {inst_block}
    {wire_block}
    {label_block}
    {nc_block}
)
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFullyUnconnectedComponent:
    """Test check_fully_unconnected_components against synthetic schematics."""

    def test_fully_unconnected_flagged(self, tmp_path: Path):
        """A component with all pins floating should produce an error."""
        components = [
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                # No pin_nets -- all pins are floating
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "unconnected.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert len(errors) == 1
        assert "R1" in errors[0].message
        assert "floating" in errors[0].message

    def test_connected_component_not_flagged(self, tmp_path: Path):
        """A component with at least one connected pin should not be flagged."""
        components = [
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "NET1",
                    # pin 2 is unconnected, but pin 1 is connected
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "connected.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert errors == []

    def test_no_connect_markers_suppress_flag(self, tmp_path: Path):
        """A component with no-connect markers on pins should not be flagged."""
        # The symbol is at (100, 50) and pin 1 is at offset (0, 0) in the lib,
        # pin 2 is at offset (0, 2.54). The pin positions in the schematic will
        # be at (100, 50) and (100, 47.46) respectively.
        components = [
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "x": 100.0,
                "y": 50.0,
                # No pin_nets -- all pins are floating
            },
        ]
        # Place no-connect markers at the pin positions.
        # Pin positions are at the component origin since the lib pin is at (0, y)
        # and the component is at (100, 50).
        nc_positions = [(100.0, 50.0), (100.0, 47.46)]
        sch_text = _build_schematic(components, no_connect_positions=nc_positions)
        sch_path = tmp_path / "with_nc.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert errors == []

    def test_power_symbols_excluded(self, tmp_path: Path):
        """Power symbols should not be flagged even if unconnected."""
        components = [
            {
                "ref": "#PWR01",
                "lib_id": "power:GND",
                "pins": [
                    ("1", "GND", "power_in"),
                ],
                # No pin_nets -- unconnected power symbol
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "power.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert errors == []

    def test_dnp_symbols_excluded(self, tmp_path: Path):
        """DNP symbols should not be flagged even if unconnected."""
        components = [
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "dnp": True,
                # No pin_nets -- all pins floating, but DNP
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "dnp.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert errors == []

    def test_graphical_only_excluded(self, tmp_path: Path):
        """Graphical-only symbols (in_bom=no, on_board=no) should be excluded."""
        components = [
            {
                "ref": "LOGO1",
                "lib_id": "Graphic:Logo",
                "pins": [
                    ("1", "~", "passive"),
                ],
                "in_bom": False,
                "on_board": False,
                # No pin_nets -- floating, but graphical only
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "graphical.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert errors == []

    def test_all_pins_no_connect_type_excluded(self, tmp_path: Path):
        """Symbols whose library pins are all typed 'no_connect' should be excluded."""
        components = [
            {
                "ref": "TP1",
                "lib_id": "TestPoint:TestPoint",
                "pins": [
                    ("1", "~", "no_connect"),
                ],
                # No pin_nets
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "nc_type.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert errors == []

    def test_error_includes_reference_and_value(self, tmp_path: Path):
        """Error message should include both reference and value."""
        components = [
            {
                "ref": "C5",
                "lib_id": "Device:C",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                # No pin_nets
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "msg_check.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert len(errors) == 1
        assert "C5" in errors[0].message
        assert "C" in errors[0].message

    def test_error_includes_sheet_location(self, tmp_path: Path):
        """Error should report the sheet path."""
        components = [
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "location.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert len(errors) == 1
        assert errors[0].location != ""

    def test_multiple_unconnected_flagged(self, tmp_path: Path):
        """Multiple unconnected components should each be flagged."""
        components = [
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
            },
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
            },
            # This one IS connected -- should not be flagged
            {
                "ref": "R2",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "VCC",
                    "2": "GND",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "multiple.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        # Extract ref from "Fully unconnected component: R1 (..."
        refs_flagged = set()
        for e in errors:
            # Message format: "Fully unconnected component: REF (value) -- ..."
            parts = e.message.split(":")
            if len(parts) >= 2:
                after_colon = parts[1].strip()
                ref_part = after_colon.split()[0] if after_colon else ""
                refs_flagged.add(ref_part)
        assert "R1" in refs_flagged
        assert "C1" in refs_flagged
        assert "R2" not in refs_flagged

    def test_rotated_component_not_false_positive(self, tmp_path: Path):
        """A 90-degree rotated capacitor with correctly-placed wires must NOT
        be flagged as unconnected.

        This is the core regression from issue #2118: when Y was negated
        before rotation, pin positions for rotated symbols were mirror-imaged,
        causing every rotated component to appear unconnected.

        C_Small has pins at library coords (0, 0) and (0, 2.54).
        Placed at (100, 50) with 90-degree rotation:
          Pin 1 (0,0)    -> rotate 90 -> (0,0)      -> negate Y -> (0,0)
                           -> schematic (100, 50)
          Pin 2 (0,2.54) -> rotate 90 -> (-2.54, 0) -> negate Y -> (-2.54, 0)
                           -> schematic (97.46, 50)
        """
        # Build schematic with correctly-placed wires for the rotated symbol.
        lib_sym = _make_lib_symbol(
            "Device:C_Small",
            [("1", "~", "passive"), ("2", "~", "passive")],
        )
        sym_inst = _make_symbol_instance(
            "C1",
            "Device:C_Small",
            [("1", "~", "passive"), ("2", "~", "passive")],
            x=100.0,
            y=50.0,
            rotation=90,
        )
        # Wires at correct pin positions (post-fix):
        #   Pin 1 at (100, 50) -- wire runs right to a label
        #   Pin 2 at (97.46, 50) -- wire runs left to a label
        sch_text = f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-rotated-uuid")
    (paper "A4")
    (lib_symbols
        {lib_sym}
    )
    {sym_inst}
    (wire
        (pts (xy 100.00 50.00) (xy 110.00 50.00))
        (stroke (width 0) (type default))
        (uuid "wire-c1-1")
    )
    (wire
        (pts (xy 97.46 50.00) (xy 87.46 50.00))
        (stroke (width 0) (type default))
        (uuid "wire-c1-2")
    )
    (label "VCC"
        (at 110.00 50.00 0)
        (effects (font (size 1.27 1.27)) (justify left bottom))
        (uuid "lbl-c1-1")
    )
    (label "GND"
        (at 87.46 50.00 0)
        (effects (font (size 1.27 1.27)) (justify left bottom))
        (uuid "lbl-c1-2")
    )
)
"""
        sch_path = tmp_path / "rotated.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        # With the fix, the rotated C1 should NOT be flagged.
        assert len(errors) == 0, (
            f"Rotated component C1 should not be flagged; got: {errors}"
        )

    def test_subgrid_component_connected_via_snap(self, tmp_path: Path):
        """Component at sub-grid position should resolve as connected when
        wire endpoints are within 0.1mm (1 integer unit) of pin positions.

        This is the core false-positive scenario: the component is placed at
        a sub-grid position like (100.05, 50.03), causing _to_coord to round
        the pin position differently than the wire endpoint.  The _snap_coord
        tolerance should bridge the 1-unit gap.
        """
        # wire_jitter=0.05 shifts the wire start X by 0.05mm relative to
        # the pin position.  After _to_coord scaling (*10, round), the pin
        # coord is (1000, 500) and the wire start is (1001, 500) -- a 1-unit
        # difference that _snap_coord should resolve.
        components = [
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "NET_A",
                    "2": "NET_B",
                },
                "x": 100.0,
                "y": 50.0,
            },
        ]
        sch_text = _build_schematic(components, wire_jitter=0.05)
        sch_path = tmp_path / "subgrid_connected.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert errors == [], (
            f"Sub-grid component R1 should not be flagged as unconnected, "
            f"but got: {[e.message for e in errors]}"
        )

    def test_subgrid_truly_unconnected_still_flagged(self, tmp_path: Path):
        """Component at sub-grid position with no wires should still be flagged."""
        components = [
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                # No pin_nets -- truly unconnected
                "x": 100.05,
                "y": 50.03,
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "subgrid_unconnected.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component" and i.severity == "error"
        ]
        assert len(errors) == 1
        assert "R1" in errors[0].message
        assert "floating" in errors[0].message

    def test_near_miss_distance_in_diagnostic(self, tmp_path: Path):
        """When a component IS flagged, the message should include near-miss
        wire distances for diagnostic purposes."""
        # Place a connected component nearby so there ARE wires in the
        # schematic, then place an unconnected component close enough that
        # its pins are within 5mm of those wires.
        components = [
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                # No pin_nets -- unconnected
                "x": 100.0,
                "y": 50.0,
            },
            {
                "ref": "R2",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "VCC",
                    "2": "GND",
                },
                "x": 102.0,  # Close enough that R1 pins are within 5mm
                "y": 50.0,
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "near_miss.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_fully_unconnected_components(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "unconnected_component"
            and i.severity == "error"
            and "R1" in i.message
        ]
        assert len(errors) == 1
        # The message should contain near-miss pin distance info
        assert "nearest wire" in errors[0].message
        assert "pin" in errors[0].message
        assert "mm" in errors[0].message


class TestSnapCoord:
    """Unit tests for the _snap_coord tolerance function."""

    def test_exact_match_returns_same(self):
        """Coordinate already in the known set should be returned unchanged."""
        known = {(100, 200), (300, 400)}
        assert _snap_coord((100, 200), known) == (100, 200)

    def test_snap_within_tolerance(self):
        """Coordinate 1 unit away from a known node should snap to it."""
        known = {(100, 200)}
        # Off by 1 in X
        assert _snap_coord((101, 200), known) == (100, 200)
        # Off by 1 in Y
        assert _snap_coord((100, 201), known) == (100, 200)
        # Off by -1 in X
        assert _snap_coord((99, 200), known) == (100, 200)

    def test_no_snap_beyond_tolerance(self):
        """Coordinate more than 1 unit away should not snap."""
        known = {(100, 200)}
        # Off by 2 in X -- Manhattan distance 2, but default tolerance is 1
        result = _snap_coord((102, 200), known)
        assert result == (102, 200)

    def test_snap_picks_nearest(self):
        """When multiple known nodes are within tolerance, pick the closest."""
        known = {(100, 200), (102, 200)}
        # (101, 200) is equidistant -- either is acceptable
        result = _snap_coord((101, 200), known)
        assert result in known

    def test_empty_known_set(self):
        """Empty known set should return the original coordinate."""
        assert _snap_coord((100, 200), set()) == (100, 200)

    def test_diagonal_within_tolerance(self):
        """Diagonal offset of (1, 1) has Manhattan distance 2, which exceeds
        the default tolerance of 1, so it should NOT snap."""
        known = {(100, 200)}
        result = _snap_coord((101, 201), known)
        assert result == (101, 201)

    def test_custom_tolerance(self):
        """With tolerance=2, diagonal (1,1) should snap."""
        known = {(100, 200)}
        result = _snap_coord((101, 201), known, tolerance=2)
        assert result == (100, 200)
