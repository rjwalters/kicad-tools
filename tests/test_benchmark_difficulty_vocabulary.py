"""The benchmark difficulty vocabulary has one source of truth (issue #4752).

``kct benchmark run --difficulty`` previously hardcoded its valid values in
two unrelated places: argparse ``choices`` in ``cli/parser.py`` and a
"valid options: easy, medium, hard" string in the handler's error message
(``cli/commands/benchmark.py``).  Adding a member to ``Difficulty`` updated
neither, so the two could silently drift from the enum and from each other.

Both now derive from ``Difficulty.values()``; these tests fail if anyone
re-hardcodes either site.
"""

from __future__ import annotations

import argparse

import pytest

from kicad_tools.benchmark.cases import Difficulty
from kicad_tools.cli.commands.benchmark import _run_benchmark
from kicad_tools.cli.parser import create_parser


def _difficulty_action() -> argparse.Action:
    """The ``--difficulty`` action of the ``benchmark run`` subparser."""
    parser = create_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    benchmark = subparsers.choices["benchmark"]
    benchmark_subparsers = next(
        a for a in benchmark._actions if isinstance(a, argparse._SubParsersAction)
    )
    run = benchmark_subparsers.choices["run"]
    return next(a for a in run._actions if "--difficulty" in a.option_strings)


class TestDifficultyVocabulary:
    def test_enum_values_are_the_declared_order(self):
        assert Difficulty.values() == ["easy", "medium", "hard"]
        assert Difficulty.values() == [d.value for d in Difficulty]

    def test_parser_choices_come_from_the_enum(self):
        action = _difficulty_action()
        assert list(action.choices or []) == Difficulty.values()

    def test_error_message_vocabulary_comes_from_the_enum(self, capsys):
        """The handler's unknown-difficulty message must name exactly the
        enum's values -- not a hand-maintained copy of them."""
        args = argparse.Namespace(difficulty="impossible", format="text")
        assert _run_benchmark(args) == 1
        out = capsys.readouterr().out
        assert "Unknown difficulty: impossible" in out
        assert f"(valid options: {', '.join(Difficulty.values())})" in out
        for value in Difficulty.values():
            assert value in out

    @pytest.mark.parametrize("value", Difficulty.values())
    def test_every_enum_value_is_accepted_by_the_parser(self, value: str):
        args = create_parser().parse_args(["benchmark", "run", "--difficulty", value])
        assert args.difficulty == value
