"""The catalog-coverage measurement script (issue #7686, part 1). Fixture-only."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "research"
    / "measure_3d_catalog_coverage.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("measure_3d_catalog_coverage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "jlcparts.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE jlc_components (lcsc INTEGER, package TEXT, library_type TEXT,"
            " category TEXT, stock INTEGER)"
        )
        conn.executemany(
            "INSERT INTO jlc_components VALUES (?,?,?,?,?)",
            [
                (1, "0603", "Basic", "Resistors", 10),
                (2, "0603", "Extended", "Resistors", 0),
                (3, "SOT-23-6", "Extended", "ICs", 5),
                (4, "WEIRD-PKG", "Extended", "ICs", 5),
            ],
        )
    return path


def test_classify_buckets_by_type_and_category(tmp_path):
    mod = _load()
    footprints = {"0603": "R:0603", "SOT-23-6": "S:SOT-23-6"}
    with sqlite3.connect(_db(tmp_path)) as conn:
        conn.row_factory = sqlite3.Row
        result = mod.classify(
            conn.execute("SELECT * FROM jlc_components"),
            footprints.get,
            lambda lib_id: lib_id == "R:0603",
        )
    assert result["total"] == {"stock_body": 2, "needs_lcsc": 1, "no_footprint": 1}
    assert result["buckets"]["type:basic"] == {"stock_body": 1}
    assert result["buckets"]["category:ICs"] == {"needs_lcsc": 1, "no_footprint": 1}
    assert "not measured" in mod.render_text(result)


def test_missing_catalog_is_reported_not_measured(tmp_path, capsys):
    rc = _load().main(["--db", str(tmp_path / "absent.sqlite3")])
    assert rc == 2
    assert "not measured" in capsys.readouterr().err


def test_json_output_roundtrips(tmp_path, capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "build_matcher", lambda: {"0603": "R:0603"}.get)
    monkeypatch.setattr(mod, "build_body_check", lambda: lambda lib_id: True)
    assert mod.main(["--db", str(_db(tmp_path)), "--json", "--in-stock-only"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total"]["stock_body"] == 1 and out["total"]["no_footprint"] == 2
