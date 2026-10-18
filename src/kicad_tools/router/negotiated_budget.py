"""Deterministic-vs-wall-clock budget selectors for the negotiated loop.

Issue #5802 (god-class slice 1).  These were ten private methods on
:class:`~kicad_tools.router.core.Autorouter`; none of them reads or writes
router state beyond the two expansion-cap integers, so they live here as plain
functions that take the active expansion cap as an explicit ``cap`` argument.
``Autorouter`` keeps every original method name and signature as a thin
delegator, so no call site changes and routing behaviour is byte-identical.

This module must stay engine-free: it may not import
``kicad_tools.router.core`` (or the lattice / mesh / diff-pair engines), which
``tests/router/test_negotiated_budget_boundary.py`` enforces.  It uses
``import time`` / ``time.time()`` (not ``from time import time``) so that tests
patching the shared ``time`` module still reach it.
"""

from __future__ import annotations

import time

from .algorithms.negotiated import (
    POST_NEGOTIATION_SWEEP_BUDGET_S,
    POST_NEGOTIATION_SWEEP_PER_NET_S,
    POST_NEGOTIATION_SWEEP_PER_NET_SAFETY_BACKSTOP_S,
    POST_NEGOTIATION_SWEEP_SAFETY_BACKSTOP_S,
)

# Issue #4536: wall-clock budget for the relief rescue's SUB-searches (the
# min-conflict probe rounds and the displaced-victim re-land passes inside
# :meth:`Autorouter._relief_rescue`).  This is the historical value and is
# LOAD-BEARING for routed reach on every run that is NOT expansion-capped -- a
# victim that fails to re-land within the budget rolls the whole transaction
# back (no-net-loss guarantee), so the value decides whether the rescued net is
# kept or dropped.
#
# On board-06 seed-42 the natural victim re-land time sits in the 8-12 s band on
# GitHub's ubuntu runners, i.e. this 10 s value STRADDLES it: the same commit
# reached 21/21 on one runner and 20/21 on another purely on machine speed (CI
# runs 31214686048 vs 31213011136 are line-identical up to the MIPI_RST rescue
# and then diverge on whether MIPI_CLK- re-lands).
#
# ``deterministic_rescue=True`` swaps this wall clock for the per-net
# node-expansion cap whenever one is active (``--deterministic-budget`` /
# ``per_net_iterations``), which takes machine speed out of the rescue's
# commit/roll-back decision.  Issue #4730 tried making that the fleet default
# and MEASURED A REGRESSION -- see ``DETERMINISTIC_RESCUE_DEFAULT`` -- so this
# value remains the default bound everywhere except callers that opt in
# explicitly (board 06).
RELIEF_SUBSEARCH_BUDGET_S = 10.0

# Issue #4536: the wall clock kept under ``deterministic_rescue=True``.  It is
# deliberately ~10x the measured natural sub-search time (a per-net search that
# exhausts the 1M-expansion cap costs ~11 s on a CI runner), so it is NOT
# load-bearing -- the binding bound is the node-expansion cap.  It exists only
# to keep the one path that the expansion cap does not price in seconds (a
# pure-Python A* fallback, 10-100x slower than the C++ backend) from grinding
# for minutes inside a rescue, per the #3989 lesson.
RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S = 120.0

