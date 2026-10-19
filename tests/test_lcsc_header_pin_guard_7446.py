"""Pin-header row x pin count guard (issue #7446)."""

from __future__ import annotations

import pytest

from kicad_tools.export.lcsc_value_check import find_header_pin_mismatch, verify_lcsc_assignment
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
        ("ZX-PZ2.54-1-2PZZ", "Pin Header 2 Position", "1x08", 8),  # unparseable
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
