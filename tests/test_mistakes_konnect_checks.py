"""Tests for the Konnect design-review audit-parity checks (issue #4899).

Covers the four net-new ``MistakeCheck`` implementations added for parity
with Konnect's ``audit_decoupling`` / ``audit_connections`` /
``check_bom_health`` rule categories, plus the #4011-style
``CheckIncomplete`` coverage-honesty discipline added to
``MistakeDetector``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.explain.checks.bom_health import BomFieldHealthCheck
from kicad_tools.explain.checks.connectivity import (
    LedSeriesResistorCheck,
    PullUpResistorCheck,
)
from kicad_tools.explain.checks.decoupling import MissingDecouplingCapCheck
from kicad_tools.explain.mistakes import (
    CheckIncomplete,
    MistakeCategory,
    MistakeDetector,
    get_default_checks,
)
from kicad_tools.schema.pcb import PCB

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
"""

_FOOTER = ")\n"


def _load_pcb(tmp_path: Path, body: str) -> PCB:
    """Write a minimal PCB (header + body) to *tmp_path* and load it."""
    pcb_file = tmp_path / "board.kicad_pcb"
    pcb_file.write_text(_HEADER + body + _FOOTER)
    return PCB.load(str(pcb_file))


# ---------------------------------------------------------------------------
# MissingDecouplingCapCheck
# ---------------------------------------------------------------------------


_IC_NO_CAP = """
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG")
  (net 4 "SIG2")

  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "U1")
    (property "Value" "MCU")
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))
    (pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))
    (pad "4" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 4 "SIG2"))
  )
"""

_IC_WITH_CAP = (
    _IC_NO_CAP
    + """
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (at 12 10)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
"""
)


def test_missing_decoupling_cap_flags_ic_with_no_cap(tmp_path: Path) -> None:
    pcb = _load_pcb(tmp_path, _IC_NO_CAP)
    mistakes = MissingDecouplingCapCheck().check(pcb)
    assert len(mistakes) == 1
    m = mistakes[0]
    assert m.category == MistakeCategory.DECOUPLING
    assert m.severity == "error"
    assert m.components == ["U1"]


def test_missing_decoupling_cap_passes_when_cap_present(tmp_path: Path) -> None:
    pcb = _load_pcb(tmp_path, _IC_WITH_CAP)
    mistakes = MissingDecouplingCapCheck().check(pcb)
    assert mistakes == []


def test_missing_decoupling_cap_ignores_boards_with_no_ics(tmp_path: Path) -> None:
    """Zero applicable components (no IC-like footprints) -> clean, no flag."""
    body = """
  (net 1 "VCC")
  (net 2 "GND")
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
"""
    pcb = _load_pcb(tmp_path, body)
    assert MissingDecouplingCapCheck().check(pcb) == []


# ---------------------------------------------------------------------------
# PullUpResistorCheck
# ---------------------------------------------------------------------------

_I2C_NO_PULLUP = """
  (net 1 "SDA")
  (net 2 "SCL")

  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "U1")
    (property "Value" "MCU")
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "SDA"))
    (pad "2" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "SCL"))
  )
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "U2")
    (property "Value" "SENSOR")
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "SDA"))
    (pad "2" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "SCL"))
  )
"""

_I2C_WITH_PULLUP = (
    _I2C_NO_PULLUP
    + """
  (net 3 "+3V3")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 5 5)
    (property "Reference" "R1")
    (property "Value" "4.7k")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "SDA"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "+3V3"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 6 5)
    (property "Reference" "R2")
    (property "Value" "4.7k")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "SCL"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "+3V3"))
  )
"""
)


def test_pullup_resistor_flags_i2c_bus_without_pullup(tmp_path: Path) -> None:
    pcb = _load_pcb(tmp_path, _I2C_NO_PULLUP)
    mistakes = PullUpResistorCheck().check(pcb)
    titles = {m.title for m in mistakes}
    assert "Missing pull-up resistor" in titles
    assert len(mistakes) == 2  # SDA and SCL each flagged


