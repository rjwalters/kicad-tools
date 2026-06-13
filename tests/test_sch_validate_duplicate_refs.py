"""Tests for duplicate reference designator detection across hierarchical sheets.

Verifies that ``check_duplicate_references()`` correctly identifies
cross-sheet reference conflicts while allowing legitimate multi-unit
symbol splits.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kicad_tools.cli.sch_validate import (
    ValidationIssue,
    check_duplicate_references,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_symbol(
    ref: str,
    lib_id: str = "Device:R",
    value: str = "10k",
    uuid: str = "aaa",
    unit: int = 1,
):
    """Create a minimal mock symbol."""
    sym = MagicMock()
    sym.reference = ref
    sym.lib_id = lib_id
    sym.value = value
    sym.uuid = uuid
    sym.unit = unit
    return sym


def _make_node(name: str, symbols: list):
    """Create a minimal mock hierarchy node with a loadable schematic."""
    node = MagicMock()
    node.name = name
    node.get_path_string.return_value = f"/{name}"
    node.path = f"/fake/{name}.kicad_sch"
    # Attach symbols so the patched Schematic.load returns them
    node._symbols = symbols
    return node


def _run_check(nodes: list) -> list[ValidationIssue]:
    """Run check_duplicate_references with mocked hierarchy and schematics."""
    hierarchy = MagicMock()
    hierarchy.all_nodes.return_value = nodes

    def _load_side_effect(path):
        for n in nodes:
            if n.path == path:
                sch = MagicMock()
                sch.symbols = n._symbols
                return sch
        raise FileNotFoundError(path)

    with (
        patch(
            "kicad_tools.cli.sch_validate.build_hierarchy",
            return_value=hierarchy,
        ),
        patch(
            "kicad_tools.cli.sch_validate.Schematic.load",
            side_effect=_load_side_effect,
        ),
    ):
        return check_duplicate_references("/fake/root.kicad_sch")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDuplicateReferences:
    """Core duplicate-reference detection tests."""

    def test_no_duplicates_clean(self):
        """No issues when all references are unique across sheets."""
        nodes = [
            _make_node("Sheet1", [_make_symbol("R1", uuid="u1")]),
            _make_node("Sheet2", [_make_symbol("R2", uuid="u2")]),
        ]
        issues = _run_check(nodes)
        assert len(issues) == 0

    def test_same_ref_different_lib_id(self):
        """Different components sharing a reference should be flagged."""
        nodes = [
            _make_node(
                "DAC",
                [_make_symbol("R7", lib_id="Device:R", value="1k", uuid="u1")],
            ),
            _make_node(
                "Sync",
                [_make_symbol("R7", lib_id="Device:R_Small", value="4.7k", uuid="u2")],
            ),
        ]
        issues = _run_check(nodes)
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].category == "duplicate_reference"
        assert "R7" in issues[0].message
        assert "DAC" in issues[0].location
        assert "Sync" in issues[0].location

    def test_same_ref_same_lib_id_same_unit(self):
        """Same lib_id and same unit on different sheets is a conflict."""
        nodes = [
            _make_node(
                "Power",
                [_make_symbol("C9", lib_id="Device:C", value="100nF", uuid="u1")],
            ),
            _make_node(
                "Sync",
                [_make_symbol("C9", lib_id="Device:C", value="10uF", uuid="u2")],
            ),
        ]
        issues = _run_check(nodes)
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "C9" in issues[0].message

    def test_multi_unit_not_flagged(self):
        """Multi-unit symbols (same ref, same lib_id, different units) are OK."""
        nodes = [
            _make_node(
                "Sheet1",
                [
                    _make_symbol(
                        "U1",
                        lib_id="Amplifier_Operational:LM358",
                        value="LM358",
                        uuid="u1",
                        unit=1,
                    )
                ],
            ),
            _make_node(
                "Sheet2",
                [
                    _make_symbol(
                        "U1",
                        lib_id="Amplifier_Operational:LM358",
                        value="LM358",
                        uuid="u2",
                        unit=2,
                    )
                ],
            ),
        ]
        issues = _run_check(nodes)
        assert len(issues) == 0

    def test_multi_unit_same_sheet_not_flagged(self):
        """Multi-unit symbols on the same sheet (different units) are OK."""
        nodes = [
            _make_node(
                "Sheet1",
                [
                    _make_symbol(
                        "U1",
                        lib_id="Amplifier_Operational:LM358",
                        uuid="u1",
                        unit=1,
                    ),
                    _make_symbol(
                        "U1",
                        lib_id="Amplifier_Operational:LM358",
                        uuid="u2",
                        unit=2,
                    ),
                ],
            ),
        ]
        issues = _run_check(nodes)
        assert len(issues) == 0

    def test_power_symbols_skipped(self):
        """Power symbols (power:*) should not be checked."""
        nodes = [
            _make_node(
                "Sheet1",
                [_make_symbol("#PWR01", lib_id="power:GND", uuid="u1")],
            ),
            _make_node(
                "Sheet2",
                [_make_symbol("#PWR01", lib_id="power:GND", uuid="u2")],
            ),
        ]
        issues = _run_check(nodes)
        assert len(issues) == 0

    def test_unannotated_skipped(self):
        """References like '?' should be skipped."""
        nodes = [
            _make_node("Sheet1", [_make_symbol("?", uuid="u1")]),
            _make_node("Sheet2", [_make_symbol("?", uuid="u2")]),
        ]
        issues = _run_check(nodes)
        assert len(issues) == 0

    def test_multiple_conflicts_reported_separately(self):
        """Each conflicting reference gets its own issue."""
        nodes = [
            _make_node(
                "DAC",
                [
                    _make_symbol("R7", uuid="u1"),
                    _make_symbol("C9", lib_id="Device:C", uuid="u3"),
                ],
            ),
            _make_node(
                "Sync",
                [
                    _make_symbol("R7", uuid="u2"),
                    _make_symbol("C9", lib_id="Device:C", uuid="u4"),
                ],
            ),
        ]
        issues = _run_check(nodes)
        assert len(issues) == 2
        refs = {i.message.split()[2] for i in issues}
        assert "R7" in refs
        assert "C9" in refs

    def test_values_included_in_message_for_different_lib_ids(self):
        """When lib_ids differ, values should be shown for diagnostics."""
        nodes = [
            _make_node(
                "SheetA",
                [_make_symbol("R1", lib_id="Device:R", value="1k", uuid="u1")],
            ),
            _make_node(
                "SheetB",
                [_make_symbol("R1", lib_id="Device:R_Small", value="4.7k", uuid="u2")],
            ),
        ]
        issues = _run_check(nodes)
        assert len(issues) == 1
        assert "1k" in issues[0].message
        assert "4.7k" in issues[0].message

    def test_sheet_parse_failure_produces_info(self):
        """If a sheet fails to parse, an info-level issue is emitted."""
        node = MagicMock()
        node.name = "Bad"
        node.get_path_string.return_value = "/Bad"
        node.path = "/fake/bad.kicad_sch"

        hierarchy = MagicMock()
        hierarchy.all_nodes.return_value = [node]

        with (
            patch(
                "kicad_tools.cli.sch_validate.build_hierarchy",
                return_value=hierarchy,
            ),
            patch(
                "kicad_tools.cli.sch_validate.Schematic.load",
                side_effect=Exception("parse error"),
            ),
        ):
            issues = check_duplicate_references("/fake/root.kicad_sch")

        assert len(issues) == 1
        assert issues[0].severity == "info"
        assert "Skipped" in issues[0].message
