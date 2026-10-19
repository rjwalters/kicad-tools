"""Pin-header row x pin count guard (issue #7446)."""

from __future__ import annotations

import pytest

from kicad_tools.export.lcsc_value_check import (
    UNVERIFIED_HEADER_CONFLICT,
    UNVERIFIED_HEADER_COUNT,
    _part_header_count,
    find_header_pin_mismatch,
    header_pin_count_unreadable,
    verify_lcsc_assignment,
)
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from kicad_tools.parts.cache import PartsCache
from kicad_tools.parts.models import Part
from kicad_tools.schema.bom import BOM, BOMItem

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


# ---------------------------------------------------------------------------
# Conflicting count forms give no count (Judge review of PR #7448).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "desc,mfr",
    [
        ("1x3 header 2 Pin", ""),  # shape vs prose in one description
        ("Pin Header 2 Position 3 Pin", ""),  # prose vs prose
        ("Pin Header 1x8P 2x4P", ""),  # same total, different shape
        ("Pin Header 8 Pin 2.54mm", "PZ254V-11-02P"),  # MPN suffix vs description
        ("", "PZ254-1x3-02P"),  # shape vs suffix inside the MPN
    ],
)
def test_conflicting_count_forms_give_no_count(desc, mfr):
    assert _part_header_count(desc, mfr) is None
    for fp in ("1x02", "1x03", "1x08", "2x04"):
        assert find_header_pin_mismatch(FP.format(fp), None, "", desc, mfr) is None
        assert header_pin_count_unreadable(FP.format(fp), desc, mfr)


@pytest.mark.parametrize(
    "desc,mfr,expected",
    [
        ("1x3 header 3 Pin", "", (3, (1, 3))),
        ("Pin Header 1x8P 8 Pin", "PZ254V-11-08P", (8, (1, 8))),
        ("Pin Header 8 Pin", "PZ254V-11-08P", (8, None)),
        ("Header 8x1", "", (8, (1, 8))),
    ],
)
def test_agreeing_count_forms_give_the_count(desc, mfr, expected):
    assert _part_header_count(desc, mfr) == expected


def test_shape_vs_prose_conflict_is_unverified_with_conflict_reason(tmp_path):
    part = Part(
        lcsc_part="C999998",
        mfr_part="X-HDR",
        package="Through Hole,P=2.54mm",
        description="1x3 header 2 Pin",
    )
    v = _verify(tmp_path, part, "1x03", 3)  # formerly a silent ``match``
    assert (v.status, v.mismatch) == ("unverified", None)
    assert v.reason == UNVERIFIED_HEADER_CONFLICT


def test_mpn_vs_description_conflict_is_unverified(tmp_path):
    part = Part(
        lcsc_part="C999997",
        mfr_part="PZ254V-11-02P",
        package="Plugin,P=2.54mm",
        description="Pin Header 8 Pin 2.54mm",
    )
    for fp, pads in (("1x02", 2), ("1x08", 8)):
        (tmp_path / fp).mkdir()
        v = _verify(tmp_path / fp, part, fp, pads)
        assert v.status == "unverified"
        assert v.reason == UNVERIFIED_HEADER_CONFLICT


# ---------------------------------------------------------------------------
# Strict policy: unverified header count FAILs under --strict-preflight.
# ---------------------------------------------------------------------------

UNREADABLE = Part(
    lcsc_part="C999999",
    mfr_part="ZX-HDR-ABC",
    package="Through Hole,P=2.54mm",
    description="Pin Header 2.54mm Pitch Single Row Through Hole",
)


def _preflight(tmp_path, parts, items, strict):
    cache = PartsCache(db_path=tmp_path / "parts.db")
    for part in parts:
        cache.put(part)
    checker = PreflightChecker(
        pcb_path=tmp_path / "board.kicad_pcb",
        config=PreflightConfig(skip_drc=True, skip_erc=True, strict_report_source=strict),
        parts_cache=cache,
        parts_catalog=None,
    )
    checker._bom = BOM(items=items)
    return checker._check_bom_lcsc_values()


def _hdr_item(ref, lcsc, fp="1x08"):
    return BOMItem(
        reference=ref, value="Conn", footprint=FP.format(fp), lib_id="Connector:Conn", lcsc=lcsc
    )


def test_unverified_header_is_warn_without_strict(tmp_path):
    result = _preflight(tmp_path, [UNREADABLE], [_hdr_item("J1", "C999999")], strict=False)
    assert result.status == "WARN"
    assert "pin count not verifiable" in result.message
    assert "J1 -> C999999 (pin count not stated by part)" in result.details
    assert not PreflightChecker.has_failures([result])


def test_unverified_header_fails_under_strict(tmp_path):
    result = _preflight(tmp_path, [UNREADABLE], [_hdr_item("J1", "C999999")], strict=True)
    assert result.status == "FAIL"
    assert "pin count not verifiable" in result.message
    assert "does not match" not in result.message
    assert "J1 -> C999999 (pin count not stated by part)" in result.details
    assert PreflightChecker.has_failures([result])


def test_conflicting_header_fails_under_strict(tmp_path):
    part = Part(
        lcsc_part="C999998",
        mfr_part="X-HDR",
        package="Through Hole,P=2.54mm",
        description="1x3 header 2 Pin",
    )
    result = _preflight(tmp_path, [part], [_hdr_item("J1", "C999998", "1x03")], strict=True)
    assert result.status == "FAIL"
    assert f"J1 -> C999998 ({UNVERIFIED_HEADER_CONFLICT})" in result.details


@pytest.mark.parametrize("strict", [False, True])
def test_cache_miss_header_stays_warn_even_under_strict(tmp_path, strict):
    """CI runners start with an empty cache: a miss never blocks."""
    result = _preflight(tmp_path, [], [_hdr_item("J1", "C7501260", "1x02")], strict=strict)
    assert result.status == "WARN"
    assert "not in parts cache or offline catalog" in result.message
    assert not PreflightChecker.has_failures([result])


def test_strict_fail_also_counts_cache_misses(tmp_path):
    items = [_hdr_item("J1", "C999999"), _hdr_item("J2", "C1234567", "1x02")]
    result = _preflight(tmp_path, [UNREADABLE], items, strict=True)
    assert result.status == "FAIL"
    assert "1 pin header/socket part(s): pin count not verifiable" in result.message
    assert "1 more unverifiable (not in parts cache or offline catalog)" in result.message
    assert "J2" not in result.details


@pytest.mark.parametrize("strict", [False, True])
def test_matching_header_is_ok_either_way(tmp_path, strict):
    result = _preflight(tmp_path, [C7501260], [_hdr_item("J1", "C7501260", "1x02")], strict=strict)
    assert result.status == "OK", result.details
