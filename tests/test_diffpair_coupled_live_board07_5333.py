"""Live native Board07 control for all seven coupled construction paths.

The committed fixture runs with seed42 and the recipe's unchanged search
parameters. Singles and pours are skipped to isolate the differential pre-pass;
this is not full-recipe manufacturing or match-group acceptance. Every pair
must pass the router's authored skew/coupling and exact geometry gates.

MIPI_DAT1 exercises conservative departure halo resolution. TMDS_D1/D2
exercise completion of saved native corridor bodies with surface-layer tails.

HOST-DEPENDENT (#5333): the qualification COUNT here is not a
machine-independent property, even under ``--deterministic-budget``. The
coupled phase keeps a per-pair wall cutoff (#3089/#3321) and an aggregate
coupled-phase cutoff (#3439) that the flag does not disable, so a slow or
loaded host qualifies fewer pairs for reasons that have nothing to do with
the implementation under test. Measured on this exact fixture with identical
source, argv and seed 42 on one loaded host (load avg ~29): the derived 60 s
per-pair wall qualified 3/7 on one run and 4/7 on the very next, while
``--diffpair-per-pair-timeout 300`` qualified 6/7. Every failure message
therefore quotes the run's own budget lines (:func:`_budget_context`) so a
wall-clock budget exit is never mistaken for a regression -- and so a passing
run is never quoted as a machine-independent qualification.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not is_cpp_available(),
        reason="Live Board07 coupled routing requires the router_cpp backend (kct build-native)",
    ),
]

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "boards" / "07-matchgroup-test" / "regression-fixture"
FIXTURE_PCB = FIXTURE_DIR / "matchgroup_test.kicad_pcb"
FIXTURE_NET_CLASS_MAP = FIXTURE_DIR / "net_class_map.json"

# Issue #5333: the recipe's 26 non-diff-pair nets (9 DDR singles, 8 ADDR bus
# singles, 3 pour nets) -- skipped so the invocation only pays for the
# diff-pair pre-pass (which runs first regardless) plus a near-empty
# negotiated remainder, not a full 31-net Board07 route.
_NON_DIFFPAIR_NETS = [
    "GND",
    "+1V2",
    "+1V8",
    "DQ0",
    "DQ1",
    "DQ2",
    "DQ3",
    "DQ4",
    "DQ5",
    "DQ6",
    "DQ7",
    "DM0",
    "A0",
    "A1",
    "A2",
    "A3",
    "A4",
    "A5",
    "A6",
    "A7",
]

_EXPECTED_QUALIFIED_PAIRS = (
    "DQS",
    "MIPI_CLK",
    "MIPI_DAT0",
    "MIPI_DAT1",
    "TMDS_D0",
    "TMDS_D1",
    "TMDS_D2",
)


@pytest.fixture(scope="module")
def board07_live_diffpair_log(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Run a real, live ``kct route --differential-pairs`` against Board07.

    One subprocess invocation, shared read-only by every test in this
    module (module-scoped) -- each test asserts a different fact about the
    same captured stdout rather than re-paying the ~75s routing cost.
    """
    scratch = tmp_path_factory.mktemp("board07_live_diffpair")
    input_pcb = scratch / "matchgroup_test.kicad_pcb"
    input_pcb.write_bytes(FIXTURE_PCB.read_bytes())
    net_class_map = scratch / "net_class_map.json"
    net_class_map.write_bytes(FIXTURE_NET_CLASS_MAP.read_bytes())
    output_pcb = scratch / "routed.kicad_pcb"

    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "42"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(input_pcb),
            "--output",
            str(output_pcb),
            "--manufacturer",
            "jlcpcb",
            "--strategy",
            "negotiated",
            "--no-auto-layers",
            "--layers",
            "4",
            "--seed",
            "42",
            # Issue #5333 repro parameters: mirror generate_design.py's
            # ROUTE_SEARCH_TIMEOUT_S / --deterministic-budget exactly (see
            # boards/07-matchgroup-test/generate_design.py); the derived
            # HARD TOTAL --timeout there also folds in placement-delta
            # probes this invocation does not run, so 600s (the same value
            # as --search-timeout) is sufficient here.
            "--timeout",
            "600",
            "--search-timeout",
            "600",
            "--deterministic-budget",
            "--skip-nets",
            ",".join(_NON_DIFFPAIR_NETS),
            "--net-class-map",
            str(net_class_map),
            "--length-match-groups",
            "--differential-pairs",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=600,
    )
    log = result.stdout + result.stderr
    # Exit 3 remains allowed because singles/pours were deliberately skipped.
    # Every differential pair must independently qualify below; no unrelated
    # open net can excuse a missing coupled pair.
    assert result.returncode in (0, 3), (
        f"unexpected kct route exit code {result.returncode}; full output:\n{log}"
    )
    return log


