"""Parity test between the outer (top-level ``kct build``) and inner
(``python -m kicad_tools.cli.build_cmd``) argument parsers.

Background
----------

There are two argument parsers for the ``build`` command:

* **Inner / authoritative** — built by
  ``kicad_tools.cli.build_cmd._build_inner_parser``.  Used when the module is
  invoked directly via ``python -m kicad_tools.cli.build_cmd`` or via the
  dispatcher in ``kicad_tools.cli.commands.build.run_build_command``.

* **Outer** — built inside ``kicad_tools.cli.parser.create_parser`` via
  ``_add_build_parser``.  Backs ``kct build`` on the top-level CLI.

These two parsers historically drift (see issue #2888).  This test fails
loudly when the inner parser adds a long option string or a ``--step``
choice that has not been mirrored to the outer parser.

Maintenance contract
--------------------

If you add an option to the inner parser, you MUST also:

1. Add the same long option string to the outer parser in
   ``_add_build_parser`` (with a ``build_*`` ``dest``).
2. Forward it from ``run_build_command`` into ``sub_argv`` so the inner
   parser actually sees it.

If you intentionally want an inner-only option (rare — usually the answer
is to surface it on the outer CLI too), add it to ``INTENTIONAL_INNER_ONLY``
below with an inline comment explaining why.
"""

from __future__ import annotations

import argparse

import pytest

from kicad_tools.cli.build_cmd import _build_inner_parser
from kicad_tools.cli.parser import create_parser

# ---------------------------------------------------------------------------
# Allowlists for intentional differences.
# ---------------------------------------------------------------------------

# Long option strings present on the inner parser that we INTENTIONALLY do
# not mirror onto the outer parser.  Keep this list short and documented.
INTENTIONAL_INNER_ONLY: frozenset[str] = frozenset(
    {
        # (none currently — issue #2888 wants full parity)
    }
)

