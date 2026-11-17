"""An IC part on a discrete footprint must not verify as ``match`` (issue #8113).

C188198 (NXP MIMXRT1052, BGA-196) shipped on a ``Diode_SMD:D_SMA`` footprint
because ``parse_ic_package`` did not know BGA, so the pad-count comparison
never ran.  Offline: injected fake cache, no network.
"""

from __future__ import annotations

import pytest

from kicad_tools.export.lcsc_value_check import parse_ic_package, verify_lcsc_assignment
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from kicad_tools.parts.cache import PartsCache
from kicad_tools.parts.models import Part
from kicad_tools.schema.bom import BOM, BOMItem

FP_SMA = "Diode_SMD:D_SMA"

MCU = Part(
    lcsc_part="C188198",
    mfr_part="MIMXRT1052CVL5B",
    package="BGA-196",
    description="600MHz Power-on reset (POR) BGA-196 Microcontrollers (MCU/MPU/SOC) ROHS",
)


def _cache(tmp_path, *parts: Part) -> PartsCache:
    cache = PartsCache(db_path=tmp_path / "parts.db")
    for p in parts:
        cache.put(p)
    return cache


@pytest.mark.parametrize("text", ["BGA-196", "LFBGA-196(10x10)", "WLCSP-49"])
def test_parse_bga_family(text):
    pkg = parse_ic_package(text)
    assert pkg is not None and pkg.family == "BGA"
    assert pkg.pins == int(text.split("-")[1].split("(")[0])


@pytest.mark.parametrize("pads", [["1", "2"], None])
def test_c188198_on_diode_footprint_is_mismatch(tmp_path, pads):
    verdict = verify_lcsc_assignment(
        _cache(tmp_path, MCU), None, "C188198", "M7", "D1", footprint=FP_SMA, pad_numbers=pads
    )
    assert verdict.status == "mismatch"


@pytest.mark.parametrize(
    "package,desc,footprint",
    [
        ("SOT-23", "Schottky diode SOT-23", "Diode_SMD:D_SOT-23_ANK"),
        ("SOD-123", "Switching diode SOD-123", "Diode_SMD:D_SOD-123"),
        ("SMA(DO-214AC)", "TVS diode SMA", FP_SMA),
    ],
)
def test_real_diodes_stay_match(tmp_path, package, desc, footprint):
    part = Part(lcsc_part="C1234", mfr_part="D1", package=package, description=desc)
    verdict = verify_lcsc_assignment(
        _cache(tmp_path, part), None, "C1234", "D", "D1", footprint=footprint
    )
    assert verdict.status == "match"


def test_category_clause_without_parsable_package(tmp_path):
    part = Part(
        lcsc_part="C9", mfr_part="X", package="", description="8-bit Microcontrollers (MCU)"
    )
    verdict = verify_lcsc_assignment(
        _cache(tmp_path, part), None, "C9", "M7", "D1", footprint=FP_SMA
    )
    assert verdict.status == "mismatch"


def test_bga_part_on_matching_bga_footprint_matches(tmp_path):
    pads = [str(i) for i in range(1, 197)]
    verdict = verify_lcsc_assignment(
        _cache(tmp_path, MCU),
        None,
        "C188198",
        "MIMXRT1052",
        "U1",
        footprint="Package_BGA:BGA-196_15.0x15.0mm",
        pad_numbers=pads,
    )
    assert verdict.status == "match"


def test_preflight_fails_on_c188198_row(tmp_path):
    checker = PreflightChecker(
        pcb_path=tmp_path / "board.kicad_pcb",
        config=PreflightConfig(skip_drc=True, skip_erc=True),
        parts_cache=_cache(tmp_path, MCU),
        parts_catalog=None,
    )
    checker._bom = BOM(
        items=[
            BOMItem(reference="D1", value="M7", footprint=FP_SMA, lib_id="Device:D", lcsc="C188198")
        ]
    )
    result = checker._check_bom_lcsc_values()
    assert result.status == "FAIL"
    assert "C188198" in result.details
    assert "discrete" in result.details or "pin count" in result.details
