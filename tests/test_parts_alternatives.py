"""Tests for ``kct parts alternatives`` (offline Basic-part substitution)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from kicad_tools.cli import parts_cmd
from kicad_tools.parts.alternatives import rank_alternatives
from kicad_tools.parts.models import Part, PartPrice

ROWS = [
    # lcsc, mfr, package, library_type, description, stock, price
    (100, "EXT-100N", "0603", "extended", "100nF 50V X7R 0603", 500, "1-:0.0100"),
    (101, "BASIC-100N", "0603", "basic", "100nF 25V X7R 0603", 9000, "1-:0.0050"),
    (102, "PREF-100N", "0603", "preferred", "100nF 50V X7R 0603", 800, "1-:0.0040"),
    (103, "EXT2-100N", "0603", "extended", "100nF 16V X7R 0603", 100, "1-:0.0030"),
    (104, "OOS-BASIC-100N", "0603", "basic", "100nF 10V X7R 0603", 0, "1-:0.0010"),
    (105, "OTHERPKG-100N", "0402", "basic", "100nF 10V X7R 0402", 900, "1-:0.0010"),
    (200, "BASIC-10K", "0603", "basic", "10kΩ 1% 0603", 100, "1-:0.0020"),
    (201, "BASIC-10K-CHEAP", "0603", "basic", "10kΩ 5% 0603", 100, "1-:0.0010"),
]


@pytest.fixture
def catalog_db(tmp_path, monkeypatch):
    path = tmp_path / "jlcparts.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE jlc_components (lcsc INTEGER PRIMARY KEY, mfr TEXT, package TEXT,"
            " manufacturer TEXT, library_type TEXT, description TEXT, datasheet TEXT,"
            " stock INTEGER, price TEXT)"
        )
        conn.executemany(
            "INSERT INTO jlc_components VALUES (?,?,?,'M',?,?,'',?,?)",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in ROWS],
        )
    monkeypatch.setattr("kicad_tools.parts.jlcparts_catalog.get_catalog_path", lambda: path)
    return path


def _part(lcsc, tier, price, stock=10):
    return Part(
        lcsc_part=lcsc,
        is_basic=tier == "basic",
        is_preferred=tier == "preferred",
        stock=stock,
        prices=[PartPrice(quantity=1, unit_price=price)] if price is not None else [],
    )


def test_rank_order_stock_and_self_exclusion():
    src = _part("C1", "extended", 0.01)
    cands = [
        src,
        _part("C2", "extended", 0.001),
        _part("C3", "preferred", 0.004),
        _part("C4", "basic", 0.005),
        _part("C5", "basic", 0.002, stock=0),
    ]
    assert [p.lcsc_part for p in rank_alternatives(src, cands)] == ["C4", "C3", "C2"]


def test_rank_basic_source_only_cheaper_same_tier():
    src = _part("C1", "basic", 0.005)
    cands = [_part("C2", "basic", 0.006), _part("C3", "basic", 0.001), _part("C4", "preferred", 0)]
    assert [p.lcsc_part for p in rank_alternatives(src, cands)] == ["C3"]


def test_rank_limit_and_empty():
    src = _part("C1", "extended", 1.0)
    assert rank_alternatives(src, [], 5) == []
    cands = [_part(f"C{i}", "basic", 0.1) for i in range(2, 9)]
    assert len(rank_alternatives(src, cands, 3)) == 3


def test_cli_json(catalog_db, capsys):
    assert parts_cmd.main(["alternatives", "C100", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"]["lcsc"] == "C100"
    ids = [a["lcsc"] for a in data["alternatives"]]
    assert ids == ["C101", "C102", "C103"]
    assert set(data["alternatives"][0]) == {
        "lcsc",
        "mfr_part",
        "package",
        "library_type",
        "stock",
        "unit_price",
    }
    assert data["alternatives"][0]["library_type"] == "basic"


def test_cli_text_and_limit(catalog_db, capsys):
    assert parts_cmd.main(["alternatives", "C100", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "C101" in out and "C102" not in out


def test_cli_basic_source_no_better(catalog_db, capsys):
    # C200 is Basic; C201 is cheaper Basic with matching value token "10kΩ".
    assert parts_cmd.main(["alternatives", "C200", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [a["lcsc"] for a in data["alternatives"]] == ["C201"]
    assert parts_cmd.main(["alternatives", "C201"]) == 0
    assert "No better" in capsys.readouterr().out


def test_cli_unknown_part(catalog_db, capsys):
    assert parts_cmd.main(["alternatives", "C999999"]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_missing_catalog(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "kicad_tools.parts.jlcparts_catalog.get_catalog_path", lambda: tmp_path / "none.sqlite3"
    )
    assert parts_cmd.main(["alternatives", "C100"]) == 1
    err = capsys.readouterr().err
    assert "sync-catalog" in err
    assert "Traceback" not in err
