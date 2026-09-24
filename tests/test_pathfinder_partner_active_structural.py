"""Deterministic structural guard for the #2715 dormant-partner optimization.

Issue #2715 threaded a pre-computed ``partner_active`` bool into
``Router._is_trace_blocked`` so the hot A* path no longer re-derives a
4-condition boolean tuple on every call::

    if partner_active is None:                  # pathfinder.py
        partner_active = (
            partner_net is not None
            and partner_net >= 0
            and partner_radius is not None
            and partner_radius < radius
        )

That property -- *"when the caller supplies ``partner_active``, the
4-condition expression is not evaluated"* -- is **structural**, and this
module asserts it structurally, by counting operations rather than
nanoseconds.

Why not a timing gate (issue #5708)
-----------------------------------
``tests/perf/test_pathfinder_dormant_partner_perf.py`` used to gate every
PR on a wall-clock ratio between the two callers.  It could not work, and
it was re-litigated twice (#3581, then #5708):

* the tuple costs **~8.0 ns/eval** and the optimized caller is
  **~6.3 ns/call** faster end-to-end,
* against a measured **~2 300 ns** per call of ``_is_trace_blocked``,
* i.e. the defended effect is **~0.26 %** of the measured quantity,
* while five back-to-back measurements on an *idle* host spanned
  **0.95x-1.35x (~+/-35 %)**, one of which already exceeded the 1.25x
  budget with no CI neighbours at all.

Signal-to-noise is roughly **1 : 130**, so no threshold value both
tolerates the noise and detects the regression -- which is why raising
the budget (1.05 -> 1.25 in #3581) bought eight weeks rather than a fix.

The probe below has no variance: it evaluates the same expression the
optimization skips and counts it exactly.  It fails immediately if the
``if partner_active is None:`` guard is removed.

Both halves of each assertion matter.  ``legacy_evals == 1`` is the
**anti-vacuity guard**: a test asserting only ``optimized_evals == 0``
passes trivially if the probe is mis-wired and never fires at all.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.rules import DesignRules

# Partner-net id used for the cell that the partner branch relaxes.
PARTNER_NET = 42
# Net id we "route as" -- distinct from PARTNER_NET so the cell is foreign.
ROUTED_NET = 1


class PartnerNetProbe(int):
    """An ``int`` that counts how often ``>=`` is evaluated against it.

    ``partner_net >= 0`` is the first (and, by short-circuit, only)
    partner-dependent comparison in the 4-condition tuple, and
    ``partner_net`` is referenced nowhere earlier in ``_is_trace_blocked``
    -- so the count of ``__ge__`` invocations is exactly the number of
    times the tuple was evaluated.

    ``calls`` is a *class* attribute so the count survives being passed
    through the router as a plain integer argument.
    """

    calls = 0

    def __ge__(self, other: int) -> bool:
        type(self).calls += 1
        return int.__ge__(self, other)


@pytest.fixture
def probe_router() -> Router:
    """A small grid with one blocked cell belonging to ``PARTNER_NET``.

    5 mm x 5 mm at 0.1 mm resolution (50x50 cells) -- built in
    milliseconds, unlike the perf module's 200x200 fixture.
    """
    rules = DesignRules(
        trace_width=0.25,
        trace_clearance=0.15,
        via_diameter=0.6,
    )
    grid = RoutingGrid(
        width=5.0,
        height=5.0,
        rules=rules,
        resolution_override=0.1,
    )
    grid._blocked[0, 10, 10] = True
    grid._net[0, 10, 10] = PARTNER_NET
    return Router(grid, rules)


def _blocked_counting_ge(router: Router, gx: int, gy: int, **kwargs: object) -> tuple[bool, int]:
    """Call ``_is_trace_blocked`` and report ``(verdict, tuple_evals)``."""
    PartnerNetProbe.calls = 0
    verdict = router._is_trace_blocked(gx, gy, 0, ROUTED_NET, False, **kwargs)  # type: ignore[arg-type]
    return verdict, PartnerNetProbe.calls


def test_probe_counts_ge_evaluations() -> None:
    """Meta-guard: the probe itself must count, and must not alter results.

    Without this, a future edit that breaks ``PartnerNetProbe`` would turn
    every assertion below into a vacuous ``0 == 0``.
    """
    probe = PartnerNetProbe(-1)

    PartnerNetProbe.calls = 0
    assert (probe >= 0) is False
    assert PartnerNetProbe.calls == 1

    PartnerNetProbe.calls = 0
    assert (PartnerNetProbe(7) >= 0) is True
    assert PartnerNetProbe.calls == 1

    # Untouched comparisons must not inflate the count.
    PartnerNetProbe.calls = 0
    assert probe is not None
    assert int(probe) == -1
    assert PartnerNetProbe.calls == 0


def test_dormant_partner_caller_skips_condition_evaluation(probe_router: Router) -> None:
    """#2715's property, dormant case: no partner configured.

    * legacy caller (no ``partner_active``): the tuple is evaluated once;
    * optimized caller (``partner_active=False``): it is not evaluated at
      all;
    * both produce the same verdict, so the optimization is a pure
      elision and not a behaviour change.
    """
    radius = probe_router._trace_half_width_cells
    # Far from the blocked partner cell at (10, 10): an ordinary,
    # unobstructed routing site -- the common dormant case.
    gx, gy = 30, 30

    legacy_verdict, legacy_evals = _blocked_counting_ge(
        probe_router,
        gx,
        gy,
        radius=radius,
        partner_net=PartnerNetProbe(-1),
        partner_radius=None,
    )
    optimized_verdict, optimized_evals = _blocked_counting_ge(
        probe_router,
        gx,
        gy,
        radius=radius,
        partner_net=PartnerNetProbe(-1),
        partner_radius=None,
        partner_active=False,
    )

    # Anti-vacuity: the legacy path really does evaluate the tuple.
    assert legacy_evals == 1, (
        "Probe never fired on the legacy dormant path -- the structural "
        "assertion below would be vacuous.  Either the 4-condition tuple "
        "moved/changed, or _is_trace_blocked now returns before reaching "
        "it for this query point."
    )
    # The property under test (issue #2715).
    assert optimized_evals == 0, (
        f"Caller-supplied partner_active=False did not skip the "
        f"4-condition tuple: it was evaluated {optimized_evals}x.  The "
        f"`if partner_active is None:` guard in "
        f"Router._is_trace_blocked has regressed (issue #2715/#5708)."
    )
    assert legacy_verdict == optimized_verdict


def test_active_partner_condition_is_live(probe_router: Router) -> None:
    """The probed expression is *live*: its value changes the verdict.

    Proves the probe is wired to a decision that matters, rather than to
    a path that is dead in both configurations:

    * with no partner relaxation the foreign partner-net cell **blocks**;
    * deriving ``partner_active`` from the kwargs (legacy caller, one
      tuple evaluation) relaxes it -- the verdict flips to unblocked;
    * supplying ``partner_active=True`` reproduces that same verdict with
      **zero** tuple evaluations.
    """
    # Partner cell (10, 10); query 3 cells away.  partner_radius=1 <
    # radius=4, so the cell sits in the slack ring: blocked without
    # partner relaxation, passable with it.
    gx, gy = 10, 13
    radius, partner_radius = 4, 1

    dormant_verdict, dormant_evals = _blocked_counting_ge(
        probe_router,
        gx,
        gy,
        radius=radius,
        partner_net=PartnerNetProbe(-1),
        partner_radius=None,
        partner_active=False,
    )
    derived_verdict, derived_evals = _blocked_counting_ge(
        probe_router,
        gx,
        gy,
        radius=radius,
        partner_net=PartnerNetProbe(PARTNER_NET),
        partner_radius=partner_radius,
    )
    supplied_verdict, supplied_evals = _blocked_counting_ge(
        probe_router,
        gx,
        gy,
        radius=radius,
        partner_net=PartnerNetProbe(PARTNER_NET),
        partner_radius=partner_radius,
        partner_active=True,
    )

    # The expression is live: deriving it True flips the verdict.
    assert dormant_verdict is True, (
        "Expected the foreign partner-net cell to block without partner "
        "relaxation -- the fixture no longer exercises the branch."
    )
    assert derived_verdict is False, (
        "Expected partner relaxation to unblock the slack-ring cell; the "
        "4-condition tuple did not evaluate True."
    )
    assert dormant_evals == 0
    # Anti-vacuity again: exactly one evaluation on the deriving caller.
    assert derived_evals == 1
    # The optimization: same verdict, zero evaluations.
    assert supplied_verdict == derived_verdict
    assert supplied_evals == 0
