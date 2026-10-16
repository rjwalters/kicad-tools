"""Replay Board07's TMDS_D1 coupled diff-pair search from a fixture (Issue #6816).

``tests/fixtures/board07_coupled_tmds_d1/`` holds every native call that built
the coupled search's ``Grid3D`` and ``CoupledPathfinder`` on a real Board07
``kct route --differential-pairs`` run, plus both searches (corridor phase and
open phase) and their recorded diagnostics.  It was written by
``boards/07-matchgroup-test/capture_coupled_search.py``; re-run that script to
refresh it after a deliberate change to the router or the board.

The exact-replay tests are the fidelity check: a replay that does not match
the recorded diagnostics exactly is a capture bug.  The control tests then
perturb ONE input of the captured search and pin how the outcome moves.  They
are diagnostics for #5333, not fixes -- nothing here changes production
routing.  What they showed (measured on the capture at ``e6c493aec``, native
``BUILD_VERSION`` 57 -- the PRE-#6826 BASELINE, kept here on purpose):

* The pair is not structurally unroutable.  The unmodified inputs succeed
  once the open phase may run 402,163 iterations; the per-phase budget is
  20,000.
* Free space alone needs about 4.5x the budget: with no copper and no pad
  records on the grid at all, the search succeeds only at 89,057 iterations,
  and at the captured budget it stalls 13 cells short.  The cost is the joint
  A* flooding its f-plateau (``sym_trail`` / ``asym_trail_*`` dominate), not
  anything the board puts in the way.
* ``via_blocked_p`` is not causal, although it is the dominant rejection.
  Forbidding layer changes or clearing every foreign raster cell near the four
  pads leaves the captured outcome where it was (best progress 48 -> 48 / 47).
  The counter is tallied once per popped state per alternative layer, so a
  search that pops 20,000 states near pads accrues it regardless.
* The F.Cu-only landing is sealed by the foreign BGA pads: restricted to F.Cu
  the open set is exhausted 44 cells from the goal, while the same search
  with no pad records succeeds.

So "landing-stall, ``via_blocked_p``" in the board report misreads a budget
exhaustion: the cause is the joint search's iteration cost on these pad
geometries (pad pitch 8 and 10 cells along the travel axis against a 3-cell
target spacing), not foreign copper at the landing.

Issue #6826 (native ``BUILD_VERSION`` 58) changed the search on purpose: inside
the relaxed endpoint zones the spacing tolerance is now each end's own
pad-pitch delta + 1 (6 at the start, 8 at the goal) instead of the zone radius
(14 / 18).  The fixture inputs (``grid.npz``, the call list, the manifest) are
NOT re-captured -- they are byte-identical, so before and after compare
directly -- but the replayed outcome is re-pinned to the post-fix values:

=====================================  ==============  ==============
Control                                before (#6816)  after (#6826)
=====================================  ==============  ==============
Open phase, budget lifted              402,163         213,996
Empty grid, budget lifted              89,057          48,031
Captured budget: best progress         48              44
Empty grid at budget: best progress    13              3
=====================================  ==============  ==============

TMDS_D1 still does not couple within the 20,000-iteration phase budget; it
needs ~214k.  The recorded manifest diagnostics stay the pre-fix baseline, so
the exact-replay test now pins that the replay *differs* from the recording
only through the search (identical inputs), and pins the post-fix diagnostics.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from kicad_tools.router.cpp_backend import is_cpp_available

pytestmark = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not built -- run `uv run kct build-native`",
)

FIXTURE = Path(__file__).parent / "fixtures" / "board07_coupled_tmds_d1"

# Positional arguments of the native ``CoupledPathfinder.route`` (the order
# ``CppCoupledPathfinder.route`` marshals them in).
ROUTE_ARGS = (
    "p_start_x",
    "p_start_y",
    "n_start_x",
    "n_start_y",
    "start_layer",
    "p_goal_x",
    "p_goal_y",
    "n_goal_x",
    "n_goal_y",
    "end_layer",
    "p_net",
    "n_net",
    "effective_target_spacing",
    "effective_approach_radius",
    "effective_departure_radius",
    "routable_layers",
    "corridor_bitset",
    "max_iterations_budget",
    "timeout_seconds",
    "p_goal_any_layer",
    "n_goal_any_layer",
)
CORRIDOR, OPEN = 0, 1

# Calls that register pads with the native grid; they index each other, so a
# control drops them together.
PAD_RECORD_METHODS = frozenset(
    {"add_pad", "set_pad_authored", "set_net_clearance_floors", "set_pad_via_policy"}
)
# Committed copper and its halo provenance (everything but the raster).
ROUTE_COPPER_METHODS = frozenset(
    {"add_stored_segment", "add_stored_via", "register_route_mark", "set_cells_static_blocked"}
)


@pytest.fixture(scope="module")
def fixture():
    from kicad_tools.router.coupled_capture import load_fixture

    return load_fixture(FIXTURE)


@pytest.fixture(scope="module")
def searches(fixture):
    """The captured calls up to and including the last coupled search.

    The capture also recorded the pose-rescue (``route_centerline``) that ran
    after both phases failed; it is not part of this issue's question and
    would only slow the replay.
    """
    last = max(i for i, c in enumerate(fixture.calls) if _is_route(c))
    return fixture.calls[: last + 1]


def _is_route(call: dict) -> bool:
    return call["op"] == "call" and call["method"] == "route"


def _route_indices(calls: list[dict]) -> list[int]:
    return [i for i, c in enumerate(calls) if _is_route(c)]


def _replay(calls: list[dict]) -> list[dict]:
    from kicad_tools.router.coupled_capture import replay

    return replay(calls).routes


def _with_route_args(calls: list[dict], phase: int, **overrides) -> list[dict]:
    calls = list(calls)
    index = _route_indices(calls)[phase]
    entry = dict(calls[index])
    args = list(entry["args"])
    for name, value in overrides.items():
        args[ROUTE_ARGS.index(name)] = value
    entry["args"] = args
    calls[index] = entry
    return calls


def _route_arg(calls: list[dict], phase: int, name: str):
    return calls[_route_indices(calls)[phase]]["args"][ROUTE_ARGS.index(name)]


def _filter_raster(calls: list[dict], keep) -> list[dict]:
    """Keep only the bulk ``mark_blocked`` rows where ``keep(x, y, layer, net)``."""
    out = []
    for call in calls:
        if call["op"] == "batch" and call["method"] == "mark_blocked":
            cols = call["columns"]
            mask = keep(cols[0], cols[1], cols[2], cols[3])
            call = {**call, "columns": [c[mask] for c in cols], "rows": int(mask.sum())}
        out.append(call)
    return out


def _drop(calls: list[dict], methods: frozenset[str]) -> list[dict]:
    return [c for c in calls if not (c["op"] in ("call", "batch") and c["method"] in methods)]


def _bare(calls: list[dict]) -> list[dict]:
    """Same search, same pads' coordinates -- but nothing at all on the grid."""
    calls = _filter_raster(calls, lambda x, y, layer, net: np.zeros(len(x), dtype=bool))
    return _drop(calls, PAD_RECORD_METHODS | ROUTE_COPPER_METHODS)


