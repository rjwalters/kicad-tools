"""Sourcing preflight uses populated references, not virtual or excluded symbols."""

import builtins
import json
from argparse import Namespace
from unittest.mock import patch

import pytest

from kicad_tools.cli.commands.parts import _run_suggest_command
from kicad_tools.cost.suggest import PartSuggestion, SuggestionResult
from kicad_tools.schema.bom import BOM, BOMItem


def item(reference, **kwargs):
    return BOMItem(reference, "10k", "Resistor_SMD:R_0603", "Device:R", **kwargs)


@pytest.fixture
def args(tmp_path):
    schematic = tmp_path / "test.kicad_sch"
    schematic.touch()
    return Namespace(
        schematic=str(schematic),
        show_all=False,
        no_basic_preference=False,
        min_stock=100,
        max_suggestions=2,
        format="json",
    )


@pytest.mark.parametrize("show_all", [False, True])
@pytest.mark.parametrize("output_format", ["json", "table"])
def test_excluded_bom_never_constructs_suggester(args, capsys, show_all, output_format):
    args.show_all = show_all
    args.format = output_format
    bom = BOM(items=[item("TP1", in_bom=False), item("#PWR01"), item("R1", dnp=True)])
    with (
        patch("kicad_tools.schema.bom.extract_bom", return_value=bom),
        patch("kicad_tools.cost.suggest.PartSuggester") as suggester,
    ):
        assert _run_suggest_command(args) == 0
    suggester.assert_not_called()
    output = capsys.readouterr()
    assert "Analyzing" not in output.err
    if output_format == "json":
        payload = json.loads(output.out)
        assert payload["suggestions"] == []
        assert payload["summary"]["active_references"] == 0


def test_sourced_and_excluded_bom_needs_no_optional_suggester(args, capsys):
    bom = BOM(items=[item("R1", lcsc="C25804"), item("TP1", in_bom=False)])
    original_import = builtins.__import__

    def reject_suggester(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "kicad_tools.cost.suggest" and "PartSuggester" in fromlist:
            raise ImportError("optional suggester must not be loaded")
        return original_import(name, globals, locals, fromlist, level)

    with (
        patch("kicad_tools.schema.bom.extract_bom", return_value=bom),
        patch("builtins.__import__", side_effect=reject_suggester),
    ):
        assert _run_suggest_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["active_references"] == 1
    assert payload["summary"]["missing_lcsc_references"] == 0


@pytest.mark.parametrize("output_format", ["json", "table"])
def test_populated_references_and_grouped_results_are_distinct(args, capsys, output_format):
    args.format = output_format
    bom = BOM(items=[item("R1"), item("R2"), item("TP1", in_bom=False), item("R3", dnp=True)])
    result = SuggestionResult(suggestions=[PartSuggestion("R1", "10k", "R_0603", "0603", None)])
    with (
        patch("kicad_tools.schema.bom.extract_bom", return_value=bom),
        patch("kicad_tools.cost.suggest.PartSuggester") as suggester,
    ):
        suggester.return_value.__enter__.return_value.suggest_for_bom.return_value = result
        assert _run_suggest_command(args) == 0
    suggester.return_value.__enter__.return_value.suggest_for_bom.assert_called_once_with(bom)
    output = capsys.readouterr()
    assert "Analyzing 2 populated references (2 missing LCSC numbers)" in output.err
    if output_format == "json":
        summary = json.loads(output.out)["summary"]
        assert summary["active_references"] == 2
        assert summary["missing_lcsc_references"] == 2
        assert summary["count_unit"] == "grouped_parts"
        assert summary["total_components"] == 1
    else:
        assert "grouped parts" in output.out
