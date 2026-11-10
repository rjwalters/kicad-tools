"""Every ``kct route`` option is forwarded to the chained pass or excluded on purpose.

Issue #8674: ``_chain_complete_argv`` builds the argv of the ``--complete`` pass
that ``kct route`` chains after a grid route.  It forwarded a hand-picked set of
flags, and ``--layers``, ``--voltage-map`` (with its lookup settings),
``--edge-clearance`` and ``--current-paths`` were not in it, so the inner pass
placed copper under different rules than the outer route.

This test reads the option list from the real argparse parser and decides
"forwarded" by calling the real ``_chain_complete_argv`` with each option set,
so nothing here has to be kept in step by hand.  An option added to
``kct route`` later fails this test until it is either forwarded or added to one
of the exclusion groups below, with the reason that group states.
"""

from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path

import pytest

from kicad_tools.cli import route_cmd

OUT = Path("out.kicad_pcb")
STAGED = Path("out.chain.kicad_pcb")

# --- Exclusion groups: option -> not forwarded, for the group's stated reason ---

# With any of these the chain does not run at all (the guard clause at the top
# of ``_chain_complete_pass``), so there is no inner pass to forward to.
CHAIN_DOES_NOT_RUN = {
    "--complete",  # the run is already a completion pass
    "--no-chain-complete",  # the operator's opt-out
    "--nets",  # an explicit route set; --complete chooses its own
    "--skip-nets",
    "--region",
    "--resume",
    "--dry-run",
    "--format",  # the chain is skipped under --format json; text is the default
    "--route-engine",  # the chain follows the grid engine only
}

# The pass has its own value for these, or the outer invocation keeps them.
OWNED_BY_ONE_INVOCATION = {
    "--output",  # the pass writes a staged board that is promoted over -o
    "--complete-report",  # names one report file; the outer run did not ask for the pass's
    "--checkpoint",  # outer-run checkpointing
    "--checkpoint-interval",
    "--timeout",  # the deadline reaches the pass through the environment
    "--lint-gate",  # the gate wraps the outer run, chained pass included
    "--lint-gate-waivers",
    "--lint-gate-strict",
    "--profile",  # profiles the outer invocation, which contains the pass
    "--profile-output",
    "--export-failed-nets",  # the outer run's failed-net list
    "--clear-cache",  # one-shot cache maintenance, done by the outer run
    "--cache-stats",
}

# ``--complete`` picks its own engine, lattice and single-attempt path
# (``_apply_complete_mode_defaults``): the lattice engine with ``--strategy``
# coerced to basic and ``--auto-layers`` off.  These select or tune the paths it
# does not take; the #4280 gate rejects the multi-pass strategies on that engine.
COMPLETE_PICKS_ITS_OWN = {
    "--auto-layers",
    "--no-auto-layers",
    "--grid",
    "--grid-strategy",
    "--max-cells",
    "--allow-unsafe-grid",
    "--strategy",
    "--mc-trials",
    "--trial-router",
    "--pop-size",
    "--generations",
    "--iterations",
    "--early-stop-patience",
    "--two-phase",
    "--two-phase-iterations",
    "--multi-resolution",
    "--hierarchical",
    "--escape-routing",
    "--no-escape-routing",
    "--high-performance",
    "--batch-routing",  # deprecated no-op (#7051)
    "--region-parallel",
    "--partition-rows",
    "--partition-cols",
    "--max-parallel-workers",
    # Escalation ladders: each re-routes the whole board at another layer count,
    # board size, fab tier or rule set.  The pass is one attempt on the board
    # the outer run wrote.
    "--max-layers",
    "--starting-layers",
    "--auto-pcb-size",
    "--packing-overhead",
    "--auto-mfr-tier",
    "--mfr-tier-ladder",
    "--adaptive-rules",
    "--no-early-stop",
    "--min-trace",
    "--min-clearance-floor",
    "--progressive-clearance",
    "--min-clearance",
    "--relaxation-levels",
    # Placement loops move footprints and re-route; they belong to the outer run.
    "--placement-feedback",
    "--no-placement-feedback",
    "--placement-feedback-budget",
    "--placement-feedback-max-movement",
    "--placement-feedback-anchor",
    "--placement-feedback-no-anchor",
    "--placement-feedback-stagnation-patience",
    "--placement-feedback-outer-timeout",
    "--placement-delta-feedback",
    "--no-placement-delta-feedback",
    "--placement-delta-feedback-budget",
    "--placement-delta-feedback-timeout",
}