def test_pullup_resistor_passes_with_pullup(tmp_path: Path) -> None:
    pcb = _load_pcb(tmp_path, _I2C_WITH_PULLUP)
    mistakes = PullUpResistorCheck().check(pcb)
    assert mistakes == []


def test_pullup_resistor_ignores_single_pad_net(tmp_path: Path) -> None:
    """A net named SDA touching only one footprint is not a real bus."""
    body = """
  (net 1 "SDA")
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "U1")
    (property "Value" "MCU")
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "SDA"))
  )
"""
    pcb = _load_pcb(tmp_path, body)
    assert PullUpResistorCheck().check(pcb) == []


# ---------------------------------------------------------------------------
# LedSeriesResistorCheck
# ---------------------------------------------------------------------------

_LED_NO_RESISTOR = """
  (net 1 "LED_A")
  (net 2 "GND")

  (footprint "LED_SMD:LED_0603"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "D1")
    (property "Value" "LED")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "LED_A"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
"""

_LED_WITH_RESISTOR = (
    _LED_NO_RESISTOR
    + """
  (net 3 "+3V3")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 8 10)
    (property "Reference" "R1")
    (property "Value" "330")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "+3V3"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "LED_A"))
  )
"""
)


def test_led_series_resistor_flags_led_without_resistor(tmp_path: Path) -> None:
    pcb = _load_pcb(tmp_path, _LED_NO_RESISTOR)
    mistakes = LedSeriesResistorCheck().check(pcb)
    assert len(mistakes) == 1
    assert mistakes[0].components == ["D1"]


def test_led_series_resistor_passes_with_resistor(tmp_path: Path) -> None:
    pcb = _load_pcb(tmp_path, _LED_WITH_RESISTOR)
    assert LedSeriesResistorCheck().check(pcb) == []


def test_led_series_resistor_ignores_boards_with_no_leds(tmp_path: Path) -> None:
    body = """
  (net 1 "SIG")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "SIG"))
  )
"""
    pcb = _load_pcb(tmp_path, body)
    assert LedSeriesResistorCheck().check(pcb) == []


# ---------------------------------------------------------------------------
# BomFieldHealthCheck
# ---------------------------------------------------------------------------

_BOM_MIXED_COVERAGE = """
  (net 1 "VCC")
  (net 2 "GND")

  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "U1")
    (property "Value" "MCU")
    (property "MPN" "STM32F103C8T6")
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "VCC"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
"""

_BOM_NO_TRACKING = """
  (net 1 "VCC")
  (net 2 "GND")

  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "U1")
    (property "Value" "MCU")
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "VCC"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
"""

_BOM_ALL_EXCLUDED = """
  (net 1 "VCC")

  (footprint "MountingHole:MountingHole_3.2mm"
    (layer "F.Cu")
    (at 10 10)
    (attr exclude_from_bom exclude_from_pos_files)
    (property "Reference" "MH1")
    (property "Value" "MountingHole")
  )
"""


def test_bom_field_health_flags_component_missing_mpn(tmp_path: Path) -> None:
    pcb = _load_pcb(tmp_path, _BOM_MIXED_COVERAGE)
    mistakes = BomFieldHealthCheck().check(pcb)
    assert len(mistakes) == 1
    assert mistakes[0].components == ["R1"]
    assert mistakes[0].category == MistakeCategory.BOM_HEALTH


def test_bom_field_health_raises_incomplete_when_no_board_wide_tracking(
    tmp_path: Path,
) -> None:
    """No component anywhere defines an MPN/LCSC field -> CheckIncomplete."""
    pcb = _load_pcb(tmp_path, _BOM_NO_TRACKING)
    with pytest.raises(CheckIncomplete):
        BomFieldHealthCheck().check(pcb)


def test_bom_field_health_no_candidates_is_a_clean_ran_check(tmp_path: Path) -> None:
    """Zero BOM-eligible components (all excluded) -> clean pass, not incomplete."""
    pcb = _load_pcb(tmp_path, _BOM_ALL_EXCLUDED)
    assert BomFieldHealthCheck().check(pcb) == []


