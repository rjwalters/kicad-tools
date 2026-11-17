"""Leading global options must not defeat the argv shims (issue #7126).

``footprint generate`` and the ``erc <report>`` shorthand were matched against
``argv[0]`` literally, so ``kct -q footprint generate ...`` and
``kct --verbose erc report.json`` failed in argparse.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.cli import normalize_argv


def _generate_argv(argv: list[str]) -> list[str]:
    with patch("kicad_tools.cli.footprint_generate.main", return_value=0) as fake:
        assert kct_main(argv) == 0
    assert fake.call_count == 1
    return list(fake.call_args[0][0])


@pytest.mark.parametrize(
    "prefix",
    [[], ["-q"], ["--verbose"], ["--units", "mils"], ["--units=mils", "-qq"]],
)
def test_footprint_generate_skips_global_prefix(prefix):
    rest = ["chip", "--size", "0402", "-o", "x.kicad_mod"]
    assert _generate_argv([*prefix, "footprint", "generate", *rest]) == rest


def test_footprint_generate_list_after_verbose():
    assert _generate_argv(["--verbose", "footprint", "generate", "--list"]) == ["--list"]


@pytest.mark.parametrize("prefix", [[], ["--verbose"], ["-q"], ["--units", "mm"]])
def test_erc_shorthand_inserts_parse_after_prefix(prefix):
    assert normalize_argv([*prefix, "erc", "report.json"]) == [
        *prefix,
        "erc",
        "parse",
        "report.json",
    ]


@pytest.mark.parametrize("sub", ["parse", "explain", "--help", "-h"])
def test_erc_known_subcommands_untouched(sub):
    argv = ["--verbose", "erc", sub]
    assert normalize_argv(argv) == argv
