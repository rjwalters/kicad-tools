"""IC-category text on a discrete footprint: whole LCSC phrases only (issue #8124).

The #8113 guard substring-matched bare ``LOGIC`` / ``MEMORY`` in the part
description, so a diode "for logic lines" or a "memory backup" cap FAILed
``bom_lcsc_values`` and blocked tapeout.  Real LCSC IC descriptions carry the
category as a phrase ("Logic Gates", "Non-Volatile Memory (ROM)"), which is
what the guard now requires, on word boundaries.
"""

from __future__ import annotations

import pytest

from kicad_tools.export.lcsc_value_check import find_discrete_footprint_ic_mismatch

SOD323 = "Diode_SMD:D_SOD-323"
SMA = "Diode_SMD:D_SMA"


@pytest.mark.parametrize(
    "footprint,package,desc",
    [
        (SOD323, "SOD-323", "ESD protection diode for logic lines SOD-323"),
        (SMA, "SMA(DO-214AC)", "Memory backup supercapacitor"),
        ("Capacitor_SMD:C_0805_2012Metric", "0805", "Memory backup supercapacitor"),
        (SOD323, "SOD-323", "logic, level clamp diode"),
        (SOD323, "SOD-323", "Logic"),
        (SOD323, "SOD-323", "TVS for memory"),
        (SOD323, "SOD-323", "Switching diode, TinyLogic compatible"),
        (SOD323, "SOD-323", "Diode for LOGICAL inputs"),
        (SMA, "SMA(DO-214AC)", "Ultra-fast rectifier diode"),
    ],
)
def test_generic_words_in_discrete_descriptions_do_not_flag(footprint, package, desc):
    assert find_discrete_footprint_ic_mismatch(footprint, package, desc) is None


@pytest.mark.parametrize(
    "desc,label",
    [
        ("8-bit Microcontrollers (MCU)", "Microcontrollers"),
        ("600MHz LQFP-48 Microcontrollers (MCU/MPU/SOC) ROHS", "Microcontrollers"),
        ("USB 2.0 Interface ICs ROHS", "Interface Ics"),
        ("74HC OR Gate Logic Gates ROHS", "Logic Gates"),
        ("logic gates", "Logic Gates"),
        ("Specialty Logic", "Specialty Logic"),
        ("8Kbit I2C Non-Volatile Memory (ROM) ROHS", "Non-Volatile Memory"),
        ("Rail-to-rail Differential Amplifiers ROHS", "Amplifiers"),
        ("Sense amplifiers,", "Amplifiers"),
    ],
)
def test_real_ic_category_phrases_still_flag(desc, label):
    # Empty package: only the category-text clause can fire (schematic-only BOM).
    mm = find_discrete_footprint_ic_mismatch(SMA, "", desc)
    assert mm is not None
    assert f"IC category '{label}'" in mm.candidate_source


def test_ic_package_branch_unchanged():
    mm = find_discrete_footprint_ic_mismatch(SMA, "BGA-196", "no category text here")
    assert mm is not None
    assert mm.candidate_source.startswith("IC package BGA-196")
