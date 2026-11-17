"""Tests for --strategy bayesian wiring (issue #5801); run without Ax installed."""

from __future__ import annotations

import pytest

from kicad_tools.placement import bo_strategy


def test_parser_accepts_bayesian():
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(
        ["optimize-placement", "b.kicad_pcb", "--strategy", "bayesian"]
    )
    assert args.strategy == "bayesian"


def test_parser_rejects_unknown_strategy():
    from kicad_tools.cli.parser import create_parser

    with pytest.raises(SystemExit):
        create_parser().parse_args(["optimize-placement", "b.kicad_pcb", "--strategy", "nope"])


def test_registry_enum_lists_bayesian():
    from kicad_tools.mcp.tools.registry import get_tool

    tool = get_tool("optimize_placement")
    assert "bayesian" in tool.parameters["properties"]["strategy"]["enum"]


def test_create_strategy_bayesian_without_ax(monkeypatch):
    from kicad_tools.cli.optimize_placement_cmd import _create_strategy

    monkeypatch.setattr(bo_strategy, "_HAS_AX", False)
    with pytest.raises(ImportError, match="bayesian"):
        _create_strategy("bayesian")


def test_create_strategy_bayesian_with_ax_flag(monkeypatch):
    from kicad_tools.cli.optimize_placement_cmd import _create_strategy

    monkeypatch.setattr(bo_strategy, "_HAS_AX", True)
    strategy = _create_strategy("bayesian")
    assert isinstance(strategy, bo_strategy.BayesianOptStrategy)


def test_population_size_is_batch_size(monkeypatch):
    monkeypatch.setattr(bo_strategy, "_HAS_AX", True)
    strategy = bo_strategy.BayesianOptStrategy()
    assert strategy._population_size == 8
    strategy._batch_size = 3
    assert strategy._population_size == 3


def test_unknown_strategy_message_lists_bayesian():
    from kicad_tools.cli.optimize_placement_cmd import _create_strategy

    with pytest.raises(ValueError, match="bayesian"):
        _create_strategy("nonexistent")
