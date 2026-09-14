"""Suppressed defaults must work with argparse's help interpolation (Python 3.14)."""

import argparse
from unittest.mock import patch

import pytest

from kicad_tools.cli.format_options import add_format_flag
from kicad_tools.cli.parts_cmd import main


def test_suppressed_format_help_and_namespace():
    parser = argparse.ArgumentParser()
    add_format_flag(parser, default=argparse.SUPPRESS)
    assert "Output format" in parser.format_help()
    assert "default:" not in parser.format_help()
    assert not hasattr(parser.parse_args([]), "format")
    assert parser.parse_args(["--format", "json"]).format == "json"


def test_normal_format_help_keeps_default():
    parser = argparse.ArgumentParser()
    add_format_flag(parser)
    assert "default: text" in parser.format_help()
    assert parser.parse_args([]).format == "text"


def test_custom_format_help_is_preserved():
    parser = argparse.ArgumentParser()
    add_format_flag(parser, default=argparse.SUPPRESS, help_text="Choose a representation")
    assert "Choose a representation" in parser.format_help()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["cache", "stats"], "text"),
        (["cache", "--format", "json", "stats"], "json"),
        (["cache", "stats", "--format", "json"], "json"),
        (["cache", "--format", "json", "stats", "--format", "text"], "text"),
    ],
)
def test_parts_cache_format_precedence(argv, expected):
    with patch("kicad_tools.cli.parts_cmd._cache", return_value=0) as handler:
        assert main(argv) == 0
    assert handler.call_args.args[0].format == expected


def test_parts_sync_catalog_parser():
    with patch("kicad_tools.cli.parts_cmd._sync_catalog", return_value=0) as handler:
        assert main(["sync-catalog", "--format", "json"]) == 0
    assert handler.call_args.args[0].format == "json"
