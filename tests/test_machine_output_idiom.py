"""Tests for the canonical machine-output idiom (``--format json``).

Issue #4543: ``--format json`` is the canonical machine-output spelling;
the boolean ``--json`` flag is a permanently supported legacy alias on the
commands that already shipped it (``placement refine``, ``calibrate``, the
``footprint generate`` shape subcommands).

Covers:

* ``format_options`` helper semantics (normalization, precedence).
* Parse-level alias equivalence on the outer ``kct`` parser.
* Shim forwarding end-to-end: the outer ``--format json`` reaches the inner
  parser argv (the bug class ``test_cli_parser_drift.py`` exists for).
* ``footprint generate`` passthrough equivalence (both spellings emit
  identical JSON).
* A regression guard: no leaf subcommand on the outer parser may carry a
  ``--json`` flag without also offering ``--format`` with a ``json`` choice
  (i.e. the legacy-only bucket stays empty forever).

See ``docs/reference/machine-output.md`` for the design note and
``scripts/audit_machine_output.py`` for the full-inventory audit tool.
"""

from __future__ import annotations

import argparse
import json

import pytest

from kicad_tools.cli import footprint_generate
from kicad_tools.cli.format_options import (
    FORMAT_JSON,
    FORMAT_TEXT,
    add_format_flag,
    add_legacy_json_flag,
    normalize_format_alias,
    wants_json,
)
from kicad_tools.cli.parser import create_parser

# ---------------------------------------------------------------------------
# format_options helpers
# ---------------------------------------------------------------------------


def _pair_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pair")
    add_format_flag(parser)
    add_legacy_json_flag(parser)
    return parser


class TestFormatOptions:
    def test_defaults_are_text_no_json(self):
        args = _pair_parser().parse_args([])
        normalize_format_alias(args)
        assert args.format == FORMAT_TEXT
        assert args.json is False
        assert wants_json(args) is False

    def test_format_json_sets_legacy_attr(self):
        args = _pair_parser().parse_args(["--format", "json"])
        normalize_format_alias(args)
        assert args.json is True
        assert args.format == FORMAT_JSON

    def test_legacy_json_sets_format_attr(self):
        args = _pair_parser().parse_args(["--json"])
        normalize_format_alias(args)
        assert args.json is True
        assert args.format == FORMAT_JSON

    def test_both_spellings_together_do_not_conflict(self):
        args = _pair_parser().parse_args(["--json", "--format", "json"])
        normalize_format_alias(args)
        assert args.json is True
        assert args.format == FORMAT_JSON

    def test_both_spellings_yield_identical_namespace(self):
        via_format = normalize_format_alias(_pair_parser().parse_args(["--format", "json"]))
        via_json = normalize_format_alias(_pair_parser().parse_args(["--json"]))
        assert vars(via_format) == vars(via_json)

    def test_wants_json_does_not_mutate(self):
        args = _pair_parser().parse_args(["--format", "json"])
        assert wants_json(args) is True
        assert args.json is False  # untouched

    def test_wants_json_custom_attrs(self):
        args = argparse.Namespace(calibrate_format="json", calibrate_json=False)
        assert wants_json(args, format_attr="calibrate_format", json_attr="calibrate_json")

    def test_normalize_custom_attrs(self):
        args = argparse.Namespace(calibrate_format="text", calibrate_json=True)
        normalize_format_alias(args, format_attr="calibrate_format", json_attr="calibrate_json")
        assert args.calibrate_format == FORMAT_JSON


# ---------------------------------------------------------------------------
# Outer-parser alias equivalence (parse level)
# ---------------------------------------------------------------------------


class TestOuterParserAliases:
    @pytest.fixture(scope="class")
    def parser(self):
        return create_parser()

    def test_placement_refine_both_spellings_equivalent(self, parser):
        via_format = parser.parse_args(["placement", "refine", "b.kicad_pcb", "--format", "json"])
        via_json = parser.parse_args(["placement", "refine", "b.kicad_pcb", "--json"])
        assert wants_json(via_format) is True
        assert wants_json(via_json) is True
        assert vars(normalize_format_alias(via_format)) == vars(normalize_format_alias(via_json))

    def test_placement_refine_accepts_both_flags_at_once(self, parser):
        args = parser.parse_args(
            ["placement", "refine", "b.kicad_pcb", "--json", "--format", "json"]
        )
        assert wants_json(args) is True

    def test_placement_refine_default_is_text(self, parser):
        args = parser.parse_args(["placement", "refine", "b.kicad_pcb"])
        assert args.format == FORMAT_TEXT
        assert wants_json(args) is False

    def test_calibrate_both_spellings_equivalent(self, parser):
        via_format = parser.parse_args(["calibrate", "--format", "json"])
        via_json = parser.parse_args(["calibrate", "--json"])
        for args in (via_format, via_json):
            normalize_format_alias(args, format_attr="calibrate_format", json_attr="calibrate_json")
        assert vars(via_format) == vars(via_json)
        assert via_format.calibrate_json is True

    def test_calibrate_default_is_text(self, parser):
        args = parser.parse_args(["calibrate"])
        assert args.calibrate_format == FORMAT_TEXT
        assert args.calibrate_json is False


# ---------------------------------------------------------------------------
# Shim forwarding end-to-end (outer flag reaches the inner parser argv)
# ---------------------------------------------------------------------------


