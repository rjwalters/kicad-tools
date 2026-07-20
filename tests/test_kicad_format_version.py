"""Guard tests for centralized KiCad file-format version stamps (#4378).

Every writer that emits a ``(version ...)`` / ``(generator_version ...)`` node
must route the value through the shared constants in
:mod:`kicad_tools.core.version` so the stamps cannot drift back to the stale,
mismatched literals (``20231014`` / ``20231120`` / ``generator_version "0.2.0"``
/ ``"9.0"`` / ``"1.0"``) they used before centralization.

There is no single "KiCad-10 version number": PCB, schematic and symbol-library
files are independent format streams, so each stream has its own constant. All
three date codes are the conservative floor that loads across the whole 10.0.x
line -- KiCad 10.0.4 authors newer codes, but earlier 10.0.x releases reject a
*future* format, so the constants must NOT be bumped to the newest code.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.core.version import (
    KICAD_BOARD_FORMAT_VERSION,
    KICAD_GENERATOR_VERSION,
    KICAD_SCH_FORMAT_VERSION,
    KICAD_SYM_FORMAT_VERSION,
)
from kicad_tools.sexp import parse_string


class TestFormatVersionConstants:
    """Pin the constant values and the cross-10.0.x safety invariant."""

    def test_constant_values(self):
        assert KICAD_BOARD_FORMAT_VERSION == 20241229
        assert KICAD_SCH_FORMAT_VERSION == 20231120
        assert KICAD_SYM_FORMAT_VERSION == 20231120
        assert KICAD_GENERATOR_VERSION == "10.0"

    def test_board_constant_not_bumped_past_10_0_x_floor(self):
        """20260206 is 10.0.4-only (rejected by 10.0.3) -- must not regress."""
        assert KICAD_BOARD_FORMAT_VERSION < 20260206


class TestPcbWriters:
    """`.kicad_pcb` writers stamp the board constant + shared generator."""

    def test_pcb_create_stamps_constants(self):
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.create(width=40, height=30)
        version = pcb._sexp.find("version")
        assert version is not None
        assert version.get_int(0) == KICAD_BOARD_FORMAT_VERSION

        gen = pcb._sexp.find("generator_version")
        assert gen is not None
        assert gen.get_string(0) == KICAD_GENERATOR_VERSION

    def test_pcb_exporter_header_stamps_constants(self):
        from kicad_tools.pcb.exporter import KiCadPCBExporter
        from kicad_tools.pcb.layout import PCBLayout

        exporter = KiCadPCBExporter(PCBLayout(name="t"))
        header = exporter._generate_header()
        assert f"(version {KICAD_BOARD_FORMAT_VERSION})" in header
        assert f'(generator_version "{KICAD_GENERATOR_VERSION}")' in header

    def test_project_scaffold_pcb_stamps_board_constant(self, tmp_path: Path):
        from kicad_tools.project import Project

        Project.create("scaffold", directory=str(tmp_path))
        text = (tmp_path / "scaffold.kicad_pcb").read_text()
        assert f"(version {KICAD_BOARD_FORMAT_VERSION})" in text
        assert f'(generator_version "{KICAD_GENERATOR_VERSION}")' in text
        # No stale literals left behind.
        assert "20231014" not in text
        assert '"0.2.0"' not in text


class TestSchematicWriters:
    """`.kicad_sch` writers stamp the schematic constant + shared generator."""

    def test_models_schematic_stamps_constants(self):
        from kicad_tools.schematic.models.schematic import Schematic

        sch = Schematic(title="T")
        root = sch.to_sexp_node()
        version = root.find("version")
        assert version is not None
        assert version.get_int(0) == KICAD_SCH_FORMAT_VERSION

        gen = root.find("generator_version")
        assert gen is not None
        assert gen.get_string(0) == KICAD_GENERATOR_VERSION

    def test_project_scaffold_sch_stamps_sch_constant(self, tmp_path: Path):
        from kicad_tools.project import Project

        Project.create("scaffold", directory=str(tmp_path))
        text = (tmp_path / "scaffold.kicad_sch").read_text()
        assert f"(version {KICAD_SCH_FORMAT_VERSION})" in text
        assert f'(generator_version "{KICAD_GENERATOR_VERSION}")' in text
        assert '"0.2.0"' not in text


class TestSymbolLibraryWriters:
    """`.kicad_sym` writers stamp the symbol constant + shared generator."""

    def test_symbol_library_stamps_constants(self):
        from kicad_tools.schema.library import SymbolLibrary

        lib = SymbolLibrary(path="mem.kicad_sym")
        lib.create_symbol("MYSYM")
        root = lib.to_sexp_node()
        version = root.find("version")
        assert version is not None
        assert version.get_int(0) == KICAD_SYM_FORMAT_VERSION

        gen = root.find("generator_version")
        assert gen is not None
        assert gen.get_string(0) == KICAD_GENERATOR_VERSION

    def test_symbol_generator_sexp_stamps_constants(self):
        from kicad_tools.schematic.symbol_generator import (
            PinDef,
            PinType,
            SymbolDef,
            generate_symbol_sexp,
        )

        sym = SymbolDef(
            name="TESTSYM",
            pins=[PinDef(number="1", name="A", pin_type=PinType.PASSIVE)],
        )
        text = generate_symbol_sexp(sym)
        assert f"(version {KICAD_SYM_FORMAT_VERSION})" in text
        assert f'(generator_version "{KICAD_GENERATOR_VERSION}")' in text

    def test_synth_pwr_symbol_lib_stamps_sym_constant(self, tmp_path: Path):
        from kicad_tools.schematic.models.schematic import Schematic
        from kicad_tools.sexp import SExp

        sch = Schematic(title="T")
        prefix = sch._PWR_SYNTH_LIB_PREFIX
        sch._synthesized_pwr_defs = {
            "GND": SExp.list("symbol", f"{prefix}:GND"),
        }
        out = tmp_path / "kicad_tools_pwr.kicad_sym"
        sch._write_synth_pwr_symbol_lib(out)
        text = out.read_text()
        assert f"(version {KICAD_SYM_FORMAT_VERSION})" in text
        assert "20231120" in text  # equals the sym constant value


class TestGeneratorVersionQuotedAtomRoundTrip:
    """`generator_version` must survive as a quoted atom, not a bare number."""

    def test_pcb_generator_version_round_trips_quoted(self):
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.create(width=40, height=30)
        serialized = pcb._sexp.to_string()
        # Emitted with quotes so kicad-cli does not downgrade "10.0" -> 10.0.
        assert f'(generator_version "{KICAD_GENERATOR_VERSION}")' in serialized

        # Re-parse: the string value survives intact.
        reparsed = parse_string(serialized)
        gen = reparsed.find("generator_version")
        assert gen is not None
        assert gen.get_string(0) == KICAD_GENERATOR_VERSION

    def test_symbol_library_generator_version_round_trips_quoted(self):
        from kicad_tools.schema.library import SymbolLibrary

        lib = SymbolLibrary(path="mem.kicad_sym")
        lib.create_symbol("MYSYM")
        serialized = lib.to_sexp_node().to_string()
        assert f'(generator_version "{KICAD_GENERATOR_VERSION}")' in serialized

        reparsed = parse_string(serialized)
        gen = reparsed.find("generator_version")
        assert gen is not None
        assert gen.get_string(0) == KICAD_GENERATOR_VERSION