# ---------------------------------------------------------------------------
# MistakeDetector coverage discipline (mirrors #4011 for `lvs`)
# ---------------------------------------------------------------------------


def test_detector_reports_incomplete_coverage_without_dropping_mistakes(
    tmp_path: Path,
) -> None:
    pcb = _load_pcb(tmp_path, _BOM_NO_TRACKING)
    detector = MistakeDetector()
    mistakes, coverage = detector.detect_with_coverage(pcb)

    # One CheckCoverage entry per registered check.
    assert len(coverage) == len(detector.checks)

    incomplete = [c for c in coverage if c.status == "incomplete"]
    assert len(incomplete) == 1
    assert incomplete[0].check_name == "BomFieldHealthCheck"
    assert incomplete[0].category == MistakeCategory.BOM_HEALTH
    assert incomplete[0].reason  # non-empty explanation

    # Every other check still ran and contributed to `mistakes` normally --
    # the incomplete BOM check does not silently swallow other findings.
    ran_names = {c.check_name for c in coverage if c.status == "ran"}
    assert "BomFieldHealthCheck" not in ran_names
    assert len(ran_names) == len(detector.checks) - 1
    assert isinstance(mistakes, list)


def test_detector_detect_still_returns_plain_list_backward_compatible(
    tmp_path: Path,
) -> None:
    """`detect()` keeps its original list[Mistake] signature (no crash,
    no coverage info required of legacy callers) even when a check is
    incomplete."""
    pcb = _load_pcb(tmp_path, _BOM_NO_TRACKING)
    mistakes = MistakeDetector().detect(pcb)
    assert isinstance(mistakes, list)


def test_get_default_checks_registers_all_four_new_checks() -> None:
    checks = get_default_checks()
    names = {type(c).__name__ for c in checks}
    assert {
        "MissingDecouplingCapCheck",
        "PullUpResistorCheck",
        "LedSeriesResistorCheck",
        "BomFieldHealthCheck",
    } <= names


@pytest.mark.parametrize("return_net", ['(net 3 "SIG")', '(net 1 "VCC")'])
def test_decoupling_requires_distinct_ground_return(tmp_path: Path, return_net: str) -> None:
    ic, cap = _IC_WITH_CAP.split('  (footprint "Capacitor_SMD:C_0402"')
    cap = cap.replace('(net 2 "GND")', return_net)
    pcb = _load_pcb(tmp_path, ic + '  (footprint "Capacitor_SMD:C_0402"' + cap)
    assert len(MissingDecouplingCapCheck().check(pcb)) == 1


@pytest.mark.parametrize("topology", ["unrelated_ground", "parallel", "same_net", "branched"])
def test_led_requires_series_topology(tmp_path: Path, topology: str) -> None:
    led, resistor = _LED_WITH_RESISTOR.split('  (footprint "Resistor_SMD:R_0402"')
    if topology == "unrelated_ground":
        resistor = resistor.replace('(net 1 "LED_A")', '(net 2 "GND")')
    elif topology == "parallel":
        resistor = resistor.replace('(net 3 "+3V3")', '(net 2 "GND")')
    elif topology == "same_net":
        resistor = resistor.replace('(net 3 "+3V3")', '(net 1 "LED_A")')
    body = led + '  (footprint "Resistor_SMD:R_0402"' + resistor
    if topology == "branched":
        body += """
  (footprint "Connector:PinHeader"
    (layer "F.Cu") (at 20 20)
    (property "Reference" "J1") (property "Value" "GPIO")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "LED_A"))
  )
"""
    pcb = _load_pcb(tmp_path, body)
    assert len(LedSeriesResistorCheck().check(pcb)) == 1


def test_led_low_side_series_resistor(tmp_path: Path) -> None:
    # Swap the external rails while preserving the exclusive LED/R junction.
    body = _LED_WITH_RESISTOR.replace('"GND"', '"VCC"').replace('"+3V3"', '"GND"')
    assert LedSeriesResistorCheck().check(_load_pcb(tmp_path, body)) == []
