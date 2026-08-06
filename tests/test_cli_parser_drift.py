"""Argparse-drift regression test (Issues #2817, #2819, #4633).

This module guards **two** three-hop parser pairs against the same bug
class:

* ``route`` -- ``parser.py :: _add_route_parser`` ->
  ``commands/routing.py :: run_route_command`` -> ``route_cmd.main``.
* ``check`` -- ``parser.py :: _add_check_parser`` ->
  ``commands/validation.py :: run_check_command`` -> ``check_cmd.main``
  (added by #4633; see the ``check``-pair section near the bottom).

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

The ``check`` pair went unguarded until #4633, and had accumulated
**three** live instances of the bug class in the meantime -- all three
declared on ``check_cmd.py`` only and rejected by the real ``kct check``
with ``error: unrecognized arguments``:

* ``--refill-zones`` (#4113, ``d42248ee``) -- entirely unreachable
  through the ``kct`` entry point despite being advertised in
  ``CHANGELOG.md`` and ``docs/reference/cli.md``.
* ``--courtyard-waivers`` (#4137/#4150, ``07fd58f1``) and ``--waivers``
  (#4417/#4445, ``59e36c47``) -- degraded partially rather than loudly,
  because both sidecars are auto-discovered next to the board, so only
  the explicit-path override was unreachable.

#4633 added all three to the outer parser and the shim, so both
``check`` allowlists below start out empty.
"""

from __future__ import annotations

import argparse
from pathlib import Path
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


def test_strict_in_pad_clearance_is_on_both_parsers_and_stamps_env(tmp_path):
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
    #
    # Issue #4559: ``main()`` now restores ``os.environ`` when it exits
    # (any path, including this SystemExit interception), so the stamp
    # must be observed MID-RUN -- inside the interception -- rather than
    # after ``main()`` returns.  The mid-run view is the one that matters
    # for #3033/#3062 anyway: it is what the lazily-constructed
    # EscapeRouter sees.  The post-return view is now pinned to "no leak".
    import os
    from unittest.mock import patch

    # route_cmd.main validates the PCB path exists before it reaches
    # _set_wall_clock_deadline, so give it a real (minimal) file
    # (stale-test fix, issue #3436 burn-down).
    dummy_pcb = tmp_path / "dummy.kicad_pcb"
    dummy_pcb.write_text("(kicad_pcb)")

    observed: dict[str, str | None] = {}

    def _capture_and_exit(*args, **kwargs):
        observed["mid_run"] = os.environ.get("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE")
        raise SystemExit(0)

    # Stage 1: flag set -> env var is "1" during the run.
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
            side_effect=_capture_and_exit,
        ):
            with pytest.raises(SystemExit):
                route_cmd.main([str(dummy_pcb), "--strict-in-pad-clearance"])
        assert observed["mid_run"] == "1", (
            "Inner route_cmd.main must stamp "
            "KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1 (visible mid-run to the "
            "lazily-constructed EscapeRouter) when --strict-in-pad-clearance "
            f"is passed; got {observed['mid_run']!r}"
        )
        assert "KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE" not in os.environ, (
            "route_cmd.main must restore os.environ on exit (issue #4559); "
            "the strict-in-pad stamp leaked out of main()"
        )

        # Stage 2: flag absent -> env var stays unset during the run too.
        observed.clear()
        with patch.object(
            route_cmd,
            "_set_wall_clock_deadline",
            side_effect=_capture_and_exit,
        ):
            with pytest.raises(SystemExit):
                route_cmd.main([str(dummy_pcb)])
        assert observed["mid_run"] is None, (
            "Inner route_cmd.main must NOT stamp the env var when the "
            "flag is absent; legacy bit-for-bit behaviour requires the "
            "env var be unset in this code path"
        )
        assert "KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE" not in os.environ
    finally:
        # Restore env to original state.
        if saved is None:
            os.environ.pop("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE", None)
        else:
            os.environ["KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE"] = saved