# Issue #4781: proportional bound on a single relief-rescue TRANSACTION.  The
# sub-search budgets above bound each stage of a rescue (probe rounds, victim
# re-lands, nested rescues), but their PRODUCT is unbounded -- the #3413 defect
# one level up.  Measured on board 07 (#4770, CI run 31281260812): one ``DQ3``
# transaction spent 279.1 s -- 46% of the whole 600 s stage -- ripping 9
# blockers and then rolling back verbatim, and the rip-up iteration that
# produces the board's 26th net is the one that time starved.
#
# The bound: a transaction (INCLUDING its depth-1 nested rescues, which share
# the parent's allowance -- the parent's commit gate depends on them, so a
# separate allowance would just rebuild the unbounded product) may spend at
# most ``max(RELIEF_RESCUE_TXN_FRACTION * remaining, RELIEF_RESCUE_TXN_FLOOR_S)``
# where ``remaining = deadline - transaction_start`` is the stage budget left
# when the transaction begins.  It is evaluated ONLY at the existing
# ``_past_deadline()`` check sites (top of each probe round, per victim
# re-land, before each nested rescue) -- never inside the A* sub-searches --
# so a single sub-search can still legally overshoot the allowance by up to
# one sub-search budget (120 s worst case under ``deterministic_rescue``).
# On breach the transaction rolls back verbatim via the existing
# ``_rollback()``; aborting early is free by construction (the board is
# restored either way) -- it can only cost rescues that would have succeeded.
#
# Calibration, from the #4770 per-pass rescue tables
# (``boards/07-matchgroup-test/diagnostic-runs/README.md``):
#
#   * The FLOOR (90 s) sits above the longest transaction the healthy `main`
#     arm ever completes (70.6 s, ``DQ4``, board 07 initial pass) -- so on a
#     main-shaped trajectory the bound never fires and board 07's A/B inputs
#     are unchanged.  Near the stage end the stage deadline is checked first,
#     so the floor never EXTENDS a transaction past ``deadline``.
#   * The FRACTION (25%) caps an early-stage pathological transaction: the
#     ``DQ3`` rescue began with ~337 s of stage remaining, so its allowance
#     becomes max(84.3, 90) = 90 s instead of the 279.1 s it actually spent,
#     returning ~189 s to the rip-up loop that produces reach.
#
# The bound is FLAT, not scaled by how much the rescue has already achieved:
# ``DQ3`` had "achieved" 9 rips and 7/9 re-lands at 279 s and still rolled
# back -- progress through the rescue pipeline is not a predictor of commit,
# so paying more for it just re-opens the hole.
#
# When the stage has no deadline (``timeout=None`` -> ``relief_deadline is
# None``: board 06's deterministic regen, the two-phase stall-relief hook)
# there is no denominator and the bound is structurally inert -- behavior is
# byte-identical to pre-#4781.
RELIEF_RESCUE_TXN_FRACTION = 0.25
RELIEF_RESCUE_TXN_FLOOR_S = 90.0


def active_expansion_cap(per_net_iterations: int, max_search_iterations: int) -> int:
    """The binding per-net node-expansion cap, or ``0`` when uncapped.

    The deterministic bound this router can hand a sub-search is the
    SMALLER of the tuned per-net cap (``--per-net-iterations`` /
    ``per_net_iterations``, #3881) and the memory backstop
    (``--max-search-iterations``, #2610); ``--deterministic-budget``
    sets both, an explicit flag may set only one.  ``0`` means neither
    is active, i.e. there is no machine-independent bound available and
    a wall clock has to stay in force.

    Extracted (Issue #4724) so the relief rescue (#4536) and the
    post-negotiation sweep (#4159) ask the SAME question -- a second
    copy of this ``min()`` is exactly how two budget selectors drift
    into disagreeing about what "deterministic mode" means.
    """
    return min(
        (cap for cap in (per_net_iterations, max_search_iterations) if cap),
        default=0,
    )


def wall_clock_free_arm(cap: int, deterministic_rescue: bool) -> bool:
    """True when the negotiated loop must not read a wall clock for reach.

    Issue #6475.  ``deterministic_rescue`` with an active expansion cap
    means the caller asked for a route that does not depend on machine
    load.  The oscillation-escape budget (#2415) and the per-iteration
    per-net cap derived from the remaining stage budget (#3989) are both
    wall clocks that decide reach, so both switch off here.  The stage
    deadline (``check_timeout``) is left alone.
    """
    return bool(deterministic_rescue and cap)


def escape_budget(
    cap: int, timeout: float | None, start_time: float, deterministic_rescue: bool
) -> float | None:
    """Wall-clock budget for ``escape_local_minimum`` (Issue #2415 / #6475).

    ``None`` (unbounded; each strategy is still bounded by the per-net
    expansion cap) on the deterministic arm or when there is no stage
    timeout; otherwise ``min(60, 25% of the remaining stage budget)``.
    """
    if timeout is None or wall_clock_free_arm(cap, deterministic_rescue):
        return None
    remaining = timeout - (time.time() - start_time)
    return max(0.0, min(60.0, remaining * 0.25))


def wall_clock_arm_line(cap: int, deterministic_rescue: bool) -> str:
    """Routing-log line naming the escape-budget / per-iteration-cap arm (#6475)."""
    if wall_clock_free_arm(cap, deterministic_rescue):
        return (
            "  Escape budget / per-iteration per-net cap: iteration-bounded "
            "(deterministic rescue; issue #6475) -- oscillation escape and "
            "per-net searches do not read a wall clock"
        )
    return (
        "  Escape budget / per-iteration per-net cap: wall clock "
        "(60s escape budget, remaining-budget per-net cap; deterministic "
        "rescue is off or no expansion cap is active; issue #6475)"
    )


