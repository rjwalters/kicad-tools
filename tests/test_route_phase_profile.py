"""`scripts/research/route_phase_profile.py` accounting (Issue #5787).

The profile published in ``docs/research/kct-route-runtime-profile.md`` is only
as good as the harness's arithmetic, so these tests pin the three properties
the doc relies on without routing anything:

* exclusive phase times sum to the wall total (nothing double-counted,
  nothing lost), including under recursion;
* every phase entry point in ``PHASES`` still resolves, and instrumentation
  is fully reverted afterwards;
* the log-derived iteration and stage splits are computed correctly.

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
