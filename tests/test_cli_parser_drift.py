"""Argparse-drift regression test (Issues #2817, #2819).

This test prevents a recurring class of bug where a flag is added to one
of the two ``route`` parsers without being added to the other (and to
the forwarding shim in ``commands/routing.py``).  Five prior instances
of this exact pattern have shipped to ``main`` and required a follow-up
bugfix:

* **#2620** -- ``--placement-feedback-outer-timeout`` declared outer,
  rejected inner (drift in the opposite direction).
* **#2622 / #2793** -- manufacturer-registry argparse drift.
* **#2812 / #2817** -- ``--checkpoint-interval`` declared inner only;
  ``kct route --checkpoint-interval 30`` rejected with
  ``error: unrecognized arguments``.
* **#2819** -- ``--max-search-iterations`` declared outer only; the
  shim dropped the flag and the inner parser never saw it, so
  ``kct route --max-search-iterations 50000`` parsed cleanly but ran
  the C++ A* with ``max_search_iterations=0`` (the historical
  ``cols*rows*4`` heuristic) regardless of the user-supplied value.

The drift is invisible to the type checker and to most unit tests
because both parsers are constructed independently with no shared
schema.  This test introspects both parsers and asserts symmetric
containment:

* ``inner_flags - outer_flags ⊆ INNER_ONLY_ALLOWLIST`` (inner-only flags
  must be explicitly allowlisted -- guards the #2812/#2817 direction).
* ``outer_flags - inner_flags ⊆ OUTER_ONLY_ALLOWLIST`` (outer-only flags
  must be explicitly allowlisted -- guards the #2819 direction, where
  the shim consumes the flag entirely or the forwarding block is
  missing).

If you legitimately need to add a new inner-only or outer-only flag,
add it to the corresponding allowlist below with a comment explaining
why the flag does not belong on the other parser.  In most cases the
right answer is to add it to BOTH parsers and to the forwarding shim --
see the ``--per-net-timeout`` block in ``commands/routing.py`` for the
model pattern.
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
# Snapshot taken 2026-05-12 while fixing #2817 and the symmetric
# outer-only check added by #2819.  Many of the entries below pre-date
# the drift test; future flags should generally NOT be added here --
# expose them through the outer parser instead.
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

# Flags that legitimately exist ONLY on the outer ``parser.py`` route
# subparser.  These are flags the shim consumes entirely before invoking
# the inner ``route_cmd.main`` (e.g. they alter how ``sub_argv`` is
# built rather than being forwarded verbatim), or flags whose outer
# spelling differs from any inner equivalent.
#
# Snapshot taken 2026-05-12 while fixing #2819.  After the #2819 fix,
# ``--max-search-iterations`` lives on BOTH parsers, so the outer-only
# set is empty -- every outer flag either has a matching inner flag or
# would be a drift bug.  Future outer-only entries belong here only if
# the flag is genuinely consumed by the shim and never forwarded.
OUTER_ONLY_ALLOWLIST: frozenset[str] = frozenset(set())


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


def test_outer_only_flags_are_in_allowlist():
    """Every flag on the outer parser must also be on the inner parser.

    Exceptions live in ``OUTER_ONLY_ALLOWLIST`` with justification.
    This guards against the #2819 direction of the drift bug class: a
    flag declared on the outer parser but never forwarded by the shim
    (and therefore never declared on the inner parser).  In that
    scenario ``kct route --flag VALUE`` parses cleanly but the inner
    command never sees the override and the value is silently
    discarded.
    """
    inner = _inner_route_parser_flags()
    outer = _outer_route_parser_flags()

    outer_only = outer - inner
    unexpected_outer_only = outer_only - OUTER_ONLY_ALLOWLIST

    if unexpected_outer_only:
        flag_list = "\n  ".join(sorted(unexpected_outer_only))
        pytest.fail(
            "Argparse drift detected: the following flags are accepted by "
            "the outer 'kct route' parser but rejected by the inner "
            "'route_cmd.py' parser:\n  "
            f"{flag_list}\n\n"
            "These flags will be silently dropped by the forwarding shim, "
            "so any user-supplied value is ignored.  Fix by:\n"
            "  1. src/kicad_tools/cli/route_cmd.py :: main "
            "(add matching add_argument)\n"
            "  2. src/kicad_tools/cli/commands/routing.py :: run_route_command "
            "(forward to sub_argv)\n\n"
            "Model after the --per-net-timeout block.  If the flag is "
            "genuinely consumed by the shim and intentionally never "
            "forwarded, add it to OUTER_ONLY_ALLOWLIST in "
            "tests/test_cli_parser_drift.py with justification."
        )


def test_allowlist_entries_are_actually_outer_only():
    """Sanity: every flag in the allowlist should genuinely be outer-only.

    Prevents the allowlist from growing stale -- if a flag is later
    added to the inner parser it should be removed from the allowlist
    so we don't accidentally mask a future drift bug for that flag.
    """
    inner = _inner_route_parser_flags()
    outer = _outer_route_parser_flags()

    stale: set[str] = set()
    for flag in OUTER_ONLY_ALLOWLIST:
        if flag not in outer:
            stale.add(f"{flag} (not on outer parser at all)")
        elif flag in inner:
            stale.add(f"{flag} (now on inner parser -- remove from allowlist)")

    if stale:
        entries = "\n  ".join(sorted(stale))
        pytest.fail(
            "Stale entries in OUTER_ONLY_ALLOWLIST:\n  "
            f"{entries}\n\n"
            "Remove these entries from tests/test_cli_parser_drift.py."
        )


def test_max_search_iterations_is_on_both_parsers():
    """Direct regression test for #2819.

    ``--max-search-iterations`` was added to the outer parser by #2610
    but the shim did not forward it and the inner parser never declared
    it, so ``kct route --max-search-iterations N`` parsed cleanly and
    the override was silently dropped (inner saw ``0`` via the defensive
    ``getattr(args, "max_search_iterations", 0)``).  This test pins
    both parsers to ensure the flag never goes missing again.
    """
    inner = _inner_route_parser_flags()
    outer = _outer_route_parser_flags()

    assert "--max-search-iterations" in inner, (
        "--max-search-iterations is missing from the inner route_cmd.py "
        "parser (this would regress #2819)"
    )
    assert "--max-search-iterations" in outer, (
        "--max-search-iterations is missing from the outer parser.py route "
        "subparser (this would regress #2610)"
    )


def test_strict_in_pad_clearance_is_on_both_parsers_and_stamps_env():
    """Direct regression test for #3033 / #3062.

    ``--strict-in-pad-clearance`` is declared on BOTH the outer and
    inner parsers and forwarded through the shim verbatim.  When set
    on the inner parser, ``route_cmd.main`` stamps the
    ``KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1`` env var so the
    lazily-constructed ``EscapeRouter`` reads the opt-in without each
    intermediate call site needing an explicit pass-through.

    This test pins three invariants so the flag never drifts again:

    1. The flag appears on the inner ``route_cmd.py`` parser.
    2. The flag appears on the outer ``parser.py`` route subparser.
    3. Running the inner parser with the flag stamps the env var to
       ``"1"`` and the absence of the flag leaves the env var unset.
    """
    inner = _inner_route_parser_flags()
    outer = _outer_route_parser_flags()

    assert "--strict-in-pad-clearance" in inner, (
        "--strict-in-pad-clearance is missing from the inner route_cmd.py "
        "parser (this would regress #3033/#3062 -- the flag must be present "
        "on the inner parser because that is where the env-var stamp "
        "happens)"
    )
    assert "--strict-in-pad-clearance" in outer, (
        "--strict-in-pad-clearance is missing from the outer parser.py "
        "route subparser (this would regress #3033/#3062 -- the outer flag "
        "is the user-facing 'kct route --strict-in-pad-clearance' surface)"
    )

    # Verify the inner parser stamps the env var when the flag is set.
    # We intercept just before any routing work happens by mocking out
    # the heavyweight downstream functions; the parse-args + env-stamp
    # block runs first so we can observe it.
    import os
    from unittest.mock import patch

    # Stage 1: flag set -> env var becomes "1".
    saved = os.environ.pop("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE", None)
    try:
        # We invoke the inner parser via the shim's sub_argv construction
        # rather than executing the full routing pipeline.  Mock the
        # inner main's exit point so we don't actually route.
        from kicad_tools.cli import route_cmd

        # Patch the function that runs after env-stamping but well
        # before any real work.  ``_set_wall_clock_deadline`` is the
        # very next line after the env-stamp block.
        with patch.object(
            route_cmd,
            "_set_wall_clock_deadline",
            side_effect=SystemExit(0),
        ):
            with pytest.raises(SystemExit):
                route_cmd.main(["dummy.kicad_pcb", "--strict-in-pad-clearance"])
        assert os.environ.get("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE") == "1", (
            "Inner route_cmd.main must stamp "
            "KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1 when "
            "--strict-in-pad-clearance is passed; got "
            f"{os.environ.get('KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE')!r}"
        )

        # Stage 2: flag absent -> env var stays unset (we cleared it
        # above; running without the flag should NOT set it).
        del os.environ["KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE"]
        with patch.object(
            route_cmd,
            "_set_wall_clock_deadline",
            side_effect=SystemExit(0),
        ):
            with pytest.raises(SystemExit):
                route_cmd.main(["dummy.kicad_pcb"])
        assert "KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE" not in os.environ, (
            "Inner route_cmd.main must NOT stamp the env var when the "
            "flag is absent; legacy bit-for-bit behaviour requires the "
            "env var be cleared in this code path"
        )
    finally:
        # Restore env to original state.
        if saved is None:
            os.environ.pop("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE", None)
        else:
            os.environ["KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE"] = saved
