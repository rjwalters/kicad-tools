"""DIP bridge rectifiers on Diode_Bridge_DIP-N footprints (issue #8132)."""

from __future__ import annotations

import pytest

from kicad_tools.export.lcsc_value_check import find_discrete_footprint_ic_mismatch as find

DESC = "600V 1A Bridge Rectifier DIP-4"


@pytest.mark.parametrize(
    "footprint",
    [
        "Diode_THT:Diode_Bridge_DIP-4_W7.62mm_P5.08mm",
        "Diode_THT:Diode_Bridge_DIP-4_W5.08mm_P2.54mm",
    ],
)
def test_dip4_bridge_on_dip4_bridge_footprint_passes(footprint):
    assert find(footprint, "DIP-4", DESC) is None


@pytest.mark.parametrize(
    "footprint,package",
    [
        ("Diode_THT:Diode_Bridge_DIP-4_W7.62mm_P5.08mm", "DIP-8"),
        ("Diode_THT:D_DO-41_SOD81_P10.16mm_Horizontal", "DIP-8"),
        ("Diode_THT:D_DO-41_SOD81_P10.16mm_Horizontal", "DIP-4"),
        ("Diode_SMD:D_SMA", "BGA-196"),
        ("Diode_SMD:D_SMA", "DIP-4"),
    ],
)
def test_other_ic_packages_still_fail(footprint, package):
    assert find(footprint, package, "") is not None