# Reporting, diagnostics and exit-code policy: none of these places copper.
NOT_COPPER = {
    "--verbose",
    "--preview",
    "--analyze",
    "--diagnostics",
    "--show-congestion",
    "--report-stage-quality",
    "--diagnose-unrouted-budget",
    "--census-advisory",
    "--census-advisory-gate",
    "--census-advisory-gate-pct",
    "--capacity-forecast",
    "--capacity-forecast-json",
    "--sync-check",
    "--no-sync-check",
    "--schematic",
    "--no-auto-build-native",
    "--min-completion",  # exit-code threshold; --complete exits 8 on any open link
    "--strict",  # exit-code policy for the outer run's connectivity verification
    "--strict-drc",  # whether a missing kicad-cli fails the outer run's DRC
}

# Issue #8694: reviewed against the single-attempt tail the pass runs
# (``_run_main_impl``).  Each is excluded for the stated evidence.
COVERED_BY_OUTER_RUN = {
    # Default-valued: the pass already runs at this value, and the opt-out
    # (--no-reserve-plane-layers / --no-auto-pour) IS forwarded.
    "--reserve-plane-layers",
    "--auto-pour",
    # Outer run writes the zones into -o (``zone_sexp``, route_cmd.py power-nets
    # block) before the chain runs; the pass loads them as existing copper and
    # re-adding would duplicate them.
    "--power-nets",
    # _run_post_save_stitching runs inside the outer run, before the chain, so
    # the stitching vias are already in the board the pass loads.
    "--stitch-power-planes",
    # Repairs the outer run's DRC findings in the outer run; the pass is
    # incremental over a board that run already repaired.
    "--auto-fix",
    "--auto-fix-passes",
}

# Governed nets are checked by ``_chain_coupled_routing_refusal``: the chain is
# skipped (not forwarded) when the flag governs an open net.
REFUSES_CHAIN_WHEN_GOVERNED = {
    "--differential-pairs",
    "--analog-nets",
    "--diffpair-spacing",  # only read under --differential-pairs
}

# NOT FORWARDED, AND NOT REVIEWED.  Each of these is read (or may be read) on
# the single-attempt path the pass runs, and can change the copper it places or
# the rules it places copper under; the pass runs with the option's default
# whatever the outer run was given.  Issue #8694 reviewed the clearance/layer,
# pour, post-route and coupled-routing families above; what remains is the
# search/budget/bus flags (--length-match-groups is now forwarded).  Moving an entry out of this
# group means forwarding it AND checking that the lattice completion honours
# it; do not move one to another group to make this test pass.
NOT_FORWARDED_UNREVIEWED = {
    "--via-in-pad-last-resort",
    "--force",
    "--allow-stranded-pour-pads",
    "--native-zone-clearance",
    "--reserve-skipped-plane-vias",
    "--reserve-pour-only-passives",  # the default; its negation IS forwarded
    # Coupled routing and buses.
    "--auto-analog",
    "--diffpair-max-delta",
    "--diffpair-per-pair-timeout",
    "--length-match-diffpairs",
    "--tighten-diffpair-gap",
    "--bus-routing",
    "--bus-mode",
    "--bus-spacing",
    "--bus-min-width",
    # Search behaviour and budget.
    "--preserve-existing",  # --complete implies it
    "--oracle-rounds",
    "--lattice-iterations",
    "--lattice-optimize",
    "--search-timeout",
    "--per-net-timeout",
    "--max-search-iterations",
    "--max-ripups-per-net",
    "--deterministic-rescue",
    "--order-method",
    "--ripup-strategy",
    "--targeted-ripup",
    "--no-rescue-pass",
    "--perturbation",
    "--no-perturbation",
    "--fallback-bidirectional",
    "--no-fallback-bidirectional",
    "--nogood-cache",
    "--nogood-cache-margin-cells",
    "--bundle-river-planner",
    "--monotone-certificate-order",
    "--cross-package-pair-corridor",
    "--slack-corridor-widening",
    "--escape-corridor-reservation",
    "--escape-corridor-assignment",
    "--no-pad-access-invariant",
    "--no-routing-plan",
    "--plan-gate",
    "--no-cache",
    "--cache-only",
    # Post-route passes.
}

