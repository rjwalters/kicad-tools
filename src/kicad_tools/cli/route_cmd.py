"""
PCB autorouting CLI command.

Provides command-line access to the autorouter:

    kicad-tools route board.kicad_pcb
    kicad-tools route board.kicad_pcb -o board_routed.kicad_pcb
    kicad-tools route board.kicad_pcb --skip-nets GND,VCC --strategy negotiated

Performance Profiling:

    Use --profile to measure routing performance and identify bottlenecks:

    # Profile routing and save results
    kicad-tools route board.kicad_pcb --profile

    # Specify custom output file
    kicad-tools route board.kicad_pcb --profile --profile-output my_profile.prof

    # Analyze results with pstats
    python -m pstats route_profile.prof

    # Visualize with snakeviz (pip install snakeviz)
    snakeviz route_profile.prof

Layer Stack Configuration:

    By default, the autorouter uses a 2-layer configuration (F.Cu, B.Cu).
    For multi-layer boards, use the --layers option:

    # 4-layer board with GND/PWR planes (typical for Pi HAT, Arduino shields)
    kicad-tools route board.kicad_pcb --layers 4

    # 4-layer with 2 signal layers (for high-density routing)
    kicad-tools route board.kicad_pcb --layers 4-sig

    # 4-layer with all 4 signal layers (no planes, maximum routing resources)
    kicad-tools route board.kicad_pcb --layers 4-all

    # 6-layer with 4 signal layers
    kicad-tools route board.kicad_pcb --layers 6

    Layer stack configurations:
    - '2': F.Cu (signal), B.Cu (signal)
    - '4': F.Cu (signal), In1.Cu (GND plane), In2.Cu (PWR plane), B.Cu (signal)
    - '4-sig': F.Cu (signal), In1.Cu (signal), In2.Cu (GND plane), B.Cu (mixed)
    - '4-all': F.Cu (signal), In1.Cu (signal), In2.Cu (signal), B.Cu (signal)
    - '6': F.Cu, In1.Cu (GND), In2.Cu (signal), In3.Cu (signal), In4.Cu (PWR), B.Cu

    For 4-layer boards with inner planes (--layers 4), signals are routed on
    the outer layers (F.Cu and B.Cu) with vias providing layer transitions
    through the planes. This is the most common configuration for hobby/small
    production boards.
"""

import argparse
import contextlib
import logging
import math
import os
import random
import shutil
import signal
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from kicad_tools.router import Autorouter, LayerStack
    from kicad_tools.router.primitives import Route

# Issue #3035: ``_auto_skip_pour_nets`` was promoted to a public helper at
# ``kicad_tools.router.auto_pour.auto_skip_pour_nets`` so in-process router
# callers (board generate_design.py scripts using
# ``router.route_all_negotiated()`` instead of subprocessing ``kct route``)
# can reach it without importing a CLI private.  The alias here is
# load-bearing: the 4 call sites in this module (and the
# ``@patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", ...)`` decorators
# in ``tests/test_layer_escalation.py`` and ``tests/test_route_auto_fix.py``)
# all reference the underscore name; this re-import preserves the symbol so
# they continue to resolve to the public implementation transparently.  Do
# not remove this alias without also updating those patch targets.
from kicad_tools.router.auto_pour import auto_skip_pour_nets as _auto_skip_pour_nets  # noqa: E402

logger = logging.getLogger(__name__)


# =============================================================================
# Issue #2802: Total wall-clock deadline helpers
# =============================================================================
#
# ``--timeout`` is intended to be a *total* wall-clock budget for the whole
# routing invocation, but historically the orchestration layer re-used it as a
# per-stage budget: every layer-escalation attempt, every placement-feedback
# iteration, and every auto-fix pass received its own fresh copy of the same
# ``args.timeout`` value, so worst-case wall-clock for a single ``kct route``
# invocation could exceed 10x the configured budget.
#
# The fix is a single monotonic deadline computed once in ``main()`` and
# threaded through every outer-loop site via these helpers.  The deadline is
# stored on the parsed ``args`` namespace as ``_wall_clock_deadline`` so it
# travels naturally with the other CLI parameters.  When ``--timeout`` is not
# set the deadline is ``None`` and these helpers preserve legacy unbounded
# behaviour.


_AUTO_FIX_RESERVE_FRACTION = 0.20
_AUTO_FIX_RESERVE_FLOOR_SEC = 60.0

# =============================================================================
# Issue #3538: Deterministic (iteration-budgeted) routing
# =============================================================================
#
# The board-07 Match-Group routing-regression CI gate re-routes from source and
# its DRC count must be reproducible across machines so the allowlist floor in
# .github/routed-drc-tolerance.yml can be an exact value instead of a
# machine-variance band.  The only routing stage that lands a load-dependent
# amount of copper is the per-net A* search: on a slow/loaded runner the per-net
# wall-clock budget (``--per-net-timeout``) cuts a search short, so the SAME
# code at the SAME ``--seed`` reaches fewer nets and reports a different DRC
# profile (the "#3466 wall-clock-budget cliff").
#
# ``--deterministic-budget`` (see the parser declaration) removes that coupling:
# it disables the per-net wall-clock cutoff and instead pins the C++ A*
# *iteration backstop* (``--max-search-iterations``, Issue #2610 / #2819) to a
# fixed node-expansion count.  Each per-net search then either finds a path or
# aborts after the SAME number of node expansions on every environment, making
# the routed artifact (and its DRC count) machine-independent.  The outer
# ``--timeout`` is retained only as a safety backstop; if it fires under
# deterministic-budget mode the run is no longer reproducible, so the
# normalization warns.
#
# The fixed backstop must be large enough that a search which WOULD succeed on
# an unbounded run still succeeds (so reach is not lost), while bounded enough
# that a genuinely unroutable net aborts in finite work.  ~12M node expansions
# is ~12x the historical ``cols * rows * 4`` heuristic for the board-07 grid
# (~1M for a 500x500 lattice) -- generous headroom for the dense DDR/MIPI/HDMI
# escapes without being unbounded.  Override with an explicit
# ``--max-search-iterations N`` alongside the flag when a board needs more.
DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS = 12_000_000

# Issue #3881: the TUNED per-net iteration cap applied by --deterministic-budget.
#
# The 12M memory backstop above is effectively UNBOUNDED per-net: on the chorus
# fixture one net (I2S_BCLK) burned 280s of a 1200s --timeout, and geometric-
# failure nets fell through to the 10-100x-slower Python A*, so only ~14 of 51
# nets were even attempted before the outer deadline fired (chorus 13/51 vs the
# old wall-clock recipe's 31/51).  A smaller per-net iteration cap bounds each
# net to a fair iteration slice so hard nets give up deterministically and more
# nets get a turn -- recovering throughput WHILE staying load-independent
# (iteration count, not wall-clock, so still reproducible).
#
# The value is tuned against the chorus fixture: the old recipe gave ~60s/net at
# 31 routed, and a chorus A* net does roughly tens-of-thousands to low-millions
# of node expansions in that window.  1,000,000 expansions sits at the top of
# that band -- generous enough that nets which WOULD succeed still do, while
# cutting off the genuine grinders (which would otherwise run to 12M) so the
# remaining nets get budget.  Override with an explicit --per-net-iterations N.
DETERMINISTIC_BUDGET_PER_NET_ITERATIONS = 1_000_000


def _normalize_deterministic_budget(args, quiet: bool = False) -> None:
    """Apply Issue #3538 iteration-budget normalization to ``args`` in place.

    When ``--deterministic-budget`` is set this:

      1. Disables the per-net wall-clock A* cutoff
         (``args.per_net_timeout = 0.0``) so a slow machine does not cut a
         search short and land less copper than a fast one.
      2. Pins the C++ A* iteration backstop
         (``args.max_search_iterations``) to
         :data:`DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS` UNLESS the user
         passed an explicit positive ``--max-search-iterations`` (which is
         then honoured verbatim).  A positive backstop is what makes each
         per-net search terminate after a fixed node-expansion count instead
         of running unbounded when the wall-clock cutoff is removed.
      3. Warns when the outer ``--timeout`` is set, because a firing outer
         deadline re-introduces wall-clock dependence and breaks the
         reproducibility guarantee (the deadline is kept as a safety
         backstop, not the binding constraint).

    No-op when ``--deterministic-budget`` is not set, so legacy behaviour is
    preserved bit-for-bit.
    """
    if not getattr(args, "deterministic_budget", False):
        return

    # (1) Disable the per-net wall-clock cutoff.
    args.per_net_timeout = 0.0

    # (2) Pin the iteration backstop unless the user gave an explicit positive
    # value.  ``--max-search-iterations`` defaults to 0 ("use cols*rows*4
    # heuristic"); under deterministic-budget that heuristic is acceptable but
    # we prefer an explicit, machine-independent fixed cap so the abort point
    # does not vary with the auto-derived grid dimensions.
    if not getattr(args, "max_search_iterations", 0):
        args.max_search_iterations = DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS

    # (2b) Issue #3881: default the TUNED per-net iteration cap unless the user
    # passed an explicit positive ``--per-net-iterations``.  The 12M backstop
    # above is effectively unbounded per-net, so without this a single hard net
    # monopolises the whole --timeout and only a fraction of nets get attempted
    # (chorus 13/51).  The per-net cap bounds each net to a fair iteration slice
    # (load-independent -> still deterministic) so more nets get a turn.
    if not getattr(args, "per_net_iterations", 0):
        args.per_net_iterations = DETERMINISTIC_BUDGET_PER_NET_ITERATIONS

    # (3) Warn if the outer wall-clock deadline could bind.
    timeout = getattr(args, "timeout", None)
    if not quiet:
        print(
            "[deterministic-budget] Iteration-budgeted routing enabled "
            "(Issue #3538): per-net wall-clock cutoff DISABLED, C++ A* "
            f"iteration backstop pinned to {args.max_search_iterations:,} "
            "node expansions.  Routed output is reproducible across machines."
        )
        print(
            "[deterministic-budget] Per-net iteration cap "
            f"{args.per_net_iterations:,} node expansions (Issue #3881): each "
            "net gives up deterministically at the cap so hard nets do not "
            "monopolise the budget and more nets get a turn (Python fallback "
            "skipped for capped nets)."
        )
        if timeout and timeout > 0:
            print(
                "[deterministic-budget] WARNING: --timeout "
                f"{timeout:g}s is set.  It is retained only as a SAFETY "
                "backstop; if the outer deadline fires the run is no longer "
                "machine-independent.  Size it generously (or omit it) so the "
                "iteration budget -- not wall-clock -- bounds the work."
            )


def _auto_fix_budget(args) -> float:
    """Return the auto-fix reserve in seconds (issue #3238).

    Returns ``0.0`` when auto-fix is not requested (no reserve needed) or
    when ``--timeout`` is unset (legacy unbounded runs preserve their
    existing behaviour: routing gets unlimited time, auto-fix runs after
    routing finishes naturally).

    When auto-fix is requested AND ``--timeout`` is set, returns
    ``max(60s, 0.20 * timeout)`` so the negotiated router cannot consume
    the entire budget and silently leave auto-fix with zero time.  See
    issue #3238 for the chorus-test regression that motivated this.

    The reserve is also capped at half of ``--timeout`` so the routing
    budget never drops below 50% of what the user asked for; on extremely
    small ``--timeout`` values (e.g. ``--timeout 30``) the floor of 60s
    would otherwise leave the routing loop with effectively zero budget.
    """
    timeout = getattr(args, "timeout", None)
    if not timeout or timeout <= 0:
        return 0.0
    if not _should_auto_fix(args):
        return 0.0
    reserve = max(_AUTO_FIX_RESERVE_FLOOR_SEC, _AUTO_FIX_RESERVE_FRACTION * float(timeout))
    # Cap at half of --timeout so the routing budget always sees at least
    # 50% of what the user asked for, even with very tight timeouts.
    return min(reserve, 0.5 * float(timeout))


def _set_wall_clock_deadline(args) -> None:
    """Stamp a monotonic deadline on ``args`` from ``args.timeout``.

    Called once near the start of ``main()`` (after argparse).  If
    ``args.timeout`` is falsy (None, 0, or negative) the deadline is set
    to ``None`` so the rest of the orchestration treats the run as
    unbounded.

    Issue #3238: When ``--auto-fix`` is requested, an additional
    ``_routing_deadline`` is stamped at ``now + (timeout - auto_fix_reserve)``
    so outer routing loops bail early enough to leave a guaranteed budget
    for auto-fix.  The original ``_wall_clock_deadline`` continues to
    bound the *total* wall-clock budget.
    """
    timeout = getattr(args, "timeout", None)
    now = time.monotonic()
    if timeout and timeout > 0:
        args._wall_clock_deadline = now + float(timeout)
        reserve = _auto_fix_budget(args)
        args._auto_fix_reserve = reserve
        # Routing deadline is the wall-clock deadline minus the auto-fix
        # reserve.  When no reserve is held (no --auto-fix, or no
        # --timeout) routing deadline collapses to the wall-clock
        # deadline -- existing behaviour is preserved bit-for-bit.
        args._routing_deadline = now + float(timeout) - reserve
    else:
        args._wall_clock_deadline = None
        args._auto_fix_reserve = 0.0
        args._routing_deadline = None


def _remaining_budget(args) -> float | None:
    """Return seconds remaining vs the *routing* deadline.

    Routing-loop callers use this helper so the auto-fix reserve is
    transparently carved out of their effective budget (issue #3238).
    Returns ``None`` when no deadline is configured (legacy unbounded
    behaviour).  Returns a non-negative float otherwise; callers should
    treat zero as "routing deadline reached, hand off to auto-fix".
    """
    # Issue #3238: prefer the routing deadline (which bounds the routing
    # loops with the auto-fix reserve already subtracted).  Fall back to
    # the wall-clock deadline for callers that may have stamped only the
    # original field (defensive against future refactors / older test
    # fixtures that don't go through ``_set_wall_clock_deadline``).
    deadline = getattr(args, "_routing_deadline", None)
    if deadline is None:
        deadline = getattr(args, "_wall_clock_deadline", None)
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _total_remaining_budget(args) -> float | None:
    """Return seconds remaining vs the *total* wall-clock deadline.

    Used by ``_run_auto_fix`` to determine whether it has *any* time to
    run -- the routing deadline may already be expired (that's expected;
    it's why auto-fix is being invoked) but the total deadline still
    leaves the carved-out reserve for auto-fix to do its work.
    """
    deadline = getattr(args, "_wall_clock_deadline", None)
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _deadline_expired(args) -> bool:
    """True iff a deadline is configured and has been reached or passed.

    Issue #3238: This checks the *routing* deadline -- outer loops should
    stop routing when the routing budget is gone (leaving the auto-fix
    reserve untouched).  Use ``_total_deadline_expired`` to check the
    *total* wall-clock deadline (which is what ``_run_auto_fix`` needs).
    """
    rem = _remaining_budget(args)
    return rem is not None and rem <= 0.0


def _total_deadline_expired(args) -> bool:
    """True iff the *total* wall-clock deadline has been reached.

    Issue #3238: ``_run_auto_fix`` uses this instead of ``_deadline_expired``
    so it can run within the reserved auto-fix budget even after the
    routing deadline has fired.
    """
    rem = _total_remaining_budget(args)
    return rem is not None and rem <= 0.0


def _budgeted_timeout(args) -> float | None:
    """Return the per-call timeout to pass into inner router routines.

    When a deadline is configured this is the smaller of the original
    ``--timeout`` (preserving per-stage semantics for the *first* stage)
    and the remaining wall-clock budget (so the *final* stage shortens
    naturally as time runs out).  When no deadline is configured this
    returns ``args.timeout`` unchanged so existing behaviour is preserved
    for users who never passed ``--timeout``.
    """
    timeout = getattr(args, "timeout", None)
    remaining = _remaining_budget(args)
    if remaining is None:
        return timeout
    if timeout is None:
        # ``_wall_clock_deadline`` is derived from ``args.timeout`` so this
        # branch is unreachable in practice; guard against future refactors
        # that decouple the two.
        return remaining
    return min(float(timeout), remaining)


# =============================================================================
# Issue #2823: Per-attempt budget allocation for escalation loops
# =============================================================================
#
# ``_budgeted_timeout`` (above) intentionally preserves "first-stage semantics":
# the *first* escalation attempt is allowed to consume the full ``--timeout``
# value as its per-call budget, and only later attempts get a smaller slice as
# the wall-clock deadline approaches.  This is correct for *single-stage* call
# sites (placement-feedback iterations, the inner route in
# ``route_with_strategy``, etc.), but it is *wrong* for the multi-attempt
# layer-escalation and combined-escalation loops.
#
# In ``--auto-layers`` mode the orchestration tries 2L, 4L, 4L-all-sig, then 6L
# in sequence.  When ``--timeout`` is set tight relative to the natural runtime
# of the first attempt, the greedy allocation gives the entire budget to the 2L
# attempt, leaves nothing for 4L, and never starts 6L.  The escalation strategy
# degenerates to "spend everything on the lowest layer count, give up."
#
# The fix is a *per-attempt* helper that divides the remaining wall-clock
# budget evenly across the remaining attempts, returning the minimum of:
#   1. ``args.timeout``          - never exceed user's original cap
#   2. ``remaining_budget``      - never overrun the total deadline
#   3. ``per_attempt_budget``    - fair slice across remaining attempts
#
# When ``--timeout`` is unset, the helper falls through to ``None`` (legacy
# unbounded behaviour, identical to ``_budgeted_timeout``).  When ``--timeout``
# is set generously (i.e. larger than the natural per-attempt runtime), the
# per-attempt cap is an *upper bound* not a target runtime, so the inner
# router can still finish early on its own.


def _per_attempt_budgeted_timeout(args, attempt_index: int, max_attempts: int) -> float | None:
    """Return the per-call timeout for one attempt of an escalation loop.

    Unlike :func:`_budgeted_timeout` (which lets the first stage consume the
    full ``--timeout`` value), this helper divides the *remaining* wall-clock
    budget evenly across the *remaining* attempts and returns the minimum of
    that fair slice, the original ``--timeout``, and the remaining budget.

    Args:
        args: Parsed CLI namespace; must carry ``timeout`` and (after
            ``_set_wall_clock_deadline``) ``_wall_clock_deadline``.
        attempt_index: 0-based index of the current attempt within the
            escalation loop.  Determines how many attempts are still
            outstanding (``max_attempts - attempt_index``).
        max_attempts: Total number of attempts the escalation loop intends
            to run.  For 1D layer escalation this is ``len(layer_configs)``;
            for the 2D combined escalation this is
            ``len(layer_configs) * len(tiers)``.

    Returns:
        ``None`` when no wall-clock deadline is configured (legacy unbounded
        behaviour, matches :func:`_budgeted_timeout`).  Otherwise a positive
        float bounded by ``args.timeout`` and the remaining wall-clock
        budget, fairly sliced across remaining attempts so later attempts
        also get a real chance to run.

    Notes:
        - When ``max_attempts <= 1`` this collapses to :func:`_budgeted_timeout`
          (no fair-slicing needed for a single attempt).
        - Unused budget from a fast-finishing attempt rolls forward
          automatically: the next call uses the *current* ``remaining_budget``
          divided by the *new* ``remaining_attempts`` count, so an early
          completion on attempt N enlarges the slice available to N+1.
    """
    timeout = getattr(args, "timeout", None)
    remaining = _remaining_budget(args)
    if remaining is None:
        # No deadline configured -> legacy unbounded behaviour.
        return timeout

    # Attempts still outstanding *including* the current one.
    remaining_attempts = max(1, max_attempts - attempt_index)
    per_attempt_slice = remaining / remaining_attempts

    if timeout is None:
        # Defensive: ``_wall_clock_deadline`` is derived from ``args.timeout``,
        # so this branch is unreachable in practice.
        return per_attempt_slice

    return min(float(timeout), remaining, per_attempt_slice)


def _routable_multi_pad_nets(router: "Autorouter") -> list[int]:
    """Return the multi-pad net IDs the router will actually route.

    Issue #3942 (Bug B): the routed/total summary denominator must count
    only the nets the router was asked to route.  A net that carries 2+
    pads but is *pour-served* -- ``router._is_pour_net(net_id)`` is True
    because its net class declares ``is_pour_net`` and it has a copper
    zone -- is stripped from the routing order by
    :meth:`Autorouter._filter_pour_nets` and is therefore never counted
    in ``stats['nets_routed']``.

    Historically these nets were excluded from the denominator only when
    the CLI's ``skip_nets`` list caught them (their pads get rewritten to
    net 0 at load time, so ``net_num > 0`` already drops them).  But the
    router's own pour classification (``net_class_map``) can flag a net as
    pour even when the CLI's zone-detection regex missed the zone and did
    not add it to ``skip_nets``.  In that case the net kept ``net_num >
    0``, landed in the multi-pad denominator, yet the router silently
    skipped it -- producing a bogus ``PARTIAL: Routed 1/2`` line on a
    board that routed every net it was asked to.

    Gating on ``_is_pour_net`` (which returns False for pour nets in
    ``_pour_nets_without_zones`` -- those are routed as signals, so they
    stay in the count) makes the denominator match precisely the set the
    router hands to the A* loop.

    Args:
        router: The Autorouter, already loaded (so ``net_class_map`` and
            ``_pour_nets_without_zones`` are populated).

    Returns:
        Sorted list of net IDs with ``net_num > 0``, 2+ pads, that the
        router will route (i.e. are not pour-served).
    """
    result: list[int] = []
    for net_num, pads in router.nets.items():
        if net_num > 0 and len(pads) >= 2 and not router._is_pour_net(net_num):
            result.append(net_num)
    return sorted(result)


def _emit_single_pad_net_warning(
    router: "Autorouter",
    single_pad_nets: list[int],
) -> None:
    """Print a top-of-output warning when single-pad signal nets exist.

    These nets are structurally unroutable -- the router silently skips
    them, which makes "13/13 routed, DRC clean" look like a successful
    build even when 4 SWD signals are floating.  Pour nets (POWER /
    GROUND) are silently allowed because a single test point or
    pour-only net is a legitimate design pattern.

    The remaining nets are categorized using the same regex rules used
    by :class:`kicad_tools.validate.rules.SinglePadNetRule` (see issue
    #2613).  Categorized output ensures the warning is not a firehose:

    - **Genuine NCs** (KiCad-emitted ``unconnected-(REF-PIN-PadN)``):
      reported at INFO level, no action required.
    - **Connector NCs** (``Net-(REF-PadN)`` on a J/P prefix, typically
      intentional GPIO no-connects): reported at INFO level.
    - **Real defects** (everything else): reported at WARNING level
      with a pointer to ``kct check --only single_pad_net``.

    See ``kct check --only single_pad_net`` for the full DRC-style
    report (this banner exists to surface the defect early in the
    pipeline; the actionable error lives in the check command).

    Args:
        router: The Autorouter (used for ``router.net_names`` lookup).
        single_pad_nets: Net IDs that have exactly one pad assigned.
    """
    if not single_pad_nets:
        return

    from kicad_tools.cli.progress import flush_print
    from kicad_tools.validate.rules.single_pad_net import (
        _CONNECTOR_NET_PATTERN,
        _KICAD_NC_PATTERN,
    )

    try:
        from kicad_tools.router.net_class import classify_and_apply_rules

        single_pad_name_map = {
            net_num: router.net_names.get(net_num, "") for net_num in single_pad_nets
        }
        # Drop unnamed nets so we don't classify the empty string.
        single_pad_name_map = {num: name for num, name in single_pad_name_map.items() if name}
        pour_rules = classify_and_apply_rules(single_pad_name_map) if single_pad_name_map else {}
        signal_single_pad_names = sorted(
            name
            for num, name in single_pad_name_map.items()
            if not (pour_rules.get(name) and pour_rules[name].is_pour_net)
        )
    except Exception:
        # Conservative: if classification blows up, surface every named
        # single-pad net so we don't hide real defects.
        signal_single_pad_names = sorted(
            router.net_names.get(num, "")
            for num in single_pad_nets
            if router.net_names.get(num, "")
        )

    if not signal_single_pad_names:
        return

    # Categorize each surviving single-pad net using the same regex
    # rules as the validate rule.  Note that this banner cannot validate
    # the footprint-ref-prefix match (it has no Footprint handle), so
    # connector_nc here is detected purely from the net name pattern.
    # A net like ``Net-(J5-1)`` whose lone pad is actually on a
    # non-connector footprint is rare in practice; the DRC-side
    # validate rule does the stricter cross-check.
    genuine_nc: list[str] = []
    connector_nc: list[str] = []
    defects: list[str] = []
    for name in signal_single_pad_names:
        if _KICAD_NC_PATTERN.match(name):
            genuine_nc.append(name)
        elif _CONNECTOR_NET_PATTERN.match(name):
            connector_nc.append(name)
        else:
            defects.append(name)

    flush_print("")
    # Banner header reflects categorized counts so agents see signal
    # vs. noise at a glance.
    parts: list[str] = []
    if defects:
        parts.append(f"{len(defects)} defect(s)")
    if connector_nc:
        parts.append(f"{len(connector_nc)} connector NC")
    if genuine_nc:
        parts.append(f"{len(genuine_nc)} explicit NC")
    summary = ", ".join(parts)
    flush_print(
        f"  WARNING: {len(signal_single_pad_names)} single-pad signal net(s) detected ({summary}):"
    )
    if defects:
        flush_print("    DEFECTS (likely missing footprint or schematic/PCB drift):")
        for name in defects:
            flush_print(f"      - {name}")
    if connector_nc:
        flush_print("    INFO -- connector-pin NCs (typically intentional GPIO no-connects):")
        for name in connector_nc:
            flush_print(f"      - {name}")
    if genuine_nc:
        flush_print("    INFO -- explicit NCs from symbol pin attributes:")
        for name in genuine_nc:
            flush_print(f"      - {name}")
    flush_print(
        "  Run 'kct check --only single_pad_net <pcb>' for details. "
        "Routing will proceed but these nets cannot be connected."
    )


def _strip_route_blocks(pcb_content: str) -> tuple[str, int, int]:
    """Strip top-level ``(segment ...)`` and ``(via ...)`` blocks from PCB content.

    Issue #2976: When ``_stage_input_for_auto_pour`` aliases ``pcb_path`` to
    ``output_path`` (so the user's input file isn't mutated by ``auto_pour``),
    subsequent ``_write_routed_pcb`` calls re-read the **previous write's**
    output rather than the original input.  Each write then *appends* the
    current route s-expression on top of stale segments/vias from the prior
    write, doubling (or worse) the via population in the output file.

    The duplicated vias trigger ``dimension_drill_clearance`` errors --
    -0.300mm clearance == two coincident drills on the same net -- because
    they are literally the same physical via emitted twice with different
    UUIDs.

    The fix is to remove any top-level routed-element blocks before
    inserting fresh ones: the in-memory router state is the source of
    truth, and the new ``route_sexp`` already contains every segment and
    via that should appear in the output.  Footprints, pads, zones, and
    other PCB structure are preserved because we only strip blocks whose
    first token is ``segment`` or ``via``.

    Args:
        pcb_content: Original PCB file content.

    Returns:
        Tuple of ``(stripped_content, segments_removed, vias_removed)``.
        Counts are returned so callers can log a diagnostic when a stale
        write is observed.
    """
    # Walk the content counting depth, identifying top-level forms whose
    # first token is "segment" or "via" and excising them.  Footprints
    # contain their own (pad ...) blocks, never (segment ...) or top-level
    # (via ...), so stripping at depth=1 is safe.
    out: list[str] = []
    i = 0
    n = len(pcb_content)
    depth = 0
    in_string = False
    prev_char = ""
    segments_removed = 0
    vias_removed = 0
    while i < n:
        ch = pcb_content[i]
        if ch == '"' and prev_char != "\\":
            in_string = not in_string
            out.append(ch)
            prev_char = ch
            i += 1
            continue
        if not in_string and ch == "(":
            # Peek at the token following the paren.
            j = i + 1
            while j < n and pcb_content[j].isspace():
                j += 1
            token_start = j
            while (
                j < n
                and not pcb_content[j].isspace()
                and pcb_content[j] != "("
                and pcb_content[j] != ")"
            ):
                j += 1
            token = pcb_content[token_start:j]
            if depth == 1 and token in ("segment", "via"):
                # Skip the entire form: find matching ")".
                form_depth = 1
                k = j
                form_in_string = False
                form_prev = ""
                while k < n and form_depth > 0:
                    c = pcb_content[k]
                    if c == '"' and form_prev != "\\":
                        form_in_string = not form_in_string
                    elif not form_in_string:
                        if c == "(":
                            form_depth += 1
                        elif c == ")":
                            form_depth -= 1
                    form_prev = c
                    k += 1
                if token == "segment":
                    segments_removed += 1
                else:
                    vias_removed += 1
                # Skip whitespace that was preceding this form (trailing
                # newline/tab from the previous emission) to keep the
                # resulting file tidy.
                while out and out[-1] in (" ", "\t"):
                    out.pop()
                # Also drop a single trailing newline so we collapse the
                # blank line the form previously occupied.
                if out and out[-1] == "\n":
                    out.pop()
                i = k
                prev_char = ")"
                continue
            depth += 1
        elif not in_string and ch == ")":
            depth -= 1
        out.append(ch)
        prev_char = ch
        i += 1
    return "".join(out), segments_removed, vias_removed


def _insert_sexp_before_closing(pcb_content: str, sexp_fragments: str) -> str:
    """Insert S-expression fragments before the final closing parenthesis of a PCB file.

    This correctly removes only the last closing parenthesis from the PCB content
    and re-adds it after the inserted fragments. Unlike ``rstrip(")")``, which
    strips ALL trailing ``)``, this function preserves the S-expression structure.

    Issue #2976: Before inserting fresh route s-expressions, any pre-existing
    ``(segment ...)`` and ``(via ...)`` blocks at the top level are stripped.
    This prevents accumulation when ``pcb_path == output_path`` (which happens
    after ``_stage_input_for_auto_pour``): each ``_write_routed_pcb`` call
    used to *append* the current route state on top of the previous write,
    producing duplicate same-net vias that the DRC flagged with negative
    drill-edge clearance.

    Args:
        pcb_content: Original PCB file content.
        sexp_fragments: S-expression string(s) to insert (segments, vias, zones).

    Returns:
        Modified PCB content with fragments inserted before the final ``)``.
    """
    pcb_content, _stripped_segs, _stripped_vias = _strip_route_blocks(pcb_content)
    content = pcb_content.rstrip()
    if content.endswith(")"):
        content = content[:-1].rstrip()

    result = content + "\n\n"
    result += f"  {sexp_fragments}\n"
    result += ")\n"
    return result


def _validate_sexp_parentheses(content: str) -> bool:
    """Validate that S-expression parentheses are balanced.

    Scans the content respecting quoted strings (parentheses inside quotes
    are not counted). Returns True if parentheses are balanced.

    Args:
        content: S-expression content to validate.

    Returns:
        True if parentheses are balanced, False otherwise.
    """
    depth = 0
    in_string = False
    prev_char = ""
    for char in content:
        if char == '"' and prev_char != "\\":
            in_string = not in_string
        elif not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return False
        prev_char = char
    return depth == 0


def _write_routed_pcb(
    pcb_path: Path,
    output_path: Path,
    route_sexp: str,
    *,
    layer_count: int = 2,
    is_checkpoint: bool = False,
) -> Path:
    """Atomically write a routed PCB file.

    Consolidates the read_text -> update_pcb_layer_stackup ->
    _insert_sexp_before_closing -> _validate_sexp_parentheses -> write
    sequence that was previously duplicated at four save sites in this
    module.

    Uses an atomic write via ``<output>.tmp`` + ``os.fsync`` +
    ``os.replace(tmp, output)`` so a SIGKILL/OOM between bytes-on-the-wire
    and final flush cannot leave a torn (truncated) PCB file at the user's
    output path.  Closes Issue #2808's torn-file hazard.

    Honors ``output_path`` exactly -- callers must NOT pre-suffix the path
    with ``_4layer`` or similar; the layer count is recorded inside the
    PCB content via :func:`update_pcb_layer_stackup`, not in the filename
    (closes Issue #2809).

    Args:
        pcb_path: Path to the source (unrouted) PCB file.  Read fresh on
            every call so checkpoint writes pick up upstream edits to the
            input file (rare, but supported).
        output_path: Final destination path.  Written atomically via a
            sibling ``.tmp`` file.
        route_sexp: S-expression fragment(s) to insert before the closing
            ``)``.  Can be empty -- in that case the original PCB is
            written back unchanged (useful for checkpoints that fire
            before any net has routed).
        layer_count: Target copper layer count for the layer stackup
            update.  ``2`` is a no-op.  Defaults to ``2``.
        is_checkpoint: When True, skip the layer-stackup mutation.
            Checkpoints serialize the in-progress best snapshot; they
            should match whatever stackup is currently in use and not
            attempt to escalate.  Defaults to False.

    Returns:
        The ``output_path`` that was written to (returned unchanged so
        callers can use the helper inline, e.g.
        ``written = _write_routed_pcb(...)``).

    Raises:
        ValueError: If the generated PCB has unbalanced S-expression
            parentheses (indicates a bug in route generation -- caller
            is responsible for surfacing this).
    """
    original_content = pcb_path.read_text()

    # Update layer stackup for terminal writes when we escalated above 2L.
    # Checkpoints skip this -- they reflect mid-route state and should not
    # rewrite the stackup at every flush.
    if not is_checkpoint and layer_count > 2:
        original_content = update_pcb_layer_stackup(original_content, layer_count)

    if route_sexp:
        output_content = _insert_sexp_before_closing(original_content, route_sexp)
    else:
        output_content = original_content

    if not _validate_sexp_parentheses(output_content):
        logger.error("Generated PCB file has unbalanced parentheses")
        raise ValueError(
            "Generated PCB file has invalid S-expression syntax "
            "(unbalanced parentheses). This is a bug in kicad-tools. "
            "Please report it."
        )

    # Atomic write: tmp file -> fsync -> rename.  Sibling-in-same-dir
    # ensures os.replace is a same-filesystem rename (atomic on POSIX).
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(output_content)
    # fsync the file so a crash between write and rename does not leave
    # a partial-content tmp file masquerading as the routed PCB.
    with open(tmp_path, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp_path, output_path)

    return output_path


def _connectivity_snapshot(router: "Autorouter"):
    """Capture per-net connectivity + deep-copied routes (issue #2596).

    Thin wrapper around
    :func:`kicad_tools.router.connectivity_invariant.snapshot_connectivity`
    so each pipeline call site has a single-line invocation.

    Returns:
        :class:`ConnectivitySnapshot` to pass to
        :func:`_enforce_connectivity_invariant_or_exit` after the phase
        runs.
    """
    from kicad_tools.router.connectivity_invariant import snapshot_connectivity

    return snapshot_connectivity(router)


def _enforce_connectivity_invariant_or_exit(
    router: "Autorouter",
    snapshot,
    *,
    phase: str,
    args: argparse.Namespace,
    quiet: bool = False,
) -> None:
    """Enforce the post-phase connectivity invariant (issue #2596).

    Reverts regressed nets in default mode; in ``--strict`` mode this
    function calls :func:`sys.exit` with code 6 (the same code used by
    the post-save ``verify_output_connectivity`` block) so the
    behaviour is identical regardless of which guard catches the
    regression first.

    Args:
        router: The :class:`Autorouter` whose routes were just mutated.
        snapshot: Snapshot returned by :func:`_connectivity_snapshot`.
        phase: Short label identifying the phase (``"optimize"`` or
            ``"nudge"``).
        args: Parsed CLI arguments (used for ``--strict`` and
            ``--verbose``).
        quiet: Suppress all stdout output.
    """
    from kicad_tools.router.connectivity_invariant import (
        ConnectivityRegressionError,
        enforce_connectivity_invariant,
    )

    strict = bool(getattr(args, "strict", False))
    verbose = bool(getattr(args, "verbose", False))
    try:
        enforce_connectivity_invariant(
            router,
            snapshot,
            phase=phase,
            strict=strict,
            verbose=verbose,
            quiet=quiet,
        )
    except ConnectivityRegressionError:
        # Strict mode: regression detected.  Mirror the exit-code-6
        # contract documented near the bottom of main() for the post-save
        # output connectivity verification.
        sys.exit(6)


def _finalize_committed_copper_or_demote(
    router: "Autorouter",
    *,
    quiet: bool = False,
) -> None:
    """Post-optimize/post-nudge different-net-short backstop (Issue #4208 / Unit 3).

    The trace optimizer and DRC-nudge passes run AFTER the negotiated
    finalize demote (Unit 1/2).  In an **rtree-less** environment the
    optimizer's :class:`GridCollisionChecker` fallback permits crossing an
    already-overused foreign cell, so optimize can introduce a cross-net
    crossing the pre-optimize finalize gate never saw.  This re-runs the
    Unit-2 seg-seg finalize gate over the CURRENT committed copper
    (reconstructed from ``router.routes``) and demotes any net that became
    a short.  A no-op in the common case (clean state / rtree present) --
    run unconditionally (no ``has_overflow`` / rtree probe) because the
    whole point is defense-in-depth that does not need to know which
    collision checker ran.

    Factored into one shared helper called from all four optimize+nudge
    call sites (``route_with_layer_escalation``,
    ``route_with_rule_relaxation``, ``route_with_combined_escalation``, and
    the base ``main()`` path) rather than copy-pasted, mirroring the
    existing ``_enforce_connectivity_invariant_or_exit`` shared-helper
    pattern.
    """
    demoted = router.revalidate_committed_copper_or_demote()
    if demoted and not quiet:
        print(
            f"  ⚠ Post-optimize backstop demoted {len(demoted)} net(s) whose "
            f"copper became a cross-net short after optimize/nudge: "
            f"{sorted(demoted)}"
        )


def _make_checkpoint_callback(
    pcb_path: Path,
    output_path: Path,
    interval: float,
    quiet: bool = False,
    *,
    preserved_sexp: str = "",
):
    """Build a checkpoint callback for ``route_all_negotiated`` (Issue #2808).

    Returns a callable matching ``Callable[[list[Route], IterationMetrics], None]``
    that atomically flushes the best-so-far snapshot to ``output_path`` no
    more often than every ``interval`` seconds.

    Returns ``None`` when ``interval <= 0`` so the caller can pass the
    return value directly to ``route_all_negotiated(checkpoint_callback=...)``
    without conditional plumbing -- the router already treats ``None`` as
    "no checkpointing".

    The callback is gated by ``time.monotonic()`` (immune to wall-clock
    skew during long routing runs).  The first improvement event always
    fires immediately (no warm-up delay) so the first checkpoint lands
    at iteration 0/1 rather than waiting one full ``interval``.

    Args:
        pcb_path: Source PCB path (passed through to ``_write_routed_pcb``).
        output_path: User's ``--output`` destination.  Honored exactly --
            no ``_4layer`` suffix appending (closes #2809).
        interval: Minimum seconds between checkpoint writes.  ``0`` (or
            negative) disables checkpointing entirely.
        quiet: Suppress the "checkpoint: wrote ..." log line.
        preserved_sexp: Issue #3155.  S-expression of preserved existing
            copper (segments/vias from ``--preserve-existing``).  Appended
            to every checkpoint write so the staged input file -- which
            ``_write_routed_pcb`` strips clean before inserting the
            snapshot, and which the layer-escalation loop *re-reads* on the
            next attempt -- never loses the preserved geometry.  Empty (the
            default) means no preservation, preserving pre-#3155 checkpoint
            bytes exactly.

    Returns:
        Callback or ``None``.
    """
    if interval <= 0:
        return None

    # Mutable container so the closure can write back the timestamp.
    # ``[None]`` sentinel means "no checkpoint written yet"; the first
    # improvement event triggers an immediate write.
    last_time: list[float | None] = [None]

    def _checkpoint(best_routes, best_metrics) -> None:
        from kicad_tools.cli.progress import flush_print

        now = time.monotonic()
        if last_time[0] is not None and (now - last_time[0]) < interval:
            return

        # Materialize sexp from the snapshot (NOT self.routes).  Each
        # Route has its own to_sexp() so we can serialize directly without
        # touching the router's live state.
        route_sexp = "\n\t".join(r.to_sexp() for r in best_routes)
        # Issue #3155: carry preserved existing copper through every
        # checkpoint so the strip-then-rewrite (#2976) inside
        # _write_routed_pcb does not erase it from the staged input.
        if preserved_sexp:
            route_sexp = f"{route_sexp}\n\t{preserved_sexp}" if route_sexp else preserved_sexp
        _write_routed_pcb(
            pcb_path,
            output_path,
            route_sexp,
            is_checkpoint=True,
        )

        last_time[0] = now
        if not quiet:
            flush_print(
                f"  checkpoint: wrote best-so-far "
                f"(iter={best_metrics.iteration}, "
                f"routed={best_metrics.routed_count}, "
                f"overflow={best_metrics.overflow}) to {output_path}"
            )

    return _checkpoint


def _capture_preserved_routes(pcb_path: Path) -> list["Route"]:
    """Parse existing copper from ``pcb_path`` for --preserve-existing (#3155).

    Returns one :class:`Route` per net that has top-level
    ``(segment ...)`` / ``(via ...)`` geometry.  This is captured *once* from
    the freshly-staged input (before any routing or checkpoint write mutates
    it) so the preserved geometry can be re-emitted by both the checkpoint
    callback and the final write -- independent of the per-attempt
    ``router.existing_routes``, which the layer-escalation loop would otherwise
    lose when it re-reads a checkpoint-overwritten staged file.

    Returns an empty list when the file has no routed copper or cannot be read.
    """
    from kicad_tools.router.optimizer.pcb import parse_segments, parse_vias
    from kicad_tools.router.primitives import Route

    try:
        text = Path(pcb_path).read_text()
    except OSError:
        return []

    segments_by_net = parse_segments(text)
    vias_by_net = parse_vias(text)
    all_net_names = set(segments_by_net) | set(vias_by_net)

    routes: list[Route] = []
    for net_name in sorted(all_net_names):
        segs = segments_by_net.get(net_name, [])
        vias = vias_by_net.get(net_name, [])
        if not segs and not vias:
            continue
        net_id = segs[0].net if segs else vias[0].net
        routes.append(Route(net=net_id, net_name=net_name, segments=segs, vias=vias))
    return routes


def _serialize_preserved_routes(
    preserved_routes: list["Route"],
    exclude_net_ids: set[int] | None = None,
) -> str:
    """Serialize preserved routes, skipping any net in ``exclude_net_ids``.

    The exclusion set is the freshly-routed net ids: a net that was both
    pre-existing *and* re-routed must be emitted from the routed copy only,
    never twice (#3155 defensive dedupe).
    """
    exclude = exclude_net_ids or set()
    parts: list[str] = []
    for route in preserved_routes:
        if route.net in exclude:
            continue
        sexp = route.to_sexp()
        if sexp:
            parts.append(sexp)
    return "\n\t".join(parts)


def _finalize_routes(
    router: "Autorouter",
    multi_pad_net_ids: set[int],
    nets_to_route: int,
    quiet: bool = False,
    *,
    strict: bool = False,
    verbose: bool = False,
    aggregate_segment_drop_threshold: float = 0.5,
    preserve_existing: bool = False,
    preserved_routes: list["Route"] | None = None,
) -> tuple[str, dict, dict]:
    """Run cleanup, compute statistics, and generate S-expressions.

    This is the single canonical sequence that must be followed whenever
    route output is produced.  The ordering is:

    1. Snapshot per-net connectivity (issue #3124) so cleanup
       regressions can be detected and reverted.
    2. ``cleanup_artifacts()`` -- mutates ``router.routes`` in place,
       removing net-0 orphans and out-of-bounds segments while preserving
       connectivity.
    3. Enforce the per-net connectivity invariant (issue #3124).  Any
       net whose ``connected_pads`` count strictly decreased across the
       cleanup is reverted (default) or raises in strict mode.
    4. ``to_sexp(skip_cleanup=True)`` -- serialize the (now clean) routes.
    5. ``get_statistics()`` -- compute metrics from the cleaned routes so
       they match what was written to disk.

    All four output paths in route_cmd.py (main CLI, layer escalation,
    rule relaxation, multi-strategy matrix) must use this helper to
    prevent the stats-before-cleanup bug from recurring.

    Args:
        router: The Autorouter instance with completed routes.
        multi_pad_net_ids: Set of net IDs with >= 2 pads (for accurate
            nets_routed counting per Issue #1643).
        nets_to_route: Total number of nets targeted for routing.
        quiet: Suppress console output.
        strict: When True, propagate strict-mode connectivity regressions
            as ``sys.exit(6)``.  When False (the default), regressed nets
            are reverted in place and a warning is logged.
        verbose: When True, the connectivity invariant emits per-net
            diff lines for debugging.
        aggregate_segment_drop_threshold: Fractional segment-count
            reduction beyond which a non-blocking warning is emitted
            (issue #3124 AC #3).  Defaults to 0.5 (warn if cleanup
            drops more than 50% of segments).  This warning fires
            *after* per-net revert so it reflects the final state
            written to disk.
        preserve_existing: Issue #3155 incremental routing.  When True,
            append the S-expression of every preserved route to
            ``route_sexp`` so pre-existing copper -- manually routed nets,
            ``--skip-nets`` geometry, and standalone stitch vias -- survives
            the strip-then-rewrite in ``_write_routed_pcb``.  Preserved
            routes whose net id was also (re-)routed into ``router.routes``
            are skipped to avoid double-emission.
        preserved_routes: The list of preserved :class:`Route` objects to
            re-emit when ``preserve_existing`` is True.  Captured once from
            the freshly-staged input via :func:`_capture_preserved_routes`
            (NOT ``router.existing_routes``, which the escalation loop can
            lose to a checkpoint-overwritten staged file).  ``None`` falls
            back to ``router.existing_routes`` for callers that load fresh
            and never checkpoint.

    Returns:
        Tuple of (route_sexp, stats, cleanup_stats) where:
        - route_sexp: S-expression string for the cleaned routes.
        - stats: Post-cleanup statistics dict from ``get_statistics()``.
        - cleanup_stats: Dict returned by ``cleanup_artifacts()`` with
          keys like ``net0_routes_removed``, ``oob_segments_removed``,
          ``segments_restored``, etc.
    """
    from kicad_tools.cli.progress import flush_print
    from kicad_tools.router.connectivity_invariant import (
        ConnectivityRegressionError,
        enforce_connectivity_invariant,
        snapshot_connectivity,
    )

    # Issue #3124: snapshot per-net connectivity *before* cleanup so we
    # can revert any net that loses a reachable pad during the cleanup
    # pass.  This guards against the finalize-phase regression that
    # silently dropped 5 nets on board 05 (iter-1 best 19/32 -> saved
    # PCB 14/32) because cleanup ran without a connectivity check.
    _ci_snapshot_finalize = snapshot_connectivity(router)

    # Step 1: Run connectivity-aware cleanup before computing statistics
    # so that metrics reflect the segments actually written to the output
    # file.  The cleanup is safe (it restores segments whose removal would
    # fragment a net).  See io.py for the canonical ordering.
    pre_cleanup_segments = sum(len(r.segments) for r in router.routes)
    pre_cleanup_vias = sum(len(r.vias) for r in router.routes)
    cleanup_stats = router.cleanup_artifacts()
    post_cleanup_segments = sum(len(r.segments) for r in router.routes)
    post_cleanup_vias = sum(len(r.vias) for r in router.routes)

    segments_removed = pre_cleanup_segments - post_cleanup_segments
    vias_removed = pre_cleanup_vias - post_cleanup_vias

    if not quiet and (segments_removed > 0 or vias_removed > 0):
        flush_print("\n--- Cleanup ---")
        flush_print(
            f"  Segments: {pre_cleanup_segments} -> {post_cleanup_segments} "
            f"({segments_removed} removed)"
        )
        if vias_removed > 0:
            flush_print(
                f"  Vias:     {pre_cleanup_vias} -> {post_cleanup_vias} ({vias_removed} removed)"
            )
        if cleanup_stats.get("segments_restored", 0) > 0:
            flush_print(
                f"  Restored: {cleanup_stats['segments_restored']} segments, "
                f"{cleanup_stats.get('vias_restored', 0)} vias (connectivity preservation)"
            )

    # Issue #3124: enforce the per-net connectivity invariant around
    # cleanup.  Any net whose connected_pads count strictly decreased
    # is reverted (default mode) or raises ConnectivityRegressionError
    # in strict mode.  This is the same invariant used after optimize
    # and nudge, applied to the previously-uncovered cleanup/finalize
    # step.
    try:
        enforce_connectivity_invariant(
            router,
            _ci_snapshot_finalize,
            phase="finalize",
            strict=strict,
            verbose=verbose,
            quiet=quiet,
        )
    except ConnectivityRegressionError:
        # Strict mode: mirror the exit-code-6 contract used by the
        # optimize / nudge guards.
        sys.exit(6)

    # Issue #3124 AC #3: emit a non-blocking warning when the aggregate
    # segment count drops by more than ``aggregate_segment_drop_threshold``
    # (default 50%).  This is observability, not a hard gate -- a 91%
    # drop on board 05 was the canary that surfaced the underlying
    # connectivity regression.  We measure the ratio *after* revert so
    # the message reflects what was actually written.
    final_segments = sum(len(r.segments) for r in router.routes)
    if pre_cleanup_segments > 0:
        drop_ratio = 1.0 - (final_segments / pre_cleanup_segments)
        if drop_ratio > aggregate_segment_drop_threshold:
            drop_pct = int(round(drop_ratio * 100))
            warning_msg = (
                f"WARNING: finalize cleanup reduced segment count by "
                f"{drop_pct}% ({pre_cleanup_segments} -> {final_segments}). "
                f"This is unusually large; check for connectivity "
                f"regressions."
            )
            logger.warning(warning_msg)
            if not quiet:
                flush_print(f"  {warning_msg}")

    # Step 2: Generate S-expressions from the cleaned routes
    route_sexp = router.to_sexp(skip_cleanup=True)

    # Issue #3155: re-emit preserved copper.  The preserved routes were
    # captured once from the freshly-staged input (``preserved_routes``),
    # falling back to ``router.existing_routes`` (populated by
    # ``load_pcb_for_routing(load_existing_routes=True)``) for callers that
    # load fresh and never checkpoint.  Neither source is touched by
    # ``cleanup_artifacts()`` (which only walks ``router.routes``), so the
    # geometry is re-emitted verbatim and survives the destructive strip in
    # ``_write_routed_pcb`` (#2976).  ``Autorouter.to_sexp()`` is left
    # untouched (regression-safe); we serialize each preserved Route directly.
    #
    # Defensive dedupe: a net that already exists *and* was re-routed would
    # otherwise appear twice (once from ``router.routes``, once from the
    # preserved set).  Skip any preserved route whose net id is present in
    # the freshly-routed set so the freshly-routed copper wins.
    if preserve_existing:
        source_routes = preserved_routes if preserved_routes is not None else router.existing_routes
        if source_routes:
            routed_net_ids = {r.net for r in router.routes}
            # Issue #4170 (Phase 2b-1): a stub net IS routed (the in-region
            # reconnection) but its OUTSIDE boundary-stub copper must still be
            # preserved -- the new in-region trace joins the stub, it does not
            # replace it.  The router records its stub net ids in
            # ``_stub_terminals``; keep those nets' existing copper by removing
            # them from the exclusion set.
            stub_net_ids = set(getattr(router, "_stub_terminals", {}) or {})
            if stub_net_ids:
                routed_net_ids = routed_net_ids - stub_net_ids
            preserved_sexp = _serialize_preserved_routes(
                source_routes, exclude_net_ids=routed_net_ids
            )
            if preserved_sexp:
                emitted = [r for r in source_routes if r.net not in routed_net_ids]
                preserved_segments = sum(len(r.segments) for r in emitted)
                preserved_vias = sum(len(r.vias) for r in emitted)
                route_sexp = f"{route_sexp}\n\t{preserved_sexp}" if route_sexp else preserved_sexp
                if not quiet:
                    flush_print(
                        f"  Preserved existing: {preserved_segments} segments, "
                        f"{preserved_vias} vias ({len(emitted)} routes)"
                    )

    # Step 3: Compute statistics from the cleaned routes
    stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)

    if not quiet:
        # Issue #3942 (Bug C): these statistics are scoped to the
        # newly-routed multi-pad nets (``get_statistics`` is filtered by
        # ``multi_pad_net_ids``).  When existing copper is preserved
        # (``--preserve-existing``), the written PCB carries additional
        # segments/vias re-emitted above and logged on the "Preserved
        # existing:" line -- so the file totals are these counts PLUS the
        # preserved copper.  Label the heading so the numbers are not
        # mistaken for file-final totals.
        _results_heading = "\n--- Results ---"
        if preserve_existing:
            _results_heading += " (newly-routed nets only; preserved copper counted above)"
        flush_print(_results_heading)
        flush_print(f"  Routes created:  {stats['routes']}")
        flush_print(f"  Segments:        {stats['segments']}")
        flush_print(f"  Vias:            {stats['vias']}")
        flush_print(f"  Total length:    {stats['total_length_mm']:.2f}mm")
        flush_print(f"  Nets routed:     {stats['nets_routed']}/{nets_to_route}")
        # Issue #3311 / #3255: partial-route count is meaningful signal
        # that today's strict-connect headline hides.  Surfaced here so
        # CI / loom-summary scrapers see both numbers next to each other.
        partial_count = stats.get("nets_partial", 0)
        unrouted_count = stats.get("nets_unrouted", 0)
        flush_print(
            f"  Partial routes:  {partial_count}/{nets_to_route} "
            f"-- have segments, not all pads connected"
        )
        flush_print(f"  Unrouted:        {unrouted_count}/{nets_to_route} -- no segments at all")

    return route_sexp, stats, cleanup_stats


# Global state for Ctrl+C handling
_interrupt_state = {
    "interrupted": False,
    "router": None,
    "output_path": None,
    "pcb_path": None,
    "quiet": False,
    "best_completed_attempt": False,
}


def _handle_interrupt(signum, frame):
    """Handle Ctrl+C by setting the interrupted flag and saving partial results."""
    _interrupt_state["interrupted"] = True
    if not _interrupt_state["quiet"]:
        print("\n\n⚠ Interrupt received! Saving partial results...")
    # Save partial results immediately
    saved = _save_partial_results()
    # Exit with code 5 to indicate SIGINT interruption with saved partial results.
    # This is distinct from code 2 (partial routing below threshold) so scripts can
    # distinguish user-interrupted from router-decided-partial.
    sys.exit(5 if saved else 130)  # 130 = 128 + SIGINT (2)


def _save_partial_results() -> bool:
    """Save partial routing results if interrupted.

    Returns:
        True if partial results were saved, False otherwise.
    """
    router = _interrupt_state["router"]
    output_path = _interrupt_state["output_path"]
    pcb_path = _interrupt_state["pcb_path"]
    quiet = _interrupt_state["quiet"]

    if router is None or output_path is None or pcb_path is None:
        return False

    if not router.routes:
        if not quiet:
            print("  No routes to save.")
        return False

    try:
        # Read original PCB content
        original_content = pcb_path.read_text()

        # Get partial route S-expressions
        route_sexp = router.to_sexp()

        if route_sexp:
            # When the interrupt state holds a best *completed* attempt from
            # adaptive-rules routing, write to the main output path (not
            # _partial) because the result is a full routing pass.
            if _interrupt_state.get("best_completed_attempt"):
                save_path = output_path
            else:
                save_path = output_path.with_stem(output_path.stem + "_partial")

            # Insert routes before final closing parenthesis
            output_content = _insert_sexp_before_closing(original_content, route_sexp)

            save_path.write_text(output_content)

            if not quiet:
                stats = router.get_statistics()
                # The _partial file is a raw router snapshot (routes inserted
                # into the *unrouted* source) written BEFORE optimize/cleanup
                # and DRC. It is therefore less-processed than the canonical
                # -o output and should not be treated as authoritative. Only
                # the adaptive best-completed case writes to output_path itself.
                if save_path == output_path:
                    print(f"\n  Partial results saved to: {save_path}")
                else:
                    print(
                        f"\n  Raw partial snapshot saved to: {save_path} "
                        f"(pre-optimize, pre-DRC; NOT canonical)"
                    )
                    print(f"  Canonical output remains: {output_path}")
                print(f"    Nets routed: {stats['nets_routed']}")
                print(f"    Segments: {stats['segments']}")
                print(f"    Vias: {stats['vias']}")
            return True
    except Exception as e:
        if not quiet:
            print(f"  Error saving partial results: {e}")

    return False


def _export_failed_nets(
    router: "Autorouter",
    net_map: dict[str, int],
    export_path: str,
    quiet: bool = False,
    nets_to_route_ids: set[int] | None = None,
) -> bool:
    """Export the list of failed (unrouted) net names to a file.

    Writes one net name per line to the specified path.

    Args:
        router: The Autorouter instance with completed routing.
        net_map: Mapping of net names to net IDs.
        export_path: File path to write the failed net names.
        quiet: If True, suppress output messages.
        nets_to_route_ids: Optional set of net IDs targeted for routing
            (multi-pad signal nets).  When provided, only nets in this set
            are considered candidates so single-pad and power nets are
            excluded from the export.

    Returns:
        True if the file was written successfully, False otherwise.
    """
    reverse_net = {v: k for k, v in net_map.items() if v > 0}
    routed_net_ids = {route.net for route in router.routes}
    if nets_to_route_ids is not None:
        unrouted_ids = nets_to_route_ids - routed_net_ids
    else:
        all_net_ids = {v for k, v in net_map.items() if v > 0}
        unrouted_ids = all_net_ids - routed_net_ids

    if not unrouted_ids:
        if not quiet:
            print("  No failed nets to export.")
        return False

    try:
        failed_names = sorted(reverse_net.get(nid, f"Net_{nid}") for nid in unrouted_ids)
        export_file = Path(export_path)
        export_file.write_text("\n".join(failed_names) + "\n")
        if not quiet:
            print(f"  Failed nets exported to: {export_file} ({len(failed_names)} nets)")
        return True
    except Exception as e:
        if not quiet:
            print(f"  Error exporting failed nets: {e}")
        return False


def show_preview(
    router,
    net_map: dict[str, int],
    nets_to_route: int,
    quiet: bool = False,
    nets_to_route_ids: set[int] | None = None,
) -> str:
    """Display routing preview with per-net breakdown.

    Args:
        router: The Autorouter instance with completed routes
        net_map: Mapping of net names to net IDs
        nets_to_route: Total number of nets expected to be routed
        quiet: If True, skip interactive prompt and return 'n'
        nets_to_route_ids: Optional set of net IDs targeted for routing.
            When provided, ``nets_routed`` only counts nets in this set.

    Returns:
        User response: 'y' (apply), 'n' (reject), or 'e' (edit - future)
    """
    # Build reverse mapping: net_id -> net_name
    reverse_net = {v: k for k, v in net_map.items()}

    # Collect per-net statistics
    net_stats: dict[int, dict] = {}
    for route in router.routes:
        net_id = route.net
        if net_id not in net_stats:
            net_stats[net_id] = {
                "net_name": route.net_name or reverse_net.get(net_id, f"Net {net_id}"),
                "segments": 0,
                "vias": 0,
                "length": 0.0,
                "layers": set(),
            }
        stats = net_stats[net_id]
        stats["segments"] += len(route.segments)
        stats["vias"] += len(route.vias)
        for seg in route.segments:
            dx = seg.x2 - seg.x1
            dy = seg.y2 - seg.y1
            stats["length"] += math.sqrt(dx * dx + dy * dy)
            stats["layers"].add(seg.layer.kicad_name)

    # Identify unrouted nets — filter to target population so the
    # "No path found" list only shows actual routing candidates,
    # not skipped power nets or single-pad nets (Issue #1833).
    routed_net_ids = set(net_stats.keys())
    if nets_to_route_ids is not None:
        unrouted_ids = nets_to_route_ids - routed_net_ids
    else:
        all_net_ids = {v for k, v in net_map.items() if v > 0}
        unrouted_ids = all_net_ids - routed_net_ids

    # Print header
    print("\n" + "=" * 60)
    print("ROUTING PREVIEW")
    print("=" * 60)

    # Print per-net breakdown
    for net_id in sorted(net_stats.keys()):
        stats = net_stats[net_id]
        net_name = stats["net_name"]
        layers = " -> ".join(sorted(stats["layers"]))
        via_info = f", {stats['vias']} via(s)" if stats["vias"] > 0 else ""

        print(f"\nNet: {net_name}")
        print(f"  Layers:   {layers}")
        print(f"  Length:   {stats['length']:.2f}mm")
        print(f"  Segments: {stats['segments']}{via_info}")
        print("  Status:   \u2713 Routed")

    # Show unrouted nets
    if unrouted_ids:
        print("\n" + "-" * 40)
        for net_id in sorted(unrouted_ids):
            net_name = reverse_net.get(net_id, f"Net {net_id}")
            if net_name:  # Skip empty net names
                print(f"\nNet: {net_name}")
                print("  Status:   \u2717 No path found")

    # Summary statistics — filter to target population (Issue #1643)
    overall_stats = router.get_statistics(nets_to_route_ids=nets_to_route_ids)
    nets_routed = overall_stats["nets_routed"]
    success_rate = (nets_routed / nets_to_route * 100) if nets_to_route > 0 else 0

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Nets routed:  {nets_routed}/{nets_to_route} ({success_rate:.0f}%)")
    # Issue #3311 / #3255: surface partial / unrouted breakdown alongside
    # the strict-connect headline so dashboards and humans see the full
    # picture (e.g. chorus: 5/48 strict but 28/48 partial -- meaningful
    # work that the headline alone obscures).
    partial_count = overall_stats.get("nets_partial", 0)
    unrouted_count = overall_stats.get("nets_unrouted", 0)
    partial_pct = (partial_count / nets_to_route * 100) if nets_to_route > 0 else 0
    unrouted_pct = (unrouted_count / nets_to_route * 100) if nets_to_route > 0 else 0
    print(
        f"  Partial routes: {partial_count}/{nets_to_route} ({partial_pct:.0f}%) "
        f"-- have segments, not all pads connected"
    )
    print(
        f"  Unrouted:     {unrouted_count}/{nets_to_route} ({unrouted_pct:.0f}%) "
        f"-- no segments at all"
    )
    print(f"  Total length: {overall_stats['total_length_mm']:.2f}mm")
    print(f"  Total vias:   {overall_stats['vias']}")
    print(f"  Segments:     {overall_stats['segments']}")

    # Layer usage summary
    all_layers: dict[str, int] = {}
    for route in router.routes:
        for seg in route.segments:
            layer_name = seg.layer.kicad_name
            all_layers[layer_name] = all_layers.get(layer_name, 0) + 1

    if all_layers:
        print("\n  Layer usage:")
        for layer_name, count in sorted(all_layers.items()):
            print(f"    {layer_name}: {count} segments")

    print("=" * 60)

    # Interactive prompt (unless quiet mode)
    if quiet:
        return "n"

    print("\nApply routes? [y/N/e(dit)]:", end=" ")
    try:
        response = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return "n"

    if response in ("y", "yes"):
        return "y"
    elif response in ("e", "edit"):
        print("  (Edit mode not yet implemented - treating as reject)")
        return "n"
    else:
        return "n"


def _write_net_class_map_sidecar(
    output_path: Path,
    net_class_map: dict | None,
    quiet: bool = False,
) -> None:
    """Serialize the router's net-class map to a sidecar next to the PCB.

    Issue #3917 Defect 1: ``net_class_map_to_dict()`` existed and was
    round-trip tested but was never called from the route step, so the
    ``output/net_class_map.json`` sidecar that every user-facing hint (and
    ``kct check`` auto-discovery) points at was never actually written.

    Writes ``<output_dir>/net_class_map.json`` adjacent to the routed PCB
    so ``kct check`` can auto-load it and fire the sidecar-gated skew /
    continuity rules.  A blocked write (read-only output dir) is a
    non-fatal warning, never a route failure.

    Args:
        output_path: Path to the routed PCB file.  The sidecar is written
            to the same directory.
        net_class_map: The autorouter's ``{net_name: NetClassRouting}``
            map.  Skipped when ``None`` or empty (an empty map would write
            a misleading sidecar that the check-side probe would treat as
            present).
        quiet: If True, suppress the confirmation line.
    """
    if not net_class_map:
        return
    import json

    from kicad_tools.router.rules import net_class_map_to_dict

    sidecar_path = output_path.parent / "net_class_map.json"
    try:
        payload = net_class_map_to_dict(net_class_map)
        sidecar_path.write_text(json.dumps(payload, indent=2))
    except (OSError, TypeError, ValueError) as e:
        # Non-fatal: a blocked / read-only output directory (or an
        # unexpectedly non-serializable map) must not fail the route.
        if not quiet:
            print(f"  Warning: could not write net-class-map sidecar: {e}")
        return
    if not quiet:
        print(f"  Net-class-map sidecar: {sidecar_path}")


def _write_drc_constraint_sidecars(
    output_path: Path,
    manufacturer: str,
    layers: int,
    copper_oz: float = 1.0,
    quiet: bool = False,
) -> None:
    """Emit ``.kicad_pro`` + ``.kicad_dru`` next to the routed PCB.

    Issue #3919: ``kicad-cli pcb drc`` auto-loads ``<board>.kicad_pro`` from
    the PCB's directory to read the board design rules (min track width,
    clearance, via diameter, etc.).  When no project file is present it falls
    back to KiCad's stricter built-in defaults (0.20 mm track, 0.50 mm via,
    0.20 mm clearance), producing *false* geometric violations for boards
    routed at a finer manufacturer capability tier (e.g. board 03 flagged 87
    bogus ``track_width`` errors on 0.15 mm traces).  Missing sidecars also
    make verdicts state-dependent: a prior run that happens to write the
    sidecars silently changes the next run's DRC result.

    Resolving the manufacturer profile here (the same ``manufacturer`` +
    ``layers`` + ``copper_oz`` the internal :class:`DRCChecker` resolves)
    and delegating to :func:`write_drc_constraints` writes both files
    atomically *before* the ``run_geometric_drc`` cross-check, so kicad-cli
    -- ours or a later user invocation -- judges against the intended
    constraints deterministically.  This mirrors the net-class-map sidecar
    precedent (Issue #3917 / PR #3948): a read-only output directory or an
    unknown manufacturer degrades to a non-fatal warning, never a route
    failure.

    Args:
        output_path: Path to the routed PCB file.  The sidecars are written
            to the same directory (``<board>.kicad_pro`` / ``.kicad_dru``).
        manufacturer: Manufacturer profile ID (e.g. ``"jlcpcb-tier1"``).
        layers: Copper-layer count (threaded into the profile's rules).
        copper_oz: Copper weight in oz (defaults to 1.0, the system default
            and the correct value for all 8 demo boards).
        quiet: If True, suppress the confirmation line.
    """
    try:
        from kicad_tools.manufacturers import get_profile, write_drc_constraints

        profile = get_profile(manufacturer)
        rules = profile.get_design_rules(layers=layers, copper_oz=copper_oz)
        written = write_drc_constraints(
            output_path,
            rules,
            manufacturer_id=profile.id,
            layers=layers,
            copper_oz=copper_oz,
        )
    except (ValueError, OSError, KeyError) as e:
        # Non-fatal: an unknown manufacturer (ValueError) or a read-only /
        # blocked output directory (OSError) must not fail the route.
        if not quiet:
            print(f"  Warning: could not write DRC-constraint sidecars: {e}")
        return
    if not quiet and written:
        print(f"  DRC-constraint sidecars: {', '.join(str(p) for p in written)}")


def run_post_route_drc(
    output_path: Path,
    manufacturer: str,
    layers: int,
    quiet: bool = False,
    net_class_map: dict | None = None,
    copper_oz: float = 1.0,
    strict_drc: bool = False,
) -> tuple[int, int]:
    """Run DRC validation on the routed PCB.

    Args:
        output_path: Path to the routed PCB file
        manufacturer: Manufacturer profile for DRC rules (e.g., "jlcpcb")
        layers: Number of PCB layers
        quiet: If True, suppress output
        net_class_map: Optional ``{net_name: NetClassRouting}`` map from
            the autorouter.  When provided, the differential-pair
            routing-continuity rule (Phase 2.5b / Issue #2652) re-derives
            its engagement state from this map + the routed PCB and
            fires per Epic #2556 Phase 2G.  Without it, that rule is a
            no-op (graceful degradation).
        copper_oz: Copper weight in oz for the manufacturer profile's
            design rules (defaults to 1.0, the system default and the
            correct value for all 8 demo boards).  Threaded into both the
            internal :class:`DRCChecker` and the emitted ``.kicad_pro`` /
            ``.kicad_dru`` sidecars so both engines judge consistently.
        strict_drc: When ``True`` (``kct route --strict-drc``), a native
            geometric DRC that did NOT actually run (kicad-cli absent,
            timed out, crashed, produced no report) is treated as a HARD
            FAILURE -- the returned ``error_count`` is bumped by 1 so the
            route command exits non-zero, and a prominent message states
            that the PASS is not authoritative.  Default ``False``
            preserves the graceful-degradation soft NOTE for KiCad-less
            environments (Issue #4178).

    Returns:
        Tuple of (error_count, warning_count)
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    # Issue #3917 Defect 1: persist the net-class map as a sidecar next to
    # the routed PCB so ``kct check`` (and re-runs of this DRC) can
    # auto-load it and fire the sidecar-gated skew / continuity rules.
    # This is the shared post-route DRC entry for all three route flows
    # (default multi-layer, rule-relaxation, and single-layer), so writing
    # here covers every callsite in one place.
    _write_net_class_map_sidecar(output_path, net_class_map, quiet=quiet)

    # Issue #3919: emit the ``.kicad_pro`` + ``.kicad_dru`` constraint
    # sidecars from the manufacturer profile *before* the geometric DRC
    # cross-check below, so ``kicad-cli pcb drc`` auto-loads the intended
    # capability-tier floors instead of KiCad's stricter built-in defaults
    # (which flag false track_width/clearance/via violations on finer
    # traces).  Writing here -- the shared post-route DRC entry for every
    # route flow -- makes the verdict deterministic and independent of any
    # sidecars a prior run may have left behind.
    _write_drc_constraint_sidecars(
        output_path, manufacturer, layers, copper_oz=copper_oz, quiet=quiet
    )

    try:
        # Load the routed PCB
        pcb = PCB.load(str(output_path))

        # Run DRC
        checker = DRCChecker(
            pcb,
            manufacturer=manufacturer,
            layers=layers,
            copper_oz=copper_oz,
            net_class_map=net_class_map,
        )
        results = checker.check_all()

        error_count = results.error_count
        warning_count = results.warning_count

        # Reconcile against native KiCad geometric DRC (kicad-cli pcb drc).
        # The internal DRCChecker is structurally blind to several KiCad
        # violation classes (shorting_items / tracks_crossing on net 0,
        # copper_edge_clearance without an Edge.Cuts outline,
        # solder_mask_bridge, silk_* overlaps), so a clean internal verdict
        # must NOT be reported as an unqualified PASS when kicad-cli finds
        # geometric defects.  This mirrors DesignAuditor._merge_geometric_drc
        # via the shared run_geometric_drc helper (issue #3803).
        from kicad_tools.drc import run_geometric_drc

        geo = run_geometric_drc(output_path)

        if geo.ran and geo.error_count > 0:
            # Native DRC found blocking geometric violations -- fold them into
            # the verdict so PASS requires BOTH engines clean.
            error_count += geo.error_count

        # Issue #4178: under --strict-drc, "kicad-cli DRC did not actually
        # run" is a HARD FAILURE, not a soft NOTE.  A clean internal verdict
        # is not authoritative when the native engine never ran, so bump the
        # error count to force a non-zero exit.  The soft-degradation default
        # (strict_drc=False) preserves the internal-only PASS for KiCad-less
        # environments.
        strict_drc_failed = strict_drc and not geo.ran
        if strict_drc_failed:
            error_count += 1

        if not quiet and strict_drc_failed:
            print("\n--- DRC Validation ---")
            reason_text = geo.note or "native geometric DRC did not run"
            print(f"  STRICT DRC FAILURE: native kicad-cli DRC did not run ({reason_text}).")
            print(
                "    The internal-engine verdict is NOT authoritative; "
                "--strict-drc requires kicad-cli DRC to run and pass."
            )
            print(
                "    Re-run without --strict-drc to allow a soft internal-only "
                "PASS, or install/repair kicad-cli."
            )
            if results.error_count > 0 or warning_count > 0:
                # Also surface any internal findings below the strict banner.
                print(f"  Internal errors: {results.error_count}")
                if warning_count > 0:
                    print(f"  Internal warnings: {warning_count}")
        elif not quiet:
            print("\n--- DRC Validation ---")
            if error_count == 0 and warning_count == 0:
                if geo.ran:
                    print(f"  DRC PASSED ({manufacturer} profile, {layers} layers)")
                    print("    (internal engine + kicad-cli geometric DRC both clean)")
                else:
                    # kicad-cli unavailable / skipped: never overstate cleanliness.
                    print(f"  DRC PASSED ({manufacturer} profile, {layers} layers)")
                    note = geo.note or "geometric DRC skipped"
                    print(f"    NOTE: {note} -> internal-engine-only PASS")
                    print("    (native kicad-cli DRC was not run; PASS is not authoritative)")
            else:
                # Loud divergence warning: internal engine clean but native
                # DRC flagged geometric violations the internal engine missed.
                if results.error_count == 0 and geo.has_errors:
                    top = geo.top_types(4)
                    type_summary = ", ".join(f"{t} ({c})" for t, c in top)
                    print(
                        "  WARNING: internal DRC clean but kicad-cli found "
                        f"{geo.error_count} geometric violation(s): {type_summary}"
                    )
                    print("           PASS withheld -- native KiCad DRC is authoritative.")
                if error_count > 0:
                    print(f"  Errors:   {error_count}")
                if warning_count > 0:
                    print(f"  Warnings: {warning_count}")

                # Show first few violations
                shown = 0
                for v in results.errors[:5]:
                    location = (
                        f" at ({v.location[0]:.2f}, {v.location[1]:.2f})" if v.location else ""
                    )
                    print(f"    - {v.rule_id}: {v.message}{location}")
                    shown += 1
                if error_count > 5:
                    print(f"    ... and {error_count - 5} more errors")

                if warning_count > 0 and shown < 5:
                    for v in results.warnings[: 5 - shown]:
                        location = (
                            f" at ({v.location[0]:.2f}, {v.location[1]:.2f})" if v.location else ""
                        )
                        print(f"    - {v.rule_id}: {v.message}{location}")
                    if warning_count > (5 - shown):
                        print(f"    ... and {warning_count - (5 - shown)} more warnings")

                print(f"\n  Run 'kct check {output_path} --mfr {manufacturer}' for full details")
                if error_count > 0:
                    print(f"  Run 'kct fix-drc {output_path}' to auto-repair clearance violations")

        return error_count, warning_count

    except Exception as e:
        if not quiet:
            print("\n--- DRC Validation ---")
            print(f"  Warning: DRC check failed: {e}")
        return -1, -1  # Indicate failure to run DRC


def _run_auto_fix(
    output_path: Path,
    max_passes: int = 1,
    quiet: bool = False,
    args=None,
) -> int:
    """Run fix-drc on the routed PCB to auto-repair DRC violations.

    Args:
        output_path: Path to the routed PCB file to repair.
        max_passes: Number of iterative repair passes.
        quiet: If True, suppress output.
        args: Parsed ``route`` CLI args.  When provided, the function
            honors ``args._wall_clock_deadline`` (issue #2802) and skips
            the auto-fix invocation entirely if the total budget has been
            consumed.  Issue #3238: also stamps ``args._auto_fix_status``
            with one of ``"ran"`` / ``"skipped_deadline"`` /
            ``"not_invoked"`` so callers can distinguish silent skips
            from benign no-ops.  Optional for backward compatibility
            with callers that do not have the args namespace handy.

    Returns:
        Exit code from fix_drc_cmd.main() (0 = all violations fixed).
        Returns a non-zero "skipped" code (1) when the wall-clock
        deadline has already expired.
    """
    from kicad_tools.cli.fix_drc_cmd import main as fix_drc_main

    # Issue #2802 + #3238: skip auto-fix when the *total* wall-clock
    # budget has been exhausted.  We deliberately check the total
    # deadline (not the routing deadline) here because the routing
    # deadline is *supposed* to be expired by the time we reach this
    # function -- that's exactly when the reserved auto-fix budget
    # (issue #3238) is meant to kick in.  Only when the entire wall-clock
    # budget is gone do we have to skip.
    if args is not None and _total_deadline_expired(args):
        # Issue #3238: stamp structured state so callers (and tests) can
        # distinguish a silent skip from the "fix-drc found nothing to
        # do" no-op (both used to return 1).
        with contextlib.suppress(AttributeError):
            args._auto_fix_status = "skipped_deadline"
        if not quiet:
            print("\n--- Auto-Fix DRC Violations ---")
            print("  Skipping: total wall-clock deadline reached (--timeout, issue #2802)")
            print(
                "  AUTOFIX_SKIPPED_BUDGET_EXHAUSTED: routing consumed the entire "
                "--timeout budget; auto-fix received zero seconds to clean DRC "
                "violations.  Consider raising --timeout or reducing the routing "
                "workload (e.g. --max-layers, --strategy).  See issue #3238."
            )
        # Issue #3238: also emit a stable machine-readable token to
        # stderr so CI gates can grep without parsing the full route
        # log.  The token is intentionally separate from the friendlier
        # stdout message so log parsers can rely on it.
        print(
            "AUTOFIX_SKIPPED_BUDGET_EXHAUSTED: --timeout exhausted by routing; "
            "auto-fix did not run (issue #3238)",
            file=sys.stderr,
        )
        return 1

    # Issue #3238: stamp "ran" before invoking fix-drc so even an
    # exception path leaves a defensible state on args.
    if args is not None:
        with contextlib.suppress(AttributeError):
            args._auto_fix_status = "ran"

    if not quiet:
        print("\n--- Auto-Fix DRC Violations ---")

    fix_argv = [
        str(output_path),
        "--max-passes",
        str(max_passes),
        "--max-displacement",
        "2.0",
        "--local-reroute",
    ]
    if quiet:
        fix_argv.append("--quiet")

    result = fix_drc_main(fix_argv)

    if not quiet:
        # Exit-code contract from fix_drc_cmd.main (see fix_drc_cmd.py:375-390):
        #   0 = all targeted violations repaired
        #   1 = no violations found or no progress made
        #   2 = partial repair (some repairable violations remain)
        #   3 = connectivity rollback (issue #2839)
        #
        # Each code gets a distinct message so the user can distinguish
        # "rollback fired and silently zeroed out the work" (3) from
        # "fix-drc tried and partially succeeded" (2) and from "nothing
        # could be done at all" (1).
        if result == 0:
            print("  Auto-fix: all targeted violations repaired!")
        elif result == 3:
            print(
                "  Auto-fix: rolled back due to connectivity regression "
                "(nudges would have broken at least one net)."
            )
            print("  Run 'kct fix-drc <pcb> --no-connectivity-check' to apply the nudges anyway")
            print("  (only safe when partial-completion regressions are acceptable).")
        elif result == 2:
            print("  Auto-fix: partial repair; some violations remain.")
        elif result == 1:
            print("  Auto-fix: no progress made (manual repair may be needed).")
        else:
            print("  Auto-fix: some violations remain (manual repair may be needed)")

    return result


def _should_auto_fix(args) -> bool:
    """Determine whether auto-fix should run based on CLI flags.

    Auto-fix runs when --auto-fix is set (which is also implied by
    --auto-fix-passes), but is suppressed by --dry-run and --skip-drc.
    """
    auto_fix = getattr(args, "auto_fix", False)
    dry_run = getattr(args, "dry_run", False)
    skip_drc = getattr(args, "skip_drc", False)

    if not auto_fix:
        return False
    if dry_run or skip_drc:
        return False
    return True


# =============================================================================
# Issue #2595: Placement-routing feedback helpers
# =============================================================================


def _parse_ref_list(value: str | None) -> set[str]:
    """Parse a comma-separated component-ref list into a set."""
    if not value:
        return set()
    return {ref.strip() for ref in value.split(",") if ref.strip()}


def _auto_detect_anchored_refs(pcb) -> set[str]:
    """Auto-detect components that should be anchored during placement feedback.

    A footprint is anchored when *any* of the following hold:

    1. Its reference starts with ``J`` or ``P`` (connectors and headers
       are mechanically constrained -- the human chose their position
       on the board edge for cabling and they cannot move freely).
    2. Its KiCad ``locked`` attribute is set to True (the human pinned
       it explicitly in the layout editor).

    Args:
        pcb: A loaded ``PCB`` object whose ``footprints`` are iterable.

    Returns:
        Set of component references to anchor.
    """
    anchored: set[str] = set()
    for fp in getattr(pcb, "footprints", []) or []:
        ref = getattr(fp, "reference", "") or ""
        if not ref:
            continue
        # Connectors (J*) and headers/test-points (P*) are mechanically
        # constrained.  We deliberately do NOT auto-anchor U* or any
        # other prefix -- ICs and passives are the things we WANT the
        # feedback loop to be able to nudge.
        if ref[0].upper() in ("J", "P"):
            anchored.add(ref)
            continue
        if getattr(fp, "locked", False):
            anchored.add(ref)
    return anchored


def _resolve_placement_feedback_anchors(pcb, args, quiet: bool = False) -> set[str]:
    """Compute the final set of anchored refs for the feedback loop.

    Combines auto-detected anchors (connectors, locked footprints) with
    the user's ``--placement-feedback-anchor`` overrides, then removes
    any refs the user explicitly opted out via
    ``--placement-feedback-no-anchor``.

    Issue #4151: user-supplied ``--placement-feedback-anchor`` /
    ``--placement-feedback-no-anchor`` refs are validated against the
    board's actual footprint references.  A typo'd ref that matches no
    footprint used to be silently absorbed into the set with no signal;
    we now emit a tolerant warning naming the unmatched ref(s) (same
    silently-inert-configuration failure class as #4149).

    Args:
        pcb: Loaded PCB object.
        args: Parsed CLI args.
        quiet: When True, suppress the unmatched-ref warning.

    Returns:
        Set of refs to anchor.
    """
    board_refs = {
        (getattr(fp, "reference", "") or "") for fp in getattr(pcb, "footprints", []) or []
    }
    board_refs.discard("")

    requested_anchor = _parse_ref_list(getattr(args, "placement_feedback_anchor", None))
    requested_no_anchor = _parse_ref_list(getattr(args, "placement_feedback_no_anchor", None))

    if not quiet:
        unknown = sorted((requested_anchor | requested_no_anchor) - board_refs)
        if unknown:
            print(
                "Warning: --placement-feedback-anchor/--placement-feedback-no-anchor "
                f"ref(s) not found on board: {', '.join(unknown)}"
            )

    anchors = _auto_detect_anchored_refs(pcb)
    anchors |= requested_anchor
    anchors -= requested_no_anchor
    return anchors


def _placement_diff_path(args, pcb_path: Path) -> Path:
    """Resolve the path of the ``<output>_placement_diff.json`` artifact."""
    if getattr(args, "output", None):
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_routed")
    return output_path.with_suffix("").with_name(output_path.stem + "_placement_diff.json")


def _run_placement_feedback(
    router,
    pcb_path: Path,
    args,
    quiet: bool,
) -> list[dict] | None:
    """Drive the placement-routing feedback loop for ``kct route``.

    Loads the PCB so the loop can mutate footprint positions, computes
    the anchor set, invokes ``router.route_with_placement_feedback``,
    persists ``<output>_placement_diff.json`` beside the routed PCB,
    and prints a human-readable summary of what moved.

    Args:
        router: The ``Autorouter`` whose state will be reset by the loop.
        pcb_path: Path to the (already-loaded) input PCB so we can
            create an in-memory ``PCB`` for placement mutation.
        args: Parsed CLI args (must include ``placement_feedback_*``).
        quiet: When True, suppress all stdout output.

    Returns:
        The placement diff as a list of dicts (suitable for JSON
        serialization), or None when the loop did not run / produced
        no diff.
    """
    import json

    from kicad_tools.schema.pcb import PCB

    # Issue #2802: bail out early when the total wall-clock budget has
    # already been consumed by upstream stages.  Each PF iteration kicks
    # off a fresh negotiated re-route, so skipping the loop entirely
    # preserves the remaining time for downstream steps (optimize, DRC,
    # auto-fix) instead of burning it on routing we'll never get to
    # finish.
    if _deadline_expired(args):
        if not quiet:
            print("\n--- Placement-Routing Feedback ---")
            print("  Skipping: total wall-clock deadline reached (--timeout, issue #2802)")
        return None

    if not quiet:
        print("\n--- Placement-Routing Feedback ---")
        failed = router.get_failed_nets()
        print(f"  Initial pass left {len(failed)} unrouted net(s); attempting feedback")

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as exc:
        if not quiet:
            print(f"  Warning: could not load PCB for feedback ({exc}); skipping")
        return None

    anchored = _resolve_placement_feedback_anchors(pcb, args, quiet=quiet)
    if not quiet and anchored:
        print(f"  Anchored refs ({len(anchored)}): {', '.join(sorted(anchored))}")

    budget = int(getattr(args, "placement_feedback_budget", 3) or 3)
    max_movement = float(getattr(args, "placement_feedback_max_movement", 5.0) or 5.0)
    use_negotiated = getattr(args, "strategy", "negotiated") == "negotiated"

    # Issue #2802: forward the remaining wall-clock budget as the per-call
    # ``timeout`` so each PF iteration's inner re-route gets a smaller
    # slice of the deadline as time runs out, rather than always asking
    # for a fresh ``args.timeout`` slot.  Falls back to ``args.timeout``
    # when no deadline is configured.
    timeout = _budgeted_timeout(args)
    per_net_timeout = getattr(args, "per_net_timeout", None)

    # Issue #2606: stagnation + outer-timeout guards.  Defaults match
    # the parser: patience=3, outer_timeout=None.
    stagnation_patience = int(getattr(args, "placement_feedback_stagnation_patience", 3) or 0)
    outer_timeout_raw = getattr(args, "placement_feedback_outer_timeout", None)
    outer_timeout = float(outer_timeout_raw) if outer_timeout_raw is not None else None

    # Issue #2802: cap the PF outer-timeout at the remaining total budget
    # so the feedback loop terminates when the deadline fires even if the
    # user did not pass ``--placement-feedback-outer-timeout``.  When both
    # are set, we take the smaller (most restrictive) value.
    _remaining = _remaining_budget(args)
    if _remaining is not None:
        outer_timeout = _remaining if outer_timeout is None else min(outer_timeout, _remaining)

    # Issue #4151: gate the per-iteration firehose (candidate breakdowns,
    # per-move deltas emitted by ``PlacementFeedbackLoop`` under
    # ``self.verbose``) on ``-v``/``--verbose`` specifically, rather than
    # on ``not quiet``.  The always-on one-line summary below
    # (iterations / exit reason / components moved / final failed nets)
    # is emitted independently whenever ``not quiet``, so a default run
    # still gets telemetry -- just without the noisy per-iteration detail.
    loop_verbose = (not quiet) and bool(getattr(args, "verbose", False))

    try:
        result = router.route_with_placement_feedback(
            pcb=pcb,
            max_adjustments=budget,
            use_negotiated=use_negotiated,
            verbose=loop_verbose,
            fixed_refs=anchored,
            max_movement=max_movement,
            timeout=timeout,
            per_net_timeout=per_net_timeout,
            stagnation_patience=stagnation_patience,
            outer_timeout=outer_timeout,
        )
    except Exception as exc:
        if not quiet:
            print(f"  Warning: placement feedback failed ({exc}); keeping initial routes")
        return None

    if not quiet:
        print(f"  Feedback iterations: {result.iterations}")
        # Issue #2606: surface the exit_reason so CI / humans can
        # distinguish "we made it" (pf_converged) from "we plateaued"
        # (pf_stagnated) or "we hit the wall clock" (pf_timeout).
        print(f"  Exit reason:        {result.exit_reason}")
        print(f"  Components moved:   {result.total_components_moved}")
        print(f"  Final failed nets:  {len(result.failed_nets)}")

    diff_data = [entry.to_dict() for entry in result.placement_diff]

    # Persist the diff JSON beside the routed PCB.  We write it even
    # when the diff is empty so consumers (CI, humans) can rely on the
    # file's presence to detect that the feedback loop was invoked.
    diff_path = _placement_diff_path(args, pcb_path)
    try:
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(json.dumps(diff_data, indent=2) + "\n")
        if not quiet:
            print(f"  Placement diff saved to: {diff_path}")
    except OSError as exc:
        if not quiet:
            print(f"  Warning: could not write placement diff ({exc})")

    return diff_data


def _maybe_run_placement_feedback_escalation(
    final_result,
    successful_result,
    pcb_path: Path,
    args,
    quiet: bool,
    *,
    stall_label: str,
) -> None:
    """Engage placement-routing feedback at the tail of an escalation loop.

    This is the shared hook used by ``route_with_layer_escalation`` and,
    via this helper, by ``route_with_rule_relaxation`` and
    ``route_with_combined_escalation`` (issue #4151).  Before this helper
    existed, the latter two dispatch paths never referenced
    ``_run_placement_feedback`` at all, so ``--placement-feedback`` was
    parsed, forwarded, and silently dropped on the floor.

    Behaviour:

    * When ``--placement-feedback`` is not requested, this is a no-op
      (byte-identical routing preserved).
    * When requested and the escalation already fully succeeded
      (``successful_result is not None``), the loop is not needed -- we
      stay silent (this is the "not needed" state, distinct from the
      "not supported" DISABLED state).
    * When requested and there are still failed nets on a partial result,
      the feedback loop runs and ``_run_placement_feedback`` emits its
      one-line summary; ``final_result``'s completion stats are refreshed
      from the post-feedback router state so optimize/save/summary all see
      the correct numbers.
    * When requested but the router produced no routes at all (nothing to
      feed back into), emit an explicit
      ``placement-feedback: DISABLED (...)`` line so the request is never
      silently swallowed.

    Args:
        final_result: The chosen result object (mutated in place on run).
        successful_result: The fully-successful result, or None.
        pcb_path: Path to the (staged) input PCB.
        args: Parsed CLI args.
        quiet: Suppress output when True.
        stall_label: Human-readable name of the escalation strategy for
            the "Engaging" banner (e.g. "rule relaxation").
    """
    if not getattr(args, "placement_feedback", False):
        return

    # Fully-succeeded escalation: feedback is simply not needed.  Stay
    # silent so "not needed" remains textually distinguishable from the
    # "not supported / disabled" state below.
    if successful_result is not None:
        return

    router = final_result.router

    # No routes at all -> nothing to feed placement changes back into.
    # Make the (previously silent) drop explicit rather than swallowing it.
    if router.routes is None:
        if not quiet:
            print(
                "placement-feedback: DISABLED (no routes produced by "
                f"{stall_label}; nothing to feed back)"
            )
        return

    if not router.get_failed_nets():
        # All nets already routed on this partial-but-complete result; the
        # loop has nothing to improve.  Not a DISABLED case.
        return

    if not quiet:
        print(
            f"\n--- Engaging placement-routing feedback "
            f"({stall_label} stalled at {final_result.completion * 100:.0f}%) ---"
        )
    _run_placement_feedback(
        router=router,
        pcb_path=pcb_path,
        args=args,
        quiet=quiet,
    )
    # Refresh completion stats from the post-feedback router state so
    # optimize/save/summary all see the correct numbers.
    _refreshed_multi_pad_ids = {n for n, p in router.nets.items() if n > 0 and len(p) >= 2}
    _refreshed = router.get_statistics(nets_to_route_ids=_refreshed_multi_pad_ids)
    final_result.nets_routed = _refreshed["nets_routed"]
    final_result.completion = (
        final_result.nets_routed / final_result.nets_to_route
        if final_result.nets_to_route > 0
        else 1.0
    )
    final_result.success = final_result.completion >= args.min_completion
    if not quiet:
        print(
            f"  Post-feedback: {final_result.nets_routed}/"
            f"{final_result.nets_to_route} "
            f"({final_result.completion * 100:.0f}%)"
        )


def _fill_zones_after_route(output_path: Path, quiet: bool = False) -> None:
    """Fill copper-pour zones after routing completes.

    Routing produces traces; copper pour zones must be filled *after* the
    trace geometry exists so the fill polygons respect trace clearances.
    Without this step, exported Gerbers contain ``(zone ...)`` definitions
    but zero ``G36...G37`` polygon-fill regions, so manufactured boards have
    no plane copper -- the bug fixed by issue #2516.

    This function mirrors :func:`pipeline_cmd._run_step_zones`:

    - Skips silently when ``kicad-cli`` is not installed (no-op with warning).
    - Delegates to :func:`runner.run_fill_zones` so the existing
      net-corruption snapshot/restore guard (``_snapshot_net_declarations`` /
      ``_restore_net_declarations``) runs.
    - Validates net format afterwards via :func:`runner.validate_net_format`
      and logs a warning when corruption is detected.

    The function is idempotent: filling a PCB whose zones are already filled
    simply rewrites the same fill polygons.  When the input PCB has no
    ``(zone ...)`` blocks at all, kicad-cli has nothing to fill and this is
    effectively a no-op.

    Args:
        output_path: Path to the routed PCB file (modified in place).
        quiet: Suppress informational output.
    """
    from kicad_tools.cli.runner import (
        find_kicad_cli,
        run_fill_zones,
        validate_net_format,
    )

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        if not quiet:
            print("  Zone fill: skipped (kicad-cli not installed)")
        return

    # Quick check: does the PCB even have any zones to fill?
    # KiCad's serializer wraps zones either as "(zone (net ...)" on one
    # line OR as "(zone\n  (net ...)" with a newline after the opening
    # token, so we need to detect both forms.
    try:
        text = output_path.read_text()
    except OSError as exc:
        logger.warning("Zone fill: could not read %s: %s", output_path, exc)
        return
    if "(zone " not in text and "(zone\n" not in text and "(zone\t" not in text:
        # Nothing to fill -- skip silently to avoid log noise on the
        # majority of boards that have no copper pours.
        return

    if not quiet:
        print("\n--- Filling Copper Zones ---")
        print(f"  Filling zones in {output_path.name}...")

    result = run_fill_zones(output_path, kicad_cli=kicad_cli)

    if not result.success:
        # Non-fatal: log warning and continue.  The board may still be
        # usable for partial inspection but the manufacturing artifacts
        # will lack plane copper.
        logger.warning(
            "Zone fill failed for %s: %s",
            output_path,
            result.stderr or "(no stderr)",
        )
        if not quiet:
            print("  Warning: zone fill failed -- Gerbers will lack plane copper")
        return

    # Validate net format after zone fill -- kicad-cli may corrupt nets.
    report = validate_net_format(output_path)
    if not report.valid:
        logger.warning(
            "Net format corruption detected after zone fill: "
            "%d element(s) have non-canonical net format "
            "(name_only_segments=%d, name_only_vias=%d, name_only_pads=%d, "
            "empty_net_segments=%d, empty_net_vias=%d, empty_net_pads=%d)",
            report.total_corrupt,
            report.name_only_segments,
            report.name_only_vias,
            report.name_only_pads,
            report.empty_net_segments,
            report.empty_net_vias,
            report.empty_net_pads,
        )

    if not quiet:
        print("  Zone fill: complete")


@dataclass
class LayerEscalationResult:
    """Result of a layer escalation routing attempt."""

    layer_count: int
    layer_stack: "LayerStack"
    router: "Autorouter"
    net_map: dict
    nets_routed: int
    nets_to_route: int
    completion: float
    success: bool
    stats: dict | None = None
    overflow: int = 0


@dataclass
class RuleRelaxationResult:
    """Result of a rule relaxation routing attempt."""

    tier: int
    trace_width: float
    clearance: float
    via_drill: float
    via_diameter: float
    tier_description: str
    router: "Autorouter"
    net_map: dict
    nets_routed: int
    nets_to_route: int
    completion: float
    success: bool
    layer_count: int = 2  # May be set by layer escalation integration
    stats: dict | None = None


def _is_better_result(
    candidate: "LayerEscalationResult | RuleRelaxationResult",
    best: "LayerEscalationResult | RuleRelaxationResult",
) -> bool:
    """Compare routing results with tiebreaking on connectivity metrics.

    Issue #2396: The primary comparison uses **absolute nets_routed** rather
    than completion ratio.  When ``nets_to_route`` differs across escalation
    attempts (e.g. power nets auto-skipped on 4L but not 2L), comparing
    ratios produces misleading results: 6/10 (0.60) vs 3/8 (0.375) looks
    like a clear win for 2L, but the raw ratio comparison used to use
    ``completion`` which could disagree when denominators differ.  Using
    absolute counts ensures we always keep the attempt that routed the most
    nets, breaking ties by completion ratio, then segments, vias, and layer
    count.
    """
    # Primary: absolute nets routed (cross-denominator safe)
    if candidate.nets_routed != best.nets_routed:
        return candidate.nets_routed > best.nets_routed

    # Tied on absolute count: use completion ratio as tiebreaker
    if candidate.completion != best.completion:
        return candidate.completion > best.completion

    # Tie on completion -- use stats-based tiebreakers (Issue #2397)
    c_stats = candidate.stats or {}
    b_stats = best.stats or {}

    c_segments = c_stats.get("segments", 0)
    b_segments = b_stats.get("segments", 0)
    if c_segments != b_segments:
        return c_segments > b_segments

    c_vias = c_stats.get("vias", 0)
    b_vias = b_stats.get("vias", 0)
    if c_vias != b_vias:
        return c_vias > b_vias

    # Still tied: prefer fewer layers (simpler board)
    return candidate.layer_count < best.layer_count


def update_pcb_layer_stackup(pcb_content: str, target_layers: int) -> str:
    """Update PCB content to have the specified number of copper layers.

    Args:
        pcb_content: Original PCB file content
        target_layers: Target number of copper layers (2, 4, or 6)

    Returns:
        Updated PCB content with correct layer definitions
    """
    import re

    # Layer definitions for different stackups
    layer_defs = {
        2: [
            '(0 "F.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
        4: [
            '(0 "F.Cu" signal)',
            '(1 "In1.Cu" signal)',
            '(2 "In2.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
        6: [
            '(0 "F.Cu" signal)',
            '(1 "In1.Cu" signal)',
            '(2 "In2.Cu" signal)',
            '(3 "In3.Cu" signal)',
            '(4 "In4.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
    }

    if target_layers not in layer_defs:
        return pcb_content

    # Check if we need to update — count ALL copper layers regardless of type
    # (signal, power, mixed, etc.) not just those marked "signal"
    current_layers = len(re.findall(r'\(\d+\s+"[^"]*\.Cu"\s+\w+', pcb_content))
    if current_layers >= target_layers:
        return pcb_content

    # Match the entire (layers ...) block including all inner entries.
    # Each inner entry is e.g. (0 "F.Cu" signal) or (44 "Edge.Cuts" user "Edge.Cuts").
    # The pattern matches from "(layers" through each "(...)" entry to the
    # block-closing ")".
    layers_pattern = re.compile(
        r'\(layers\s*\n(\s+\(\d+\s+"[^"]+"\s+\w+[^)]*\)\s*\n)+\s*\)',
        re.MULTILINE,
    )

    # Non-copper layer entry pattern (e.g. B.SilkS, Edge.Cuts, F.Fab)
    non_copper_re = re.compile(
        r'(\s*\(\d+\s+"(?!.*\.Cu")[^"]+"\s+\w+[^)]*\))',
    )

    def replace_layers(match):
        matched_text = match.group(0)
        # Extract non-copper layer entries from the original block
        non_copper_entries = non_copper_re.findall(matched_text)
        # Build new layers content with copper layers
        new_layers = "\n    ".join(layer_defs[target_layers])
        # Append non-copper layers after copper layers
        if non_copper_entries:
            non_copper_lines = "\n".join(entry.strip() for entry in non_copper_entries)
            return f"(layers\n    {new_layers}\n    {non_copper_lines}\n  )"
        return f"(layers\n    {new_layers}\n  )"

    new_content = layers_pattern.sub(replace_layers, pcb_content)

    # Validate output has balanced parentheses to catch regressions early
    if not _validate_sexp_parentheses(new_content):
        import warnings

        warnings.warn(
            "update_pcb_layer_stackup produced unbalanced parentheses; "
            "returning original content unchanged",
            stacklevel=2,
        )
        return pcb_content

    return new_content


def _print_power_stall_suggestions(
    stalled_nets: list[str],
    layer_count: int,
    pcb_arg: str,
) -> None:
    """Print actionable suggestions for a power-net stall (Issue #2388).

    Surfaces concrete remediation flags naming the stalled nets so users
    can pick the appropriate workaround instead of being told the router
    timed out.

    Args:
        stalled_nets: Names of the power/pour nets that stalled.
        layer_count: Layer count used for the failing attempt.
        pcb_arg: The original PCB argument the user passed (for echoing
            in suggested commands).
    """
    if not stalled_nets:
        return
    nets_csv = ", ".join(stalled_nets)
    # Default zone-layer assignment: GND on B.Cu, others on F.Cu.
    pour_assignments = []
    for n in stalled_nets:
        if n.upper() in {"GND", "VSS", "AGND", "DGND", "PGND", "GROUND"}:
            pour_assignments.append(f"{n}:B.Cu")
        else:
            pour_assignments.append(f"{n}:F.Cu")
    pour_arg = ",".join(pour_assignments)

    print()
    print(f"Routing did not complete: {nets_csv} could not be routed on {layer_count} layer(s).")
    print("Suggestions:")
    print(f'  1. Add copper zones for power nets:  --power-nets "{pour_arg}"')
    print("  2. Increase layer count:              --layers 4 (or --max-layers 6)")
    print(f"  3. Manual routing in KiCad for the {len(stalled_nets)} remaining net(s)")
    print()


def _stage_input_for_auto_pour(pcb_path: Path, output_path: Path) -> Path:
    """Stage the input PCB so ``auto_pour_if_missing`` does not mutate the user's input.

    ``auto_pour_if_missing`` writes zone definitions in-place to its target
    file.  When ``kct route INPUT -o OUTPUT`` is invoked with distinct paths
    the user expects ``INPUT`` to be left untouched; this helper copies
    ``pcb_path`` to ``output_path`` and returns ``output_path`` so the rest
    of the route flow can operate on the copy.

    When ``pcb_path`` and ``output_path`` resolve to the same file (the
    pipeline ``kct build`` case where input == output), no copy is needed
    and ``pcb_path`` is returned unchanged — preserving the existing
    in-place behavior the pipeline depends on.

    Args:
        pcb_path: User-supplied input PCB path.
        output_path: User-supplied output PCB path.

    Returns:
        Path that subsequent route steps should use as their working PCB.
        Either ``pcb_path`` (input == output) or ``output_path`` (after copy).

    See: issue #2548 — ``kct route`` should not modify INPUT when output
    differs.  Mirrors the ``shutil.copy2`` pattern in ``runner.py``.
    """
    if pcb_path.resolve() == output_path.resolve():
        return pcb_path
    shutil.copy2(pcb_path, output_path)
    return output_path


def _cleanup_stale_layer_artifacts(output_path: Path, quiet: bool = False) -> list[Path]:
    """Remove any stale ``<stem>_<N>layer.kicad_pcb`` siblings of *output_path*.

    The auto-layers escalation paths rename the output to include the layer
    count when escalation actually happens (e.g. ``board_routed_4layer.kicad_pcb``).
    A previous failed-2L run leaves that ``_4layer`` file behind; the next
    run that succeeds on 2 layers does NOT touch it.  The result is a
    confusing pair of files where only the canonical one reflects the
    current run (issue #2674).

    This helper deletes the stale siblings before routing begins so a
    clean run yields a clean output directory, deterministically reflecting
    the current run's result regardless of any prior failed attempts.

    Args:
        output_path: The canonical (un-suffixed) output PCB path that
            the route command was asked to write.  Stale ``_4layer`` and
            ``_6layer`` siblings of this path's stem are removed.
        quiet: Suppress informational output.

    Returns:
        List of paths that were actually removed.  Useful for tests
        and verbose logging.
    """
    removed: list[Path] = []
    # Accept str or Path -- the escalation entry points are sometimes
    # passed string paths in tests.
    output_path = Path(output_path)
    parent = output_path.parent
    stem = output_path.stem
    # Layer counts that the escalation path can produce (matches the
    # ``layer_configs`` table in ``route_with_layer_escalation`` and
    # ``route_with_combined_escalation``).
    for n in (4, 6):
        for suffix in (".kicad_pcb", ".kicad_prl"):
            stale = parent / f"{stem}_{n}layer{suffix}"
            if stale.exists():
                try:
                    stale.unlink()
                    removed.append(stale)
                except OSError:
                    # Best-effort cleanup; a permission error on a stale
                    # artifact must not abort the route.
                    continue
    if removed and not quiet:
        from kicad_tools.cli.progress import flush_print

        for p in removed:
            flush_print(f"  Removed stale artifact: {p.name}")
    return removed


def _detect_pcb_layer_profile(pcb_path: Path) -> tuple[int, bool]:
    """Inspect the PCB's declared ``(layers ...)`` block and inner-layer zones.

    Returns the number of copper layers and whether any *inner* copper layer
    has a zone definition (the canonical signature of a board that was
    designed against power/ground planes on ``In1.Cu``/``In2.Cu``).  These two
    pieces of information are what the layer-escalation loops need to refuse
    structurally-invalid 2L probes on a 4L board (issue #2916) and to put the
    plane-aware 4L variant ahead of the all-signal variant when planes exist.

    The parsing matches :func:`kicad_tools.router.io.detect_layer_stack` -- we
    reuse the same regexes so the two paths agree on what counts as a copper
    layer and what counts as a plane zone.

    Args:
        pcb_path: Path to the input ``.kicad_pcb`` file.

    Returns:
        ``(num_copper_layers, has_inner_plane_zones)``.  Returns
        ``(2, False)`` on any parse failure so the escalation loops fall
        through to legacy behaviour (start at 2L) rather than crashing.
    """
    import re

    # Accept str or Path (a few callers pass string paths, mirroring
    # ``_cleanup_stale_layer_artifacts``).
    pcb_path = Path(pcb_path)
    try:
        pcb_text = pcb_path.read_text()
    except OSError as exc:
        logger.debug("Could not read %s for layer detection: %s", pcb_path, exc)
        return (2, False)

    # Parse the (layers ...) section to find copper layers -- mirrors the
    # regex used by ``detect_layer_stack`` so both paths agree.
    copper_names: list[str] = []
    layers_match = re.search(r"\(layers\s+(.*?)\n\s*\)", pcb_text, re.DOTALL)
    if layers_match:
        for layer_match in re.finditer(r'\((\d+)\s+"([^"]+\.Cu)"\s+(\w+)', layers_match.group(1)):
            copper_names.append(layer_match.group(2))

    num_copper = len(copper_names) if copper_names else 2

    # Detect zones on inner layers (anything that is not F.Cu or B.Cu).
    has_inner_planes = False
    for zone_match in re.finditer(
        r'\(zone\s+.*?\(layer\s+"([^"]+)"\)',
        pcb_text,
        re.DOTALL,
    ):
        layer_name = zone_match.group(1)
        if layer_name.endswith(".Cu") and layer_name not in ("F.Cu", "B.Cu"):
            has_inner_planes = True
            break

    return (num_copper, has_inner_planes)


def _filter_layer_configs_for_pcb(
    layer_configs: list[tuple[int, "LayerStack"]],
    pcb_path: Path,
    max_layers: int,
    quiet: bool = False,
    starting_layers: int = 2,
) -> list[tuple[int, "LayerStack"]]:
    """Filter and reorder *layer_configs* to honour the PCB's declared stackup.

    Issue #2916: ``--auto-layers`` previously started at 2L for every board
    regardless of the PCB's declared copper count.  On a 4L board (e.g.
    chorus-test-revA with ``In1.Cu``/``In2.Cu`` plane zones already drawn) the
    2L probe burns a fair share of the wall-clock budget on a configuration
    that cannot succeed, leaving the real 4L attempt to start against an
    exhausted deadline (issue #2823 + #2802).

    This helper applies four transformations:

    1. **Floor by detected copper count.**  Entries whose ``layer_count`` is
       below the PCB's declared count are dropped.  A 4-copper-layer PCB has
       4L as its minimum sensible probe; 2L is never tried.
    2. **Promote plane-aware variants.**  When inner-layer zones are present
       in the PCB (the ``four_layer_sig_gnd_pwr_sig`` shape), reorder so the
       plane-aware 4L variant runs before ``four_layer_all_signal``.  This
       matches what the single-pass path already does via
       :func:`detect_layer_stack`.
    3. **Honour ``--max-layers``.**  If the user-requested cap is below the
       detected count, emit a warning and keep the cap (the user's explicit
       wish wins, but they are told that it is structurally below the
       declared stackup so a partial/failed result is expected).
    4. **Honour ``starting_layers`` (Issue #3400).**  Drop entries below
       ``starting_layers`` so a board can opt out of the 2L tax.  When
       ``starting_layers=4`` the ladder becomes ``[4, 6]`` (skipping 2L
       even on a board whose declared stackup is 2-copper).

    Args:
        layer_configs: The unfiltered escalation ladder
            ``[(n_layers, LayerStack), ...]``.
        pcb_path: Path to the input ``.kicad_pcb`` file (used to probe the
            declared copper count and inner-zone presence).
        max_layers: User-requested ``--max-layers`` cap.
        quiet: Suppress informational output.
        starting_layers: Lower rung of the escalation ladder (Issue #3400).
            Default 2 preserves historical behaviour.

    Returns:
        A new list with entries below ``detected_count`` and below
        ``starting_layers`` removed, plane-aware variants promoted (when
        applicable), and capped at ``max_layers``.  Falls through to the
        input list unchanged when detection fails or the detected count is
        <= 2 (the natural starting point for the ladder).
    """
    from kicad_tools.cli.progress import flush_print

    detected_count, has_inner_planes = _detect_pcb_layer_profile(pcb_path)

    # Honour ``--max-layers`` even when it falls below the declared count --
    # but warn loudly so the user knows their cap is structurally too low.
    effective_floor = detected_count
    if max_layers < detected_count:
        if not quiet:
            flush_print(
                f"  Warning: --max-layers={max_layers} is below the PCB's "
                f"declared copper count ({detected_count}).  Using the "
                f"requested cap, but the board was designed for "
                f"{detected_count} layers so routing will likely fail or "
                "produce a partial result (issue #2916)."
            )
        # User's explicit cap wins -- relax the floor so the ladder still
        # has at least one rung.
        effective_floor = min(detected_count, max_layers)

    # Apply max_layers cap as before.
    filtered = [(n, s) for n, s in layer_configs if n <= max_layers]

    # Drop entries below the detected floor (or the user's cap if lower).
    filtered = [(n, s) for n, s in filtered if n >= effective_floor]

    # Issue #3400: honour ``starting_layers`` as an additional floor.  When
    # the user (or recipe) declares ``starting_layers=4`` we skip the 2L
    # probe entirely even if the PCB's declared stackup is 2-copper.
    if starting_layers > 2:
        prior = filtered
        filtered = [(n, s) for n, s in filtered if n >= starting_layers]
        if not quiet and len(filtered) != len(prior):
            ladder_str = ", ".join(f"{n}L" for n, _ in filtered)
            flush_print(
                f"  Issue #3400: starting_layers={starting_layers}; "
                f"ladder filtered to [{ladder_str}]"
            )

    # When inner-layer plane zones exist, promote the plane-aware 4L variant
    # ahead of the all-signal variant.  This matches the single-pass path
    # which calls ``detect_layer_stack`` and returns the plane-aware shape.
    if has_inner_planes:
        plane_aware = []
        all_signal = []
        other = []
        for n, s in filtered:
            if n == 4 and "ALL-SIG" in s.name.upper():
                all_signal.append((n, s))
            elif n == 4:
                plane_aware.append((n, s))
            else:
                other.append((n, s))
        # Preserve overall layer-count ordering: 4L (plane-aware then
        # all-signal) sandwiched between any 2L (none after floor) and 6L.
        # ``other`` already preserves the relative ordering of the original
        # list, so we splice the 4L entries back at their natural position.
        if plane_aware or all_signal:
            result = []
            inserted_four = False
            for n, s in other:
                if n > 4 and not inserted_four:
                    result.extend(plane_aware)
                    result.extend(all_signal)
                    inserted_four = True
                result.append((n, s))
            if not inserted_four:
                result.extend(plane_aware)
                result.extend(all_signal)
            filtered = result

    # Defensive: if the filter wiped the ladder (e.g. detected count > 6 and
    # max_layers < detected), fall back to the original list capped at
    # max_layers so the loop still has something to try.  The warning above
    # already alerted the user.  Issue #3400: still respect starting_layers
    # so the user's explicit floor is honoured.
    if not filtered:
        filtered = [(n, s) for n, s in layer_configs if n <= max_layers and n >= starting_layers]

    if not quiet and detected_count > 2:
        dropped = len(layer_configs) - len(filtered)
        if dropped > 0 or has_inner_planes:
            ladder_str = ", ".join(f"{n}L" for n, _ in filtered)
            flush_print(
                f"  Detected {detected_count}-copper-layer PCB"
                f"{' with inner plane zones' if has_inner_planes else ''}; "
                f"escalation ladder: [{ladder_str}] (issue #2916)"
            )

    return filtered


# Issue #3035: ``_auto_skip_pour_nets`` was promoted to
# ``kicad_tools.router.auto_pour.auto_skip_pour_nets``; the alias is bound
# near the top of this module (search ``Issue #3035``).  The 4 call sites
# in this file keep using ``_auto_skip_pour_nets`` unchanged so the
# ``@patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", ...)``
# decorators in ``tests/test_layer_escalation.py`` and
# ``tests/test_route_auto_fix.py`` continue to resolve.


def _apply_ripup_budget_override(router: "Autorouter", args) -> None:
    """Apply an explicit ``--max-ripups-per-net`` to the router (Issue #3470).

    Historically the flag only fed ``route_all_negotiated``'s
    ``--targeted-ripup`` path.  Board 05's production recipe routes via
    ``route_with_escape`` -> ``route_all_two_phase``, whose stall-recovery
    BLOCKED_BY_COMPONENT rip-up budget was hardcoded at 3, and the standard
    ``route_all`` flow's budget was hardcoded at 2 (core.py).  When the user
    passes the flag explicitly, thread it into both so the destructive
    rip-up budget is recipe-tunable on every flow.  When the flag is absent
    (None) every flow keeps its historical default.
    """
    budget = getattr(args, "max_ripups_per_net", None)
    if budget is None:
        return
    router._route_all_max_ripups_per_net = budget
    router.stall_ripup_budget = budget


def _apply_rescue_pass_override(router: "Autorouter", args) -> None:
    """Disable the post-negotiation rescue sweep when requested (Issue #4159).

    ON by default (``router._post_negotiation_rescue`` initialised True): after
    the negotiated batch loop converges/stalls/times out, each still-stranded
    net is re-attempted SOLO on the live grid, recovering long-hauls the batch
    loop starved on per-net budget.  The pass is bounded and strictly additive
    (failed attempts roll back), so it only raises the routed count.
    ``--no-rescue-pass`` sets the flag False for A/B comparison or when a caller
    wants the raw negotiated result.  A no-op when the flag is absent.
    """
    if getattr(args, "no_rescue_pass", False):
        router._post_negotiation_rescue = False


def _apply_bundle_river_planner(router: "Autorouter", args) -> None:
    """Enable the scoped bundle river planner when requested (Issue #4053).

    Off by default (mirrors ``enable_byte_lane_reorder`` / the #4051
    precedent).  When ``--bundle-river-planner`` is passed, set the
    router's ``enable_bundle_river_planner`` flag so
    ``_apply_byte_lane_inner_priority`` reserves inner-layer via-hop
    corridors for the inverted (crossing) pairs of a mirrored byte-lane
    bus reversal (board 07's DDR byte).  A no-op when the flag is absent,
    so production routing is byte-identical to pre-#4053 main.
    """
    if getattr(args, "bundle_river_planner", False):
        router.enable_bundle_river_planner = True


def _apply_monotone_certificate_order(router: "Autorouter", args) -> None:
    """Enable the monotone-certificate escape order when requested (Issue #4089).

    Off by default (mirrors ``enable_bundle_river_planner`` / the #4051
    precedent).  When ``--monotone-certificate-order`` is passed, set the
    router's ``enable_monotone_certificate_order`` flag so
    ``_apply_byte_lane_inner_priority`` runs its certificate pre-stage: the
    board-07 DDR byte is proven feasible and routes 11/11 in isolation
    (#4089).  When the certificate finds the bundle infeasible as-pinned the
    order is left at IDENTITY, so there is no regression vs. flag-off.  A
    no-op when the flag is absent, so production routing is byte-identical to
    pre-#4089 main.
    """
    if getattr(args, "monotone_certificate_order", False):
        router.enable_monotone_certificate_order = True


def _apply_cross_package_pair_corridor(router: "Autorouter", args) -> None:
    """Enable the cross-package pair corridor when requested (Issue #4090).

    Off by default (mirrors ``enable_bundle_river_planner`` / the #4051
    precedent).  When ``--cross-package-pair-corridor`` is passed, set the
    router's ``enable_cross_package_pair_corridor`` flag so the reservation
    is threaded into ``EscapeRouter`` for pairs whose members escape from
    facing packages.  A no-op when the flag is absent, so production routing
    is byte-identical to pre-#4090 main.
    """
    if getattr(args, "cross_package_pair_corridor", False):
        router.enable_cross_package_pair_corridor = True


def _apply_slack_corridor_widening(router: "Autorouter", args) -> None:
    """Enable slack-corridor widening when requested (Issue #4092).

    Off by default (mirrors ``enable_bundle_river_planner`` / the #4051
    precedent).  When ``--slack-corridor-widening`` is passed, set the
    router's ``enable_slack_corridor_widening`` flag so slack-reserved
    corridors are preferred and threaded into ``EscapeRouter`` and
    ``apply_diffpair_length_tuning``.  A no-op when the flag is absent, so
    production routing is byte-identical to pre-#4092 main.
    """
    if getattr(args, "slack_corridor_widening", False):
        router.enable_slack_corridor_widening = True


def _targeted_ripup_budget(args) -> int:
    """Resolve ``--max-ripups-per-net`` for the negotiated targeted path.

    Issue #3470 follow-up (judge note on PR #3478): the previous inline
    ``getattr(args, "max_ripups_per_net", None) or 3`` coerced an explicit
    ``--max-ripups-per-net 0`` to 3 on the negotiated targeted path while
    ``_apply_ripup_budget_override`` correctly applied 0 to the route_all /
    stall budgets.  ``None`` (flag absent) keeps the historical default of
    3; any explicit value -- including 0 -- is honored as-is.
    """
    budget = getattr(args, "max_ripups_per_net", None)
    return budget if budget is not None else 3


def _apply_net_class_map_sidecar(router: "Autorouter", args, quiet: bool = False) -> None:
    """Merge the pre-loaded --net-class-map sidecar into the router (Issue #2996).

    ``main()`` validates and deserializes the sidecar early (so error
    paths short-circuit before any routing work runs) and stashes the
    resolved ``{net_name: NetClassRouting}`` map on
    ``args._loaded_net_class_map``.  Each post-load callsite (the
    standalone path in ``main()`` plus the three ``route_with_*``
    wrappers) calls this helper to merge the rich per-pair / per-group
    fields onto the router's name-pattern-classified map.

    Issue #4149: board nets carry KiCad's hierarchical sheet prefix
    (``/FUSED_LINE``) for label-derived nets while power-symbol nets stay
    bare (``GND``).  Sidecar keys are almost always written bare, so a raw
    ``.update()`` silently keyed the overrides by names that
    ``self.net_class_map.get(net_name)`` never looks up.  We now resolve
    each bare key against the board's actual net names (matching on the
    suffix after the last ``/``), rekey the overrides to the board net
    name, and emit an aggregate stderr diagnostic for keys that matched no
    board net or matched ambiguously.  Ambiguous keys are applied to
    *neither* candidate — silently picking one would just relocate the bug.

    Idempotent and a no-op when the flag was not supplied.
    """
    loaded = getattr(args, "_loaded_net_class_map", None)
    if not loaded:
        return

    from kicad_tools.router.net_names import (
        nearest_net_names,
        resolve_net_class_map_keys,
    )

    board_net_names = list(router.net_names.values())
    resolution = resolve_net_class_map_keys(loaded.keys(), board_net_names)

    # Apply overrides rekeyed to the board's actual net names so
    # ``self.net_class_map.get(net_name)`` finds them at routing time.
    for board_net, user_key in resolution.resolved.items():
        router.net_class_map[board_net] = loaded[user_key]

    _warn_unresolved_net_class_map(resolution, board_net_names, nearest_net_names)

    if not quiet:
        from kicad_tools.cli.progress import flush_print

        flush_print(
            f"  Net-class map: merged {len(resolution.resolved)}/{resolution.total} sidecar entries"
        )


def _warn_unresolved_net_class_map(resolution, board_net_names, nearest_fn) -> None:
    """Emit the Issue #4149 misconfiguration diagnostic to stderr.

    Prints an aggregate summary line plus per-key hints when any
    ``--net-class-map`` key failed to resolve to a board net (zero-match)
    or matched more than one distinct board net (ambiguous).  A no-op when
    every key resolved.

    This is always printed (never suppressed by ``--quiet``): the
    softstart-rev-B incident showed a fully-inert class map runs to exit 0
    with no signal, so the misconfiguration warning must survive
    ``--quiet``.  Exit code is unchanged (advisory, not a hard gate).
    """
    if not resolution.unmatched and not resolution.ambiguous:
        return

    import sys

    matched = len(resolution.resolved)
    total = resolution.total
    lines: list[str] = [
        f"WARNING: net-class-map: {matched}/{total} entries matched a board net "
        f"after normalization."
    ]

    max_hints = 12
    shown = 0
    for key in resolution.unmatched:
        if shown >= max_hints:
            break
        hints = nearest_fn(key, board_net_names)
        if hints:
            lines.append(f"    {key!r} -> nearest board net(s): {', '.join(hints)}")
        else:
            lines.append(f"    {key!r} -> no similar board net found")
        shown += 1
    for key, candidates in resolution.ambiguous.items():
        if shown >= max_hints:
            break
        lines.append(
            f"    {key!r} -> AMBIGUOUS, matches {len(candidates)} board nets "
            f"({', '.join(candidates)}); skipped, use the fully-qualified name"
        )
        shown += 1

    remaining = (len(resolution.unmatched) + len(resolution.ambiguous)) - shown
    if remaining > 0:
        lines.append(f"    ... +{remaining} more")

    print("\n".join(lines), file=sys.stderr)


def _resolve_analog_net_names(router: "Autorouter", args) -> set[str]:
    """Resolve the set of analog net names selected by the analog flags (#3171).

    The union of:

    * ``--analog-nets "NET1,NET2,..."`` -- explicit, comma-separated, with
      surrounding whitespace stripped and empty entries dropped (mirrors the
      ``--skip-nets`` comma-split pattern).
    * ``--auto-analog`` -- auto-detected via Phase 2's
      :func:`detect_analog_nets`, run against the router's loaded net names.

    Returns an empty set when neither flag is supplied (the feature is a
    strict no-op when absent).  Never raises: auto-detection failures are
    swallowed so a detection edge case never blocks routing.
    """
    names: set[str] = set()

    explicit = getattr(args, "analog_nets", None)
    if explicit:
        names |= {n.strip() for n in explicit.split(",") if n.strip()}

    if getattr(args, "auto_analog", False):
        try:
            from kicad_tools.analysis.analog_detect import detect_analog_nets

            # ``detect_analog_nets`` consumes a PCB exposing ``.nets`` as a
            # ``{number: net-with-.name}`` mapping.  The router already holds
            # the loaded ``net_names`` (``{net_id: name}``); wrap it in a tiny
            # adapter so we reuse the Phase 2 classifier verbatim rather than
            # duplicating its naming rules here.
            net_names = getattr(router, "net_names", {}) or {}
            adapter = _AnalogDetectAdapter(net_names)
            names |= {a.name for a in detect_analog_nets(adapter)}
        except Exception:
            # Auto-detection is advisory; never let it block routing.
            pass

    return names


class _AnalogDetectAdapter:
    """Minimal PCB-shaped adapter exposing ``.nets`` for ``detect_analog_nets``.

    ``detect_analog_nets`` only touches ``pcb.nets`` as a mapping of
    ``{number: net}`` where each ``net`` has a ``.name`` attribute.  This
    adapter projects the router's ``{net_id: name}`` mapping into that shape
    so the Phase 2 (#3170) classifier can be reused without re-loading the
    PCB at the per-attempt routing callsites.
    """

    class _Net:
        __slots__ = ("name",)

        def __init__(self, name: str) -> None:
            self.name = name

    def __init__(self, net_names: dict[int, str]) -> None:
        self.nets = {num: self._Net(name) for num, name in net_names.items()}


def _apply_analog_net_class(router: "Autorouter", args, quiet: bool = False) -> None:
    """Inject the boosted analog routing class for selected nets (Issue #3171).

    Phase 3 of analog-aware routing.  For each net selected by ``--analog-nets``
    and/or ``--auto-analog`` (resolved via :func:`_resolve_analog_net_names`)
    that is present on the board, set ``router.net_class_map[net]`` to
    :data:`NET_CLASS_ANALOG` -- a priority- and cost-boosted class
    (``priority=2``, ``cost_multiplier=0.85``) so the net routes earlier and
    shorter.  No A*/pathfinder changes: the existing per-net priority
    (``_get_net_priority``) and per-net ``cost_multiplier`` consumers do the
    work.

    Pour/ground guard (AC #5): a selected net whose *existing* class is a pour
    net (``is_pour_net=True`` or ``route_via="pour"`` -- e.g. ``GNDA``) is
    LEFT UNTOUCHED.  Those nets are satisfied by a copper zone, not the
    pathfinder; forcing the analog (pathfinder) class onto them would drag a
    pour/ground net into the router as an ordinary trace.

    Idempotent and a strict no-op when neither flag is supplied.  Called at
    every post-load callsite alongside :func:`_apply_net_class_map_sidecar`.
    """
    selected = _resolve_analog_net_names(router, args)
    if not selected:
        return

    from kicad_tools.router.rules import NET_CLASS_ANALOG

    # Only act on nets actually present on the board (AC: missing names are
    # silently ignored).  ``router.net_names`` is {net_id: name}.
    present = {name for name in router.net_names.values() if name}

    applied: list[str] = []
    skipped_pour: list[str] = []
    for name in sorted(selected & present):
        existing = router.net_class_map.get(name)
        # Pour/ground guard: never force a poured net into the pathfinder.
        if existing is not None and (
            getattr(existing, "is_pour_net", False) or getattr(existing, "route_via", "") == "pour"
        ):
            skipped_pour.append(name)
            continue
        router.net_class_map[name] = NET_CLASS_ANALOG
        applied.append(name)

    if not quiet and (applied or skipped_pour):
        from kicad_tools.cli.progress import flush_print

        if applied:
            flush_print(
                f"  Analog routing: boosted {len(applied)} net(s) "
                f"(priority={NET_CLASS_ANALOG.priority}, "
                f"cost_multiplier={NET_CLASS_ANALOG.cost_multiplier}): "
                f"{', '.join(applied)}"
            )
        if skipped_pour:
            flush_print(
                f"  Analog routing: left {len(skipped_pour)} pour/ground net(s) "
                f"as-is (not forced into pathfinder): {', '.join(skipped_pour)}"
            )


def _apply_order_method(
    router: "Autorouter",
    args,
    router_factory: "Callable[[], Autorouter] | None" = None,
    quiet: bool = False,
) -> None:
    """Compute an explicit net order via ``--order-method`` (Issue #3897).

    Wires the previously-orphaned
    :meth:`kicad_tools.optim.routing.RoutingOptimizer.optimize_net_order`
    into ``kct route``.  When ``args.order_method`` is one of ``greedy``,
    ``critical_first``, ``congestion`` or ``hybrid``, this computes the net
    routing order with that heuristic and stashes it on
    ``router._forced_net_order``.  Both :meth:`Autorouter.route_all` and
    :meth:`Autorouter.route_all_negotiated` consult that attribute to seed
    their base ordering, overriding the internal ``_get_net_priority`` sort.

    Strict no-op when ``--order-method`` is not supplied, so routing output is
    byte-identical to the historical behaviour (the ``_forced_net_order``
    attribute stays ``None``).

    The ``congestion`` and ``hybrid`` methods require a congestion map.  We
    obtain one from the already-loaded ``router`` via
    :meth:`Autorouter.get_congestion_map`; on failure we emit a warning and
    fall back to ``greedy`` rather than crashing.

    Args:
        router: The loaded router whose ``_forced_net_order`` will be set.
        args: Parsed CLI namespace (reads ``args.order_method``).
        router_factory: Callable returning a fresh, fully-loaded router used by
            the optimizer to evaluate the candidate order.  When ``None``, a
            factory returning ``router`` itself is used (the optimizer's final
            evaluation route then runs on ``router``; callers that must keep
            ``router`` pristine should pass a fresh-build factory).
        quiet: Suppress the informational log line when True.
    """
    method = getattr(args, "order_method", None)
    if method is None:
        return

    from kicad_tools.cli.progress import flush_print
    from kicad_tools.optim.routing import RoutingOptimizer

    congestion_map = None
    effective_method = method
    if method in ("congestion", "hybrid"):
        try:
            congestion_map = router.get_congestion_map()
        except Exception as exc:  # noqa: BLE001 - fall back rather than crash
            if not quiet:
                flush_print(
                    f"  Warning: --order-method {method} could not obtain a "
                    f"congestion map ({exc}); falling back to greedy ordering."
                )
            effective_method = "greedy"
            congestion_map = None

    if router_factory is None:

        def router_factory() -> "Autorouter":
            return router

    optimizer = RoutingOptimizer()
    order, _fom = optimizer.optimize_net_order(
        router_factory,
        method=effective_method,
        congestion_map=congestion_map,
    )
    router._forced_net_order = order

    if not quiet:
        flush_print(
            f"  Net order: --order-method {method} "
            f"(overrides default priority sort; {len(order)} nets)"
            + (f" [fell back to {effective_method}]" if effective_method != method else "")
        )


def _log_fine_pitch_escape_regions(
    router: "Autorouter",
    quiet: bool = False,
) -> int:
    """Log the fine-pitch escape regions installed by ``load_pcb_for_routing``.

    Issue #3371 (P_FP3) -- the detector + region install runs inside
    :func:`kicad_tools.router.io.load_pcb_for_routing` (so the
    Python-side pad halos pick up the in-region clearance at
    ``add_pad`` time).  This helper just surfaces the result on the
    routing console.

    Detection is **unconditional** when the recipe-relative trigger
    fires (Q_FP1) so every recipe with a fine-pitch SOIC/QFN/etc.
    automatically picks up the manufacturer-aware escape clearance.
    No CLI flag is required (per P_FP3 deliverable #1 -- this is the
    new default behaviour).  When the detector finds no qualifying
    package the helper is a strict no-op.

    Manufacturer fallback warning (P_FP2 builder decision #6): when
    no manufacturer is configured (neither via ``--manufacturer`` nor
    via ``rules.manufacturer``), the region's escape clearance falls
    back to ``rules.trace_clearance`` (no shrink).  We surface a
    warning in that case so users notice the detector ran but did
    not effectively shrink the corridor.

    Args:
        router: Autorouter returned by ``load_pcb_for_routing``;
            ``router.grid.get_fine_pitch_regions()`` is consulted for
            the installed regions.
        quiet: When True, suppress all output.

    Returns:
        The number of installed regions (``0`` when nothing
        qualifies).  Provided for tests; callers do not need to use
        the return value.
    """
    if quiet:
        return len(router.grid.get_fine_pitch_regions())

    from kicad_tools.cli.progress import flush_print

    regions = router.grid.get_fine_pitch_regions()
    if not regions:
        return 0

    refs = ", ".join(r.package_ref for r in regions)
    escape_clearance = regions[0].escape_clearance
    flush_print(
        f"  Fine-pitch escape regions detected: {len(regions)} "
        f"({refs}); escape clearance {escape_clearance:.3f}mm"
    )

    # Manufacturer fallback warning (P_FP2 decision #6).  The detector
    # marks a region as a NO-OP by leaving its ``escape_clearance``
    # equal to ``rules.trace_clearance``; we surface this so users do
    # not silently miss the fact that the detector ran without
    # shrinking.
    if regions[0].escape_clearance >= router.rules.trace_clearance:
        flush_print(
            "  WARNING: no manufacturer configured (rules.manufacturer "
            "unset); fine-pitch escape regions detected but escape "
            "clearance defaulted to rules.trace_clearance (no shrink). "
            "Set --manufacturer to enable the tier-aware escape clearance."
        )

    return len(regions)


def _mfr_supports_via_in_pad(manufacturer: str | None) -> bool:
    """Return True when ``manufacturer`` supports via-in-pad processing.

    Issue #3371 / P_FP5 -- the auto-layers fallback composition is gated
    on manufacturer capability so we never silently produce a routed PCB
    with via-in-pad geometry on a tier (e.g. plain ``jlcpcb``) that does
    not offer the process.

    Args:
        manufacturer: Manufacturer key (e.g. ``"jlcpcb-tier1"``) or
            ``None`` when no manufacturer is configured.

    Returns:
        ``True`` when ``get_mfr_limits(manufacturer).via_in_pad_supported``
        is True; ``False`` otherwise (including unknown manufacturer or
        ``None`` input).
    """
    if not manufacturer:
        return False
    try:
        from kicad_tools.router.mfr_limits import get_mfr_limits

        limits = get_mfr_limits(manufacturer)
    except (ValueError, ImportError):
        return False
    return bool(getattr(limits, "via_in_pad_supported", False))


def _interleave_fine_pitch_fallback_attempts(
    layer_configs: list,
    enabled: bool,
):
    """Interleave a via-in-pad fallback retry between consecutive layer attempts.

    Issue #3371 / P_FP5 -- the fine-pitch escape ladder needs a cheap rung
    between consecutive layer-count escalations.  When the configured
    manufacturer supports via-in-pad AND the user did not pin the flag on
    explicitly, this helper inserts a fallback retry attempt after each
    baseline layer attempt.  The result is a 3-tuple
    ``(layer_count, layer_stack, via_in_pad_fallback: bool)`` per entry;
    callers consult the third element to stamp the
    ``KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK`` env var around the per-
    attempt routing call.

    When ``enabled=False``, the input tuples are returned with
    ``via_in_pad_fallback=False`` appended -- preserving pre-P_FP5
    behaviour exactly (no new attempts).  The CLI default is
    ``enabled=True`` when ``manufacturer`` supports via-in-pad and the
    user did not pass ``--micro-via-in-pad-fallback`` (which would imply
    "stay on for every attempt"; treating that case as "compose" would
    not change observable behaviour but would double the attempt count
    needlessly).

    Args:
        layer_configs: Pre-P_FP5 list of ``(layer_count, layer_stack)``
            tuples (the output of ``_filter_layer_configs_for_pcb``).
        enabled: When ``True``, insert a fallback retry after each entry.

    Returns:
        A new list of ``(layer_count, layer_stack, via_in_pad_fallback)``
        triples.  Length is ``len(layer_configs)`` when ``enabled=False``
        and ``2 * len(layer_configs)`` otherwise.
    """
    if not enabled:
        return [(lc, ls, False) for lc, ls in layer_configs]
    interleaved: list = []
    for lc, ls in layer_configs:
        interleaved.append((lc, ls, False))
        interleaved.append((lc, ls, True))
    return interleaved


def _rung_dedup_fingerprint(
    layer_count: int,
    layer_stack_id: str,
    via_in_pad_fallback: bool,
    skip_nets: "list[str] | tuple[str, ...]",
) -> tuple[int, str, bool, tuple[str, ...]]:
    """Compute the pre-rung deduplication fingerprint for issue #3923.

    A layer-escalation rung's result is fully determined by its
    ``(layer_count, layer_stack, via_in_pad_fallback, skip_nets)`` inputs: the
    per-attempt ``Autorouter`` is constructed fresh from ``pcb_path`` + these
    inputs inside ``load_pcb_for_routing`` (no routed copper is carried across
    rungs), so two rungs sharing this fingerprint provably produce the same
    nets-routed / overflow result.

    ``layer_stack_id`` is the stack's identity (its ``name``) -- CRUCIAL so that
    genuinely-different stacks at the SAME layer count are NOT collapsed.  The
    default ladder contains two distinct 4-layer stacks (``4-Layer
    SIG-GND-PWR-SIG`` vs ``4-Layer ALL-SIG``) that must both run; only a rung
    that repeats the *same* stack with the *same* fallback + skip_nets is a true
    duplicate (e.g. the fine-pitch via-in-pad interleave or a
    ``_filter_layer_configs_for_pcb`` reordering that re-emits an identical
    entry -- the ``[4L, 4L]`` pattern the sweep observed on board-05).

    ``skip_nets`` is normalized to a *sorted* tuple so set-iteration order
    cannot defeat the dedup, and ``via_in_pad_fallback`` is coerced to ``bool``
    so a truthy non-bool cannot slip a duplicate through.

    The existing stagnation / zero-overflow early-stops only fire AFTER a rung
    has already spent its full routing budget, so they cannot prevent the
    re-run; this fingerprint skips it before any wall time is spent.
    """
    return (
        int(layer_count),
        str(layer_stack_id),
        bool(via_in_pad_fallback),
        tuple(sorted(skip_nets)),
    )


def route_with_layer_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with automatic layer escalation.

    Tries routing at 2, 4, and 6 layers until success or max is reached.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import flush_print, spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        ensure_cpp_backend_available,
        load_pcb_for_routing,
        show_routing_summary,
    )

    # Handle backend selection (auto-build C++ extension on first use; #2549)
    ok, force_python, exit_code = ensure_cpp_backend_available(
        backend=args.backend,
        quiet=quiet,
        allow_auto_build=not getattr(args, "no_auto_build_native", False),
    )
    if not ok:
        return exit_code if exit_code is not None else 1

    # Configure design rules
    fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
    rules = DesignRules(
        grid_resolution=args.grid,
        trace_width=args.trace_width,
        trace_clearance=args.clearance,
        via_drill=args.via_drill,
        via_diameter=args.via_diameter,
        fine_pitch_clearance=fine_pitch_cl,
        # Issue #2695: forward manufacturer so the escape router can opt in
        # to in-pad escape for fine-pitch LQFP/QFP (and SSOP/TSSOP) when the
        # manufacturer supports via-in-pad processing.
        manufacturer=getattr(args, "manufacturer", None),
        # Issue #2891: forward escalation-in-progress flag so the escape
        # router demotes the #2880 ERROR log when an outer wrapper will
        # retry on a tier that supports via-in-pad.
        auto_mfr_tier_in_progress=getattr(args, "_auto_mfr_tier_in_progress", False),
    )

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Issue #2674: remove stale ``<stem>_<N>layer.kicad_pcb`` siblings
    # from a previous failed-2L run before routing begins.  Without this,
    # a successful 2L run leaves the prior failed 4L/6L artifact behind
    # and the output directory shows a confusing pair of routed PCBs.
    _cleanup_stale_layer_artifacts(output_path, quiet=quiet)

    # Auto-create copper pours for power nets (before skip detection).
    # auto_pour_if_missing writes in-place; stage a copy at output_path
    # first so the user's INPUT is left untouched (issue #2548).
    # Issue #3092: forward the user-supplied skip_nets as force_pour_nets
    # so an all-power board (e.g. board 01 VIN/VOUT/GND) still emits a
    # zone for any net the user explicitly committed to pouring -- without
    # this, the all-power guard suppresses every zone and a --skip-nets
    # GND target ends up with neither traces nor a zone.
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _stage_input_for_auto_pour(pcb_path, output_path)
        auto_pour_if_missing(
            pcb_path,
            quiet=quiet,
            edge_clearance=getattr(args, "edge_clearance", None),
            force_pour_nets=skip_nets,
        )

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=quiet)

    # Issue #3155: capture preserved copper ONCE from the staged input before
    # any routing or checkpoint write mutates it.  The escalation loop below
    # re-reads ``pcb_path`` (== the staged ``output_path``) on every attempt,
    # and checkpoint writes overwrite it with routed-only geometry, so the
    # per-attempt ``router.existing_routes`` cannot be trusted to retain the
    # original copper.  Capturing here keeps it stable across all attempts.
    _preserve = bool(getattr(args, "preserve_existing", False))
    _preserved_routes = _capture_preserved_routes(pcb_path) if _preserve else []
    _preserved_sexp = _serialize_preserved_routes(_preserved_routes) if _preserve else ""

    # Layer stacks to try (in escalation order)
    layer_configs = [
        (2, LayerStack.two_layer()),
        (4, LayerStack.four_layer_sig_gnd_pwr_sig()),
        (4, LayerStack.four_layer_all_signal()),
        (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
    ]

    # Issue #2916: filter and reorder by the PCB's declared stackup.
    # Drops entries below the detected copper count (so a 4L board never
    # wastes budget on a 2L probe) and promotes the plane-aware 4L variant
    # ahead of all-signal when inner plane zones exist.
    # Issue #3400: also honour ``--starting-layers`` so boards can opt
    # out of the 2L probe explicitly.
    layer_configs = _filter_layer_configs_for_pcb(
        layer_configs,
        pcb_path,
        args.max_layers,
        quiet=quiet,
        starting_layers=int(getattr(args, "starting_layers", None) or 2),
    )

    # Issue #3371 / P_FP5: compose fine-pitch escape with the auto-layers
    # ladder.  When the user did NOT explicitly enable
    # ``--micro-via-in-pad-fallback`` and the configured manufacturer
    # supports via-in-pad (e.g. jlcpcb-tier1), interleave a fallback retry
    # between consecutive layer attempts.  The ladder becomes:
    #
    #     L=2 baseline -> L=2 + via-in-pad fallback (if mfr allows) ->
    #     L=4 baseline -> L=4 + via-in-pad fallback (if mfr allows) ->
    #     L=6 baseline -> L=6 + via-in-pad fallback (if mfr allows)
    #
    # The fallback retry is *cheaper* than escalating layers (a single
    # process-env-var flip), and on fine-pitch SOIC packages (e.g.
    # softstart rev B UCC27211) it lifts pin-2 escape reach 22/30 -> 28+/30
    # without requiring 6-layer escalation.  When the user explicitly
    # passes ``--micro-via-in-pad-fallback`` we keep the original ladder
    # exactly so the flag's behaviour matches its single-attempt
    # documentation (it stays on for *every* attempt).  When the mfr
    # tier does not support via-in-pad (e.g. tier-0 jlcpcb), the
    # composition is a strict no-op.
    _user_explicit_fallback = bool(getattr(args, "micro_via_in_pad_fallback", False))
    _fp5_compose_fallback = not _user_explicit_fallback and _mfr_supports_via_in_pad(
        getattr(args, "manufacturer", None)
    )
    layer_configs = _interleave_fine_pitch_fallback_attempts(
        layer_configs, enabled=_fp5_compose_fallback
    )

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Layer Escalation Mode")
        flush_print("=" * 60)
        flush_print(f"Input:          {pcb_path}")
        flush_print(f"Output:         {output_path}")
        flush_print(f"Strategy:       {args.strategy}")
        flush_print(f"Max layers:     {args.max_layers}")
        flush_print(f"Min completion: {args.min_completion * 100:.0f}%")
        if skip_nets:
            flush_print(f"Skip:           {', '.join(skip_nets)}")
        flush_print()

    best_result: LayerEscalationResult | None = None
    successful_result: LayerEscalationResult | None = None

    # Issue #2412: Track previous attempt metrics for early termination
    prev_nets_routed: int | None = None
    prev_overflow: int | None = None

    # Issue #3241: Track monotonic regression across attempts (cross-attempt
    # decrease in nets_routed).  Unlike the #2412 stagnation heuristic above,
    # which is gated by the #2673 50% completion floor, the regression-exit
    # fires regardless of floor because "more layers + fewer routed nets" is
    # structurally backwards -- adding still more layers cannot cure it.
    # See issue #3241 (chorus-test-revA 15% -> 12% -> 10% observation).
    #
    # Thresholds (intentionally tunable as named constants for readability):
    #   - REGRESSION_TOLERANCE: small flicker (default 2 nets) does NOT count
    #     as a regression.  PR #3193 made A* tie-break deterministic, so most
    #     flicker is gone -- but routing-order changes across stacks still
    #     produce 1-2 net jitter even on equivalent topologies.
    #   - HARD_DROP_NETS: a single-attempt drop of >=5 nets is severe enough
    #     to exit immediately without waiting for a second regression.
    #   - CONSECUTIVE_REGRESSIONS: otherwise, require 2 consecutive regression
    #     observations before exiting (avoids false-positive on a single
    #     unlucky attempt).
    REGRESSION_TOLERANCE = 2
    HARD_DROP_NETS = 5
    CONSECUTIVE_REGRESSIONS = 2
    regression_streak: int = 0

    # Issue #2388: Track power-net stall across escalation attempts.  When
    # a 2-layer attempt aborts due to power-net stall, the next attempt
    # is biased toward a stack with dedicated planes for those nets, and
    # we auto-extend skip_nets with the plane nets so the router relies
    # on the plane connections instead of routing power as signals.
    last_power_stall_nets: list[str] = []

    # Issue #3051: Build the checkpoint callback ONCE before the
    # escalation loop so every per-attempt ``route_all_negotiated`` call
    # gets best-so-far persistence (closes the iteration-0 kill-loses-
    # work hole observed in the curator audit).  The primary single-
    # attempt path at the bottom of ``main()`` already wires this
    # callback; without lifting it here, kills mid-loop on the
    # layer-escalation path produce an empty output PCB.
    _checkpoint_cb = _make_checkpoint_callback(
        pcb_path,
        output_path,
        float(getattr(args, "checkpoint_interval", 30.0) or 0.0),
        quiet=quiet,
        preserved_sexp=_preserved_sexp,
    )

    # Issue #3923: pre-rung deduplication.  The escalation ladder built by
    # ``_build_layer_configs_for_escalation`` + the fine-pitch interleave can
    # contain entries with an identical ``(layer_count, via_in_pad_fallback)``
    # tuple (most visibly the ``[4L, 4L]`` pattern on board-05).  When the
    # board state feeding a rung is also identical -- same skip_nets, no
    # per-attempt state carried between them -- the rung re-executes a full
    # routing budget only to produce the same result (same nets routed, same
    # overflow).  The existing stagnation / zero-overflow early-stops only
    # fire AFTER a rung has spent its budget (and both carve out the
    # same-layer via-in-pad fallback), so they cannot prevent the re-run.
    # Fingerprint each attempted config and skip a rung whose fingerprint was
    # already attempted -- each unique ``(layer_count, via_in_pad_fallback,
    # skip_nets)`` config runs at most once per invocation.
    _attempted_rung_fingerprints: set[tuple[int, str, bool, tuple[str, ...]]] = set()

    for attempt_num, (layer_count, layer_stack, via_in_pad_fallback) in enumerate(layer_configs, 1):
        # Issue #3371 / P_FP5: stamp / clear the via-in-pad fallback env var
        # *around* this attempt so the lazily-constructed EscapeRouter
        # picks up the per-attempt opt-in.  The env var is sticky across
        # subprocess invocations (the read site is in
        # ``EscapeRouter.__init__``) so we always set it explicitly to
        # match this attempt's intent -- both ON and OFF -- which also
        # makes the test fixture happy without monkey-patching env.
        import os as _os_fp5

        if via_in_pad_fallback:
            _os_fp5.environ["KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK"] = "1"
        elif not _user_explicit_fallback:
            # Only clear when the user did NOT explicitly request the
            # fallback (which we preserve verbatim across the entire
            # ladder per the helper's docstring).
            _os_fp5.environ.pop("KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK", None)

        # Issue #2802: honor the total wall-clock deadline before starting
        # another layer-stack attempt.  Without this guard the loop would
        # blindly start a fresh ``route_all_negotiated`` call (each with
        # its own copy of ``args.timeout``) even after the user-configured
        # total budget has expired.
        if _deadline_expired(args):
            if not quiet:
                flush_print(
                    f"  Wall-clock deadline reached before attempt {attempt_num}; "
                    "stopping layer escalation (issue #2802)"
                )
            break

        # Issue #2388: When the previous attempt stalled on power nets and
        # this stack provides dedicated planes for them, auto-skip those
        # plane nets so the router doesn't try to route them as signals.
        attempt_skip_nets = list(skip_nets)
        plane_nets_in_stack = {lyr.plane_net for lyr in layer_stack.plane_layers if lyr.plane_net}
        if last_power_stall_nets and plane_nets_in_stack:
            auto_plane_skip = [
                n
                for n in last_power_stall_nets
                if n in plane_nets_in_stack and n not in attempt_skip_nets
            ]
            if auto_plane_skip:
                attempt_skip_nets.extend(auto_plane_skip)
                if not quiet:
                    flush_print(
                        f"  Auto-skipping {', '.join(auto_plane_skip)} "
                        "(connected via dedicated plane(s) in this stack)"
                    )

        # Issue #3923: skip a rung whose (layer_count, via_in_pad_fallback,
        # skip_nets) fingerprint was already attempted this invocation.  The
        # board state that feeds a rung is fully determined by these three
        # inputs (the per-attempt Autorouter is constructed fresh from
        # ``pcb_path`` + ``attempt_skip_nets`` inside ``load_pcb_for_routing``
        # below -- no routed copper is carried across rungs), so an identical
        # fingerprint provably reproduces the previous rung's result.  Running
        # it again only burns a full ``route_all_negotiated`` budget (60s+ per
        # affected board) for +0 routed nets.  ``attempt_skip_nets`` is sorted
        # so set-order jitter cannot defeat the dedup.
        _rung_fingerprint = _rung_dedup_fingerprint(
            layer_count,
            getattr(layer_stack, "name", str(layer_count)),
            via_in_pad_fallback,
            attempt_skip_nets,
        )
        if _rung_fingerprint in _attempted_rung_fingerprints:
            if not quiet:
                _fallback_suffix = " + via-in-pad fallback" if via_in_pad_fallback else ""
                flush_print(
                    f"  Skipping attempt {attempt_num}: {layer_count} layers"
                    f"{_fallback_suffix} -- identical config already attempted "
                    "(issue #3923 rung dedup)"
                )
            continue
        _attempted_rung_fingerprints.add(_rung_fingerprint)

        if not quiet:
            flush_print("=" * 60)
            # P_FP5: surface the via-in-pad fallback opt-in for this attempt
            # so the user can see why the same layer count is being retried.
            _fallback_suffix = " + via-in-pad fallback" if via_in_pad_fallback else ""
            flush_print(
                f"Attempt {attempt_num}: {layer_count} layers ({layer_stack.name})"
                f"{_fallback_suffix}"
            )
            flush_print("=" * 60)

        # Load PCB with this layer stack
        try:
            with spinner(f"Loading PCB ({layer_count} layers)...", quiet=quiet):
                router, net_map = load_pcb_for_routing(
                    str(pcb_path),
                    skip_nets=attempt_skip_nets,
                    rules=rules,
                    edge_clearance=args.edge_clearance,
                    layer_stack=layer_stack,
                    force_python=force_python,
                    # Issue #4268: thread the mesh-router strategy selector through.
                    strategy=getattr(args, "route_engine", "grid"),
                    validate_drc=not args.force,
                    strict_drc=False,
                    # Issue #3155: incremental routing.  When set, existing
                    # copper is loaded as grid obstacles + populated into
                    # router.existing_routes so it survives the route pass.
                    load_existing_routes=getattr(args, "preserve_existing", False),
                    # Issue #4148: region-bounded routing.  When set, cells
                    # outside the board-relative box are marked as obstacles.
                    region=getattr(args, "_region_box", None),
                    # Issue #4170 (Phase 2b-1): board-relative boundary stub
                    # terminals whose tip cells are carved open as same-net
                    # reconnection targets (None when no --region / no stubs).
                    stub_terminals=getattr(args, "_stub_terminals", None),
                    # Issue #3877: thread the C++ iteration backstop through the
                    # layer-escalation sub-router too.  Without this the
                    # escalation-mode Autorouter (board-01/03 et al.) routes with
                    # _max_search_iterations=0, so --deterministic-budget falls
                    # back to the wall-clock 10%-of-stage per-net cap and the
                    # route stays load-dependent.  The flag's normalization
                    # (route_cmd.py:_normalize_deterministic_budget) pins
                    # args.max_search_iterations to 12M; ``or 0`` preserves the
                    # historic 0="use cols*rows*4 heuristic" semantics.
                    max_search_iterations=getattr(args, "max_search_iterations", 0) or 0,
                    # Issue #3881: thread the tuned per-net iteration cap through
                    # the layer-escalation sub-router too, so the per-net bound
                    # applies there (defaulted by --deterministic-budget).
                    per_net_iterations=getattr(args, "per_net_iterations", 0) or 0,
                )
        except Exception as e:
            if not quiet:
                print(f"  Error loading PCB: {e}")
            continue

        # Issue #2996: merge --net-class-map sidecar onto router's map.
        _apply_net_class_map_sidecar(router, args, quiet=quiet)
        # Issue #3470: thread --max-ripups-per-net into the destructive
        # rip-up budgets (route_all + two-phase stall recovery).
        _apply_ripup_budget_override(router, args)
        _apply_rescue_pass_override(router, args)
        _apply_bundle_river_planner(router, args)
        _apply_monotone_certificate_order(router, args)
        _apply_cross_package_pair_corridor(router, args)
        _apply_slack_corridor_widening(router, args)

        # Issue #3171: inject boosted analog routing class for --analog-nets /
        # --auto-analog selected nets (pour/ground nets are left untouched).
        _apply_analog_net_class(router, args, quiet=quiet)

        # Issue #3371 (P_FP3): surface the fine-pitch escape regions that
        # ``load_pcb_for_routing`` installed (if any) and warn when the
        # detector ran without a manufacturer floor.
        _log_fine_pitch_escape_regions(router, quiet=quiet)

        # Issue #2396: Ensure pristine per-attempt state.  Today this is a
        # no-op (load_pcb_for_routing creates a fresh Autorouter) but it
        # documents the contract and prevents silent regression if future
        # refactors reuse an Autorouter across attempts.
        router.reset_attempt_state()

        # Issue #1841: Tell the autorouter which pour nets lack zones
        router._pour_nets_without_zones = set(_no_zone)

        # Count nets to route.  Issue #3942 (Bug B): exclude pour-served
        # multi-pad nets (router-stripped via _filter_pour_nets) from the
        # denominator so routed/total matches what the router was asked to
        # route.  See _routable_multi_pad_nets for the rationale.
        multi_pad_nets = _routable_multi_pad_nets(router)
        single_pad_nets = [
            net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) == 1
        ]
        nets_to_route = len(multi_pad_nets)

        if not quiet:
            flush_print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
            flush_print(f"  Nets to route: {nets_to_route}")
            _emit_single_pad_net_warning(router, single_pad_nets)

        # Route
        if not quiet:
            flush_print(f"\n  Routing ({args.strategy})...")

        escape_flag = _resolve_escape_routing_flag(args)

        # Issue #2823: divide the remaining wall-clock budget fairly across
        # the remaining layer-escalation attempts.  Without this, the first
        # attempt (typically 2L) greedily consumes the entire ``--timeout``,
        # leaving the higher-layer attempts (4L, 6L) no time to run -- so
        # the escalation strategy degenerates to "spend everything on the
        # lowest layer count, give up."  ``attempt_num`` is 1-based; the
        # helper expects a 0-based index, hence ``attempt_num - 1``.
        # Falls back to ``args.timeout`` when no deadline is configured.
        _attempt_timeout = _per_attempt_budgeted_timeout(
            args,
            attempt_index=attempt_num - 1,
            max_attempts=len(layer_configs),
        )

        try:
            if _should_use_escape_routing(router, escape_flag, quiet):
                # Issue #3952: compose the escape pre-phase with the
                # CoupledPathfinder diff-pair pre-pass when
                # --differential-pairs is requested so Phase A runs on
                # escape-forced boards; otherwise take the unchanged escape
                # path (byte-identical for no-pair boards).
                _dp_cfg = _build_diffpair_config(args)
                if _dp_cfg is not None:
                    router.route_with_escape_and_diffpairs(
                        _dp_cfg,
                        use_negotiated=(args.strategy == "negotiated"),
                        timeout=_attempt_timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    )
                else:
                    router.route_with_escape(
                        use_negotiated=(args.strategy == "negotiated"),
                        timeout=_attempt_timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    )
            elif getattr(args, "multi_resolution", False):
                router.route_all_multi_resolution(
                    use_negotiated=(args.strategy == "negotiated"),
                    max_iterations=args.iterations,
                    timeout=_attempt_timeout,
                )
            elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                router.route_all_two_phase(
                    use_negotiated=True,
                    corridor_width_factor=2.0,
                    timeout=_attempt_timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                )
            elif args.strategy == "negotiated":
                router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=_attempt_timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    batch_routing=getattr(args, "batch_routing", False)
                    or getattr(args, "high_performance", False),
                    hierarchical=getattr(args, "hierarchical", False),
                    perturbation=getattr(args, "perturbation", True),
                    # Issue #3039: forward --seed for deterministic routing.
                    seed=getattr(args, "seed", None),
                    # Issue #3054 (Phase 2 of #3045): forward region-based
                    # parallelism opt-in.  Defaults preserve single-threaded
                    # behaviour bit-for-bit.
                    region_parallel=getattr(args, "region_parallel", False),
                    partition_rows=getattr(args, "partition_rows", 2),
                    partition_cols=getattr(args, "partition_cols", 2),
                    max_parallel_workers=getattr(args, "max_parallel_workers", 4),
                    # Issue #3051: forward checkpoint callback so kills
                    # mid-loop persist the best-so-far snapshot.
                    checkpoint_callback=_checkpoint_cb,
                    # Issue #3438 / #3414: forward --targeted-ripup so the
                    # pre-existing targeted rip-up path in
                    # route_all_negotiated is CLI-reachable.
                    use_targeted_ripup=getattr(args, "targeted_ripup", False),
                    max_ripups_per_net=_targeted_ripup_budget(args),
                    # Issue #3101: best-metric early-stop patience.  0
                    # disables (matches pre-#3101 behaviour).
                    best_stall_patience=(getattr(args, "early_stop_patience", 2) or None),
                )
            elif args.strategy == "basic":
                router.route_all()
            elif args.strategy == "monte-carlo":
                router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
            elif args.strategy == "evolutionary":
                router.route_all_evolutionary(
                    pop_size=args.pop_size,
                    generations=args.generations,
                    verbose=args.verbose and not quiet,
                    timeout=_attempt_timeout,
                )
        except Exception as e:
            if not quiet:
                print(f"  Routing error: {e}")
            continue

        # Issue #2426: Run cleanup before computing statistics so that the
        # best-result selector compares post-cleanup connectivity counts —
        # the same metric shown in the final summary.  cleanup_artifacts()
        # is idempotent, so the subsequent call in _finalize_routes() is a
        # safe no-op.
        router.cleanup_artifacts()

        # Calculate completion — filter to multi-pad nets only (Issue #1643)
        multi_pad_net_ids = set(multi_pad_nets)
        stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)
        nets_routed = stats["nets_routed"]
        completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0

        # Issue #2412: Capture overflow for early termination detection
        overflow = int(router.grid.get_total_overflow())

        # Create result
        result = LayerEscalationResult(
            layer_count=layer_count,
            layer_stack=layer_stack,
            router=router,
            net_map=net_map,
            nets_routed=nets_routed,
            nets_to_route=nets_to_route,
            completion=completion,
            success=completion >= args.min_completion,
            stats=stats,
            overflow=overflow,
        )

        # Track best result (Issue #2396: absolute nets_routed comparison)
        if best_result is None or _is_better_result(result, best_result):
            best_result = result
            if not quiet:
                flush_print(
                    f"  Best result so far: {best_result.layer_count}L with "
                    f"{best_result.nets_routed}/{best_result.nets_to_route} "
                    f"({best_result.completion:.0%})"
                )

        # Issue #2673: Completion-floor guard.  Both early-termination heuristics
        # below (zero-overflow at #2412, stagnation at #2412) are calibrated for
        # the case where the router already routed a substantial fraction of the
        # board on the prior attempt.  When best-so-far completion is very low
        # (e.g., < 50%), the failure mode is more likely "router got stuck on a
        # handful of nets and timed out" than "the design is genuinely unrouteable
        # with more layers", so we should keep trying additional layer
        # configurations rather than short-circuit.  Board 05 on 2026-05-11
        # exhibits exactly this regression: 2L=0/35, 4L sig-gnd-pwr-sig=0/35,
        # stagnation check fires and 4L all-sig is never attempted.  The same
        # board at commit a9790ad0 produced 2L=9/35 (26%) and tried all three
        # 4L variants before stopping.  Floor of 50% is conservative — any
        # board with >=50% completion has enough signal for the heuristics
        # to be trustworthy; below that, escalation should run to completion.
        best_completion_so_far = best_result.completion if best_result is not None else 0.0
        completion_floor_for_early_stop = 0.5
        below_completion_floor = best_completion_so_far < completion_floor_for_early_stop

        # Issue #2412: Early termination — zero overflow means failures
        # are placement/topology issues, not congestion.  Adding layers
        # cannot help when there is no congestion to relieve.
        #
        # Issue #2634: This heuristic was calibrated for ``route_all_negotiated``,
        # which deliberately allows overlapping tracks and records overflow as
        # a first-class congestion signal.  The basic A*, monte-carlo, and
        # evolutionary strategies never plant overlaps, so ``grid.get_total_overflow()``
        # is 0 by construction regardless of true congestion.  Reading 0 from
        # those strategies as "no congestion" is wrong, and historically it
        # broke after attempt #1 even with ``--auto-layers`` on (see the
        # chorus-test-revA fixture, which negotiated escalates 2L -> 4L while
        # MC stopped at 2L=42%).  Skip the heuristic for those strategies.
        strategies_without_overflow_signal = {"monte-carlo", "basic", "evolutionary"}
        # Issue #3371 / P_FP5: when the next attempt is the via-in-pad
        # fallback retry at the *same* layer count, do not exit on
        # zero-overflow.  Via-in-pad fallback is precisely the rescue
        # mechanism for non-congestion (clearance) failures around
        # fine-pitch packages -- the canonical case is UCC27211 SOIC-8
        # pin-2 escape on softstart rev B.  Without this skip the L=2
        # baseline -> L=2 fallback transition would be cut off when the
        # 2L baseline finishes with 0 overflow + missing fine-pitch escapes.
        _next_is_same_layer_fallback = (
            attempt_num < len(layer_configs)
            and layer_configs[attempt_num][2]  # next entry's via_in_pad_fallback
            and layer_configs[attempt_num][0] == layer_count  # same layer count
            and not via_in_pad_fallback
        )
        if (
            overflow == 0
            and nets_routed < nets_to_route
            and args.strategy not in strategies_without_overflow_signal
            and not below_completion_floor
            and not _next_is_same_layer_fallback
        ):
            if not quiet:
                flush_print(
                    "  Escalation stopped: failures are not congestion-related (overflow=0)"
                )
            # Report attempt result before breaking
            if not quiet:
                flush_print(
                    f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)"
                )
                flush_print("  Status: INSUFFICIENT - early stop (zero overflow)")
            break
        elif (
            overflow == 0
            and nets_routed < nets_to_route
            and args.strategy not in strategies_without_overflow_signal
            and below_completion_floor
            and not quiet
        ):
            flush_print(
                "  Note: overflow=0 but best completion "
                f"{best_completion_so_far * 100:.0f}% < "
                f"{completion_floor_for_early_stop * 100:.0f}% floor — "
                "continuing escalation (issue #2673)"
            )

        # Issue #2412: Early termination — stagnation detection.  If adding
        # layers did not improve nets_routed or reduce overflow, further
        # escalation is unlikely to help.
        #
        # Issue #3371 / P_FP5: skip when this attempt is the fallback retry
        # at the *same* layer count as the previous attempt -- we have not
        # added layers, we have changed the via-in-pad opt-in, so the
        # comparison is not "adding layers did not help" but "the fallback
        # did not improve over baseline at this layer count".  The next
        # attempt will be at a higher layer count and the stagnation check
        # there will use the correct baseline.
        _this_is_same_layer_fallback = (
            attempt_num > 1
            and via_in_pad_fallback
            and layer_configs[attempt_num - 2][0] == layer_count
            and not layer_configs[attempt_num - 2][2]
        )
        if (
            prev_nets_routed is not None
            and nets_routed <= prev_nets_routed
            and overflow >= prev_overflow
            and not below_completion_floor
            and not _this_is_same_layer_fallback
        ):
            if not quiet:
                flush_print("  Escalation stopped: no improvement after adding layers")
            # Report attempt result before breaking
            if not quiet:
                flush_print(
                    f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)"
                )
                flush_print("  Status: INSUFFICIENT - early stop (stagnation)")
            break
        elif (
            prev_nets_routed is not None
            and nets_routed <= prev_nets_routed
            and overflow >= prev_overflow
            and below_completion_floor
            and not quiet
        ):
            flush_print(
                "  Note: stagnation detected but best completion "
                f"{best_completion_so_far * 100:.0f}% < "
                f"{completion_floor_for_early_stop * 100:.0f}% floor — "
                "continuing escalation (issue #2673)"
            )

        # Issue #3241: Monotonic-regression early-exit.  Unlike the #2412
        # stagnation check above, this is NOT gated by the #2673 50% floor.
        # "More layers + strictly fewer routed nets" is structurally
        # backwards -- adding still more layers cannot cure it -- so we
        # break the ladder even when both attempts are below 50%.  The
        # check requires either (a) a hard drop of >=HARD_DROP_NETS on a
        # single attempt, or (b) CONSECUTIVE_REGRESSIONS consecutive
        # regressions each exceeding REGRESSION_TOLERANCE.  ``best_result``
        # is already tracked via the #2396 ``_is_better_result`` rule above,
        # so the loop just breaks; the caller receives the pre-regression
        # best attempt's router state.
        #
        # Issue #3371 / P_FP5: skip this regression check when the previous
        # attempt was the via-in-pad-fallback retry at the *same* layer
        # count as this attempt's baseline.  The fallback attempt may
        # legitimately route fewer nets (the smaller via-in-pad geometry
        # disables some surface escape paths in trade for fine-pitch
        # rescues), but that's a within-layer-count tradeoff, not a
        # "more layers + fewer nets" structural regression.  Without
        # this skip the L=4-fallback -> L=6-baseline pair could trigger
        # a false-positive exit, cutting off the rest of the ladder.
        regression_exit = False
        _prev_was_same_layer_fallback = (
            attempt_num > 1
            and layer_configs[attempt_num - 2][2]  # previous via_in_pad_fallback
            and layer_configs[attempt_num - 2][0] == layer_count  # same layer count
        )
        if prev_nets_routed is not None and not _prev_was_same_layer_fallback:
            drop = prev_nets_routed - nets_routed
            if drop >= HARD_DROP_NETS:
                if not quiet:
                    flush_print(
                        f"  Escalation stopped: monotonic regression -- "
                        f"hard drop of {drop} nets vs previous attempt "
                        f"(>={HARD_DROP_NETS} threshold, issue #3241)"
                    )
                regression_exit = True
            elif drop > REGRESSION_TOLERANCE:
                regression_streak += 1
                if regression_streak >= CONSECUTIVE_REGRESSIONS:
                    if not quiet:
                        flush_print(
                            f"  Escalation stopped: monotonic regression -- "
                            f"{regression_streak} consecutive attempts with "
                            f"nets_routed decreasing by >{REGRESSION_TOLERANCE} "
                            "(issue #3241)"
                        )
                    regression_exit = True
                elif not quiet:
                    flush_print(
                        f"  Regression observed: {drop} fewer nets routed "
                        f"than previous attempt (streak={regression_streak}/"
                        f"{CONSECUTIVE_REGRESSIONS}, issue #3241)"
                    )
            else:
                # Improvement or flicker within tolerance -- reset streak.
                regression_streak = 0

        prev_nets_routed = nets_routed
        prev_overflow = overflow

        if regression_exit:
            # Report attempt result before breaking, mirroring the #2412
            # exit paths above.  ``best_result`` already retains the
            # pre-regression best per the #2396 selector.
            if not quiet:
                flush_print(
                    f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)"
                )
                flush_print("  Status: INSUFFICIENT - early stop (regression)")
                # Issue #3241 (Option D): emit failure-cause histogram so
                # future investigations of the same regression have data.
                _log_failure_cause_histogram(router, quiet=quiet)
            break

        # Issue #2388: Record any power-net stall for the next attempt's
        # bias logic.  ``power_stall_nets`` is populated by
        # ``route_all_negotiated`` when the early-abort heuristic fires.
        if getattr(router, "power_stall_abort", False):
            last_power_stall_nets = list(getattr(router, "power_stall_nets", []))
            if not quiet and last_power_stall_nets:
                flush_print(
                    f"  Power-net stall on this attempt: {', '.join(last_power_stall_nets)}"
                )
        else:
            last_power_stall_nets = []

        # Report attempt result
        status = "SUCCESS" if result.success else "INSUFFICIENT - escalating"
        if not quiet:
            flush_print(f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)")
            flush_print(f"  Status: {status}")
            # Issue #3241 (Option D): per-attempt failure-cause histogram.
            # Helps diagnose why a higher-layer attempt can land at a worse
            # local optimum (the trigger for this issue's regression-exit).
            _log_failure_cause_histogram(router, quiet=quiet)

        # Check for success
        if result.success:
            successful_result = result
            break

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("LAYER ESCALATION SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Design routed successfully on {final_result.layer_count} layers "
                f"({final_result.completion * 100:.0f}% completion)"
            )
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result on {final_result.layer_count} layers "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"on any layer count (max: {args.max_layers})"
            )
            # Issue #2388: Surface actionable suggestions when escalation
            # exhausted because of a power-net stall.
            if last_power_stall_nets:
                _print_power_stall_suggestions(
                    last_power_stall_nets,
                    final_result.layer_count,
                    args.pcb,
                )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
            if last_power_stall_nets:
                _print_power_stall_suggestions(
                    last_power_stall_nets,
                    args.max_layers,
                    args.pcb,
                )
        return 1

    # Issue #2621: placement-routing feedback for partial layer-escalation
    # results.  The main single-pass entry point hooks ``_run_placement_feedback``
    # at the end of routing (route_cmd.py around line 4802), but the
    # auto-layers escalation path never reached that hook -- so
    # ``--placement-feedback`` on a board that exhausts layer escalation
    # with PARTIAL completion silently did nothing.  Engage the feedback
    # loop here, mirroring the main-path trigger:
    #   * only when escalation didn't already succeed,
    #   * only when --placement-feedback is set,
    #   * only when there are still failed nets to address.
    # The loop mutates ``final_result.router.routes`` in place, so we
    # refresh ``final_result``'s stats afterwards before optimize/save.
    _maybe_run_placement_feedback_escalation(
        final_result,
        successful_result,
        pcb_path,
        args,
        quiet,
        stall_label="layer escalation",
    )

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
            optimize_routes_grid_synced,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.  This prevents the
        # optimizer from fragmenting routes through overused cells.
        has_overflow = final_result.router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(
            final_result.router.grid, ignore_overflow=has_overflow
        )
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        # Issue #2596: snapshot per-net connectivity before optimize so
        # we can revert any net whose pad-to-pad connectivity regresses.
        _ci_snapshot = _connectivity_snapshot(final_result.router)

        with spinner("Optimizing traces...", quiet=quiet):
            # Issue #3507: grid-transactional optimize -- each mutated
            # route's old copper is unmarked and the new copper marked so
            # the grid never goes stale across the pass.
            optimize_routes_grid_synced(final_result.router, optimizer)

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot,
            phase="optimize",
            args=args,
            quiet=quiet,
        )

    # Post-optimization DRC nudge pass
    if final_result.router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        # Issue #2596: snapshot connectivity again before nudge.  The
        # post-optimize routes are the new baseline -- nudge must not
        # regress them further.
        _ci_snapshot_nudge = _connectivity_snapshot(final_result.router)

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(final_result.router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot_nudge,
            phase="nudge",
            args=args,
            quiet=quiet,
        )

        # Issue #4208 (Unit 3): re-run the Unit-2 seg-seg finalize gate
        # over the post-optimize/post-nudge copper.  An rtree-less
        # optimizer can introduce a cross-net crossing the pre-optimize
        # finalize gate never saw; demote it before the canonical write.
        _finalize_committed_copper_or_demote(final_result.router, quiet=quiet)

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    _final_multi_pad_ids = {n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2}
    route_sexp, final_stats, _cleanup_stats = _finalize_routes(
        final_result.router,
        _final_multi_pad_ids,
        final_result.nets_to_route,
        quiet=quiet,
        strict=bool(getattr(args, "strict", False)),
        verbose=bool(getattr(args, "verbose", False)),
        preserve_existing=bool(getattr(args, "preserve_existing", False)),
        preserved_routes=_preserved_routes,
    )
    # Update result with post-cleanup stats
    final_result.nets_routed = final_stats["nets_routed"]
    final_result.completion = (
        final_result.nets_routed / final_result.nets_to_route
        if final_result.nets_to_route > 0
        else 1.0
    )
    final_result.success = final_result.completion >= args.min_completion

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        if not route_sexp and not quiet:
            print("  Warning: No routes generated!")
        # Issue #2808: atomic write via _write_routed_pcb (consolidates the
        # read -> stackup-update -> insert -> validate -> write sequence).
        # Issue #2809: honor --output exactly; layer count is recorded in
        # the PCB content via update_pcb_layer_stackup, NOT in the filename.
        _write_routed_pcb(
            pcb_path,
            output_path,
            route_sexp,
            layer_count=final_result.layer_count,
        )

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Layer count: {final_result.layer_count}")

    # Fill copper-pour zones now that traces exist (issue #2516).
    # Must run BEFORE DRC so the DRC sees filled zones rather than bare
    # zone outlines and does not flag zone-to-trace clearance against
    # unfilled polygons.
    if final_result.nets_routed > 0:
        _fill_zones_after_route(output_path, quiet=quiet)

    # Run DRC validation unless skipped
    fix_result: int | None = None
    if not args.skip_drc and final_result.nets_routed > 0:
        drc_errors, _ = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
            # Issue #2652, Epic #2556 Phase 2.5b: thread the autorouter's
            # net_class_map into the post-route DRC so the diff-pair
            # routing-continuity rule can re-derive its engagement state.
            net_class_map=getattr(final_result.router, "net_class_map", None),
            # Issue #4178: forward --strict-drc so a native DRC that did
            # not run becomes a hard failure instead of a soft NOTE.
            strict_drc=getattr(args, "strict_drc", False),
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            fix_result = _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
                args=args,  # Issue #2802: honor total wall-clock deadline
            )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print(f"SUCCESS: Design requires minimum {final_result.layer_count} layers")
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"on {final_result.layer_count} layers"
            )
            _multi_pad_ids = {
                n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
            }
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
                current_strategy=args.strategy,
                pcb_file=args.pcb,
                nets_to_route_ids=_multi_pad_ids,
                single_pad_count=getattr(final_result, "single_pad_count", 0),
                # Issue #2634: auto-layer escalation already ran in this code
                # path; suppress the redundant "Try --auto-layers" recommendation.
                auto_layers_attempted=True,
            )

    # Issue #2881: Stash the final router on args so an outer
    # ``route_with_mfr_tier_escalation`` wrapper can inspect
    # ``missed_via_in_pad_rescues`` to decide whether to escalate to the
    # next manufacturer tier.  Harmless when no outer wrapper exists.
    with contextlib.suppress(AttributeError, TypeError):
        args._last_router = final_result.router
        # Issue #3352 (P_AS3): Stash the full LayerEscalationResult so an
        # outer ``route_with_size_escalation`` wrapper can inspect
        # ``nets_routed`` / ``nets_to_route`` / ``overflow`` to construct
        # ``RoutingResultMetrics`` and call ``decide_escalation``.
        args._last_layer_result = final_result

    if final_result.success:
        # Issue #3238: propagate auto-fix-skipped-by-deadline so the
        # caller exits with the distinct exit code (7) rather than the
        # generic exit-0 success path.
        if getattr(args, "_auto_fix_status", None) == "skipped_deadline":
            return 7
        # Issue #2852: propagate --auto-fix rollback (exit 3) so callers can
        # detect a silent rollback on an otherwise-clean routing run.  The
        # documented exit-3 contract ("meets threshold but DRC violations
        # detected") already covers this case semantically.
        if fix_result == 3:
            return 3
        return 0
    # Partial routing: some nets were routed but not all — pipeline should continue
    if final_result.nets_routed > 0:
        return 2
    # Nothing was routed — treat as fatal failure
    return 1


def route_with_rule_relaxation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with automatic design rule relaxation.

    Tries routing with progressively relaxed design rules (trace width,
    clearance) until success or manufacturer minimum limits are reached.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import flush_print, spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        ensure_cpp_backend_available,
        get_relaxation_tiers,
        load_pcb_for_routing,
        show_routing_summary,
    )
    from kicad_tools.router.io import detect_layer_stack

    # Handle backend selection (auto-build C++ extension on first use; #2549)
    ok, force_python, exit_code = ensure_cpp_backend_available(
        backend=args.backend,
        quiet=quiet,
        allow_auto_build=not getattr(args, "no_auto_build_native", False),
    )
    if not ok:
        return exit_code if exit_code is not None else 1

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Auto-create copper pours for power nets (before skip detection).
    # auto_pour_if_missing writes in-place; stage a copy at output_path
    # first so the user's INPUT is left untouched (issue #2548).
    # Issue #3092: forward user-supplied skip_nets as force_pour_nets (see
    # the layer-escalation site above for the rationale).
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _stage_input_for_auto_pour(pcb_path, output_path)
        auto_pour_if_missing(
            pcb_path,
            quiet=quiet,
            edge_clearance=getattr(args, "edge_clearance", None),
            force_pour_nets=skip_nets,
        )

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=quiet)

    # Get relaxation tiers
    tiers = get_relaxation_tiers(
        initial_trace_width=args.trace_width,
        initial_clearance=args.clearance,
        initial_via_drill=args.via_drill,
        initial_via_diameter=args.via_diameter,
        manufacturer=args.manufacturer,
        min_trace_floor=args.min_trace,
        min_clearance_floor=args.min_clearance_floor,
    )

    # Issue #3155: capture preserved copper once before routing/checkpoints.
    _preserve = bool(getattr(args, "preserve_existing", False))
    _preserved_routes = _capture_preserved_routes(pcb_path) if _preserve else []
    _preserved_sexp = _serialize_preserved_routes(_preserved_routes) if _preserve else ""

    # Determine layer stack
    if args.layers == "auto":
        pcb_text = pcb_path.read_text()
        layer_stack = detect_layer_stack(pcb_text)
    else:
        layer_stack_map = {
            "2": LayerStack.two_layer(),
            "4": LayerStack.four_layer_sig_gnd_pwr_sig(),
            "4-sig": LayerStack.four_layer_sig_sig_gnd_pwr(),
            "4-all": LayerStack.four_layer_all_signal(),
            "6": LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig(),
        }
        layer_stack = layer_stack_map[args.layers]

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Adaptive Rules Mode")
        flush_print("=" * 60)
        flush_print(f"Input:          {pcb_path}")
        flush_print(f"Output:         {output_path}")
        flush_print(f"Strategy:       {args.strategy}")
        flush_print(f"Manufacturer:   {args.manufacturer}")
        flush_print(f"Min completion: {args.min_completion * 100:.0f}%")
        flush_print(f"Relaxation tiers: {len(tiers)}")
        if skip_nets:
            flush_print(f"Skip:           {', '.join(skip_nets)}")
        flush_print()

    best_result: RuleRelaxationResult | None = None
    successful_result: RuleRelaxationResult | None = None

    # Register signal handlers so SIGTERM/SIGINT save the best attempt so far
    _interrupt_state["output_path"] = output_path
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["router"] = None
    _interrupt_state["interrupted"] = False
    _interrupt_state["best_completed_attempt"] = False
    prev_sigint = signal.signal(signal.SIGINT, _handle_interrupt)
    prev_sigterm = signal.signal(signal.SIGTERM, _handle_interrupt)

    # Issue #3051: Build the checkpoint callback ONCE before the
    # relaxation loop so every per-tier ``route_all_negotiated`` call
    # gets best-so-far persistence (closes the iteration-0 kill-loses-
    # work hole observed in the curator audit).
    _checkpoint_cb = _make_checkpoint_callback(
        pcb_path,
        output_path,
        float(getattr(args, "checkpoint_interval", 30.0) or 0.0),
        quiet=quiet,
        preserved_sexp=_preserved_sexp,
    )

    # Issue #2823: precompute total tier count so per-attempt budget can
    # divide the remaining wall-clock budget fairly across all tiers
    # rather than letting tier 0 greedily consume the full ``--timeout``.
    _relaxation_max_attempts = max(1, len(tiers))
    for _tier_idx, tier in enumerate(tiers):
        # Issue #2802: honor the total wall-clock deadline before starting
        # another rule-relaxation tier.
        if _deadline_expired(args):
            if not quiet:
                flush_print(
                    f"  Wall-clock deadline reached before tier {tier.tier + 1}; "
                    "stopping rule relaxation (issue #2802)"
                )
            break

        if not quiet:
            flush_print("=" * 60)
            flush_print(f"Attempt {tier.tier + 1}: {tier.description}")
            flush_print(f"  trace={tier.trace_width:.3f}mm, clearance={tier.clearance:.3f}mm")
            flush_print("=" * 60)

        # Configure design rules for this tier
        fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
        rules = DesignRules(
            grid_resolution=args.grid,
            trace_width=tier.trace_width,
            trace_clearance=tier.clearance,
            via_drill=tier.via_drill,
            via_diameter=tier.via_diameter,
            fine_pitch_clearance=fine_pitch_cl,
            # Issue #2695: forward manufacturer so the escape router can opt
            # in to in-pad escape for fine-pitch LQFP/QFP/SSOP/TSSOP when
            # the manufacturer supports via-in-pad processing.
            manufacturer=getattr(args, "manufacturer", None),
            # Issue #2891: forward escalation-in-progress flag.
            auto_mfr_tier_in_progress=getattr(args, "_auto_mfr_tier_in_progress", False),
        )

        # Load PCB
        try:
            with spinner(f"Loading PCB (tier {tier.tier})...", quiet=quiet):
                router, net_map = load_pcb_for_routing(
                    str(pcb_path),
                    skip_nets=skip_nets,
                    rules=rules,
                    edge_clearance=args.edge_clearance,
                    layer_stack=layer_stack,
                    force_python=force_python,
                    # Issue #4268: thread the mesh-router strategy selector through.
                    strategy=getattr(args, "route_engine", "grid"),
                    validate_drc=not args.force,
                    strict_drc=False,
                    # Issue #3155: incremental routing (see route_with_layer_escalation).
                    load_existing_routes=getattr(args, "preserve_existing", False),
                    # Issue #4148: region-bounded routing (see main()).
                    region=getattr(args, "_region_box", None),
                    # Issue #4170 (Phase 2b-1): board-relative boundary stub
                    # terminals whose tip cells are carved open as same-net
                    # reconnection targets (None when no --region / no stubs).
                    stub_terminals=getattr(args, "_stub_terminals", None),
                )
        except Exception as e:
            if not quiet:
                print(f"  Error loading PCB: {e}")
            continue

        # Issue #2996: merge --net-class-map sidecar onto router's map.
        _apply_net_class_map_sidecar(router, args, quiet=quiet)
        # Issue #3470: thread --max-ripups-per-net into the destructive
        # rip-up budgets (route_all + two-phase stall recovery).
        _apply_ripup_budget_override(router, args)
        _apply_rescue_pass_override(router, args)
        _apply_bundle_river_planner(router, args)
        _apply_monotone_certificate_order(router, args)
        _apply_cross_package_pair_corridor(router, args)
        _apply_slack_corridor_widening(router, args)

        # Issue #3171: inject boosted analog routing class for --analog-nets /
        # --auto-analog selected nets (pour/ground nets are left untouched).
        _apply_analog_net_class(router, args, quiet=quiet)

        # Issue #3371 (P_FP3): surface the fine-pitch escape regions that
        # ``load_pcb_for_routing`` installed (if any) and warn when the
        # detector ran without a manufacturer floor.
        _log_fine_pitch_escape_regions(router, quiet=quiet)

        # Issue #1841: Tell the autorouter which pour nets lack zones
        router._pour_nets_without_zones = set(_no_zone)

        # Count nets to route.  Issue #3942 (Bug B): exclude pour-served
        # multi-pad nets (router-stripped via _filter_pour_nets) from the
        # denominator so routed/total matches what the router was asked to
        # route.  See _routable_multi_pad_nets for the rationale.
        multi_pad_nets = _routable_multi_pad_nets(router)
        single_pad_nets = [
            net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) == 1
        ]
        nets_to_route = len(multi_pad_nets)

        if not quiet:
            flush_print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
            flush_print(f"  Nets to route: {nets_to_route}")
            _emit_single_pad_net_warning(router, single_pad_nets)

        # Route
        if not quiet:
            flush_print(f"\n  Routing ({args.strategy})...")

        escape_flag = _resolve_escape_routing_flag(args)

        # Issue #2823: divide the remaining wall-clock budget fairly across
        # the remaining rule-relaxation tiers so the looser-rule attempts
        # also get a real chance to run.  Without this, tier 0 greedily
        # consumes the full ``--timeout`` and the relaxation strategy
        # degenerates to "spend everything on the strictest tier, give up."
        # Falls back to ``args.timeout`` when no deadline is configured.
        _attempt_timeout = _per_attempt_budgeted_timeout(
            args,
            attempt_index=_tier_idx,
            max_attempts=_relaxation_max_attempts,
        )

        try:
            if _should_use_escape_routing(router, escape_flag, quiet):
                # Issue #3952: compose the escape pre-phase with the
                # CoupledPathfinder diff-pair pre-pass when
                # --differential-pairs is requested so Phase A runs on
                # escape-forced boards; otherwise take the unchanged escape
                # path (byte-identical for no-pair boards).
                _dp_cfg = _build_diffpair_config(args)
                if _dp_cfg is not None:
                    router.route_with_escape_and_diffpairs(
                        _dp_cfg,
                        use_negotiated=(args.strategy == "negotiated"),
                        timeout=_attempt_timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    )
                else:
                    router.route_with_escape(
                        use_negotiated=(args.strategy == "negotiated"),
                        timeout=_attempt_timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    )
            elif getattr(args, "multi_resolution", False):
                router.route_all_multi_resolution(
                    use_negotiated=(args.strategy == "negotiated"),
                    max_iterations=args.iterations,
                    timeout=_attempt_timeout,
                )
            elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                router.route_all_two_phase(
                    use_negotiated=True,
                    corridor_width_factor=2.0,
                    timeout=_attempt_timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                )
            elif args.strategy == "negotiated":
                router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=_attempt_timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    batch_routing=getattr(args, "batch_routing", False)
                    or getattr(args, "high_performance", False),
                    hierarchical=getattr(args, "hierarchical", False),
                    perturbation=getattr(args, "perturbation", True),
                    # Issue #3039: forward --seed for deterministic routing.
                    seed=getattr(args, "seed", None),
                    # Issue #3054 (Phase 2 of #3045): forward region-based
                    # parallelism opt-in.  Defaults preserve single-threaded
                    # behaviour bit-for-bit.
                    region_parallel=getattr(args, "region_parallel", False),
                    partition_rows=getattr(args, "partition_rows", 2),
                    partition_cols=getattr(args, "partition_cols", 2),
                    max_parallel_workers=getattr(args, "max_parallel_workers", 4),
                    # Issue #3051: forward checkpoint callback so kills
                    # mid-loop persist the best-so-far snapshot.
                    checkpoint_callback=_checkpoint_cb,
                    # Issue #3438 / #3414: forward --targeted-ripup so the
                    # pre-existing targeted rip-up path in
                    # route_all_negotiated is CLI-reachable.
                    use_targeted_ripup=getattr(args, "targeted_ripup", False),
                    max_ripups_per_net=_targeted_ripup_budget(args),
                    # Issue #3101: best-metric early-stop patience.  0
                    # disables (matches pre-#3101 behaviour).
                    best_stall_patience=(getattr(args, "early_stop_patience", 2) or None),
                )
            elif args.strategy == "basic":
                router.route_all()
            elif args.strategy == "monte-carlo":
                router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
            elif args.strategy == "evolutionary":
                router.route_all_evolutionary(
                    pop_size=args.pop_size,
                    generations=args.generations,
                    verbose=args.verbose and not quiet,
                    timeout=_attempt_timeout,
                )
        except Exception as e:
            if not quiet:
                print(f"  Routing error: {e}")
            continue

        # Issue #2426: Run cleanup before computing statistics so that the
        # best-result selector compares post-cleanup connectivity counts.
        router.cleanup_artifacts()

        # Calculate completion — filter to multi-pad nets only (Issue #1643)
        multi_pad_net_ids = set(multi_pad_nets)
        stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)
        nets_routed = stats["nets_routed"]
        completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0

        # Create result
        result = RuleRelaxationResult(
            tier=tier.tier,
            trace_width=tier.trace_width,
            clearance=tier.clearance,
            via_drill=tier.via_drill,
            via_diameter=tier.via_diameter,
            tier_description=tier.description,
            router=router,
            net_map=net_map,
            nets_routed=nets_routed,
            nets_to_route=nets_to_route,
            completion=completion,
            success=completion >= args.min_completion,
            layer_count=layer_stack.num_layers,
            stats=stats,
        )

        # Track best result (Issue #2396: absolute nets_routed comparison)
        if best_result is None or _is_better_result(result, best_result):
            best_result = result
            # Update interrupt state so signal handler saves the best attempt
            _interrupt_state["router"] = result.router
            _interrupt_state["best_completed_attempt"] = True

        # Report attempt result
        status = "SUCCESS" if result.success else "INSUFFICIENT - relaxing rules"
        if not quiet:
            flush_print(f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)")
            flush_print(f"  Status: {status}")

        # Check for success
        if result.success:
            successful_result = result
            break

        # Early termination: skip remaining tiers when completion regresses
        if not getattr(args, "no_early_stop", False) and best_result is not None:
            if completion < best_result.completion:
                if not quiet:
                    flush_print(
                        f"\n  Early stop: tier {tier.tier + 1} completion "
                        f"({completion * 100:.0f}%) is worse than best "
                        f"({best_result.completion * 100:.0f}%) — "
                        f"skipping remaining tiers"
                    )
                break

    # Restore original signal handlers
    signal.signal(signal.SIGINT, prev_sigint)
    signal.signal(signal.SIGTERM, prev_sigterm)
    _interrupt_state["best_completed_attempt"] = False

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("ADAPTIVE RULES SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Design routed successfully with relaxed rules "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print("\nFinal design rules:")
            print(f"  Trace width: {final_result.trace_width:.3f}mm (was {args.trace_width}mm)")
            print(f"  Clearance:   {final_result.clearance:.3f}mm (was {args.clearance}mm)")
            if final_result.tier > 0:
                print(f"\n  Note: Rules were relaxed ({final_result.tier_description})")
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result at tier {final_result.tier} "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"even at manufacturer minimum tolerances"
            )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
        return 1

    # Issue #4151: engage placement-routing feedback when --placement-feedback
    # is set and rule relaxation stalled with unrouted nets.  Before this
    # hook, --placement-feedback was silently dropped on the rule-relaxation
    # dispatch path (--adaptive-rules without effective --auto-layers).
    _maybe_run_placement_feedback_escalation(
        final_result,
        successful_result,
        pcb_path,
        args,
        quiet,
        stall_label="rule relaxation",
    )

    # Check if at manufacturer minimum
    from kicad_tools.router import get_mfr_limits

    mfr = get_mfr_limits(args.manufacturer)
    at_minimum = (
        final_result.trace_width <= mfr.min_trace + 0.001
        and final_result.clearance <= mfr.min_clearance + 0.001
    )
    if at_minimum and not quiet:
        print(f"\nWARNING: Design uses {args.manufacturer.upper()} minimum tolerances.")
        print("Consider adding layers for more manufacturing margin.")

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
            optimize_routes_grid_synced,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.
        has_overflow = final_result.router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(
            final_result.router.grid, ignore_overflow=has_overflow
        )
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        # Issue #2596: snapshot per-net connectivity before optimize.
        _ci_snapshot = _connectivity_snapshot(final_result.router)

        with spinner("Optimizing traces...", quiet=quiet):
            # Issue #3507: grid-transactional optimize (see
            # optimize_routes_grid_synced).
            optimize_routes_grid_synced(final_result.router, optimizer)

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot,
            phase="optimize",
            args=args,
            quiet=quiet,
        )

    # Post-optimization DRC nudge pass
    if final_result.router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        # Issue #2596: snapshot connectivity before nudge.
        _ci_snapshot_nudge = _connectivity_snapshot(final_result.router)

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(final_result.router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot_nudge,
            phase="nudge",
            args=args,
            quiet=quiet,
        )

        # Issue #4208 (Unit 3): re-run the Unit-2 seg-seg finalize gate
        # over the post-optimize/post-nudge copper.  An rtree-less
        # optimizer can introduce a cross-net crossing the pre-optimize
        # finalize gate never saw; demote it before the canonical write.
        _finalize_committed_copper_or_demote(final_result.router, quiet=quiet)

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    _final_multi_pad_ids = {n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2}
    route_sexp, final_stats, _cleanup_stats = _finalize_routes(
        final_result.router,
        _final_multi_pad_ids,
        final_result.nets_to_route,
        quiet=quiet,
        strict=bool(getattr(args, "strict", False)),
        verbose=bool(getattr(args, "verbose", False)),
        preserve_existing=bool(getattr(args, "preserve_existing", False)),
        preserved_routes=_preserved_routes,
    )
    # Update result with post-cleanup stats
    final_result.nets_routed = final_stats["nets_routed"]
    final_result.completion = (
        final_result.nets_routed / final_result.nets_to_route
        if final_result.nets_to_route > 0
        else 1.0
    )
    final_result.success = final_result.completion >= args.min_completion

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        if not route_sexp and not quiet:
            print("  Warning: No routes generated!")
        # Issue #2808: atomic write via _write_routed_pcb.  Rule-relaxation
        # flow always runs at the same layer count it started with, so we
        # pass layer_count=2 (no stackup update needed for the typical 2L
        # rule-relaxation path; layer_count is a no-op when <= 2 anyway).
        _write_routed_pcb(
            pcb_path,
            output_path,
            route_sexp,
            layer_count=final_result.layer_count,
        )

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Final trace width: {final_result.trace_width:.3f}mm")
        print(f"  Final clearance: {final_result.clearance:.3f}mm")

    # Fill copper-pour zones now that traces exist (issue #2516).
    if final_result.nets_routed > 0:
        _fill_zones_after_route(output_path, quiet=quiet)

    # Run DRC validation unless skipped
    fix_result: int | None = None
    if not args.skip_drc and final_result.nets_routed > 0:
        drc_errors, _ = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
            # Issue #2652, Epic #2556 Phase 2.5b: thread the autorouter's
            # net_class_map into the post-route DRC so the diff-pair
            # routing-continuity rule can re-derive its engagement state.
            net_class_map=getattr(final_result.router, "net_class_map", None),
            # Issue #4178: forward --strict-drc so a native DRC that did
            # not run becomes a hard failure instead of a soft NOTE.
            strict_drc=getattr(args, "strict_drc", False),
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            fix_result = _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
                args=args,  # Issue #2802: honor total wall-clock deadline
            )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print("SUCCESS: Routing complete with adaptive rules")
            if final_result.tier > 0:
                print(
                    f"  Note: Relaxed from tier 0 to tier {final_result.tier} "
                    f"({final_result.tier_description})"
                )
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"at tier {final_result.tier}"
            )
            _multi_pad_ids = {
                n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
            }
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
                current_strategy=args.strategy,
                pcb_file=args.pcb,
                nets_to_route_ids=_multi_pad_ids,
                single_pad_count=getattr(final_result, "single_pad_count", 0),
            )

    if final_result.success:
        # Issue #3238: propagate auto-fix-skipped-by-deadline.
        if getattr(args, "_auto_fix_status", None) == "skipped_deadline":
            return 7
        # Issue #2852: propagate --auto-fix rollback (exit 3) so callers can
        # detect a silent rollback on an otherwise-clean routing run.
        if fix_result == 3:
            return 3
        return 0
    # Partial routing: some nets were routed but not all — pipeline should continue
    if final_result.nets_routed > 0:
        return 2
    # Nothing was routed — treat as fatal failure
    return 1


def _log_failure_cause_histogram(router, quiet: bool = False) -> None:
    """Emit a per-attempt failure-cause histogram.

    Issue #3241 (Option D): when the auto-layers escalation ladder
    produces decreasing results across attempts, the operator log should
    make the *why* legible.  Currently each attempt prints a "Best result
    so far" line and the run ends with a Status line — no breakdown of
    which failure causes are dominant on the failed nets.

    This helper aggregates ``router.routing_failures`` by
    :class:`FailureCause` and prints a single ``Failure causes: {...}``
    line in machine-parseable format.  It is intentionally low-cost
    (~5 lines of Counter + dict() formatting) so it is safe to call on
    every attempt, not just the regression-exit path.

    Args:
        router: Inner ``Autorouter`` instance from the latest attempt.
            May be ``None`` or a mock without ``routing_failures``.
        quiet: Suppress all output when ``True``.
    """
    if quiet or router is None:
        return
    from collections import Counter

    from kicad_tools.cli.progress import flush_print

    failures = getattr(router, "routing_failures", None)
    if not failures:
        return

    causes: list[str] = []
    for f in failures:
        cause = getattr(f, "failure_cause", None)
        if cause is None:
            continue
        # ``cause`` is a FailureCause enum; ``.value`` is the snake_case
        # string already used elsewhere in the codebase (route_cmd.py
        # _classify_dominant_failure_cause).
        causes.append(getattr(cause, "value", str(cause)))
    if not causes:
        return

    histogram = dict(Counter(causes).most_common())
    flush_print(f"  Failure causes: {histogram}")


def _classify_dominant_failure_cause(router):
    """Pick the most-common FailureCause across ``router.routing_failures``.

    Issue #2883: the outer ``--auto-mfr-tier`` loop needs to consult the
    :data:`router.failure_analysis.MFR_TIER_ESCALATION_TRIGGERS` registry
    before walking forward.  That requires picking a single dominant cause
    from the (possibly heterogeneous) set of per-net failures returned by
    the inner routing attempt.

    The rule used here:

    1. If the router has no ``routing_failures`` attribute (e.g. the inner
       call was stubbed in tests or returned before classification), return
       ``None`` so callers fall back to the legacy
       ``missed_via_in_pad_rescues`` signal.
    2. Otherwise, tally :class:`FailureCause` values across all failures
       and return the most common.  Ties are broken by the registry order
       in :data:`MFR_TIER_ESCALATION_TRIGGERS` (triggering causes win), so
       that an even split between e.g. PIN_ACCESS and BLOCKED_PATH still
       lets escalation engage.

    Args:
        router: Inner ``Autorouter`` instance (may be ``None`` or a mock
            that lacks ``routing_failures``).

    Returns:
        The dominant :class:`FailureCause`, or ``None`` if no failure
        records were available.
    """
    from collections import Counter

    if router is None:
        return None
    failures = getattr(router, "routing_failures", None)
    if not failures:
        return None

    # Tally causes; ignore entries without a recognized FailureCause.
    causes = []
    for f in failures:
        cause = getattr(f, "failure_cause", None)
        if cause is not None:
            causes.append(cause)
    if not causes:
        return None

    counter = Counter(causes)
    most_common_count = counter.most_common(1)[0][1]

    # Find all causes tied for most common.
    tied = [c for c, n in counter.items() if n == most_common_count]
    if len(tied) == 1:
        return tied[0]

    # Tie-break: prefer a triggering cause so escalation isn't suppressed
    # by an arbitrary tally tie.  Lazy import to avoid module cycles.
    from kicad_tools.router.failure_analysis import MFR_TIER_ESCALATION_TRIGGERS

    for cause in tied:
        if MFR_TIER_ESCALATION_TRIGGERS.get(cause, False):
            return cause
    # No triggering cause present in the tie; return any (the first).
    return tied[0]


def route_with_mfr_tier_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with manufacturer-tier escalation (Issue #2881).

    Walks the ladder of manufacturer tiers (e.g. ``jlcpcb`` ->
    ``jlcpcb-tier1``).  For each tier in the ladder, mutates
    ``args.manufacturer`` to point at that tier and re-enters the
    layer-escalation path (``--auto-layers`` is enabled implicitly because
    this entry point is only reached when the user passed
    ``--auto-mfr-tier``; the inner call respects whatever
    ``args.auto_layers`` is set to).

    Escalation triggers on the per-attempt ``EscapeRouter`` instrumentation
    counter (``missed_via_in_pad_rescues``).  When that counter is non-zero
    after a routing attempt completes, the next tier in the ladder is
    tried -- but only if that tier offers a real capability gain (via-in-pad
    OR scalar relaxation; see :func:`mfr_limits.can_escalate_via_in_pad`
    and :func:`can_escalate_scalar`).  Pure same-scalar/same-capability
    tier swaps are skipped to avoid pointless retry loops.

    Trigger table (from Issue #2881):

    +----------------------------+-------------+----------------------------+
    | FailureCause               | Escalate?   | Why / why not              |
    +----------------------------+-------------+----------------------------+
    | PIN_ACCESS + fine-pitch    | YES         | Exactly what tier1 fixes.  |
    | + via_in_pad missing       |             |                            |
    +----------------------------+-------------+----------------------------+
    | CLEARANCE at mfr minimum   | conditional | Only if next tier offers   |
    |                            |             | scalar relaxation.         |
    +----------------------------+-------------+----------------------------+
    | BLOCKED_PATH               | NO          | Placement issue, not       |
    |                            |             | manufacturer-fixable.      |
    +----------------------------+-------------+----------------------------+
    | CONGESTION                 | NO          | Layer issue;               |
    |                            |             | --auto-layers handles it.  |
    +----------------------------+-------------+----------------------------+
    | UNKNOWN / timeouts         | NO          | Algorithm issue, may mask  |
    |                            |             | bugs.                      |
    +----------------------------+-------------+----------------------------+

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments (must have ``manufacturer``,
            ``auto_mfr_tier``, optionally ``mfr_tier_ladder``)
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure, 2 = partial)
    """
    from kicad_tools.cli.mfr_tier_budget import per_tier_routing_budget
    from kicad_tools.cli.progress import flush_print
    from kicad_tools.router.failure_analysis import (
        MFR_TIER_ESCALATION_TRIGGERS,
        should_escalate_mfr_tier,
    )
    from kicad_tools.router.mfr_limits import (
        can_escalate_scalar,
        can_escalate_via_in_pad,
        get_mfr_limits,
        get_mfr_tier_ladder,
    )

    # Resolve the ladder.  Explicit --mfr-tier-ladder wins; otherwise look
    # up the default ladder for args.manufacturer.
    explicit_ladder = getattr(args, "mfr_tier_ladder", None)
    if explicit_ladder:
        ladder = [t.strip() for t in explicit_ladder.split(",") if t.strip()]
        # Validate each ladder entry resolves to a real manufacturer.
        for tier_name in ladder:
            get_mfr_limits(tier_name)  # raises ValueError on unknown
    else:
        try:
            ladder = get_mfr_tier_ladder(args.manufacturer)
        except ValueError as e:
            flush_print(f"Error: {e}", file=sys.stderr)
            return 1

    # Find the user's starting tier in the ladder.  If args.manufacturer
    # is not in the ladder, start from the first ladder entry that
    # matches; otherwise prepend args.manufacturer at the head.
    start_idx = 0
    cur_canonical = args.manufacturer.lower()
    for i, t in enumerate(ladder):
        if t.lower() == cur_canonical:
            start_idx = i
            break

    tiers_to_try = ladder[start_idx:]

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Manufacturer-Tier Escalation Mode")
        flush_print("=" * 60)
        flush_print(f"Input:           {pcb_path}")
        flush_print(f"Output:          {output_path}")
        flush_print(f"Starting tier:   {args.manufacturer}")
        flush_print(f"Tier ladder:     {' -> '.join(tiers_to_try)}")
        flush_print()

    last_exit_code: int = 1
    last_router = None
    final_exit_code: int = 1
    saw_terminating_success: bool = False

    # Issue #2891: suppress the per-attempt "does not support via-in-pad"
    # ERROR log (#2880) while escalation is in flight.  The flag is read
    # by ``EscapeRouter`` via ``rules.auto_mfr_tier_in_progress`` (set by
    # the DesignRules construction sites elsewhere in this module).  We
    # explicitly clear it before the FINAL tier attempt so a fully-
    # exhausted ladder still surfaces the diagnostic on its last try.
    last_tier_idx = len(tiers_to_try) - 1

    # Issue #2881: budget-fair per-tier slicing.  Reuse the existing
    # ``_per_attempt_budgeted_timeout`` helper transparently by having the
    # inner ``route_with_layer_escalation`` call divide ``args.timeout``
    # across its own layer attempts.  Wall-clock deadline is enforced by
    # ``_deadline_expired`` checked at the top of each tier iteration.
    for tier_idx, tier_name in enumerate(tiers_to_try):
        if _deadline_expired(args):
            if not quiet:
                flush_print(
                    f"  Wall-clock deadline reached before tier "
                    f"{tier_idx + 1} ({tier_name}); stopping tier escalation."
                )
            break

        # Convergence guard (Issue #2881): only attempt subsequent tiers
        # when the new tier offers a real capability gain over the current
        # one.  Pure same-scalar/same-capability swaps are no-ops and would
        # produce identical routing results (potentially looping forever
        # if some tier got registered twice).
        if tier_idx > 0:
            prev_tier = tiers_to_try[tier_idx - 1]
            gains_capability = can_escalate_via_in_pad(prev_tier, tier_name)
            gains_scalar = can_escalate_scalar(prev_tier, tier_name)
            # Issue #2881: trigger-aware escalation -- only walk forward
            # when the failure mode is one that escalation could fix.
            # ``missed_via_in_pad_rescues`` is the canonical signal for
            # "via-in-pad would have helped".  Without an instrumented
            # signal, we still escalate if either capability gain holds
            # (defensive: the user opted in to escalation, and a tighter
            # tier is registered in the ladder).
            triggered_by_missed_in_pad = False
            if last_router is not None:
                # Read the canonical private attribute set by
                # Autorouter._escape (see core.py:8856).
                escape_router = getattr(last_router, "_escape_router", None)
                if escape_router is not None:
                    missed = getattr(escape_router, "missed_via_in_pad_rescues", 0)
                    try:
                        triggered_by_missed_in_pad = bool(missed and int(missed) > 0)
                    except (TypeError, ValueError):
                        triggered_by_missed_in_pad = False

            # Issue #2883: consult MFR_TIER_ESCALATION_TRIGGERS on the
            # dominant failure cause from the previous tier.  When the
            # router reports a placement-class failure (BLOCKED_PATH,
            # CONGESTION, KEEPOUT, ROUTING_ORDER, UNKNOWN, ...) the
            # trigger table returns False, and we suppress escalation
            # regardless of whether the next tier offers a capability /
            # scalar gain -- escalation cannot fix a placement problem.
            #
            # When ``routing_failures`` is unavailable (e.g. in unit
            # tests that stub the inner call), ``dominant_cause`` is
            # None and we fall back to the legacy
            # ``missed_via_in_pad_rescues`` / capability-gain heuristic
            # exactly as before.
            dominant_cause = _classify_dominant_failure_cause(last_router)
            trigger_table_vetoes = False
            if dominant_cause is not None:
                # The cause is recognized AND the registry explicitly
                # excludes it from escalation -- veto.
                if dominant_cause in MFR_TIER_ESCALATION_TRIGGERS and not should_escalate_mfr_tier(
                    dominant_cause
                ):
                    trigger_table_vetoes = True

            should_escalate = False
            reason = ""
            # Issue #3463: the canonical ``missed_via_in_pad_rescues``
            # signal takes precedence over the dominant-cause trigger-table
            # veto.  A non-zero missed-rescue counter is *direct,
            # instrumented* evidence that one or more fine-pitch pins would
            # be rescued by an in-pad via on the next tier -- strictly
            # stronger than a statistical tally of per-net failure causes.
            #
            # On board-04 the dominant cause across the (few) failing nets
            # is classified as BLOCKED_PATH (a single keepout-blocked net),
            # which the trigger table marks not-manufacturer-fixable.  But
            # the EscapeRouter separately recorded a non-zero
            # ``missed_via_in_pad_rescues`` for the LQFP-48 inner pins --
            # exactly what ``jlcpcb-tier1`` fixes.  Letting the BLOCKED_PATH
            # tally veto escalation here aborts the ladder before the
            # capable tier ever runs (the #3463 regression).  So we check
            # the missed-rescue signal *first*: when it fires and the next
            # tier gains via-in-pad capability, escalate regardless of the
            # dominant-cause tally.  The trigger-table veto still applies
            # when there is *no* instrumented missed-rescue signal (the
            # legacy placement-class suppression for #2883).
            if gains_capability and triggered_by_missed_in_pad:
                should_escalate = True
                reason = "missed via-in-pad rescues detected on previous tier"
            elif trigger_table_vetoes:
                # Trigger table says this failure category is not
                # manufacturer-fixable and there is no missed-rescue
                # signal to override it.  Suppress escalation even if
                # capability / scalar gain exists.
                reason = (
                    f"dominant failure cause ({dominant_cause.value}) is not "
                    "manufacturer-fixable (trigger table veto)"
                )
            elif gains_capability:
                # No instrumented missed-rescue signal, but capability gain
                # exists -- escalate defensively (the user asked for it).
                should_escalate = True
                reason = (
                    "next tier offers via-in-pad capability (no missed-rescue signal available)"
                )
            elif gains_scalar:
                should_escalate = True
                reason = "next tier offers scalar relaxation (clearance/trace/via)"

            if not should_escalate:
                if not quiet:
                    if trigger_table_vetoes:
                        flush_print(f"  Skipping tier {tier_name}: {reason}.")
                    else:
                        flush_print(
                            f"  Skipping tier {tier_name}: no capability or scalar "
                            f"gain over {prev_tier} (convergence guard)."
                        )
                break

            if not quiet:
                flush_print(f"  Escalating to {tier_name}: {reason}")

        # Mutate args to point at this tier.  Note: route_with_layer_escalation
        # re-reads args.manufacturer when constructing DesignRules, so the
        # mutation takes effect for the next inner call.
        original_mfr = args.manufacturer
        args.manufacturer = tier_name

        # Issue #2891: declare whether escalation is in flight for THIS
        # tier attempt.  All tiers except the final one are escalation-in-
        # progress (the inner ERROR is a false alarm because we will retry
        # on the next tier).  The FINAL tier is NOT escalation-in-progress:
        # if it fails, the user must see the ERROR.  The DesignRules
        # construction sites in this module read this attribute via
        # ``getattr(args, "_auto_mfr_tier_in_progress", False)`` and
        # forward it onto ``DesignRules.auto_mfr_tier_in_progress``,
        # which gates the demotion in
        # ``EscapeRouter._escape_qfp_alternating``.
        is_final_tier = tier_idx == last_tier_idx
        args._auto_mfr_tier_in_progress = not is_final_tier
        try:
            if not quiet:
                flush_print("=" * 60)
                flush_print(f"Tier {tier_idx + 1}/{len(tiers_to_try)}: {tier_name}")
                flush_print("=" * 60)

            # Dispatch to the layer-escalation path.  When args.auto_layers
            # is False we fall through to single-layer routing.  When True,
            # full 2D escalation (layers x mfr-tier) occurs.
            #
            # Issue #3463: wrap the inner call in a per-tier routing-budget
            # window so the base tier's layer-escalation loop cannot consume
            # the entire routing deadline.  Without this, the inner
            # ``_per_attempt_budgeted_timeout`` sees the full remaining
            # budget at base-tier time, the base tier expands to fill it,
            # and ``_deadline_expired(args)`` is True at the top of the next
            # tier iteration -- so the ladder aborts before ever attempting
            # ``jlcpcb-tier1`` (the via-in-pad-capable tier).  The context
            # manager narrows ``args._routing_deadline`` to a fair per-tier
            # slice and restores the original deadline on exit (so auto-fix
            # still sees its reserved budget).
            with per_tier_routing_budget(
                args,
                tier_index=tier_idx,
                tier_count=len(tiers_to_try),
            ):
                # Both branches dispatch identically today; when the user
                # explicitly disabled --auto-layers the layer-escalation
                # path honours --max-layers via its inner filter.
                inner_rc = route_with_layer_escalation(
                    pcb_path=pcb_path,
                    output_path=output_path,
                    args=args,
                    quiet=quiet,
                )

            last_exit_code = inner_rc
            final_exit_code = inner_rc

            # Read the stashed router from the inner call.  This is the
            # signal source for ``missed_via_in_pad_rescues`` -- when
            # non-zero on a failed tier, the next iteration knows to walk
            # forward (if the next tier offers via-in-pad capability).
            last_router = getattr(args, "_last_router", None)

            # Successful routing -- stop escalation.
            if inner_rc == 0:
                saw_terminating_success = True
                if not quiet:
                    flush_print(
                        f"\n  Tier {tier_name} achieved routing success; stopping tier escalation."
                    )
                # Print cost note if escalation actually moved off the
                # starting tier.
                if tier_idx > 0:
                    final_limits = get_mfr_limits(tier_name)
                    if final_limits.cost_note:
                        flush_print(
                            f"\nRecommendation: order from {tier_name}. {final_limits.cost_note}."
                        )
                break

        finally:
            # Restore original manufacturer only when we did NOT succeed --
            # on success the mutation is intentional (and surfaced via the
            # cost-note recommendation).  On failure, restore so subsequent
            # CLI calls aren't surprised.
            if last_exit_code != 0:
                args.manufacturer = original_mfr
            # Issue #2891: always clear the escalation-in-progress flag
            # on exit so callers that re-use ``args`` aren't surprised by
            # log demotion in unrelated code paths.
            args._auto_mfr_tier_in_progress = False

    # Diagnostic: name the constraint when escalation did not succeed.
    if not saw_terminating_success and not quiet:
        flush_print("\n" + "=" * 60)
        flush_print("MANUFACTURER-TIER ESCALATION SUMMARY")
        flush_print("=" * 60)
        flush_print(f"Result: No tier in {' -> '.join(tiers_to_try)} achieved routing success.")

        # Issue #2884: name the dominant unfixable constraint surfaced by
        # the last inner attempt before printing the generic remediation
        # list.  This points the user at the specific component / pin /
        # constraint that blocked progress rather than leaving them to
        # guess from a categorical 4-option menu.
        named_line = _name_dominant_unfixable_constraint(
            last_router=last_router,
            manufacturer=args.manufacturer,
        )
        if named_line:
            flush_print("\nDiagnosis:")
            flush_print(f"  {named_line}")

        # Concrete remediation options.  Always print at least three so
        # users have actionable alternatives:
        flush_print("\nOptions:")
        flush_print(
            "  1. Switch to a tighter manufacturer tier (try "
            "--mfr-tier-ladder with a custom ladder)."
        )
        flush_print(
            "  2. Change fine-pitch package(s) to a wider-pitch alternative "
            "(e.g. LQFP-48 0.5mm -> LQFP-32 0.8mm)."
        )
        flush_print(
            "  3. Move fine-pitch component(s) toward the board centre so "
            "escape traces have a wider channel (5+mm from edge)."
        )
        flush_print("  4. Add layers via --auto-layers --max-layers 6 (if not already on).")

    return final_exit_code


def route_with_size_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with auto-pcb-size escalation (Issue #3352, P_AS3).

    Wraps :func:`route_with_layer_escalation` (or the single-layer path when
    ``--no-auto-layers`` is set).  After each inner routing attempt, builds
    a :class:`RoutingResultMetrics` from the stashed
    ``args._last_layer_result`` and calls
    :func:`kicad_tools.router.auto_pcb_size.decide_escalation` to determine
    the next step.

    Decision handling:

      * :attr:`EscalationDecision.NO_ESCALATION_NEEDED` -- inner routing met
        the reach + DRC density thresholds; return the inner exit code.
      * :attr:`EscalationDecision.ESCALATE` -- grow the PCB outline via
        :func:`kicad_tools.pcb.outline.grow_board_outline_corner_anchored`
        (corner-anchored per Q2), advance the tier index, re-route.
      * :attr:`EscalationDecision.REFUSE_HARD_ENVELOPE` -- emit the
        actionable error from the architect proposal (BOM / layers /
        clearance / envelope-relax levers) and return the inner exit code.
      * :attr:`EscalationDecision.REFUSE_HOLES_DONT_FIT` -- emit an error
        naming the hole-group anchor and the new envelope; return inner
        exit code.
      * :attr:`EscalationDecision.REFUSE_MAX_TIER` -- ladder exhausted;
        emit error noting the topmost tier reached.

    Per Q5: ``--auto-pcb-size`` IMPLIES ``--auto-layers`` unless
    ``--no-auto-layers`` was explicit.  This wrapper does NOT enforce
    that policy itself -- it dispatches to the layer-escalation path when
    ``args.auto_layers`` is truthy and to the single-attempt path
    otherwise.  Setting ``args.auto_layers = True`` at the CLI dispatch
    site is the canonical way to honour the Q5 implication.

    The escalation policy comes from ``args._escalation_policy`` when the
    CLI dispatch site has loaded one from ``project.kct``; otherwise a
    default :class:`EscalationPolicy` is constructed (which defaults to
    ``layers-first`` ladder and 0.5 viols/cm^2 density trigger).

    Args:
        pcb_path: Path to input PCB file.
        output_path: Path for output routed PCB file.
        args: Parsed command-line arguments.  Honours
            ``args.auto_layers``, ``args.manufacturer``,
            ``args._escalation_policy`` (optional), ``args._envelope_hard``
            (optional), ``args._hole_group`` (optional).
        quiet: Suppress output.

    Returns:
        Exit code (0 = success, 1 = failure, 2 = partial,
        3 = DRC violations).  Mirrors the inner function's contract.
    """
    from kicad_tools.cli.progress import flush_print
    from kicad_tools.pcb.outline import (
        OutlineGrowError,
        grow_board_outline_corner_anchored,
    )
    from kicad_tools.router.auto_pcb_size import (
        EscalationContext,
        EscalationDecision,
        RoutingResultMetrics,
        decide_escalation,
        envelope_meets_area_estimate,
        estimate_required_area,
    )
    from kicad_tools.router.io import extract_board_dimensions
    from kicad_tools.router.mfr_limits import (
        find_smallest_admitting_tier,
        get_mfr_limits,
        get_mfr_size_tier_ladder,
    )
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.spec.schema import EscalationPolicy

    # Resolve the escalation policy.  CLI dispatch site may have loaded
    # one from project.kct; otherwise use the default policy (layers-first
    # ladder, 0.5 viols/cm^2 density trigger, max_layers=4).
    policy: EscalationPolicy = getattr(args, "_escalation_policy", None) or EscalationPolicy()
    envelope_hard: bool = bool(getattr(args, "_envelope_hard", False))
    hole_group = getattr(args, "_hole_group", None)

    # Issue #3403: --packing-overhead CLI flag overrides the policy default.
    # When the user passes --packing-overhead N, replace the policy's value
    # with a copy honouring the flag.  ``None`` means "no override" -- use
    # the policy's value as-is.
    cli_packing_overhead = getattr(args, "packing_overhead", None)
    if cli_packing_overhead is not None:
        policy = policy.model_copy(update={"packing_overhead": float(cli_packing_overhead)})

    # Manufacturer for the size ladder.  Mirrors the layer-escalation
    # path's reliance on args.manufacturer.
    manufacturer = getattr(args, "manufacturer", "jlcpcb") or "jlcpcb"

    # Issue #3403: manufacturer limits used by the pre-route area estimator.
    # Resolved once outside the loop because the limits don't change
    # across size-tier escalation steps (clearance is a separate axis;
    # auto-mfr-tier handles clearance escalation independently).
    try:
        mfr_limits_for_estimate = get_mfr_limits(manufacturer)
    except ValueError:
        # Defensive: unknown manufacturer.  Fall back to a conservative
        # clearance (0.127 mm, the JLCPCB default) so the estimator still
        # produces a sane number.  The reactive backstop will catch
        # mis-estimates.
        from kicad_tools.router.mfr_limits import MFR_JLCPCB

        mfr_limits_for_estimate = MFR_JLCPCB

    # Discover the starting tier index from the current board dimensions.
    dims = extract_board_dimensions(pcb_path)
    if dims is None:
        if not quiet:
            flush_print(
                "Error: --auto-pcb-size requires a board outline on Edge.Cuts; "
                "no outline detected.  Run `kct pcb edit-outline --set-outline rect "
                "--origin X Y --size W H` to add one before re-running.",
                file=sys.stderr,
            )
        return 1

    cur_w, cur_h = dims
    try:
        ladder = get_mfr_size_tier_ladder(manufacturer)
    except ValueError as exc:
        if not quiet:
            flush_print(f"Error: {exc}", file=sys.stderr)
        return 1

    starting_tier = find_smallest_admitting_tier(cur_w, cur_h, manufacturer)
    if starting_tier is None:
        # Board already exceeds the largest tier; can't escalate further.
        if not quiet:
            flush_print(
                f"Error: --auto-pcb-size: current board ({cur_w:g}x{cur_h:g} mm) "
                f"already exceeds the largest registered tier for {manufacturer} "
                f"({ladder[-1].max_width_mm:g}x{ladder[-1].max_height_mm:g} mm).",
                file=sys.stderr,
            )
        return 1

    # Find the index of the starting tier in the ladder.  The smallest-
    # admitting tier is unique by construction in the architect proposal's
    # ladder (ascending area), so this is deterministic.
    current_tier_index = 0
    for i, t in enumerate(ladder):
        if (
            abs(t.max_width_mm - starting_tier.max_width_mm) < 1e-6
            and abs(t.max_height_mm - starting_tier.max_height_mm) < 1e-6
        ):
            current_tier_index = i
            break

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Size Escalation Mode")
        flush_print("=" * 60)
        flush_print(f"Input:              {pcb_path}")
        flush_print(f"Output:             {output_path}")
        flush_print(f"Starting envelope:  {cur_w:g}x{cur_h:g} mm")
        flush_print(
            f"Starting tier:      [{current_tier_index}] "
            f"{starting_tier.max_width_mm:g}x{starting_tier.max_height_mm:g} mm "
            f"({starting_tier.note or 'no note'})"
        )
        flush_print(f"Policy ladder:      {policy.ladder}")
        flush_print(f"Envelope hard:      {envelope_hard}")
        flush_print()

    # Track the last exit code to return when no escalation is needed
    # or refusal is encountered after a successful-ish inner attempt.
    last_exit_code: int = 1

    # P_AS4: Multi-attempt regression detection.  Mirrors the pattern in
    # ``route_with_layer_escalation`` (PR #3244 -- REGRESSION_TOLERANCE,
    # HARD_DROP_NETS, CONSECUTIVE_REGRESSIONS) but accumulates across
    # *size* attempts rather than *layer* attempts.  ``decide_escalation``
    # consumes the list via its ``history`` parameter and may return
    # ``REFUSE_REGRESSION`` when growing the envelope produces strictly
    # worse routing.
    history: list[RoutingResultMetrics] = []

    # P_AS4: Resolve the ladder strategy.  ``layers-first`` walks the
    # inner layer-escalation loop fully at each size tier; ``size-first``
    # walks size tiers first with layer-escalation pinned at the recipe's
    # starting layer count, then walks layers as a final fallback at the
    # largest tier.  Both modes share the size-grow + decision-tree
    # plumbing below; the inner dispatch picks which call path runs.
    ladder_policy = policy.ladder
    size_first_mode = ladder_policy in ("size-first", "size-only")

    # When in size-first mode, save the original max_layers so we can
    # honour it as a final fallback after the size ladder exhausts.  The
    # inner routing call is pinned to the smallest allowed layer count
    # while the outer loop walks size tiers.
    _original_max_layers = int(getattr(args, "max_layers", 4) or 4)
    if size_first_mode:
        # Pin inner layer count to the smallest layer config that admits
        # this PCB; for most recipes that's 2.  The layer-escalation
        # filter inside route_with_layer_escalation will collapse the
        # ladder to a single rung at that count.
        # NB: this mutates args but we restore it before the layers-as-
        # fallback final attempt; it's the same pattern the
        # auto-mfr-tier escalation uses to swap manufacturer per attempt.
        args.max_layers = 2

    # Outer size-escalation loop.  Each iteration:
    #   1. Run the inner layer-escalation path (or single-attempt path).
    #   2. Read the stashed metrics from args._last_layer_result.
    #   3. Call decide_escalation; act on the result.
    #
    # The loop bounds itself by REFUSE_MAX_TIER -- ladder exhaustion is
    # the terminal failure mode.  Without that bound the loop could run
    # forever if the trigger never settles; the manufacturer's ladder is
    # finite by construction (typically 4-6 rungs), so this is safe.
    MAX_SIZE_ATTEMPTS = len(ladder) + 1  # defensive cap
    attempt_num = 0
    while attempt_num < MAX_SIZE_ATTEMPTS:
        attempt_num += 1

        if not quiet and attempt_num > 1:
            flush_print("=" * 60)
            cur_tier = ladder[current_tier_index]
            flush_print(
                f"Size attempt {attempt_num}: tier [{current_tier_index}] "
                f"{cur_tier.max_width_mm:g}x{cur_tier.max_height_mm:g} mm "
                f"({cur_tier.note or 'no note'})"
            )
            flush_print("=" * 60)

        # Issue #3403: pre-route sum-of-clearances area-estimate check.
        # Before spending a routing budget, compute the geometric lower
        # bound on the envelope area required for the design.  When the
        # current envelope is smaller than the estimate, the attempt is
        # structurally infeasible -- skip directly to the next size tier.
        #
        # The reactive DRC-density backstop stays as the fallback for
        # cases where the heuristic under-estimates (loose layouts, low
        # pin-count designs).  We deliberately log the estimate even
        # when the envelope meets it, so calibration data is captured
        # in the route summary.
        pre_route_skip = False
        if policy.packing_overhead > 0:
            try:
                pre_pcb = PCB.load(pcb_path)
                area_estimate = estimate_required_area(
                    pre_pcb,
                    mfr_limits_for_estimate,
                    packing_overhead=policy.packing_overhead,
                )
                # Use the CURRENT envelope (not the stale starting envelope).
                pre_dims = extract_board_dimensions(pcb_path)
                if pre_dims is not None:
                    pre_w, pre_h = pre_dims
                    pre_envelope_mm2 = pre_w * pre_h
                    if not quiet:
                        flush_print(
                            f"  Area estimate: {area_estimate.total_mm2:.0f} mm^2 "
                            f"required "
                            f"(footprints={area_estimate.footprint_area_mm2:.0f}, "
                            f"halo={area_estimate.clearance_halo_mm2:.0f}, "
                            f"channels={area_estimate.routing_channel_mm2:.0f}, "
                            f"x{area_estimate.packing_overhead:g} packing) "
                            f"vs envelope {pre_envelope_mm2:.0f} mm^2"
                        )
                    if not envelope_meets_area_estimate(pre_envelope_mm2, area_estimate):
                        ratio = (
                            pre_envelope_mm2 / area_estimate.total_mm2
                            if area_estimate.total_mm2 > 0
                            else 0.0
                        )
                        if not quiet:
                            flush_print(
                                f"  Pre-route check: envelope < estimate "
                                f"(ratio {ratio:.2f}); skipping doomed route "
                                f"attempt and escalating directly."
                            )
                        pre_route_skip = True
            except Exception as exc:
                # Best-effort: if the estimate fails (malformed PCB,
                # missing data), log and fall through to the normal
                # route attempt.  The estimator is an OPTIMISATION, not
                # a correctness gate -- silently degrading to the
                # reactive path is the right behaviour.
                if not quiet:
                    flush_print(
                        f"  Pre-route check: area estimate failed ({exc!r}); "
                        f"falling back to reactive escalation."
                    )

        if pre_route_skip:
            # Skip the inner routing attempt -- jump directly to the
            # ESCALATE branch below.  Synthesise minimal metrics so the
            # decision tree treats this attempt as "envelope too small"
            # (low reach, no DRC density yet -- but we set the density
            # above threshold so should_escalate fires).
            #
            # Use the current envelope for the area term so density
            # divisor is meaningful.
            board_w, board_h = pre_dims
            board_area_cm2 = (board_w * board_h) / 100.0
            # Synthetic metrics: 0% reach + above-threshold density so
            # decide_escalation returns ESCALATE (or a refusal if the
            # ladder/hard-envelope blocks).  We pick density = 2 *
            # threshold to be safely above the trigger floor.
            density_floor = policy.density_threshold_viols_per_cm2
            metrics = RoutingResultMetrics(
                signal_nets_routed=0,
                signal_nets_total=1,
                drc_violations=int(2 * density_floor * board_area_cm2) + 1,
                board_area_cm2=board_area_cm2,
            )
            history.append(metrics)

            context = EscalationContext(
                current_tier_index=current_tier_index,
                policy=policy,
                manufacturer=manufacturer,
                hole_group=hole_group,
                envelope_hard=envelope_hard,
            )
            decision = decide_escalation(metrics, context, history=history)

            if decision == EscalationDecision.REFUSE_MAX_TIER:
                if not quiet:
                    _print_size_escalation_refusal(
                        reason="max_tier",
                        current_tier_index=current_tier_index,
                        ladder=ladder,
                        cur_dims=(board_w, board_h),
                    )
                return 1

            if decision == EscalationDecision.REFUSE_HARD_ENVELOPE:
                if not quiet:
                    _print_size_escalation_refusal(
                        reason="envelope_hard",
                        current_tier_index=current_tier_index,
                        ladder=ladder,
                        cur_dims=(board_w, board_h),
                    )
                return 1

            if decision == EscalationDecision.REFUSE_HOLES_DONT_FIT:
                if not quiet:
                    _print_size_escalation_refusal(
                        reason="holes_dont_fit",
                        current_tier_index=current_tier_index,
                        ladder=ladder,
                        cur_dims=(board_w, board_h),
                        hole_group=hole_group,
                    )
                return 1

            # Grow the board and loop.  Mirrors the post-route ESCALATE
            # branch below; we duplicate rather than refactor to keep the
            # pre-route path self-contained.
            next_index = current_tier_index + 1
            if next_index >= len(ladder):
                if not quiet:
                    _print_size_escalation_refusal(
                        reason="max_tier",
                        current_tier_index=current_tier_index,
                        ladder=ladder,
                        cur_dims=(board_w, board_h),
                    )
                return 1
            next_tier = ladder[next_index]
            if not quiet:
                flush_print(
                    f"  Growing board (pre-route skip): tier [{next_index}] "
                    f"{next_tier.max_width_mm:g}x{next_tier.max_height_mm:g} mm "
                    f"({next_tier.note or 'no note'})"
                )
            try:
                pcb_obj = PCB.load(pcb_path)
                grow_board_outline_corner_anchored(
                    pcb_obj,
                    new_width_mm=next_tier.max_width_mm,
                    new_height_mm=next_tier.max_height_mm,
                )
                pcb_obj.save(pcb_path)
            except OutlineGrowError as exc:
                if not quiet:
                    flush_print(
                        f"Error: --auto-pcb-size: outline grow failed: {exc}",
                        file=sys.stderr,
                    )
                return 1
            current_tier_index = next_index
            continue  # restart the loop at the new tier (re-estimate)

        # Dispatch to the inner routing path.  Both branches call into
        # route_with_layer_escalation -- the difference is whether layers
        # are pinned (size-first) or allowed to escalate (layers-first /
        # default).  args.max_layers is mutated above for size-first.
        inner_rc = route_with_layer_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=quiet,
        )

        last_exit_code = inner_rc

        # Read the inner attempt's metrics from the stash.  When the
        # stash is absent (e.g. inner routing crashed before reaching
        # the stash site), conservatively report 0 nets routed and exit.
        last_result = getattr(args, "_last_layer_result", None)
        if last_result is None:
            if not quiet:
                flush_print(
                    "  Size escalation: inner routing did not stash a result; "
                    "exiting escalation loop (returning inner exit code)."
                )
            return inner_rc

        # Compute board area from the current outline.  After the first
        # attempt, the outline reflects the just-attempted (grown) size;
        # for subsequent attempts the metrics' area must match the size
        # actually attempted so density is computed against the right
        # denominator.
        post_dims = extract_board_dimensions(pcb_path)
        if post_dims is None:
            # Defensive: outline was destroyed somehow; can't escalate
            # without an outline to grow.
            if not quiet:
                flush_print(
                    "  Size escalation: lost board outline after routing; exiting escalation loop."
                )
            return inner_rc
        board_w, board_h = post_dims
        board_area_cm2 = (board_w * board_h) / 100.0

        # DRC-violation proxy: the router's grid overflow counter.  A
        # principled implementation would invoke a full DRC pass here
        # (see ``run_drc`` in kicad_tools.drc.checker), but P_AS3 uses
        # the cheaper proxy because the routing attempt already
        # populated it.  Overflow on the grid represents track-track
        # congestion, which is the dominant failure mode the
        # auto-pcb-size trigger is designed to detect.  Promote to a
        # real DRC count in a follow-up (P_AS4 candidate).
        nets_routed = int(getattr(last_result, "nets_routed", 0) or 0)
        nets_total = int(getattr(last_result, "nets_to_route", 0) or 0)
        overflow = int(getattr(last_result, "overflow", 0) or 0)

        metrics = RoutingResultMetrics(
            signal_nets_routed=nets_routed,
            signal_nets_total=nets_total,
            drc_violations=overflow,
            board_area_cm2=board_area_cm2,
        )

        # P_AS4: accumulate the cross-attempt history so the regression
        # detector can fire on the second (and later) attempts when the
        # ladder is making things worse.
        history.append(metrics)

        context = EscalationContext(
            current_tier_index=current_tier_index,
            policy=policy,
            manufacturer=manufacturer,
            hole_group=hole_group,
            envelope_hard=envelope_hard,
        )

        decision = decide_escalation(metrics, context, history=history)

        if not quiet:
            flush_print(
                f"  Size-escalation decision: {decision.value} "
                f"(reach={metrics.completion:.0%}, "
                f"drc_density={metrics.drc_density:.2f}/cm^2 over "
                f"{board_area_cm2:.1f} cm^2)"
            )

        if decision == EscalationDecision.NO_ESCALATION_NEEDED:
            # Inner routing was good enough -- return the inner exit code.
            return inner_rc

        if decision == EscalationDecision.REFUSE_REGRESSION:
            if not quiet:
                _print_size_escalation_refusal(
                    reason="regression",
                    current_tier_index=current_tier_index,
                    ladder=ladder,
                    cur_dims=(board_w, board_h),
                )
            return inner_rc

        if decision == EscalationDecision.REFUSE_MAX_TIER:
            # In size-first mode, exhaust the size ladder, then try a
            # final pass with full layer escalation at the largest tier.
            # If that already happened (max_layers restored), refuse cleanly.
            if size_first_mode and args.max_layers != _original_max_layers:
                if not quiet:
                    flush_print(
                        "  Size ladder exhausted; falling back to layer "
                        f"escalation at tier [{current_tier_index}] (size-first policy)."
                    )
                args.max_layers = _original_max_layers
                size_first_mode = False  # don't loop here again
                # Re-dispatch to the layer-escalation path immediately.
                inner_rc = route_with_layer_escalation(
                    pcb_path=pcb_path,
                    output_path=output_path,
                    args=args,
                    quiet=quiet,
                )
                last_exit_code = inner_rc
                # Read the final attempt's metrics and decide once more.
                last_result_final = getattr(args, "_last_layer_result", None)
                if last_result_final is not None:
                    final_nets_routed = int(getattr(last_result_final, "nets_routed", 0) or 0)
                    final_nets_total = int(getattr(last_result_final, "nets_to_route", 0) or 0)
                    final_overflow = int(getattr(last_result_final, "overflow", 0) or 0)
                    final_metrics = RoutingResultMetrics(
                        signal_nets_routed=final_nets_routed,
                        signal_nets_total=final_nets_total,
                        drc_violations=final_overflow,
                        board_area_cm2=board_area_cm2,
                    )
                    if not quiet:
                        flush_print(
                            f"  Size-first fallback result: reach={final_metrics.completion:.0%}, "
                            f"drc_density={final_metrics.drc_density:.2f}/cm^2"
                        )
                return inner_rc
            if not quiet:
                _print_size_escalation_refusal(
                    reason="max_tier",
                    current_tier_index=current_tier_index,
                    ladder=ladder,
                    cur_dims=(board_w, board_h),
                )
            return inner_rc

        if decision == EscalationDecision.REFUSE_HARD_ENVELOPE:
            if not quiet:
                _print_size_escalation_refusal(
                    reason="envelope_hard",
                    current_tier_index=current_tier_index,
                    ladder=ladder,
                    cur_dims=(board_w, board_h),
                )
            return inner_rc

        if decision == EscalationDecision.REFUSE_HOLES_DONT_FIT:
            if not quiet:
                _print_size_escalation_refusal(
                    reason="holes_dont_fit",
                    current_tier_index=current_tier_index,
                    ladder=ladder,
                    cur_dims=(board_w, board_h),
                    hole_group=hole_group,
                )
            return inner_rc

        # decision == ESCALATE -- grow the board to the next tier.
        next_index = current_tier_index + 1
        if next_index >= len(ladder):
            # Defensive: decide_escalation said ESCALATE but the ladder
            # has no room.  Treat as max-tier refusal.
            if not quiet:
                _print_size_escalation_refusal(
                    reason="max_tier",
                    current_tier_index=current_tier_index,
                    ladder=ladder,
                    cur_dims=(board_w, board_h),
                )
            return inner_rc

        next_tier = ladder[next_index]
        if not quiet:
            flush_print(
                f"  Growing board: tier [{next_index}] "
                f"{next_tier.max_width_mm:g}x{next_tier.max_height_mm:g} mm "
                f"({next_tier.note or 'no note'})"
            )

        # Grow the outline corner-anchored (Q2).  Load the PCB, mutate,
        # save back.  The routing pipeline expects pcb_path to be the
        # canonical input each iteration; the staged output_path may
        # have been consumed earlier in the loop, so we mutate pcb_path
        # in place here -- callers wanting an untouched input should
        # have set --output to a distinct path (the layer-escalation
        # path already requires this; we mirror it).
        try:
            pcb_obj = PCB.load(pcb_path)
            grow_board_outline_corner_anchored(
                pcb_obj,
                new_width_mm=next_tier.max_width_mm,
                new_height_mm=next_tier.max_height_mm,
            )
            pcb_obj.save(pcb_path)
        except OutlineGrowError as exc:
            if not quiet:
                flush_print(
                    f"Error: --auto-pcb-size: outline grow failed: {exc}",
                    file=sys.stderr,
                )
            return inner_rc

        current_tier_index = next_index

    # Loop exhausted without convergence -- conservative fallback.
    return last_exit_code


def _load_project_kct_for_escalation(pcb_path: Path, args) -> None:
    """Load EscalationPolicy / envelope_hard / mounting hole group from project.kct.

    Issue #3352 (P_AS3): when ``--auto-pcb-size`` is engaged, best-effort
    discover a ``project.kct`` file next to the PCB (in the PCB's directory
    or any parent up to the cwd) and forward its
    :attr:`ManufacturingRequirements.escalation` policy,
    :attr:`MechanicalRequirements.envelope_hard` flag, and
    :attr:`MechanicalRequirements.mounting_hole_group` to
    :func:`route_with_size_escalation`.

    The helper is silent on missing fields -- absent
    ``ManufacturingRequirements.escalation`` means "use the default
    EscalationPolicy"; absent ``MechanicalRequirements`` means "envelope is
    soft, no mounting hole group".

    Args:
        pcb_path: Path to the input PCB file (the discovery anchor).
        args: Parsed CLI args; will be mutated with
            ``_escalation_policy``, ``_envelope_hard``, ``_hole_group``.
    """
    from kicad_tools.pcb.mounting_holes import MountingHoleGroup

    # Search upward from the PCB's directory for a project.kct file.
    pcb_dir = pcb_path.parent if pcb_path.parent != Path("") else Path.cwd()
    candidate_paths: list[Path] = []
    cur = pcb_dir.resolve()
    for _ in range(6):  # bounded ancestor walk
        candidate_paths.append(cur / "project.kct")
        if cur.parent == cur:
            break
        cur = cur.parent

    spec_path: Path | None = None
    for p in candidate_paths:
        if p.is_file():
            spec_path = p
            break

    if spec_path is None:
        return  # No project.kct discovered; size escalation uses defaults.

    try:
        from kicad_tools.spec.parser import load_spec

        spec = load_spec(spec_path)
    except Exception:
        # Be permissive: a malformed or partially-valid spec must not
        # block routing.  Leave args._escalation_policy unset so the
        # default EscalationPolicy is used.
        return

    requirements = getattr(spec, "requirements", None)
    if requirements is None:
        return

    # ManufacturingRequirements.escalation -> args._escalation_policy
    manufacturing = getattr(requirements, "manufacturing", None)
    if manufacturing is not None:
        escalation = getattr(manufacturing, "escalation", None)
        if escalation is not None:
            args._escalation_policy = escalation

    # MechanicalRequirements.envelope_hard -> args._envelope_hard
    # MechanicalRequirements.mounting_hole_group -> args._hole_group
    mechanical = getattr(requirements, "mechanical", None)
    if mechanical is not None:
        args._envelope_hard = bool(getattr(mechanical, "envelope_hard", False))
        hole_spec = getattr(mechanical, "mounting_hole_group", None)
        if hole_spec is not None:
            # P_AS4: normalise the spec's anchor against the PCB outline's
            # origin so the fits_in_envelope check (which assumes a
            # 0-relative envelope) is consistent regardless of where the
            # board outline lives in absolute coordinates.  KiCad's default
            # outline origin is (100, 100); without normalisation a hole
            # group declared at (5, 5) board-coords would fail the fit
            # check against a 100x100 envelope (because 5+keepout > 100
            # is false but 5+keepout < 100 is false too -- the math is
            # just inconsistent).
            envelope_origin: tuple[float, float] | None = None
            try:
                from kicad_tools.router.io import extract_board_origin

                envelope_origin = extract_board_origin(pcb_path)
            except Exception:
                # Best-effort: if origin can't be discovered, fall back to
                # the 0-relative interpretation (spec.anchor is already
                # envelope-local).
                envelope_origin = None
            try:
                args._hole_group = MountingHoleGroup.from_spec(
                    hole_spec, envelope_origin=envelope_origin
                )
            except (ValueError, TypeError):
                # Defensive: invalid spec content; treat as no hole group.
                args._hole_group = None


def _resolve_starting_layers(pcb_path: Path, args) -> None:
    """Resolve ``args.starting_layers`` from CLI > project.kct > default 2.

    Issue #3400: ``EscalationPolicy.starting_layers`` lets boards opt out
    of the 2L tax.  The CLI flag ``--starting-layers {2,4,6}`` takes
    precedence over the spec field, and both override the default of 2.

    After this helper returns, ``args.starting_layers`` is guaranteed to
    be a concrete int in ``{2, 4, 6}``; the caller validates it against
    ``args.max_layers``.

    Args:
        pcb_path: Path to the input ``.kicad_pcb`` file (the spec
            discovery anchor; the helper walks ancestors looking for a
            ``project.kct`` sibling).
        args: Parsed CLI args; ``args.starting_layers`` may be ``None``
            (CLI flag was not supplied).  Mutated in place with the
            resolved value.
    """
    # If the CLI flag was supplied (parser stores the int), use it as-is.
    cli_value = getattr(args, "starting_layers", None)
    if cli_value is not None:
        return

    # Otherwise, look for a project.kct sibling and consume the spec
    # value when present.  Discovery mirrors the bounded ancestor walk
    # used by ``_load_project_kct_for_escalation`` (Issue #3352).
    spec_value: int | None = None
    pcb_dir = pcb_path.parent if pcb_path.parent != Path("") else Path.cwd()
    candidate_paths: list[Path] = []
    cur = pcb_dir.resolve()
    for _ in range(6):  # bounded ancestor walk
        candidate_paths.append(cur / "project.kct")
        if cur.parent == cur:
            break
        cur = cur.parent

    spec_path: Path | None = None
    for p in candidate_paths:
        if p.is_file():
            spec_path = p
            break

    if spec_path is not None:
        try:
            from kicad_tools.spec.parser import load_spec

            spec = load_spec(spec_path)
            requirements = getattr(spec, "requirements", None)
            if requirements is not None:
                manufacturing = getattr(requirements, "manufacturing", None)
                if manufacturing is not None:
                    escalation = getattr(manufacturing, "escalation", None)
                    if escalation is not None:
                        spec_value = int(escalation.starting_layers)
        except Exception:
            # Be permissive: a malformed spec must not block routing.
            spec_value = None

    args.starting_layers = spec_value if spec_value is not None else 2


def _print_size_escalation_refusal(
    reason: str,
    current_tier_index: int,
    ladder: list,
    cur_dims: tuple[float, float],
    hole_group=None,
) -> None:
    """Print the actionable refusal message for auto-pcb-size escalation.

    Mirrors the architect proposal's §4 refusal UX: enumerate concrete
    alternative levers (BOM / layers / envelope / clearance / spec
    amendment) so the user has a clear path forward rather than just a
    "refused" sentinel.
    """
    from kicad_tools.cli.progress import flush_print

    cur_tier = ladder[current_tier_index]
    cur_w, cur_h = cur_dims

    flush_print()
    flush_print("=" * 60)
    flush_print("AUTO-PCB-SIZE ESCALATION REFUSED")
    flush_print("=" * 60)

    if reason == "envelope_hard":
        flush_print(
            f"Reason: recipe declares envelope_hard=true (current envelope "
            f"{cur_w:g}x{cur_h:g} mm is a non-negotiable mechanical "
            f"constraint).  Auto-pcb-size cannot grow the board."
        )
    elif reason == "max_tier":
        flush_print(
            f"Reason: ladder exhausted at tier [{current_tier_index}] "
            f"({cur_tier.max_width_mm:g}x{cur_tier.max_height_mm:g} mm) -- "
            f"no further size escalation is registered for this manufacturer."
        )
    elif reason == "holes_dont_fit":
        if hole_group is not None:
            anchor = getattr(hole_group, "anchor", (0.0, 0.0))
            flush_print(
                f"Reason: mounting hole group at anchor ({anchor[0]:g}, "
                f"{anchor[1]:g}) would fall outside the next-tier envelope "
                f"at its declared position.  Auto-pcb-size cannot grow the "
                f"board without violating the mounting-hole constraint."
            )
        else:
            flush_print("Reason: mounting hole group doesn't fit in next tier.")
    elif reason == "regression":
        flush_print(
            f"Reason: size-tier escalation regressed -- growing the envelope "
            f"from a smaller tier to {cur_tier.max_width_mm:g}x"
            f"{cur_tier.max_height_mm:g} mm produced strictly worse routing "
            f"(see :func:`detect_regression_history` in auto_pcb_size).  "
            f"This indicates the bottleneck is not the envelope -- typically "
            f"placement quality or BOM density.  Further envelope growth "
            f"cannot help."
        )

    flush_print()
    flush_print("This board's BOM density x clearance x envelope is over-constrained.")
    flush_print("Consider these alternative levers (not all are mutually exclusive):")
    flush_print()
    flush_print(
        "  1. Reduce BOM density -- remove non-essential components or "
        "consolidate functionally-similar parts."
    )
    flush_print(
        "  2. Enable more layers -- pass --auto-layers --max-layers 6 "
        "(layers-first is cheapest at prototype quantities)."
    )
    flush_print(
        "  3. Enlarge mechanical envelope manually -- update the project "
        "spec dimensions and re-run.  If envelope_hard=true, change that "
        "declaration to false to allow size escalation."
    )
    flush_print(
        "  4. Loosen clearance via spec amendment -- lower --clearance or "
        "raise --fine-pitch-clearance (within manufacturer limits)."
    )
    flush_print(
        "  5. Upgrade manufacturer tier -- pass --auto-mfr-tier (e.g. "
        "jlcpcb -> jlcpcb-tier1 for via-in-pad capability)."
    )
    flush_print()


def _name_dominant_unfixable_constraint(
    last_router,
    manufacturer: str | None,
) -> str | None:
    """Compose the per-component diagnostic for tier-ladder exhaustion.

    Issue #2884: When ``--auto-mfr-tier`` walks the full ladder and still
    fails, surface a single human-readable line identifying *which*
    component (and, where available, which pin) carried the unfixable
    constraint -- using :func:`name_unfixable_constraint` from
    ``failure_analysis``.

    The dominant signal we have at this surface is the EscapeRouter's
    ``missed_via_in_pad_rescues`` counter and the companion
    ``missed_via_in_pad_components`` ref set.  A non-zero counter means
    one or more fine-pitch pins would have been rescued by an in-pad
    via -- the canonical PIN_ACCESS failure mode for the mfr-tier
    trigger table.

    Args:
        last_router: The Autorouter instance from the last inner attempt
            (may be ``None`` if no attempt completed cleanly).
        manufacturer: Manufacturer name at the time of the final failure.

    Returns:
        A one-line diagnostic string suitable for terminal output, or
        ``None`` when no signal is available (caller suppresses the
        Diagnosis section in that case).
    """
    from kicad_tools.router.failure_analysis import (
        FailureCause,
        name_unfixable_constraint,
    )

    if last_router is None:
        return None

    escape_router = getattr(last_router, "_escape_router", None)
    if escape_router is None:
        return None

    # Primary signal: missed via-in-pad rescues -> PIN_ACCESS failure mode.
    missed = getattr(escape_router, "missed_via_in_pad_rescues", 0) or 0
    try:
        missed_int = int(missed)
    except (TypeError, ValueError):
        missed_int = 0

    if missed_int <= 0:
        return None

    # Pick a representative component ref from the per-attempt set.
    # Sorted to keep the diagnostic deterministic across runs.
    refs = getattr(escape_router, "missed_via_in_pad_components", None) or set()
    try:
        ref_list = sorted(refs)
    except TypeError:
        ref_list = []
    component_ref = ref_list[0] if ref_list else None

    # Compose the canonical named-constraint line.  We do not yet have
    # per-pin attribution at this surface -- the EscapeRouter tracks
    # component refs only -- so we pass ``pin=None`` and let
    # name_unfixable_constraint fall back to component-level phrasing.
    base = name_unfixable_constraint(
        FailureCause.PIN_ACCESS,
        manufacturer=manufacturer,
        component_ref=component_ref,
        pin=None,
    )

    # When multiple components shared the same fault, mention the count
    # so users know whether they're chasing one outlier or a board-wide
    # problem.
    extras = len(ref_list) - 1
    if extras > 0:
        base = f"{base} ({extras} other component(s) affected by the same constraint.)"

    return base


def route_with_combined_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with combined layer and rule escalation (2D search).

    Implements a 2D search across both layer counts and design rule tiers
    to find the minimum viable configuration.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import flush_print, spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        ensure_cpp_backend_available,
        get_relaxation_tiers,
        load_pcb_for_routing,
        show_routing_summary,
    )

    # Handle backend selection (auto-build C++ extension on first use; #2549)
    ok, force_python, exit_code = ensure_cpp_backend_available(
        backend=args.backend,
        quiet=quiet,
        allow_auto_build=not getattr(args, "no_auto_build_native", False),
    )
    if not ok:
        return exit_code if exit_code is not None else 1

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Issue #2674: remove stale ``<stem>_<N>layer.kicad_pcb`` siblings
    # from a previous failed-2L run before routing begins.  Without this,
    # a successful 2L run leaves the prior failed 4L/6L artifact behind
    # and the output directory shows a confusing pair of routed PCBs.
    _cleanup_stale_layer_artifacts(output_path, quiet=quiet)

    # Auto-create copper pours for power nets (before skip detection).
    # auto_pour_if_missing writes in-place; stage a copy at output_path
    # first so the user's INPUT is left untouched (issue #2548).
    # Issue #3092: forward user-supplied skip_nets as force_pour_nets (see
    # the layer-escalation site above for the rationale).
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _stage_input_for_auto_pour(pcb_path, output_path)
        auto_pour_if_missing(
            pcb_path,
            quiet=quiet,
            edge_clearance=getattr(args, "edge_clearance", None),
            force_pour_nets=skip_nets,
        )

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=quiet)

    # Get relaxation tiers
    tiers = get_relaxation_tiers(
        initial_trace_width=args.trace_width,
        initial_clearance=args.clearance,
        initial_via_drill=args.via_drill,
        initial_via_diameter=args.via_diameter,
        manufacturer=args.manufacturer,
        min_trace_floor=args.min_trace,
        min_clearance_floor=args.min_clearance_floor,
    )

    # Issue #3155: capture preserved copper once before routing/checkpoints.
    _preserve = bool(getattr(args, "preserve_existing", False))
    _preserved_routes = _capture_preserved_routes(pcb_path) if _preserve else []
    _preserved_sexp = _serialize_preserved_routes(_preserved_routes) if _preserve else ""

    # Layer stacks to try (in escalation order)
    layer_configs = [
        (2, LayerStack.two_layer()),
        (4, LayerStack.four_layer_sig_gnd_pwr_sig()),
        (4, LayerStack.four_layer_all_signal()),
        (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
    ]

    # Issue #2916: filter and reorder by the PCB's declared stackup.
    # Drops entries below the detected copper count (so a 4L board never
    # wastes budget on a 2L probe) and promotes the plane-aware 4L variant
    # ahead of all-signal when inner plane zones exist.
    # Issue #3400: also honour ``--starting-layers``.
    layer_configs = _filter_layer_configs_for_pcb(
        layer_configs,
        pcb_path,
        args.max_layers,
        quiet=quiet,
        starting_layers=int(getattr(args, "starting_layers", None) or 2),
    )

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Combined Escalation Mode")
        flush_print("=" * 60)
        flush_print(f"Input:          {pcb_path}")
        flush_print(f"Output:         {output_path}")
        flush_print(f"Strategy:       {args.strategy}")
        flush_print(f"Manufacturer:   {args.manufacturer}")
        flush_print(f"Max layers:     {args.max_layers}")
        flush_print(f"Min completion: {args.min_completion * 100:.0f}%")
        flush_print(f"Rule tiers:     {len(tiers)}")
        flush_print(f"Layer configs:  {[n for n, _ in layer_configs]}")
        if skip_nets:
            flush_print(f"Skip:           {', '.join(skip_nets)}")
        flush_print()
        flush_print("Search matrix:")
        flush_print("         ", end="")
        for n, _ in layer_configs:
            flush_print(f" {n}L    ", end="")
        flush_print()

    best_result: RuleRelaxationResult | None = None
    successful_result: RuleRelaxationResult | None = None
    results_matrix: dict[tuple[int, int], float] = {}  # (tier, layers) -> completion

    # Register signal handlers so SIGTERM/SIGINT save the best attempt so far
    _interrupt_state["output_path"] = output_path
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["router"] = None
    _interrupt_state["interrupted"] = False
    _interrupt_state["best_completed_attempt"] = False
    prev_sigint = signal.signal(signal.SIGINT, _handle_interrupt)
    prev_sigterm = signal.signal(signal.SIGTERM, _handle_interrupt)

    # Issue #3051: Build the checkpoint callback ONCE before the 2D
    # combined-escalation matrix so every ``route_all_negotiated`` cell
    # gets best-so-far persistence (closes the iteration-0 kill-loses-
    # work hole observed in the curator audit).
    _checkpoint_cb = _make_checkpoint_callback(
        pcb_path,
        output_path,
        float(getattr(args, "checkpoint_interval", 30.0) or 0.0),
        quiet=quiet,
        preserved_sexp=_preserved_sexp,
    )

    # 2D search: prioritize fewer layers first, then stricter rules.
    # Issue #2823: precompute total cell count so per-attempt budget can
    # divide the remaining wall-clock budget fairly across the entire 2D
    # matrix (not just within one layer column).
    _combined_max_attempts = max(1, len(layer_configs) * len(tiers))
    # Issue #3241: cross-layer regression tracking.  The combined-escalation
    # 2D search already has an intra-layer regression guard at line ~4770
    # (skip remaining tiers when a tier underperforms the layer's best so
    # far).  But the cross-layer case ("4L wins, 6L loses") is structurally
    # identical to the layer-escalation ladder regression filed as #3241,
    # and the same fix applies: when the best completion at layer N+1 drops
    # below layer N's best, exit before starting layer N+2.  Best-result
    # tracking already uses the #2396 ``_is_better_result`` rule so this
    # break preserves the pre-regression winner.
    prev_layer_best_completion: float | None = None
    layer_regression_streak: int = 0
    CL_REGRESSION_TOLERANCE = 0.02  # 2 percentage points (cross-layer noise)
    CL_HARD_DROP = 0.10  # 10 percentage points triggers immediate exit
    CL_CONSECUTIVE = 2

    for _layer_idx, (layer_count, layer_stack) in enumerate(layer_configs):
        # Issue #2802: honor the total wall-clock deadline before starting
        # another layer-stack column of the 2D search.
        if _deadline_expired(args):
            if not quiet:
                flush_print(
                    f"  Wall-clock deadline reached before {layer_count}L column; "
                    "stopping combined escalation (issue #2802)"
                )
            break

        best_completion_for_layer: float | None = None
        for _tier_idx, tier in enumerate(tiers):
            # Issue #2802: honor the deadline before each tier within the
            # current layer column.
            if _deadline_expired(args):
                if not quiet:
                    flush_print(
                        f"  Wall-clock deadline reached before {layer_count}L "
                        f"tier {tier.tier}; stopping combined escalation "
                        "(issue #2802)"
                    )
                break

            # Issue #2823: linear attempt index across the 2D matrix
            # (row-major: layers outer, tiers inner) so the per-attempt
            # budget divides the remaining wall-clock budget across all
            # remaining cells, not just the cells in this column.
            _combined_attempt_index = _layer_idx * len(tiers) + _tier_idx

            if not quiet:
                flush_print(
                    f"\nTrying: {layer_count} layers, tier {tier.tier} "
                    f"(trace={tier.trace_width:.2f}mm, clearance={tier.clearance:.2f}mm)"
                )

            # Configure design rules for this tier
            fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
            rules = DesignRules(
                grid_resolution=args.grid,
                trace_width=tier.trace_width,
                trace_clearance=tier.clearance,
                via_drill=tier.via_drill,
                via_diameter=tier.via_diameter,
                fine_pitch_clearance=fine_pitch_cl,
                # Issue #2695: forward manufacturer so the escape router
                # can opt in to in-pad escape for fine-pitch LQFP/QFP/SSOP/
                # TSSOP when the manufacturer supports via-in-pad processing.
                manufacturer=getattr(args, "manufacturer", None),
                # Issue #2891: forward escalation-in-progress flag.
                auto_mfr_tier_in_progress=getattr(args, "_auto_mfr_tier_in_progress", False),
            )

            # Load PCB
            try:
                with spinner(f"Loading PCB ({layer_count}L, tier {tier.tier})...", quiet=quiet):
                    router, net_map = load_pcb_for_routing(
                        str(pcb_path),
                        skip_nets=skip_nets,
                        rules=rules,
                        edge_clearance=args.edge_clearance,
                        layer_stack=layer_stack,
                        force_python=force_python,
                        # Issue #4268: thread the mesh-router strategy selector through.
                        strategy=getattr(args, "route_engine", "grid"),
                        validate_drc=not args.force,
                        strict_drc=False,
                        # Issue #3155: incremental routing (see route_with_layer_escalation).
                        load_existing_routes=getattr(args, "preserve_existing", False),
                        # Issue #4148: region-bounded routing (see main()).
                        region=getattr(args, "_region_box", None),
                        # Issue #4170 (Phase 2b-1): board-relative boundary stub
                        # terminals whose tip cells are carved open as same-net
                        # reconnection targets (None when no --region / no stubs).
                        stub_terminals=getattr(args, "_stub_terminals", None),
                    )
            except Exception as e:
                if not quiet:
                    print(f"  Error loading PCB: {e}")
                results_matrix[(tier.tier, layer_count)] = 0.0
                continue

            # Issue #2996: merge --net-class-map sidecar onto router's map.
            _apply_net_class_map_sidecar(router, args, quiet=quiet)
            # Issue #3470: thread --max-ripups-per-net into the destructive
            # rip-up budgets (route_all + two-phase stall recovery).
            _apply_ripup_budget_override(router, args)
            _apply_rescue_pass_override(router, args)
            _apply_bundle_river_planner(router, args)
            _apply_monotone_certificate_order(router, args)
            _apply_cross_package_pair_corridor(router, args)
            _apply_slack_corridor_widening(router, args)

            # Issue #3171: inject boosted analog routing class for --analog-nets
            # / --auto-analog selected nets (pour/ground nets left untouched).
            _apply_analog_net_class(router, args, quiet=quiet)

            # Issue #3371 (P_FP3): surface fine-pitch escape regions.
            _log_fine_pitch_escape_regions(router, quiet=quiet)

            # Issue #1841: Tell the autorouter which pour nets lack zones
            router._pour_nets_without_zones = set(_no_zone)

            # Count nets to route.  Issue #3942 (Bug B): exclude pour-served
            # multi-pad nets from the denominator (see _routable_multi_pad_nets).
            multi_pad_nets = _routable_multi_pad_nets(router)
            nets_to_route = len(multi_pad_nets)

            # Route
            escape_flag = _resolve_escape_routing_flag(args)

            # Issue #2823: divide the remaining wall-clock budget fairly
            # across all remaining cells of the 2D combined-escalation
            # matrix so later (higher-layer or stricter-rule) attempts
            # also get a real chance to run.  Without this, the first
            # cell (2L, tier 0) greedily consumes the entire ``--timeout``
            # and the rest of the matrix is starved.
            # Falls back to ``args.timeout`` when no deadline is configured.
            _attempt_timeout = _per_attempt_budgeted_timeout(
                args,
                attempt_index=_combined_attempt_index,
                max_attempts=_combined_max_attempts,
            )

            try:
                if _should_use_escape_routing(router, escape_flag, quiet):
                    # Issue #3952: compose escape + diff-pair pre-pass when
                    # --differential-pairs is requested (combined 2D
                    # escalation path); otherwise unchanged escape path.
                    _dp_cfg = _build_diffpair_config(args)
                    if _dp_cfg is not None:
                        router.route_with_escape_and_diffpairs(
                            _dp_cfg,
                            use_negotiated=(args.strategy == "negotiated"),
                            timeout=_attempt_timeout,
                            per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                        )
                    else:
                        router.route_with_escape(
                            use_negotiated=(args.strategy == "negotiated"),
                            timeout=_attempt_timeout,
                            per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                        )
                elif getattr(args, "multi_resolution", False):
                    router.route_all_multi_resolution(
                        use_negotiated=(args.strategy == "negotiated"),
                        max_iterations=args.iterations,
                        timeout=_attempt_timeout,
                    )
                elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                    router.route_all_two_phase(
                        use_negotiated=True,
                        corridor_width_factor=2.0,
                        timeout=_attempt_timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                        max_iterations=getattr(args, "two_phase_iterations", None)
                        or args.iterations,
                    )
                elif args.strategy == "negotiated":
                    router.route_all_negotiated(
                        max_iterations=args.iterations,
                        timeout=_attempt_timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                        batch_routing=getattr(args, "batch_routing", False)
                        or getattr(args, "high_performance", False),
                        hierarchical=getattr(args, "hierarchical", False),
                        perturbation=getattr(args, "perturbation", True),
                        # Issue #3039: forward --seed for deterministic routing.
                        seed=getattr(args, "seed", None),
                        # Issue #3054 (Phase 2 of #3045): forward region-based
                        # parallelism opt-in.  Defaults preserve single-threaded
                        # behaviour bit-for-bit.
                        region_parallel=getattr(args, "region_parallel", False),
                        partition_rows=getattr(args, "partition_rows", 2),
                        partition_cols=getattr(args, "partition_cols", 2),
                        max_parallel_workers=getattr(args, "max_parallel_workers", 4),
                        # Issue #3051: forward checkpoint callback so kills
                        # mid-loop persist the best-so-far snapshot.
                        checkpoint_callback=_checkpoint_cb,
                        # Issue #3438 / #3414: forward --targeted-ripup so the
                        # pre-existing targeted rip-up path in
                        # route_all_negotiated is CLI-reachable.
                        use_targeted_ripup=getattr(args, "targeted_ripup", False),
                        max_ripups_per_net=_targeted_ripup_budget(args),
                        # Issue #3101: best-metric early-stop patience.  0
                        # disables (matches pre-#3101 behaviour).
                        best_stall_patience=(getattr(args, "early_stop_patience", 2) or None),
                    )
                elif args.strategy == "basic":
                    router.route_all()
                elif args.strategy == "monte-carlo":
                    router.route_all_monte_carlo(
                        num_trials=args.mc_trials,
                        verbose=args.verbose and not quiet,
                    )
                elif args.strategy == "evolutionary":
                    router.route_all_evolutionary(
                        pop_size=args.pop_size,
                        generations=args.generations,
                        verbose=args.verbose and not quiet,
                        timeout=_attempt_timeout,
                    )
            except Exception as e:
                if not quiet:
                    print(f"  Routing error: {e}")
                results_matrix[(tier.tier, layer_count)] = 0.0
                continue

            # Issue #2426: Run cleanup before computing statistics so that
            # the best-result selector compares post-cleanup connectivity.
            router.cleanup_artifacts()

            # Calculate completion — filter to multi-pad nets only (Issue #1643)
            multi_pad_net_ids = set(multi_pad_nets)
            stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)
            nets_routed = stats["nets_routed"]
            completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0
            results_matrix[(tier.tier, layer_count)] = completion

            if not quiet:
                flush_print(f"  Routed: {nets_routed}/{nets_to_route} ({completion * 100:.0f}%)")

            # Create result
            result = RuleRelaxationResult(
                tier=tier.tier,
                trace_width=tier.trace_width,
                clearance=tier.clearance,
                via_drill=tier.via_drill,
                via_diameter=tier.via_diameter,
                tier_description=tier.description,
                router=router,
                net_map=net_map,
                nets_routed=nets_routed,
                nets_to_route=nets_to_route,
                completion=completion,
                success=completion >= args.min_completion,
                layer_count=layer_count,
                stats=stats,
            )

            # Track best result (Issue #2396: absolute nets_routed comparison)
            if best_result is None or _is_better_result(result, best_result):
                best_result = result
                # Update interrupt state so signal handler saves the best attempt
                _interrupt_state["router"] = result.router
                _interrupt_state["best_completed_attempt"] = True

            # Track best completion for this layer config
            if best_completion_for_layer is None or completion > best_completion_for_layer:
                best_completion_for_layer = completion

            # Check for success (first success wins - minimum config)
            if result.success:
                successful_result = result
                break

            # Early termination: skip remaining tiers when completion regresses
            # within this layer config
            if not getattr(args, "no_early_stop", False):
                if best_completion_for_layer is not None and completion < best_completion_for_layer:
                    if not quiet:
                        flush_print(
                            f"\n  Early stop: {layer_count}L tier {tier.tier} "
                            f"completion ({completion * 100:.0f}%) is worse than "
                            f"best for {layer_count}L "
                            f"({best_completion_for_layer * 100:.0f}%) — "
                            f"skipping remaining tiers for {layer_count}L"
                        )
                    break

        # If we found a successful config at this layer count, stop
        if successful_result:
            break

        # Issue #3241: cross-layer monotonic-regression check.  If this
        # column's best completion regressed materially vs the prior
        # column's best, abort the ladder.  Mirrors the same logic used
        # by ``route_with_layer_escalation`` but expressed in fractional
        # completion rather than absolute nets_routed since tier-relaxed
        # routes can produce a slightly different net-set per cell.
        if (
            prev_layer_best_completion is not None
            and best_completion_for_layer is not None
            and not getattr(args, "no_early_stop", False)
        ):
            drop = prev_layer_best_completion - best_completion_for_layer
            if drop >= CL_HARD_DROP:
                if not quiet:
                    flush_print(
                        f"\n  Escalation stopped: {layer_count}L best "
                        f"({best_completion_for_layer * 100:.0f}%) regressed "
                        f"by {drop * 100:.0f}pp vs previous layer "
                        f"({prev_layer_best_completion * 100:.0f}%); "
                        "hard cross-layer drop (issue #3241)"
                    )
                break
            elif drop > CL_REGRESSION_TOLERANCE:
                layer_regression_streak += 1
                if layer_regression_streak >= CL_CONSECUTIVE:
                    if not quiet:
                        flush_print(
                            f"\n  Escalation stopped: {layer_regression_streak} "
                            "consecutive cross-layer regressions "
                            f"(latest {drop * 100:.0f}pp drop, issue #3241)"
                        )
                    break
            else:
                layer_regression_streak = 0
        # Track best across layers for cross-layer comparison.
        if best_completion_for_layer is not None:
            if (
                prev_layer_best_completion is None
                or best_completion_for_layer > prev_layer_best_completion
            ):
                prev_layer_best_completion = best_completion_for_layer

    # Restore original signal handlers
    signal.signal(signal.SIGINT, prev_sigint)
    signal.signal(signal.SIGTERM, prev_sigterm)
    _interrupt_state["best_completed_attempt"] = False

    # Print results matrix
    if not quiet:
        print("\n" + "=" * 60)
        print("SEARCH MATRIX RESULTS")
        print("=" * 60)
        print("         ", end="")
        for n, _ in layer_configs:
            print(f" {n}L     ", end="")
        print()
        for tier in tiers:
            print(f"Tier {tier.tier}:  ", end="")
            for n, _ in layer_configs:
                comp = results_matrix.get((tier.tier, n), 0.0)
                if comp >= args.min_completion:
                    print(f" {comp * 100:3.0f}%✓  ", end="")
                else:
                    print(f" {comp * 100:3.0f}%   ", end="")
            print()

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("COMBINED ESCALATION SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Minimum viable configuration found\n"
                f"  Layers: {final_result.layer_count}\n"
                f"  Tier: {final_result.tier} ({final_result.tier_description})\n"
                f"  Completion: {final_result.completion * 100:.0f}%"
            )
            print("\nFinal design rules:")
            print(f"  Trace width: {final_result.trace_width:.3f}mm")
            print(f"  Clearance:   {final_result.clearance:.3f}mm")
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result at {final_result.layer_count} layers, "
                f"tier {final_result.tier} ({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"in any configuration"
            )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
        return 1

    # Issue #4151: engage placement-routing feedback when --placement-feedback
    # is set and the combined layer x rule-tier search stalled with unrouted
    # nets.  Before this hook, --placement-feedback was silently dropped on
    # the combined-escalation dispatch path (--auto-layers --adaptive-rules
    # together).
    _maybe_run_placement_feedback_escalation(
        final_result,
        successful_result,
        pcb_path,
        args,
        quiet,
        stall_label="combined escalation",
    )

    # Check if at manufacturer minimum
    from kicad_tools.router import get_mfr_limits

    mfr = get_mfr_limits(args.manufacturer)
    at_minimum = (
        final_result.trace_width <= mfr.min_trace + 0.001
        and final_result.clearance <= mfr.min_clearance + 0.001
    )
    if at_minimum and not quiet:
        print(f"\nWARNING: Design uses {args.manufacturer.upper()} minimum tolerances.")
        print("Consider redesigning placement for more margin.")

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
            optimize_routes_grid_synced,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.
        has_overflow = final_result.router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(
            final_result.router.grid, ignore_overflow=has_overflow
        )
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        # Issue #2596: snapshot per-net connectivity before optimize.
        _ci_snapshot = _connectivity_snapshot(final_result.router)

        with spinner("Optimizing traces...", quiet=quiet):
            # Issue #3507: grid-transactional optimize (see
            # optimize_routes_grid_synced).
            optimize_routes_grid_synced(final_result.router, optimizer)

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot,
            phase="optimize",
            args=args,
            quiet=quiet,
        )

    # Post-optimization DRC nudge pass
    if final_result.router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        # Issue #2596: snapshot connectivity before nudge.
        _ci_snapshot_nudge = _connectivity_snapshot(final_result.router)

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(final_result.router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot_nudge,
            phase="nudge",
            args=args,
            quiet=quiet,
        )

        # Issue #4208 (Unit 3): re-run the Unit-2 seg-seg finalize gate
        # over the post-optimize/post-nudge copper.  An rtree-less
        # optimizer can introduce a cross-net crossing the pre-optimize
        # finalize gate never saw; demote it before the canonical write.
        _finalize_committed_copper_or_demote(final_result.router, quiet=quiet)

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    _final_multi_pad_ids = {n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2}
    route_sexp, final_stats, _cleanup_stats = _finalize_routes(
        final_result.router,
        _final_multi_pad_ids,
        final_result.nets_to_route,
        quiet=quiet,
        strict=bool(getattr(args, "strict", False)),
        verbose=bool(getattr(args, "verbose", False)),
        preserve_existing=bool(getattr(args, "preserve_existing", False)),
        preserved_routes=_preserved_routes,
    )
    # Update result with post-cleanup stats
    final_result.nets_routed = final_stats["nets_routed"]
    final_result.completion = (
        final_result.nets_routed / final_result.nets_to_route
        if final_result.nets_to_route > 0
        else 1.0
    )
    final_result.success = final_result.completion >= args.min_completion

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        if not route_sexp and not quiet:
            print("  Warning: No routes generated!")
        # Issue #2808: atomic write via _write_routed_pcb.
        # Issue #2809: honor --output exactly; do NOT append _Nlayer suffix.
        _write_routed_pcb(
            pcb_path,
            output_path,
            route_sexp,
            layer_count=final_result.layer_count,
        )

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Layer count: {final_result.layer_count}")
        print(f"  Final trace width: {final_result.trace_width:.3f}mm")
        print(f"  Final clearance: {final_result.clearance:.3f}mm")

    # Fill copper-pour zones now that traces exist (issue #2516).
    if final_result.nets_routed > 0:
        _fill_zones_after_route(output_path, quiet=quiet)

    # Run DRC validation unless skipped
    fix_result: int | None = None
    if not args.skip_drc and final_result.nets_routed > 0:
        drc_errors, _ = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
            # Issue #2652, Epic #2556 Phase 2.5b: thread the autorouter's
            # net_class_map into the post-route DRC so the diff-pair
            # routing-continuity rule can re-derive its engagement state.
            net_class_map=getattr(final_result.router, "net_class_map", None),
            # Issue #4178: forward --strict-drc so a native DRC that did
            # not run becomes a hard failure instead of a soft NOTE.
            strict_drc=getattr(args, "strict_drc", False),
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            fix_result = _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
                args=args,  # Issue #2802: honor total wall-clock deadline
            )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print(
                f"SUCCESS: Minimum viable config = {final_result.layer_count} layers + "
                f"tier {final_result.tier} rules"
            )
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"at {final_result.layer_count} layers, tier {final_result.tier}"
            )
            _multi_pad_ids = {
                n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
            }
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
                current_strategy=args.strategy,
                pcb_file=args.pcb,
                nets_to_route_ids=_multi_pad_ids,
                single_pad_count=getattr(final_result, "single_pad_count", 0),
                # Issue #2634: combined escalation includes auto-layer escalation
                # as one of its axes; suppress the redundant "Try --auto-layers"
                # recommendation.
                auto_layers_attempted=True,
            )

    if final_result.success:
        # Issue #3238: propagate auto-fix-skipped-by-deadline.
        if getattr(args, "_auto_fix_status", None) == "skipped_deadline":
            return 7
        # Issue #2852: propagate --auto-fix rollback (exit 3) so callers can
        # detect a silent rollback on an otherwise-clean routing run.
        if fix_result == 3:
            return 3
        return 0
    # Partial routing: some nets were routed but not all — pipeline should continue
    if final_result.nets_routed > 0:
        return 2
    # Nothing was routed — treat as fatal failure
    return 1


def _resolve_escape_routing_flag(args) -> bool | None:
    """Resolve --escape-routing / --no-escape-routing into a tri-state value.

    Returns:
        True if escape routing is explicitly enabled,
        False if explicitly disabled,
        None for auto-detect (default).
    """
    no_escape = getattr(args, "no_escape_routing", False)
    escape = getattr(args, "escape_routing", None)

    if no_escape:
        return False
    if escape:
        return True
    return None


def _validate_route_engine_strategy(args) -> int:
    """Hard compatibility gate for ``--route-engine mesh|lattice`` (#4280).

    The mesh/lattice engines dispatch through ``Autorouter.route_net`` --
    the ONLY seam that consults the engine selector (``core.py``
    ``route_net``).  Every other stock strategy/modifier bypasses that seam:

    - ``--strategy negotiated`` (the DEFAULT) goes ``route_all_negotiated``
      -> ``_route_net_negotiated``, which builds a grid ``NegotiatedRouter``
      directly and never calls ``route_net`` -- the engine flag is silently
      inert and the run ships grid copper labeled as mesh/lattice output.
    - ``--strategy monte-carlo`` / ``evolutionary`` reach the seam but the
      engines negotiate the whole netset ONCE and cache; per-trial resets
      never clear that cache, so every trial after the first is vacuous.
    - ``--two-phase`` and ``--multi-resolution`` wrap or bypass the seam.
    - Escape routing commits grid escape stubs the engines' whole-netset
      negotiation cannot see (mixed/incoherent copper).

    Rather than silently shipping grid copper (or silently swapping the
    user's negotiation algorithm), any such combination is rejected loudly
    BEFORE any board loading or output writing.  ``--strategy basic`` is
    the supported combination.

    Escape AUTO-detect (dense packages, no flag) is not an error -- the
    user didn't ask for it -- so it is forced off with a printed notice by
    stamping ``args.no_escape_routing``; every dispatch site resolves the
    tri-state via :func:`_resolve_escape_routing_flag`, so one stamp covers
    all of them.

    Returns 0 when the combination is valid (ALWAYS for ``--route-engine
    grid`` -- this gate is a strict no-op on the production default), or 2
    (usage error) with a message on stderr naming the remedy.
    """
    engine = getattr(args, "route_engine", "grid")
    if engine == "grid":
        return 0

    conflicts: list[str] = []
    if args.strategy != "basic":
        default_note = " (the default)" if args.strategy == "negotiated" else ""
        conflicts.append(f"--strategy {args.strategy}{default_note}")
    if getattr(args, "two_phase", False):
        conflicts.append("--two-phase")
    if getattr(args, "multi_resolution", False):
        conflicts.append("--multi-resolution")
    if _resolve_escape_routing_flag(args) is True:
        conflicts.append("--escape-routing")

    if conflicts:
        print(
            f"Error: --route-engine {engine} is incompatible with "
            f"{', '.join(conflicts)}.\n"
            f"The {engine} engine runs its own whole-netset negotiation and "
            "dispatches only through the basic per-net routing path; the "
            "negotiated/monte-carlo/evolutionary strategies and the "
            "--two-phase/--multi-resolution/--escape-routing modifiers "
            "bypass that path, so the engine selection would be silently "
            "ignored and grid copper shipped (issue #4280).\n"
            "Supported combination:\n"
            f"    kct route <board> --route-engine {engine} --strategy basic",
            file=sys.stderr,
        )
        return 2

    # engine != grid with --strategy basic: suppress escape auto-detect
    # (tri-state None -> forced off).  Explicit --no-escape-routing needs
    # no notice; explicit --escape-routing was rejected above.
    if _resolve_escape_routing_flag(args) is None:
        args.no_escape_routing = True
        if not getattr(args, "quiet", False):
            print(
                f"  Escape routing: auto-detect disabled (--route-engine {engine} "
                "performs whole-netset negotiation that cannot see grid escape stubs)"
            )
    return 0


def _build_diffpair_config(args):
    """Build a ``DifferentialPairConfig`` from parsed CLI args, or ``None``.

    Issue #3952: the escape / auto-layers-escalation dispatch sites need a
    ``diffpair_config`` to compose diff-pair routing with escape routing via
    ``Autorouter.route_with_escape_and_diffpairs``.  The ``do_routing()``
    fixed-layer path builds this inline (route_cmd.py); the escalation
    functions build their own routers and so need their own config.  This
    helper centralizes the construction so all four dispatch sites stay in
    sync with the flag surface (``--diffpair-spacing``, ``--diffpair-max-delta``,
    ``--diffpair-per-pair-timeout``).

    Returns ``None`` when ``--differential-pairs`` was not requested so callers
    can gate the composed path on a truthy result (the no-pair regression
    firewall -- boards without pairs take the byte-identical old path).
    """
    if not getattr(args, "differential_pairs", False):
        return None

    from kicad_tools.router import DifferentialPairConfig

    return DifferentialPairConfig(
        enabled=True,
        spacing=args.diffpair_spacing,
        max_length_delta=args.diffpair_max_delta,
        # Issue #3275: forward the optional per-pair wall-clock budget so the
        # CoupledPathfinder's per-pair coupled A* search can be bounded.
        per_pair_timeout=getattr(args, "diffpair_per_pair_timeout", None),
    )


def _should_use_escape_routing(router, escape_flag: bool | None, quiet: bool) -> bool:
    """Determine whether to use escape routing for the current board.

    Args:
        router: The Autorouter instance.
        escape_flag: True=force on, False=force off, None=auto-detect.
        quiet: Suppress progress output.

    Returns:
        True if escape routing should be used.
    """
    if escape_flag is True:
        if not quiet:
            print("  Escape routing: enabled (--escape-routing)")
        return True
    if escape_flag is False:
        return False

    # Auto-detect dense packages
    dense_packages = router.detect_dense_packages()
    if dense_packages:
        if not quiet:
            refs = [p.ref for p in dense_packages]
            print(f"  Escape routing: auto-enabled (dense packages: {refs})")
        return True
    return False


def _parse_region_box(
    region_arg: str,
) -> tuple[float, float, float, float] | str:
    """Parse a ``--region`` string into a normalized board-relative box.

    Issue #4148.  Mirrors the ``pcb strip --region`` CLI validation
    (``cli/commands/pcb.py``) so both commands accept exactly the same
    ``x1,y1,x2,y2`` strings and reject the same degenerate inputs.

    Returns the normalized ``(x1, y1, x2, y2)`` tuple (x1<x2, y1<y2) on
    success, or a human-readable error string on failure.
    """
    parts = [p.strip() for p in region_arg.split(",")]
    if len(parts) != 4:
        return f"--region expects 'x1,y1,x2,y2' (four comma-separated numbers), got {region_arg!r}"
    try:
        x1, y1, x2, y2 = (float(p) for p in parts)
    except ValueError:
        return f"--region values must be numeric, got {region_arg!r}"
    if x1 >= x2 or y1 >= y2:
        return f"--region must satisfy x1 < x2 and y1 < y2 (got x1={x1}, y1={y1}, x2={x2}, y2={y2})"
    return (x1, y1, x2, y2)


def _detect_region_stub_terminals(pcb, region_box: tuple[float, float, float, float], skip: set):
    """Run the shared boundary-stub detector against a loaded schema ``PCB``.

    Issue #4170 (Phase 2b-1).  Adapts the board-relative schema geometry
    (``pcb._segments`` / ``pcb._vias`` / footprint pads -- all rebased to
    ``pcb._board_origin``) into the pure detector's plain-data shapes and returns
    ``{net_id: [StubTerminal, ...]}`` in the SAME board-relative frame as
    ``region_box``.  ``load_pcb_for_routing`` adds the board origin when it
    carves the tips, so the whole pipeline stays in board-relative coordinates
    (matching ``pcb strip --region`` / ``route --region``).

    Both ``route`` (this function) and the future ``route-auto`` orchestrator
    (Phase 2c, #4173) call the SAME ``detect_boundary_stub_terminals`` producer,
    so stub geometry is never re-derived independently (the #3428 foot-gun).
    """
    from kicad_tools.core.types import CopperLayer
    from kicad_tools.router.stub_terminals import (
        PadLocation,
        RegionBox,
        StubSegment,
        detect_boundary_stub_terminals,
    )

    rx1, ry1, rx2, ry2 = region_box
    region = RegionBox(rx1, ry1, rx2, ry2)

    def _layer(name: str):
        try:
            return CopperLayer.from_kicad_name(name)
        except ValueError:
            return None

    seg_inputs: list[StubSegment] = []
    for seg in pcb._segments:
        if not seg.net_name or seg.net_name in skip:
            continue
        layer = _layer(seg.layer)
        if layer is None:
            continue
        seg_inputs.append(
            StubSegment(
                net_id=seg.net_number,
                net_name=seg.net_name,
                x1=seg.start[0],
                y1=seg.start[1],
                x2=seg.end[0],
                y2=seg.end[1],
                layer=layer,
                uuid=getattr(seg, "uuid", "") or None,
            )
        )

    pad_inputs: list[PadLocation] = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            net_name = getattr(pad, "net_name", "") or ""
            if not net_name:
                continue
            pos = pcb.get_pad_position(fp.reference, pad.number)
            if pos is None:
                continue
            pad_inputs.append(PadLocation(net_id=pad.net_number, x=pos[0], y=pos[1]))
    # Vias are coincidence-rejection reference points too (a via on the boundary
    # routes "for free" and must not be double-targeted as a bare stub).
    for via in pcb._vias:
        pad_inputs.append(PadLocation(net_id=via.net_number, x=via.position[0], y=via.position[1]))

    return detect_boundary_stub_terminals(seg_inputs, pad_inputs, region)


def _parse_and_apply_region(args, pcb_path: Path, region_arg: str) -> int:
    """Validate --region, stamp ``args._region_box``, and gate unreachable nets.

    Issue #4148 (region-bounded routing, Phase 2a).  On success returns 0 and
    sets ``args._region_box`` to the normalized board-relative box (plus forces
    ``args.preserve_existing = True`` so outside copper is preserved).  On any
    validation / reachability failure prints a clear message to stderr and
    returns a non-zero exit code so no routing budget is spent.

    Reachability rule: a net can only be routed inside the region when ALL of
    its pads lie within the box.  A net with a pad outside the region needs
    outside access we cannot provide in Phase 2a (bare-stub reconnection is
    deferred to Phase 2b), so it is reported per-net and the run fails fast.
    Pads / vias that coincide with the boundary work "for free" (inclusive
    box test).
    """
    parsed = _parse_region_box(region_arg)
    if isinstance(parsed, str):
        print(f"Error: {parsed}", file=sys.stderr)
        return 1

    x1, y1, x2, y2 = parsed
    args._region_box = (x1, y1, x2, y2)
    # Region mode implies preserve-existing so copper outside the box is loaded
    # as fixed obstacles and re-emitted unchanged.
    args.preserve_existing = True
    # Region mode implies --no-cache: the routing cache key
    # (``CacheKey.compute(pcb_content, rules, grid)``) does not incorporate the
    # region box or the derived skip-net set, so a full-board cached result
    # from a prior non-region run would otherwise be (incorrectly) reused and
    # re-route nets that must stay outside the region.  A region route is a
    # distinct operation; skip the cache entirely.
    args.no_cache = True

    quiet = getattr(args, "quiet", False)

    # Load the board (board-relative pad frame) to check bounds + reachability.
    try:
        from kicad_tools.schema.pcb import PCB as _SchemaPCB

        pcb = _SchemaPCB.load(str(pcb_path))
    except Exception as e:  # pragma: no cover - load errors surface later too
        print(f"Error loading PCB for --region validation: {e}", file=sys.stderr)
        return 1

    # A region entirely outside the board's outline is a documented no-op:
    # there is nothing to route inside it.  Report clearly rather than spend a
    # routing budget that can only fail.
    outline = pcb.get_board_outline()
    if outline:
        bx1 = min(p[0] for p in outline)
        by1 = min(p[1] for p in outline)
        bx2 = max(p[0] for p in outline)
        by2 = max(p[1] for p in outline)
        if x2 <= bx1 or x1 >= bx2 or y2 <= by1 or y1 >= by2:
            print(
                "Error: --region "
                f"({x1}, {y1})-({x2}, {y2}) is entirely outside the board "
                f"bounds ({bx1:.3f}, {by1:.3f})-({bx2:.3f}, {by2:.3f}); "
                "nothing to route.",
                file=sys.stderr,
            )
            return 1

    # Nets the user explicitly skipped are not candidates for reachability.
    skip = set()
    if getattr(args, "skip_nets", None):
        skip = {n.strip() for n in args.skip_nets.split(",")}

    # Collect board-relative pad positions per net.
    net_pads: dict[str, list[tuple[float, float]]] = {}
    for fp in pcb.footprints:
        ref = fp.reference
        for pad in fp.pads:
            net_name = getattr(pad, "net_name", "") or ""
            if not net_name or net_name in skip:
                continue
            pos = pcb.get_pad_position(ref, pad.number)
            if pos is None:
                continue
            net_pads.setdefault(net_name, []).append(pos)

    def _inside(px: float, py: float) -> bool:
        return x1 <= px <= x2 and y1 <= py <= y2

    # Issue #4170 (Phase 2b-1): run the shared boundary-stub detector so a net
    # with pad(s) outside the region can still be routed when a bare boundary
    # stub on the same net provides in-region reconnection access.  The detector
    # already applies the four-part spec (boundary endpoint, other-end-outside,
    # not pad-coincident, net straddles the boundary), so a returned terminal
    # means "this net has a reconnectable stub".
    try:
        stub_terminals = _detect_region_stub_terminals(pcb, (x1, y1, x2, y2), skip)
    except Exception as e:  # pragma: no cover - detector should not raise
        print(f"Error detecting boundary stub terminals for --region: {e}", file=sys.stderr)
        return 1
    # Map net_id -> net_name so we can key stub reachability by name.
    stub_net_names: set[str] = set()
    for terminals in stub_terminals.values():
        for term in terminals:
            stub_net_names.add(term.net_name)
    # Stash for the load_pcb_for_routing call sites to forward (board-relative).
    args._stub_terminals = stub_terminals or None

    # A net is routable in-region only if it has >= 2 pads AND every pad is
    # inside the box.  Single-pad nets have nothing to route.  Nets with zero
    # in-region pads are simply not this region's concern (ignored).  Nets
    # with SOME pads inside and SOME outside need outside access; Phase 2b-1
    # reconnects them when a same-net boundary stub exists, else they fail.
    unreachable: list[str] = []
    routable_nets: list[str] = []
    skip_outside: list[str] = []
    for net_name, positions in net_pads.items():
        inside = [p for p in positions if _inside(p[0], p[1])]
        if not inside:
            # Net lives entirely outside this region -- do NOT route it (its
            # pads sit in region-blocked cells).  Skip it so its existing
            # copper is preserved untouched rather than re-routed.
            skip_outside.append(net_name)
            continue
        if len(inside) != len(positions):
            # Pad(s) outside the region.  Reachable in Phase 2b-1 iff a same-net
            # boundary stub gives in-region reconnection access.
            if net_name in stub_net_names:
                routable_nets.append(net_name)
            else:
                unreachable.append(net_name)
        elif len(positions) >= 2:
            routable_nets.append(net_name)
        elif net_name in stub_net_names:
            # A single in-region pad plus a boundary stub is routable: the stub
            # tip is the second target.
            routable_nets.append(net_name)
        else:
            # Single in-region pad -- nothing to route; skip so it is left as-is.
            skip_outside.append(net_name)

    routable_in_region = len(routable_nets)

    if unreachable:
        preview = ", ".join(sorted(unreachable)[:10])
        more = "" if len(unreachable) <= 10 else f" (+{len(unreachable) - 10} more)"
        print(
            "Error: --region cannot route the following net(s) because they "
            "have pad(s) outside the region with no same-net boundary stub to "
            f"reconnect to: {preview}{more}",
            file=sys.stderr,
        )
        return 1

    if routable_in_region == 0:
        print(
            "Error: --region "
            f"({x1}, {y1})-({x2}, {y2}) contains no routable multi-pad net; "
            "nothing to route inside the region.",
            file=sys.stderr,
        )
        return 1

    # Auto-skip every net that has no routable work inside the region (fully
    # outside, or a single in-region pad).  Their pads sit in region-blocked
    # cells, so routing them would fail; skipping keeps their existing copper
    # untouched (region mode implies --preserve-existing).  Merge with any
    # user-supplied --skip-nets.
    if skip_outside:
        existing = (
            [n.strip() for n in args.skip_nets.split(",") if n.strip()]
            if getattr(args, "skip_nets", None)
            else []
        )
        merged = existing + [n for n in skip_outside if n not in existing]
        args.skip_nets = ",".join(merged)

    if not quiet:
        print(
            f"[region] Confining routing to board-relative box "
            f"({x1}, {y1})-({x2}, {y2}); {routable_in_region} net(s) routable "
            "inside (implies --preserve-existing)."
        )
    return 0


def _offboard_preflight(pcb_path: Path) -> int:
    """Abort routing when any footprint is placed outside the Edge.Cuts outline.

    Off-board placement (the whole board shifted N mm off the outline, or a
    stray footprint dropped outside it) makes routing pointless — the affected
    nets can never complete, and the resulting low completion percentage is
    indistinguishable from routing congestion.  This preflight surfaces the
    real cause up front (issue #4156).

    Returns 0 when the placement is on-board (or the board has no outline, or
    the check cannot run), and 2 (matching the blocking netlist-sync gate's
    exit convention) when off-board footprints are found.
    """
    try:
        from kicad_tools.placement import ConflictType, PlacementAnalyzer

        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts(pcb_path)
    except Exception:
        # A preflight that cannot run must never block routing outright.
        return 0

    offboard = [c for c in conflicts if c.type == ConflictType.OFF_BOARD]
    if not offboard:
        return 0

    refs = sorted({c.component1 for c in offboard})
    pad_count = sum(
        len(comp.pads) for comp in analyzer.get_components() if comp.reference in set(refs)
    )
    print(
        f"ERROR: {len(refs)} footprint(s) / {pad_count} pad(s) outside "
        "Edge.Cuts — placement invalid",
        file=sys.stderr,
    )
    print(f"       Off-board: {', '.join(refs)}", file=sys.stderr)
    print(
        "       Run `kct placement check` for details, or `--allow-offboard` to route anyway.",
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------------------
# Issue #4263: analytical --dry-run grid/cell/budget plan.
#
# A plain ``route --dry-run`` used to *construct* the full RoutingGrid (inside
# ``load_pcb_for_routing``) before it could report anything, so on a large
# board it ran >45s and got OOM-killed -- exactly when the user is trying to
# discover whether the board even fits the budget.  These helpers compute the
# same plan the user wants (selected grid, cell count, memory estimate, budget
# verdict, finer/coarser lattice candidates) using pure selection arithmetic
# (``auto_select_grid_resolution`` + ``estimate_memory_bytes``), allocating NO
# grid, so the answer returns in well under a second regardless of board size.
# ---------------------------------------------------------------------------


@dataclass
class DryRunGridCandidate:
    """One lattice-aligned candidate grid in the analytical dry-run plan.

    All fields are derived analytically -- no RoutingGrid is allocated.
    """

    resolution: float
    cols: int
    rows: int
    cells: int
    est_memory_bytes: int
    fits: bool
    off_grid_pads: int


@dataclass
class DryRunGridPlan:
    """Analytical grid/cell/budget plan for ``route --dry-run`` (issue #4263).

    Every field is computed from board dimensions + pad positions + the
    grid-selection arithmetic, without constructing a ``RoutingGrid``.
    """

    board_width: float
    board_height: float
    resolution: float
    num_layers: int
    cols: int
    rows: int
    cells: int
    est_memory_bytes: int
    max_cells: int
    fits: bool
    auto_resolution: float
    grid_explicit: bool
    memory_capped: bool
    memory_forced_unsafe_grid: bool
    candidates: list[DryRunGridCandidate]


def _grid_dims_for(width: float, height: float, resolution: float) -> tuple[int, int]:
    """Canonical grid dimensions for a board (``grid.py:739-740`` formula).

    ``cols = int(width / res) + 1``, ``rows = int(height / res) + 1``.  Kept
    in one place so the analytical dry-run plan matches the real grid exactly.
    """
    cols = int(width / resolution) + 1
    rows = int(height / resolution) + 1
    return cols, rows


def compute_dry_run_grid_plan(
    pcb_path: Path,
    selected_grid: float,
    clearance: float,
    num_layers: int,
    max_cells: int,
) -> DryRunGridPlan | None:
    """Compute the analytical grid/cell/budget plan for ``--dry-run``.

    Reuses the allocation-free selection math (``auto_select_grid_resolution``)
    and the canonical cells/memory formulas.  Constructs **no** RoutingGrid, so
    it returns in well under a second and cannot OOM on large boards.

    Args:
        pcb_path: Path to the .kicad_pcb file.
        selected_grid: The resolved grid resolution in mm (explicit ``--grid``
            value or the auto-selected float).
        clearance: Trace clearance in mm (feeds the candidate DRC filter).
        num_layers: Number of routing layers (``layer_stack.num_layers``).
        max_cells: Cell budget from ``--max-cells``.

    Returns:
        A ``DryRunGridPlan``, or ``None`` if the board has no detectable
        outline (in which case the caller should fall through to the normal
        load path rather than guess dimensions).
    """
    from kicad_tools.acceleration.backend import estimate_memory_bytes
    from kicad_tools.router.io import (
        auto_select_grid_resolution,
        extract_board_dimensions,
        extract_pad_positions,
    )

    dims = extract_board_dimensions(pcb_path)
    if dims is None:
        return None
    board_width, board_height = dims

    pads = extract_pad_positions(pcb_path)

    # Pure pad + area arithmetic -- allocates NO grid (io.py:1097).  Used both
    # for the finer/coarser candidate lattice and to report what "auto" would
    # pick alongside an explicit --grid.
    selection = auto_select_grid_resolution(
        pads=pads,
        clearance=clearance,
        board_width=board_width,
        board_height=board_height,
        max_cells=max_cells,
    )

    # Off-grid counts keyed by resolution, from the selector's candidate trials.
    off_grid_by_res = dict(selection.candidates_tried)

    def _make_candidate(resolution: float) -> DryRunGridCandidate:
        cols, rows = _grid_dims_for(board_width, board_height, resolution)
        cells = cols * rows * num_layers
        return DryRunGridCandidate(
            resolution=resolution,
            cols=cols,
            rows=rows,
            cells=cells,
            est_memory_bytes=estimate_memory_bytes(cols, rows, num_layers),
            fits=cells <= max_cells,
            off_grid_pads=off_grid_by_res.get(resolution, -1),
        )

    # Candidate lattice: the canonical PCB grid lattice (io.py:1158) restricted
    # to DRC-compliant resolutions (``<= clearance``, the selector's own
    # ``valid_candidates`` filter at io.py:1186), unioned with the selected and
    # auto-recommended resolutions so the report always brackets the selection
    # with its finer/coarser neighbours.  The memory filter is intentionally
    # NOT applied here -- showing coarser grids that *do* fit the budget is the
    # whole point of the finer/coarser candidate list.
    canonical_lattice = [0.5, 0.25, 0.127, 0.1, 0.065, 0.05, 0.0508]
    candidate_set = {res for res in canonical_lattice if res <= clearance}
    candidate_set.add(selected_grid)
    candidate_set.add(selection.resolution)
    candidate_resolutions = sorted(candidate_set, reverse=True)
    candidates = [_make_candidate(res) for res in candidate_resolutions]

    selected = _make_candidate(selected_grid)

    return DryRunGridPlan(
        board_width=board_width,
        board_height=board_height,
        resolution=selected_grid,
        num_layers=num_layers,
        cols=selected.cols,
        rows=selected.rows,
        cells=selected.cells,
        est_memory_bytes=selected.est_memory_bytes,
        max_cells=max_cells,
        fits=selected.fits,
        auto_resolution=selection.resolution,
        grid_explicit=abs(selected_grid - selection.resolution) > 1e-9,
        memory_capped=selection.memory_capped,
        memory_forced_unsafe_grid=selection.memory_forced_unsafe_grid,
        candidates=candidates,
    )


def format_dry_run_grid_plan(plan: DryRunGridPlan) -> str:
    """Render a ``DryRunGridPlan`` as the human-readable dry-run report."""
    bar = "=" * 60

    def _mb(nbytes: int) -> str:
        return f"{nbytes / 1e6:,.1f} MB"

    verdict = "FITS" if plan.fits else "EXCEEDS"
    if plan.fits:
        budget_line = (
            f"Budget:         max-cells={plan.max_cells:,} -> FITS "
            f"({plan.max_cells - plan.cells:,} cells to spare)"
        )
    else:
        budget_line = (
            f"Budget:         max-cells={plan.max_cells:,} -> EXCEEDS "
            f"by {plan.cells - plan.max_cells:,} cells"
        )

    if plan.grid_explicit:
        auto_line = f"Auto would use: {plan.auto_resolution}mm"
    else:
        cap = " (memory-capped)" if plan.memory_capped else ""
        auto_line = f"Grid source:    auto-selected{cap}"

    lines = [
        bar,
        "Dry Run - Analytical Grid Plan (no grid allocated)",
        bar,
        f"Board:          {plan.board_width:g}mm x {plan.board_height:g}mm",
        f"Selected grid:  {plan.resolution}mm",
        f"Grid cells:     {plan.cols:,} x {plan.rows:,} x {plan.num_layers} = {plan.cells:,} cells",
        f"Est. memory:    {_mb(plan.est_memory_bytes)} (18 bytes/cell)",
        budget_line,
        auto_line,
    ]

    if plan.memory_forced_unsafe_grid:
        lines.append(
            "Warning:        memory cap forced a grid coarser than "
            "clearance/2 (DRC-short risk; see --allow-unsafe-grid)"
        )

    lines.append("")
    lines.append("Lattice-aligned candidates (coarse -> fine):")
    for cand in plan.candidates:
        marker = " <- selected" if abs(cand.resolution - plan.resolution) <= 1e-9 else ""
        rel = ""
        if not marker:
            rel = "finer " if cand.resolution < plan.resolution else "coarser"
        tag = "FITS   " if cand.fits else "EXCEEDS"
        lines.append(
            f"  {rel:7s} {cand.resolution:<8g}mm  "
            f"{cand.cols:,} x {cand.rows:,} x {plan.num_layers} = {cand.cells:,} cells  "
            f"({_mb(cand.est_memory_bytes)})  {tag}{marker}"
        )

    lines.append(bar)
    lines.append(f"Verdict: {plan.resolution}mm grid {verdict} the {plan.max_cells:,}-cell budget.")
    lines.append(bar)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for route command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools route",
        description="Autoroute a KiCad PCB file",
        epilog=textwrap.dedent("""\
            exit codes:
              0  all nets routed (or meets --min-completion), DRC clean
              1  fatal failure -- no nets routed
              2  partial routing -- below --min-completion threshold
              3  routing meets threshold but DRC violations remain
              4  partial routing AND segment-segment clearance violations
              5  interrupted by SIGINT with partial results saved
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: <input>_routed.kicad_pcb)",
    )
    parser.add_argument(
        "--strategy",
        choices=["basic", "negotiated", "monte-carlo", "evolutionary"],
        default="negotiated",
        help="Routing strategy (default: negotiated)",
    )
    parser.add_argument(
        "--skip-nets",
        help="Comma-separated nets to skip (e.g., GND,VCC,VBUS)",
    )
    parser.add_argument(
        "--preserve-existing",
        action="store_true",
        default=False,
        help=(
            "Incremental routing (Issue #3155): load existing "
            "(segment ...)/(via ...) copper as immovable obstacles and "
            "re-emit it unchanged, so only unconnected nets are routed. "
            "Preserves manually-routed nets, skipped nets' geometry, and "
            "standalone stitch vias across a route pass. Default off (full "
            "re-route, existing copper is replaced by freshly routed nets)."
        ),
    )
    parser.add_argument(
        "--grid",
        type=str,
        default="auto",
        help=(
            "Grid resolution in mm or 'auto' for automatic selection "
            "(default: auto, analyzes pad positions and clearance; "
            "use explicit value like 0.1 for dense QFP)"
        ),
    )
    parser.add_argument(
        "--grid-strategy",
        choices=["adaptive", "uniform"],
        default="adaptive",
        help=(
            "Grid strategy when --grid auto is used. "
            "'adaptive' (default) uses multi-resolution grids with fine zones "
            "around fine-pitch components. 'uniform' forces single-resolution grid."
        ),
    )
    parser.add_argument(
        "--max-cells",
        type=int,
        default=500_000,
        help=(
            "Maximum grid cells to allow for --grid auto (default: 500,000). "
            "Raise this on large boards when auto-grid selects a coarse, "
            "unsafe grid because of the memory budget cap (this is the "
            "caller-facing override for the budget named in the 'Increase "
            "max_cells' warning/error text). Values below the default are "
            "honored too, making the memory cap bind harder. Threaded to both "
            "the uniform and adaptive (multi-resolution) grid-selection paths. "
            "No effect when --grid is an explicit value rather than 'auto'."
        ),
    )
    parser.add_argument(
        "--trace-width",
        type=float,
        default=0.2,
        help="Trace width in mm (default: 0.2)",
    )
    parser.add_argument(
        "--clearance",
        type=float,
        default=0.15,
        help="Trace clearance in mm (default: 0.15)",
    )
    parser.add_argument(
        "--fine-pitch-clearance",
        type=float,
        default=None,
        help=(
            "Clearance for fine-pitch components (pitch < 0.8mm) in mm. "
            "When set, SSOP/QFP/QFN packages automatically use this reduced "
            "clearance to allow traces between pins. Example: --fine-pitch-clearance 0.08"
        ),
    )
    parser.add_argument(
        "--via-drill",
        type=float,
        default=0.3,
        help="Via drill size in mm (default: 0.3)",
    )
    parser.add_argument(
        "--via-diameter",
        type=float,
        default=0.6,
        help="Via pad diameter in mm (default: 0.6)",
    )
    parser.add_argument(
        "--mc-trials",
        type=int,
        default=10,
        help="Number of Monte Carlo trials (default: 10)",
    )
    parser.add_argument(
        "--pop-size",
        type=int,
        default=20,
        help="Evolutionary optimizer population size (default: 20)",
    )
    parser.add_argument(
        "--generations",
        type=int,
        default=10,
        help="Evolutionary optimizer generations (default: 10)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=15,
        help=(
            "Max iterations for negotiated routing (default: 15). "
            "Also applies to two-phase routing when --two-phase-iterations "
            "is not explicitly set."
        ),
    )
    parser.add_argument(
        "--early-stop-patience",
        type=int,
        default=2,
        help=(
            "Number of consecutive negotiated-rip-up iterations with no "
            "improvement to the best-metric lex tuple "
            "(routed_count, clearance_violations, overflow) before the "
            "outer loop breaks early (Issue #3101). Default: 2. The "
            "iteration-0 result is preserved via the existing post-loop "
            "best-state restore. Use 0 to disable (matches pre-#3101 "
            "behaviour where the loop ran until --iterations or one of "
            "the existing should_terminate_early heuristics fired)."
        ),
    )
    parser.add_argument(
        "--targeted-ripup",
        action="store_true",
        help=(
            "Enable targeted rip-up in the negotiated routing loop "
            "(Issue #3438 / #3414).  Instead of ripping up every net that "
            "shares an overused cell, the negotiator identifies the "
            "specific nets blocking each failed net (direct-path scan + "
            "same-tier destination siblings) and displaces only those.  "
            "Helps parallel pad-array bundles (facing QFN pin columns, "
            "DDR byte lanes) where the last-ordered member finds its only "
            "escape corridor consumed by siblings and whole-cell rip-up "
            "cannot recover.  The underlying implementation "
            "(route_all_negotiated(use_targeted_ripup=...)) predates this "
            "flag but was previously unreachable from the CLI."
        ),
    )
    parser.add_argument(
        "--bundle-river-planner",
        action="store_true",
        help=(
            "Enable the scoped bundle river planner for mirrored byte-lane "
            "bus reversals (Issue #4053, epic #4049).  Board 07's DDR data "
            "byte is a FULL bus reversal between two facing QFN-48 pin "
            "columns (all C(11,2)=55 pairs cross), which planar same-layer "
            "lane ordering cannot solve — every ordering-only approach "
            "(#3438/#4050/#4051) capped at <=10/11.  This flag resolves both "
            "facing rows, diffs their permutation, and reserves one "
            "inner-layer via-hop corridor per inverted (crossing) pair so "
            "the losing net can dip under its partner.  Default OFF "
            "(byte-identical to prior behaviour when absent); DDR-bundle "
            "scoped in v1."
        ),
    )
    parser.add_argument(
        "--monotone-certificate-order",
        action="store_true",
        help=(
            "Enable the monotone-certificate escape order for byte-lane "
            "buses (Issue #4089, epic #4049).  Board 07's DDR byte is proven "
            "feasible and routes 11/11 in isolation (#4089) but has not been "
            "validated end-to-end on the assembled board; when the "
            "certificate finds the bundle infeasible as-pinned, order is left "
            "at IDENTITY (no regression vs. flag-off).  Default OFF "
            "(byte-identical to prior behaviour when absent)."
        ),
    )
    parser.add_argument(
        "--no-rescue-pass",
        action="store_true",
        help=(
            "Disable the post-negotiation rescue sweep (Issue #4159).  ON by "
            "default: after the negotiated batch loop converges/stalls/times "
            "out, each still-stranded net is re-attempted SOLO on the live "
            "grid, recovering long-haul nets the batch loop starved on per-net "
            "search budget (each such net routes in <1s alone).  The pass is "
            "bounded and strictly additive (failed attempts roll back), so it "
            "can only raise the routed count.  Pass this flag to get the raw "
            "negotiated result (e.g. for A/B comparison)."
        ),
    )
    parser.add_argument(
        "--cross-package-pair-corridor",
        action="store_true",
        help=(
            "Enable the cross-package pair corridor for diff/matched pairs "
            "whose members escape from facing packages (Issue #4090, epic "
            "#4049).  Reserves a shared corridor so the pair's two escapes "
            "stay coupled across the package gap.  Default OFF "
            "(byte-identical to prior behaviour when absent)."
        ),
    )
    parser.add_argument(
        "--slack-corridor-widening",
        action="store_true",
        help=(
            "Enable slack-corridor widening (Issue #4092, epic #4049).  "
            "Prefers slack-reserved corridors and threads the reservation "
            "into the escape router and diff-pair length tuning so tuned "
            "nets can widen into reserved slack.  Default OFF "
            "(byte-identical to prior behaviour when absent)."
        ),
    )
    parser.add_argument(
        "--max-ripups-per-net",
        type=int,
        default=None,
        help=(
            "Per-net rip-up budget for rip-up recovery.  Caps how many "
            "times any single net can be displaced to prevent "
            "displacement loops.  Default: 3 for --targeted-ripup and "
            "the two-phase stall recovery, 2 for the standard route_all "
            "flow.  Issue #3470: previously only fed --targeted-ripup; "
            "now also governs the BLOCKED_BY_COMPONENT destructive "
            "rip-up budget in route_all and the two-phase initial-pass "
            "stall recovery."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Total wall-clock timeout in seconds for the whole routing "
            "invocation (default: no timeout).  This is a TOTAL budget: "
            "auto-layer escalation, placement-routing feedback, auto-fix "
            "passes, and inner negotiated/two-phase/escape calls all share "
            "the same deadline.  The command returns the best partial "
            "result available when the deadline fires (issue #2802)."
        ),
    )
    parser.add_argument(
        "--per-net-timeout",
        type=float,
        default=30.0,
        help="Wall-clock timeout in seconds for each per-net A* search (default: 30). "
        "Prevents individual nets from monopolizing the router. Use 0 to disable.",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=float,
        default=30.0,
        help=(
            "Interval in seconds between best-so-far checkpoint writes to "
            "--output during the negotiated routing loop (Issue #2808). "
            "Each checkpoint atomically replaces the file at --output so a "
            "crash/SIGTERM/--timeout leaves the user with the best partial "
            "result rather than the original unrouted input. Default: 30.0. "
            "Use 0 to disable checkpointing (only the terminal save fires)."
        ),
    )
    # Issue #2819: declare --max-search-iterations on the inner parser so the
    # forwarding shim in ``commands/routing.py`` can hand it through.  Default
    # 0 = use the historical ``cols * rows * 4`` heuristic (matches the outer
    # parser declaration at parser.py and the C++ A* backstop semantics from
    # Issue #2610).
    parser.add_argument(
        "--max-search-iterations",
        type=int,
        default=0,
        help=(
            "Override the C++ A* iteration backstop (default: 0 = use "
            "cols*rows*4, which is ~1M for a 500x500 grid). Positive values "
            "let dense boards trade memory for completeness. Iteration-cap "
            "aborts are logged distinctly from --per-net-timeout (wall-clock) "
            "aborts so you can tell which limit fired."
        ),
    )
    # Issue #3881: --per-net-iterations is the TUNED per-net iteration cap,
    # distinct from --max-search-iterations (the 12M memory backstop).  Under
    # --deterministic-budget the 12M backstop is effectively unbounded per-net,
    # so one hard net can monopolise the whole --timeout and starve the rest.
    # A smaller per-net cap bounds each net to a fair iteration slice
    # (load-independent, so still deterministic) and lets more nets get a turn.
    parser.add_argument(
        "--per-net-iterations",
        type=int,
        default=0,
        help=(
            "Tuned per-net C++ A* iteration cap (default: 0 = unset). When set, "
            "each net's search gives up DETERMINISTICALLY after N node "
            "expansions (FAILURE_ITERATION_LIMIT) so a hard net cannot "
            "monopolise the budget -- the next net gets its turn. Distinct from "
            "--max-search-iterations (the absolute memory backstop): the "
            "effective per-net cap is min(N, max-search-iterations). A net that "
            "hits this tuned cap is a deterministic give-up and its Python "
            "fallback is skipped, so the cap is a hard per-net bound. Iteration "
            "count is load-independent, so routing stays reproducible. "
            "--deterministic-budget defaults this to a sensible value "
            f"({DETERMINISTIC_BUDGET_PER_NET_ITERATIONS:,})."
        ),
    )
    parser.add_argument(
        "--deterministic-budget",
        action="store_true",
        help=(
            "Bound routing work by an ITERATION budget instead of wall-clock "
            "time so the routed output (and its DRC count) is reproducible "
            "across machines regardless of runner speed or load (Issue #3538). "
            "The per-net A* search is the only wall-clock-coupled stage that "
            "lands different amounts of copper on a slow vs fast machine; this "
            "flag disables the per-net wall-clock cutoff (--per-net-timeout 0) "
            "and pins the C++ A* iteration backstop (--max-search-iterations) "
            "to a fixed positive value, so each search either finds a path or "
            "aborts after the SAME node-expansion count on every environment. "
            "--timeout (the outer wall-clock budget) is kept only as a safety "
            "backstop; if it fires it is logged as a determinism-breaking "
            "warning. Combine with --seed for byte-stable re-routes. The fixed "
            "iteration backstop value can be overridden by passing an explicit "
            "--max-search-iterations N alongside this flag (N is then used "
            "verbatim); see DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS for the "
            "default."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Seed the global ``random`` module with N before routing for "
            "reproducible results (Issue #2589). When set, escape strategies "
            "in the negotiated router (``_escape_shuffle_order``, "
            "``_escape_random_subset``, ``_escape_full_reorder``) and the "
            "MST fine-grid trial shuffle become deterministic across "
            "invocations, so two runs with identical inputs and the same "
            "--seed produce byte-identical routed output -- including the "
            "per-element UUID lines, which are derived from the seeded RNG "
            "when --seed is set (Issue #3272/#3925), so a same-seed regen "
            "yields a zero-line git diff. Without --seed the "
            "router uses Python's default os.urandom-derived entropy and "
            "results (and UUIDs) vary run-to-run. Note: determinism is "
            "per-router-version -- a regen from a newer router may "
            "legitimately differ from a committed artifact; only "
            "fresh-vs-fresh at the same version is byte-identical. --seed "
            "also does NOT remove all sources of variance -- wall-clock "
            "escape budgets (e.g. --timeout) can still terminate early on a "
            "loaded machine; for fully reproducible CI runs combine --seed "
            "with a generous --timeout."
        ),
    )
    parser.add_argument(
        "--order-method",
        choices=["greedy", "critical_first", "congestion", "hybrid"],
        default=None,
        help=(
            "Compute the net routing order with a named heuristic (Issue #3897) "
            "instead of the default priority-based sort. Overrides the internal "
            "_get_net_priority ordering. Choices: 'greedy' (fewest pads first), "
            "'critical_first' (power/clock nets first), 'congestion' (most "
            "congested nets first), 'hybrid' (critical_first + congestion). "
            "'congestion' and 'hybrid' require a congestion map; if one cannot "
            "be obtained the command warns and falls back to 'greedy'. When "
            "omitted, ordering is byte-identical to the default behaviour."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show routing preview with per-net details before saving (interactive)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing output",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Analyze routability before routing and show diagnostic report",
    )
    parser.add_argument(
        "--bus-routing",
        action="store_true",
        help="Enable bus-aware routing (routes bus signals together)",
    )
    parser.add_argument(
        "--bus-mode",
        choices=["parallel", "stacked", "bundled"],
        default="parallel",
        help="Bus routing mode (default: parallel)",
    )
    parser.add_argument(
        "--bus-spacing",
        type=float,
        help="Spacing between bus signals in mm (default: trace_width + clearance)",
    )
    parser.add_argument(
        "--bus-min-width",
        type=int,
        default=2,
        help="Minimum signals to form a bus group (default: 2)",
    )
    parser.add_argument(
        "--differential-pairs",
        action="store_true",
        help="Enable differential pair routing (routes paired signals together)",
    )
    parser.add_argument(
        "--diffpair-spacing",
        type=float,
        help="Spacing between differential pair traces in mm (default: auto based on type)",
    )
    parser.add_argument(
        "--diffpair-max-delta",
        type=float,
        help="Maximum length mismatch for differential pairs in mm (default: auto based on type)",
    )
    parser.add_argument(
        "--diffpair-per-pair-timeout",
        type=float,
        default=None,
        help=(
            "Per-pair wall-clock budget for the CoupledPathfinder in "
            "seconds (Issue #3089).  When set, each diff-pair coupled "
            "A* search abandons after this many seconds and the pair "
            "is deferred to the main strategy (single-ended A*).  "
            "Required for boards with dense BGA/QFN escape geometry "
            "where the unbounded coupled search can hang for many "
            "minutes per pair (e.g. board 07's MIPI lanes, Issue "
            "#3275).  Default: no per-pair budget (bounded only by "
            "the overall --timeout and the C++ grid-cell*4 iteration "
            "ceiling)."
        ),
    )
    parser.add_argument(
        "--length-match-diffpairs",
        action="store_true",
        help=(
            "Enable per-pair differential length-match tuning (Epic #2556 "
            "Phase 3I). Inserts serpentines on the shorter half of each "
            "length-critical diff pair until skew is within the per-class "
            "tolerance. Requires --differential-pairs to be set; emits a "
            "warning and short-circuits otherwise."
        ),
    )
    parser.add_argument(
        "--length-match-groups",
        action="store_true",
        help=(
            "Enable N-trace match-group length-match tuning (Epic #2661 "
            "Phase 3H). Detects parallel-bus groups (DDR, MIPI, HDMI TMDS) "
            "declared via NetClassRouting.length_match_group, then inserts "
            "serpentines on shorter group members until the per-group skew "
            "is within tolerance. Compatible with --length-match-diffpairs; "
            "groups whose members are diff pairs (MIPI/HDMI lane groups) "
            "engage the Phase 2F symmetric-serpentine path."
        ),
    )
    parser.add_argument(
        "--net-class-map",
        dest="net_class_map",
        default=None,
        help=(
            "Path to a JSON sidecar mapping net names to NetClassRouting "
            "fields (Issue #2996).  Merged into the autorouter's "
            "name-pattern-classified net_class_map so per-pair / per-group "
            "fields (intra_pair_clearance, coupled_routing, "
            "coupled_continuity_threshold, target_diff_impedance, "
            "length_match_group) project through to the routing-time "
            "pathfinder.  Mirrors the kct check --net-class-map flag.  "
            "Keys are matched against the board net's sheet-local suffix "
            "(the segment after the last '/'), so a bare key FUSED_LINE "
            "matches KiCad's '/'-prefixed label net /FUSED_LINE while "
            "global power nets (GND, +3.3V) stay bare; keys that match no "
            "board net or match ambiguously are reported on stderr and "
            "skipped (Issue #4149)."
        ),
    )
    parser.add_argument(
        "--analog-nets",
        dest="analog_nets",
        default=None,
        help=(
            "Comma-separated list of analog net names (e.g. "
            '"AUDIO_L,AUDIO_R") to route with a boosted analog class '
            "(Issue #3171, Phase 3).  Selected nets get priority=2 (route "
            "before digital signals) and cost_multiplier=0.85 (shorter-path "
            "bias).  Pour/ground nets (e.g. GNDA) are never forced into the "
            "pathfinder.  NOTE: guard-trace / shield-copper generation is NOT "
            "implemented and is deferred to a follow-up (Phase 4)."
        ),
    )
    parser.add_argument(
        "--auto-analog",
        dest="auto_analog",
        action="store_true",
        help=(
            "Auto-detect analog nets via the Phase 2 detector "
            "(detect_analog_nets) and route them with the boosted analog "
            "class (Issue #3171).  May be combined with --analog-nets (the "
            "two sets are unioned)."
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )
    parser.add_argument(
        "--perturbation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enable stochastic cost perturbation to escape local minima "
            "(default: enabled). Use --no-perturbation to disable."
        ),
    )
    parser.add_argument(
        "--power-nets",
        help=(
            "Generate copper zones for power nets: 'NET1:LAYER1,NET2:LAYER2,...' "
            "(e.g., 'GND:B.Cu,+3.3V:F.Cu')"
        ),
    )
    parser.add_argument(
        "--edge-clearance",
        type=float,
        help=(
            "Copper-to-edge clearance in mm. Blocks routing within this distance "
            "of the board edge. Common values: 0.25-0.5mm (default: no clearance)"
        ),
    )
    parser.add_argument(
        "--layers",
        choices=["auto", "2", "4", "4-sig", "4-all", "6"],
        default="auto",
        help=(
            "Layer stack configuration for routing: "
            "'auto' = auto-detect from PCB file (default); "
            "'2' = 2-layer (F.Cu, B.Cu); "
            "'4' = 4-layer with GND/PWR planes (F.Cu, In1=GND, In2=PWR, B.Cu); "
            "'4-sig' = 4-layer with 2 signal layers (F.Cu, In1=signal, In2=GND, B.Cu); "
            "'4-all' = 4-layer with all 4 signal layers (no planes); "
            "'6' = 6-layer with 4 signal layers. "
            "Auto-detection parses the PCB's layer definitions and zones to "
            "determine the appropriate layer stack."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Force routing even when grid resolution exceeds clearance. "
            "Without this flag, routing will fail if grid > clearance to "
            "prevent DRC violations. Use with caution."
        ),
    )
    parser.add_argument(
        "--allow-unsafe-grid",
        action="store_true",
        help=(
            "Allow --grid auto to route on a grid coarser than clearance/2 when "
            "the memory budget cap forces it (issue #3911). Without this flag, "
            "routing is REFUSED in that case because it reliably produces "
            "cross-net clearance shorts (an unrouted net is strictly safer than "
            "a short). Only pass this if you understand and accept the DRC risk; "
            "prefer enlarging/splitting the board, adding layers, or loosening "
            "the manufacturer clearance instead. (--force also overrides.)"
        ),
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help=(
            "Enable profiling to measure performance. Outputs a cProfile "
            "stats file that can be analyzed with pstats or visualization tools."
        ),
    )
    parser.add_argument(
        "--profile-output",
        metavar="FILE",
        help=(
            "Output file for profile data (default: route_profile.prof). "
            "Analyze with: python -m pstats route_profile.prof, or "
            "visualize with: snakeviz route_profile.prof"
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "cpp", "python"],
        default="auto",
        help=(
            "Router backend to use: "
            "'auto' = use C++ if available, fall back to Python (default); "
            "'cpp' = require C++ backend (fails if not available); "
            "'python' = force Python backend (for testing/debugging). "
            "C++ backend provides 10-100x speedup for fine-grid routing."
        ),
    )
    parser.add_argument(
        "--route-engine",
        choices=["grid", "mesh", "lattice"],
        default="grid",
        help=(
            "Routing substrate (issues #4268/#4278), orthogonal to --backend: "
            "'grid' = uniform-grid A* (default, unchanged); "
            "'mesh' = navmesh router (poly2tri CDT + funnel + clearance-aware "
            "45deg fit, multi-net portal negotiation); 'lattice' = adaptive "
            "octilinear lattice (balanced quadtree, paths are 45deg-legal by "
            "construction, negotiated multi-net, through-vias at free-space "
            "lattice nodes). Mesh and lattice are experimental engines that "
            "run their own whole-netset negotiation and REQUIRE --strategy "
            "basic; combining them with the default negotiated strategy, "
            "monte-carlo, evolutionary, --two-phase, --multi-resolution, or "
            "--escape-routing is rejected (issue #4280). Grid remains the "
            "production default and works with every strategy."
        ),
    )
    parser.add_argument(
        "--no-auto-build-native",
        action="store_true",
        help=(
            "Disable silent auto-build of the C++ router extension on first use "
            "(Issue #2549). When --backend is 'auto' (the default) and the "
            "compiled router_cpp.*.so is missing, kct route normally invokes "
            "'kct build-native' once and uses C++ for the rest of the session. "
            "Pass this flag (or set KICAD_TOOLS_NO_AUTO_BUILD=1) to skip the "
            "build attempt and fall straight through to pure Python."
        ),
    )
    parser.add_argument(
        "--skip-drc",
        action="store_true",
        help=(
            "Skip post-routing DRC validation. By default, the router runs "
            "a DRC check after routing and warns about violations. Use this "
            "flag for performance-critical use or when running separate validation."
        ),
    )
    # Issue #4178: hard-gate on native (kicad-cli) DRC actually running.
    # Mirror of the outer parser.py flag; both sites must stay in sync per
    # ``tests/test_cli_parser_drift.py``.
    parser.add_argument(
        "--strict-drc",
        action="store_true",
        default=False,
        help=(
            "Treat 'native kicad-cli DRC did not run' (kicad-cli absent, "
            "timed out, crashed, or produced no report) as a HARD FAILURE "
            "(non-zero exit) instead of a soft NOTE. By default the post-route "
            "gate degrades gracefully to an internal-engine-only PASS when "
            "kicad-cli is unavailable, which is not authoritative. Use this in "
            "CI / manufacturing pipelines that require the native DRC to have "
            "actually run and passed."
        ),
    )
    # Issue #3154: advisory schematic/PCB drift banner.  When a schematic is
    # auto-discovered (or passed via --schematic) and the component sets have
    # drifted, kct route prints a one-line, non-blocking warning before
    # routing so an engineer is not lulled by a "65% routed" number on a board
    # that is missing a third of the netlist.  --no-sync-check opts out.
    parser.add_argument(
        "--sync-check",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Print an advisory banner when the PCB footprint set has drifted "
            "from the schematic netlist (default: enabled). The banner is "
            "non-blocking; use --no-sync-check to suppress it. See "
            "'kct check --netlist-sync' for a blocking gate."
        ),
    )
    parser.add_argument(
        "--schematic",
        default=None,
        help=(
            "Explicit .kicad_sch path for the advisory drift banner "
            "(default: auto-discover from project.kct or sibling file)."
        ),
    )
    # Issue #4156: hard off-board preflight.  Unlike the advisory drift banner,
    # a footprint placed outside the Edge.Cuts outline makes routing pointless
    # (its nets can never complete), so kct route aborts by default before any
    # router work.  --allow-offboard is the explicit escape hatch for boards
    # that intentionally stage footprints outside the outline.
    parser.add_argument(
        "--allow-offboard",
        action="store_true",
        default=False,
        help=(
            "Skip the off-board placement preflight. By default kct route "
            "aborts (exit 2) when any footprint's courtyard falls outside the "
            "Edge.Cuts outline, since routing an off-board net always fails. "
            "Use this to proceed anyway (e.g. intentional staging/reference "
            "footprints)."
        ),
    )
    parser.add_argument(
        "--manufacturer",
        "--mfr",
        default="jlcpcb",
        help=(
            "Manufacturer profile for DRC validation (default: jlcpcb). "
            "Determines minimum clearances, trace widths, and other design rules."
        ),
    )
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        help=(
            "Automatically run 'kct fix-drc' after routing if DRC violations are "
            "detected. Suppressed by --dry-run and --skip-drc. Uses iterative "
            "repair to fix clearance and drill violations."
        ),
    )
    parser.add_argument(
        "--auto-fix-passes",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Number of repair passes for --auto-fix (default: 3). "
            "Implies --auto-fix. Multiple passes can fix cascading violations "
            "where fixing one violation exposes or resolves others."
        ),
    )
    # Issue #2595: placement-feedback opt-in flags.  When the initial
    # routing pass leaves nets unrouted with BLOCKED_BY_COMPONENT root
    # cause, --placement-feedback invokes the existing closed-loop
    # placement adjuster (Autorouter.route_with_placement_feedback) to
    # nudge non-anchored components and re-route.  Connectors (J*) and
    # locked footprints are anchored automatically.
    parser.add_argument(
        "--placement-feedback",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "After the initial routing pass, if any nets failed with "
            "BLOCKED_BY_COMPONENT root cause, invoke the placement-routing "
            "feedback loop to nudge non-anchored components and re-route "
            "(default: disabled).  Use --no-placement-feedback to disable "
            "explicitly.  Connectors (refs starting with 'J' or 'P') and "
            "footprints with the KiCad 'locked' attribute are never moved "
            "(see --placement-feedback-anchor / --placement-feedback-no-anchor "
            "for overrides).  Issue #2595."
        ),
    )
    parser.add_argument(
        "--placement-feedback-budget",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Maximum number of placement adjustments to attempt when "
            "--placement-feedback is set (default: 3). Each adjustment "
            "moves one or more components and re-routes from scratch."
        ),
    )
    parser.add_argument(
        "--placement-feedback-max-movement",
        type=float,
        default=5.0,
        metavar="MM",
        help=(
            "Hard cap on per-component movement distance for the placement "
            "feedback loop, in mm (default: 5.0). Strategies that would "
            "move any component by more than this distance are filtered out."
        ),
    )
    parser.add_argument(
        "--placement-feedback-anchor",
        default=None,
        metavar="REFS",
        help=(
            "Additional component references to anchor (never move) during "
            "the placement feedback loop, comma-separated. Combined with the "
            "auto-detected anchors (connectors, locked footprints). "
            "Example: --placement-feedback-anchor U5,U7"
        ),
    )
    parser.add_argument(
        "--placement-feedback-no-anchor",
        default=None,
        metavar="REFS",
        help=(
            "Component references to remove from the anchor set, "
            "comma-separated. Use this to allow movement of components that "
            "would otherwise be auto-anchored (e.g. a non-mechanical "
            "connector). Example: --placement-feedback-no-anchor J3"
        ),
    )
    # Issue #2606: stagnation + outer-timeout guards on the
    # PlacementFeedbackLoop.  Mirror parser.py:2349-2376 so the
    # top-level forwarder in commands/routing.py can pass these
    # through without tripping argparse.  Defaults match parser.py.
    # Issue #2620: previously missing on the inner parser, causing
    # "unrecognized arguments" when the forwarder injected them.
    parser.add_argument(
        "--placement-feedback-stagnation-patience",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Number of consecutive outer placement-feedback iterations with "
            "no fully-routed-net-count improvement before the loop exits "
            "early with exit_reason=pf_stagnated. Default 3. Set to 0 to "
            "disable stagnation detection. Issue #2606."
        ),
    )
    parser.add_argument(
        "--placement-feedback-outer-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Hard wall-clock budget for the entire outer placement-feedback "
            "loop, in seconds. When exceeded between iterations the loop "
            "exits with exit_reason=pf_timeout. Default: no outer cap "
            "(only the per-iteration --timeout applies). Issue #2606."
        ),
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help=(
            "Skip trace optimization after routing. By default, traces are "
            "optimized to merge collinear segments, eliminate zigzags, and "
            "convert corners to 45 degrees. Use this flag to keep raw "
            "grid-step segments for debugging."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        dest="no_optimize",
        help="Alias for --no-optimize (keep raw grid-step segments for debugging)",
    )
    parser.add_argument(
        "--auto-pour",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Automatically create copper pour zones for power-classified "
            "nets (GND, VCC, etc.) when the input PCB has none "
            "(default: enabled). Use --no-auto-pour to disable."
        ),
    )
    parser.add_argument(
        "--auto-layers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Automatically escalate layer count on routing failure "
            "(default: enabled). Tries 2 → 4 → 6 layers until routing "
            "succeeds or --max-layers is reached. Reports minimum viable "
            "layer count for the design. The first attempt is still "
            "2-layer, so 2-layer-solvable boards pay no extra cost. "
            "Use --no-auto-layers to disable and route at a fixed layer "
            "count (whatever --layers specifies, or auto-detected)."
        ),
    )
    parser.add_argument(
        "--max-layers",
        type=int,
        default=6,
        choices=[2, 4, 6],
        help=(
            "Maximum layer count for auto-escalation (default: 6). Only used with --auto-layers."
        ),
    )
    # Issue #3400: ``--starting-layers`` lets boards opt out of the 2L tax.
    # Default 2 preserves the historical 2L->4L->6L ladder.  When the user
    # passes ``--starting-layers 4`` the ladder skips 2L and starts at 4L
    # directly.  Precedence: CLI flag > project.kct EscalationPolicy field
    # > default 2.  Validated against ``--max-layers`` at startup.
    parser.add_argument(
        "--starting-layers",
        type=int,
        default=None,
        choices=[2, 4, 6],
        help=(
            "Lower rung of the auto-escalation ladder (default: 2). "
            "Use --starting-layers 4 to skip the 2L probe on boards with "
            "no realistic chance of routing at 2L (Issue #3400). Only used "
            "with --auto-layers. Must be <= --max-layers. Overrides any "
            "starting_layers value in project.kct."
        ),
    )
    # Issue #3352 (P_AS3): --auto-pcb-size escalation.  Walks the
    # manufacturer's size-tier ladder when routing reach + DRC density
    # indicate the envelope (not layers or clearance) is the bottleneck.
    # Default off; opt-in because growing the board adds material cost.
    # Q5 decision: --auto-pcb-size IMPLIES --auto-layers (layers-first is
    # the default ladder and skipping layer escalation forfeits the
    # cheapest rung).  Use --no-auto-layers to opt out of the layers axis.
    parser.add_argument(
        "--auto-pcb-size",
        action="store_true",
        default=False,
        help=(
            "Automatically escalate PCB envelope to the next manufacturer "
            "size tier when routing reach + DRC density indicate the "
            "envelope is the bottleneck (default: disabled). Walks the "
            "registered cost-tier ladder for the current --mfr (e.g. "
            "JLCPCB 100x100 -> 100x150 -> 150x150 -> ...). Per Issue #3352 "
            "Q5, --auto-pcb-size implies --auto-layers because the "
            "layers-first default ladder skips the cheapest rung otherwise; "
            "pass --no-auto-layers to opt out of the layers axis. "
            "When the recipe declares envelope_hard=true OR a mounting hole "
            "group is pinned, the wrapper falls back to layers-only "
            "escalation with an actionable refusal message enumerating "
            "alternative recipe levers (BOM reduction, more layers, larger "
            "envelope manually, looser clearance via spec amendment)."
        ),
    )
    # Issue #3403: --packing-overhead overrides EscalationPolicy.packing_overhead
    # for the sum-of-clearances pre-route area estimator.  The estimator
    # computes ``required = packing_overhead * (sum(footprint_area + halo)
    # + routing_channels)`` and skips doomed routing attempts before they
    # waste a routing budget.  ``None`` (default) means "use the policy
    # value (default 2.5)".  ``0`` disables the pre-route check (reactive
    # DRC-density backstop still applies).
    parser.add_argument(
        "--packing-overhead",
        type=float,
        default=None,
        help=(
            "Packing-density multiplier for the --auto-pcb-size pre-route "
            "area estimator (Issue #3403).  Default uses the recipe's "
            "EscalationPolicy.packing_overhead (default 2.5).  Bump to "
            "3.0+ for tight layouts, down to 1.8 for loose ones.  Set to "
            "0 to disable the pre-route check (reactive DRC-density "
            "backstop still applies).  No effect when --auto-pcb-size "
            "is off."
        ),
    )
    # Issue #2881: --auto-mfr-tier / --mfr-tier-ladder.  Opt-in escalation
    # along a registered ladder of manufacturer tiers (e.g.
    # ``jlcpcb`` -> ``jlcpcb-tier1``).  Default off because tighter tiers
    # have higher cost; users explicitly opt in to allow the surcharge.
    parser.add_argument(
        "--auto-mfr-tier",
        action="store_true",
        default=False,
        help=(
            "Automatically escalate to a tighter manufacturer tier when "
            "geometric infeasibility blocks routing on the current tier "
            "(default: disabled). Walks the registered ladder for the "
            "current --mfr (e.g. jlcpcb -> jlcpcb-tier1, which adds via-in-pad "
            "capability for fine-pitch QFP escape). Opt-in because tighter "
            "tiers can incur a manufacturing surcharge."
        ),
    )
    parser.add_argument(
        "--mfr-tier-ladder",
        type=str,
        default=None,
        help=(
            "Explicit comma-separated manufacturer tier ladder for "
            "--auto-mfr-tier (e.g. 'jlcpcb,jlcpcb-tier1'). Overrides the "
            "default ladder registered for the current --mfr. Each entry "
            "must be a recognized manufacturer name."
        ),
    )
    parser.add_argument(
        "--min-completion",
        type=float,
        default=0.95,
        help=(
            "Minimum routing completion rate for success (default: 0.95 = 95%%). "
            "Controls the exit code threshold: routing above this rate returns "
            "exit code 0 (success), below returns exit code 2 (partial). "
            "Also used with --auto-layers to control layer escalation. "
            "If no layer count achieves this, the best result is saved."
        ),
    )
    parser.add_argument(
        "--adaptive-rules",
        action="store_true",
        help=(
            "Automatically relax design rules on routing failure. "
            "Tries progressively relaxed trace widths and clearances "
            "until routing succeeds or manufacturer limits are reached. "
            "Reports which rules were relaxed and warns if minimum tolerances used."
        ),
    )
    parser.add_argument(
        "--no-early-stop",
        action="store_true",
        help=(
            "Disable early termination of adaptive-rules tier search. "
            "By default, if a tier routes fewer nets than the best prior "
            "attempt, remaining tiers are skipped. Use this flag to force "
            "all tiers to run (useful for debugging or benchmarking)."
        ),
    )
    parser.add_argument(
        "--min-trace",
        type=float,
        help=(
            "Minimum trace width floor for adaptive rules (mm). "
            "Prevents relaxation below this value. "
            "Default: manufacturer minimum (e.g., 0.127mm for JLCPCB)."
        ),
    )
    parser.add_argument(
        "--min-clearance-floor",
        type=float,
        help=(
            "Minimum clearance floor for adaptive rules (mm). "
            "Prevents relaxation below this value. "
            "Default: manufacturer minimum (e.g., 0.127mm for JLCPCB)."
        ),
    )
    parser.add_argument(
        "--progressive-clearance",
        action="store_true",
        help=(
            "Enable progressive clearance relaxation for failed nets. "
            "Routes all nets with standard clearance first, then retries "
            "failed nets with progressively relaxed clearance (up to --min-clearance). "
            "Unlike --adaptive-rules which globally relaxes all rules, this only "
            "relaxes clearance for specific failed nets. Reports which nets needed "
            "relaxation and the clearance used."
        ),
    )
    parser.add_argument(
        "--min-clearance",
        type=float,
        help=(
            "Minimum clearance for progressive relaxation (mm). "
            "Used with --progressive-clearance to set the floor for relaxation. "
            "Default: 50%% of --clearance value."
        ),
    )
    parser.add_argument(
        "--relaxation-levels",
        type=int,
        default=3,
        help=(
            "Number of progressive relaxation levels (default: 3). "
            "More levels = finer-grained relaxation steps."
        ),
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help=(
            "Show detailed routing failure diagnostics. For each failed net, "
            "reports the specific failure reason, blocking obstacles, coordinates, "
            "and actionable suggestions. Failures are grouped by cause for analysis."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help=(
            "Output format for routing diagnostics: "
            "'text' = human-readable output (default); "
            "'json' = JSON output for tooling and automation."
        ),
    )
    parser.add_argument(
        "--high-performance",
        action="store_true",
        help=(
            "Use high-performance mode with aggressive parallelization and more trials. "
            "Uses calibrated settings if available (run 'kicad-tools calibrate' first)."
        ),
    )
    parser.add_argument(
        "--hierarchical",
        action="store_true",
        help=(
            "Enable hierarchical coarse-to-fine routing mode. "
            "First performs global routing on a coarse grid (4x resolution) "
            "to establish corridors, then refines with the fine grid only "
            "near pads and congestion points. Can significantly speed up "
            "fine-grid routing (0.05mm-0.1mm) on large boards."
        ),
    )
    parser.add_argument(
        "--multi-resolution",
        action="store_true",
        help=(
            "Enable multi-resolution routing with fine-grid fallback. "
            "First routes all nets on the coarse grid, then retries failed "
            "nets on a finer grid (2x resolution) scoped to their bounding "
            "boxes. Useful for boards where some nets fail due to grid "
            "resolution limitations."
        ),
    )
    parser.add_argument(
        "--escape-routing",
        action="store_true",
        default=None,
        help=(
            "Enable escape routing phase before global routing. "
            "Generates escape routes for dense QFP/QFN/BGA packages "
            "where pin pitch is too small for traces to pass between "
            "adjacent pins. Without this flag, escape routing is "
            "auto-detected based on package density."
        ),
    )
    parser.add_argument(
        "--no-escape-routing",
        action="store_true",
        help=(
            "Disable automatic escape routing detection. By default, "
            "the router auto-detects dense packages and enables escape "
            "routing when needed. Use this flag to skip escape routing "
            "even when dense packages are present."
        ),
    )
    parser.add_argument(
        "--two-phase",
        action="store_true",
        help=(
            "Use two-phase global+detailed routing. Phase 1 allocates "
            "coarse corridors on a tile graph; Phase 2 routes within those "
            "corridors using negotiated congestion. Produces dramatically "
            "better results on complex multi-layer boards by preventing "
            "overflow divergence. When combined with escape routing, "
            "replaces the negotiated rip-up phase after escape generation."
        ),
    )
    parser.add_argument(
        "--two-phase-iterations",
        type=int,
        default=None,
        help=(
            "Max rip-up-and-reroute iterations for the Phase 2 detailed "
            "negotiated routing loop in two-phase mode. Overrides --iterations "
            "for the two-phase path when both are given. If omitted, falls back "
            "to --iterations (default: 20 when neither flag is set). "
            "Only effective with --two-phase."
        ),
    )
    parser.add_argument(
        "--batch-routing",
        action="store_true",
        help=(
            "Enable GPU-accelerated batch routing for parallel net processing. "
            "Routes multiple independent nets simultaneously using GPU compute. "
            "Best results with 4+ independent nets and Metal/CUDA GPU. "
            "Enabled automatically in high-performance mode."
        ),
    )
    # Issue #3054 (Phase 2 of #3045): expose region-based parallelism on the
    # inner parser so the outer ``kct route --region-parallel`` flag has a
    # forwarding target.  ``route_all_negotiated`` already implements the
    # partitioning logic; this is wiring only.
    parser.add_argument(
        "--region-parallel",
        action="store_true",
        default=False,
        help=(
            "Enable region-based parallel routing (Issue #965). Partitions "
            "the routing grid into regions and routes non-adjacent regions "
            "in parallel during each negotiated iteration. Default off. "
            "NOTE: auto-disabled with a log warning on small / dense boards "
            "where nets-per-region falls below 16 -- the partition + worker "
            "overhead would exceed the per-region A* savings on those "
            "workloads (Issue #3100; board-07 case showed +55%% wall-clock). "
            "Best used on >= 64-net boards with a 2x2 partition."
        ),
    )
    parser.add_argument(
        "--partition-rows",
        type=int,
        default=2,
        metavar="N",
        help="Number of region rows for --region-parallel (default: 2).",
    )
    parser.add_argument(
        "--partition-cols",
        type=int,
        default=2,
        metavar="N",
        help="Number of region columns for --region-parallel (default: 2).",
    )
    parser.add_argument(
        "--max-parallel-workers",
        type=int,
        default=4,
        metavar="N",
        help=("Maximum parallel workers per region group for --region-parallel (default: 4)."),
    )
    # Issue #4148: region-bounded routing.  SPATIAL routing bound -- confine all
    # new routing to an axis-aligned box.  Deliberately distinct from
    # --region-parallel (above), which is a parallelism-partitioning knob, NOT
    # a spatial bound.  argparse resolves the exact string ``--region`` to THIS
    # flag even though it is a prefix of ``--region-parallel`` (exact matches
    # take priority over abbreviation), so there is no ambiguity for the exact
    # spelling; only a shorter abbreviation like ``--regio`` is ambiguous.
    parser.add_argument(
        "--region",
        metavar="X1,Y1,X2,Y2",
        default=None,
        help=(
            "SPATIAL routing bound (Issue #4148): confine all new routing to "
            "the axis-aligned box 'x1,y1,x2,y2' (board-relative mm, same "
            "convention as 'pcb strip --region'). Cells outside the box are "
            "fixed obstacles; existing copper outside is preserved unchanged "
            "(implies --preserve-existing). Unrelated to --region-parallel."
        ),
    )

    # Power plane stitching
    parser.add_argument(
        "--stitch-power-planes",
        action="store_true",
        help=(
            "Automatically add stitching vias for power planes after routing. "
            "Connects surface-mount component pads to their power plane layers. "
            "Equivalent to running 'kicad-pcb-stitch' after routing."
        ),
    )

    # Export failed nets
    parser.add_argument(
        "--export-failed-nets",
        metavar="PATH",
        help=(
            "Export failed (unrouted) net names to a file, one per line. "
            "Useful for scripted workflows or manual completion in KiCad."
        ),
    )

    # Cache arguments
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable routing cache (force fresh routing)",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Only use cached results (fail if cache miss)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear routing cache before routing",
    )
    parser.add_argument(
        "--cache-stats",
        action="store_true",
        help="Show routing cache statistics and exit",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Fail with exit code 6 if connectivity is reduced by the optimize "
            "or DRC nudge pipeline phases (issue #2596), or if output "
            "connectivity verification detects any disconnected net. Without "
            "this flag, regressions are reverted (optimize/nudge) or reported "
            "as warnings (output verification) but do not affect the exit code."
        ),
    )
    parser.add_argument(
        "--strict-in-pad-clearance",
        action="store_true",
        dest="strict_in_pad_clearance",
        help=(
            "Issue #3033 / #3062: refuse to commit an in-pad rescue via that "
            "would clip a neighbouring foreign-net pad on a fine-pitch "
            "QFP/SSOP (the 'proceed anyway, defer DRC to the user' branch "
            "from PR #2945).  Sets KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1 so "
            "the lazily-constructed EscapeRouter inside the negotiated/two-"
            "phase pipelines picks up the flag without each call site needing "
            "an explicit pass-through.  Default off preserves the legacy "
            "bit-for-bit behaviour."
        ),
    )
    parser.add_argument(
        "--micro-via-in-pad-fallback",
        action="store_true",
        dest="micro_via_in_pad_fallback",
        help=(
            "Issue #3118: retry in-pad escape vias with smaller micro-via "
            "dimensions when the standard manufacturer-floor via clips a "
            "neighbouring foreign-net pad.  Sets "
            "KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK=1 so EscapeRouter "
            "inherits the opt-in.  Default off."
        ),
    )
    parser.add_argument(
        "--micro-via-size",
        type=float,
        default=0.3,
        dest="micro_via_size",
        help=(
            "Micro-via pad diameter in mm for the in-pad fallback "
            "(default: 0.3).  Stamps KICAD_TOOLS_MICRO_VIA_SIZE.  Only "
            "used with --micro-via-in-pad-fallback."
        ),
    )
    parser.add_argument(
        "--micro-via-drill",
        type=float,
        default=0.15,
        dest="micro_via_drill",
        help=(
            "Micro-via drill diameter in mm for the in-pad fallback "
            "(default: 0.15).  Stamps KICAD_TOOLS_MICRO_VIA_DRILL.  Only "
            "used with --micro-via-in-pad-fallback."
        ),
    )
    parser.add_argument(
        "--show-congestion",
        action="store_true",
        help=(
            "Show pre-route RUDY congestion estimation before routing begins. "
            "Displays an ASCII heatmap of predicted congestion per tile, useful "
            "for diagnosing routing failures caused by congestion hotspots."
        ),
    )

    args = parser.parse_args(argv)

    # Issue #4280: hard compatibility gate for --route-engine mesh|lattice.
    # Runs before ANY other work (env stamping, board loading, escalation
    # dispatch) so an inert engine flag can never silently ship grid copper.
    # This single chokepoint covers every routing path -- all five
    # load_pcb_for_routing sites and the escalation wrappers are reached
    # from main().  Strict no-op for --route-engine grid (the default).
    _engine_gate_rc = _validate_route_engine_strategy(args)
    if _engine_gate_rc != 0:
        return _engine_gate_rc

    # Issue #3033 / #3062: When --strict-in-pad-clearance is set, stamp the
    # env var so EscapeRouter (lazily constructed several layers below the
    # CLI) reads the same opt-in state.  See escape.py's __init__ for the
    # env-var read site.  Defaults to "0" so absence preserves the legacy
    # "proceed anyway" behaviour bit-for-bit.
    import os as _os

    if getattr(args, "strict_in_pad_clearance", False):
        _os.environ["KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE"] = "1"

    # Issue #3118: When --micro-via-in-pad-fallback is set, stamp the env vars
    # so EscapeRouter (lazily constructed several layers below the CLI) reads
    # the opt-in plus the micro-via dimensions.  Defaults preserve legacy
    # behaviour bit-for-bit (the env var read in escape.py defaults to "0").
    if getattr(args, "micro_via_in_pad_fallback", False):
        _os.environ["KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK"] = "1"
        _os.environ["KICAD_TOOLS_MICRO_VIA_SIZE"] = str(getattr(args, "micro_via_size", 0.3))
        _os.environ["KICAD_TOOLS_MICRO_VIA_DRILL"] = str(getattr(args, "micro_via_drill", 0.15))

    # Issue #2589: Seed the global ``random`` module for reproducible runs.
    # When ``--seed`` is supplied, all unseeded ``random.shuffle`` /
    # ``random.sample`` callsites in the negotiated router
    # (``algorithms/negotiated.py`` escape strategies, ``core.py`` MST trial
    # shuffle) and any other unseeded global-random consumers downstream
    # become deterministic.  This is the primary fix for board-03 run-to-run
    # variance.  When ``--seed`` is omitted, the global RNG is left at its
    # default os.urandom-derived state -- existing behaviour is preserved.
    if args.seed is not None:
        random.seed(args.seed)
        if not getattr(args, "quiet", False):
            print(f"[seed] Seeded global random with --seed {args.seed}")

    # Issue #3538: normalize --deterministic-budget BEFORE any wall-clock
    # deadline is stamped or any inner router routine reads per_net_timeout /
    # max_search_iterations off ``args``.  This disables the per-net wall-clock
    # cutoff and pins the C++ A* iteration backstop so the routed output is
    # reproducible across machines.  No-op when the flag is unset.
    _normalize_deterministic_budget(args, quiet=getattr(args, "quiet", False))

    # Resolve two-phase iteration count.
    # Priority: --two-phase-iterations (explicit) > --iterations (explicit) > 20 (default)
    _TWO_PHASE_DEFAULT = 20
    _two_phase_iters_explicit = getattr(args, "two_phase_iterations", None) is not None
    _iterations_explicitly_set = args.iterations != parser.get_default("iterations")
    if not _two_phase_iters_explicit:
        if _iterations_explicitly_set:
            args.two_phase_iterations = args.iterations
        else:
            args.two_phase_iterations = _TWO_PHASE_DEFAULT

    # Validate input
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Warning: Expected .kicad_pcb file, got {pcb_path.suffix}")

    # Issue #4148: parse + validate --region (SPATIAL routing bound).  Stores a
    # normalized board-relative box on ``args._region_box`` that the
    # ``load_pcb_for_routing`` call sites forward.  Region mode implies
    # --preserve-existing so copper outside the box is loaded as fixed
    # obstacles and re-emitted unchanged.  Degenerate / non-numeric boxes are
    # rejected here (mirroring the ``pcb strip --region`` CLI validation) so
    # no routing budget is spent on an invalid request.
    args._region_box = None
    # Issue #4170 (Phase 2b-1): board-relative boundary stub terminals detected
    # during region validation, forwarded to load_pcb_for_routing so their tip
    # cells are carved open as same-net reconnection targets.  None when no
    # --region or no stubs.
    args._stub_terminals = None
    _region_arg = getattr(args, "region", None)
    if _region_arg:
        rc = _parse_and_apply_region(args, pcb_path, _region_arg)
        if rc != 0:
            return rc

    # Issue #3154: advisory schematic/PCB drift banner.  Non-blocking -- the
    # hard gate lives behind 'kct check --netlist-sync'.  Skips silently when
    # no schematic is discovered, when in sync, or when --no-sync-check / --quiet.
    if getattr(args, "sync_check", True) and not getattr(args, "quiet", False):
        try:
            from kicad_tools.sync.drift import analyze_drift, format_drift_banner

            _drift_analysis, _ = analyze_drift(pcb_path, getattr(args, "schematic", None))
            if _drift_analysis is not None:
                _banner = format_drift_banner(_drift_analysis, pcb_path)
                if _banner:
                    print(_banner)
        except Exception:
            # Drift detection is advisory; never let it block routing.
            pass

    # Issue #4156: hard off-board placement preflight.  A footprint whose
    # courtyard falls outside the Edge.Cuts outline can never route (its nets
    # fail outright), and the failure signature is indistinguishable from
    # congestion — which is exactly what cost multiple wasted routing passes in
    # the field.  Abort before any router/component loading unless the user
    # opted out with --allow-offboard.  The check is O(footprints), computed
    # once, and reuses the same get_board_outline()-based analysis as
    # 'kct placement check'.
    if not getattr(args, "allow_offboard", False):
        rc = _offboard_preflight(pcb_path)
        if rc != 0:
            return rc

    # Issue #2996: Validate and load the optional --net-class-map sidecar
    # early -- before dispatching to any of the route_with_* sub-flows --
    # so the error paths (missing file / malformed JSON / invalid structure)
    # short-circuit with exit 1 and a clear message regardless of which
    # routing path the args select.  The loaded map is stashed on
    # ``args._loaded_net_class_map`` for downstream consumers (each
    # ``load_pcb_for_routing`` site merges it into ``router.net_class_map``).
    args._loaded_net_class_map = None
    if getattr(args, "net_class_map", None) is not None:
        import json as _ncm_json

        from kicad_tools.router.rules import net_class_map_from_dict

        ncm_path = Path(args.net_class_map).resolve()
        if not ncm_path.exists():
            print(f"Error: net-class-map file not found: {ncm_path}", file=sys.stderr)
            return 1
        try:
            _ncm_data = _ncm_json.loads(ncm_path.read_text())
        except _ncm_json.JSONDecodeError as e:
            print(f"Error parsing net-class-map JSON: {e}", file=sys.stderr)
            return 1
        try:
            args._loaded_net_class_map = net_class_map_from_dict(_ncm_data)
        except (TypeError, ValueError) as e:
            print(f"Error: invalid net-class-map structure: {e}", file=sys.stderr)
            return 1

    # Normalize --auto-fix-passes: explicit value implies --auto-fix
    if args.auto_fix_passes is not None:
        if args.auto_fix_passes < 1:
            print("Error: --auto-fix-passes must be at least 1", file=sys.stderr)
            return 1
        args.auto_fix = True
    else:
        # Default to 3 passes when --auto-fix is used without explicit --auto-fix-passes
        args.auto_fix_passes = 3

    # Issue #2802 / #3238: Stamp a single monotonic wall-clock deadline derived
    # from ``--timeout`` onto ``args`` so every orchestration site (layer-
    # escalation loop, rule-relaxation tiers, combined-escalation 2D search,
    # placement feedback, auto-fix passes, inner negotiated/two-phase/escape
    # calls) shares the same budget rather than receiving a fresh per-stage
    # copy of ``args.timeout``.
    #
    # Order note (issue #3238): this MUST run after the --auto-fix-passes
    # normalization above, because the deadline helper consults
    # ``_should_auto_fix(args)`` to decide whether to reserve an auto-fix
    # budget.  Reordering this call to run earlier silently disables the
    # reserve for users who pass ``--auto-fix-passes N`` without also
    # passing ``--auto-fix`` explicitly.
    #
    # See ``_set_wall_clock_deadline`` / ``_remaining_budget`` /
    # ``_deadline_expired`` / ``_auto_fix_budget`` for the helpers that
    # consume it.
    _set_wall_clock_deadline(args)
    # Issue #3238: initialize the structured auto-fix status field to
    # ``"not_invoked"`` so the final exit-code branches can distinguish
    # "auto-fix never reached" (drc-clean route, or --skip-drc) from
    # "auto-fix was skipped due to deadline" (the failure mode we now
    # surface with exit code 7).
    args._auto_fix_status = "not_invoked"

    # Issue #2388: --auto-layers is now enabled by default.  When --layers
    # is explicitly set, silently disable auto-escalation so existing
    # users of --layers see no behavior change.  Only raise an error if
    # the user *explicitly* typed both --auto-layers and --layers
    # (a true conflict in intent).
    _argv_for_detect = argv if argv is not None else sys.argv
    explicit_auto_layers = "--auto-layers" in _argv_for_detect
    if args.auto_layers and args.layers != "auto":
        if explicit_auto_layers:
            print(
                f"Error: --auto-layers cannot be used with --layers {args.layers}.\n"
                "Use --auto-layers alone, or use --layers to specify a "
                "fixed layer count (and pass --no-auto-layers to silence "
                "this error).",
                file=sys.stderr,
            )
            return 1
        # --layers was explicit but --auto-layers was the default; honor --layers.
        args.auto_layers = False

    # Validate --adaptive-rules is not used with explicit --layers (unless also using --auto-layers)
    if args.adaptive_rules and args.layers != "auto" and not args.auto_layers:
        print(
            f"Error: --adaptive-rules cannot be used with --layers {args.layers}.\n"
            "Use --adaptive-rules alone, with --auto-layers, or use --layers for fixed config.",
            file=sys.stderr,
        )
        return 1

    # Validate min-completion is between 0 and 1
    if args.min_completion < 0 or args.min_completion > 1:
        print(
            f"Error: --min-completion must be between 0 and 1 (got {args.min_completion}).",
            file=sys.stderr,
        )
        return 1

    # Apply high-performance settings if requested
    if getattr(args, "high_performance", False):
        from kicad_tools.performance import get_performance_config

        perf_config = get_performance_config(high_performance=True)

        # Override defaults with high-performance settings
        if not args.quiet:
            print("\n--- High-Performance Mode ---")
            print(f"  CPU cores:         {perf_config.cpu_cores}")
            print(f"  Monte Carlo trials: {perf_config.monte_carlo_trials}")
            print(f"  Parallel workers:   {perf_config.parallel_workers}")
            print(f"  Max iterations:     {perf_config.negotiated_iterations}")
            if perf_config.calibrated:
                print(f"  (Using calibrated settings from {perf_config.calibration_date})")
            print()

        # Apply to routing parameters
        args.mc_trials = perf_config.monte_carlo_trials
        args.iterations = perf_config.negotiated_iterations
        # Also apply calibrated iterations to two-phase if not explicitly set
        if not _two_phase_iters_explicit:
            args.two_phase_iterations = perf_config.negotiated_iterations

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_routed")

    # Resolve grid value: "auto" or numeric
    # We need to resolve this early, before sub-functions are called
    grid_auto_result = None
    multi_res_plan = None
    if args.grid.lower() == "auto":
        from kicad_tools.router.io import (
            auto_select_grid_resolution,
            compute_multi_resolution_plan,
            extract_board_dimensions,
            extract_pad_positions,
        )

        if not args.quiet:
            print("\n--- Auto-selecting grid resolution ---")
        pad_positions = extract_pad_positions(pcb_path)
        board_dims = extract_board_dimensions(pcb_path)
        board_width = board_dims[0] if board_dims else None
        board_height = board_dims[1] if board_dims else None
        max_cells = getattr(args, "max_cells", 500_000)
        grid_auto_result = auto_select_grid_resolution(
            pads=pad_positions,
            clearance=args.clearance,
            board_width=board_width,
            board_height=board_height,
            max_cells=max_cells,
        )

        # When grid_strategy is adaptive (default), attempt multi-resolution
        grid_strategy = getattr(args, "grid_strategy", "adaptive")
        if grid_strategy == "adaptive":
            # compute_multi_resolution_plan needs full Pad objects (with ref)
            # Try loading them; fall back to uniform if not available
            try:
                from kicad_tools.router.io import load_pads_for_analysis

                full_pads = load_pads_for_analysis(pcb_path)
                multi_res_plan = compute_multi_resolution_plan(
                    pads=full_pads,
                    clearance=args.clearance,
                    board_width=board_width,
                    board_height=board_height,
                    max_cells=max_cells,
                )
            except Exception:
                # Fall back: try with pad positions (won't have ref info)
                multi_res_plan = compute_multi_resolution_plan(
                    pads=pad_positions,
                    clearance=args.clearance,
                    board_width=board_width,
                    board_height=board_height,
                    max_cells=max_cells,
                )

        if multi_res_plan is not None and multi_res_plan.is_multi_resolution:
            # Use coarse resolution for the global grid
            args.grid = multi_res_plan.coarse_resolution
            if not args.quiet:
                print(multi_res_plan.summary())
                print()
        else:
            # No fine-pitch components or uniform strategy requested
            multi_res_plan = None
            args.grid = grid_auto_result.resolution
            if not args.quiet:
                print(grid_auto_result.summary())
                print()

        # Store grid origin offset from auto-selection for DesignRules
        args._grid_origin_offset = grid_auto_result.origin_offset

        # Issue #3911: gate on the memory-forced unsafe grid.  When the memory
        # budget cap forced auto-grid coarser than clearance/2 (while a finer
        # safe candidate existed and this was NOT a deliberate #3441 lattice
        # rescue), the router's own safety rule (min_res = clearance/2) rejects
        # the grid and routing reliably produces cross-net clearance shorts.
        # Refuse to route rather than silently ship shorts; require an explicit
        # opt-in (--allow-unsafe-grid or --force) to override.
        if grid_auto_result.memory_forced_unsafe_grid and not (
            args.force or getattr(args, "allow_unsafe_grid", False)
        ):
            recommended = args.clearance / 2
            print(
                f"Error: Auto-grid selected {grid_auto_result.resolution}mm > "
                f"clearance/2 ({recommended}mm) because the memory budget cap "
                f"(max_cells={grid_auto_result.memory_budget_used:,}) forced a "
                f"coarser grid.\n"
                f"The router's own safety rule rejects this grid; routing WILL "
                f"produce clearance-violating vias/segments (DRC shorts).\n"
                f"An unrouted net is strictly safer than a short.\n\n"
                f"Options:\n"
                f"  1. Enlarge or split the board (fewer cells per mm^2)\n"
                f"  2. Add routing layers (--auto-layers / --starting-layers)\n"
                f"  3. Loosen the manufacturer clearance (looser --manufacturer "
                f"profile or larger --clearance)\n"
                f"  4. Pass --allow-unsafe-grid (or --force) to route anyway and "
                f"accept the DRC risk (not recommended)\n",
                file=sys.stderr,
            )
            return 1
    else:
        try:
            args.grid = float(args.grid)
        except ValueError:
            print(
                f"Error: Invalid grid value '{args.grid}'. Use a number (e.g., 0.25) or 'auto'.",
                file=sys.stderr,
            )
            return 1

    # Handle cache-related commands early
    if args.cache_stats:
        from kicad_tools.router import RoutingCache

        cache = RoutingCache()
        stats = cache.stats()
        print("\n--- Routing Cache Statistics ---")
        print(f"  Cache directory:     {stats['cache_dir']}")
        print(f"  Routing results:     {stats['routing_results_count']}")
        print(f"  Partial net routes:  {stats['partial_routes_count']}")
        print(f"  Total size:          {stats['total_size_mb']:.2f} MB")
        print(f"  Valid results:       {stats['valid_results']}")
        print(f"  Expired results:     {stats['expired_results']}")
        print(f"  TTL:                 {stats['ttl_days']} days")
        print(f"  Max size:            {stats['max_size_mb']:.0f} MB")
        if stats["oldest"]:
            print(f"  Oldest entry:        {stats['oldest']}")
        if stats["newest"]:
            print(f"  Newest entry:        {stats['newest']}")
        return 0

    if args.clear_cache:
        from kicad_tools.router import RoutingCache

        cache = RoutingCache()
        count = cache.clear()
        if not args.quiet:
            print(f"Cleared {count} entries from routing cache")

    # Auto-apply edge clearance from manufacturer when not explicitly set.
    # This ensures --manufacturer jlcpcb automatically enforces the 0.3mm
    # copper-to-edge clearance without requiring a separate --edge-clearance flag.
    if args.edge_clearance is None:
        from kicad_tools.router.mfr_limits import get_mfr_limits

        try:
            _mfr = get_mfr_limits(args.manufacturer)
            if _mfr.min_edge_clearance > 0:
                args.edge_clearance = _mfr.min_edge_clearance
                if not args.quiet:
                    print(
                        f"Edge clearance: {args.edge_clearance}mm "
                        f"(from {args.manufacturer} manufacturer limits)"
                    )
        except ValueError:
            pass  # Unknown manufacturer -- edge_clearance stays None

    # Issue #3400: resolve the effective ``starting_layers`` value with the
    # precedence CLI flag > project.kct EscalationPolicy field > default 2.
    # This runs before every escalation dispatch so all paths share the
    # same source of truth (size-first, layers-only, mfr-tier, combined,
    # plain --auto-layers).  Validation is performed against the resolved
    # value of ``args.max_layers`` so we catch the structural error
    # (starting > ceiling) up front rather than producing an empty ladder.
    _resolve_starting_layers(pcb_path, args)
    if args.starting_layers > args.max_layers:
        print(
            f"Error: --starting-layers={args.starting_layers} must be "
            f"<= --max-layers={args.max_layers}.  Lower the floor or "
            "raise the ceiling.",
            file=sys.stderr,
        )
        return 2

    # Issue #3352 (P_AS3): --auto-pcb-size wraps the layer escalation path
    # with an outer size-escalation loop.  Q5 decision: --auto-pcb-size
    # IMPLIES --auto-layers unless --no-auto-layers was explicit (the
    # layers-first ladder is meaningless when layers can't escalate).
    # Per Q4: auto-load project.kct EscalationPolicy + envelope_hard +
    # mounting_hole_group when a project.kct sits next to the PCB.
    if getattr(args, "auto_pcb_size", False):
        # Q5 enforcement: --auto-pcb-size implies --auto-layers unless
        # the user explicitly passed --no-auto-layers.
        _argv_for_q5 = argv if argv is not None else sys.argv
        _explicit_no_auto_layers = "--no-auto-layers" in _argv_for_q5
        if not _explicit_no_auto_layers and not args.auto_layers:
            args.auto_layers = True
            if not args.quiet:
                print(
                    "  (Q5: --auto-pcb-size implies --auto-layers; "
                    "pass --no-auto-layers to opt out.)"
                )

        # Best-effort project.kct discovery.  When present, load the
        # MechanicalRequirements.envelope_hard + mounting_hole_group + the
        # ManufacturingRequirements.escalation policy and stash them on
        # args so route_with_size_escalation can consume them.
        _load_project_kct_for_escalation(pcb_path, args)

        return route_with_size_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )

    # Issue #2881: --auto-mfr-tier wraps --auto-layers / --adaptive-rules,
    # iterating over registered manufacturer tiers from cheapest -> tightest.
    # Inside each tier the routing dispatches to the existing layer-escalation
    # path (or the single-layer path when --no-auto-layers is set).
    if getattr(args, "auto_mfr_tier", False):
        return route_with_mfr_tier_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )

    # Handle auto-layers mode (separate code path)
    if args.auto_layers and args.adaptive_rules:
        # Combined 2D search: layers + rules
        return route_with_combined_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )
    elif args.auto_layers:
        return route_with_layer_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )
    elif args.adaptive_rules:
        # Adaptive rules only (fixed layer count)
        return route_with_rule_relaxation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Auto-create copper pours for power nets (before skip detection).
    # auto_pour_if_missing writes in-place; stage a copy at output_path
    # first so the user's INPUT is left untouched (issue #2548).
    # Issue #3092: forward user-supplied skip_nets as force_pour_nets so
    # an all-power board (e.g. board 01 VIN/VOUT/GND) still emits a zone
    # for any net the user explicitly committed to pouring.
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _stage_input_for_auto_pour(pcb_path, output_path)
        auto_pour_if_missing(
            pcb_path,
            quiet=args.quiet,
            edge_clearance=getattr(args, "edge_clearance", None),
            force_pour_nets=skip_nets,
        )

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=args.quiet)

    # Issue #3155: capture preserved copper once before routing/checkpoints.
    _preserve = bool(getattr(args, "preserve_existing", False))
    _preserved_routes = _capture_preserved_routes(pcb_path) if _preserve else []
    _preserved_sexp = _serialize_preserved_routes(_preserved_routes) if _preserve else ""

    # Import router modules
    from kicad_tools.analysis import ComplexityAnalyzer, ComplexityRating
    from kicad_tools.router import (
        BusRoutingConfig,
        BusRoutingMode,
        DesignRules,
        DifferentialPairConfig,
        LayerStack,
        RoutabilityAnalyzer,
        ensure_cpp_backend_available,
        load_pcb_for_routing,
        print_routing_diagnostics_json,
        show_routing_summary,
    )
    from kicad_tools.router.io import detect_layer_stack
    from kicad_tools.schema.pcb import PCB

    # Handle backend selection (auto-build C++ extension on first use; #2549)
    ok, force_python, exit_code = ensure_cpp_backend_available(
        backend=args.backend,
        quiet=getattr(args, "quiet", False),
        allow_auto_build=not getattr(args, "no_auto_build_native", False),
    )
    if not ok:
        return exit_code if exit_code is not None else 1

    # Grid resolution already resolved early in main()
    # (args.grid is now a float, grid_auto_result set if "auto" was used)

    # Validate grid resolution vs clearance (prevents DRC violations)
    # Skip validation for auto mode since auto_select_grid_resolution ensures DRC compliance
    if grid_auto_result is None and args.grid > args.clearance:
        recommended_grid = args.clearance / 2
        if not args.force:
            print(
                f"Error: Grid resolution {args.grid}mm exceeds clearance {args.clearance}mm.\n"
                f"This WILL cause DRC violations.\n\n"
                f"Options:\n"
                f"  1. Use a finer grid: --grid {recommended_grid}\n"
                f"  2. Use --grid auto for automatic selection\n"
                f"  3. Use --force to override (not recommended)\n",
                file=sys.stderr,
            )
            return 1
        else:
            # User forced, continue with warning
            print(
                f"Warning: Grid resolution {args.grid}mm exceeds clearance {args.clearance}mm.\n"
                f"Proceeding anyway due to --force flag. Expect DRC violations.",
                file=sys.stderr,
            )

    # Create layer stack from --layers argument (or auto-detect)
    if args.layers == "auto":
        # Auto-detect layer stack from PCB file
        pcb_text = pcb_path.read_text()
        layer_stack = detect_layer_stack(pcb_text)
    else:
        layer_stack_map = {
            "2": LayerStack.two_layer(),
            "4": LayerStack.four_layer_sig_gnd_pwr_sig(),
            "4-sig": LayerStack.four_layer_sig_sig_gnd_pwr(),
            "4-all": LayerStack.four_layer_all_signal(),
            "6": LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig(),
        }
        layer_stack = layer_stack_map[args.layers]

    # Configure design rules
    grid_origin_offset = getattr(args, "_grid_origin_offset", (0.0, 0.0))
    fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
    rules = DesignRules(
        grid_resolution=args.grid,
        grid_origin_offset=grid_origin_offset,
        trace_width=args.trace_width,
        trace_clearance=args.clearance,
        via_drill=args.via_drill,
        via_diameter=args.via_diameter,
        fine_pitch_clearance=fine_pitch_cl,
        # Issue #2605: forward manufacturer so the escape router can opt in
        # to in-pad escape for fine-pitch SSOP/TSSOP when the manufacturer
        # supports via-in-pad processing.
        manufacturer=getattr(args, "manufacturer", None),
        # Issue #2891: forward escalation-in-progress flag so the escape
        # router demotes the #2880 ERROR log when an outer wrapper will
        # retry on a tier that supports via-in-pad.
        auto_mfr_tier_in_progress=getattr(args, "_auto_mfr_tier_in_progress", False),
    )

    # Import progress helpers
    from kicad_tools.cli.progress import flush_print, spinner

    quiet = args.quiet

    # Print header (unless quiet)
    if not quiet:
        print("=" * 60)
        print("KiCad PCB Autorouter")
        print("=" * 60)
        print(f"Input:    {pcb_path}")
        print(f"Output:   {output_path}")
        print(f"Strategy: {args.strategy}")
        print(f"Layers:   {layer_stack.name} ({layer_stack.num_layers} layers)")
        if skip_nets:
            print(f"Skip:     {', '.join(skip_nets)}")
        if args.bus_routing:
            print(f"Bus:      enabled ({args.bus_mode} mode)")
        if args.differential_pairs:
            print("DiffPair: enabled")

        if args.edge_clearance:
            print(f"Edge:     {args.edge_clearance}mm clearance")
        if args.verbose:
            print("\nDesign Rules:")
            grid_mode = " (auto)" if grid_auto_result else ""
            print(f"  Grid resolution: {rules.grid_resolution}mm{grid_mode}")
            print(f"  Trace width:     {rules.trace_width}mm")
            print(f"  Clearance:       {rules.trace_clearance}mm")
            print(f"  Via drill:       {rules.via_drill}mm")
            print(f"  Via diameter:    {rules.via_diameter}mm")
            if args.edge_clearance:
                print(f"  Edge clearance:  {args.edge_clearance}mm")

            print(f"\nLayer Stack ({layer_stack.name}):")
            signal_layers = [lyr.name for lyr in layer_stack.signal_layers]
            plane_layers = [f"{lyr.name} ({lyr.plane_net})" for lyr in layer_stack.plane_layers]
            print(f"  Signal layers:  {', '.join(signal_layers)}")
            if plane_layers:
                print(f"  Plane layers:   {', '.join(plane_layers)}")

    # Issue #4263: analytical --dry-run short-circuit.
    #
    # A plain ``--dry-run`` only needs the strategy/grid selection verdict, so
    # report it analytically and return BEFORE ``load_pcb_for_routing`` (which
    # allocates the full RoutingGrid at route_cmd's load site and, on a large
    # board, is the >45s / OOM step).  ``args.grid`` is already a float here
    # (explicit value, or resolved from ``--grid auto`` above) and
    # ``layer_stack`` is known, so the plan reuses the allocation-free
    # selection math without touching the router.
    #
    # ``--analyze --dry-run`` is intentionally left to the downstream
    # complexity/routability path (route_cmd:~10183), which needs a loaded
    # ``router``; only the plain dry run short-circuits here.
    if args.dry_run and not args.analyze:
        dry_run_plan = compute_dry_run_grid_plan(
            pcb_path=pcb_path,
            selected_grid=args.grid,
            clearance=args.clearance,
            num_layers=layer_stack.num_layers,
            max_cells=getattr(args, "max_cells", 500_000),
        )
        if dry_run_plan is not None:
            if not quiet:
                print(format_dry_run_grid_plan(dry_run_plan))
            return 0
        # No detectable board outline: fall through to the normal load path
        # rather than guess dimensions (the pre-#4263 dry-run behavior).

    # Load PCB
    if not quiet:
        flush_print("\n--- Loading PCB ---")
    try:
        with spinner("Loading PCB...", quiet=quiet):
            router, net_map = load_pcb_for_routing(
                str(pcb_path),
                skip_nets=skip_nets,
                rules=rules,
                edge_clearance=args.edge_clearance,
                layer_stack=layer_stack,
                force_python=force_python,
                # Issue #4268: thread the mesh-router strategy selector through.
                strategy=getattr(args, "route_engine", "grid"),
                validate_drc=not args.force,
                strict_drc=False,  # Only fail on hard constraint (grid > clearance)
                # Issue #3155: incremental routing (see route_with_layer_escalation).
                load_existing_routes=getattr(args, "preserve_existing", False),
                # Issue #4148: region-bounded routing (see main()).
                region=getattr(args, "_region_box", None),
                # Issue #4170 (Phase 2b-1): board-relative boundary stub
                # terminals whose tip cells are carved open as same-net
                # reconnection targets (None when no --region / no stubs).
                stub_terminals=getattr(args, "_stub_terminals", None),
                # Issue #2610: thread --max-search-iterations through.
                # The inner parser declares this flag with default=0 (Issue
                # #2819), so the attribute is guaranteed to exist; the
                # ``or 0`` guards against an explicit ``--max-search-iterations 0``
                # being treated as falsy (which is the intended behaviour:
                # 0 means "use the cols*rows*4 heuristic").
                max_search_iterations=args.max_search_iterations or 0,
                # Issue #3881: thread the tuned per-net iteration cap through.
                # Defaulted by --deterministic-budget normalization above; 0 =
                # unset (no per-net cap, legacy behaviour).
                per_net_iterations=getattr(args, "per_net_iterations", 0) or 0,
            )
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Issue #2996: merge --net-class-map sidecar onto router's map.
    _apply_net_class_map_sidecar(router, args, quiet=quiet)
    # Issue #3470: thread --max-ripups-per-net into the destructive
    # rip-up budgets (route_all + two-phase stall recovery).
    _apply_ripup_budget_override(router, args)
    _apply_rescue_pass_override(router, args)
    _apply_bundle_river_planner(router, args)
    _apply_monotone_certificate_order(router, args)
    _apply_cross_package_pair_corridor(router, args)
    _apply_slack_corridor_widening(router, args)

    # Issue #3171: inject boosted analog routing class for --analog-nets /
    # --auto-analog selected nets (pour/ground nets are left untouched).
    _apply_analog_net_class(router, args, quiet=quiet)

    # Issue #3371 (P_FP3): surface fine-pitch escape regions installed by
    # ``load_pcb_for_routing``.
    _log_fine_pitch_escape_regions(router, quiet=quiet)

    # Issue #3897: wire the previously-orphaned RoutingOptimizer.optimize_net_order
    # into ``kct route`` via ``--order-method``.  The optimizer evaluates the
    # candidate order with a throw-away full route, so we hand it a factory that
    # builds a FRESH router (identical load parameters) to keep the real
    # ``router`` above pristine.  Strict no-op when --order-method is absent.
    if getattr(args, "order_method", None) is not None:

        def _order_router_factory() -> "Autorouter":
            fresh, _ = load_pcb_for_routing(
                str(pcb_path),
                skip_nets=skip_nets,
                rules=rules,
                edge_clearance=args.edge_clearance,
                layer_stack=layer_stack,
                force_python=force_python,
                # Issue #4268: thread the mesh-router strategy selector through.
                strategy=getattr(args, "route_engine", "grid"),
                validate_drc=not args.force,
                strict_drc=False,
                load_existing_routes=getattr(args, "preserve_existing", False),
                # Issue #4148: region-bounded routing (see main()).
                region=getattr(args, "_region_box", None),
                # Issue #4170 (Phase 2b-1): board-relative boundary stub
                # terminals whose tip cells are carved open as same-net
                # reconnection targets (None when no --region / no stubs).
                stub_terminals=getattr(args, "_stub_terminals", None),
                max_search_iterations=args.max_search_iterations or 0,
                per_net_iterations=getattr(args, "per_net_iterations", 0) or 0,
            )
            _apply_net_class_map_sidecar(fresh, args, quiet=True)
            _apply_analog_net_class(fresh, args, quiet=True)
            return fresh

        _apply_order_method(
            router,
            args,
            router_factory=_order_router_factory,
            quiet=quiet,
        )

    # Pass fine zones from multi-resolution plan to the router (Issue #1828).
    # This enables SubGridRouter to use fine-grid resolution for escape
    # routing of pads within dense IC packages (e.g. SSOP at 0.05mm)
    # instead of the coarse global grid (e.g. 0.17mm).
    if multi_res_plan is not None and multi_res_plan.is_multi_resolution:
        router.fine_zones = list(multi_res_plan.fine_zones)
        if not quiet:
            flush_print(f"  Fine zones: {len(router.fine_zones)} (sub-grid escape routing enabled)")

    # Issue #1841: Tell the autorouter which pour nets lack zones
    router._pour_nets_without_zones = set(_no_zone)

    # Set up Ctrl+C handling to save partial results
    _interrupt_state["router"] = router
    _interrupt_state["output_path"] = output_path
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["interrupted"] = False
    signal.signal(signal.SIGINT, _handle_interrupt)

    # Count nets by category for accurate status reporting (Issue #812)
    # - Multi-pad nets: 2+ pads, need actual routing
    # - Single-pad nets: 1 pad, trivially complete (no routing needed)
    # - Power nets: skipped via skip_nets, handled by copper pours
    #
    # Issue #3942 (Bug B): pour-served multi-pad nets that the router
    # strips via ``_filter_pour_nets`` are excluded from the denominator
    # by ``_routable_multi_pad_nets`` so the routed/total summary counts
    # only the nets the router was actually asked to route.  Without this
    # a pour net the CLI's zone regex missed (kept ``net_num > 0``) but the
    # router's net-class flagged as pour was counted-but-never-routed,
    # yielding a spurious ``PARTIAL: Routed 1/2`` on a fully-routed board.
    multi_pad_nets = _routable_multi_pad_nets(router)
    single_pad_nets = [
        net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) == 1
    ]
    nets_to_route = len(multi_pad_nets)  # Only routable multi-pad nets need routing
    power_nets_skipped = len(skip_nets)

    if not quiet:
        flush_print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
        backend_info = router.backend_info
        grid_cells = router.grid.cols * router.grid.rows * router.grid.num_layers
        from kicad_tools.router.cpp_backend import format_backend_status

        backend_status = format_backend_status(backend_info, grid_cells)
        flush_print(f"  Backend:    {backend_status}")
        flush_print(
            f"  Grid:       {router.grid.cols}x{router.grid.rows}x{router.grid.num_layers} = {grid_cells:,} cells"
        )
        flush_print(f"  Total nets: {len(net_map)}")
        flush_print(f"  Nets to route: {nets_to_route} (multi-pad signal nets)")

        if args.verbose:
            print("\n  Net breakdown:")
            for net_name, net_num in sorted(net_map.items(), key=lambda x: x[1]):
                if net_name and net_name not in skip_nets:
                    pad_count = len(router.nets.get(net_num, []))
                    print(f"    {net_name}: {pad_count} pads")

    # Surface single-pad signal nets as a top-of-output warning before
    # routing starts.  These nets are structurally unroutable -- the router
    # silently skips them, which makes "13/13 routed, DRC clean" look like
    # a successful build even when 4 SWD signals are floating.  See
    # `kct check --only single_pad_net` for the full DRC-style report.
    if not quiet:
        _emit_single_pad_net_warning(router, single_pad_nets)

    # Analyze fine-pitch components for grid compatibility warnings
    # This runs automatically to warn users about potential routing issues
    if not quiet:
        from kicad_tools.router.fine_pitch import analyze_fine_pitch_components
        from kicad_tools.router.output import show_fine_pitch_warnings

        fine_pitch_report = analyze_fine_pitch_components(
            pads=router.pads,
            grid_resolution=args.grid,
            trace_width=args.trace_width,
            clearance=args.clearance,
        )
        if fine_pitch_report.has_warnings:
            # Issue #3441: ``use_waypoint_injection`` is backend-aware --
            # waypoint injection (#2330) only exists in the pure-Python
            # pathfinder.  Under the C++ backend it reports False and the
            # sub-grid escape pre-pass + PIN_ACCESS retry (#1603) are the
            # active off-grid recovery mechanisms, so the banner names the
            # mechanism that will actually run.
            if router.use_waypoint_injection:
                # Waypoint injection handles off-grid pads by injecting their
                # exact positions into the A* search graph, so the grid-alignment
                # warnings are misleading.  Show a brief summary instead.
                if fine_pitch_report.total_off_grid > 0:
                    flush_print(
                        f"\n  {fine_pitch_report.total_off_grid} pads off-grid; "
                        "waypoint injection will handle pad connections "
                        "(Python pathfinder)"
                    )
                # Still show full per-component detail at verbose (-v)
                if args.verbose:
                    flush_print("\n--- Fine-Pitch Component Analysis (verbose) ---")
                    show_fine_pitch_warnings(fine_pitch_report, quiet=quiet, verbose=True)
            else:
                if fine_pitch_report.total_off_grid > 0:
                    flush_print(
                        f"\n  {fine_pitch_report.total_off_grid} pads off-grid; "
                        "A* lands on pad metal directly, with the sub-grid "
                        "escape pre-pass (uncovered pads) + PIN_ACCESS retry "
                        "as recovery (waypoint injection unavailable on this "
                        "backend)"
                    )
                flush_print("\n--- Fine-Pitch Component Analysis ---")
                show_fine_pitch_warnings(fine_pitch_report, quiet=quiet, verbose=args.verbose)

    # Show pre-route RUDY congestion estimation (Issue #2278)
    if getattr(args, "show_congestion", False):
        try:
            estimator = router._ensure_congestion_estimator()
            if not quiet:
                flush_print("\n--- Pre-Route Congestion Estimation (RUDY) ---")
            if args.format == "json":
                import json as _json

                print(_json.dumps(estimator.format_json(), indent=2))
            else:
                print(estimator.format_ascii_heatmap())
                # Summary stats
                max_demand = max(
                    (
                        estimator.demand[r][c]
                        for r in range(estimator.grid.rows)
                        for c in range(estimator.grid.cols)
                    ),
                    default=0.0,
                )
                scored_nets = [(nid, s) for nid, s in estimator.net_scores.items() if s > 0]
                scored_nets.sort(key=lambda x: -x[1])
                print(f"\n  Peak tile demand: {max_demand:.2f}")
                print(f"  Nets with congestion score: {len(scored_nets)}")
                if scored_nets and not quiet:
                    print("  Top congested nets:")
                    for nid, score in scored_nets[:5]:
                        name = router.net_names.get(nid, f"Net {nid}")
                        print(f"    {name}: {score:.3f}")
        except Exception as e:
            if not quiet:
                print(f"  Warning: Congestion estimation failed: {e}", file=sys.stderr)

    # Analyze routability if requested
    if args.analyze:
        # Run pre-routing complexity analysis first
        if not quiet:
            print("\n--- Pre-Routing Complexity Analysis ---")
        try:
            pcb_for_analysis = PCB.load(str(pcb_path))
            complexity_analyzer = ComplexityAnalyzer()
            complexity = complexity_analyzer.analyze(pcb_for_analysis)

            # Show complexity summary
            print(f"\n{'=' * 60}")
            print("COMPLEXITY ANALYSIS")
            print(f"{'=' * 60}")
            print(f"Board: {complexity.board_width_mm:.1f}mm x {complexity.board_height_mm:.1f}mm")
            print(f"Pads: {complexity.total_pads}, Nets: {complexity.total_nets}")

            # Show complexity rating with color
            rating_symbols = {
                ComplexityRating.TRIVIAL: "[TRIVIAL]",
                ComplexityRating.SIMPLE: "[SIMPLE]",
                ComplexityRating.MODERATE: "[MODERATE]",
                ComplexityRating.COMPLEX: "[COMPLEX]",
                ComplexityRating.EXTREME: "[EXTREME]",
            }
            print(
                f"Complexity: {complexity.overall_score:.0f}/100 - "
                f"{rating_symbols[complexity.complexity_rating]}"
            )

            # Show layer predictions
            print("\nLayer Predictions:")
            for pred in complexity.layer_predictions:
                rec_str = " (recommended)" if pred.recommended else ""
                print(
                    f"  {pred.layer_count} layers: {pred.success_probability * 100:.0f}% success{rec_str}"
                )

            # Show bottlenecks
            if complexity.bottlenecks:
                print(f"\nBottlenecks ({len(complexity.bottlenecks)}):")
                for bottleneck in complexity.bottlenecks[:3]:
                    print(f"  - {bottleneck.component_ref}: {bottleneck.description}")

            print(f"{'=' * 60}")
        except Exception as e:
            print(f"Warning: Complexity analysis failed: {e}", file=sys.stderr)

        if not quiet:
            print("\n--- Routability Analysis ---")
        try:
            analyzer = RoutabilityAnalyzer(router)
            report = analyzer.analyze()

            # Print analysis report
            print(f"\n{'=' * 60}")
            print("ROUTABILITY ANALYSIS")
            print(f"{'=' * 60}")
            print(
                f"Estimated completion: {report.estimated_success_rate * 100:.0f}% "
                f"({report.expected_routable}/{report.total_nets} nets)"
            )

            # Show layer utilization
            if report.layer_utilization:
                print("\nLayer Utilization:")
                for layer_name, util in report.layer_utilization.items():
                    bar = "#" * int(util * 20)
                    print(f"  {layer_name:10s}: [{bar:20s}] {util * 100:.0f}%")

            # Show problem nets
            if report.problem_nets:
                print(f"\nProblem Nets ({len(report.problem_nets)}):")
                for net_report in report.problem_nets[:10]:  # Show first 10
                    print(f"\n  {net_report.net_name} ({net_report.pad_count} pads):")
                    print(f"    Severity: {net_report.severity.name}")
                    print(f"    Difficulty: {net_report.difficulty_score:.0f}/100")
                    if net_report.blocking_obstacles:
                        print("    Blocked by:")
                        for obs in net_report.blocking_obstacles[:5]:
                            print(f"      - {obs}")
                    if net_report.alternatives:
                        print("    Alternatives:")
                        for alt in net_report.alternatives[:3]:
                            print(f"      {alt}")
                    if net_report.suggestions:
                        print("    Suggestions:")
                        for sug in net_report.suggestions:
                            print(f"      - {sug}")

            # Show recommendations
            if report.recommendations:
                print("\nRecommendations:")
                for i, rec in enumerate(report.recommendations, 1):
                    print(f"  {i}. {rec}")

            print(f"{'=' * 60}")

            # If just analyzing, exit here
            if args.dry_run:
                return 0

        except Exception as e:
            print(f"Warning: Analysis failed: {e}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    # Configure bus routing if enabled
    bus_config = None
    if args.bus_routing:
        bus_mode_map = {
            "parallel": BusRoutingMode.PARALLEL,
            "stacked": BusRoutingMode.STACKED,
            "bundled": BusRoutingMode.BUNDLED,
        }
        bus_config = BusRoutingConfig(
            enabled=True,
            mode=bus_mode_map[args.bus_mode],
            spacing=args.bus_spacing,
            min_bus_width=args.bus_min_width,
        )

        # Show detected buses
        if args.verbose and not quiet:
            analysis = router.get_bus_analysis()
            if analysis["total_groups"] > 0:
                print(f"\n  Detected {analysis['total_groups']} bus groups:")
                for group in analysis["groups"]:
                    status = "complete" if group["complete"] else "partial"
                    print(f"    - {group['name']}: {group['width']} bits ({status})")
            else:
                print("\n  No bus signals detected")

    # Configure differential pair routing if enabled
    diffpair_config = None
    diffpair_warnings = []
    # Issue #4095: names of diff pairs that budget-exited coupled routing
    # and fell back to single-ended.  Accumulated from
    # ``router.diffpair_budget_exit_pair_names()`` after each
    # ``--differential-pairs`` dispatch so the report site can warn the
    # operator (fallback can regress completion / DRC on bundle-dense
    # boards; see #4095).  De-duplicated at report time.
    diffpair_budget_exit_pairs: list[str] = []
    if args.differential_pairs:
        diffpair_config = DifferentialPairConfig(
            enabled=True,
            spacing=args.diffpair_spacing,
            max_length_delta=args.diffpair_max_delta,
            # Issue #3275: forward the optional per-pair wall-clock
            # budget so callers (boards/07-matchgroup-test) can bound
            # the CoupledPathfinder's per-pair coupled A* search and
            # let pairs that exceed the budget fall through to the
            # main strategy.  Default ``None`` preserves the
            # unbounded behaviour the rest of the CLI relies on.
            per_pair_timeout=getattr(args, "diffpair_per_pair_timeout", None),
        )

        # Show detected differential pairs
        if args.verbose and not quiet:
            analysis = router.analyze_differential_pairs()
            if analysis["total_pairs"] > 0:
                print(f"\n  Detected {analysis['total_pairs']} differential pairs:")
                for pair in analysis["pairs"]:
                    print(
                        f"    - {pair['name']}: {pair['type']} "
                        f"(spacing={pair['spacing']}mm, max_delta={pair['max_delta']}mm)"
                    )
                if analysis["unpaired"]:
                    print(f"\n  Unpaired differential signals: {analysis['unpaired_signals']}")
                    for sig in analysis["unpaired"]:
                        print(f"    - {sig['net_name']} ({sig['polarity']})")
            else:
                print("\n  No differential pairs detected")

    # Check cache for existing routing result (unless --no-cache)
    cache_key = None
    cached_result = None
    use_cache = not args.no_cache

    if use_cache:
        from kicad_tools.router import CacheKey, RoutingCache

        try:
            # Compute cache key from PCB content and rules
            pcb_content = pcb_path.read_bytes()
            cache_key = CacheKey.compute(pcb_content, rules, args.grid)

            cache = RoutingCache()

            if not quiet:
                flush_print("\n--- Checking routing cache ---")

            cached_result = cache.get(cache_key)
            if cached_result is not None:
                if not quiet:
                    print(f"  Cache HIT: {cached_result.success_count} nets routed")
                    print(
                        f"  Segments: {cached_result.total_segments}, Vias: {cached_result.total_vias}"
                    )
                    print(f"  Original compute time: {cached_result.compute_time_ms}ms")

                # Deserialize and apply cached routes
                cached_routes = cache.deserialize_routes(cached_result.routes_data)

                # Apply cached routes to router
                router.routes = cached_routes

                if not quiet:
                    print("  Using cached routing result")
            else:
                if not quiet:
                    print(f"  Cache MISS (key: {cache_key.full_key[:32]}...)")
                if args.cache_only:
                    print(
                        "Error: --cache-only specified but no cached result found", file=sys.stderr
                    )
                    return 1
        except Exception as e:
            if not quiet:
                print(f"  Cache error: {e}")
            cached_result = None
            if args.cache_only:
                print("Error: --cache-only specified but cache lookup failed", file=sys.stderr)
                return 1

    # Track nets that needed clearance relaxation (for --progressive-clearance)
    relaxed_nets_report: dict[int, float] = {}
    routing_start_time = None

    # Route (skip if using cached result)
    if cached_result is not None:
        # Skip routing - using cached result
        if not quiet:
            flush_print("\n--- Using cached result (skipping routing) ---")
    else:
        # Route
        if not quiet:
            flush_print(f"\n--- Routing ({args.strategy}) ---")
            if args.timeout:
                flush_print(f"  Timeout: {args.timeout}s")
            per_net_timeout_val = getattr(args, "per_net_timeout", None)
            if per_net_timeout_val:
                flush_print(f"  Per-net timeout: {per_net_timeout_val}s")
            # Issue #2610: report the iteration backstop override if set.
            # Issue #2819: the inner parser now declares the flag (default=0),
            # so the attribute is guaranteed to exist; ``or 0`` preserves the
            # historic semantics (0 = use cols*rows*4 heuristic, suppress log).
            _max_iter_val = args.max_search_iterations or 0
            if _max_iter_val:
                flush_print(f"  Max search iterations: {_max_iter_val}")
            if args.profile:
                profile_output = args.profile_output or "route_profile.prof"
                flush_print(f"  Profiling enabled: {profile_output}")

        import time

        routing_start_time = time.time()

        # Resolve escape routing flag: True=force on, False=force off, None=auto-detect
        escape_routing_flag = _resolve_escape_routing_flag(args)

        # Build checkpoint callback once -- shared across every
        # route_all_negotiated call in this invocation (Issue #2808).
        # Returns None when --checkpoint-interval is 0 or unset, in which
        # case the router treats it as "no checkpointing".
        # The last_write_time is internal to the closure and persists
        # across all route_all_negotiated calls (placement-feedback iter
        # loops do NOT reset cadence, so back-to-back PF iterations can
        # share a single throttle window).
        _checkpoint_cb = _make_checkpoint_callback(
            pcb_path,
            output_path,
            float(getattr(args, "checkpoint_interval", 30.0) or 0.0),
            quiet=quiet,
            preserved_sexp=_preserved_sexp,
        )

        # Define routing function for profiling
        def do_routing():
            nonlocal diffpair_warnings, relaxed_nets_report, diffpair_budget_exit_pairs

            # Adaptive multi-resolution routing (when --grid auto selects it)
            if multi_res_plan is not None and multi_res_plan.is_multi_resolution:
                from kicad_tools.router.adaptive_grid import AdaptiveGridRouter

                if not quiet:
                    flush_print("  Using adaptive multi-resolution grid strategy")
                adaptive_router = AdaptiveGridRouter(
                    grid=router.grid,
                    rules=rules,
                    router=router,
                )

                # Build nets dict (filter to routable multi-pad nets)
                adaptive_nets = {
                    net_id: pad_keys
                    for net_id, pad_keys in router.nets.items()
                    if net_id > 0 and len(pad_keys) >= 2
                }
                adaptive_pads = router.pads

                # Define the Phase 2 routing function
                def phase2_route_fn():
                    # Issue #2464: When --differential-pairs is set, run a
                    # diff-pair pre-pass before the configured strategy.
                    # The strategy then routes the remaining nets; diff-pair
                    # nets are filtered from the negotiated loop via the
                    # prerouted-net skip in route_all_negotiated.
                    if args.differential_pairs and args.strategy in ("negotiated", "basic"):

                        def _phase2_strategy():
                            if args.strategy == "negotiated":
                                return router.route_all_negotiated(
                                    max_iterations=args.iterations,
                                    timeout=_budgeted_timeout(args),
                                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                                    batch_routing=getattr(args, "batch_routing", False)
                                    or getattr(args, "high_performance", False),
                                    hierarchical=getattr(args, "hierarchical", False),
                                    perturbation=getattr(args, "perturbation", True),
                                    # Issue #3039: forward --seed for deterministic routing.
                                    seed=getattr(args, "seed", None),
                                    # Issue #3054 (Phase 2 of #3045): forward
                                    # region-based parallelism opt-in.
                                    region_parallel=getattr(args, "region_parallel", False),
                                    partition_rows=getattr(args, "partition_rows", 2),
                                    partition_cols=getattr(args, "partition_cols", 2),
                                    max_parallel_workers=getattr(args, "max_parallel_workers", 4),
                                    checkpoint_callback=_checkpoint_cb,
                                    # Issue #3438 / #3414: forward --targeted-ripup so the
                                    # pre-existing targeted rip-up path in
                                    # route_all_negotiated is CLI-reachable.
                                    use_targeted_ripup=getattr(args, "targeted_ripup", False),
                                    max_ripups_per_net=_targeted_ripup_budget(args),
                                    # Issue #3132: forward --early-stop-patience
                                    # to the inner main-path negotiator call so
                                    # the CLI flag is honored (the previous
                                    # implementation silently defaulted to 2).
                                    best_stall_patience=(
                                        getattr(args, "early_stop_patience", 2) or None
                                    ),
                                )
                            return router.route_all()

                        # coupled_only=True so pairs that the
                        # CoupledPathfinder cannot handle (3-pad nets,
                        # etc.) fall through to the main strategy
                        # rather than being half-routed independently
                        # and then skipped.  Issue #2464.
                        # Issue #3321: forward --timeout so the diff-pair
                        # pre-pass derives a per-pair budget when the user
                        # has not opted in via --diffpair-per-pair-timeout.
                        # Without this, the CoupledPathfinder could peg
                        # CPU for >40min on board 07's MIPI lanes despite
                        # --timeout being set.
                        result, dp_warnings = router.route_all_with_diffpairs(
                            diffpair_config,
                            non_diffpair_strategy=_phase2_strategy,
                            coupled_only=(args.strategy == "negotiated"),
                            timeout=_budgeted_timeout(args),
                        )
                        diffpair_warnings.extend(dp_warnings)
                        # Issue #4095: surface any coupled pairs that
                        # budget-exited and fell back to single-ended.
                        diffpair_budget_exit_pairs.extend(router.diffpair_budget_exit_pair_names())
                        return result
                    if args.strategy == "evolutionary":
                        return router.route_all_evolutionary(
                            pop_size=args.pop_size,
                            generations=args.generations,
                            verbose=args.verbose and not quiet,
                            timeout=_budgeted_timeout(args),
                        )
                    elif args.strategy == "monte-carlo":
                        return router.route_all_monte_carlo(
                            num_trials=args.mc_trials,
                            verbose=args.verbose and not quiet,
                        )
                    elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                        return router.route_all_two_phase(
                            use_negotiated=True,
                            corridor_width_factor=2.0,
                            timeout=_budgeted_timeout(args),
                            per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                            max_iterations=getattr(args, "two_phase_iterations", None)
                            or args.iterations,
                        )
                    elif args.strategy == "negotiated":
                        return router.route_all_negotiated(
                            max_iterations=args.iterations,
                            timeout=_budgeted_timeout(args),
                            per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                            batch_routing=getattr(args, "batch_routing", False)
                            or getattr(args, "high_performance", False),
                            hierarchical=getattr(args, "hierarchical", False),
                            perturbation=getattr(args, "perturbation", True),
                            # Issue #3039: forward --seed for deterministic routing.
                            seed=getattr(args, "seed", None),
                            # Issue #3054 (Phase 2 of #3045): forward
                            # region-based parallelism opt-in.
                            region_parallel=getattr(args, "region_parallel", False),
                            partition_rows=getattr(args, "partition_rows", 2),
                            partition_cols=getattr(args, "partition_cols", 2),
                            max_parallel_workers=getattr(args, "max_parallel_workers", 4),
                            checkpoint_callback=_checkpoint_cb,
                            # Issue #3438 / #3414: forward --targeted-ripup so the
                            # pre-existing targeted rip-up path in
                            # route_all_negotiated is CLI-reachable.
                            use_targeted_ripup=getattr(args, "targeted_ripup", False),
                            max_ripups_per_net=_targeted_ripup_budget(args),
                            # Issue #3132: forward --early-stop-patience to the
                            # inner main-path negotiator call so the CLI flag
                            # is honored (default 2 was silently overriding it).
                            best_stall_patience=(getattr(args, "early_stop_patience", 2) or None),
                        )
                    else:
                        return router.route_all()

                adaptive_result = adaptive_router.route_adaptive(
                    nets=adaptive_nets,
                    pads=adaptive_pads,
                    route_fn=phase2_route_fn,
                )

                if not quiet:
                    flush_print(f"\n{adaptive_result.format_summary()}")

                return adaptive_result.all_routes

            # Check if escape routing should run as a pre-phase
            if _should_use_escape_routing(router, escape_routing_flag, quiet):
                # Issue #3952: compose the escape pre-phase with the
                # CoupledPathfinder diff-pair pre-pass when
                # --differential-pairs is requested so Phase A runs on
                # escape-forced boards (the fixed-layer escape branch used
                # to bypass the diff-pair dispatch just like the escalation
                # paths).  ``diffpair_config`` is already built above and in
                # scope here; gating on it keeps no-pair boards on the
                # byte-identical old ``route_with_escape`` path.
                if args.differential_pairs and diffpair_config is not None:
                    routes, dp_warnings = router.route_with_escape_and_diffpairs(
                        diffpair_config,
                        use_negotiated=(args.strategy == "negotiated"),
                        timeout=_budgeted_timeout(args),
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    )
                    diffpair_warnings.extend(dp_warnings)
                    # Issue #4095: the escape-composed path delegates to the
                    # same diff-pair orchestrator; surface its budget-exit
                    # fallback too.
                    diffpair_budget_exit_pairs.extend(router.diffpair_budget_exit_pair_names())
                    return routes
                return router.route_with_escape(
                    use_negotiated=(args.strategy == "negotiated"),
                    timeout=_budgeted_timeout(args),
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                )

            # Progressive clearance relaxation mode
            if getattr(args, "progressive_clearance", False):
                routes, relaxed_nets_report = router.route_with_progressive_clearance(
                    min_clearance=getattr(args, "min_clearance", None),
                    num_relaxation_levels=getattr(args, "relaxation_levels", 3),
                    max_iterations=args.iterations,
                    timeout=_budgeted_timeout(args),
                )
                return routes
            elif getattr(args, "multi_resolution", False):
                return router.route_all_multi_resolution(
                    use_negotiated=(args.strategy == "negotiated"),
                    max_iterations=args.iterations,
                    timeout=_budgeted_timeout(args),
                )
            elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                return router.route_all_two_phase(
                    use_negotiated=True,
                    corridor_width_factor=2.0,
                    timeout=_budgeted_timeout(args),
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                )
            elif args.differential_pairs and args.strategy == "negotiated":
                # Issue #2464: Diff-pair pre-pass + negotiated for the rest.
                # The negotiated loop honors prerouted nets via the new skip
                # logic in route_all_negotiated.  coupled_only=True so
                # unsupported pad configurations fall through cleanly.
                def _neg_strategy():
                    return router.route_all_negotiated(
                        max_iterations=args.iterations,
                        timeout=_budgeted_timeout(args),
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                        batch_routing=getattr(args, "batch_routing", False)
                        or getattr(args, "high_performance", False),
                        hierarchical=getattr(args, "hierarchical", False),
                        perturbation=getattr(args, "perturbation", True),
                        # Issue #3039: forward --seed for deterministic routing.
                        seed=getattr(args, "seed", None),
                        # Issue #3054 (Phase 2 of #3045): forward
                        # region-based parallelism opt-in.
                        region_parallel=getattr(args, "region_parallel", False),
                        partition_rows=getattr(args, "partition_rows", 2),
                        partition_cols=getattr(args, "partition_cols", 2),
                        max_parallel_workers=getattr(args, "max_parallel_workers", 4),
                        checkpoint_callback=_checkpoint_cb,
                        # Issue #3438 / #3414: forward --targeted-ripup so the
                        # pre-existing targeted rip-up path in
                        # route_all_negotiated is CLI-reachable.
                        use_targeted_ripup=getattr(args, "targeted_ripup", False),
                        max_ripups_per_net=_targeted_ripup_budget(args),
                        # Issue #3132: forward --early-stop-patience to the
                        # inner negotiator so the CLI flag is honored.  This
                        # is the call site board 05's
                        # `--differential-pairs --strategy negotiated` recipe
                        # actually hits; previously the parameter silently
                        # defaulted to 2.
                        best_stall_patience=(getattr(args, "early_stop_patience", 2) or None),
                    )

                # Issue #3321: forward --timeout so the diff-pair
                # pre-pass derives a per-pair budget when the user
                # has not opted in via --diffpair-per-pair-timeout.
                result, dp_warnings = router.route_all_with_diffpairs(
                    diffpair_config,
                    non_diffpair_strategy=_neg_strategy,
                    coupled_only=True,
                    timeout=_budgeted_timeout(args),
                )
                diffpair_warnings.extend(dp_warnings)
                # Issue #4095: surface coupled pairs that budget-exited to
                # single-ended on the negotiated dispatch path.
                diffpair_budget_exit_pairs.extend(router.diffpair_budget_exit_pair_names())
                return result
            elif args.strategy == "negotiated":
                return router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=_budgeted_timeout(args),
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    batch_routing=getattr(args, "batch_routing", False)
                    or getattr(args, "high_performance", False),
                    hierarchical=getattr(args, "hierarchical", False),
                    perturbation=getattr(args, "perturbation", True),
                    # Issue #3039: forward --seed for deterministic routing.
                    seed=getattr(args, "seed", None),
                    # Issue #3054 (Phase 2 of #3045): forward region-based
                    # parallelism opt-in.
                    region_parallel=getattr(args, "region_parallel", False),
                    partition_rows=getattr(args, "partition_rows", 2),
                    partition_cols=getattr(args, "partition_cols", 2),
                    max_parallel_workers=getattr(args, "max_parallel_workers", 4),
                    checkpoint_callback=_checkpoint_cb,
                    # Issue #3438 / #3414: forward --targeted-ripup so the
                    # pre-existing targeted rip-up path in
                    # route_all_negotiated is CLI-reachable.
                    use_targeted_ripup=getattr(args, "targeted_ripup", False),
                    max_ripups_per_net=_targeted_ripup_budget(args),
                    # Issue #3132: forward --early-stop-patience to the inner
                    # negotiator call so the CLI flag is honored.  Previously
                    # the parameter silently defaulted to 2 even when the
                    # CLI passed a higher value.
                    best_stall_patience=(getattr(args, "early_stop_patience", 2) or None),
                )
            elif args.differential_pairs and args.strategy == "basic":
                # Issue #3321: forward --timeout so the diff-pair pre-pass
                # derives a per-pair budget when the user has not opted in
                # via --diffpair-per-pair-timeout.
                result, dp_warnings = router.route_all_with_diffpairs(
                    diffpair_config,
                    timeout=_budgeted_timeout(args),
                )
                diffpair_warnings.extend(dp_warnings)
                # Issue #4095: surface coupled pairs that budget-exited to
                # single-ended on the basic dispatch path.
                diffpair_budget_exit_pairs.extend(router.diffpair_budget_exit_pair_names())
                return result
            elif args.bus_routing and args.strategy == "basic":
                return router.route_all_with_buses(bus_config)
            elif args.strategy == "basic":
                return router.route_all()
            elif args.differential_pairs and args.strategy in ("monte-carlo", "evolutionary"):
                # Issue #2464: MC/GA reset the grid per trial (see
                # _reset_for_new_trial), which would wipe pre-routed
                # diff-pair traces.  For now, surface a warning and fall
                # through to the standard strategy.  Follow-up work needed
                # to integrate diff-pair pre-pass with these strategies.
                if not quiet:
                    flush_print(
                        "  Warning: --differential-pairs is not yet supported with "
                        f"strategy='{args.strategy}' (each trial resets the grid). "
                        "Falling through to standard strategy. See Issue #2464."
                    )
                if args.strategy == "monte-carlo":
                    return router.route_all_monte_carlo(
                        num_trials=args.mc_trials,
                        verbose=args.verbose and not quiet,
                    )
                else:
                    return router.route_all_evolutionary(
                        pop_size=args.pop_size,
                        generations=args.generations,
                        verbose=args.verbose and not quiet,
                    )
            elif args.strategy == "monte-carlo":
                return router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
            elif args.strategy == "evolutionary":
                return router.route_all_evolutionary(
                    pop_size=args.pop_size,
                    generations=args.generations,
                    verbose=args.verbose and not quiet,
                    timeout=_budgeted_timeout(args),
                )
            return None

        try:
            if args.profile:
                # Profile the routing operation
                import cProfile
                import pstats

                profile_output = args.profile_output or "route_profile.prof"
                profiler = cProfile.Profile()
                profiler.enable()
                try:
                    _ = do_routing()
                finally:
                    profiler.disable()
                    # Save profile data
                    profiler.dump_stats(profile_output)
                    if not quiet:
                        print(f"\n  Profile saved to: {profile_output}")
                        # Print top 20 functions by cumulative time
                        print("\n--- Profile Summary (top 20 by cumulative time) ---")
                        stats = pstats.Stats(profiler)
                        stats.strip_dirs().sort_stats("cumulative").print_stats(20)
            else:
                # Normal routing without profiling
                if args.strategy == "negotiated":
                    # Negotiated routing has its own progress output - don't use spinner
                    _ = do_routing()
                else:
                    with spinner(f"Routing {nets_to_route} nets...", quiet=quiet):
                        _ = do_routing()
        except KeyboardInterrupt:
            # Handle any KeyboardInterrupt that wasn't caught by signal handler
            _interrupt_state["interrupted"] = True
            if not quiet:
                print("\n\n⚠ Routing interrupted!")
        except Exception as e:
            # Provide actionable guidance when escape routing detected a
            # doomed coarse grid (issue #2387).
            try:
                from kicad_tools.router.adaptive_grid import (
                    FinePitchEscapeFailure,
                )
            except Exception:
                FinePitchEscapeFailure = None  # type: ignore[assignment]
            if FinePitchEscapeFailure is not None and isinstance(e, FinePitchEscapeFailure):
                print(
                    f"Error during routing: {e}",
                    file=sys.stderr,
                )
                print(
                    f"  Suggested fix: rerun with --grid {e.suggested_grid:.4f}",
                    file=sys.stderr,
                )
            else:
                print(f"Error during routing: {e}", file=sys.stderr)
            # Still try to save partial results on error
            if router.routes:
                _save_partial_results()
            return 1

        # Check if interrupted and save partial results
        if _interrupt_state["interrupted"]:
            _save_partial_results()
            return 5  # Exit code 5 indicates interruption with partial results saved

        # Issue #2595: placement-routing feedback loop.  When the user
        # opted in via --placement-feedback and the initial pass left
        # nets unrouted, invoke route_with_placement_feedback() to
        # nudge non-anchored components and re-route from scratch.
        # The helper writes <output>_placement_diff.json beside the
        # routed PCB; the return value is not consumed here because
        # downstream stages (cache, optimize, save) read directly from
        # router.routes (which the feedback loop mutates in place).
        if (
            getattr(args, "placement_feedback", False)
            and router.routes is not None
            and router.get_failed_nets()
        ):
            _run_placement_feedback(
                router=router,
                pcb_path=pcb_path,
                args=args,
                quiet=quiet,
            )

        # Cache the routing result (if caching enabled and routing succeeded)
        if use_cache and cache_key is not None and router.routes:
            import time

            try:
                routing_time_ms = (
                    int((time.time() - routing_start_time) * 1000) if routing_start_time else 0
                )
                stats = router.get_statistics()
                cache.put(cache_key, router.routes, stats, routing_time_ms)
                if not quiet:
                    print(f"  Cached routing result ({routing_time_ms}ms compute time)")
            except Exception as e:
                if not quiet:
                    print(f"  Warning: Failed to cache result: {e}")

    # Get pre-optimization statistics (also used in the no-optimize path
    # below so the segment/via summary print does not raise
    # UnboundLocalError when --no-optimize is set).
    pre_segments = sum(len(r.segments) for r in router.routes)
    pre_vias = sum(len(r.vias) for r in router.routes)

    # Optimize traces (unless --no-optimize/--raw flag is set)
    if not args.no_optimize and router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
            optimize_routes_grid_synced,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        # Configure and run optimizer
        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.
        has_overflow = router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(router.grid, ignore_overflow=has_overflow)
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        # Issue #2596: snapshot per-net connectivity before optimize so
        # any net whose pad-to-pad connectivity regresses can be reverted.
        _ci_snapshot = _connectivity_snapshot(router)

        with spinner("Optimizing traces...", quiet=quiet):
            # Issue #3507: grid-transactional optimize (see
            # optimize_routes_grid_synced).
            optimize_routes_grid_synced(router, optimizer)

        _enforce_connectivity_invariant_or_exit(
            router,
            _ci_snapshot,
            phase="optimize",
            args=args,
            quiet=quiet,
        )

    # Post-optimization DRC nudge pass
    if router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        # Issue #2596: snapshot connectivity before nudge.
        _ci_snapshot_nudge = _connectivity_snapshot(router)

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

        _enforce_connectivity_invariant_or_exit(
            router,
            _ci_snapshot_nudge,
            phase="nudge",
            args=args,
            quiet=quiet,
        )

        # Issue #4208 (Unit 3): re-run the Unit-2 seg-seg finalize gate
        # over the post-optimize/post-nudge copper.  An rtree-less
        # optimizer can introduce a cross-net crossing the pre-optimize
        # finalize gate never saw; demote it before the canonical write.
        _finalize_committed_copper_or_demote(router, quiet=quiet)

        # Get post-optimization statistics
        post_segments = sum(len(r.segments) for r in router.routes)
        post_vias = sum(len(r.vias) for r in router.routes)

        if not quiet:
            segment_reduction = (
                ((pre_segments - post_segments) / pre_segments * 100) if pre_segments > 0 else 0
            )
            via_reduction = ((pre_vias - post_vias) / pre_vias * 100) if pre_vias > 0 else 0
            print(f"  Segments: {pre_segments} -> {post_segments} ({-segment_reduction:+.1f}%)")
            if pre_vias > 0:
                print(f"  Vias:     {pre_vias} -> {post_vias} ({-via_reduction:+.1f}%)")

    # Differential-pair length-match (skew) tuning -- Epic #2556 Phase 3I (Issue #2648).
    # Engaged via --length-match-diffpairs (opt-in).  Requires --differential-pairs;
    # otherwise emit a warning and short-circuit (no detection means no pairs).
    if getattr(args, "length_match_diffpairs", False) and router.routes:
        if not args.differential_pairs:
            if not quiet:
                print(
                    "\n--- Length-Match Diff-Pairs: skipped ---\n"
                    "  --length-match-diffpairs requires --differential-pairs "
                    "(no detection means no pairs to tune)."
                )
        else:
            from kicad_tools.router.diffpair_detection import detect_diff_pairs

            if not quiet:
                print("\n--- Length-Match Diff-Pairs (Epic #2556 Phase 3I) ---")
            detected_pairs = detect_diff_pairs(net_names=router.net_names)
            if not detected_pairs:
                if not quiet:
                    print("  No differential pairs detected; nothing to tune.")
            else:
                # Snapshot connectivity for the pad-to-pad invariant.
                _ci_snapshot_skew = _connectivity_snapshot(router)
                tune_results = router.apply_diffpair_length_tuning(
                    detected_pairs=detected_pairs,
                    verbose=not quiet,
                )
                _enforce_connectivity_invariant_or_exit(
                    router,
                    _ci_snapshot_skew,
                    phase="length_match_diffpairs",
                    args=args,
                    quiet=quiet,
                )
                if not quiet:
                    n_tuned = sum(1 for r in tune_results.values() if r.reason == "tuned")
                    n_clean = sum(
                        1 for r in tune_results.values() if r.reason == "already_within_tolerance"
                    )
                    n_rollback = sum(
                        1
                        for r in tune_results.values()
                        if r.reason == "post_insertion_drc_violation"
                    )
                    n_budget = sum(
                        1 for r in tune_results.values() if r.reason == "exceeded_max_inserts"
                    )
                    n_skipped = sum(
                        1 for r in tune_results.values() if r.reason == "not_length_critical"
                    )
                    print(
                        f"  Summary: {n_tuned} tuned, {n_clean} clean, "
                        f"{n_rollback} rolled back, {n_budget} budget-exhausted, "
                        f"{n_skipped} skipped (not length-critical)"
                    )

    # N-trace match-group length-match (skew) tuning -- Epic #2661 Phase 3H
    # (Issue #2723).  Engaged via --length-match-groups (opt-in).  Runs
    # AFTER --length-match-diffpairs so the within-pair skew invariant is
    # preserved before group tuning perturbs lane lengths.  Graceful
    # short-circuit when no groups are declared / detected.
    if getattr(args, "length_match_groups", False) and router.routes:
        from kicad_tools.router.match_group_detection import detect_match_groups

        if not quiet:
            print("\n--- Length-Match Groups (Epic #2661 Phase 3H) ---")
        # Issue #3440: --length-match-groups silently no-ops without
        # length_match_group declarations (detection consults
        # NetClassRouting declarations exclusively when suffix inference
        # is off; the built-in DEFAULT_NET_CLASS_MAP declares none).
        # Warn LOUDLY -- on stderr, regardless of --quiet -- so a recipe
        # that forgot --net-class-map doesn't sail through with untuned
        # skew.
        _has_group_declarations = any(
            nc.effective_length_match_group() for nc in router.net_class_map.values()
        )
        if not _has_group_declarations:
            print(
                "WARNING: --length-match-groups is INACTIVE: no loaded net "
                "class declares length_match_group, so no match groups can "
                "be detected and no skew tuning will run.  Pass "
                "--net-class-map <sidecar.json> (e.g. the board's "
                "output/net_class_map.json) with length_match_group "
                "declarations.",
                file=sys.stderr,
            )
        # Build net_to_class + a class-name-keyed routing map so the
        # explicit-declaration consultation in ``_gather_explicit_groups``
        # can find each net's NetClassRouting.  ``router.net_class_map`` is
        # keyed by NET NAME (e.g. "DQ0"), but ``detect_match_groups`` looks
        # up by CLASS NAME (e.g. "DDR_DATA_BYTE_0").  Mirrors the
        # synth_routing idiom in ``validate/match_group_skew.py:174-181``
        # (Issue #3098 -- without this the detector returned an empty
        # group list and the orchestrator silently no-op'd).
        net_to_class: dict[str, str] = {}
        synth_routing: dict = dict(router.net_class_map)
        for net_name, net_class in router.net_class_map.items():
            net_to_class[net_name] = net_class.name
            synth_routing.setdefault(net_class.name, net_class)
        try:
            detected_groups = detect_match_groups(
                net_names=router.net_names,
                net_class_routing=synth_routing,
                net_to_class=net_to_class,
                length_tracker=router.length_tracker,
                enable_suffix_inference=False,
            )
        except Exception as exc:
            if not quiet:
                print(f"  Match-group detection failed: {exc}; skipping.")
            detected_groups = []

        if not detected_groups:
            if not quiet:
                print("  No match groups detected; nothing to tune.")
        else:
            # Snapshot connectivity for the pad-to-pad invariant.
            _ci_snapshot_groups = _connectivity_snapshot(router)
            tune_results_groups = router.apply_match_group_tuning(
                detected_groups=detected_groups,
                verbose=not quiet,
            )
            _enforce_connectivity_invariant_or_exit(
                router,
                _ci_snapshot_groups,
                phase="length_match_groups",
                args=args,
                quiet=quiet,
            )
            if not quiet:
                # Aggregate per-member counters across all groups for a
                # single end-of-phase summary line.  Issue #3440: the
                # shared formatter counts EVERY TuneResult.reason value
                # (the legacy five-bucket line silently dropped
                # ``reference`` / ``longer_than_reference`` / ``unrouted``
                # members, producing the all-zeros line for a
                # 15.4mm-skew group).
                from kicad_tools.router.match_group_tuning import (
                    format_reason_counts,
                )

                all_results = [
                    res
                    for member_dict in tune_results_groups.values()
                    for (_route, res) in member_dict.values()
                ]
                print(
                    f"  Summary: {len(tune_results_groups)} groups, "
                    f"{format_reason_counts(r.reason for r in all_results)}"
                )

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    multi_pad_net_ids = set(multi_pad_nets)
    route_sexp, stats, cleanup_stats = _finalize_routes(
        router,
        multi_pad_net_ids,
        nets_to_route,
        quiet=quiet,
        strict=bool(getattr(args, "strict", False)),
        verbose=bool(getattr(args, "verbose", False)),
        preserve_existing=bool(getattr(args, "preserve_existing", False)),
        preserved_routes=_preserved_routes,
    )

    # Report differential pair length mismatch warnings
    if diffpair_warnings and not quiet:
        print(f"\n--- Differential Pair Warnings ({len(diffpair_warnings)}) ---")
        for warning in diffpair_warnings:
            print(f"  {warning}")

    # Issue #4095: report diff pairs that budget-exited coupled routing and
    # fell back to single-ended.  Printed unconditionally (only gated on
    # ``not quiet``, mirroring the length-mismatch block above) so the
    # regression risk is never accidentally hidden behind --verbose.  On
    # bundle-dense boards the coupled attempts can regress completion / DRC
    # vs. a plain single-ended route (board 07: 34 vs 13 DRC errors, 22/31
    # vs 26/31 nets; epic #4049 closeout), so the operator is told which
    # pairs fell back and advised to compare a single-ended re-route.
    if diffpair_budget_exit_pairs and not quiet:
        # De-duplicate while preserving order (a pair can appear once per
        # dispatch; the escape path can re-enter the orchestrator).
        _seen: set[str] = set()
        _exit_pairs: list[str] = []
        for name in diffpair_budget_exit_pairs:
            if name not in _seen:
                _seen.add(name)
                _exit_pairs.append(name)
        print("\n--- Differential Pair Budget-Exit Warning ---")
        print(
            f"  {len(_exit_pairs)} pair(s) budget-exited coupled routing and "
            f"fell back to single-ended: {', '.join(_exit_pairs)}"
        )
        print(
            "  WARNING: --differential-pairs can regress completion/DRC vs. a "
            "plain single-ended route on bundle-dense boards (see #4095)."
        )
        print(
            "  Consider re-routing without --differential-pairs and comparing "
            "`kct check` results if this board is dense with matched-length "
            "bundles."
        )

    # Report nets that needed clearance relaxation (--progressive-clearance mode)
    if relaxed_nets_report and not quiet:
        original_clearance = rules.trace_clearance
        print(f"\n--- Clearance Relaxation Report ({len(relaxed_nets_report)} nets) ---")
        print(f"  Original clearance: {original_clearance:.3f}mm")
        for net_id, clearance in sorted(relaxed_nets_report.items(), key=lambda x: x[1]):
            net_name = router.net_names.get(net_id, f"Net {net_id}")
            reduction = (1 - clearance / original_clearance) * 100
            print(f"  {net_name}: {clearance:.3f}mm ({reduction:.0f}% relaxation)")

    # Show preview if requested
    if args.preview:
        response = show_preview(
            router,
            net_map,
            nets_to_route,
            quiet=quiet,
            nets_to_route_ids=multi_pad_net_ids,
        )
        if response != "y":
            if not quiet:
                print("\nRouting cancelled. No changes saved.")
            return 0

    # Generate power zones if requested
    zone_sexp = ""
    if args.power_nets:
        from kicad_tools.zones import ZoneGenerator, parse_power_nets

        try:
            power_nets = parse_power_nets(args.power_nets)
        except ValueError as e:
            print(f"Error parsing power-nets: {e}", file=sys.stderr)
            return 1

        if power_nets and not quiet:
            print("\n--- Generating copper zones ---")
            print(f"  Power nets: {', '.join(f'{n}:{l}' for n, l in power_nets)}")

        if power_nets:
            try:
                gen = ZoneGenerator.from_pcb(
                    str(pcb_path),
                    edge_clearance=args.edge_clearance,
                )
                for net_name, layer in power_nets:
                    # GND gets higher priority (fills last, on top)
                    priority = 1 if net_name.upper() in ("GND", "GNDA", "GNDD") else 0
                    try:
                        gen.add_zone(
                            net=net_name,
                            layer=layer,
                            priority=priority,
                        )
                        if not quiet:
                            print(f"    Added zone: {net_name} on {layer} (priority {priority})")
                    except ValueError as e:
                        print(f"  Warning: Could not add zone for {net_name}: {e}")

                zone_sexp = gen.generate_sexp()
            except Exception as e:
                print(f"  Warning: Zone generation failed: {e}")

    # Pre-save clearance validation
    # Issue #1666: Segment-to-segment violations now cause a non-zero exit
    # code so that CI pipelines and DRC workflows can detect the failure.
    seg_seg_violation_count = 0
    if stats["nets_routed"] > 0 and not args.dry_run:
        from kicad_tools.router.io import format_clearance_violations, validate_routes

        clearance_violations = validate_routes(router)
        if clearance_violations:
            seg_seg_violation_count = sum(
                1
                for v in clearance_violations
                if v.obstacle_type == "segment" and not v.component_inherent
            )
            if not quiet:
                print("\n--- Pre-save Clearance Validation ---")
                if seg_seg_violation_count > 0:
                    print(
                        f"  ERROR: {seg_seg_violation_count} segment-to-segment "
                        f"clearance violation(s) remain after routing"
                    )
                print(f"  {format_clearance_violations(clearance_violations)}")

    # Save output
    output_content = ""  # Tracks written content for output connectivity verification
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
    else:
        if not quiet:
            print("\n--- Saving routed PCB ---")

        with spinner("Saving routed PCB...", quiet=quiet):
            # route_sexp was already generated by _finalize_routes() above
            # Combine zone + route fragments before insertion.
            # Note: KiCad's S-expression format doesn't support ; comments.
            combined_sexp = ""
            if route_sexp or zone_sexp:
                fragments = []
                if zone_sexp:
                    fragments.append(zone_sexp)
                if route_sexp:
                    fragments.append(route_sexp)
                combined_sexp = "\n  ".join(fragments)
            elif not quiet:
                print("  Warning: No routes generated!")

            # Issue #2808: atomic write via _write_routed_pcb.  No layer
            # escalation in this flow, so layer_count defaults to 2 (no-op).
            _write_routed_pcb(
                pcb_path,
                output_path,
                combined_sexp,
            )

            # We need output_content for the connectivity verification below;
            # re-read since the atomic write left output_path with the final
            # content we just wrote.
            output_content = output_path.read_text()

        if not quiet:
            print(f"  Saved to: {output_path}")

    # Output connectivity verification (Issue #2264)
    # Re-parse written S-expressions and verify pad-to-pad connectivity
    output_has_disconnected = False
    if not args.dry_run and router.pads and router.nets and output_content:
        from kicad_tools.router.io import verify_output_connectivity

        # Build net_pads mapping (same as get_statistics)
        verify_net_pads: dict[int, list] = {}
        for net_id, pad_keys in router.nets.items():
            if net_id not in multi_pad_net_ids:
                continue
            pad_list = [router.pads[k] for k in pad_keys if k in router.pads]
            if len(pad_list) >= 2:
                verify_net_pads[net_id] = pad_list

        # Build net name lookup
        reverse_net_map = {v: k for k, v in net_map.items()}

        output_connectivity = verify_output_connectivity(
            pcb_content=output_content,
            net_pads=verify_net_pads,
            net_names=reverse_net_map,
        )

        disconnected_nets = {
            nid: info
            for nid, info in output_connectivity.items()
            if not info["connected"] and info["total_pads"] >= 2
        }

        if disconnected_nets:
            output_has_disconnected = True
            if not quiet:
                print("\n--- Output Connectivity Verification ---")
                print(
                    f"  WARNING: {len(disconnected_nets)} net(s) have disconnected "
                    f"pads in written output"
                )
                for nid, info in sorted(disconnected_nets.items(), key=lambda x: x[1]["net_name"]):
                    disc_str = ", ".join(info["disconnected_pads"][:5])
                    if len(info["disconnected_pads"]) > 5:
                        disc_str += f" (+{len(info['disconnected_pads']) - 5} more)"
                    print(
                        f"  {info['net_name']}: "
                        f"{info['connected_pads']}/{info['total_pads']} pads connected"
                        f" -- disconnected: {disc_str}"
                    )
        else:
            if not quiet:
                print("\n--- Output Connectivity Verification ---")
                print("  All nets verified connected in written output")

    # Run power plane stitching if requested
    stitch_result = None
    if getattr(args, "stitch_power_planes", False) and not args.dry_run:
        from kicad_tools.cli.stitch_cmd import find_all_plane_nets, run_stitch

        if not quiet:
            print("\n--- Stitching Power Planes ---")

        # Load the saved PCB to find plane nets
        from kicad_tools.core.sexp_file import load_pcb as load_stitch_pcb

        stitch_sexp = load_stitch_pcb(output_path)
        plane_nets = find_all_plane_nets(stitch_sexp)

        if plane_nets:
            net_names = list(plane_nets.keys())
            if not quiet:
                print(f"  Found {len(net_names)} power plane nets: {', '.join(sorted(net_names))}")

            stitch_result = run_stitch(
                pcb_path=output_path,
                net_names=net_names,
                via_size=args.via_diameter,  # Use same via size as routing
                drill=args.via_drill,
                clearance=args.clearance,
                dry_run=False,
            )

            if not quiet:
                if stitch_result.vias_added:
                    print(f"  Added {len(stitch_result.vias_added)} stitching vias")
                else:
                    print("  No stitching vias needed (all pads already connected)")
        else:
            if not quiet:
                print("  No power plane nets found (no zones with assigned nets)")

    # Fill copper-pour zones now that traces (and any stitching vias)
    # are in place (issue #2516).  Must run BEFORE DRC so the DRC sees
    # filled zones rather than bare zone outlines.
    if not args.dry_run and stats["nets_routed"] > 0:
        _fill_zones_after_route(output_path, quiet=quiet)

    # Run DRC validation unless skipped or dry-run
    drc_errors = 0
    drc_warnings = 0
    drc_ran = False
    fix_result: int | None = None

    if not args.dry_run and not args.skip_drc and stats["nets_routed"] > 0:
        drc_ran = True
        drc_errors, drc_warnings = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=layer_stack.num_layers,
            quiet=quiet,
            # Issue #2652, Epic #2556 Phase 2.5b: see other call sites.
            net_class_map=getattr(router, "net_class_map", None),
            # Issue #4178: forward --strict-drc (see other call sites).
            strict_drc=getattr(args, "strict_drc", False),
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            fix_result = _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
                args=args,  # Issue #2802: honor total wall-clock deadline
            )
            if fix_result == 0:
                drc_errors = 0

    # Summary
    all_nets_routed = stats["nets_routed"] == nets_to_route
    drc_passed = drc_errors <= 0  # -1 means DRC failed to run, treat as passed
    completion_ratio = stats["nets_routed"] / nets_to_route if nets_to_route > 0 else 1.0
    meets_threshold = completion_ratio >= args.min_completion

    # Build summary suffix for net breakdown (Issue #812)
    summary_parts = []
    if len(single_pad_nets) > 0:
        summary_parts.append(f"{len(single_pad_nets)} single-pad")
    if power_nets_skipped > 0:
        summary_parts.append(f"{power_nets_skipped} power skipped")
    summary_suffix = f" ({', '.join(summary_parts)})" if summary_parts else ""

    if not quiet:
        print("\n" + "=" * 60)
        if all_nets_routed and drc_passed:
            if drc_ran and drc_errors == 0:
                print(f"SUCCESS: All signal nets routed, DRC passed!{summary_suffix}")
            else:
                print(f"SUCCESS: All signal nets routed!{summary_suffix}")
                if not drc_ran and not args.skip_drc and not args.dry_run:
                    print("  Note: Run 'kct check' to validate before manufacturing")
        elif meets_threshold and not all_nets_routed and drc_passed:
            pct = completion_ratio * 100
            print(
                f"SUCCESS: Routed {stats['nets_routed']}/{nets_to_route} signal nets "
                f"({pct:.0f}%, meets {args.min_completion * 100:.0f}% threshold){summary_suffix}"
            )
            if not drc_ran and not args.skip_drc and not args.dry_run:
                print("  Note: Run 'kct check' to validate before manufacturing")
        elif all_nets_routed and not drc_passed:
            print("ROUTING FAILED: DRC violations detected")
            print("=" * 60)
            print()
            print("Net Statistics:")
            print(f"  Multi-pad nets:  {nets_to_route}")
            print(f"  Nets connected:  {stats['nets_routed']} (topologically complete)")
            print("  Nets DRC-clean:  0 (manufacturing blocked)")
            if len(single_pad_nets) > 0 or power_nets_skipped > 0:
                print(f"  Also:{summary_suffix}")
            print()
            print("DRC Summary:")
            print(f"  Violations: {drc_errors}")
            print()
            print("The autorouter connected all nets but violated design rules.")
            print("This board cannot be manufactured without fixing DRC errors.")
            print()
            print("Suggestions:")
            print(f"  - Auto-repair DRC violations: kct fix-drc {output_path} --max-passes 20")
            print(
                f"  - Try Monte Carlo routing: kct route {args.pcb} --strategy monte-carlo --mc-trials 10"
            )
            print("  - Increase board area")
            print("  - Reduce component density")
            print("  - Try 4-layer routing: kct route --layers 4")
            print(f"  - Or re-route with auto-fix: kct route {args.pcb} --auto-fix")
            print()
            print(f"  Run 'kct check {output_path} --mfr {args.manufacturer}' for full details")
        else:
            print(
                f"PARTIAL: Routed {stats['nets_routed']}/{nets_to_route} signal nets{summary_suffix}"
            )
            if drc_ran and drc_errors > 0:
                print(f"  Additionally, {drc_errors} DRC violation(s) detected.")

            # Issue #2388: When the negotiated loop bailed out due to a
            # power-net stall, surface actionable suggestions naming the
            # specific stalled nets and recommended remediation flags.
            if getattr(router, "power_stall_abort", False):
                _print_power_stall_suggestions(
                    list(getattr(router, "power_stall_nets", [])),
                    layer_stack.num_layers,
                    args.pcb,
                )

            # Show comprehensive routing summary with successes, failures, and suggestions
            # Use JSON format if requested
            if args.format == "json":
                print_routing_diagnostics_json(
                    router,
                    net_map,
                    nets_to_route,
                    current_strategy=args.strategy,
                    nets_to_route_ids=multi_pad_net_ids,
                    single_pad_count=len(single_pad_nets),
                )
            else:
                # Verbose mode shows detailed path analysis for each failure
                verbose = args.verbose or args.diagnostics
                show_routing_summary(
                    router,
                    net_map,
                    nets_to_route,
                    quiet=quiet,
                    verbose=verbose,
                    current_strategy=args.strategy,
                    pcb_file=args.pcb,
                    nets_to_route_ids=multi_pad_net_ids,
                    single_pad_count=len(single_pad_nets),
                )

    # Save partial results on clean partial exit (not just SIGINT)
    if not all_nets_routed and not args.dry_run and router.routes:
        partial_saved = _save_partial_results()
        if partial_saved and not quiet:
            # Make the authoritative file unambiguous: the -o target (written
            # by the routing pipeline with optimize + DRC applied) is canonical;
            # the _partial file is a raw pre-optimize snapshot (see
            # _save_partial_results). Emit exactly one canonical-output line.
            print(f"  Canonical output: {output_path} (full route + optimize + DRC)")
            print("  Open in KiCad to complete remaining nets manually")

    # Export failed nets to file if requested
    if getattr(args, "export_failed_nets", None) and not all_nets_routed:
        _export_failed_nets(
            router,
            net_map,
            args.export_failed_nets,
            quiet=quiet,
            nets_to_route_ids=multi_pad_net_ids,
        )

    # Exit codes:
    # 0 = Routing meets --min-completion threshold AND (DRC passed OR DRC not run)
    # 1 = Fatal failure — no nets routed, no useful output
    # 2 = Partial routing — some nets routed but below --min-completion threshold
    # 3 = Meets threshold but DRC violations detected (includes seg-seg violations).
    #     Issue #2852: also returned when --auto-fix rolled back due to a
    #     connectivity regression (fix-drc exit 3) — semantically the same
    #     contract ("routing succeeded but DRC is dirty"); callers cannot
    #     trust the post-route DRC state.
    # 4 = Seg-seg clearance violations remain AND routing is below threshold (Issue #1666)
    # 5 = Interrupted by SIGINT with partial results saved (handled in _handle_interrupt)
    # 6 = Connectivity regression detected (--strict mode only): either
    #     the optimize / DRC nudge phases reduced the number of fully-
    #     connected nets (issue #2596) or post-save output verification
    #     reported disconnected pads (issue #2264).
    # 7 = Auto-fix was requested but skipped because the total --timeout
    #     budget was exhausted by routing (issue #3238).  Distinct from
    #     exit 3 ("routing succeeded but DRC is dirty after auto-fix
    #     tried and failed") so CI gates can detect silent skip-on-deadline
    #     regressions without parsing the full route log.  The
    #     AUTOFIX_SKIPPED_BUDGET_EXHAUSTED stderr token is emitted on
    #     this path.
    #
    # The --min-completion flag (default 0.95) controls the success threshold.
    # With --min-completion 0.80, routing 85% of nets returns exit code 0.
    completion_ratio = stats["nets_routed"] / nets_to_route if nets_to_route > 0 else 1.0
    meets_threshold = completion_ratio >= args.min_completion

    # --strict: output connectivity verification failure is fatal
    if getattr(args, "strict", False) and output_has_disconnected:
        return 6

    # Issue #3238: distinct exit code when auto-fix was requested but
    # skipped because the total wall-clock budget was exhausted.  This
    # has to be checked *before* the generic exit-3 ("DRC dirty") path
    # because skip-on-deadline always leaves DRC dirty (auto-fix never
    # ran), and we want the caller to see "the user asked for auto-fix
    # and it didn't happen" as a distinct signal from "auto-fix ran and
    # couldn't clean everything".
    if getattr(args, "_auto_fix_status", None) == "skipped_deadline":
        return 7

    # Issue #2852: make the --auto-fix rollback path explicit.  Today this
    # case already falls through to the ``return 3`` branch below because
    # ``drc_errors`` is unchanged when fix-drc rolls back -- but that is
    # accidentally correct, not by design.  Surface it explicitly so the
    # contract is documented in code and survives future refactors that
    # might zero out ``drc_errors`` on the fix-drc path.
    if fix_result == 3:
        return 3

    if stats["nets_routed"] == 0 and nets_to_route > 0:
        # Nothing was routed — treat as fatal failure
        return 1
    elif meets_threshold and drc_passed and seg_seg_violation_count == 0:
        return 0
    elif meets_threshold and (not drc_passed or seg_seg_violation_count > 0):
        # Meets completion threshold but has DRC or clearance violations
        return 3
    elif not meets_threshold and seg_seg_violation_count > 0:
        # Below threshold AND has seg-seg clearance violations
        return 4
    else:
        # Partial routing: some nets routed but below threshold
        return 2


if __name__ == "__main__":
    sys.exit(main())