class TestShimForwarding:
    def _forwarded_placement_argv(self, monkeypatch, outer_argv: list[str]) -> list[str]:
        import kicad_tools.cli.placement_cmd as placement_cmd
        from kicad_tools.cli.commands.placement import run_placement_command

        captured: list[list[str]] = []
        monkeypatch.setattr(placement_cmd, "main", lambda argv: captured.append(argv) or 0)
        args = create_parser().parse_args(outer_argv)
        assert run_placement_command(args) == 0
        assert len(captured) == 1
        return captured[0]

    def _forwarded_calibrate_argv(self, monkeypatch, outer_argv: list[str]) -> list[str]:
        import kicad_tools.cli.calibrate_cmd as calibrate_cmd
        from kicad_tools.cli import _run_calibrate_command

        captured: list[list[str]] = []
        monkeypatch.setattr(calibrate_cmd, "main", lambda argv: captured.append(argv) or 0)
        args = create_parser().parse_args(outer_argv)
        assert _run_calibrate_command(args) == 0
        assert len(captured) == 1
        return captured[0]

    def test_placement_refine_forwards_format(self, monkeypatch):
        argv = self._forwarded_placement_argv(
            monkeypatch, ["placement", "refine", "b.kicad_pcb", "--format", "json"]
        )
        assert argv[:2] == ["refine", "b.kicad_pcb"]
        assert "--format" in argv
        assert argv[argv.index("--format") + 1] == "json"

    def test_placement_refine_forwards_legacy_json(self, monkeypatch):
        argv = self._forwarded_placement_argv(
            monkeypatch, ["placement", "refine", "b.kicad_pcb", "--json"]
        )
        assert "--json" in argv

    def test_placement_refine_forwarded_argv_parses_in_inner_parser(self, monkeypatch):
        # The inner placement parser must accept everything the shim forwards.
        import kicad_tools.cli.placement_cmd as placement_cmd

        real_main = placement_cmd.main  # before the helper stubs it
        argv = self._forwarded_placement_argv(
            monkeypatch, ["placement", "refine", "b.kicad_pcb", "--format", "json"]
        )
        # Re-parse with the real inner main, stopping at cmd dispatch by
        # stubbing cmd_refine.
        inner_captured: list[argparse.Namespace] = []
        monkeypatch.setattr(
            placement_cmd, "cmd_refine", lambda args: inner_captured.append(args) or 0
        )
        assert real_main(argv) == 0
        assert len(inner_captured) == 1
        inner = normalize_format_alias(inner_captured[0])
        assert inner.json is True

    def test_calibrate_forwards_format(self, monkeypatch):
        argv = self._forwarded_calibrate_argv(monkeypatch, ["calibrate", "--format", "json"])
        assert "--format" in argv
        assert argv[argv.index("--format") + 1] == "json"

    def test_calibrate_forwards_legacy_json(self, monkeypatch):
        argv = self._forwarded_calibrate_argv(monkeypatch, ["calibrate", "--json"])
        assert "--json" in argv

    def test_calibrate_text_default_forwards_no_format(self, monkeypatch):
        argv = self._forwarded_calibrate_argv(monkeypatch, ["calibrate"])
        assert "--format" not in argv
        assert "--json" not in argv


# ---------------------------------------------------------------------------
# footprint generate passthrough (both spellings emit identical JSON)
# ---------------------------------------------------------------------------


class TestFootprintGenerateAliases:
    SHAPES = [
        ["soic", "--pins", "8"],
        ["chip", "--size", "0603"],
    ]

    @pytest.mark.parametrize("shape_argv", SHAPES, ids=lambda a: a[0])
    def test_format_json_and_json_emit_identical_payload(self, shape_argv, capsys):
        assert footprint_generate.main([*shape_argv, "--format", "json"]) == 0
        via_format = capsys.readouterr().out
        assert footprint_generate.main([*shape_argv, "--json"]) == 0
        via_json = capsys.readouterr().out
        assert via_format == via_json
        json.loads(via_format)  # valid single JSON document

    def test_text_default_is_not_json(self, capsys):
        assert footprint_generate.main(["soic", "--pins", "8"]) == 0
        out = capsys.readouterr().out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# Regression guard: the legacy-only bucket stays empty
# ---------------------------------------------------------------------------


def _iter_leaves(parser, path=()):
    subactions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subactions:
        yield path, parser
        return
    for subaction in subactions:
        for name, sub in subaction.choices.items():
            yield from _iter_leaves(sub, (*path, name))


class TestCanonicalIdiomGuard:
    def test_no_json_only_leaf_subcommands(self):
        """Every leaf with a --json flag must also offer --format with json.

        The canonical machine-output spelling is ``--format json``
        (docs/reference/machine-output.md, #4543).  Existing ``--json`` flags
        are legacy aliases and must be paired with the canonical spelling;
        new commands must not introduce ``--json`` at all.
        """
        offenders = []
        for path, leaf in _iter_leaves(create_parser()):
            has_json = False
            has_format_json = False
            for action in leaf._actions:
                if "--json" in action.option_strings:
                    has_json = True
                if (
                    "--format" in action.option_strings
                    and action.choices
                    and "json" in action.choices
                ):
                    has_format_json = True
            if has_json and not has_format_json:
                offenders.append(" ".join(path))
        assert offenders == [], (
            f"Leaf subcommands with a legacy --json flag but no canonical "
            f"'--format json': {offenders}. New commands must use --format "
            f"with a 'json' choice (see docs/reference/machine-output.md); "
            f"if you are aliasing an existing --json, add --format alongside it."
        )

    def test_known_alias_carriers_offer_both_spellings(self):
        """placement refine and calibrate each carry both spellings."""
        found = {}
        for path, leaf in _iter_leaves(create_parser()):
            key = " ".join(path)
            if key in ("placement refine", "calibrate"):
                opts = {o for a in leaf._actions for o in a.option_strings}
                found[key] = opts
        assert set(found) == {"placement refine", "calibrate"}
        for key, opts in found.items():
            assert "--json" in opts, key
            assert "--format" in opts, key