def relief_subsearch_budget(
    cap: int, per_net_timeout: float | None, deterministic_rescue: bool
) -> float | None:
    """Budget for the relief rescue's probe / victim-re-land searches.

    Issue #4536.  The rescue is a TRANSACTION with a no-net-loss
    guarantee: if a displaced victim does not re-land, the whole
    rescue rolls back and the rescued net stays unrouted.  The budget
    those sub-searches get is therefore **reach-determining**, not a
    mere performance knob.

    Historically that budget was a flat ``RELIEF_SUBSEARCH_BUDGET_S``
    (10 s) wall clock.  On board-06 seed-42 the natural victim
    re-land time on GitHub ubuntu runners sits in the 8-12 s band --
    i.e. the budget STRADDLES it, and the SAME code at the SAME
    commit lands 21/21 on a fast runner and 20/21 on a slow one (the
    rescue of ``MIPI_RST`` at iteration 2 keeps or drops
    ``MIPI_CLK-``).  That is the same defect class as the 360 s stage
    backstop this issue removed, one level down.

    ``deterministic_rescue=True`` replaces the wall clock with the
    DETERMINISTIC per-net node-expansion cap that already bounds every
    other search in the stage (``--deterministic-budget`` /
    ``per_net_iterations``, #3877/#3881): the sub-search runs exactly
    the same number of node expansions on every machine, so the rescue
    commits or rolls back identically everywhere.
    ``RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S`` is kept as a NON-load-bearing
    outer guard (~10x the natural sub-search time) purely for the
    pure-Python A* fallback, which the expansion cap does not price in
    seconds.

    The deterministic mode requires an active expansion cap; without
    one there is no deterministic bound to fall back on, so the
    historical wall clock is kept (degrading to the pre-#4536
    behavior rather than to an unbounded search).

    Issue #4730 proposed making ``deterministic_rescue=True`` the
    fleet default and measured a board-07 regression on the
    negotiated entry point, so it stays OPT-IN
    (:data:`DETERMINISTIC_RESCUE_DEFAULT` is ``False``); board 06
    opts in explicitly.  Every entry point that can reach a rescue
    now carries the value per call, so the choice is made where the
    measurement is.
    """
    if deterministic_rescue and cap:
        return RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S
    if per_net_timeout:
        return min(RELIEF_SUBSEARCH_BUDGET_S, per_net_timeout)
    return RELIEF_SUBSEARCH_BUDGET_S


def relief_reland_retry_budget(
    cap: int, per_net_timeout: float | None, subsearch_budget: float | None
) -> float | None:
    """Budget for re-landing a victim whose reduced-budget re-land ran out of time.

    Issue #6151.  The rescue re-lands its displaced victims under the
    REDUCED :meth:`_relief_subsearch_budget` (10 s wall clock by default)
    so a hopeless rescue fails fast.  A victim did originally land under
    the caller's full ``per_net_timeout`` though, and on board 03 the
    ``USB_D+`` re-land (a 5-pad net, two J1 rows plus the MCU side) needs
    ~1.5 s idle but ~4.5 s at a load average of 60 and more beyond that --
    the reduced budget straddles it, so the SAME rescue commits on an idle
    machine and loses a net on a busy one.

    A victim whose first re-land *consumed* its budget (a geometric
    failure returns well before it -- the open set drains) is retried
    once under the budget it originally routed with:

    * the caller's ``per_net_timeout`` when one is set (the plain
      ``kct route`` path: 30 s);
    * otherwise, when a node-expansion cap is active
      (``--deterministic-budget``), the non-binding
      :data:`RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S` -- the expansion cap is
      then the binding, machine-independent bound, exactly the
      ``deterministic_rescue`` arm of :meth:`_relief_subsearch_budget`;
    * otherwise the sub-search budget unchanged (no larger bound exists
      that is not unbounded).

    Never smaller than ``subsearch_budget``; ``None`` (unbounded) stays
    ``None``.
    """
    if subsearch_budget is None:
        return None
    if per_net_timeout:
        return max(subsearch_budget, per_net_timeout)
    if cap:
        return max(subsearch_budget, RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S)
    return subsearch_budget