# ---------------------------------------------------------------------------
# Manufacturer-flag consistency across DRC-family subcommands (#3159)
# ---------------------------------------------------------------------------
#
# ``--mfr`` selects the manufacturer profile that supplies clearance /
# design-rule targets.  Every subcommand that derives rules from a
# manufacturer profile must accept it so users do not have to learn a
# different (or missing) flag per subcommand.
#
# ``fix-drc`` was the drift case (#3159): it silently pinned its
# internally-generated DRC report to ``jlcpcb`` because it had no
# ``--mfr`` flag at all, while ``check`` / ``audit`` / ``route`` / ``drc`` /
# ``repair-clearance`` all declare it.
#
# NOTE: the *spelling* and ``choices`` source are intentionally not
# unified here -- ``check``/``audit`` use ``get_manufacturer_ids()`` while
# ``route``/``drc``/``fix-drc`` use ``get_all_manufacturer_names()``.  This
# test only pins that ``--mfr`` is *present* on each; unifying the
# parent-parser is deferred to a separate issue.
MFR_SUBCOMMANDS = ("check", "audit", "route", "drc", "fix-drc", "repair-clearance")


def _outer_subparser_flags(name: str) -> set[str]:
    """Return the ``--long-form`` flags declared on the named outer subparser."""
    from kicad_tools.cli.parser import create_parser

    main_parser = create_parser()
    for action in main_parser._actions:
        choices = getattr(action, "choices", None)
        if choices and name in choices:
            return _flags_from_parser(choices[name])
    raise AssertionError(f"could not find {name!r} subparser on outer parser")


@pytest.mark.parametrize("subcommand", MFR_SUBCOMMANDS)
def test_mfr_flag_is_consistent_across_subcommands(subcommand):
    """Every manufacturer-relevant subcommand must accept ``--mfr``.

    Catches future drift of the kind that left ``fix-drc`` without the
    flag (#3159).
    """
    flags = _outer_subparser_flags(subcommand)
    assert "--mfr" in flags, (
        f"kct {subcommand} is missing --mfr; manufacturer-relevant "
        f"subcommands must accept --mfr consistently (regresses #3159)."
    )


def test_fix_drc_mfr_on_inner_and_outer_parser():
    """Direct regression test for #3159.

    ``fix-drc`` had no ``--mfr`` flag on either parser, so
    ``kct fix-drc --mfr jlcpcb-tier1 board.kicad_pcb`` failed with
    ``error: unrecognized arguments``.  Pin both parsers so the flag
    never goes missing again.
    """
    outer = _outer_subparser_flags("fix-drc")
    assert "--mfr" in outer, (
        "--mfr is missing from the outer parser.py fix-drc subparser (this would regress #3159)"
    )

    # Inner parser: the flag must parse without an "unrecognized
    # arguments" error.  We parse a minimal argv and assert the value
    # lands on the namespace.
    from kicad_tools.cli.fix_drc_cmd import main as fix_drc_main

    captured: dict[str, object] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def fake_parse_args(self, *args, **kwargs):
        ns = real_parse_args(self, *args, **kwargs)
        if getattr(self, "prog", "") == "kicad-tools fix-drc":
            captured["ns"] = ns
            raise SystemExit(0)
        return ns

    with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
        with pytest.raises(SystemExit):
            fix_drc_main(["board.kicad_pcb", "--mfr", "jlcpcb"])

    assert "ns" in captured, "failed to capture inner fix-drc namespace"
    assert getattr(captured["ns"], "mfr", None) == "jlcpcb", (
        "inner fix-drc parser must accept --mfr and store it on the namespace (regresses #3159)"
    )


def test_fix_drc_threads_manufacturer_into_drc_checker():
    """``--mfr`` must flow into the self-generated DRC report.

    In the no-``--drc-report`` path, ``fix-drc`` builds its own report via
    ``_run_python_drc`` -> ``DRCChecker(pcb, manufacturer=...)``.  Before
    #3159 the manufacturer was never passed, so clearance targets were
    silently pinned to jlcpcb.  Assert the selected profile reaches the
    checker.
    """
    from unittest.mock import MagicMock

    from kicad_tools.cli import fix_drc_cmd

    fake_checker = MagicMock()
    fake_checker.check_all.return_value = []

    with (
        patch("kicad_tools.validate.checker.DRCChecker", return_value=fake_checker) as mk,
        patch("kicad_tools.schema.pcb.PCB.load", return_value=MagicMock()),
        patch("kicad_tools.drc.compat.drc_results_to_report", return_value=MagicMock()),
    ):
        fix_drc_cmd._run_python_drc(Path("board.kicad_pcb"), manufacturer="oshpark", layers=4)

    assert mk.call_count == 1, "_run_python_drc should construct exactly one DRCChecker"
    _, kwargs = mk.call_args
    assert kwargs.get("manufacturer") == "oshpark", (
        "_run_python_drc must thread --mfr into DRCChecker(manufacturer=...) (regresses #3159)"
    )
    assert kwargs.get("layers") == 4, (
        "_run_python_drc must thread --layers into DRCChecker(layers=...)"
    )


