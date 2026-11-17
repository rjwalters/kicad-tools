"""Replay the other Board07 pairs whose coupled search exhausts its budget (Issue #6852).

``test_coupled_search_replay_board07.py`` replays TMDS_D1 (issue #6816).  With
``--differential-pairs`` on, six of Board07's seven pairs hit the 20,000-
iteration phase cap; this module replays the five that file does not cover.
Each ``tests/fixtures/board07_coupled_<pair>/`` was written by::

    uv run python boards/07-matchgroup-test/capture_coupled_search.py \\
        --pair <PAIR> --fixture-dir tests/fixtures/board07_coupled_<pair>

(main @ ``76601f66``, native ``BUILD_VERSION`` 59, ``PYTHONHASHSEED=42``).  The
captures predate ``set_partner_via_clearance`` only in the sense that the
recorder does not log it, so the replay re-derives it exactly as the TMDS_D1
module does.

What the bounded controls on identical inputs showed (open phase, 20,000-
iteration budget unless noted):

===========  ========  =======  =========  ==========  =========  ========
pair         dominant  best     budget     empty grid  empty grid  hw=3.0
                       progress lifted to  at budget   lifted to   at budget
===========  ========  =======  =========  ==========  =========  ========
TMDS_D1      sym_trail 44       132,899    stalls (3)  48,031     44
DQS          via_own   10       1,689,940  stalls (3)  180,433    8
MIPI_DAT0    via_own   125      221,955    12,523 ok   12,523     0
MIPI_DAT1    via_own   116      285,538    stalls (8)  66,806     116
TMDS_D0      sym_trail 24       1,097,459  stalls (3)  48,359     24
TMDS_D2      sym_trail 67       no, >2M    stalls (3)  47,785     64
===========  ========  =======  =========  ==========  =========  ========

* Raising the budget alone couples five of the six, but needs 6x to 85x the
  phase cap (TMDS_D2 does not couple even at 2,000,000 iterations).  That is
  the lever #3921 already rejected on board 06, and these numbers agree.
* The joint A* is expensive even in free space: with no copper and no pad
  records, four of the six still miss the cap and need 48k-180k iterations.
  Only MIPI_DAT0 couples in free space within the cap (12,523).
* A weighted heuristic (``heuristic_weight`` 1.5 - 5) does not couple any pair
  at the cap, so the stall is not f-plateau tie-breaking.
* Zeroing the endpoint approach/departure radii ends the search in 1-4
  iterations (``asym_spacing_*``); doubling them or dropping the target
  spacing by one cell does not couple any pair either.

No router change is supported by this evidence, so none is made: the tests
below pin the baseline and the controls so a future router change can be
measured against them.  They are diagnostics, not fixes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from tests.test_coupled_search_replay_board07 import (
    OPEN,
    _bare,
    _dominant,
    _is_route,
    _replay,
    _route_arg,
    _with_partner_via_clearance,
    _with_route_args,
)

pytestmark = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not built -- run `uv run kct build-native`",
)

FIXTURES = Path(__file__).parent / "fixtures"

# pair -> (class, best progress at the cap, dominant rejection) of the
# unmodified open phase, as captured and replayed.
BASELINE = {
    "DQS": ("landing-stall", 10.0, "via_own_h2h"),
    "MIPI_DAT0": ("joint-A*-plateau", 125.0, "via_own_h2h"),
    "MIPI_DAT1": ("joint-A*-plateau", 116.0, "via_own_h2h"),
    "TMDS_D0": ("landing-stall", 24.0, "sym_trail"),
    "TMDS_D2": ("joint-A*-plateau", 67.0, "sym_trail"),
}
PAIRS = list(BASELINE)


def _dirname(pair: str) -> Path:
    return FIXTURES / f"board07_coupled_{pair.lower()}"


@pytest.fixture(scope="module")
def loaded():
    from kicad_tools.router.coupled_capture import load_fixture

    return {pair: load_fixture(_dirname(pair)) for pair in PAIRS}


@pytest.fixture(scope="module")
def searches(loaded):
    out = {}
    for pair, fx in loaded.items():
        last = max(i for i, c in enumerate(fx.calls) if _is_route(c))
        out[pair] = _with_partner_via_clearance(fx.calls[: last + 1])
    return out


def _with_heuristic_weight(calls: list[dict], weight: float) -> list[dict]:
    out = []
    for call in calls:
        if call["op"] == "new" and call["type"] == "CoupledPathfinder":
            call = {**call, "args": [*call["args"][:-1], weight]}
        out.append(call)
    return out


@pytest.mark.parametrize("pair", PAIRS)
def test_capture_shows_the_measured_exhaustion(loaded, pair):
    fx = loaded[pair]
    assert fx.manifest["pair"] == pair
    assert "--differential-pairs" in fx.manifest["provenance"]["argv"]
    assert fx.manifest["provenance"]["pythonhashseed"] == "42"
    klass, _, _ = BASELINE[pair]
    assert [c["class"] for c in fx.manifest["classification"]] == [klass]
    for phase in fx.manifest["phases"]:
        diag = phase["native_diagnostics"]
        assert diag["success"] is False
        assert diag["iteration_limited"] is True
        assert diag["iterations"] == phase["python_kwargs"]["max_iterations_budget"] == 20000
    size = sum(p.stat().st_size for p in _dirname(pair).iterdir())
    assert size <= 5_000_000
    json.loads((_dirname(pair) / "manifest.json").read_text())


@pytest.mark.parametrize("pair", PAIRS)
def test_replay_reproduces_the_budget_exit(searches, pair):
    _, best, dominant = BASELINE[pair]
    open_phase = _replay(searches[pair])[-1]
    assert open_phase["success"] is False
    assert open_phase["iteration_limited"] is True
    assert open_phase["iterations"] == 20000
    assert open_phase["best_progress"] == best
    assert _dominant(open_phase) == dominant
    assert _replay(searches[pair])[-1] == open_phase  # deterministic


# Iterations the open phase needs once the budget is lifted (None: not within
# 2,000,000).  Only the cheap ones run here; DQS (1,689,940, ~75 s), TMDS_D0
# (1,097,459, ~33 s) and TMDS_D2 (none in 2M, ~140 s) are recorded in the
# module docstring from the same replay.
LIFTED = {"MIPI_DAT0": 221_955, "MIPI_DAT1": 285_538}


@pytest.mark.parametrize(("pair", "iterations"), LIFTED.items())
def test_passing_control_budget_alone_couples_the_pair(searches, pair, iterations):
    lifted = _with_route_args(
        searches[pair], OPEN, max_iterations_budget=1_000_000, timeout_seconds=0.0
    )
    open_phase = _replay(lifted)[-1]
    assert open_phase["success"] is True
    assert open_phase["iterations"] == iterations
    assert iterations > 10 * 20000  # an order of magnitude past the phase cap


# Empty grid, budget lifted: iterations to couple.
BARE_LIFTED = {
    "DQS": 180_433,
    "MIPI_DAT0": 12_523,
    "MIPI_DAT1": 66_806,
    "TMDS_D0": 48_359,
    "TMDS_D2": 47_785,
}


@pytest.mark.parametrize(("pair", "iterations"), BARE_LIFTED.items())
def test_control_free_space_cost(searches, pair, iterations):
    """Even with nothing on the grid the joint search needs far more than the cap.

    MIPI_DAT0 is the exception: it couples in free space within the cap, so
    its blocker is board copper rather than search cost.
    """
    bare = _bare(searches[pair])
    at_budget = _replay(bare)[-1]
    assert at_budget["success"] is (pair == "MIPI_DAT0")
    lifted = _replay(
        _with_route_args(bare, OPEN, max_iterations_budget=400_000, timeout_seconds=0.0)
    )[-1]
    assert lifted["success"] is True
    assert lifted["iterations"] == iterations


@pytest.mark.parametrize("weight", [1.5, 3.0, 5.0])
@pytest.mark.parametrize("pair", PAIRS)
def test_failing_control_weighted_heuristic_does_not_couple(searches, pair, weight):
    open_phase = _replay(_with_heuristic_weight(searches[pair], weight))[-1]
    assert open_phase["success"] is False
    assert open_phase["iteration_limited"] is True


@pytest.mark.parametrize("pair", PAIRS)
def test_failing_control_endpoint_radius_variants_do_not_couple(searches, pair):
    calls = searches[pair]
    approach = _route_arg(calls, OPEN, "effective_approach_radius")
    departure = _route_arg(calls, OPEN, "effective_departure_radius")
    spacing = _route_arg(calls, OPEN, "effective_target_spacing")

    zero = _replay(
        _with_route_args(calls, OPEN, effective_approach_radius=0, effective_departure_radius=0)
    )[-1]
    assert zero["success"] is False
    assert zero["iterations"] < 10  # every first move is refused: asym_spacing_*
    assert _dominant(zero) == "asym_spacing_n"

    for overrides in (
        {
            "effective_approach_radius": approach * 2,
            "effective_departure_radius": departure * 2,
        },
        {"effective_target_spacing": max(1, spacing - 1)},
    ):
        variant = _replay(_with_route_args(calls, OPEN, **overrides))[-1]
        assert variant["success"] is False
        assert variant["iteration_limited"] is True