def relief_subsearch_bound_line(budget: float | None, deterministic_rescue: bool) -> str:
    """Routing-log line recording which relief-rescue sub-search bound is live.

    Issue #4536 put this in the log because the bound decides whether a
    rescue COMMITS -- i.e. it decides routed reach, so any routing log
    used as A/B evidence has to say which one was in force.  The
    ``iteration-bounded`` wording is the greppable evidence line; do not
    reword it.

    The arms state facts, not caller intent.  The deterministic bound is
    opt-in (:data:`DETERMINISTIC_RESCUE_DEFAULT` is ``False`` after #4730's
    measured board-07 regression), so the OFF arm is reached both by the
    ordinary default and by an explicit opt-out, and the capless
    wall-clock arm is reached by an opt-in run that simply has no
    expansion cap to hand the sub-searches to.  Neither arm claims a
    request it cannot know about.

    Emitted by both routing entry points that can reach a relief rescue:
    :meth:`route_all_negotiated` and :meth:`route_all_two_phase` (whose
    stall-relief hook, #3471, calls :meth:`_relief_rescue` positionally
    with the bound bound on by ``functools.partial``).  Each reports the
    value ITS call is running under, so an opt-in is visible in the log
    rather than assumed.
    """
    if deterministic_rescue and budget == RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S:
        return (
            "  Relief-rescue sub-search cap: iteration-bounded "
            "(per-net node-expansion cap; issue #4536) -- probe / "
            "victim-re-land outcomes are machine-independent; "
            f"{RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S:.0f}s wall clock kept "
            "only as a non-binding Python-fallback backstop"
        )
    wall_clock = float(budget if budget else RELIEF_SUBSEARCH_BUDGET_S)
    if deterministic_rescue:
        return (
            f"  Relief-rescue sub-search cap: {wall_clock:.1f}s wall clock -- "
            "no per-net node-expansion cap is active, so the machine-dependent "
            "bound is kept (issue #4536)"
        )
    return (
        f"  Relief-rescue sub-search cap: {wall_clock:.1f}s wall clock -- "
        "deterministic rescue is off for this route (the default; opt in with "
        "deterministic_rescue=True; issue #4730)"
    )


def relief_txn_bound_line(timeout: float | None) -> str:
    """Routing-log line recording the relief-rescue TRANSACTION bound in force.

    Issue #4781.  Same evidence discipline as
    :meth:`_relief_subsearch_bound_line` (#4536): the bound decides how
    much of the stage budget a single rescue can burn before rolling
    back -- i.e. it decides how many rip-up iterations the stage can
    afford, which decides reach -- so any routing log used as A/B
    evidence has to say which arm was live.  The
    ``Relief-rescue transaction bound:`` prefix is the greppable
    evidence token; do not reword it.

    Args:
        timeout: The stage wall-clock budget of the calling loop, or
            ``None`` when the stage runs without a deadline (then
            ``relief_deadline is None`` and the bound is structurally
            inert -- there is no "remaining stage budget" denominator).
    """
    if timeout is not None:
        return (
            "  Relief-rescue transaction bound: proportional -- one rescue "
            "transaction (nested rescues share the parent's allowance) may "
            f"spend at most max({RELIEF_RESCUE_TXN_FRACTION:.0%} of the "
            f"remaining stage budget, {RELIEF_RESCUE_TXN_FLOOR_S:.0f}s) "
            "before rolling back verbatim (issue #4781)"
        )
    return (
        "  Relief-rescue transaction bound: none -- the stage has no "
        "wall-clock deadline, so transactions are bounded by their "
        "sub-search caps only (issue #4781)"
    )


