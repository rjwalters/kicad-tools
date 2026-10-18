"""``kct route`` hands its argv straight to ``route_cmd.main`` (issue #7017).

``kct route`` used to declare every flag three times: an outer parser in
``cli/parser.py``, a forwarding shim in ``cli/commands/routing.py`` that
rebuilt an argv from the outer ``Namespace`` flag by flag, and the real
parser in ``route_cmd._route_parser``.  The shim forwarded a value only when
it differed from a hard-coded default and, to tell "typed the default" from
"typed nothing" (#4875), sniffed ``sys.argv`` -- which an in-process
``kicad_tools.cli.main([...])`` call never populates.  So an explicit
``--clearance 0.15 --manufacturer jlcpcb`` was silently dropped and the
router took both values from the board's own rules instead.

These tests pin the replacement contract: whatever follows ``route`` reaches
``route_cmd.main`` verbatim, the only permitted addition being a trailing
``--quiet`` contributed by the global ``-q``.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from kicad_tools.cli import main as kct_main


def _forwarded(argv: list[str]) -> list[str]:
    """Run ``kct <argv>`` in-process and return what ``route_cmd.main`` got."""
    with patch("kicad_tools.cli.route_cmd.main", return_value=0) as fake:
        assert kct_main(argv) == 0
    assert fake.call_count == 1
    return list(fake.call_args[0][0])


# ---------------------------------------------------------------------------
# Regression: explicit defaults must survive an in-process call (#7017/#4875)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        ["--clearance", "0.15", "--manufacturer", "jlcpcb"],
        ["--clearance", "0.15", "--mfr", "jlcpcb"],
        ["--clearance=0.15", "--manufacturer=jlcpcb"],
    ],
    ids=["long", "mfr-alias", "equals-spelling"],
)
def test_explicit_default_clearance_and_manufacturer_reach_router(flags):
    forwarded = _forwarded(["route", "x.kicad_pcb", *flags])
    assert forwarded == ["x.kicad_pcb", *flags]


# ---------------------------------------------------------------------------
# Characterization: kct route <argv>  ==  route_cmd.main(<argv>)
# ---------------------------------------------------------------------------

CHARACTERIZATION_CASES = [
    ["x.kicad_pcb"],
    ["x.kicad_pcb", "-o", "out.kicad_pcb"],
    ["x.kicad_pcb", "--strategy", "monte-carlo"],
    ["x.kicad_pcb", "--nets", "SDA,SCL"],
    ["x.kicad_pcb", "--no-auto-layers"],
    ["x.kicad_pcb", "--auto-layers"],  # #4502: an explicit default
    ["x.kicad_pcb", "--format", "json"],
    ["x.kicad_pcb", "--grid", "auto"],  # explicit default the shim dropped
    ["x.kicad_pcb", "--no-auto-pour"],  # BooleanOptionalAction
    ["x.kicad_pcb", "--per-net-timeout", "30.0"],  # explicit default
    ["x.kicad_pcb", "--show-congestion"],  # formerly inner-only
    ["--clearance", "0.15", "x.kicad_pcb"],  # optional before the positional
    ["x.kicad_pcb", "--quiet", "--verbose", "--dry-run"],
]


@pytest.mark.parametrize("argv", CHARACTERIZATION_CASES, ids=lambda a: " ".join(a))
def test_kct_route_forwards_argv_verbatim(argv):
    assert _forwarded(["route", *argv]) == argv


# ---------------------------------------------------------------------------
# Global flags in front of ``route``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("quiet", ["-q", "--quiet"])
def test_global_quiet_becomes_inner_quiet(quiet):
    assert _forwarded([quiet, "route", "x.kicad_pcb"]) == ["x.kicad_pcb", "--quiet"]


@pytest.mark.parametrize("inner", ["-q", "--quiet"])
def test_global_quiet_not_duplicated_when_route_already_quiet(inner):
    assert _forwarded(["-q", "route", "x.kicad_pcb", inner]) == ["x.kicad_pcb", inner]


@pytest.mark.parametrize(
    "prefix",
    [["--units", "mils"], ["--units=mils"]],
    ids=["separate", "equals"],
)
def test_global_units_sets_formatter(prefix):
    from kicad_tools.units import get_current_formatter, set_current_formatter

    before = get_current_formatter()
    try:
        assert _forwarded([*prefix, "route", "x.kicad_pcb"]) == ["x.kicad_pcb"]
        assert get_current_formatter().system.value == "mils"
    finally:
        set_current_formatter(before)


def test_all_globals_together():
    forwarded = _forwarded(["--verbose", "-q", "--units", "mm", "route", "x.kicad_pcb", "-v"])
    assert forwarded == ["x.kicad_pcb", "-v", "--quiet"]


def test_global_verbose_prints_traceback_on_error(capsys):
    def boom(argv):
        raise RuntimeError("router exploded")

    with patch("kicad_tools.cli.route_cmd.main", side_effect=boom):
        rc = kct_main(["--verbose", "route", "x.kicad_pcb"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "router exploded" in err
    assert "Traceback" in err


def test_error_without_verbose_has_no_traceback(capsys):
    with patch("kicad_tools.cli.route_cmd.main", side_effect=RuntimeError("router exploded")):
        rc = kct_main(["route", "x.kicad_pcb"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "router exploded" in err
    assert "Traceback" not in err


def test_invalid_global_units_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        kct_main(["--units", "furlongs", "route", "x.kicad_pcb"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Real inner parser: help, usage errors, unknown flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_route_help_is_the_inner_parser_help(flag, capsys):
    from kicad_tools.cli.route_cmd import _route_parser

    with pytest.raises(SystemExit) as exc:
        kct_main(["route", flag])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out == _route_parser().format_help()
    assert "exit codes:" in out
    # Formerly inner-only flags are now listed by ``kct route --help``.
    for inner_only in ("--show-congestion", "--profile", "--batch-routing"):
        assert inner_only in out


def test_route_without_pcb_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        kct_main(["route"])
    assert exc.value.code == 2
    assert "usage: kicad-tools route" in capsys.readouterr().err


def test_unknown_route_flag_is_rejected(capsys):
    with pytest.raises(SystemExit) as exc:
        kct_main(["route", "x.kicad_pcb", "--no-such-flag"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --no-such-flag" in capsys.readouterr().err


def test_route_is_still_listed_in_top_level_help(capsys):
    with pytest.raises(SystemExit):
        kct_main(["--help"])
    out = capsys.readouterr().out
    assert "route" in out
    assert "Autoroute a PCB" in out


# ---------------------------------------------------------------------------
# Startup cost: route_cmd stays off the import path until route is dispatched
# ---------------------------------------------------------------------------


def test_top_level_help_and_create_parser_do_not_import_route_cmd():
    code = (
        "import sys, contextlib, io\n"
        "from kicad_tools.cli.parser import create_parser\n"
        "create_parser()\n"
        "assert 'kicad_tools.cli.route_cmd' not in sys.modules, 'create_parser'\n"
        "from kicad_tools.cli import main\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    try:\n"
        "        main(['--help'])\n"
        "    except SystemExit:\n"
        "        pass\n"
        "assert 'kicad_tools.cli.route_cmd' not in sys.modules, 'kct --help'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


# ---------------------------------------------------------------------------
# kct build's route subprocess argv is still accepted
# ---------------------------------------------------------------------------


def test_build_style_route_argv_is_accepted_by_the_real_parser():
    """``build_cmd`` runs ``python -m kicad_tools.cli route <pcb> ...``."""
    from kicad_tools.cli.route_cmd import _route_parser

    argv = [
        "x.kicad_pcb",
        "-o",
        "x_routed.kicad_pcb",
        "--manufacturer",
        "jlcpcb",
        "--clearance",
        "0.15",
        "--quiet",
    ]
    assert _forwarded(["route", *argv]) == argv
    args = _route_parser().parse_args(argv)
    assert args.clearance == pytest.approx(0.15)