def _pair_report_line(log: str, pair: str) -> str:
    match = re.search(rf"\[coupled-pair-report\] pair={re.escape(pair)} .*", log)
    assert match is not None, (
        f"no [coupled-pair-report] line for pair={pair}{_budget_context(log)}\n\n"
        f"full output:\n{log}"
    )
    return match.group(0)


def _budget_context(log: str) -> str:
    """Wall-budget context to append to any qualification failure (#5333).

    This module's qualification count is NOT a machine-independent property.
    The coupled phase applies a per-pair wall cutoff (#3089/#3321) and an
    aggregate coupled-phase cutoff (#3439); ``--deterministic-budget``
    disables neither.  Measured on this fixture with identical source, argv
    and seed on one loaded host: the derived 60 s per-pair wall qualified 3/7
    pairs on one run and 4/7 on the next, while ``--diffpair-per-pair-timeout
    300`` qualified 6/7.  A failure here is therefore ambiguous between "the
    implementation regressed" and "this host was too slow for the wall
    budget", and the run's own budget lines are what tells them apart -- so
    quote them in the failure instead of making the reader re-run it.
    """
    lines = [
        line.strip()
        for line in log.splitlines()
        if "DIFFPAIR_NONDETERMINISTIC_BUDGET" in line
        or "DIFFPAIR_BUDGET_EXIT_FALLBACK" in line
        or "DIFFPAIR_AGGREGATE_BUDGET_EXCEEDED" in line
        or "budget exceeded" in line
    ]
    if not lines:
        return " (no budget-exit lines in the run -- this is NOT a budget exit)"
    joined = "\n  ".join(lines)
    return (
        "\n\nBUDGET CONTEXT (#5333) -- a wall-clock budget exit here is "
        "host-speed dependent, not necessarily an implementation regression:"
        f"\n  {joined}"
    )


def _construction_line_before(log: str, report_line: str) -> str | None:
    """The ``[coupled-construction]`` diagnostic immediately preceding a pair's report line."""
    prefix = log[: log.index(report_line)]
    # Construction logs are emitted before their own pair report. Stop at
    # the prior report so a missing target diagnostic cannot borrow its work.
    prefix = prefix.rsplit("[coupled-pair-report]", 1)[-1]
    matches = re.findall(r"\[coupled-construction\] .*", prefix)
    return matches[-1] if matches else None


@pytest.mark.parametrize("pair", _EXPECTED_QUALIFIED_PAIRS)
def test_previously_qualified_pairs_remain_coupled_ok(
    board07_live_diffpair_log: str, pair: str
) -> None:
    """Every actual native pair must pass the unchanged qualification gates.

    The gates themselves are unchanged and unwaived.  What is NOT asserted is
    that this count is machine-independent -- see :func:`_budget_context`.
    """
    line = _pair_report_line(board07_live_diffpair_log, pair)
    context = _budget_context(board07_live_diffpair_log)
    assert f"pair={pair} class=coupled-ok" in line, f"{line}{context}"
    assert "coupled=True" in line, f"{line}{context}"


def test_tmds_d1_corridor_stage_is_invoked_in_a_live_search(board07_live_diffpair_log: str) -> None:
    """Live confirmation of the b4c764ef corridor-starvation fix (#5333).

    Before b4c764ef, TMDS_D1's ``[coupled-construction]`` diagnostic reported
    ``corridor_attempts=0`` in production (the lattice stage burned the
    entire per-pair wall-clock window on tail widening before the
    corridor-guided stage ever ran) -- see pair_construction.py's
    ``CORRIDOR_WALL_RESERVE_FRACTION`` docstring for the full measurement.
    This asserts the corridor stage now actually runs at least once in a
    real, live search (not a replay), which is the literal, durable version
    of the "confirm end-to-end" verification named in the #5333 progress
    comment that shipped that fix.
    """
    report_line = _pair_report_line(board07_live_diffpair_log, "TMDS_D1")
    construction_line = _construction_line_before(board07_live_diffpair_log, report_line)
    assert construction_line is not None, (
        "no [coupled-construction] diagnostic found for TMDS_D1 -- the "
        "construction stage (added by #5333) did not run at all"
    )
    match = re.search(r"corridor_attempts=(\d+)", construction_line)
    assert match is not None, construction_line
    corridor_attempts = int(match.group(1))
    assert corridor_attempts > 0, (
        "TMDS_D1's corridor-guided construction stage was never invoked "
        f"(corridor_attempts=0) -- this is the exact pre-b4c764ef starvation "
        f"regression: {construction_line}"
    )
    iters_match = re.search(r"corridor_iters=(\d+)", construction_line)
    assert iters_match is not None, construction_line
    assert int(iters_match.group(1)) > 0, construction_line
