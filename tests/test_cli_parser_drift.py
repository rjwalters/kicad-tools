"""Argparse-drift regression test (Issue #2817).

This test prevents a recurring class of bug where a new flag is added to
the *inner* ``route_cmd.py`` parser without being added to the *outer*
``parser.py`` parser and to the forwarding shim in
``commands/routing.py``.  Three prior instances of this exact pattern
have shipped to ``main`` and required a follow-up bugfix:

* **#2620** -- ``--placement-feedback-outer-timeout`` declared outer,
  rejected inner (drift in the opposite direction).
* **#2622 / #2793** -- manufacturer-registry argparse drift.
* **#2812 / #2817** -- ``--checkpoint-interval`` declared inner only;
  ``kct route --checkpoint-interval 30`` rejected with
  ``error: unrecognized arguments``.

The drift is invisible to the type checker and to most unit tests
because both parsers are constructed independently with no shared
schema.  This test introspects both parsers and asserts that every
``--flag`` accepted by the inner parser is also accepted by the outer
parser, except for an explicit allowlist of historically inner-only
flags.

If you legitimately need to add a new inner-only flag, add it to
``INNER_ONLY_ALLOWLIST`` below with a comment explaining why it does
not belong on the outer parser.  In most cases the right answer is to
add it to BOTH parsers and to the forwarding shim -- see the
``--per-net-timeout`` block in ``commands/routing.py`` for the model
pattern.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

# Flags that legitimately exist ONLY on the inner ``route_cmd.py`` parser.
#
# Each entry should have a short justification.  When adding a new
# inner-only flag, prefer to add it to BOTH parsers (and the forwarding
# shim) instead -- the allowlist is for flags that are intentionally
# internal-only (debug/diagnostic toggles, dev-only profiling switches,
# experimental features that should not be advertised through ``kct``).
#
# Snapshot taken 2026-05-12 while fixing #2817.  Many of the entries
# below pre-date the drift test; future flags should generally NOT be
# added here -- expose them through the outer parser instead.
INNER_ONLY_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Diagnostics / debug / profiling -- not part of the
        # user-facing ``kct route`` surface.
        "--analyze",
        "--diagnostics",
        "--profile",
        "--profile-output",
        "--cache-stats",
        "--cache-only",
        "--clear-cache",
        "--preview",
        "--show-congestion",
        "--format",
        # Internal routing-algorithm toggles exposed through the
        # ``route_cmd.py`` parser for development experimentation.
        # Promote to the outer parser when ready to support officially.
        "--auto-pour",
        "--no-auto-pour",
        "--batch-routing",
        "--bus-min-width",
        "--bus-mode",
        "--bus-routing",
        "--bus-spacing",
        "--edge-clearance",
        "--escape-routing",
        "--no-escape-routing",
        "--grid-strategy",
        "--hierarchical",
        "--min-clearance",
        "--multi-resolution",
        "--no-early-stop",
        "--no-perturbation",
        "--perturbation",
        "--progressive-clearance",
        "--relaxation-levels",
        "--stitch-power-planes",
        "--two-phase",
        "--two-phase-iterations",
    }
)


def _flags_from_parser(parser: argparse.ArgumentParser) -> set[str]:
    """Return the set of ``--long-form`` option strings declared on ``parser``.

    Short flags (``-o``, ``-q``, ``-v``) are intentionally excluded
    because the drift bug class only manifests for long flags -- short
    flags are typically aliases and live alongside their long form.
    """
    flags: set[str] = set()
    for action in parser._actions:
        for option_string in action.option_strings:
            if option_string.startswith("--"):
                flags.add(option_string)
    return flags


def _inner_route_parser_flags() -> set[str]:
    """Capture the inner ``route_cmd.main`` parser without parsing argv.

    The parser is constructed inline inside ``main()``; we intercept
    ``ArgumentParser.parse_args`` to grab the live parser instance and
    then exit before any routing work happens.
    """
    from kicad_tools.cli.route_cmd import main as route_main

    captured: dict[str, argparse.ArgumentParser] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def fake_parse_args(self, *args, **kwargs):
        # Only capture the inner ``kicad-tools route`` parser -- some
        # add_argument calls below may also instantiate sub-parsers.
        if getattr(self, "prog", "") == "kicad-tools route":
            captured["parser"] = self
            raise SystemExit(0)
        return real_parse_args(self, *args, **kwargs)

    with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
        with pytest.raises(SystemExit):
            route_main([])

    assert "parser" in captured, "failed to capture inner route parser"
    return _flags_from_parser(captured["parser"])


def _outer_route_parser_flags() -> set[str]:
    """Walk ``create_parser()`` to extract the ``route`` subparser flags."""
    from kicad_tools.cli.parser import create_parser

    main_parser = create_parser()
    for action in main_parser._actions:
        choices = getattr(action, "choices", None)
        if choices and "route" in choices:
            return _flags_from_parser(choices["route"])
    raise AssertionError("could not find 'route' subparser on outer parser")


def test_inner_only_flags_are_in_allowlist():
    """Every flag on the inner parser must also be on the outer parser.

    Exceptions live in ``INNER_ONLY_ALLOWLIST`` with justification.
    This guards against the #2620 / #2622 / #2793 / #2812 / #2817 bug
    class where a new flag is added to one site only.
    """
    inner = _inner_route_parser_flags()
    outer = _outer_route_parser_flags()

    inner_only = inner - outer
    unexpected_inner_only = inner_only - INNER_ONLY_ALLOWLIST

    if unexpected_inner_only:
        flag_list = "\n  ".join(sorted(unexpected_inner_only))
        pytest.fail(
            "Argparse drift detected: the following flags are accepted by "
            "the inner 'route_cmd.py' parser but rejected by the outer "
            "'kct route' parser:\n  "
            f"{flag_list}\n\n"
            "Fix by adding each flag to BOTH:\n"
            "  1. src/kicad_tools/cli/parser.py :: _add_route_parser\n"
            "  2. src/kicad_tools/cli/commands/routing.py :: run_route_command "
            "(forward to sub_argv)\n\n"
            "Model after the --per-net-timeout block.  If the flag is "
            "genuinely internal-only (debug/profiling), add it to "
            "INNER_ONLY_ALLOWLIST in tests/test_cli_parser_drift.py with "
            "justification."
        )


def test_allowlist_entries_are_actually_inner_only():
    """Sanity: every flag in the allowlist should genuinely be inner-only.

    Prevents the allowlist from growing stale -- if a flag is later
    added to the outer parser it should be removed from the allowlist
    so we don't accidentally mask a future drift bug for that flag.
    """
    inner = _inner_route_parser_flags()
    outer = _outer_route_parser_flags()

    stale: set[str] = set()
    for flag in INNER_ONLY_ALLOWLIST:
        if flag not in inner:
            stale.add(f"{flag} (not on inner parser at all)")
        elif flag in outer:
            stale.add(f"{flag} (now on outer parser -- remove from allowlist)")

    if stale:
        entries = "\n  ".join(sorted(stale))
        pytest.fail(
            "Stale entries in INNER_ONLY_ALLOWLIST:\n  "
            f"{entries}\n\n"
            "Remove these entries from tests/test_cli_parser_drift.py."
        )


def test_checkpoint_interval_is_on_both_parsers():
    """Direct regression test for #2817.

    ``--checkpoint-interval`` was added to the inner parser by #2812 but
    not to the outer parser, so ``kct route --checkpoint-interval 30``
    failed with ``error: unrecognized arguments``.  This test pins both
    parsers to ensure the flag never goes missing again.
    """
    inner = _inner_route_parser_flags()
    outer = _outer_route_parser_flags()

    assert "--checkpoint-interval" in inner, (
        "--checkpoint-interval is missing from the inner route_cmd.py parser "
        "(this would regress #2812)"
    )
    assert "--checkpoint-interval" in outer, (
        "--checkpoint-interval is missing from the outer parser.py route "
        "subparser (this would regress #2817)"
    )
