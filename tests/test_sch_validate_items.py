"""Tests for label/net name enrichment in ERC validation messages.

Verifies that ``run_erc()`` extracts item descriptions from KiCad's raw
ERC JSON and populates both ``ValidationIssue.items`` and an enriched
``ValidationIssue.message``.
"""

from __future__ import annotations

import json
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cli.sch_validate import (
    ValidationIssue,
    ValidationResult,
    main,
    print_result,
    run_erc,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_erc_json(violations: list[dict], sheet_path: str = "/") -> str:
    """Build a minimal KiCad ERC JSON report string."""
    return json.dumps(
        {
            "source": "test.kicad_sch",
            "kicad_version": "8.0.0",
            "coordinate_units": "mm",
            "sheets": [
                {
                    "path": sheet_path,
                    "uuid_path": "00000000-0000-0000-0000-000000000000",
                    "violations": violations,
                }
            ],
        }
    )


def _run_erc_with_violations(violations: list[dict]) -> list[ValidationIssue]:
    """Run ``run_erc`` with mocked subprocess and tempfile containing *violations*."""
    import os
    import tempfile as _tempfile

    erc_json = _make_erc_json(violations)

    # Write ERC JSON to a real temp file so run_erc can read it back.
    tmp = _tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    tmp.write(erc_json)
    tmp.close()

    class _FakeTmp:
        name = tmp.name

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    with (
        patch("kicad_tools.cli.sch_validate.find_kicad_cli", return_value="/usr/bin/kicad-cli"),
        patch("kicad_tools.cli.sch_validate.subprocess.run") as mock_run,
        patch("kicad_tools.cli.sch_validate.filter_cross_sheet_global_labels", side_effect=lambda v, p: v),
        patch("tempfile.NamedTemporaryFile", return_value=_FakeTmp()),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        issues = run_erc("test.kicad_sch")

    if os.path.exists(tmp.name):
        os.unlink(tmp.name)
    return issues


# ---------------------------------------------------------------------------
# Tests: item extraction and message enrichment
# ---------------------------------------------------------------------------


class TestItemExtraction:
    """Verify items are extracted from raw KiCad violations."""

    def test_label_dangling_includes_items(self):
        violations = [
            {
                "type": "label_dangling",
                "severity": "warning",
                "description": "Label not connected to anything",
                "pos": {"x": 10, "y": 20},
                "items": [{"description": "Label 'SYNC_L'"}],
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert len(issues) == 1
        assert issues[0].items == ["Label 'SYNC_L'"]

    def test_similar_labels_includes_both_items(self):
        violations = [
            {
                "type": "similar_labels",
                "severity": "warning",
                "description": "Labels are similar and may be confused",
                "pos": {"x": 10, "y": 20},
                "items": [
                    {"description": "Label 'SIG1'"},
                    {"description": "Label 'SIG_1'"},
                ],
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert len(issues) == 1
        assert issues[0].items == ["Label 'SIG1'", "Label 'SIG_1'"]

    def test_non_label_type_still_populates_items(self):
        """Even non-label types should have items populated."""
        violations = [
            {
                "type": "pin_not_connected",
                "severity": "error",
                "description": "Pin not connected",
                "pos": {"x": 10, "y": 20},
                "items": [{"description": "Pin 1 (input) of R1"}],
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert len(issues) == 1
        assert issues[0].items == ["Pin 1 (input) of R1"]

    def test_empty_items_produces_empty_list(self):
        violations = [
            {
                "type": "label_dangling",
                "severity": "warning",
                "description": "Label not connected",
                "pos": {"x": 10, "y": 20},
                "items": [],
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert len(issues) == 1
        assert issues[0].items == []

    def test_missing_items_key_produces_empty_list(self):
        violations = [
            {
                "type": "label_dangling",
                "severity": "warning",
                "description": "Label not connected",
                "pos": {"x": 10, "y": 20},
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert len(issues) == 1
        assert issues[0].items == []


class TestMessageEnrichment:
    """Verify that messages for label-related types include item context."""

    def test_label_dangling_message_enriched(self):
        violations = [
            {
                "type": "label_dangling",
                "severity": "warning",
                "description": "Label not connected to anything",
                "pos": {"x": 10, "y": 20},
                "items": [{"description": "Label 'SYNC_L'"}],
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert "SYNC_L" in issues[0].message
        assert "[Label 'SYNC_L']" in issues[0].message

    def test_similar_labels_message_shows_both(self):
        violations = [
            {
                "type": "similar_labels",
                "severity": "warning",
                "description": "Labels are similar",
                "pos": {"x": 10, "y": 20},
                "items": [
                    {"description": "Label 'SIG1'"},
                    {"description": "Label 'SIG_1'"},
                ],
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert "SIG1" in issues[0].message
        assert "SIG_1" in issues[0].message

    def test_non_label_type_message_not_enriched(self):
        """pin_not_connected should NOT get bracket-enrichment."""
        violations = [
            {
                "type": "pin_not_connected",
                "severity": "error",
                "description": "Pin not connected",
                "pos": {"x": 10, "y": 20},
                "items": [{"description": "Pin 1 (input) of R1"}],
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert "[" not in issues[0].message
        assert issues[0].message == "Pin not connected"

    def test_no_double_print_when_desc_contains_item(self):
        """If the description already contains the item text, skip enrichment."""
        violations = [
            {
                "type": "single_global_label",
                "severity": "warning",
                "description": "Global label 'AUDIO_L' appears only once",
                "pos": {"x": 10, "y": 20},
                "items": [{"description": "Global label 'AUDIO_L'"}],
            }
        ]
        issues = _run_erc_with_violations(violations)
        # The item text "Global label 'AUDIO_L'" is contained in the
        # description, so no bracket enrichment should occur.
        assert issues[0].message.count("AUDIO_L") == 1

    def test_empty_items_no_brackets(self):
        violations = [
            {
                "type": "label_dangling",
                "severity": "warning",
                "description": "Label not connected",
                "pos": {"x": 10, "y": 20},
                "items": [],
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert "[" not in issues[0].message

    def test_hier_label_mismatch_enriched(self):
        violations = [
            {
                "type": "hier_label_mismatch",
                "severity": "error",
                "description": "Hierarchical label mismatch",
                "pos": {"x": 10, "y": 20},
                "items": [{"description": "Hierarchical label 'CLK'"}],
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert "CLK" in issues[0].message

    def test_multiple_net_names_enriched(self):
        violations = [
            {
                "type": "multiple_net_names",
                "severity": "error",
                "description": "Wire has multiple net names",
                "pos": {"x": 10, "y": 20},
                "items": [
                    {"description": "Net 'VCC_3V3'"},
                    {"description": "Net 'VCC_5V'"},
                ],
            }
        ]
        issues = _run_erc_with_violations(violations)
        assert "VCC_3V3" in issues[0].message
        assert "VCC_5V" in issues[0].message


class TestJSONOutput:
    """Verify that JSON output includes the items field."""

    def test_json_includes_items_key(self, capsys):
        """The JSON output should include an 'items' array for each issue."""
        result = ValidationResult(
            schematic="test.kicad_sch",
            checks_run=["erc"],
            issues=[
                ValidationIssue(
                    severity="warning",
                    category="erc",
                    message="Label not connected [Label 'SYNC_L']",
                    location="/",
                    items=["Label 'SYNC_L'"],
                ),
                ValidationIssue(
                    severity="error",
                    category="erc",
                    message="Pin not connected",
                    location="/",
                    items=["Pin 1 (input) of R1"],
                ),
                ValidationIssue(
                    severity="warning",
                    category="footprint",
                    message="Missing footprint: R2 (10k)",
                    location="/",
                    items=[],
                ),
            ],
        )

        # Simulate JSON output by calling main with a mocked validate_schematic
        with patch("kicad_tools.cli.sch_validate.validate_schematic", return_value=result):
            with patch("kicad_tools.cli.sch_validate.Path") as mock_path:
                mock_path.return_value.exists.return_value = True
                try:
                    main(["test.kicad_sch", "--format", "json"])
                except SystemExit:
                    pass

        output = capsys.readouterr().out
        data = json.loads(output)

        # Every issue should have an "items" key
        for issue in data["issues"]:
            assert "items" in issue, f"Issue missing 'items' key: {issue}"

        # First issue should have the label item
        assert data["issues"][0]["items"] == ["Label 'SYNC_L'"]
        # Third issue should have empty items
        assert data["issues"][2]["items"] == []


class TestTextOutput:
    """Verify that text output renders items below the message."""

    def test_items_printed_below_message(self, capsys):
        result = ValidationResult(
            schematic="test.kicad_sch",
            checks_run=["erc"],
            issues=[
                ValidationIssue(
                    severity="warning",
                    category="erc",
                    message="Label not connected [Label 'SYNC_L']",
                    location="/",
                    items=["Label 'SYNC_L'"],
                ),
            ],
        )
        print_result(result)
        output = capsys.readouterr().out
        assert "Label 'SYNC_L'" in output

    def test_no_items_no_extra_lines(self, capsys):
        result = ValidationResult(
            schematic="test.kicad_sch",
            checks_run=["erc"],
            issues=[
                ValidationIssue(
                    severity="warning",
                    category="erc",
                    message="Some warning",
                    location="/",
                    items=[],
                ),
            ],
        )
        print_result(result)
        output = capsys.readouterr().out
        lines = [l for l in output.split("\n") if l.strip()]
        # Should not have any indented item lines
        item_lines = [l for l in lines if l.startswith("       ")]
        assert len(item_lines) == 0