def _pair_nets(calls: list[dict]) -> tuple[int, int]:
    return _route_arg(calls, OPEN, "p_net"), _route_arg(calls, OPEN, "n_net")


def _pad_cells(calls: list[dict]) -> list[tuple[int, int]]:
    a = [_route_arg(calls, OPEN, n) for n in ROUTE_ARGS[:9]]
    return [(a[0], a[1]), (a[2], a[3]), (a[5], a[6]), (a[7], a[8])]


def _dominant(diag: dict) -> str:
    return max(diag["rejections"].items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# Fixture and fidelity
# ---------------------------------------------------------------------------


def test_fixture_carries_provenance_and_stays_small(fixture):
    prov = fixture.manifest["provenance"]
    for key in (
        "source_commit",
        "input_pcb_sha256",
        "net_class_sidecar_sha256",
        "argv_sha256",
        "router_cpp_version",
        "router_cpp_build_version",
    ):
        assert prov[key], key
    assert "--differential-pairs" in prov["argv"]
    assert prov["pythonhashseed"] == "42"
    size = sum(p.stat().st_size for p in FIXTURE.iterdir())
    assert size <= 5_000_000


def test_capture_shows_the_measured_exhaustion(fixture):
    """TMDS_D1 as measured on main 2026-10-15: landing-stall, 48, via_blocked_p."""
    phases = fixture.manifest["phases"]
    assert [p["phase"] for p in phases] == ["corridor", "open"]
    for phase in phases:
        diag = phase["native_diagnostics"]
        assert diag["success"] is False
        assert diag["iteration_limited"] is True
        assert diag["iterations"] == phase["python_kwargs"]["max_iterations_budget"] == 20000
        assert diag["best_progress"] == 48.0
        assert _dominant(diag) == "via_blocked_p"
        # The Python wrapper saw the same numbers the native search returned.
        py = phase["python_diagnostics"]
        assert py["iterations"] == diag["iterations"]
        assert py["best_progress"] == diag["best_progress"]
        assert py["rejections"] == diag["rejections"]
    assert [c["class"] for c in fixture.manifest["classification"]] == ["landing-stall"]


# Post-#6826 diagnostics of the unmodified replay (corridor phase, open phase).
# The pre-#6826 values are the recording itself (``recorded_routes`` / the
# manifest): both phases at 20,000 iterations, best progress 48,
# ``via_blocked_p`` dominant.
POST_6826_PHASES = [
    {
        "best_progress": 44.0,
        "iteration_limited": True,
        "iterations": 20000,
        "path_len": 0,
        "rejections": {
            "asym_blocked_n": 2319,
            "asym_blocked_p": 2312,
            "asym_floor_n": 3719,
            "asym_floor_p": 3686,
            "asym_spacing_n": 15135,
            "asym_spacing_p": 15301,
            "asym_trail_n": 14114,
            "asym_trail_p": 13908,
            "corridor": 17,
            "sym_blocked_n": 2088,
            "sym_blocked_p": 2312,
            "sym_spacing": 5769,
            "sym_trail": 28652,
            "via_blocked_n": 4605,
            "via_blocked_p": 15501,
            "via_own_h2h": 25599,
        },
        "success": False,
        "timeout_exceeded": True,
    },
    {
        "best_progress": 44.0,
        "iteration_limited": True,
        "iterations": 20000,
        "path_len": 0,
        "rejections": {
            "asym_blocked_n": 2372,
            "asym_blocked_p": 2314,
            "asym_floor_n": 3720,
            "asym_floor_p": 3686,
            "asym_spacing_n": 15110,
            "asym_spacing_p": 15318,
            "asym_trail_n": 14105,
            "asym_trail_p": 13887,
            "sym_blocked_n": 2139,
            "sym_blocked_p": 2314,
            "sym_spacing": 5769,
            "sym_trail": 28605,
            "via_blocked_n": 4605,
            "via_blocked_p": 15501,
            "via_own_h2h": 25599,
        },
        "success": False,
        "timeout_exceeded": True,
    },
]


def test_replay_reproduces_both_phases_exactly(fixture, searches):
    """The replay is exact against the post-#6826 search.

    Before #6826 this asserted ``replayed == recorded_routes(...)``.  The
    endpoint-zone tolerance change alters the search deliberately, so the
    replay now differs from the (unchanged, pre-fix) recording and matches the
    pinned post-fix diagnostics instead.  Re-capturing would hide the
    before/after comparison, so the fixture is left as recorded.
    """
    from kicad_tools.router.coupled_capture import recorded_routes

    start = time.monotonic()
    replayed = _replay(searches)
    elapsed = time.monotonic() - start
    assert replayed == POST_6826_PHASES
    manifest_diags = [p["native_diagnostics"] for p in fixture.manifest["phases"]]
    assert recorded_routes(fixture.calls) == manifest_diags
    assert replayed != manifest_diags
    assert elapsed < 10.0


def test_replay_is_deterministic(searches):
    assert _replay(searches) == _replay(searches)


def test_replay_is_sensitive_to_one_landing_cell(searches):
    """Tamper check: drop the foreign raster cell nearest a pad and the match breaks."""
    p_net, n_net = _pair_nets(searches)
    (px, py) = _pad_cells(searches)[0]
    batch = next(c for c in searches if c["op"] == "batch" and c["method"] == "mark_blocked")
    x, y, layer, net = batch["columns"][:4]
    foreign = (layer == 0) & (net != p_net) & (net != n_net)
    dist = np.where(foreign, np.abs(x - px) + np.abs(y - py), np.iinfo(np.int64).max)
    target = int(np.argmin(dist))

    tampered = _filter_raster(searches, lambda *cols: np.arange(len(cols[0])) != target)
    # Compared against the post-#6826 pinned replay, not the pre-fix
    # recording (which every replay now differs from).
    assert _replay(tampered) != POST_6826_PHASES


# ---------------------------------------------------------------------------
# Single-variable controls (diagnostic for #5333 -- observed outcomes pinned)
# ---------------------------------------------------------------------------


def test_passing_control_budget_alone_lets_the_captured_pair_couple(searches):
    """Same inputs, bigger open-phase budget: the search succeeds.

    So the board does not make TMDS_D1 unroutable.  Before #6826 it needed
    402,163 iterations (about 20x the 20,000-iteration phase budget); bounding
    the endpoint-zone tolerance by pad pitch roughly halves that.
    """
    [_, open_phase] = _replay(
        _with_route_args(searches, OPEN, max_iterations_budget=1_000_000, timeout_seconds=0.0)
    )
    assert open_phase["success"] is True
    assert open_phase["iterations"] == 213_996  # 402,163 before #6826
    assert open_phase["iteration_limited"] is False


def test_control_free_space_needs_more_than_the_budget(searches):
    """An empty grid (no raster, no pads, no routes) still misses at 20,000.

    With the budget lifted it succeeds at 48,031 iterations (89,057 before
    #6826, best progress at the budget 13 -> 3): even in free space the joint
    search needs ~2.4x the per-phase budget for these endpoints.
    """
    bare = _bare(searches)
    [_, at_budget] = _replay(bare)
    assert at_budget["success"] is False
    assert at_budget["iteration_limited"] is True
    assert at_budget["best_progress"] == 3.0  # 13 before #6826
    assert _dominant(at_budget) == "sym_trail"

    [_, lifted] = _replay(
        _with_route_args(bare, OPEN, max_iterations_budget=400_000, timeout_seconds=0.0)
    )
    assert lifted["success"] is True
    assert lifted["iterations"] == 48_031  # 89,057 before #6826


def test_failing_control_no_layer_changes_leaves_progress_unchanged(searches):
    """Forbid vias: ``via_blocked_p`` disappears but the outcome does not move."""
    [_, open_phase] = _replay(_with_route_args(searches, OPEN, routable_layers=[0]))
    assert open_phase["success"] is False
    assert open_phase["iteration_limited"] is True
    assert open_phase["best_progress"] == 44.0  # 48 before #6826, as unmodified
    assert not any(k.startswith("via_") for k in open_phase["rejections"])
    assert _dominant(open_phase) == "sym_trail"


# 48 / 47 before #6826; 44 (the unmodified replay's value) after.
@pytest.mark.parametrize(("radius", "best_progress"), [(10, 44.0), (40, 44.0)])
def test_failing_control_clearing_landing_copper_does_not_help(searches, radius, best_progress):
    """Remove every foreign raster cell within ``radius`` of the four pads."""
    p_net, n_net = _pair_nets(searches)
    pads = _pad_cells(searches)

    def keep(x, y, layer, net):
        near = np.zeros(len(x), dtype=bool)
        for px, py in pads:
            near |= (np.abs(x - px) <= radius) & (np.abs(y - py) <= radius)
        return ~(near & (net != p_net) & (net != n_net))

    [_, open_phase] = _replay(_filter_raster(searches, keep))
    assert open_phase["success"] is False
    assert open_phase["iteration_limited"] is True
    assert open_phase["best_progress"] == best_progress


def test_discriminating_control_fcu_landing_is_sealed_by_foreign_pads(searches):
    """On F.Cu alone the goal is unreachable; without pad records it is reached.

    Both runs get an unbounded budget, so this is reachability, not budget.
    The raster's pad halos and the authored pad-kernel records each seal it
    independently (dropping only one is not enough).
    """
    fcu = _with_route_args(
        searches, OPEN, routable_layers=[0], max_iterations_budget=0, timeout_seconds=0.0
    )
    [_, sealed] = _replay(fcu)
    assert sealed["success"] is False
    assert sealed["iteration_limited"] is False  # open set exhausted
    assert sealed["best_progress"] == 44.0

    [_, open_ok] = _replay(_bare(fcu))
    assert open_ok["success"] is True


def test_manifest_is_json_round_trippable():
    # Guards the hand-written fixture format against an accidental NaN or a
    # non-JSON value sneaking into a future re-capture.
    text = (FIXTURE / "manifest.json").read_text()
    assert json.loads(text)["pair"] == "TMDS_D1"