# ---------------------------------------------------------------------------
# ``check`` parser pair (#4633)
# ---------------------------------------------------------------------------
#
# ``kct check`` has the identical three-hop forwarding shape as ``route``:
#
#     parser.py :: _add_check_parser
#         -> commands/validation.py :: run_check_command
#             -> check_cmd.py :: main
#
# and went unguarded until #4633, by which point three flags had drifted
# (``--refill-zones``, ``--courtyard-waivers``, ``--waivers`` -- see the
# module docstring).  The allowlists below are DELIBERATELY EMPTY.

# Flags that legitimately exist ONLY on the inner ``check_cmd.py`` parser.
#
# Empty by construction: as of #4633 every inner ``check`` flag is also
# declared on the outer ``kct check`` subparser and forwarded by the shim.
# A future entry belongs here only if the flag is genuinely internal-only
# (a debug/profiling/experimental toggle that must NOT be advertised on the
# ``kct`` surface), and must carry a one-line justification -- exactly like
# the route ``INNER_ONLY_ALLOWLIST`` entries above.  A user-facing,
# documented flag is a DRIFT BUG: add it to _add_check_parser and
# run_check_command instead of allowlisting it.
INNER_ONLY_CHECK_ALLOWLIST: frozenset[str] = frozenset(set())

# Flags that legitimately exist ONLY on the outer ``kct check`` subparser.
#
# Empty by construction, mirroring the route ``OUTER_ONLY_ALLOWLIST``.  A
# future entry belongs here only if the shim consumes the flag entirely
# before building ``sub_argv`` (so it is never forwarded and the inner
# parser has no equivalent), and must carry a one-line justification.
# Otherwise an outer-only flag parses cleanly and is then silently dropped
# -- the #2819 failure mode, applied to ``check``.
OUTER_ONLY_CHECK_ALLOWLIST: frozenset[str] = frozenset(set())


def _inner_check_parser_flags(prog: str = "kct check") -> set[str]:
    """Capture the inner ``check_cmd.main`` parser without parsing argv.

    ``prog`` is parameterised ONLY so the vacuity control below can prove
    that a wrong prog string fails loudly rather than silently capturing
    nothing.  Production callers must use the default.

    .. warning::

       The inner check parser's ``prog`` is ``"kct check"``, NOT
       ``"kicad-tools check"`` (``check_cmd.py`` ``main()``), whereas the
       inner *route* parser uses ``"kicad-tools route"``.  Copy-pasting
       ``_inner_route_parser_flags`` and its prog filter here captures
       nothing.  The hard ``assert`` below is what turns that mistake into
       a loud failure instead of a permanently-green, vacuous test.
    """
    from kicad_tools.cli.check_cmd import main as check_main

    captured: dict[str, argparse.ArgumentParser] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def fake_parse_args(self, *args, **kwargs):
        if getattr(self, "prog", "") == prog:
            captured["parser"] = self
            raise SystemExit(0)
        return real_parse_args(self, *args, **kwargs)

    with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
        with pytest.raises(SystemExit):
            check_main([])

    assert "parser" in captured, (
        f"failed to capture inner check parser with prog={prog!r} -- "
        "check_cmd.main's prog string has changed; update "
        "_inner_check_parser_flags rather than letting this test go vacuous"
    )
    return _flags_from_parser(captured["parser"])


def test_check_inner_only_flags_are_in_allowlist():
    """Every flag on the inner check parser must be on the outer one.

    Guards the direction that actually shipped three times (#4113,
    #4137, #4417): a flag declared on ``check_cmd.py`` only, so
    ``kct check --flag`` dies with ``error: unrecognized arguments``.
    """
    inner = _inner_check_parser_flags()
    outer = _outer_subparser_flags("check")

    unexpected_inner_only = (inner - outer) - INNER_ONLY_CHECK_ALLOWLIST

    if unexpected_inner_only:
        flag_list = "\n  ".join(sorted(unexpected_inner_only))
        pytest.fail(
            "Argparse drift detected: the following flags are accepted by "
            "the inner 'check_cmd.py' parser but rejected by the outer "
            "'kct check' parser:\n  "
            f"{flag_list}\n\n"
            "Fix by adding each flag to BOTH:\n"
            "  1. src/kicad_tools/cli/parser.py :: _add_check_parser\n"
            "  2. src/kicad_tools/cli/commands/validation.py :: "
            "run_check_command (forward to sub_argv)\n\n"
            "Model after the --refill-zones / --waivers blocks added by "
            "#4633.  If the flag is genuinely internal-only "
            "(debug/profiling), add it to INNER_ONLY_CHECK_ALLOWLIST in "
            "tests/test_cli_parser_drift.py with justification."
        )


