"""Issue #4635: the #4580 crossover census is state-neutral but not budget-neutral.

PR #4611 established that the ``KCT_CROSSTAIL_CENSUS=1`` sweep cannot perturb
router STATE -- every gate it calls past the accept point is mutation-free.  It
does, however, spend real wall clock inside the window ``spec_t0`` opens in
``route_differential_pair_coupled``, and every downstream deadline in that
window is computed as ``<budget> - (now - spec_t0)``.  So a census-on run could
differ from a census-off run through **budget pressure alone**, with no state
mutation anywhere -- which is exactly the confound the census exists to remove.

The fix credits the census's INCREMENTAL cost (the sweep after the first legal
candidate, i.e. the part the un-instrumented first-legal loop would never have
paid) back to that window via ``DiffPairRouter._census_elapsed_s``.

These tests pin four things:

1. With the census OFF the credit is exactly ``0.0``, so the default path's
   timing arithmetic is bit-identical.
2. With the census ON a crossover whose first legal candidate is not the last
   one accrues a positive credit -- and a crossover with NO legal candidate
   accrues zero (both modes scanned the whole lattice).
3. All four deadline sites subtract the credit, including the two that are
   reachable with the census ON and shadow construction OFF.
4. The cost is reported in the ``[crosstail-census]`` header, appended after
   the pre-existing fields so their names and order are untouched.

Deliberately kept in its own module: ``tests/test_diffpair_shadow.py`` owns the
#4580 census block and is not modified by this change; its fixtures are reused
here by import (the convention used by
``tests/test_cli_route_complete_localize_4472.py``).
"""

from __future__ import annotations

import re
import time as _real_time

import pytest

from kicad_tools.router.diffpair_routing import DiffPairRouter
from tests.test_diffpair_coupled_instrumentation_4459 import _two_pad_router_and_pair
from tests.test_diffpair_shadow import (
    _crossing_router,
    _crossing_tail,
    _CrossingPathfinder,
    _register_unrouted_neighbour,
    _tail_pads,
)

_HEADER_FIELDS = re.compile(
    r"\[crosstail-census\] net=(?P<net>\S+) "
    r"head=\((?P<head>[^)]*)\) goal=\((?P<goal>[^)]*)\) "
    r"legal=(?P<legal>\d+)/(?P<total>\d+) "
    r"distinct_v1=(?P<distinct>\d+) "
    r"census_s=(?P<census_s>[0-9.]+)$"
)


def _census_on(monkeypatch) -> None:
    import kicad_tools.router.diffpair_routing as dpr_mod

    monkeypatch.setattr(dpr_mod, "_CROSSTAIL_CENSUS", True)


def _census_header(capsys) -> str:
    lines = [
        line.strip()
        for line in capsys.readouterr().out.splitlines()
        if "[crosstail-census]" in line
    ]
    assert lines, "the census must actually have run"
    return lines[0]


class _SealedPathfinder(_CrossingPathfinder):
    """Every cell blocked: the census finds ``legal=0`` out of the whole lattice.

    This is the saturated extreme.  The un-instrumented loop also scans all 225
    candidates here (it never finds one to return on), so the census costs
    nothing extra and must credit nothing.
    """

    def _is_cell_blocked(self, gx: int, gy: int, layer_idx: int, net: int) -> bool:
        return True


