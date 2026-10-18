"""Golden-table parity for the negotiated-loop budget selectors (issue #5802).

``fixtures/negotiated_budget_golden.json`` was recorded from the ten original
``Autorouter`` methods at the merge-base, *before* they were extracted into
:mod:`kicad_tools.router.negotiated_budget`.  Both the extracted functions and
the ``Autorouter`` delegators must reproduce it exactly (floats with ``==``,
strings byte-for-byte).  The grid covers the wall-clock arms, which no routed
board hash can make deterministic.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest import mock

import pytest

from kicad_tools.router import core
from kicad_tools.router import negotiated_budget as nb
from kicad_tools.router.core import Autorouter

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "negotiated_budget_golden.json").read_text()
)["records"]

_CLOCK = 1000.0  # the frozen clock the golden table was recorded under

_METHODS = {
    "active_expansion_cap": "_active_expansion_cap",
    "wall_clock_free_arm": "_wall_clock_free_arm",
    "escape_budget": "_escape_budget",
    "wall_clock_arm_line": "_wall_clock_arm_line",
    "relief_subsearch_budget": "_relief_subsearch_budget",
    "relief_reland_retry_budget": "_relief_reland_retry_budget",
    "relief_subsearch_bound_line": "_relief_subsearch_bound_line",
    "relief_txn_bound_line": "_relief_txn_bound_line",
    "post_negotiation_sweep_bounds": "_post_negotiation_sweep_bounds",
    "post_negotiation_sweep_bound_line": "_post_negotiation_sweep_bound_line",
}


class _Stub:
    """Binds only the delegators; carries just the two cap attributes."""

    def __init__(self, per_net_iterations: int, max_search_iterations: int) -> None:
        self._per_net_iterations = per_net_iterations
        self._max_search_iterations = max_search_iterations


for _method in _METHODS.values():
    setattr(_Stub, _method, getattr(Autorouter, _method))


def _via_delegator(rec: dict) -> object:
    a = rec["args"]
    stub = _Stub(a["per_net_iterations"], a["max_search_iterations"])
    m = getattr(stub, _METHODS[rec["fn"]])
    fn = rec["fn"]
    if fn == "active_expansion_cap":
        return m()
    if fn in ("wall_clock_free_arm", "wall_clock_arm_line"):
        return m(a["deterministic_rescue"])
    if fn == "escape_budget":
        return m(a["timeout"], a["start_time"], a["deterministic_rescue"])
    if fn in ("relief_subsearch_budget", "relief_subsearch_bound_line"):
        return m(a["per_net_timeout"], a["deterministic_rescue"])
    if fn == "relief_reland_retry_budget":
        return m(a["per_net_timeout"], a["subsearch_budget"])
    if fn == "relief_txn_bound_line":
        return m(a["timeout"])
    return m(a["timeout"], a["per_net_timeout"], a["elapsed"], a["timed_out"])


def _via_function(rec: dict) -> object:
    a = rec["args"]
    fn = rec["fn"]
    cap = nb.active_expansion_cap(a["per_net_iterations"], a["max_search_iterations"])
    if fn == "active_expansion_cap":
        return cap
    if fn == "wall_clock_free_arm":
        return nb.wall_clock_free_arm(cap, a["deterministic_rescue"])
    if fn == "wall_clock_arm_line":
        return nb.wall_clock_arm_line(cap, a["deterministic_rescue"])
    if fn == "escape_budget":
        return nb.escape_budget(cap, a["timeout"], a["start_time"], a["deterministic_rescue"])
    if fn == "relief_subsearch_budget":
        return nb.relief_subsearch_budget(cap, a["per_net_timeout"], a["deterministic_rescue"])
    if fn == "relief_subsearch_bound_line":
        budget = nb.relief_subsearch_budget(cap, a["per_net_timeout"], a["deterministic_rescue"])
        return nb.relief_subsearch_bound_line(budget, a["deterministic_rescue"])
    if fn == "relief_reland_retry_budget":
        return nb.relief_reland_retry_budget(cap, a["per_net_timeout"], a["subsearch_budget"])
    if fn == "relief_txn_bound_line":
        return nb.relief_txn_bound_line(a["timeout"])
    bounds = nb.post_negotiation_sweep_bounds(
        cap, a["timeout"], a["per_net_timeout"], a["elapsed"], a["timed_out"]
    )
    if fn == "post_negotiation_sweep_bounds":
        return bounds
    return nb.post_negotiation_sweep_bound_line(
        bounds[0], bounds[1], a["timeout"], a["per_net_timeout"]
    )


def _normalise(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


def test_golden_table_is_complete() -> None:
    assert {r["fn"] for r in GOLDEN} == set(_METHODS)
    assert len(GOLDEN) == 1028


@pytest.mark.parametrize("impl", [_via_function, _via_delegator], ids=["functions", "delegators"])
def test_matches_golden_table(impl) -> None:
    mismatches = []
    with mock.patch("time.time", lambda: _CLOCK):
        for rec in GOLDEN:
            got = _normalise(impl(rec))
            if got != rec["result"]:
                mismatches.append((rec["fn"], rec["args"], rec["result"], got))
    assert not mismatches, mismatches[:3]


def test_moved_constants_still_importable_from_core() -> None:
    for name in (
        "RELIEF_SUBSEARCH_BUDGET_S",
        "RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S",
        "RELIEF_RESCUE_TXN_FRACTION",
        "RELIEF_RESCUE_TXN_FLOOR_S",
    ):
        assert getattr(core, name) is getattr(nb, name)


def test_delegators_keep_original_signatures() -> None:
    expected = {
        "_active_expansion_cap": "(self) -> 'int'",
        "_wall_clock_free_arm": "(self, deterministic_rescue: 'bool') -> 'bool'",
        "_escape_budget": "(self, timeout: 'float | None', start_time: 'float', "
        "deterministic_rescue: 'bool') -> 'float | None'",
        "_wall_clock_arm_line": "(self, deterministic_rescue: 'bool') -> 'str'",
        "_relief_subsearch_budget": "(self, per_net_timeout: 'float | None', "
        "deterministic_rescue: 'bool') -> 'float | None'",
        "_relief_reland_retry_budget": "(self, per_net_timeout: 'float | None', "
        "subsearch_budget: 'float | None') -> 'float | None'",
        "_relief_subsearch_bound_line": "(self, per_net_timeout: 'float | None', "
        "deterministic_rescue: 'bool') -> 'str'",
        "_relief_txn_bound_line": "(self, timeout: 'float | None') -> 'str'",
        "_post_negotiation_sweep_bounds": "(self, timeout: 'float | None', "
        "per_net_timeout: 'float | None', elapsed: 'float', timed_out: 'bool') "
        "-> 'tuple[float, float | None]'",
        "_post_negotiation_sweep_bound_line": "(self, timeout: 'float | None', "
        "per_net_timeout: 'float | None', elapsed: 'float', timed_out: 'bool') -> 'str'",
    }
    for name, sig in expected.items():
        assert str(inspect.signature(getattr(Autorouter, name))) == sig, name