def test_check_outer_only_flags_are_in_allowlist():
    """Every flag on the outer check parser must be on the inner one.

    Guards the #2819 direction applied to ``check``: a flag the outer
    parser accepts but the shim never forwards, so the user-supplied
    value is silently discarded.
    """
    inner = _inner_check_parser_flags()
    outer = _outer_subparser_flags("check")

    unexpected_outer_only = (outer - inner) - OUTER_ONLY_CHECK_ALLOWLIST

    if unexpected_outer_only:
        flag_list = "\n  ".join(sorted(unexpected_outer_only))
        pytest.fail(
            "Argparse drift detected: the following flags are accepted by "
            "the outer 'kct check' parser but rejected by the inner "
            "'check_cmd.py' parser:\n  "
            f"{flag_list}\n\n"
            "These flags will be silently dropped by the forwarding shim, "
            "so any user-supplied value is ignored.  Fix by:\n"
            "  1. src/kicad_tools/cli/check_cmd.py :: main "
            "(add matching add_argument)\n"
            "  2. src/kicad_tools/cli/commands/validation.py :: "
            "run_check_command (forward to sub_argv)\n\n"
            "If the flag is genuinely consumed by the shim and "
            "intentionally never forwarded, add it to "
            "OUTER_ONLY_CHECK_ALLOWLIST with justification."
        )


def test_check_allowlist_entries_are_actually_inner_only():
    """Sanity: ``INNER_ONLY_CHECK_ALLOWLIST`` must not go stale.

    Mirrors ``test_allowlist_entries_are_actually_inner_only`` for the
    check pair, so an allowlisted flag that later gains an outer
    declaration stops masking future drift for that flag.
    """
    inner = _inner_check_parser_flags()
    outer = _outer_subparser_flags("check")

    stale: set[str] = set()
    for flag in INNER_ONLY_CHECK_ALLOWLIST:
        if flag not in inner:
            stale.add(f"{flag} (not on inner parser at all)")
        elif flag in outer:
            stale.add(f"{flag} (now on outer parser -- remove from allowlist)")

    if stale:
        entries = "\n  ".join(sorted(stale))
        pytest.fail(
            "Stale entries in INNER_ONLY_CHECK_ALLOWLIST:\n  "
            f"{entries}\n\n"
            "Remove these entries from tests/test_cli_parser_drift.py."
        )


def test_check_allowlist_entries_are_actually_outer_only():
    """Sanity: ``OUTER_ONLY_CHECK_ALLOWLIST`` must not go stale."""
    inner = _inner_check_parser_flags()
    outer = _outer_subparser_flags("check")

    stale: set[str] = set()
    for flag in OUTER_ONLY_CHECK_ALLOWLIST:
        if flag not in outer:
            stale.add(f"{flag} (not on outer parser at all)")
        elif flag in inner:
            stale.add(f"{flag} (now on inner parser -- remove from allowlist)")

    if stale:
        entries = "\n  ".join(sorted(stale))
        pytest.fail(
            "Stale entries in OUTER_ONLY_CHECK_ALLOWLIST:\n  "
            f"{entries}\n\n"
            "Remove these entries from tests/test_cli_parser_drift.py."
        )


def test_inner_check_parser_capture_is_not_vacuous():
    """Negative control for the ``prog``-string trap (#4633).

    The inner check parser uses ``prog="kct check"`` while the outer
    subparser inherits ``"kicad-tools check"``.  A filter on the wrong
    prog would capture nothing; depending on how the helper is written
    that either fails loudly or degrades into comparing a set against
    itself (a permanently-green test).  This pins the loud behaviour:

    1. the default capture returns a non-empty flag set, and
    2. a wrong prog string raises ``AssertionError``, it does not
       silently return an empty set.
    """
    inner = _inner_check_parser_flags()
    assert inner, "inner check parser capture returned an empty flag set"
    # A few flags that have been on the inner parser since long before
    # #4633 -- if these vanish, the capture is looking at the wrong parser.
    assert {"--errors-only", "--strict", "--layers"} <= inner, (
        f"inner check capture looks wrong; got {sorted(inner)}"
    )

    with pytest.raises(AssertionError, match="failed to capture inner check parser"):
        _inner_check_parser_flags(prog="kicad-tools check")


