"""Tests for the ``bom_lcsc_stock`` preflight check (issue #8121)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kicad_tools.cli.export_cmd import build_parser
from kicad_tools.export.lcsc_value_check import stock_shortfall
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from kicad_tools.parts.cache import PartsCache
from kicad_tools.parts.models import Part
from kicad_tools.schema.bom import BOM, BOMItem

FP_0805 = "Resistor_SMD:R_0805_2012Metric"


def _part(lcsc: str, stock: int, source: str = "unknown", age_h: float = 0.0) -> Part:
    return Part(
        lcsc_part=lcsc,
        mfr_part="CRCW0805137KFKEAHP",
        description="137k 1% 0805 resistor",
        package="0805",
        value="137kΩ",
        stock=stock,
        stock_source=source,
        fetched_at=datetime.now(tz=timezone.utc) - timedelta(hours=age_h),
    )


def _item(ref: str, lcsc: str, dnp: bool = False) -> BOMItem:
    return BOMItem(
        reference=ref, value="137k", footprint=FP_0805, lib_id="Device:R", lcsc=lcsc, dnp=dnp
    )


def _run(tmp_path: Path, parts: list[Part], items: list[BOMItem], **cfg):
    cache = PartsCache(db_path=tmp_path / "parts.db")
    for p in parts:
        cache.put(p)
    checker = PreflightChecker(
        pcb_path=tmp_path / "board.kicad_pcb",
        config=PreflightConfig(skip_drc=True, skip_erc=True, **cfg),
        parts_cache=cache,
    )
    checker._bom = BOM(items=items)
    return checker.check_bom_lcsc_stock()


def test_c4223175_zero_stock_unknown_source_warns(tmp_path):
    r = _run(tmp_path, [_part("C4223175", 0)], [_item("R1", "C4223175")])
    assert r.name == "bom_lcsc_stock"
    assert r.status == "WARN"
    assert "C4223175" in r.details


def test_c4223175_zero_stock_strict_fails(tmp_path):
    r = _run(tmp_path, [_part("C4223175", 0)], [_item("R1", "C4223175")], strict_report_source=True)
    assert r.status == "FAIL"


def test_live_zero_stock_fails(tmp_path):
    r = _run(tmp_path, [_part("C4223175", 0, "live")], [_item("R1", "C4223175")])
    assert r.status == "FAIL"
    assert "needs 5, stock 0" in r.details


def test_live_short_fails_exact_equal_ok(tmp_path):
    items = [_item("R1", "C1")]
    assert _run(tmp_path, [_part("C1", 4, "live")], items).status == "FAIL"
    assert _run(tmp_path, [_part("C1", 5, "live")], items).status == "OK"


def test_stale_live_observation_is_warn(tmp_path):
    r = _run(tmp_path, [_part("C1", 0, "live", age_h=48)], [_item("R1", "C1")])
    assert r.status == "WARN"


def test_order_boards_one(tmp_path):
    items = [_item("R1", "C1")]
    assert _run(tmp_path, [_part("C1", 1, "live")], items, order_boards=1).status == "OK"


def test_multi_reference_quantity_summed(tmp_path):
    items = [_item("R1", "C1"), _item("R2", "C1"), _item("R3", "C1")]
    # 3 placements x 5 boards = 15
    assert _run(tmp_path, [_part("C1", 14, "live")], items).status == "FAIL"
    assert _run(tmp_path, [_part("C1", 15, "live")], items).status == "OK"


def test_dnp_and_unassigned_skipped(tmp_path):
    items = [_item("R1", "C1", dnp=True), _item("R2", "")]
    r = _run(tmp_path, [_part("C1", 0, "live")], items)
    assert r.status == "OK"


def test_part_missing_from_sources_not_double_reported(tmp_path):
    r = _run(tmp_path, [], [_item("R1", "C999")])
    assert r.status == "OK"


def test_stock_shortfall_pure():
    assert stock_shortfall(_part("C1", 10), 2, 5) is None
    s = stock_shortfall(_part("C1", 9), 2, 5)
    assert s is not None and (s.needed, s.available) == (10, 9)


def test_boards_flag():
    p = build_parser()
    assert p.parse_args(["x.kicad_pcb"]).boards == 5
    assert p.parse_args(["x.kicad_pcb", "--boards", "20"]).boards == 20
    with pytest.raises(SystemExit):
        p.parse_args(["x.kicad_pcb", "--boards", "0"])