EXCLUSION_GROUPS = {
    "CHAIN_DOES_NOT_RUN": CHAIN_DOES_NOT_RUN,
    "OWNED_BY_ONE_INVOCATION": OWNED_BY_ONE_INVOCATION,
    "COMPLETE_PICKS_ITS_OWN": COMPLETE_PICKS_ITS_OWN,
    "NOT_COPPER": NOT_COPPER,
    "COVERED_BY_OUTER_RUN": COVERED_BY_OUTER_RUN,
    "REFUSES_CHAIN_WHEN_GOVERNED": REFUSES_CHAIN_WHEN_GOVERNED,
    "NOT_FORWARDED_UNREVIEWED": NOT_FORWARDED_UNREVIEWED,
}
EXCLUDED = frozenset().union(*EXCLUSION_GROUPS.values())

# The constraints #8674 is about, and the ones the function forwarded before it.
# A regression that stops forwarding one fails here even if somebody also adds
# it to an exclusion group.
MUST_FORWARD = {
    "--length-match-groups",  # Issue #8694
    "--layers",
    "--voltage-map",
    "--creepage-standard",
    "--pollution-degree",
    "--material-group",
    "--hv-threshold",
    "--edge-clearance",
    "--current-paths",
    "--no-current-paths",
    "--manufacturer",
    "--copper",
    "--net-class-map",
    "--clearance",
    "--trace-width",
    "--via-drill",
    "--via-diameter",
    "--complete-exclude-nets",
    "--no-plane-via-reservation",
    "--no-reserve-pour-only-passives",
    # Issue #8694
    "--strict-layers",
    "--strict-pad-clearance",
    "--strict-in-pad-clearance",
    "--micro-via-in-pad-fallback",
    "--micro-via-size",
    "--micro-via-drill",
    "--via-clearance",
    "--fine-pitch-clearance",
    "--no-reserve-plane-layers",
    "--no-auto-pour",
    "--no-optimize",
    "--raw",  # implies --no-optimize, which is what reaches the pass
}


