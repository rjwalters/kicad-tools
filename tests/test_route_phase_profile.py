"""`scripts/research/route_phase_profile.py` accounting (Issue #5787).

The profile published in ``docs/research/kct-route-phase-profile.md`` is only
as good as the harness's arithmetic, so these tests pin the properties the doc
relies on without routing anything:

* exclusive phase times sum to the wall total (nothing double-counted,
  nothing lost), including under recursion;
* every phase entry point in ``PHASES`` still resolves, and instrumentation
  is fully reverted afterwards;
* the log-derived iteration and stage splits are computed correctly;
* ``--repeat``'s median/min/max aggregation across runs, including a phase
  that appears in only some runs and older JSON with no ``kicad_cli`` block;
* the ``kicad-cli`` subprocess tally counts only KiCad children, groups them
  by subcommand ignoring flags and board paths, and reverts its patch.

The script lives under ``scripts/`` (not ``src/``), so it is loaded by path.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "research" / "route_phase_profile.py"


def _load():
    spec = importlib.util.spec_from_file_location("route_phase_profile_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rpp = _load()


def test_exclusive_times_partition_the_total():
    timer = rpp.PhaseTimer()
    inner = timer.wrap("inner", lambda: time.sleep(0.02))

    def outer_body():
        time.sleep(0.01)
        inner()
        inner()

    outer = timer.wrap("outer", outer_body)
    timer.enter("root")
    t0 = time.perf_counter()
    outer()
    timer.exit()
    total = time.perf_counter() - t0

    s = timer.stats
    assert s["inner"].calls == 2
    assert s["outer"].calls == 1
    assert s["outer"].inclusive >= s["inner"].inclusive >= 0.04
    assert s["outer"].exclusive < s["outer"].inclusive - 0.035
    excl = sum(st.exclusive for st in s.values())
    assert abs(excl - total) < 0.005


def test_recursion_counts_inclusive_once():
    timer = rpp.PhaseTimer()

    def rec(n):
        time.sleep(0.005)
        if n:
            wrapped(n - 1)

    wrapped = timer.wrap("rec", rec)
    wrapped(3)
    st = timer.stats["rec"]
    assert st.calls == 4
    # Outermost frame only: inclusive equals total elapsed, not ~4x it.
    assert abs(st.inclusive - st.exclusive) < 0.002


def test_every_phase_entry_point_resolves_and_is_reverted():
    timer = rpp.PhaseTimer()
    originals = {}
    for _label, module_name, qualname in rpp.PHASES:
        owner = importlib.import_module(module_name)
        *path, attr = qualname.split(".")
        for part in path:
            owner = getattr(owner, part)
        assert hasattr(owner, attr), f"{module_name}.{qualname} no longer exists"
        originals[(module_name, qualname)] = (owner, attr, getattr(owner, attr))

    undo = rpp.instrument(timer)
    try:
        for owner, attr, _orig in originals.values():
            assert getattr(getattr(owner, attr), "__route_phase_profile__", False)
    finally:
        rpp.uninstrument(undo)
    for owner, attr, orig in originals.values():
        assert getattr(owner, attr) is orig


def test_iteration_split():
    lines = [
        (1.0, "--- Iteration 0: Initial routing with sharing ---"),
        (3.0, "--- Iteration 1: Rip-up and reroute ---"),
        (7.5, "  Best-metric early-stop: no improvement"),
        (8.0, "--- Iteration 0: Initial routing with sharing ---"),
        (9.0, "=== Negotiated Routing Complete ==="),
    ]
    out = rpp.iteration_split(lines)
    assert [(r["attempt"], r["iteration"], r["seconds"]) for r in out] == [
        (0, 0, 2.0),
        (0, 1, 4.5),
        (1, 0, 1.0),
    ]
    assert all("astar_s" not in r for r in out)


def test_iteration_split_two_phase_markers():
    lines = [
        (10.0, "--- Phase 2: Detailed Routing ---"),
        (40.0, "  Iteration 1: ripping up 12 nets (47.8s)"),
        (60.0, "  Iteration 1 complete: clearance_viol=0, overflow=34"),
        (61.0, "  Iteration 2: ripping up 12 nets (74.5s)"),
        (90.0, "=== Two-Phase Routing Complete ==="),
    ]
    out = rpp.iteration_split(lines)
    assert [(r["iteration"], r["seconds"]) for r in out] == [(0, 30.0), (1, 21.0), (2, 29.0)]


def test_iteration_split_reports_astar_share_and_unterminated_loop():
    lines = [
        (0.0, "--- Iteration 0: Initial routing with sharing ---", 0.0),
        (4.0, "  Routed 10/10 nets", 3.0),
        (5.0, "--- Iteration 1: Rip-up and reroute ---", 3.5),
        (9.0, "  Rerouted 6/6 nets", 6.0),
    ]
    out = rpp.iteration_split(lines)
    assert [(r["iteration"], r["seconds"], r["astar_s"]) for r in out] == [
        (0, 5.0, 3.5),
        (1, 4.0, 2.5),
    ]
    assert out[-1]["unterminated"] is True
    assert "unterminated" not in out[0]


def test_stage_split_sums_repeated_stages():
    rows = rpp.stage_split([(1.0, "routing"), (5.0, "serialization"), (6.0, "routing")], 10.0)
    by = {r["stage"]: r for r in rows}
    assert by["(before first stage marker)"]["seconds"] == 1.0
    assert by["routing"]["seconds"] == 8.0
    assert by["routing"]["entries"] == 2
    assert by["serialization"]["seconds"] == 1.0


def test_subprocess_tally_counts_only_kicad_cli_children():
    """The kicad-cli invocation count is a cost driver the phase table hides."""
    import subprocess

    tally = rpp.SubprocessTally()
    undo = rpp._count_kicad_cli_subprocesses(tally)
    try:
        subprocess.run(["/bin/echo", "not kicad"], capture_output=True)
        subprocess.run(["/usr/bin/true"], capture_output=True)
    finally:
        undo()
    assert tally.calls == []
    assert subprocess.run is not undo  # the patch was reverted

    # Simulate kicad-cli children without launching KiCad.
    tally = rpp.SubprocessTally()
    tally.record(["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli", "version"], 6.0)
    tally.record(["/opt/kicad/kicad-cli", "pcb", "drc", "--refill-zones", "b.kicad_pcb"], 11.0)
    tally.record(["/opt/kicad/kicad-cli", "pcb", "drc", "b.kicad_pcb"], 9.0)
    summary = tally.summary()
    assert summary["invocations"] == 3
    assert summary["total_s"] == 26.0
    # Grouped by subcommand, most expensive first; flags are not part of the key.
    assert summary["by_subcommand"][0] == {"subcommand": "pcb drc", "calls": 2, "seconds": 20.0}
    assert summary["by_subcommand"][1] == {"subcommand": "version", "calls": 1, "seconds": 6.0}


def test_subprocess_patch_times_a_real_kicad_cli_shaped_child(tmp_path):
    """A child whose basename is kicad-cli is tallied with its wall time."""
    import subprocess

    fake = tmp_path / "kicad-cli"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    tally = rpp.SubprocessTally()
    undo = rpp._count_kicad_cli_subprocesses(tally)
    try:
        subprocess.run([str(fake), "version"], capture_output=True)
    finally:
        undo()
    assert len(tally.calls) == 1
    assert tally.calls[0]["exe"] == "kicad-cli"
    assert tally.calls[0]["subcommand"] == "version"
    assert tally.calls[0]["seconds"] >= 0.0


def _run(wall, phases, stages=(), load=(1.0, 1.0, 1.0), exit_code=0, kicad_cli=None):
    """A minimal ``profile_route``-shaped payload for the aggregator."""
    return {
        "board": "b.kicad_pcb",
        "argv": ["kct", "route"],
        "exit_code": exit_code,
        "wall_s": wall,
        "phases": [
            {
                "phase": label,
                "calls": calls,
                "inclusive_s": excl,
                "exclusive_s": excl,
                "exclusive_pct": round(100.0 * excl / wall, 1),
            }
            for label, calls, excl in phases
        ],
        "stages": [{"stage": s, "entries": 1, "seconds": sec} for s, sec in stages],
        "host": {"loadavg_start": list(load), "loadavg_end": list(load)},
        "kicad_cli": kicad_cli,
    }


def test_aggregate_runs_reports_median_and_spread():
    runs = [
        _run(100.0, [("A*", 90, 50.0), ("pour", 1, 20.0)]),
        _run(120.0, [("A*", 90, 60.0), ("pour", 1, 22.0)]),
        _run(110.0, [("A*", 90, 55.0), ("pour", 1, 21.0)]),
    ]
    agg = rpp.aggregate_runs(runs)
    assert agg["runs"] == 3
    assert agg["wall_median_s"] == 110.0
    assert (agg["wall_min_s"], agg["wall_max_s"]) == (100.0, 120.0)
    assert agg["wall_spread"] == 1.2
    assert agg["wall_all_s"] == [100.0, 120.0, 110.0]
    assert agg["exit_codes"] == [0, 0, 0]
    assert len(agg["loadavg_per_run"]) == 3

    # Sorted by median exclusive, descending; min/max bracket the median.
    assert [r["phase"] for r in agg["phases"]] == ["A*", "pour"]
    astar = agg["phases"][0]
    assert (astar["exclusive_median_s"], astar["exclusive_min_s"], astar["exclusive_max_s"]) == (
        55.0,
        50.0,
        60.0,
    )
    # Percentage is median-phase against median-wall, not an average of ratios.
    assert astar["exclusive_median_pct"] == 50.0
    assert astar["runs"] == 3
    assert astar["calls_median"] == 90


def test_aggregate_runs_flags_a_phase_missing_from_some_runs():
    """A phase that appears in only some runs must not look uniformly cheap."""
    runs = [
        _run(100.0, [("A*", 90, 50.0), ("python fallback", 1, 9.0)]),
        _run(100.0, [("A*", 90, 50.0)]),
    ]
    agg = rpp.aggregate_runs(runs)
    by = {r["phase"]: r for r in agg["phases"]}
    assert by["A*"]["runs"] == 2
    assert by["python fallback"]["runs"] == 1
    # Median is over the runs it occurred in -- not diluted toward zero.
    assert by["python fallback"]["exclusive_median_s"] == 9.0


def test_aggregate_runs_stages_and_single_run():
    runs = [
        _run(10.0, [("A*", 1, 5.0)], stages=(("routing", 6.0), ("post-route-drc", 3.0))),
        _run(20.0, [("A*", 1, 9.0)], stages=(("routing", 14.0),)),
    ]
    agg = rpp.aggregate_runs(runs)
    by = {r["stage"]: r for r in agg["stages"]}
    assert by["routing"]["median_s"] == 10.0
    assert by["post-route-drc"]["runs"] == 1

    solo = rpp.aggregate_runs([runs[0]])
    assert solo["runs"] == 1
    assert solo["wall_spread"] == 1.0
    assert solo["wall_median_s"] == 10.0


def test_aggregate_runs_summarises_kicad_cli_invocations():
    runs = [
        _run(
            100.0,
            [("A*", 1, 50.0)],
            kicad_cli={"invocations": 5, "total_s": 30.0, "by_subcommand": []},
        ),
        _run(
            200.0,
            [("A*", 1, 50.0)],
            kicad_cli={"invocations": 5, "total_s": 40.0, "by_subcommand": []},
        ),
    ]
    agg = rpp.aggregate_runs(runs)
    cli = agg["kicad_cli"]
    assert cli["invocations_median"] == 5
    assert cli["invocations_all"] == [5, 5]
    assert (cli["total_median_s"], cli["total_min_s"], cli["total_max_s"]) == (35.0, 30.0, 40.0)
    assert cli["median_pct_of_wall"] == round(100.0 * 35.0 / 150.0, 1)
    assert "kicad-cli: 5 invocations (median), 35.0 s total" in rpp.render_aggregate_markdown(agg)


def test_aggregate_runs_tolerates_runs_without_a_kicad_cli_tally():
    """Older JSON (pre-tally) must still aggregate rather than crash."""
    runs = [_run(100.0, [("A*", 1, 50.0)]), _run(100.0, [("A*", 1, 50.0)])]
    agg = rpp.aggregate_runs(runs)
    assert agg["kicad_cli"] is None
    md = rpp.render_aggregate_markdown(agg)
    assert "kicad-cli:" not in md


def test_aggregate_runs_rejects_empty_input():
    try:
        rpp.aggregate_runs([])
    except ValueError:
        pass
    else:  # pragma: no cover - the assertion below is the failure path
        raise AssertionError("aggregate_runs([]) must raise")


def test_render_aggregate_markdown_includes_spread_and_per_run_load():
    agg = rpp.aggregate_runs(
        [
            _run(100.0, [("A*", 90, 50.0)], stages=(("routing", 70.0),), load=(2.0, 2.1, 2.2)),
            _run(130.0, [("A*", 90, 70.0)], stages=(("routing", 90.0),), load=(9.0, 9.1, 9.2)),
        ]
    )
    md = rpp.render_aggregate_markdown(agg)
    assert "2 runs" in md
    assert "spread 1.3x" in md
    assert "| A* | 2 | 90 | 60.00 | 50.00 | 70.00 |" in md
    assert "[9.0, 9.1, 9.2]" in md  # per-run load average is visible in the table
    assert "| routing | 2 | 80.00 | 70.00 | 90.00 |" in md
