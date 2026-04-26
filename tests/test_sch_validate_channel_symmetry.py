"""Tests for matched channel symmetry detection in sch validate."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_validate import (
    ValidationIssue,
    _ChannelFilterSignature,
    _find_matched_pairs,
    check_matched_channel_symmetry,
)


# ---------------------------------------------------------------------------
# Unit tests for pair detection
# ---------------------------------------------------------------------------


class TestFindMatchedPairs:
    """Test _find_matched_pairs suffix matching."""

    def test_stereo_lr(self):
        pairs = _find_matched_pairs({"AUDIO_L", "AUDIO_R", "GND"})
        assert len(pairs) == 1
        net_a, net_b, base, ptype = pairs[0]
        assert {net_a, net_b} == {"AUDIO_L", "AUDIO_R"}
        assert base == "AUDIO"
        assert ptype == "stereo"

    def test_differential_pn(self):
        pairs = _find_matched_pairs({"USB_D_P", "USB_D_N", "GND"})
        assert len(pairs) == 1
        _, _, base, ptype = pairs[0]
        assert base == "USB_D"
        assert ptype == "differential"

    def test_channel_ab(self):
        pairs = _find_matched_pairs({"CH_A", "CH_B"})
        assert len(pairs) == 1
        _, _, _, ptype = pairs[0]
        assert ptype == "stereo"

    def test_plus_minus(self):
        pairs = _find_matched_pairs({"VOUT+", "VOUT-"})
        assert len(pairs) == 1
        _, _, _, ptype = pairs[0]
        assert ptype == "differential"

    def test_no_pair(self):
        pairs = _find_matched_pairs({"GPIO_A5", "UART_TX", "GND"})
        assert pairs == []

    def test_single_net_no_match(self):
        pairs = _find_matched_pairs({"AUDIO_L"})
        assert pairs == []

    def test_deduplication(self):
        """Each pair should appear exactly once even though both sides match."""
        pairs = _find_matched_pairs({"SIG_L", "SIG_R"})
        assert len(pairs) == 1

    def test_multiple_pairs(self):
        pairs = _find_matched_pairs(
            {"AUDIO_L", "AUDIO_R", "USB_D_P", "USB_D_N"}
        )
        assert len(pairs) == 2


class TestChannelFilterSignature:
    """Test _ChannelFilterSignature.describe_diff."""

    def test_identical_signatures_no_diff(self):
        a = _ChannelFilterSignature(shunt_caps=2, series_components=["R"])
        b = _ChannelFilterSignature(shunt_caps=2, series_components=["R"])
        assert a.describe_diff(b) is None

    def test_different_shunt_caps(self):
        a = _ChannelFilterSignature(shunt_caps=2)
        b = _ChannelFilterSignature(shunt_caps=0)
        diff = a.describe_diff(b)
        assert diff is not None
        assert "shunt caps" in diff
        assert "2 vs 0" in diff

    def test_different_series(self):
        a = _ChannelFilterSignature(series_components=["R", "C"])
        b = _ChannelFilterSignature(series_components=["R"])
        diff = a.describe_diff(b)
        assert diff is not None
        assert "series components" in diff

    def test_total_passives(self):
        sig = _ChannelFilterSignature(
            shunt_caps=1, shunt_resistors=1, series_components=["R"]
        )
        assert sig.total_passives == 3


# ---------------------------------------------------------------------------
# Helpers to generate synthetic KiCad schematics
# ---------------------------------------------------------------------------


def _make_lib_symbol(
    lib_id: str,
    pins: list[tuple[str, str, str]],
) -> str:
    """Generate a lib_symbols entry."""
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
    ref: str, lib_id: str, pins: list[tuple[str, str, str]], x: float, y: float,
) -> str:
    """Generate a symbol instance S-expression."""
    pin_entries = "\n".join(
        f'(pin "{num}" (uuid "pin-{ref.lower()}-{num}"))' for num, _, _ in pins
    )
    return f"""(symbol
        (lib_id "{lib_id}")
        (at {x} {y} 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp no)
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


def _build_schematic(components: list[dict]) -> str:
    """Build a complete schematic string from component descriptors."""
    lib_symbols = []
    symbol_instances = []
    wires = []
    labels = []
    seen_lib_ids: set[str] = set()

    for idx, comp in enumerate(components):
        ref = comp["ref"]
        lib_id = comp["lib_id"]
        pins = comp["pins"]
        pin_nets = comp.get("pin_nets", {})
        x = comp.get("x", 100.0 + idx * 100.0)
        y = comp.get("y", 50.0)

        if lib_id not in seen_lib_ids:
            lib_symbols.append(_make_lib_symbol(lib_id, pins))
            seen_lib_ids.add(lib_id)

        symbol_instances.append(_make_symbol_instance(ref, lib_id, pins, x, y))

        for pin_idx, (pin_num, _, _) in enumerate(pins):
            if pin_num not in pin_nets:
                continue
            net_name = pin_nets[pin_num]
            pin_y = y - pin_idx * 2.54
            pin_x = x
            label_x = pin_x + 10.0

            wires.append(
                f"""(wire
                (pts (xy {pin_x:.2f} {pin_y:.2f}) (xy {label_x:.2f} {pin_y:.2f}))
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

    lib_block = "\n".join(lib_symbols)
    inst_block = "\n".join(symbol_instances)
    wire_block = "\n".join(wires)
    label_block = "\n".join(labels)

    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-channel-symmetry-uuid")
    (paper "A4")
    (lib_symbols
        {lib_block}
    )
    {inst_block}
    {wire_block}
    {label_block}
)
"""


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestCheckMatchedChannelSymmetry:
    """Test check_matched_channel_symmetry against synthetic schematics."""

    def test_asymmetric_shunt_caps_detected(self, tmp_path: Path):
        """AUDIO_R has 2 shunt caps to GND, AUDIO_L has 0 -- should warn."""
        components = [
            {
                "ref": "U1",
                "lib_id": "IC:DAC",
                "pins": [
                    ("1", "OUTL", "output"),
                    ("2", "OUTR", "output"),
                    ("3", "VCC", "power_in"),
                    ("4", "GND", "power_in"),
                ],
                "pin_nets": {
                    "1": "AUDIO_L",
                    "2": "AUDIO_R",
                    "3": "+3.3V",
                    "4": "GND",
                },
            },
            # Shunt cap on AUDIO_R only
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "AUDIO_R",
                    "2": "GND",
                },
            },
            {
                "ref": "C2",
                "lib_id": "Device:C",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "AUDIO_R",
                    "2": "GND",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "asymmetric_caps.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_matched_channel_symmetry(str(sch_path))
        warnings = [
            i for i in issues
            if i.category == "matched_channel_symmetry"
            and i.severity == "warning"
        ]
        assert len(warnings) == 1
        assert "AUDIO_L" in warnings[0].message
        assert "AUDIO_R" in warnings[0].message
        assert "shunt caps" in warnings[0].message
        assert "2 vs 0" in warnings[0].message or "0 vs 2" in warnings[0].message

    def test_symmetric_channels_no_warning(self, tmp_path: Path):
        """Both channels have identical filter topology -- no warning."""
        components = [
            {
                "ref": "U1",
                "lib_id": "IC:DAC",
                "pins": [
                    ("1", "OUTL", "output"),
                    ("2", "OUTR", "output"),
                ],
                "pin_nets": {
                    "1": "AUDIO_L",
                    "2": "AUDIO_R",
                },
            },
            # Cap on AUDIO_L
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "AUDIO_L",
                    "2": "GND",
                },
            },
            # Cap on AUDIO_R
            {
                "ref": "C2",
                "lib_id": "Device:C",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "AUDIO_R",
                    "2": "GND",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "symmetric.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_matched_channel_symmetry(str(sch_path))
        symmetry_issues = [
            i for i in issues
            if i.category == "matched_channel_symmetry"
            and i.severity in ("warning", "error")
        ]
        assert symmetry_issues == []

    def test_no_passives_no_false_positive(self, tmp_path: Path):
        """Matched nets with no passive components should not trigger."""
        components = [
            {
                "ref": "U1",
                "lib_id": "IC:DAC",
                "pins": [
                    ("1", "OUTL", "output"),
                    ("2", "OUTR", "output"),
                ],
                "pin_nets": {
                    "1": "AUDIO_L",
                    "2": "AUDIO_R",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "no_passives.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_matched_channel_symmetry(str(sch_path))
        symmetry_issues = [
            i for i in issues
            if i.category == "matched_channel_symmetry"
            and i.severity in ("warning", "error")
        ]
        assert symmetry_issues == []

    def test_differential_pair_error_severity(self, tmp_path: Path):
        """Asymmetry on _P/_N differential pair should be flagged as error."""
        components = [
            {
                "ref": "U1",
                "lib_id": "IC:USB",
                "pins": [
                    ("1", "DP", "bidirectional"),
                    ("2", "DN", "bidirectional"),
                ],
                "pin_nets": {
                    "1": "USB_D_P",
                    "2": "USB_D_N",
                },
            },
            # Cap only on _P side
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "USB_D_P",
                    "2": "GND",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "diff_asymmetric.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_matched_channel_symmetry(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "matched_channel_symmetry"
            and i.severity == "error"
        ]
        assert len(errors) == 1
        assert "USB_D_P" in errors[0].message
        assert "USB_D_N" in errors[0].message

    def test_series_component_asymmetry(self, tmp_path: Path):
        """One channel has a series resistor, the other does not."""
        components = [
            {
                "ref": "U1",
                "lib_id": "IC:DAC",
                "pins": [
                    ("1", "OUTL", "output"),
                    ("2", "OUTR", "output"),
                ],
                "pin_nets": {
                    "1": "AUDIO_L",
                    "2": "AUDIO_R",
                },
            },
            # Series resistor on AUDIO_R only (to a downstream net)
            {
                "ref": "R1",
                "lib_id": "Device:R",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "AUDIO_R",
                    "2": "AUDIO_R_FILT",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "series_asymmetric.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_matched_channel_symmetry(str(sch_path))
        warnings = [
            i for i in issues
            if i.category == "matched_channel_symmetry"
            and i.severity == "warning"
        ]
        assert len(warnings) == 1
        assert "series" in warnings[0].message

    def test_check_registered_in_validate(self, tmp_path: Path):
        """matched_channel_symmetry should appear in checks_run."""
        from kicad_tools.cli.sch_validate import validate_schematic

        # Minimal schematic with no components
        sch_text = """(kicad_sch
    (version 20231120)
    (generator "test")
    (uuid "test-uuid")
    (paper "A4")
    (lib_symbols)
)
"""
        sch_path = tmp_path / "empty.kicad_sch"
        sch_path.write_text(sch_text)

        result = validate_schematic(str(sch_path))
        assert "matched_channel_symmetry" in result.checks_run

    def test_ab_pair_detection(self, tmp_path: Path):
        """_A/_B suffix pairs should be detected."""
        components = [
            {
                "ref": "U1",
                "lib_id": "IC:AMP",
                "pins": [
                    ("1", "OUTA", "output"),
                    ("2", "OUTB", "output"),
                ],
                "pin_nets": {
                    "1": "CH_A",
                    "2": "CH_B",
                },
            },
            # Cap only on CH_A
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "CH_A",
                    "2": "GND",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "ab_pair.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_matched_channel_symmetry(str(sch_path))
        warnings = [
            i for i in issues
            if i.category == "matched_channel_symmetry"
            and i.severity == "warning"
        ]
        assert len(warnings) == 1
        assert "CH_A" in warnings[0].message
        assert "CH_B" in warnings[0].message