class _FakeClock:
    """A hand-advanced stand-in for the ``time`` module inside the router.

    The budget arithmetic under test is exact float arithmetic on
    ``time.monotonic()``; asserting it against real wall clock would flake.
    Installed with ``monkeypatch.setattr(dpr_mod, "time", clock)`` so only
    ``diffpair_routing``'s module-global name is replaced.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += float(dt)

    def __getattr__(self, name: str):  # pragma: no cover - delegation safety net
        return getattr(_real_time, name)


# ---------------------------------------------------------------------------
# The accumulator: what gets charged, and what does not
# ---------------------------------------------------------------------------


def test_census_off_credits_exactly_zero():
    """The load-bearing safety property: default-off arithmetic is unchanged.

    ``_census_elapsed_s`` must be exactly ``0.0`` -- not "small" -- so every
    ``- (now - spec_t0 - self._census_elapsed_s)`` term is bit-identical to the
    pre-#4635 ``- (now - spec_t0)``.
    """
    dpr = _crossing_router()
    _register_unrouted_neighbour(dpr)

    assert _crossing_tail(dpr) is not None
    assert dpr._census_elapsed_s == 0.0
    assert isinstance(dpr._census_elapsed_s, float)


def test_census_on_credits_the_post_first_legal_sweep(monkeypatch, capsys):
    """An open lattice finds its first legal candidate early, so the sweep costs.

    This is the case the issue is about: 224 further candidates get validated
    purely for the measurement, inside the shadow phase's budget.
    """
    _census_on(monkeypatch)
    dpr = _crossing_router()

    tail = _crossing_tail(dpr)

    assert tail is not None
    header = _census_header(capsys)
    match = _HEADER_FIELDS.search(header)
    assert match is not None, header
    # Non-vacuity: the first legal candidate must not be the last one, or there
    # would be no incremental sweep to charge for.
    assert int(match.group("legal")) > 1
    assert dpr._census_elapsed_s > 0.0


def test_census_credits_the_value_it_reports(monkeypatch, capsys):
    """The reported ``census_s`` IS the credit -- not an independent estimate."""
    _census_on(monkeypatch)
    dpr = _crossing_router()

    assert _crossing_tail(dpr) is not None

    match = _HEADER_FIELDS.search(_census_header(capsys))
    assert match is not None
    assert dpr._census_elapsed_s == pytest.approx(float(match.group("census_s")), abs=5e-5)


def test_no_legal_candidate_credits_zero(monkeypatch, capsys):
    """A saturated crossover scanned the whole lattice in BOTH modes.

    Charging the whole census loop -- the naive fix -- would hand this pair a
    budget extension it never earned.  Only the post-first-legal sweep is
    incremental, and here there is none.
    """
    _census_on(monkeypatch)
    dpr = _crossing_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))

    tail = dpr._synthesize_crossing_tail(_SealedPathfinder(), head, goal, 0, [])

    assert tail is None
    match = _HEADER_FIELDS.search(_census_header(capsys))
    assert match is not None
    assert int(match.group("legal")) == 0, "fixture must be saturated for this to be meaningful"
    assert int(match.group("total")) == 225
    assert dpr._census_elapsed_s == 0.0


# ---------------------------------------------------------------------------
# The header: new field appended, existing fields untouched
# ---------------------------------------------------------------------------


def test_header_appends_census_s_after_the_existing_fields(capsys):
    """Field names and order are a parsed interface -- ``census_s`` goes LAST."""
    head, goal = _tail_pads((1.0, 2.0), (4.0, 2.0))

    DiffPairRouter._report_crossing_tail_census(
        head, goal, [(3, 7, 0.25, (1.5, 2.5), (3.5, 2.5))], 225, 0.0125
    )

    match = _HEADER_FIELDS.search(_census_header(capsys))
    assert match is not None, "the header field names/order must be unchanged"
    assert match.group("legal") == "1"
    assert match.group("total") == "225"
    assert match.group("distinct") == "1"
    assert float(match.group("census_s")) == pytest.approx(0.0125)


def test_report_stays_callable_without_the_cost_argument(capsys):
    """Back-compat: existing direct callers pass four positional arguments."""
    head, goal = _tail_pads((1.0, 2.0), (4.0, 2.0))

    DiffPairRouter._report_crossing_tail_census(head, goal, [], 225)

    match = _HEADER_FIELDS.search(_census_header(capsys))
    assert match is not None
    assert float(match.group("census_s")) == 0.0


# ---------------------------------------------------------------------------
# The budget: all four deadline sites credit the census back
# ---------------------------------------------------------------------------

_CREDIT = 3.0
_PROBE_COST = 10.0


def _guide_route():
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    guide = Route(net=1, net_name="USB_D+")
    guide.segments.append(
        Segment(x1=5.0, y1=4.8, x2=25.0, y2=4.8, width=0.2, layer=Layer.F_CU, net=1)
    )
    return guide


def _install_clock(monkeypatch) -> _FakeClock:
    import kicad_tools.router.diffpair_routing as dpr_mod

    clock = _FakeClock()
    monkeypatch.setattr(dpr_mod, "time", clock)
    return clock


def _probe_deadlines(monkeypatch, *, shadow: bool, credit: float) -> dict[str, list[float]]:
    """Drive one coupled spec and record every deadline the budget arithmetic hands out.

    ``credit`` is injected as the census's accrued cost during the FIRST guide
    probe -- i.e. after ``route_differential_pair_coupled`` resets the
    accumulator for this spec, exactly where a real census would accrue it.
    """
    import kicad_tools.router.diffpair_routing as dpr_mod

    router, pair = _two_pad_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = shadow
    clock = _install_clock(monkeypatch)
    seen: dict[str, list[float]] = {"probe": [], "bias": [], "coupled": []}

    def _guide(*_a, **kwargs):
        seen["probe"].append(kwargs["per_net_timeout"])
        dpr._census_elapsed_s = credit
        clock.advance(_PROBE_COST)
        return _guide_route()

    monkeypatch.setattr(dpr, "_single_ended_guide_route", _guide)
    monkeypatch.setattr(dpr, "_shadow_route_pair", lambda *a, **k: None)

    def _biased(*a, **_k):
        seen["bias"].append(a[8])
        return None

    monkeypatch.setattr(dpr, "_shadow_with_guide_bias", _biased)

    class _StubPathfinder:
        last_timeout_exceeded = False
        last_iteration_limited = False
        last_iterations = 0
        last_best_progress = float("inf")
        last_best_state = None
        last_best_node = None
        last_coupled_backend = "python"
        last_rejections: dict[str, int] = {}

        def route_coupled(self, *_a, **kwargs):
            seen["coupled"].append(kwargs.get("timeout_seconds"))
            return None

    monkeypatch.setattr(dpr_mod, "CoupledPathfinder", lambda *a, **k: _StubPathfinder())

    dpr.route_differential_pair_coupled(pair, coupled_only=True, per_pair_timeout=60.0)
    return seen


def test_shadow_probe_deadlines_credit_the_census(monkeypatch):
    """Sites ``n_probe_timeout`` and ``bias_timeout`` (shadow ON).

    Both are ``_SHADOW_PER_PAIR_BUDGET_S - (now - spec_t0)``.  With the census
    credited, a census-on spec hands the swapped probe and the guide-biased
    re-route exactly the deadlines a census-off spec would.
    """
    baseline = _probe_deadlines(monkeypatch, shadow=True, credit=0.0)
    credited = _probe_deadlines(monkeypatch, shadow=True, credit=_CREDIT)

    assert len(baseline["probe"]) >= 2, "the swapped N probe must have run"
    assert baseline["bias"], "the guide-biased re-route must have run"
    # The FIRST probe is bounded before any census time can accrue.
    assert credited["probe"][0] == baseline["probe"][0]
    # Every later deadline is longer by exactly the credit.
    assert credited["probe"][1] == pytest.approx(baseline["probe"][1] + _CREDIT)
    assert credited["bias"][0] == pytest.approx(baseline["bias"][0] + _CREDIT)
    # Non-vacuity: the deadlines are genuinely budget-limited, not clamped to
    # ``probe_timeout`` (a clamp would hide the credit).
    assert baseline["probe"][1] < baseline["probe"][0]


def test_corridor_and_open_deadlines_credit_the_census(monkeypatch):
    """Sites ``corridor_budget`` and ``remaining_budget`` (shadow OFF).

    ``_CROSSTAIL_CENSUS`` is independent of ``enable_shadow_construction``, so
    a census-on / shadow-off run pressures these two.  Leaving them uncorrected
    would be a partial fix that is harder to reason about than none.
    """
    baseline = _probe_deadlines(monkeypatch, shadow=False, credit=0.0)
    credited = _probe_deadlines(monkeypatch, shadow=False, credit=_CREDIT)

    assert len(baseline["coupled"]) == 2, "corridor attempt then open fallback"
    corridor_base, open_base = baseline["coupled"]
    corridor_credited, open_credited = credited["coupled"]
    # Non-vacuity: both budgets are the remaining-time expression, not a floor.
    assert corridor_base == pytest.approx(60.0 * 0.5 - _PROBE_COST)
    assert open_base == pytest.approx(60.0 - _PROBE_COST)
    assert corridor_credited == pytest.approx(corridor_base + _CREDIT)
    assert open_credited == pytest.approx(open_base + _CREDIT)


def test_logged_pair_wall_clock_is_not_census_adjusted(monkeypatch, caplog):
    """``spec_elapsed`` stays the TRUE elapsed time.

    Crediting the budget means a census-on pair may exceed
    ``_SHADOW_PER_PAIR_BUDGET_S`` by the census's own cost.  That is the
    deliberate trade; hiding it in the timing log would defeat the point.
    """
    import logging

    with caplog.at_level(logging.INFO, logger="kicad_tools.router.diffpair_routing"):
        _probe_deadlines(monkeypatch, shadow=False, credit=_CREDIT)

    timing = [rec for rec in caplog.records if "diffpair coupled timing" in rec.getMessage()]
    assert timing, "the per-pair timing line must still be logged"
    # The fake clock advanced exactly ``_PROBE_COST`` during the spec, and the
    # credit must NOT have been subtracted from the reported elapsed time.
    assert f"elapsed={_PROBE_COST:.2f}s" in timing[-1].getMessage()