def post_negotiation_sweep_bounds(
    cap: int,
    timeout: float | None,
    per_net_timeout: float | None,
    elapsed: float,
    timed_out: bool,
) -> tuple[float, float | None]:
    """Bounds for the #4159 post-negotiation rescue sweep.

    Returns ``(whole_sweep_budget_s, per_net_budget_s)``.

    Issue #4724.  The sweep decides WHICH stranded nets get one solo
    re-attempt on the live grid, so both of its historical bounds --
    ``POST_NEGOTIATION_SWEEP_BUDGET_S`` (60 s for the whole pass) and
    ``POST_NEGOTIATION_SWEEP_PER_NET_S`` (10 s per net) -- decide routed
    REACH, not runtime.  They were applied UNCONDITIONALLY, including on
    the deterministic-budget callers that pass ``timeout=None`` /
    ``per_net_timeout=None`` precisely so that no wall clock decides
    anything (the #4536 rationale, board 06's regen).  A capped per-net
    search that runs to its 1M-expansion cap costs ~11 s on a CI runner,
    so the 10 s per-net bound straddles it exactly the way the relief
    rescue's 10 s sub-search budget did.

    Selection table -- the same shape as
    :meth:`_relief_subsearch_budget`, so the two cannot drift:

    * **Unbudgeted caller + active expansion cap** -> the deterministic
      arm: the node-expansion cap is the binding bound and the returned
      wall clocks are the ~10x non-binding backstops
      (``POST_NEGOTIATION_SWEEP_SAFETY_BACKSTOP_S`` /
      ``POST_NEGOTIATION_SWEEP_PER_NET_SAFETY_BACKSTOP_S``) that keep a
      pure-Python fallback from grinding.
    * **Unbudgeted caller, no expansion cap** -> unchanged 60 s / 10 s.
      There is no machine-independent bound to fall back on, so the
      historical wall clock is kept rather than degrading to an
      unbounded sweep (identical reasoning to #4536's capless arm).
    * **Budgeted caller** (``timeout`` set) -> unchanged: the sweep gets
      ``min(60 s, remaining stage budget)`` when the loop exited with
      budget left, else its own contained 60 s allowance, and the per-net
      cap is clamped by any caller ``per_net_timeout``.

    Args:
        timeout: The stage wall-clock budget the caller passed, or
            ``None`` for an unbudgeted (deterministic) run.
        per_net_timeout: The caller's per-net A* budget, or ``None``.
        elapsed: Seconds spent in the negotiated loop so far (used only
            to compute the remaining stage budget of a budgeted caller).
        timed_out: Whether the loop exited on the stage timeout (then
            the remaining budget is spent and the sweep runs on its own
            contained allowance, as before).
    """
    if timeout is None and per_net_timeout is None and cap:
        return (
            POST_NEGOTIATION_SWEEP_SAFETY_BACKSTOP_S,
            POST_NEGOTIATION_SWEEP_PER_NET_SAFETY_BACKSTOP_S,
        )

    sweep_remaining: float | None = None
    if timeout is not None:
        sweep_remaining = timeout - elapsed
    if sweep_remaining is not None and sweep_remaining > 0 and not timed_out:
        sweep_budget = min(POST_NEGOTIATION_SWEEP_BUDGET_S, sweep_remaining)
    else:
        sweep_budget = POST_NEGOTIATION_SWEEP_BUDGET_S

    sweep_per_net = per_net_timeout
    if not sweep_per_net:
        # Issue #4776: a literal ``0.0`` is the ``--deterministic-budget``
        # "wall-clock cutoff disabled" sentinel.  Treat it the way
        # :meth:`_relief_subsearch_budget` already does (falsy == absent);
        # taking ``min(0.0, 10 s)`` instead handed every stranded net a
        # zero-second solo budget and silently no-op'd the sweep.  The
        # ``is None`` gate on the safety-backstop arm above is
        # deliberately unchanged.
        sweep_per_net = POST_NEGOTIATION_SWEEP_PER_NET_S
    else:
        sweep_per_net = min(sweep_per_net, POST_NEGOTIATION_SWEEP_PER_NET_S)
    return max(0.0, sweep_budget), sweep_per_net


def post_negotiation_sweep_bound_line(
    budget: float,
    per_net: float | None,
    timeout: float | None,
    per_net_timeout: float | None,
) -> str:
    """Routing-log line recording which sweep bound is live (Issue #4724).

    Same evidence discipline as :meth:`_relief_subsearch_bound_line`
    (#4536): the bound decides which stranded nets get a rescue, so a
    routing log used as A/B evidence has to say which one was in force.
    The ``iteration-bounded`` wording is the greppable evidence token;
    do not reword it.
    """
    if budget == POST_NEGOTIATION_SWEEP_SAFETY_BACKSTOP_S:
        return (
            "  Post-negotiation sweep bound: iteration-bounded "
            "(per-net node-expansion cap; issue #4724) -- which stranded "
            "nets are rescued is machine-independent; "
            f"{POST_NEGOTIATION_SWEEP_SAFETY_BACKSTOP_S:.0f}s/"
            f"{POST_NEGOTIATION_SWEEP_PER_NET_SAFETY_BACKSTOP_S:.0f}s wall "
            "clocks kept only as non-binding Python-fallback backstops"
        )
    per_net_str = "unbounded" if per_net is None else f"{per_net:.1f}s"
    if timeout is not None:
        why = "the caller passed a stage timeout (issue #4159)"
    elif per_net_timeout is not None:
        why = "the caller passed an explicit per-net wall clock (issue #4159)"
    else:
        why = (
            "no per-net node-expansion cap is active, so the "
            "machine-dependent bound is kept (issue #4724)"
        )
    return (
        f"  Post-negotiation sweep bound: {budget:.1f}s whole-sweep / "
        f"{per_net_str} per-net wall clock -- {why}"
    )
