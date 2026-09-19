"""Real CLI acceptance for useful routing around invalid placement (#5348)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.native_concurrency import ENV_LIMIT, configured_limit, slot_wait_ceiling
from kicad_tools.schema.pcb import PCB
from tests.test_routing_placement_disposition import board_text

# Wall-clock budgets (Issue #5579)
# --------------------------------
# The ``finite`` half of these tests passes ``--timeout`` to `kct route` so the
# #5413 preservation/reporting semantics are proven to hold under the deadline
# SUPERVISOR too (`kicad_tools.cli.route_deadline.run_attempt` only takes the
# supervised-subprocess path for a positive budget).  The deadline FIRING is not
# what any assertion here is about -- when it fires, the supervisor quarantines
# the routed PCB under a `_timeout_unverified_` name and every downstream
# assertion collapses into noise.
#
# The budget used to be a flat 30 s, which is not what this invocation costs in
# the hosted Test job.  Four parametrizations across four consecutive runs blew
# it during `post-route-drc` on 2026-09-19 (35414096506 and 35420001281 on
# `main`; 35425883418 and 35427127450 on PR #5577), all with routing itself
# reporting 0.0 s and the PCB already saved.  `post-route-drc` shells out to
# `kicad-cli pcb drc` (kicad_tools/drc/geometric.py), which under CI's
# `KCT_NATIVE_MAX_CONCURRENCY=1` gate (#5501) must first take a permit from a
# semaphore shared with every other xdist worker.  That queue wait is ordinary
# wall time inside the route worker, so it is charged to the route budget --
# and unlike the pytest-timeout credit added in #5572, nothing gives it back:
# the credit only extends an armed `SIGALRM` deadline in a pytest process, and
# the waiter here is a grandchild subprocess with no such deadline.
#
# So the budget is composed, not guessed: the invocation's own work, plus the
# gate's own fail-open queue bound when (and only when) the gate is configured.
# That bound is the largest queue time the gate can inject by construction, so
# the budget absorbs the load-sensitive term in full rather than out-guessing
# it.  With the gate inert -- local runs, every non-Test CI job -- the numbers
# are exactly the historical 30 s/90 s, keeping hang detection tight where
# there is no queue to wait in.
_ROUTE_WORK_SECONDS = 30.0
_SUBPROCESS_MARGIN_SECONDS = 60.0
_PYTEST_MARGIN_SECONDS = 60.0


def _queue_ceiling_seconds() -> float:
    """Worst-case native-permit queue time this invocation can be charged."""
    return slot_wait_ceiling() if configured_limit() is not None else 0.0


#: Value handed to ``kct route --timeout`` in the ``finite`` parametrizations.
ROUTE_DEADLINE_SECONDS = _ROUTE_WORK_SECONDS + _queue_ceiling_seconds()
#: Outer bound on the CLI subprocess.  Strictly greater than the route budget
#: so a genuine overrun surfaces as the supervisor's diagnosable PARTIAL report
#: rather than an opaque ``subprocess.TimeoutExpired``.
CLI_SUBPROCESS_SECONDS = ROUTE_DEADLINE_SECONDS + _SUBPROCESS_MARGIN_SECONDS
#: pytest-timeout override.  The Test job arms a 60 s default (`--timeout=60`),
#: which is below the budgets above and would reap the test before either can
#: report anything useful.  Ordering is deliberate: route budget < subprocess
#: bound < pytest deadline, so the innermost bound is always the one that fires.
PYTEST_TIMEOUT_SECONDS = CLI_SUBPROCESS_SECONDS + _PYTEST_MARGIN_SECONDS

pytestmark = pytest.mark.timeout(PYTEST_TIMEOUT_SECONDS)


def _reject_deadline_expiry(output: Path, diagnostics: str = "") -> None:
    """Fail loudly, and legibly, when the route deadline actually fired.

    A deadline expiry is still a FAILURE -- these parametrizations exist to
    enforce the #5413 preservation behavior and must never pass by tolerating a
    timeout.  But the bare ``assert output.is_file()`` that used to report it
    named neither the deadline nor the stage that consumed it, which is what
    made #5579 take four CI runs to diagnose.  Read the supervisor's own report
    and say so.
    """
    report = output.with_suffix(".timeout.json")
    if not report.is_file():
        return
    try:
        state = json.loads(report.read_text())
    except (OSError, ValueError):  # pragma: no cover - unreadable report
        state = {}
    quarantined = state.get("unverified_output")
    pytest.fail(
        "kct route exceeded its "
        f"{state.get('timeout_seconds', ROUTE_DEADLINE_SECONDS)}s wall-clock deadline "
        f"during stage {state.get('stage')!r}, so "
        + (
            f"the routed PCB was quarantined as {quarantined}"
            if quarantined
            else "no canonical routed PCB was produced"
        )
        + " and the placement assertions below cannot run.\n"
        "This is a budget-sizing failure, not a placement regression (#5579): the budget is "
        f"{_ROUTE_WORK_SECONDS}s of the invocation's own work plus a "
        f"{_queue_ceiling_seconds()}s native-permit queue allowance "
        f"({ENV_LIMIT}={os.environ.get(ENV_LIMIT)!r}). Re-size it against the stage named "
        f"above rather than re-running.\nReport: {report}\n{diagnostics}"
    )


def test_finite_budget_absorbs_the_native_permit_queue(monkeypatch):
    """The route budget tracks the gate's bound instead of hardcoding one.

    Regression guard for #5579: a future edit that drops the queue term (or
    pins a literal that goes stale when ``KCT_NATIVE_SLOT_WAIT_SECONDS``
    changes) puts the flat-30 s flake straight back.
    """
    from kicad_tools.native_concurrency import ENV_WAIT_SECONDS

    monkeypatch.delenv(ENV_LIMIT, raising=False)
    assert _queue_ceiling_seconds() == 0.0, "an inert gate must not inflate the budget"

    monkeypatch.setenv(ENV_LIMIT, "1")
    monkeypatch.setenv(ENV_WAIT_SECONDS, "45")
    assert _queue_ceiling_seconds() == 45.0
    assert slot_wait_ceiling() == 45.0

    # Ordering: the innermost bound must always be the one that fires, so the
    # failure is the supervisor's diagnosable PARTIAL report rather than an
    # opaque subprocess kill or a pytest-timeout reap.
    assert ROUTE_DEADLINE_SECONDS >= _ROUTE_WORK_SECONDS
    assert CLI_SUBPROCESS_SECONDS > ROUTE_DEADLINE_SECONDS
    assert PYTEST_TIMEOUT_SECONDS > CLI_SUBPROCESS_SECONDS


def test_deadline_expiry_fails_loudly_rather_than_passing(tmp_path):
    """A genuine expiry stays a failure, and names the stage that caused it."""
    output = tmp_path / "routed.kicad_pcb"
    _reject_deadline_expiry(output)  # no report -> no-op

    output.with_suffix(".timeout.json").write_text(
        json.dumps({"stage": "post-route-drc", "timeout_seconds": 30.0, "status": "partial"})
    )
    with pytest.raises(pytest.fail.Exception) as caught:
        _reject_deadline_expiry(output)
    assert "post-route-drc" in str(caught.value)
    assert "#5579" in str(caught.value)


@pytest.mark.parametrize("entry", ["inner", "outer"])
@pytest.mark.parametrize("finite", [False, True])
@pytest.mark.parametrize(
    "selection",
    [
        "mixed",
        "coupled",
        "valid_only",
        "skip_invalid",
        "complete_noop",
        "complete_partial",
        "complete_excluded_noop",
    ],
)
def test_partial_placement_cli_preserves_and_reports(
    tmp_path, entry, finite, selection, extra_options=()
):
    board = tmp_path / "mixed.kicad_pcb"
    text = board_text()
    if selection.startswith("complete_"):
        additions = """
        (segment (start 106 103) (end 108 103) (width 0.2) (layer "F.Cu") (net 1))
        (segment (start 108 103) (end 125 105) (width 0.2) (layer "F.Cu") (net 1))
        (segment (start 105 107) (end 110 107) (width 0.2) (layer "F.Cu") (net 2))
        (segment (start 105 109) (end 110 109) (width 0.2) (layer "F.Cu") (net 3))
        """
        if selection == "complete_partial":
            additions = additions.replace(
                '(segment (start 105 107) (end 110 107) (width 0.2) (layer "F.Cu") (net 2))',
                "",
            )
        text = text.rstrip()[:-1] + additions + ")"
    board.write_text(text)
    original = board.read_bytes()
    output = tmp_path / "routed.kicad_pcb"
    report = tmp_path / "complete.json"
    failed = tmp_path / "failed.json"
    sidecar = tmp_path / "classes.json"
    sidecar.write_text(
        json.dumps(
            {"BAD": {"name": "Pair", "coupled_routing": True, "diffpair_partner": "PARTNER"}}
        )
    )
    options = {
        "mixed": [],
        "coupled": ["--net-class-map", str(sidecar)],
        "valid_only": ["--nets", "GOOD"],
        "skip_invalid": ["--skip-nets", "BAD"],
        "complete_noop": ["--complete"],
        "complete_partial": ["--complete"],
        "complete_excluded_noop": ["--complete", "--complete-exclude-nets", "BAD,PLANE"],
    }[selection]
    argv = [
        str(board),
        "-o",
        str(output),
        "--complete-report",
        str(report),
        "--export-failed-nets",
        str(failed),
        *options,
        *extra_options,
    ]
    if finite:
        argv += ["--timeout", str(ROUTE_DEADLINE_SECONDS)]
    code = (
        "from kicad_tools.cli.route_cmd import main; import sys; sys.exit(main(sys.argv[1:]))"
        if entry == "inner"
        else 'from kicad_tools.cli import main; import sys; sys.exit(main(["route", *sys.argv[1:]]))'
    )
    result = subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True,
        text=True,
        timeout=CLI_SUBPROCESS_SECONDS,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert board.read_bytes() == original
    _reject_deadline_expiry(output, result.stdout + result.stderr)
    assert output.is_file(), result.stdout + result.stderr
    if selection == "complete_partial" or (extra_options and selection == "mixed"):
        # The retained off-board trace can also trigger the DRC exit (3).
        # Placement must preserve that stronger failure, never turn it into success.
        assert result.returncode in {2, 3}
    else:
        assert result.returncode == (2 if selection in {"mixed", "coupled", "complete_noop"} else 0)
    if selection in {"mixed", "coupled", "complete_noop", "complete_partial"}:
        assert "SUCCESS:" not in result.stdout
        assert "Status: SUCCESS" not in result.stdout
        assert "Design routed successfully" not in result.stdout
        assert "Minimum viable configuration found" not in result.stdout
    parsed = PCB.load(output)
    assert any(segment.net_name == "GOOD" for segment in parsed.segments)
    before = PCB.load(board)

    def identities(pcb):
        return sorted((f.reference, p.number, p.net_name) for f in pcb.footprints for p in f.pads)

    def bad_copper(pcb):
        return sorted(
            (s.start, s.end, s.width, s.layer) for s in pcb.segments if s.net_name == "BAD"
        )

    assert identities(parsed) == identities(before)
    assert bad_copper(parsed) == bad_copper(before)
    assert len([v for v in parsed.vias if v.net_name == "BAD"]) == 1
    disposition = json.loads(report.read_text())["placement_disposition"]
    assert disposition["direct_invalid_nets"] == ["BAD"]
    assert disposition["requested_blocked_nets"] == (
        ["BAD", "PARTNER"]
        if selection == "coupled"
        else ["BAD"]
        if selection in {"mixed", "complete_noop", "complete_partial"}
        else []
    )
    assert "GOOD" in disposition["completed_nets"]

    if selection == "complete_partial":
        assert disposition["plane_excluded_nets"] == []
        assert "PARTNER" in disposition["completed_nets"]
    if selection == "complete_excluded_noop":
        assert disposition["plane_excluded_nets"] == ["BAD", "PLANE"]

    if selection == "coupled":
        assert disposition["coupled_invalid_nets"] == ["PARTNER"]
        assert not any(s.net_name == "PARTNER" for s in parsed.segments)

    if disposition["requested_blocked_nets"]:
        entries = json.loads(failed.read_text())
        blocked = [
            entry for entry in entries if entry["status"] == "placement-invalid, not attempted"
        ]
        assert sorted(entry["net"] for entry in blocked) == disposition["requested_blocked_nets"]
        assert all(entry["attempted"] is False for entry in blocked)
        assert all(entry["status"] != "unrouted" for entry in entries if entry["net"] == "BAD")


@pytest.mark.parametrize("suffix", [".json", ".txt"])
def test_all_invalid_export_replaces_stale_attempt(tmp_path, suffix):
    from kicad_tools.cli.route_cmd import main

    source = tmp_path / "mixed.kicad_pcb"
    source.write_text(board_text())
    target = tmp_path / ("failed" + suffix)
    target.write_text(
        '[{"net": "STALE", "status": "unrouted"}]' if suffix == ".json" else "STALE\n"
    )
    assert main([str(source), "--nets", "BAD", "--export-failed-nets", str(target)]) == 2
    assert "STALE" not in target.read_text()
    if suffix == ".json":
        entries = json.loads(target.read_text())
        assert entries == [
            {
                "net": "BAD",
                "status": "placement-invalid, not attempted",
                "attempted": False,
                "pads": ["X1.1", "X2.1", "X3.1"],
            }
        ]
    else:
        assert target.read_text() == "# placement-invalid, not attempted\nBAD\n"


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(["--no-auto-layers", "--strategy", "basic"], id="basic"),
        pytest.param(["--no-auto-layers", "--strategy", "monte-carlo"], id="monte-carlo"),
        pytest.param(["--no-auto-layers", "--strategy", "evolutionary"], id="evolutionary"),
        pytest.param(
            ["--no-auto-layers", "--strategy", "basic", "--route-engine", "mesh"], id="mesh"
        ),
        pytest.param(
            ["--no-auto-layers", "--strategy", "basic", "--route-engine", "lattice"], id="lattice"
        ),
        pytest.param(["--no-auto-layers", "--adaptive-rules"], id="adaptive-rules"),
        pytest.param(["--adaptive-rules"], id="combined"),
        pytest.param(["--region", "0,0,20,12"], id="region"),
        pytest.param(["--auto-pcb-size"], id="size"),
        pytest.param(["--auto-mfr-tier"], id="manufacturer"),
    ],
)
def test_partial_placement_route_modes(tmp_path, options):
    test_partial_placement_cli_preserves_and_reports(
        tmp_path, "outer", True, "mixed", extra_options=options
    )


@pytest.mark.parametrize("name_only", [False, True])
def test_escaped_invalid_net_survives_actual_cli_and_failed_export(tmp_path, name_only):
    from kicad_tools.cli.route_cmd import main

    invalid = 'BAD\\return"pin'
    source = tmp_path / "escaped.kicad_pcb"
    source.write_text(board_text(name_only=name_only).replace('"BAD"', json.dumps(invalid)))
    original = source.read_bytes()
    output = tmp_path / "output.kicad_pcb"
    report = tmp_path / "report.json"
    failed = tmp_path / "failed.json"
    rc = main(
        [
            str(source),
            "-o",
            str(output),
            "--complete-report",
            str(report),
            "--export-failed-nets",
            str(failed),
        ]
    )
    assert rc in {2, 3}
    assert source.read_bytes() == original
    assert json.loads(report.read_text())["placement_disposition"]["requested_blocked_nets"] == [
        invalid
    ]
    blocked = [r for r in json.loads(failed.read_text()) if r["net"] == invalid]
    assert len(blocked) == 1 and blocked[0]["attempted"] is False
    before, after = PCB.load(source), PCB.load(output)
    assert any(s.net_name == "GOOD" for s in after.segments)
    assert sorted(
        (f.reference, p.number, p.net_name) for f in before.footprints for p in f.pads
    ) == sorted((f.reference, p.number, p.net_name) for f in after.footprints for p in f.pads)
    assert [
        (s.start, s.end, s.width, s.layer) for s in before.segments if s.net_name == invalid
    ] == [(s.start, s.end, s.width, s.layer) for s in after.segments if s.net_name == invalid]


@pytest.mark.parametrize("entry", ["inner", "outer"])
@pytest.mark.parametrize("selection", ["mixed", "all_invalid", "valid_only"])
@pytest.mark.parametrize("finite", [False, True])
def test_json_attempt_summary_includes_placement_on_all_outcomes(
    tmp_path, capfd, entry, selection, finite
):
    from kicad_tools.cli import main as outer_main
    from kicad_tools.cli.route_cmd import main as inner_main

    source = tmp_path / "source.kicad_pcb"
    source.write_text(board_text())
    output = tmp_path / "output.kicad_pcb"
    argv = [str(source), "-o", str(output), "--quiet", "--format", "json"]
    if finite:
        argv += ["--timeout", str(ROUTE_DEADLINE_SECONDS)]
    if selection != "mixed":
        argv += ["--nets", "BAD" if selection == "all_invalid" else "GOOD"]
    rc = outer_main(["route", *argv]) if entry == "outer" else inner_main(argv)
    stdout = capfd.readouterr().out
    _reject_deadline_expiry(output, stdout)
    # Existing progress output can precede JSON. The final object is the
    # attempt summary, not an inferred success from a prior routing diagnostic.
    start = stdout.rfind("\n{") + 1 if "\n{" in stdout else 0
    summary = json.loads(stdout[start:])
    assert summary["exit_code"] == rc
    assert summary["output_written"] is (selection != "all_invalid")
    placement = summary["placement_disposition"]
    assert placement["direct_invalid_nets"] == ["BAD"]
    assert placement["requested_blocked_nets"] == ([] if selection == "valid_only" else ["BAD"])
    if selection != "valid_only":
        assert rc != 0 and placement["clean_success"] is False
    if selection == "all_invalid":
        assert placement["completed_nets"] == []
    else:
        assert any(s.net_name == "GOOD" for s in PCB.load(output).segments)
