"""KiCad CLI round-trip smoke tests for schematic generation.

Regression guard for issue #2780: kicad-tools' schematic writer must
produce files that ``kicad-cli sch erc`` can load under KiCad 10.

Two bugs caused every generated schematic to fail with
"Failed to load schematic" (exit 3):

1. ``(property "Value" 330)`` — numeric-looking string values (e.g. a
   resistor with ``value="330"``) were emitted as bare numerics because
   the SExp serializer's ``_needs_quoting`` returned ``False`` for
   strings that parse as numeric. Fixed by using ``SExp.quoted_atom``
   in ``symbol_property_node`` (``src/kicad_tools/sexp/builders.py``).

2. ``(page 1)`` — the page number inside ``sheet_instances`` was emitted
   as a bare numeric for the same reason. Fixed the same way for
   ``sheet_instances`` in the same module.

This test builds a minimal schematic exercising both bugs and asserts
``kicad-cli sch erc`` accepts it (ERC violations themselves are not the
concern — only that the file is well-formed enough to load).

When ``find_kicad_cli()`` returns ``None`` the entire module is skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli, run_erc
from kicad_tools.schematic.models.schematic import Schematic

pytestmark = pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")


def _format_load_failure(
    producer: str,
    sch_path: Path,
    return_code: int,
    stderr: str,
) -> str:
    """Build a clear, attributed error message for a kicad-cli load failure."""
    return (
        "kicad-cli rejected schematic emitted by kicad-tools writer.\n"
        f"  Producer: {producer}\n"
        f"  Schematic: {sch_path}\n"
        f"  kicad-cli return code: {return_code}\n"
        "  kicad-cli stderr:\n"
        f"    {stderr.strip() or '<empty>'}\n"
        "\n"
        f"Inspect the emitted file: head -50 {sch_path}"
    )


def _assert_kicad_cli_loads(
    sch_path: Path,
    producer: str,
    tmp_path: Path,
) -> None:
    """Assert that kicad-cli successfully loads the given schematic.

    Uses ``run_erc`` as the load primitive — kicad-cli only writes an ERC
    report when the file loads; "Failed to load schematic" returns exit
    3 with no report.  ERC violations themselves are not a concern here.
    """
    output_path = tmp_path / f"{sch_path.stem}_erc.json"
    result = run_erc(sch_path, output_path=output_path, format="json")

    assert result.success, _format_load_failure(
        producer, sch_path, result.return_code, result.stderr
    )


class TestSchematicSaveRoundtrip:
    """``Schematic.write()`` must produce a kicad-cli-loadable file.

    Targets the regression where ``(property "Value" 330)`` and
    ``(page 1)`` were emitted as bare numerics, causing
    ``kicad-cli sch erc`` to fail with "Failed to load schematic".
    """

    def test_blank_schematic_roundtrip(self, tmp_path: Path) -> None:
        """An empty schematic must load via kicad-cli.

        Verifies that the ``sheet_instances`` page emission uses
        ``(page "1")`` (quoted) rather than ``(page 1)`` (bare numeric).
        """
        sch = Schematic(title="kicad-cli roundtrip blank", revision="A")
        sch_path = tmp_path / "blank.kicad_sch"
        sch.write(sch_path)

        # Regression guard: the page field must be quoted.
        contents = sch_path.read_text()
        assert '(page "1")' in contents, (
            "Regression guard: sheet_instances must emit (page \"1\") with "
            "the page value quoted.  Bare-numeric (page 1) is rejected by "
            "kicad-cli with 'Failed to load schematic' (exit 3).  See "
            "src/kicad_tools/sexp/builders.py:sheet_instances."
        )

        _assert_kicad_cli_loads(
            sch_path, producer="Schematic.write (blank)", tmp_path=tmp_path
        )

    def test_schematic_with_numeric_value_symbol_roundtrip(self, tmp_path: Path) -> None:
        """A schematic with a resistor whose value is "330" must load.

        This is the exact regression vector from issue #2780: a numeric-
        looking property value (``value="330"``) was emitted as
        ``(property "Value" 330)`` rather than ``(property "Value" "330")``,
        which kicad-cli 10 rejects.

        We accept that the symbol library lookup may not succeed in this
        minimal test env; the goal is that the writer's output passes the
        kicad-cli load gate.
        """
        sch = Schematic(title="numeric value roundtrip", revision="A")
        # Add a resistor with a numeric-looking value. The symbol_generator
        # path emits the lib_symbols entry (where Value is already an f-
        # string with quotes) AND the SymbolInstance path which historically
        # passed the bare string to SExp.list. The fix in
        # ``symbol_property_node`` covers the SymbolInstance path.
        try:
            sch.add_symbol(
                lib_id="Device:R",
                x=50.0,
                y=50.0,
                ref="R1",
                value="330",
                footprint="Resistor_SMD:R_0805_2012Metric",
            )
        except Exception as exc:
            pytest.skip(f"Symbol library not available in test env: {exc}")

        sch_path = tmp_path / "resistor.kicad_sch"
        sch.write(sch_path)

        # Regression guard: the resistor's "Value" property must be quoted.
        contents = sch_path.read_text()
        assert '(property "Value" "330"' in contents, (
            "Regression guard: a SymbolInstance with value=\"330\" must emit "
            "(property \"Value\" \"330\" ...) with the value quoted.  Bare-"
            "numeric form is rejected by kicad-cli with 'Failed to load "
            "schematic' (exit 3).  See "
            "src/kicad_tools/sexp/builders.py:symbol_property_node."
        )

        _assert_kicad_cli_loads(
            sch_path,
            producer="Schematic.write + numeric-value resistor",
            tmp_path=tmp_path,
        )