# Long option strings present on the outer parser that the inner parser does
# not need to see (typically because the dispatcher consumes them itself or
# the global parser already surfaces them).  Right now there are no outer-
# only options.
INTENTIONAL_OUTER_ONLY: frozenset[str] = frozenset(
    {
        # (none currently)
    }
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _long_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    """Return the set of long option strings (``--foo``) declared on ``parser``."""
    out: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 — argparse offers no public API
        for opt in action.option_strings:
            if opt.startswith("--"):
                out.add(opt)
    # ``--help`` is added automatically by argparse on every parser; it is not
    # interesting for parity purposes.
    out.discard("--help")
    return out


def _get_outer_build_parser() -> argparse.ArgumentParser:
    """Locate the ``build`` subparser within the top-level CLI parser."""
    top = create_parser()
    for action in top._actions:  # noqa: SLF001 — argparse offers no public API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            if "build" in action.choices:
                return action.choices["build"]
    raise AssertionError(
        "Top-level parser does not expose a 'build' subcommand; did "
        "_add_build_parser get removed from create_parser()?"
    )


def _step_action(parser: argparse.ArgumentParser) -> argparse.Action:
    """Return the ``--step`` action from ``parser``."""
    for action in parser._actions:  # noqa: SLF001
        if "--step" in action.option_strings:
            return action
    raise AssertionError("Parser is missing the required --step option")


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_inner_parser_options_are_mirrored_on_outer_parser() -> None:
    """Every inner long option (modulo the allowlist) must exist on outer."""
    inner = _build_inner_parser()
    outer = _get_outer_build_parser()

    inner_opts = _long_option_strings(inner)
    outer_opts = _long_option_strings(outer)

    missing_on_outer = (inner_opts - outer_opts) - INTENTIONAL_INNER_ONLY
    assert not missing_on_outer, (
        "The following long options are declared on the inner build parser "
        f"({sorted(missing_on_outer)}) but not on the outer (top-level "
        "kct build) parser.  Update src/kicad_tools/cli/parser.py::"
        "_add_build_parser to mirror them, then add forwarding in "
        "src/kicad_tools/cli/commands/build.py::run_build_command."
    )


def test_outer_parser_does_not_advertise_unknown_options() -> None:
    """Every outer long option (modulo the allowlist) must exist on inner."""
    inner = _build_inner_parser()
    outer = _get_outer_build_parser()

    inner_opts = _long_option_strings(inner)
    outer_opts = _long_option_strings(outer)

    surplus_on_outer = (outer_opts - inner_opts) - INTENTIONAL_OUTER_ONLY
    assert not surplus_on_outer, (
        "The following long options are declared on the outer parser "
        f"({sorted(surplus_on_outer)}) but not understood by the inner "
        "build_cmd parser.  Either remove them from _add_build_parser or "
        "add them to _build_inner_parser."
    )


def test_step_choice_parity() -> None:
    """``--step`` choices must match exactly between inner and outer."""
    inner = _build_inner_parser()
    outer = _get_outer_build_parser()

    inner_choices = set(_step_action(inner).choices or ())
    outer_choices = set(_step_action(outer).choices or ())

    missing = inner_choices - outer_choices
    surplus = outer_choices - inner_choices

    assert inner_choices == outer_choices, (
        f"--step choices drift detected. Missing on outer: {sorted(missing)}; "
        f"surplus on outer: {sorted(surplus)}. The outer parser must accept "
        f"the same set as the inner parser."
    )


@pytest.mark.parametrize(
    "step",
    ["schematic", "erc", "pcb", "sync", "preflight-routing", "verify", "export", "all"],
)
def test_outer_parser_accepts_each_step_choice(step: str) -> None:
    """Smoke test that the outer parser parses each documented step choice."""
    top = create_parser()
    args = top.parse_args(["build", "--step", step])
    assert getattr(args, "build_step", None) == step


def test_outer_parser_accepts_new_flags() -> None:
    """Outer parser must accept the flags added in issue #2888."""
    top = create_parser()
    args = top.parse_args(
        [
            "build",
            "--allow-incomplete",
            "--optimize-placement",
            "--no-smoke-check",
            "-o",
            "/tmp/out",
            "--quiet",
        ]
    )
    assert getattr(args, "build_allow_incomplete", False) is True
    assert getattr(args, "build_optimize_placement", False) is True
    assert getattr(args, "build_no_smoke_check", False) is True
    assert getattr(args, "build_output", None) == "/tmp/out"
    assert getattr(args, "build_quiet", False) is True


def test_dispatcher_forwards_new_flags() -> None:
    """``run_build_command`` must forward each new flag into ``sub_argv``."""
    from types import SimpleNamespace

    from kicad_tools.cli.commands import build as build_cmd_dispatch

    captured: dict[str, list[str] | None] = {"argv": None}

    def _fake_build_main(argv: list[str] | None) -> int:
        captured["argv"] = list(argv) if argv is not None else None
        return 0

    # The dispatcher imports the inner main lazily inside the function body
    # (``from ..build_cmd import main as build_main``).  Patch the module-
    # level attribute so the import resolves to our fake.
    import kicad_tools.cli.build_cmd as inner_module

    orig_main = inner_module.main
    inner_module.main = _fake_build_main  # type: ignore[assignment]
    try:
        args = SimpleNamespace(
            build_spec="boards/x/project.kct",
            build_step="preflight-routing",
            build_mfr="jlcpcb",
            build_dry_run=True,
            build_verbose=False,
            build_quiet=True,
            global_quiet=False,
            build_force=False,
            build_output="/tmp/out",
            build_optimize_placement=True,
            build_no_smoke_check=True,
            build_allow_incomplete=True,
        )
        rc = build_cmd_dispatch.run_build_command(args)
    finally:
        inner_module.main = orig_main  # type: ignore[assignment]

    assert rc == 0
    argv = captured["argv"]
    assert argv is not None
    assert "boards/x/project.kct" in argv
    assert argv[1:3] == ["--step", "preflight-routing"]
    assert "--dry-run" in argv
    assert "--quiet" in argv
    assert "--output" in argv and "/tmp/out" in argv
    assert "--optimize-placement" in argv
    assert "--no-smoke-check" in argv
    assert "--allow-incomplete" in argv
