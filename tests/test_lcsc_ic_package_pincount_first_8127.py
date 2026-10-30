"""Pin-count-first LCSC BGA strings and LFCSP parse as ICs (issue #8127)."""

from __future__ import annotations

import pytest

from kicad_tools.export.lcsc_value_check import (
    find_discrete_footprint_ic_mismatch,
    parse_ic_package,
)


@pytest.mark.parametrize(
    ("text", "equivalent", "pins"),
    [
        ("48-TFBGA (6x8)", "TFBGA-48", 48),
        ("54-VFBGA (6x9)", "VFBGA-54", 54),
    ],
)
def test_pincount_first_bga(text, equivalent, pins):
    pkg = parse_ic_package(text)
    assert pkg is not None
    assert pkg.family == "BGA" and pkg.pins == pins
    assert (pkg.family, pkg.pins) == (
        parse_ic_package(equivalent).family,
        parse_ic_package(equivalent).pins,
    )


def test_lfcsp_is_qfn_group():
    pkg = parse_ic_package("LFCSP-32-WQ(5x5)")
    assert pkg is not None
    assert (pkg.family, pkg.pins, pkg.body, pkg.exposed_pad) == ("QFN", 32, (5.0, 5.0), True)


@pytest.mark.parametrize("text", ["8-SOIC", "SMD-4P", "0603", "2-PIN"])
def test_non_ic_unchanged(text):
    assert parse_ic_package(text) is None


def test_discrete_footprint_flags_pincount_first_bga():
    result = find_discrete_footprint_ic_mismatch("Diode_SMD:D_SMA", "48-TFBGA (6x8)", "")
    assert result is not None


def test_sot363_stays_excluded():
    assert find_discrete_footprint_ic_mismatch("Diode_SMD:D_SMA", "SOT-363", "") is None
