"""``--quantity`` / ``--inventory-covered`` on the stock gate (issue #8807)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kicad_tools.export.preflight import (
    PreflightChecker,
    PreflightConfig,
    evaluate_lcsc_stock,
    parse_inventory_covered,
)
from kicad_tools.parts.cache import PartsCache
from kicad_tools.parts.models import Part
from kicad_tools.schema.bom import BOM, BOMItem

FP = "Resistor_SMD:R_0805_2012Metric"


def _part(lcsc, stock, source="live"):
    return Part(
        lcsc_part=lcsc,
        mfr_part="CRCW0805137KFKEAHP",
        description="137k 0805",
        package="0805",
        value="137kΩ",
        stock=stock,
        stock_source=source,
        fetched_at=datetime.now(tz=timezone.utc) - timedelta(hours=0),
    )


def _run(tmp_path, parts, refs, **cfg):
    cache = PartsCache(db_path=tmp_path / "parts.db")
    for p in parts:
        cache.put(p)
    checker = PreflightChecker(
        pcb_path=tmp_path / "board.kicad_pcb",
        config=PreflightConfig(skip_drc=True, skip_erc=True, **cfg),
        parts_cache=cache,
    )
    checker._bom = BOM(
        items=[
            BOMItem(reference=r, value="137k", footprint=FP, lib_id="Device:R", lcsc=lc)
            for r, lc in refs
        ]
    )
    return checker.check_bom_lcsc_stock()


def test_parse_inventory_covered_normalises():
    assert parse_inventory_covered(" c123 , C456,,") == frozenset({"C123", "C456"})
    assert parse_inventory_covered("") == frozenset()
    assert parse_inventory_covered(None) == frozenset()


def test_short_at_five_passes_at_two(tmp_path):
    parts, refs = [_part("C1", 2)], [("Y2", "C1")]
    assert _run(tmp_path, parts, refs, order_boards=5).status == "FAIL"
    assert _run(tmp_path, parts, refs, order_boards=2).status == "OK"


def test_covered_short_line_passes_and_is_listed(tmp_path):
    r = _run(tmp_path, [_part("C1", 2)], [("Y2", "C1")], inventory_covered=frozenset({"C1"}))
    assert r.status == "OK"
    assert "held in operator inventory, not stock-verified: C1" in r.details
    assert r.data["inventory_covered"] == ["C1"]


def test_covered_but_not_short_and_unused_are_reported(tmp_path):
    r = _run(
        tmp_path,
        [_part("C1", 500)],
        [("R1", "C1")],
        inventory_covered=frozenset({"C1", "C999"}),
    )
    assert r.status == "WARN"
    assert "not short" in r.details and "C1" in r.details
    assert "not in the BOM: C999" in r.details


def test_only_covered_part_does_not_hide_other_shortfall(tmp_path):
    r = _run(
        tmp_path,
        [_part("C1", 2), _part("C2", 1)],
        [("Y2", "C1"), ("R1", "C2")],
        inventory_covered=frozenset({"C1"}),
    )
    assert r.status == "FAIL"
    assert "C2" in r.details


def test_evaluator_parity_from_recorded_data(tmp_path):
    parts, refs = [_part("C1", 2)], [("Y2", "C1")]
    first = _run(tmp_path, parts, refs)
    again = evaluate_lcsc_stock(first.data, 5, frozenset(), False)
    assert (again.status, again.message, again.details) == (
        first.status,
        first.message,
        first.details,
    )
    assert evaluate_lcsc_stock(first.data, 2, frozenset(), False).status == "OK"
    assert evaluate_lcsc_stock(first.data, 5, frozenset({"c1"}), False).status == "OK"


def test_unverified_shortfall_is_warn_with_source_named(tmp_path):
    r = _run(tmp_path, [_part("C1", 2, source="cache")], [("Y2", "C1")])
    assert r.status == "WARN"
    assert "unverified cache" in r.details


# ---- readiness gate -------------------------------------------------------

from kicad_tools.cli import readiness_cmd  # noqa: E402
from tests.test_kct_readiness_cmd import FakeEngines, make_board  # noqa: E402


def _stock_entry(stock=2):
    data = {
        "order_boards": 5,
        "inventory_covered": [],
        "strict": False,
        "parts": [
            {
                "lcsc": "C1",
                "refs": ["Y2"],
                "per_board": 1,
                "stock": stock,
                "verified": True,
                "source": "live",
                "age_days": 0,
            }
        ],
        "unresolved": [],
    }
    res = evaluate_lcsc_stock(data, 5, frozenset(), False)
    return res.to_dict()


def _gate(board, entry, *flags):
    fake = FakeEngines(board, stock_check=entry)
    code = readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--generate", *flags], engines=fake.bundle()
    )
    out = board / "output"
    path = next(
        c
        for c in (out / "readiness-attempt" / "readiness.json", out / "readiness.json")
        if c.is_file()
    )
    return code, json.loads(path.read_text())


def test_gate_fails_by_default_passes_with_quantity_or_covered(tmp_path):
    board = make_board(tmp_path)
    entry = _stock_entry()
    code, report = _gate(board, entry)
    assert code != 0
    assert any("Y2 -> C1: needs 5, stock 2 (live)" in b for b in report["blockers"])


@pytest.mark.parametrize("flags", [("--quantity", "2"), ("--inventory-covered", "c1,")])
def test_gate_honours_flags(tmp_path, flags):
    board = make_board(tmp_path)
    code, report = _gate(board, _stock_entry(), *flags)
    bom = next(c for c in report["checks"] if c["name"] == "bom")
    assert bom["status"] != "failed", bom
    assert report["metrics"]["lcsc_stock_order"]["order_boards"] in (2, 5)
    readme = next((board / "output").rglob("README.txt")).read_text()
    assert "LCSC stock order" in readme
    if "--inventory-covered" in flags:
        assert "held in operator inventory, not stock-verified: C1" in readme


def test_legacy_manifest_without_data_refuses(tmp_path):
    board = make_board(tmp_path)
    entry = {"name": "bom_lcsc_stock", "status": "OK", "message": "ok"}
    code, report = _gate(board, entry, "--quantity", "2")
    bom = next(c for c in report["checks"] if c["name"] == "bom")
    assert bom["status"] == "failed"
    assert any("regenerate" in b for b in report["blockers"])
    # No flags: legacy verdict stands.
    board2 = make_board(tmp_path / "b")
    _, report2 = _gate(board2, entry)
    assert next(c for c in report2["checks"] if c["name"] == "bom")["status"] != "failed"


def test_quantity_zero_rejected(tmp_path, capsys):
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    code = readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--generate", "--quantity", "0"], engines=fake.bundle()
    )
    assert code != 0


def test_verify_reevaluates_recorded_data_and_keeps_recorded_values(tmp_path):
    board = make_board(tmp_path)
    entry = _stock_entry()
    entry["data"]["order_boards"] = 2  # what a real --quantity 2 export records
    code, _ = _gate(board, entry, "--quantity", "2")
    assert code == 0
    out = board / "output"
    # No flags: verify keeps the recorded order size, so it stays passing.
    fake = FakeEngines(board)
    assert readiness_cmd.main([str(board), "--mfr", "jlcpcb"], engines=fake.bundle()) == 0
    # Explicit larger quantity re-evaluates the recorded stock and blocks.
    fake = FakeEngines(board)
    code = readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--quantity", "5"], engines=fake.bundle()
    )
    assert code != 0
    assert out.exists()