def test_no_net_class_map_is_on_both_check_parsers():
    """Direct regression pin for #4601 / PR #4629.

    ``--no-net-class-map`` is the flag whose Judge review surfaced the
    missing ``check`` guard (#4633).  It is correct on both parsers
    today; pin it so it cannot drift.
    """
    inner = _inner_check_parser_flags()
    outer = _outer_subparser_flags("check")

    assert "--no-net-class-map" in inner, (
        "--no-net-class-map is missing from the inner check_cmd.py parser "
        "(this would regress #4601)"
    )
    assert "--no-net-class-map" in outer, (
        "--no-net-class-map is missing from the outer parser.py check "
        "subparser (this would regress #4601)"
    )


@pytest.mark.parametrize(
    "flag",
    ["--refill-zones", "--courtyard-waivers", "--waivers"],
)
def test_drifted_check_flags_are_on_both_parsers(flag):
    """Direct regression pins for the three drift instances fixed by #4633.

    Each was declared on ``check_cmd.py`` only and rejected by the real
    ``kct check`` CLI:  ``--refill-zones`` (#4113) was entirely
    unreachable; ``--courtyard-waivers`` (#4137) and ``--waivers``
    (#4417) lost only their explicit-path override because both sidecars
    are auto-discovered next to the board.
    """
    inner = _inner_check_parser_flags()
    outer = _outer_subparser_flags("check")

    assert flag in inner, f"{flag} is missing from the inner check_cmd.py parser"
    assert flag in outer, (
        f"{flag} is missing from the outer parser.py check subparser "
        "(this would regress #4633 -- `kct check` would reject it with "
        "'unrecognized arguments')"
    )


def test_check_shim_forwards_drifted_flags_to_inner_main():
    """The shim must actually forward the #4633 flags, not just parse them.

    Declaring the flags on the outer parser alone would fix the
    ``unrecognized arguments`` error while still discarding the value --
    the #2819 failure mode.  This drives the real
    ``create_parser() -> run_check_command`` path and asserts the flags
    and their values land in the argv handed to ``check_cmd.main``.
    """
    from kicad_tools.cli.commands.validation import run_check_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(
        [
            "check",
            "board.kicad_pcb",
            "--refill-zones",
            "--courtyard-waivers",
            "/tmp/cw.json",
            "--waivers",
            "/tmp/w.json",
        ]
    )

    with patch("kicad_tools.cli.check_cmd.main", return_value=0) as inner_main:
        assert run_check_command(args) == 0

    assert inner_main.call_count == 1
    sub_argv = inner_main.call_args[0][0]

    assert "--refill-zones" in sub_argv, f"run_check_command dropped --refill-zones; got {sub_argv}"
    for opt, value in (
        ("--courtyard-waivers", "/tmp/cw.json"),
        ("--waivers", "/tmp/w.json"),
    ):
        assert opt in sub_argv, f"run_check_command dropped {opt}; got {sub_argv}"
        assert sub_argv[sub_argv.index(opt) + 1] == value, (
            f"run_check_command forwarded {opt} without its value; got {sub_argv}"
        )


def test_check_shim_omits_drifted_flags_when_unset():
    """Absent flags must not be forwarded (default behaviour preserved).

    Note the value-bearing flags use a truthiness guard, matching the
    existing ``--net-class-map`` block: an empty-string path is treated
    as "not supplied" rather than forwarded as ``--waivers ''``.
    """
    from kicad_tools.cli.commands.validation import run_check_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(["check", "board.kicad_pcb"])
    with patch("kicad_tools.cli.check_cmd.main", return_value=0) as inner_main:
        run_check_command(args)
    sub_argv = inner_main.call_args[0][0]
    for opt in ("--refill-zones", "--courtyard-waivers", "--waivers"):
        assert opt not in sub_argv, f"{opt} forwarded even though it was not supplied"

    args = create_parser().parse_args(["check", "board.kicad_pcb", "--waivers", ""])
    with patch("kicad_tools.cli.check_cmd.main", return_value=0) as inner_main:
        run_check_command(args)
    sub_argv = inner_main.call_args[0][0]
    assert "--waivers" not in sub_argv, (
        "an empty --waivers path should be treated as absent (matching the "
        f"--net-class-map truthiness guard); got {sub_argv}"
    )
