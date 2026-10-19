"""Pin-header row x pin count guard (issue #7446)."""

from __future__ import annotations

import pytest

from kicad_tools.export.lcsc_value_check import (
    UNVERIFIED_HEADER_COUNT,
    find_header_pin_mismatch,
    header_pin_count_unreadable,
    verify_lcsc_assignment,
)
from kicad_tools.parts.cache import PartsCache
from kicad_tools.parts.models import Part

FP = "Connector_PinHeader_2.54mm:PinHeader_{}_P2.54mm_Vertical"


def _pads(n):
    return [str(i) for i in range(1, n + 1)]


@pytest.mark.parametrize(
    "lcsc,mfr,desc,fp,pads",
    [
        ("C492401", "PZ254V-11-02P", "Pin Header 1x2P 2.54mm", "1x08", 8),
        ("C492407", "PZ254V-11-08P", "Pin Header 1x8P 2.54mm", "1x02", 2),
    ],
)
def test_issue_repro_rows_mismatch(tmp_path, lcsc, mfr, desc, fp, pads):
    cache = PartsCache(db_path=tmp_path / "c.db")
    cache.put(Part(lcsc_part=lcsc, mfr_part=mfr, package="Plugin,P=2.54mm", description=desc))
    v = verify_lcsc_assignment(
        cache, None, lcsc, "x", "J2", footprint=FP.format(fp), pad_numbers=_pads(pads)
    )
    assert v.status == "mismatch"


@pytest.mark.parametrize(
    "mfr,desc,fp,pads",
    [
        ("PZ254V-11-02P", "1x2P", "1x02", 2),
        ("PZ254V-11-08P", "", "1x08", 8),
        ("2.54-2*3P", "", "2x03", 6),
        ("2.54-1x6P", "", "1x06", 6),
        ("B-2100S02P-A110", "Through-hole, 1x2, 2.54mm", "1x02", 2),
        ("", "5.84mm 6P 2x3P", "2x03", 6),
        ("ZX-PZ2.54-1-2PZZ", "Pin Header 2 Position 2.54mm Pitch Single Row", "1x02", 2),
        ("S5B-PH-SM4-TB(LF)(SN)", "Connector Header 5 position 2mm Pitch", "1x05", 5),
        ("", "Pin Header 8 Pin 2.54mm", "1x08", 8),
        ("", "8-pin header, 2.54mm", "1x08", 8),
        ("", "2.54mm Pins Header", "1x08", 8),  # unparseable -> no mismatch
    ],
)
def test_matching_or_unparseable_is_none(mfr, desc, fp, pads):
    assert find_header_pin_mismatch(FP.format(fp), _pads(pads), "", desc, mfr) is None


@pytest.mark.parametrize(
    "mfr,fp",
    [
        ("2.54-1x6P", "2x03"),  # same total, different shape
        ("2.54-2*3P", "1x06"),
        ("PZ254V-11-04P", "1x03"),
    ],
)
def test_shape_and_total_mismatch(mfr, fp):
    assert find_header_pin_mismatch(FP.format(fp), None, "", "", mfr) is not None


@pytest.mark.parametrize(
    "desc,fp",
    [
        ("Pin Header 2 Position", "1x08"),
        ("connector header 5 POSITION", "1x02"),
        ("Pin Header 8 Pin", "1x02"),
        ("2-pin header", "2x04"),
    ],
)
def test_prose_count_mismatch(desc, fp):
    assert find_header_pin_mismatch(FP.format(fp), None, "", desc, "") is not None


C7501260 = Part(
    lcsc_part="C7501260",
    mfr_part="ZX-PZ2.54-1-2PZZ",
    package="Through Hole,P=2.54mm",
    description="Pin Header 2 Position 2.54mm Pitch Single Row Through Hole -40℃~+105℃",
)


def _verify(tmp_path, part, fp, pads):
    cache = PartsCache(db_path=tmp_path / "c.db")
    cache.put(part)
    return verify_lcsc_assignment(
        cache, None, part.lcsc_part, "x", "J1", footprint=FP.format(fp), pad_numbers=_pads(pads)
    )


@pytest.mark.parametrize("fp,pads", [("1x08", 8), ("1x40", 40), ("2x01", 2)])
def test_c7501260_on_wrong_header_is_mismatch(tmp_path, fp, pads):
    """The fleet's most-used header part (2 Position) is now pin-checked."""
    v = _verify(tmp_path, C7501260, fp, pads)
    if fp == "2x01":  # same total, shape not stated -> passes on total
        assert v.status == "match"
    else:
        assert v.status == "mismatch"
        assert "pin count 2 vs footprint" in v.mismatch.describe()


def test_c7501260_on_1x02_is_match(tmp_path):
    assert _verify(tmp_path, C7501260, "1x02", 2).status == "match"


def test_unparseable_header_record_is_unverified_not_match(tmp_path):
    part = Part(
        lcsc_part="C999999",
        mfr_part="ZX-HDR-ABC",
        package="Through Hole,P=2.54mm",
        description="Pin Header 2.54mm Pitch Single Row Through Hole",
    )
    assert header_pin_count_unreadable(FP.format("1x08"), part.description, part.mfr_part)
    v = _verify(tmp_path, part, "1x08", 8)
    assert (v.status, v.source, v.mismatch) == ("unverified", "parts cache", None)
    assert v.reason == UNVERIFIED_HEADER_COUNT


def test_unparseable_record_on_non_header_is_not_affected(tmp_path):
    assert not header_pin_count_unreadable("Package_SO:SOIC-8", "Pin Header", "")


def test_socket_and_pad_count():
    socket = "Connector_PinSocket_2.54mm:PinSocket_1x04_P2.54mm_Vertical"
    assert find_header_pin_mismatch(socket, ["1", "2", "3", "4", "MP"], "", "1x4P", "") is None
    assert find_header_pin_mismatch(socket, ["1", "2", "3"], "", "1x4P", "") is not None
    assert find_header_pin_mismatch(socket, None, "", "1x2P", "") is not None


def test_non_header_footprint_is_none():
    assert find_header_pin_mismatch("Package_SO:SOIC-8", None, "", "1x2P", "X-02P") is None


def test_real_lcsc_description_with_noise_tokens():
    desc = (
        "-40℃~+105℃ 1 1x8P 2.54mm 2.5mm 250V 3A 3mm 6.1mm 8P Black Brass Gold Square pin "
        "Through Hole Plugin,P=2.54mm Pin Headers ROHS"
    )
    assert find_header_pin_mismatch(FP.format("1x08"), _pads(8), "", desc, "PZ254V-11-08P") is None
    assert find_header_pin_mismatch(FP.format("1x02"), _pads(2), "", desc, "PZ254V-11-08P")
