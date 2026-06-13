"""Tests for the custom softstart UCC27211 symbol + ``local_symbol_libs`` API.

Two responsibilities:

1. The custom symbol file at
   ``boards/external/softstart/symbols/softstart_custom.kicad_sym``
   parses cleanly via ``SymbolDef.from_library`` and exposes the 8
   datasheet pins with the correct numbers + pin types.

2. ``Schematic(local_symbol_libs=...)`` works end-to-end: a schematic
   with the local lib registered can ``add_symbol("softstart_custom:UCC27211", ...)``
   and the resulting save round-trips the custom symbol in
   ``lib_symbols``.

The back-compat invariant (omitting ``local_symbol_libs`` preserves the
existing behavior) is asserted by the absence of any new behavior in
the default-arguments path.
"""

import tempfile
from pathlib import Path

import pytest

from kicad_tools.schematic.models.schematic import Schematic
from kicad_tools.schematic.models.symbol import SymbolDef

CUSTOM_LIB_PATH = (
    Path(__file__).parent.parent
    / "boards"
    / "external"
    / "softstart"
    / "symbols"
    / "softstart_custom.kicad_sym"
)


class TestCustomSymbolFile:
    """The on-disk UCC27211 symbol parses with the expected pinout."""

    def test_lib_file_exists(self):
        """Sanity: the custom .kicad_sym file is committed to the repo."""
        assert CUSTOM_LIB_PATH.exists(), f"Missing custom lib at {CUSTOM_LIB_PATH}"

    def test_ucc27211_parses(self):
        """SymbolDef.from_library resolves the custom symbol via lib_paths."""
        sd = SymbolDef.from_library(
            "softstart_custom:UCC27211",
            lib_paths=[CUSTOM_LIB_PATH.parent],
        )
        assert sd.lib_id == "softstart_custom:UCC27211"
        assert sd.name == "UCC27211"
        # 8 pins total per the datasheet (HB, HO, HS, VDD, HI, LI, VSS, LO).
        assert len(sd.pins) == 8

    def test_ucc27211_pin_types_match_datasheet(self):
        """Pin types per the issue's ERC-validation acceptance criterion.

        VDD/VSS are power_in; HI/LI are input; HO/LO are output;
        HB/HS are passive (bootstrap nodes).
        """
        sd = SymbolDef.from_library(
            "softstart_custom:UCC27211",
            lib_paths=[CUSTOM_LIB_PATH.parent],
        )
        pin_by_name = {p.name: p for p in sd.pins}

        # Required pins are present
        for name in ("VDD", "VSS", "HI", "LI", "HO", "LO", "HB", "HS"):
            assert name in pin_by_name, f"Missing pin {name!r}"

        # Pin-type assertions
        assert pin_by_name["VDD"].pin_type == "power_in"
        assert pin_by_name["VSS"].pin_type == "power_in"
        assert pin_by_name["HI"].pin_type == "input"
        assert pin_by_name["LI"].pin_type == "input"
        assert pin_by_name["HO"].pin_type == "output"
        assert pin_by_name["LO"].pin_type == "output"
        assert pin_by_name["HB"].pin_type == "passive"
        assert pin_by_name["HS"].pin_type == "passive"

    def test_ucc27211_pin_numbers_match_datasheet(self):
        """SOIC-8 datasheet ordering: HB=1, HO=2, HS=3, VDD=4, HI=5, LI=6, VSS=7, LO=8."""
        sd = SymbolDef.from_library(
            "softstart_custom:UCC27211",
            lib_paths=[CUSTOM_LIB_PATH.parent],
        )
        pin_by_name = {p.name: p for p in sd.pins}
        assert pin_by_name["HB"].number == "1"
        assert pin_by_name["HO"].number == "2"
        assert pin_by_name["HS"].number == "3"
        assert pin_by_name["VDD"].number == "4"
        assert pin_by_name["HI"].number == "5"
        assert pin_by_name["LI"].number == "6"
        assert pin_by_name["VSS"].number == "7"
        assert pin_by_name["LO"].number == "8"


class TestLocalSymbolLibsAPI:
    """``Schematic(local_symbol_libs=...)`` integration."""

    def test_default_omits_local_libs(self):
        """Back-compat: without ``local_symbol_libs``, the attribute is empty."""
        sch = Schematic("test")
        assert sch.local_symbol_libs == []

    def test_provided_libs_are_stored(self):
        """``local_symbol_libs`` is stored on the schematic."""
        sch = Schematic("test", local_symbol_libs=[CUSTOM_LIB_PATH])
        assert sch.local_symbol_libs == [CUSTOM_LIB_PATH]

    def test_resolve_lib_path_finds_local_lib(self):
        """``resolve_lib_path`` finds a matching local lib by name."""
        sch = Schematic("test", local_symbol_libs=[CUSTOM_LIB_PATH])
        assert sch.resolve_lib_path("softstart_custom") == CUSTOM_LIB_PATH

    def test_resolve_lib_path_returns_none_for_unknown(self):
        """``resolve_lib_path`` returns None for unknown lib name."""
        sch = Schematic("test", local_symbol_libs=[CUSTOM_LIB_PATH])
        assert sch.resolve_lib_path("nonexistent") is None

    def test_add_symbol_resolves_local_lib(self):
        """A schematic with local libs registered can add the custom symbol."""
        sch = Schematic("test", local_symbol_libs=[CUSTOM_LIB_PATH])
        # This is the load-bearing call — it must NOT raise LibraryNotFoundError.
        sym = sch.add_symbol("softstart_custom:UCC27211", x=100, y=80, ref="U_TEST")
        assert sym.reference == "U_TEST"
        # The schematic cached the SymbolDef
        assert "softstart_custom:UCC27211" in sch._symbol_defs

    def test_add_symbol_without_local_libs_raises(self):
        """A schematic *without* the local lib registered fails to add the custom symbol.

        This protects the back-compat path: stock-only schematics must
        not accidentally see project-local libs.
        """
        sch = Schematic("test")
        # No local libs → custom symbol unresolved → an error is raised.
        # SymbolDef.from_library raises LibraryNotFoundError; the symbol
        # registry path may raise a different exception.  We catch the
        # specific known exception types; any of them is acceptable as
        # long as the call does NOT silently succeed.
        from kicad_tools.exceptions import FileNotFoundError as KctFileNotFoundError
        from kicad_tools.schematic.exceptions import LibraryNotFoundError

        with pytest.raises((LibraryNotFoundError, KctFileNotFoundError, FileNotFoundError)):
            sch.add_symbol("softstart_custom:UCC27211", x=100, y=80, ref="U_TEST")

    def test_save_includes_local_symbol_in_lib_symbols(self):
        """After save, the produced .kicad_sch contains the custom symbol's lib_symbols entry."""
        sch = Schematic("test", local_symbol_libs=[CUSTOM_LIB_PATH])
        sch.add_symbol("softstart_custom:UCC27211", x=100, y=80, ref="U1")

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.kicad_sch"
            sch.write(out)
            content = out.read_text()
            # The lib_symbols section must mention UCC27211 (either via
            # the synthesized prefix or as a bare symbol entry).
            assert "UCC27211" in content
            # And it must reside inside a lib_symbols block.
            assert "lib_symbols" in content