def _long_options(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    return {
        opt: action
        for action in parser._actions
        for opt in action.option_strings
        if opt.startswith("--") and opt != "--help"
    }


def _sample_value(action: argparse.Action) -> list[str]:
    """A value for ``action`` that parses and differs from its default."""
    if action.nargs == 0:
        return []
    if action.choices:
        return [str(next(c for c in action.choices if c != action.default))]
    for candidate in ("7", "0.5", "x"):
        try:
            value = action.type(candidate) if action.type else candidate
        except (ValueError, TypeError, argparse.ArgumentTypeError):
            continue
        if value != action.default:
            return [candidate]
    raise AssertionError(f"no sample value for {action.option_strings}")


def _chain_argv(parser: argparse.ArgumentParser, outer: list[str]) -> list[str]:
    with contextlib.redirect_stderr(io.StringIO()):
        args = parser.parse_args(outer)
    return route_cmd._chain_complete_argv(args, outer, OUT, STAGED)


def _forwarded_options() -> set[str]:
    """Options whose presence on the outer argv changes the inner argv."""
    parser = route_cmd._route_parser()
    baseline = _chain_argv(parser, ["b.kicad_pcb"])
    return {
        opt
        for opt, action in _long_options(parser).items()
        if _chain_argv(parser, ["b.kicad_pcb", opt, *_sample_value(action)]) != baseline
    }


def test_every_route_option_is_forwarded_or_excluded_on_purpose():
    options = set(_long_options(route_cmd._route_parser()))
    unclassified = sorted(options - _forwarded_options() - EXCLUDED)
    assert not unclassified, (
        f"`kct route` option(s) {unclassified} are neither forwarded to the chained "
        "--complete pass by _chain_complete_argv nor listed in an exclusion group in "
        f"{Path(__file__).name}. If the option constrains or places copper, forward it "
        "and check the completion pass honours it; otherwise add it to the group whose "
        "reason applies."
    )


def test_issue_8674_constraints_are_forwarded():
    missing = sorted(MUST_FORWARD - _forwarded_options())
    assert not missing, f"_chain_complete_argv no longer forwards {missing}"
    assert not MUST_FORWARD & EXCLUDED


def test_exclusion_groups_are_current_and_disjoint():
    options = set(_long_options(route_cmd._route_parser()))
    assert not sorted(EXCLUDED - options), "exclusion groups name options that no longer exist"
    both = sorted(EXCLUDED & _forwarded_options())
    assert not both, f"{both} are forwarded now; remove them from the exclusion groups"
    names = list(EXCLUSION_GROUPS)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            overlap = sorted(EXCLUSION_GROUPS[a] & EXCLUSION_GROUPS[b])
            assert not overlap, f"{overlap} listed in both {a} and {b}"


@pytest.mark.parametrize("option", sorted(CHAIN_DOES_NOT_RUN - {"--format", "--route-engine"}))
def test_chain_does_not_run_group_is_true(option, tmp_path, monkeypatch):
    """The group's reason is checked, not asserted: the pass must not start."""
    parser = route_cmd._route_parser()
    out = tmp_path / "out.kicad_pcb"
    out.write_text("negotiated")
    outer = [str(tmp_path / "in.kicad_pcb"), "-o", str(out), option]
    outer += _sample_value(_long_options(parser)[option])
    args = parser.parse_args(outer)
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: ["OPEN"],
    )
    monkeypatch.setattr(route_cmd, "_main_impl", lambda argv: pytest.fail("pass must not run"))
    assert route_cmd._chain_complete_pass(args, outer, 2) == 2


@pytest.mark.parametrize(
    "outer_flag", [["--format", "json"], ["--route-engine", "lattice"], ["--route-engine", "mesh"]]
)
def test_chain_does_not_run_for_json_or_non_grid_engine(outer_flag, tmp_path, monkeypatch):
    parser = route_cmd._route_parser()
    out = tmp_path / "out.kicad_pcb"
    out.write_text("negotiated")
    outer = [str(tmp_path / "in.kicad_pcb"), "-o", str(out), *outer_flag]
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: ["OPEN"],
    )
    monkeypatch.setattr(route_cmd, "_main_impl", lambda argv: pytest.fail("pass must not run"))
    assert route_cmd._chain_complete_pass(parser.parse_args(outer), outer, 2) == 2


def test_guard_detects_a_new_unforwarded_option(monkeypatch):
    """The guard has teeth: an option nobody classified fails the first test."""
    real_parser = route_cmd._route_parser

    def parser_with_new_option() -> argparse.ArgumentParser:
        parser = real_parser()
        parser.add_argument("--new-copper-rule", type=float, default=None)
        return parser

    monkeypatch.setattr(route_cmd, "_route_parser", parser_with_new_option)
    with pytest.raises(AssertionError, match="--new-copper-rule"):
        test_every_route_option_is_forwarded_or_excluded_on_purpose()
