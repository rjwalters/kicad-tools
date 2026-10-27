"""Tests for symbol library registration, delete and rename (issue #7995)."""

from __future__ import annotations

import pytest

from kicad_tools.library.lib_table import register_symbol_library
from kicad_tools.schema.library import SymbolLibrary
from kicad_tools.sexp import parse_string


def _names(table_path):
    table = parse_string(table_path.read_text())
    return [str(n.get("name").get_first_atom()) for n in table.find_all("lib")]


class TestRegisterSymbolLibrary:
    def test_creates_table(self, tmp_path):
        table = tmp_path / "sym-lib-table"
        assert register_symbol_library(table, "mine", "${KIPRJMOD}/mine.kicad_sym", "d")
        assert _names(table) == ["mine"]
        assert "${KIPRJMOD}/mine.kicad_sym" in table.read_text()

    def test_appends_preserving_others(self, tmp_path):
        table = tmp_path / "sym-lib-table"
        register_symbol_library(table, "a", "${KIPRJMOD}/a.kicad_sym")
        assert register_symbol_library(table, "b", "${KIPRJMOD}/b.kicad_sym")
        assert _names(table) == ["a", "b"]

    def test_idempotent(self, tmp_path):
        table = tmp_path / "sym-lib-table"
        register_symbol_library(table, "a", "${KIPRJMOD}/a.kicad_sym")
        before = table.read_text()
        assert not register_symbol_library(table, "a", "${KIPRJMOD}/other.kicad_sym")
        assert table.read_text() == before

    def test_malformed_raises_and_untouched(self, tmp_path):
        table = tmp_path / "sym-lib-table"
        table.write_text("(sym_lib_table (lib")
        with pytest.raises(ValueError):
            register_symbol_library(table, "a", "x")
        assert table.read_text() == "(sym_lib_table (lib"

    def test_wrong_root_raises(self, tmp_path):
        table = tmp_path / "sym-lib-table"
        table.write_text("(fp_lib_table (version 7))")
        with pytest.raises(ValueError):
            register_symbol_library(table, "a", "x")


def _lib_with(path, *names):
    lib = SymbolLibrary.create(str(path))
    for n in names:
        sym = lib.create_symbol(n)
        sym.add_property("Value", n)
        sym.add_pin(number="1", name="P", pin_type="passive", position=(0, 0), rotation=0)
    lib.save()
    return lib


class TestDeleteRename:
    def test_delete_round_trip(self, tmp_path):
        path = tmp_path / "l.kicad_sym"
        _lib_with(path, "A", "B")
        lib = SymbolLibrary.load(str(path))
        lib.delete_symbol("A")
        lib.save()
        assert list(SymbolLibrary.load(str(path)).symbols) == ["B"]

    def test_delete_missing(self, tmp_path):
        lib = _lib_with(tmp_path / "l.kicad_sym", "A")
        with pytest.raises(KeyError):
            lib.delete_symbol("Z")

    def test_rename_round_trip(self, tmp_path):
        path = tmp_path / "l.kicad_sym"
        _lib_with(path, "A", "B")
        lib = SymbolLibrary.load(str(path))
        lib.rename_symbol("A", "C")
        lib.save()
        text = path.read_text()
        assert '"C_1_1"' in text and '"A_1_1"' not in text
        loaded = SymbolLibrary.load(str(path))
        assert list(loaded.symbols) == ["C", "B"]
        assert loaded.symbols["C"].properties["Value"] == "C"
        assert len(loaded.symbols["C"].pins) == 1

    def test_rename_to_existing(self, tmp_path):
        lib = _lib_with(tmp_path / "l.kicad_sym", "A", "B")
        with pytest.raises(ValueError):
            lib.rename_symbol("A", "B")

    def test_rename_missing(self, tmp_path):
        lib = _lib_with(tmp_path / "l.kicad_sym", "A")
        with pytest.raises(KeyError):
            lib.rename_symbol("Z", "Y")

    def test_append_after_load_persists(self, tmp_path):
        path = tmp_path / "l.kicad_sym"
        _lib_with(path, "A")
        lib = SymbolLibrary.load(str(path))
        lib.create_symbol("B")
        lib.save()
        assert set(SymbolLibrary.load(str(path)).symbols) == {"A", "B"}


class TestGenerateSymbolRegister:
    def test_cli_registers(self, tmp_path, monkeypatch, capsys):
        import argparse

        from kicad_tools.cli import datasheet_cmd
        from kicad_tools.datasheet import parser as dparser
        from kicad_tools.datasheet.pins import ExtractedPin, PinTable

        pins = PinTable(
            pins=[
                ExtractedPin(number="1", name="VCC", type="power_in"),
                ExtractedPin(number="2", name="GND", type="power_in"),
            ]
        )

        class FakeParser:
            def __init__(self, *_a, **_k):
                pass

            def extract_pins(self, **_k):
                return pins

        monkeypatch.setattr(dparser, "DatasheetParser", FakeParser)
        pdf = tmp_path / "d.pdf"
        pdf.write_bytes(b"%PDF")
        out = tmp_path / "proj" / "gen.kicad_sym"
        args = argparse.Namespace(
            pdf=str(pdf),
            name="PART",
            output=str(out),
            package=None,
            layout="functional",
            pages=None,
            footprint=None,
            manufacturer=None,
            datasheet_url=None,
            description=None,
            append=False,
            register=str(tmp_path / "proj"),
            nickname=None,
        )
        assert datasheet_cmd._generate_symbol(args) == 0
        table = tmp_path / "proj" / "sym-lib-table"
        assert _names(table) == ["gen"]
        assert "${KIPRJMOD}/gen.kicad_sym" in table.read_text()
        assert "PART" in SymbolLibrary.load(str(out)).symbols
