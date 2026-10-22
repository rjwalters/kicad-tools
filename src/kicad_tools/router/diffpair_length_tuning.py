"""Per-pair differential-pair length-match (skew) tuner.

Issue #2648, Epic #2556 Phase 3I.

This module implements :func:`tune_diff_pair_skew`, a router-internal
helper that inserts serpentines (trombones) on the *shorter* half of a
detected differential pair until the pair's skew is within the per-class
:meth:`~kicad_tools.router.rules.NetClassRouting.effective_skew_tolerance`
window, OR a cascade-safety budget of three insertion attempts is
exhausted.

Design notes
============

* **Outer-normal bulges only.**  Bulging the shorter half *toward* the
  partner trace would consume intra-pair coupling room and trigger an
  intra-pair clearance violation immediately.  The tuner therefore
  computes the partner trace's bearing at the chosen insertion segment,
  derives a unit outer-normal vector, and passes it to the trombone
  generator via the new :attr:`SerpentineConfig.outer_normal_hint`
  field (``side="outer"``).  See the
  :func:`kicad_tools.router.optimizer.serpentine.SerpentineGenerator.generate_trombone`
  implementation for how the hint is consumed.

* **Per-insertion DRC self-check.**  The first call in the chain has no
  collision-checking safety net -- :func:`add_serpentine` carries a
  literal ``TODO`` for that.  This module supplies the safety net via
  :func:`_post_insertion_clearance_ok`, which iterates the new
  serpentine segments against every other route's segments and rejects
  the insertion if any pair drops below the configured intra-pair
  clearance threshold.  On rejection the tuner discards the proposed
  ``new_route`` and returns the **original** ``route`` reference (and
  its original ``.segments`` list reference) -- the byte-for-byte
  rollback contract.

* **Cascade-safety budget N=3.**  A single trombone insertion can fall
  short of the target when the chosen segment is too crowded for enough
  amplitude; the tuner therefore re-evaluates and tries again, up to a
  bounded number of attempts.  Without this budget a near-zero-skew but
  unreachable target (a tight via cluster) could loop forever.

* **The longer half is never mutated.**  ``tune_diff_pair_skew`` only
  touches the shorter route.  The drift-prevention regression test
  asserts ``is``-identity on both the longer ``Route`` object and its
  ``.segments`` list.

Out of scope for Phase 3I:

* Multi-segment trombone splitting (very tight boards).
* Differential-pair coupling preservation during the bulge (today the
  bulge is on only the shorter half; the longer half is unchanged, so
  coupling is necessarily broken within the bulge.  Phase 3K's
  impedance-controlled rebuild is the place to restore coupling).
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .diffpair_bump_gates import (
    bump_amplitude_cap_mm,
    bump_gate_violation,
    jog_spaced,
    replaced_index,
)
from .optimizer.geometry import segment_length
from .optimizer.serpentine import (
    SerpentineConfig,
    SerpentineGenerator,
    SerpentineResult,
)
from .tuning_pattern import (
    STATUS_TOO_LONG,
    STATUS_TOO_SHORT,
    STATUS_TUNED,
    TuningPattern,
    retag,
    skew_bump_pattern,
    tag_skew_bump,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import Any

    from kicad_tools.validate.rules.diffpair_uncoupled_length import Copper

    from .diffpair_detection import DetectedPair
    from .grid import RoutingGrid
    from .primitives import Route, Segment


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class DiffPairTuneResult:
    """Outcome of a per-pair length-match tuning attempt.

    Attributes:
        success: True if the pair's skew is now within ``tolerance_mm`` AND
            the modifications passed the post-insertion DRC self-check at
            every attempt.  False if either no progress could be made
            (no insertable segment, geometry too tight) or every attempt
            failed the self-check.
        reason: Short machine-readable reason code.  One of:
            * ``"already_within_tolerance"`` -- the pair was already
              acceptable; no change was made.
            * ``"tuned"`` -- one or more trombones brought the pair into
              tolerance.
            * ``"exceeded_max_inserts"`` -- the cascade budget was
              exhausted before reaching tolerance.
            * ``"post_insertion_drc_violation"`` -- a candidate trombone
              would violate intra-pair (or neighbor) clearance; the
              insertion was rolled back and no further attempts were
              made on this pair.
            * ``"no_suitable_segment"`` -- the shorter route has no
              segment long enough to host any trombone amplitude.
            * ``"unrouted"`` -- one or both halves were not in
              ``routes_by_net``.
            * ``"not_length_critical"`` -- the engagement gate declined the
              pair (``length_critical=False``); no change was made.
            * ``"skew_limit_not_enforced"`` -- Issue #6664: in the
              checker-limit engagement mode the pair's skew limit resolves
              to ``skew_severity="warning"`` (an uncited protocol value
              ``kct check`` only warns on), so the router declines to
              tune it; no change was made.
            * ``"rolled_back_skew_worse"`` -- Issue #6664 backstop in the
              checker-limit path: the insertions would have left the
              checker-measured skew larger than before, so they were
              discarded; no change was made.
        attempts: Number of trombone insertions actually attempted.
        inserts_applied: Number of trombones whose post-insertion check
            passed and were committed.
        skew_before_mm: Pair skew (``|L_p - L_n|``) at entry.
        skew_after_mm: Pair skew after the (possibly empty) sequence of
            successful insertions.
        message: Human-readable summary.
        tolerance_mm: The skew limit the pair was tuned against (Issue
            #6664: reported next to the before/after skew).  ``None`` when
            the caller did not record it.
        gate_refusal: Issue #7521: the last AC3 bump-gate refusal (excursion,
            jog or mismatch-source proximity) of any candidate, ``""`` when no
            gate refused one.  A refused bump is never committed; when the
            pair ends outside its tolerance the refusal is also appended to
            ``message``, so it is never dropped silently behind a later,
            generic failure.
    """

    success: bool = False
    reason: str = ""
    attempts: int = 0
    inserts_applied: int = 0
    skew_before_mm: float = 0.0
    skew_after_mm: float = 0.0
    message: str = ""
    tolerance_mm: float | None = None
    gate_refusal: str = ""
    # The trombone results, kept for diagnostic / test inspection.  An
    # entry is present for every attempt (including rejected ones).
    serpentine_results: list[SerpentineResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


# Cascade-safety budget: cap the number of trombone insertions per pair.
# Three is enough to bring most realistic skews into tolerance at a 1 mm
# default amplitude, while keeping the worst case bounded for unreachable
# targets (very large skew on a congested board).
MAX_INSERTS_PER_PAIR: int = 3

# Issue #7521: the excursion cap (2x the pair gap, issue #7649) shrinks each bump, so
# the default budget grows with it, up to this multiple of
# :data:`MAX_INSERTS_PER_PAIR`.
MAX_INSERT_BUDGET_SCALE: int = 4

# Fraction of the skew tolerance to deliberately leave un-closed when
# sizing the trombone (issue #3543).  Closing 100% of the gap aims the
# serpentine at the partner's exact length; because loop length is
# quantized (45deg doglegs, integer loop counts) the artifact routinely
# *overshoots* by a fraction of a loop, landing the measured skew just
# *over* tolerance (the observed 0.501 mm against a 0.500 mm budget).
# Under-targeting by 0.9x tolerance banks ~0.45 mm of headroom (at the
# 0.5 mm default) so a quantization overshoot stays comfortably in-bounds,
# while the residual ~0.45 mm skew is itself still within tolerance.
SERPENTINE_TARGET_HEADROOM_FRACTION: float = 0.9

# Issue #7546: corner chamfer (in trace widths) of the skew tuner's
# plain-copper fallback bumps.
# kct and pinned kicad-cli 10.0.6 read the same uncoupled length for a plain
# bump of any chamfer, square included (Issue #7565 closed the square-loop
# divergence #7546 was first written against).  The half-width floor is
# where a chamfer starts to *pay*: below it a chamfered bump reads more
# uncoupled than a square one, and from it up less on thin traces.  On a
# 0.127 mm pair with 0.5 mm skew the plain figure is 0.90 mm square, 1.10 mm
# at 0.4 widths, 0.75 mm at 0.5 and 0.78 mm at 0.55, in both engines.  There
# a square plain bump reads the same as the square tuning pattern, so it
# would add no fallback option.  0.55 width keeps a margin well above the
# 0.1 um coordinate quantum while costing each loop only ~1.3 widths of
# added length.  Every chamfer is also uncoupled copper on both legs
# (45-degree runs couple to nothing), so a larger chamfer reads worse in
# both engines.
SKEW_BUMP_CHAMFER_WIDTHS: float = 0.55

# Issue #7521: corner chamfers (in trace widths) the gated tuner tries for its
# plain-copper bumps, in order.  Under the excursion cap (2x the pair gap,
# issue #7649) host length is what runs out, and a tighter chamfer leaves each
# leg more vertical rise: at a 0.225 mm trace and 0.267 mm amplitude a loop
# adds 0.24 mm over 1.85 mm of host at 0.55 widths, 0.40 mm over 1.58 mm at
# 0.25 and 0.45 mm over 1.49 mm at 0.15.  The whole cascade runs at each rung
# in turn, from the pair's untouched routes, and the first rung that brings
# the pair inside its limit wins; a pair the 0.55-width bumps already close is
# tuned exactly as before.  Every rung is a 45-degree chamfer (operator ruling
# Q32) under the same gates, and the uncoupled guard measures every bump (a
# chamfer under half a width can read more uncoupled than a square loop; the
# guard rejects a bump that breaks the pair's limit that way).
SKEW_BUMP_CHAMFER_LADDER: tuple[float, ...] = (SKEW_BUMP_CHAMFER_WIDTHS, 0.25, 0.15)


def tune_diff_pair_skew(
    pair: DetectedPair,
    routes_by_net: dict[int, Route],
    *,
    tolerance_mm: float,
    intra_pair_clearance_mm: float,
    config: SerpentineConfig | None = None,
    max_inserts: int = MAX_INSERTS_PER_PAIR,
    length_critical: bool = True,
    grid: RoutingGrid | None = None,
    prefer_reserved_slack: bool = False,
    fixed_segment_ids: set[int] | None = None,
    extra_clearance_ok: Callable[..., bool] | None = None,
    route_length: Callable[[Route], float] | None = None,
    max_uncoupled_mm: float | None = None,
    corner_chamfer_widths: float = 0.0,
) -> tuple[Route, Route, DiffPairTuneResult]:
    """Tune the skew of a detected diff pair by serpentining the shorter half.

    Args:
        pair: A detected differential pair (positive + negative).
        routes_by_net: ``{net_id: Route}`` lookup for all routed nets on the
            board.  Used both to fetch the pair's two halves AND as the
            corpus against which the post-insertion DRC self-check looks
            for newly-introduced clearance violations.
        tolerance_mm: The per-class skew tolerance from
            :meth:`NetClassRouting.effective_skew_tolerance`.  Pair is
            considered "matched" when ``|L_p - L_n| <= tolerance_mm``.
        intra_pair_clearance_mm: The per-pair intra clearance threshold
            from :meth:`NetClassRouting.effective_intra_pair_clearance`.
            Used by the post-insertion DRC self-check to verify the new
            segments do not violate the pair's own coupling rule.
        config: Optional :class:`SerpentineConfig` override.  When supplied
            its ``side`` and ``outer_normal_hint`` fields are overwritten by
            the tuner; other fields (amplitude, gap_factor, ...) are honored.
        max_inserts: Cascade-safety budget (default :data:`MAX_INSERTS_PER_PAIR`
            = 3).  The tuner stops after this many trombone attempts even
            if the pair remains out of tolerance.
        length_critical: Engagement gate (default ``True``).  When ``False``
            the tuner returns the pair unchanged with
            ``reason="not_length_critical"``.  Callers that integrate from
            the autorouter pipeline pass
            ``net_class_routing.length_critical`` here -- pairs whose net
            class is not flagged as length-critical are skipped, matching
            the curator's engagement-gate spec on Issue #2648.
        grid: Issue #4085 (Phase 1).  Optional routing grid.  When supplied
            together with ``prefer_reserved_slack=True``, the tuner biases
            its serpentine-insertion segment choice toward segments sitting
            inside the shorter half's own reserved slack corridor (the one
            widened by ``EscapeRouter._reserve_pair_continuation_corridor``)
            so the meander lands in already-protected space.  When ``None``
            or ``prefer_reserved_slack=False`` (both defaults) segment
            selection is byte-identical to the pre-#4085 geometric
            heuristic.
        fixed_segment_ids: Fixed escape segment identities; retained in measurement
            and clearance views, but never selected or replaced as meander hosts.
        prefer_reserved_slack: Issue #4085.  Gate for the slack-aware
            segment preference above.  Default ``False`` (inert).  Has no
            effect unless ``grid`` is also supplied.
        extra_clearance_ok: Issue #6664.  Optional extra post-insertion
            gate ``(new_segments, shorter_net_id, *, host_segments) -> bool``
            (``host_segments``: the segments the bump replaces) run after the
            built-in segment-vs-segment self-check.  ``False`` rolls the
            insertion back exactly like a built-in violation
            (``reason="post_insertion_drc_violation"``).  The lattice
            auto-engage path uses it for foreign pads, vias and net-class
            clearances; ``None`` (default) keeps the pre-#6664 check.
        route_length: Issue #6664.  Optional ``Route -> mm`` measure used
            for every length and skew the tuner compares.  The lattice
            auto-engage path passes the checker's via-inclusive measure
            (segments plus drilled via barrel length), so a pair with
            unequal via counts is tuned on the skew ``kct check`` reports
            and the *checker's* shorter half gets the bump.  The serpentine
            only ever adds segment length; via length is constant across
            insertions.  ``None`` (default) keeps the segment-only measure.
        max_uncoupled_mm: Issue #6795.  Optional uncoupled-length limit
            (the one ``kct check`` enforces).  A candidate meander that leaves
            the pair's figure -- ``max`` of both halves' uncoupled lengths,
            as the checker measures it -- over this limit AND above its
            pre-insert value is rejected like a failed placement and the next
            option is tried; when none fits the pair keeps its pre-insert
            state.  A pair already over the limit may still be tuned so long
            as the meander does not grow its uncoupled figure.
            ``None`` (default) applies no guard.
        corner_chamfer_widths: Issue #7546.  45-degree corner chamfer of the
            inserted trombones, in trace widths (see
            :attr:`SerpentineConfig.corner_chamfer_widths`); overrides
            ``config``.  ``0`` (default) keeps the square loops: the tuner
            writes its bumps as KiCad tuning patterns, and on 06b/06rf/07rf
            chamfered patterned bumps read 0.5-3.4 mm *more* uncoupled in
            pinned kicad-cli (a longer base line uncouples more partner
            copper).  :data:`SKEW_BUMP_CHAMFER_WIDTHS` is the chamfer the
            plain-copper fallback uses, where a plain bump starts to read
            less uncoupled than a square one.

    Returns:
        ``(p_route, n_route, result)`` where ``p_route`` and ``n_route`` are
        the (possibly tuned) Route references for the positive and negative
        halves.  The half that was *not* lengthened is returned **by
        reference** -- the same object the caller passed in
        ``routes_by_net`` -- with the same ``.segments`` list object.  If
        rollback fired, BOTH halves are returned by reference.

    Rollback contract:
        When the post-insertion DRC self-check fails, the proposed new
        Route is discarded and the original Route reference (the one in
        ``routes_by_net``) is returned with its original ``.segments``
        list object intact.  The test ``test_drift_prevention`` asserts
        this contract via ``is`` identity on both the longer route and
        its segments list.
    """
    # Issue #7521: gated bumps try each chamfer of the ladder in turn.
    ladder = (
        SKEW_BUMP_CHAMFER_LADDER if intra_pair_clearance_mm > 0.0 else (SKEW_BUMP_CHAMFER_WIDTHS,)
    )
    first: tuple[Route, Route, DiffPairTuneResult] | None = None
    for chamfer in ladder:
        outcome = _tune_diff_pair_skew_once(
            pair,
            routes_by_net,
            tolerance_mm=tolerance_mm,
            intra_pair_clearance_mm=intra_pair_clearance_mm,
            config=config,
            max_inserts=max_inserts,
            length_critical=length_critical,
            grid=grid,
            prefer_reserved_slack=prefer_reserved_slack,
            fixed_segment_ids=fixed_segment_ids,
            extra_clearance_ok=extra_clearance_ok,
            route_length=route_length,
            max_uncoupled_mm=max_uncoupled_mm,
            corner_chamfer_widths=corner_chamfer_widths,
            plain_chamfer_widths=chamfer,
        )
        if first is None:
            first = outcome
        if outcome[2].success or outcome[2].reason == "unrouted":
            return outcome
    # No rung closed the pair: report the first (0.55-width) attempt, as before.
    assert first is not None
    return first


def _tune_diff_pair_skew_once(
    pair: DetectedPair,
    routes_by_net: dict[int, Route],
    *,
    tolerance_mm: float,
    intra_pair_clearance_mm: float,
    config: SerpentineConfig | None,
    max_inserts: int,
    length_critical: bool,
    grid: RoutingGrid | None,
    prefer_reserved_slack: bool,
    fixed_segment_ids: set[int] | None,
    extra_clearance_ok: Callable[..., bool] | None,
    route_length: Callable[[Route], float] | None,
    max_uncoupled_mm: float | None,
    corner_chamfer_widths: float,
    plain_chamfer_widths: float,
) -> tuple[Route, Route, DiffPairTuneResult]:
    """One :func:`tune_diff_pair_skew` cascade with plain bumps chamfered at
    ``plain_chamfer_widths`` trace widths (issue #7521)."""
    p_id = pair.pair.positive.net_id
    n_id = pair.pair.negative.net_id

    # Engagement gate: pairs whose net class does NOT have length_critical=True
    # are not tuned (the curator's spec on Issue #2648).
    if not length_critical:
        p_route = routes_by_net.get(p_id)
        n_route = routes_by_net.get(n_id)
        result = DiffPairTuneResult(
            success=True,
            reason="not_length_critical",
            message=(
                f"Pair {pair.pair.positive.net_name}/{pair.pair.negative.net_name} "
                "is not length_critical; skipping tuning per engagement gate."
            ),
        )
        return (
            p_route if p_route is not None else _empty_route(p_id, pair.pair.positive.net_name),
            n_route if n_route is not None else _empty_route(n_id, pair.pair.negative.net_name),
            result,
        )

    p_route = routes_by_net.get(p_id)
    n_route = routes_by_net.get(n_id)
    if p_route is None or n_route is None:
        result = DiffPairTuneResult(
            success=False,
            reason="unrouted",
            message=(
                f"Pair {pair.pair.positive.net_name}/{pair.pair.negative.net_name} "
                "has an unrouted half; nothing to tune."
            ),
        )
        return (
            p_route if p_route is not None else _empty_route(p_id, pair.pair.positive.net_name),
            n_route if n_route is not None else _empty_route(n_id, pair.pair.negative.net_name),
            result,
        )

    from kicad_tools.validate.rules.diffpair_uncoupled_length import UNCOUPLED_EPSILON_MM

    from .length import LengthTracker  # avoid cycle

    measure = route_length or LengthTracker.calculate_route_length
    l_p = measure(p_route)
    l_n = measure(n_route)
    skew = abs(l_p - l_n)

    # Already within tolerance -- byte-for-byte unchanged.
    if skew <= tolerance_mm:
        return (
            p_route,
            n_route,
            DiffPairTuneResult(
                success=True,
                reason="already_within_tolerance",
                skew_before_mm=skew,
                skew_after_mm=skew,
                message=f"Pair already matched: skew={skew:.4f}mm <= tol={tolerance_mm:.4f}mm",
            ),
        )

    # The shorter half gets the trombone; the longer half is the target
    # length and is NEVER touched.
    if l_p < l_n:
        shorter_id, shorter_route, longer_id, longer_route = p_id, p_route, n_id, n_route
        longer_is_p = False
    else:
        shorter_id, shorter_route, longer_id, longer_route = n_id, n_route, p_id, p_route
        longer_is_p = True
    target_length = max(l_p, l_n)
    # Under-target by a fraction of the tolerance so quantized loop sizing
    # does not overshoot the partner length and push the measured skew just
    # *over* tolerance (issue #3543).  ``serpentine_target`` is what the
    # trombone generator aims for; ``target_length`` remains the partner's
    # true length and is used for skew measurement / early-stop below.  The
    # headroom is clamped so it never reaches/inverts the gap on a pair
    # whose skew is smaller than the headroom itself.
    target_headroom = min(
        SERPENTINE_TARGET_HEADROOM_FRACTION * tolerance_mm,
        skew,
    )
    serpentine_target = target_length - target_headroom
    original_longer_segments = longer_route.segments  # for rollback assertion

    # Snapshot the original shorter route -- we may need to roll back.
    original_shorter_route = shorter_route
    original_shorter_segments = shorter_route.segments

    # Issue #7546: optional chamfered corners (off by default).
    base_config = dataclasses.replace(
        config or SerpentineConfig(), corner_chamfer_widths=corner_chamfer_widths
    )
    # Issue #7521 (AC3): a bump stays within 2x the pair gap (the interim of
    # operator ruling Q35, issue #7649), so cap the loop amplitude there and
    # let the generator add loops instead.  Each insert then adds less, so the default cascade
    # budget scales with the cap (an explicit ``max_inserts`` is honoured).
    gated = intra_pair_clearance_mm > 0.0
    if gated:
        capped = min(base_config.amplitude, bump_amplitude_cap_mm(intra_pair_clearance_mm))
        if max_inserts == MAX_INSERTS_PER_PAIR and capped < base_config.amplitude:
            max_inserts *= min(
                MAX_INSERT_BUDGET_SCALE, max(1, round(base_config.amplitude / capped))
            )
        base_config = dataclasses.replace(base_config, amplitude=capped)

    result = DiffPairTuneResult(skew_before_mm=skew)
    current_shorter = shorter_route
    current_skew = skew
    # Committed bumps and their tuning patterns (issue #7521).
    bumps: list[tuple[list[Segment], TuningPattern]] = []

    # Cascade loop.  Each iteration: compute outer normal, attempt one
    # trombone, run the self-check, commit or reject.
    for attempt in range(max_inserts):
        result.attempts = attempt + 1

        # Pick the insertion segment first (shared by both the trombone
        # generator and the outer-normal calculation).  This mirrors the
        # logic in SerpentineGenerator.find_best_segment so the hint is
        # computed for the same segment that the generator will use.
        generator = SerpentineGenerator(base_config)  # provisional
        # Issue #4085: when a grid is supplied and the slack-preference gate
        # is on, bias the segment choice toward the shorter half's own
        # reserved slack corridor.  Otherwise this is byte-identical to the
        # pre-#4085 geometric-only selection.
        best = generator.find_best_segment(
            current_shorter,
            grid=grid if prefer_reserved_slack else None,
            reserved_net_id=shorter_id if prefer_reserved_slack else None,
            fixed_segment_ids=fixed_segment_ids,
        )
        if best is None:
            result.reason = "no_suitable_segment"
            result.message = (
                f"No segment long enough for a trombone on net {shorter_id} "
                f"(skew={current_skew:.4f}mm > tol={tolerance_mm:.4f}mm)"
            )
            # Inserts committed by earlier attempts stay (issue #6788); with
            # none committed the original route comes back by reference.
            if result.inserts_applied == 0:
                current_shorter = original_shorter_route
            break
        seg_idx, insertion_segment = best

        # Compute the outer-normal hint: the half-plane *away* from the
        # partner trace at the insertion segment's midpoint.
        hint = _outer_normal_hint(insertion_segment, longer_route)

        # An overshoot (the formerly shorter leg is now the longer one)
        # leaves nothing to add.  Stop with a distinct reason instead of
        # committing an empty serpentine that burns an insert (issue #6788).
        length_needed = serpentine_target - measure(current_shorter)
        if length_needed <= 0:
            result.reason = "overshoot"
            result.message = (
                f"Trombone on net {shorter_id} overshot the partner length "
                f"(skew={current_skew:.4f}mm > tol={tolerance_mm:.4f}mm)"
            )
            if current_skew >= skew:
                current_shorter = original_shorter_route
                # Reverted to the original route: no insert survives (#6795).
                result.inserts_applied = 0
            break

        candidate_route = None
        cand_bump: tuple[TuningPattern, list[Segment]] | None = None
        drc_rejected = False
        generator_failure = ""
        pre_insert_figure: float | None = None  # computed on first need (#6795)
        placement_kwargs = {
            "current_shorter": current_shorter,
            "first_host": (seg_idx, insertion_segment),
            "first_hint": hint,
            "longer_route": longer_route,
            "length_needed": length_needed,
            "grid": grid,
            "prefer_reserved_slack": prefer_reserved_slack,
            "shorter_id": shorter_id,
            "fixed_segment_ids": fixed_segment_ids,
            "serpentine_target": serpentine_target,
            "measure": measure,
        }
        for cand_route, cand_serp, as_pattern in _pattern_then_plain_options(
            base_config,
            max_uncoupled_mm is not None,
            placement_kwargs,
            plain_only=gated,
            plain_chamfer_widths=plain_chamfer_widths,
        ):
            result.serpentine_results.append(cand_serp)
            if not cand_serp.success:
                generator_failure = cand_serp.message
                continue
            # An option that adds no copper is not an insert (issue #6788).
            if measure(cand_route) - measure(current_shorter) <= 1e-9:
                generator_failure = "serpentine added no length"
                continue
            # Issue #7521 (AC3): excursion, jog and mismatch-source proximity.
            # Measured on the generated geometry, chamfered or square.
            gate_violation = bump_gate_violation(
                cand_serp.new_segments,
                current_shorter,
                replaced_index(current_shorter, cand_route),
                longer_route,
                nominal_gap_mm=intra_pair_clearance_mm,
            )
            if gate_violation is not None:
                generator_failure = gate_violation
                result.gate_refusal = gate_violation
                continue
            # Issue #7521: a pattern-form candidate (ungated tuning only) is
            # tagged before the uncoupled guard measures it (the measure
            # leaves pattern copper out, as kicad-cli 10.0.6 does).  Gated
            # bumps (operator ruling Q32) are chamfered plain copper, so the
            # guard counts their legs honestly.  Only the segments that leave
            # the host line are members.
            for seg in cand_serp.new_segments:
                seg.tuning_pattern = None
            cand_bump = (
                skew_bump_pattern(
                    cand_serp.new_segments,
                    longer_route,
                    diff_pair_gap=intra_pair_clearance_mm,
                    min_spacing=base_config.min_spacing * base_config.gap_factor,
                    tolerance_mm=tolerance_mm,
                )
                if as_pattern
                else None
            )
            if cand_bump is not None:
                tag_skew_bump(cand_bump[1], cand_bump[0])
            # Uncoupled-length guard (issue #6795): do not trade a skew error
            # for an uncoupled-length error.  The figure is the one
            # ``kct check`` judges -- the MAX of both halves' uncoupled
            # lengths -- so a meander that breaks the longer half's coupling
            # counts too.  Only a meander that leaves the pair over the limit
            # AND worse than before this insert is rejected: on a pair already
            # over the limit (on either half) the skew can still be fixed as
            # long as the uncoupled figure does not grow.
            #
            # Issue #7521: the figure leaves tuning-pattern copper out on both
            # sides, exactly as kicad-cli 10.0.6 does, so a pattern uncouples
            # the partner span opposite the bump.  A bump whose pattern form
            # fails is not retried as *square* plain copper: since Issue
            # #7565 kct reads a square plain bump exactly as kicad-cli does,
            # but on the tuner's square bumps that figure equals the square
            # pattern's (0.127 and 0.2 mm pairs, Issue #7593), so the retry
            # would add nothing there.
            #
            # Issue #7546: once every pattern option has failed, the same
            # placements are retried as *chamfered* plain copper
            # (:data:`SKEW_BUMP_CHAMFER_WIDTHS`, amplitude >= one width), which
            # reads less uncoupled than the square loop on thin traces (in
            # both engines; kct and kicad-cli measure plain bumps the same).
            # A bump is then committed plain only when that figure passes.  On
            # thin traces it can (0.127 mm, skew 0.5: pattern 0.90 mm, plain
            # chamfered 0.78 mm); on 0.2 mm traces the square pattern is
            # always at least as good.
            if max_uncoupled_mm is not None:
                cand_figure = _pair_uncoupled_mm(cand_route, longer_route)
                if cand_figure > max_uncoupled_mm + UNCOUPLED_EPSILON_MM:
                    if pre_insert_figure is None:
                        pre_insert_figure = _pair_uncoupled_mm(current_shorter, longer_route)
                    if cand_figure > pre_insert_figure + UNCOUPLED_EPSILON_MM:
                        generator_failure = (
                            f"uncoupled length exceeds limit ({cand_figure:.3f} mm > "
                            f"{max_uncoupled_mm:.3f} mm, was {pre_insert_figure:.3f} mm)"
                        )
                        continue
            # Post-insertion DRC self-check.  The candidate route's new
            # segments must not violate intra-pair clearance against the
            # partner OR collide with any other routed net's segments.
            if _post_insertion_clearance_ok(
                new_segments=cand_serp.new_segments,
                shorter_net_id=shorter_id,
                longer_net_id=longer_id,
                routes_by_net=routes_by_net,
                intra_pair_clearance_mm=intra_pair_clearance_mm,
            ) and (
                extra_clearance_ok is None
                or extra_clearance_ok(
                    cand_serp.new_segments,
                    shorter_id,
                    # Issue #7612: the host segment(s) the bump replaces.
                    host_segments=_replaced_segments(current_shorter, cand_route),
                )
            ):
                candidate_route = cand_route
                break
            drc_rejected = True

        if candidate_route is None:
            if drc_rejected:
                # Rollback: every placement failed.  Discard everything and
                # return the ORIGINAL shorter route (and the original longer
                # route reference).
                result.reason = "post_insertion_drc_violation"
                result.skew_after_mm = current_skew
                result.message = (
                    f"Post-insertion DRC self-check failed on net {shorter_id} "
                    f"(intra={intra_pair_clearance_mm:.4f}mm); rolled back."
                )
            else:
                result.reason = "no_suitable_segment"
                result.message = (
                    f"Trombone generation failed on net {shorter_id}: {generator_failure}"
                )
            # The drift-prevention contract.  Inserts committed by earlier
            # attempts stay (issue #6788); with none committed the original
            # route is returned by reference.
            if result.inserts_applied == 0:
                current_shorter = original_shorter_route
                assert current_shorter is original_shorter_route
                assert current_shorter.segments is original_shorter_segments
            break

        # Commit this attempt's new route and re-measure skew.
        current_shorter = candidate_route
        result.inserts_applied += 1
        if cand_bump is not None:
            bumps.append((cand_bump[1], cand_bump[0]))
        new_shorter_length = measure(current_shorter)
        current_skew = (
            abs(new_shorter_length - target_length)
            if longer_is_p
            else abs(target_length - new_shorter_length)
        )
        # Both branches reduce to abs(target_length - new_shorter_length)
        current_skew = abs(target_length - new_shorter_length)

        if current_skew <= tolerance_mm:
            result.success = True
            result.reason = "tuned"
            break
    else:
        # for/else: completed max_inserts without break (no success path).
        result.reason = result.reason or "exceeded_max_inserts"

    if result.reason == "" or result.reason == "exceeded_max_inserts":
        result.message = result.message or (
            f"Cascade budget exhausted (attempts={result.attempts}, "
            f"inserts_applied={result.inserts_applied}, "
            f"skew={current_skew:.4f}mm vs tol={tolerance_mm:.4f}mm)"
        )

    result.skew_after_mm = (
        abs(target_length - measure(current_shorter)) if result.inserts_applied > 0 else skew
    )
    # Issue #7521: a gate that refused a bump is named even when a later,
    # generic failure ended the cascade.
    if result.gate_refusal and not result.success and result.gate_refusal not in result.message:
        result.message = f"{result.message} (bump gate refused a candidate: {result.gate_refusal})"

    # Issue #7521: record the pair's final state on the bumps' patterns
    # (``last_status``), the way KiCad reports a saved pattern.
    if result.inserts_applied > 0 and bumps:
        final_shorter = measure(current_shorter)
        if abs(target_length - final_shorter) <= tolerance_mm:
            status = STATUS_TUNED
        elif final_shorter < target_length:
            status = STATUS_TOO_SHORT
        else:
            status = STATUS_TOO_LONG
        for members, pattern in bumps:
            if pattern.last_status != status:
                retag(members, pattern, last_status=status)

    # Assemble return values, restoring P/N polarity ordering.  The
    # longer route is ALWAYS returned by the same reference the caller
    # passed in -- the drift-prevention regression test asserts on this.
    if longer_is_p:
        # longer = P, shorter = N
        assert longer_route is p_route
        assert longer_route.segments is original_longer_segments
        return p_route, current_shorter, result
    else:
        # longer = N, shorter = P
        assert longer_route is n_route
        assert longer_route.segments is original_longer_segments
        return current_shorter, n_route, result


def _checker_segments(
    segments: list[Segment],
) -> list[tuple[float, float, float, float, str, float]]:
    """Segments as the ``(x1, y1, x2, y2, layer, width)`` tuples the uncoupled-length rule reads.

    Issue #7521: tuning-pattern copper (``Segment.tuning_pattern``) is left
    out, as ``kct check`` and pinned kicad-cli 10.0.6 leave it out.
    """
    return [
        (s.x1, s.y1, s.x2, s.y2, str(s.layer), s.width)
        for s in segments
        if s.tuning_pattern is None
    ]


def _pair_copper(a: Route, b: Route) -> Copper:
    """The pair's own tracks and vias as pairing obstacles (Issue #6554).

    ``kct check`` lets other copper stand between two parallel runs
    (``Copper``); in a candidate meander that copper is mostly the pair's own
    folded legs.  Pads and third-net copper are not in a ``Route``, so the
    guard sees only the pair.  Tuning-pattern copper is still copper here,
    as it is on the board.
    """
    from kicad_tools.validate.rules.diffpair_uncoupled_length import Copper

    tracks: list[tuple[float, float, float, float, float, str | None]] = [
        (s.x1, s.y1, s.x2, s.y2, s.width, str(s.layer)) for r in (a, b) for s in r.segments
    ]
    tracks += [(v.x, v.y, v.x, v.y, v.diameter, None) for r in (a, b) for v in r.vias]
    return Copper(tracks, ())


def _is_n_name(name: str) -> bool:
    return name.endswith(("-", "_N", "_n", "N"))


def _pair_uncoupled_mm(a: Route, b: Route) -> float:
    """The pair's uncoupled-length figure as ``kct check`` judges it (issue #6795).

    ``diffpair_uncoupled_length`` fails a pair on ``max(uncoupled(P),
    uncoupled(N))``, each half measured against the other, so this is
    symmetric in its arguments.  Tuning-pattern copper is excluded on both
    sides (issue #7521).  The pair's own copper may stand between two
    parallel runs, as in ``kct check`` (issue #6554); P's stored direction
    anchors the probe, so ``a`` leads unless it is the N net.
    """
    from kicad_tools.validate.rules.diffpair_uncoupled_length import measure_uncoupled

    sa = _checker_segments(a.segments)
    sb = _checker_segments(b.segments)
    copper = _pair_copper(a, b)
    a_leads = not (_is_n_name(a.net_name) and not _is_n_name(b.net_name))
    return max(
        measure_uncoupled(sa, sb, copper, a_leads, a.net_name, b.net_name),
        measure_uncoupled(sb, sa, copper, not a_leads, b.net_name, a.net_name),
    )


# ---------------------------------------------------------------------------
# Placement search (issue #6788)
# ---------------------------------------------------------------------------

#: Hosts tried per insert: the best-ranked segment plus the next-best ones.
MAX_HOST_CANDIDATES: int = 3

#: Amplitude back-off ladder applied to ``SerpentineConfig.amplitude`` when a
#: placement is rejected (same ladder the match-group tuner uses).  No factor
#: above 1: a taller loop pushes the bump out past the excursion cap
#: (2x the pair gap, issue #7649).
AMPLITUDE_LADDER: tuple[float, ...] = (1.0, 0.5, 0.25)


def exact_fit_amplitude(
    length_needed: float,
    max_amplitude: float,
    config: SerpentineConfig | None = None,
    width: float = 0.0,
) -> tuple[int, float]:
    """Return ``(loops, amplitude)`` adding exactly ``length_needed`` mm.

    Issue #7546: with a ``config`` whose loops have corner chamfers, the
    loop length model is :meth:`SerpentineConfig.loop_length_added` for a
    ``width`` trace instead of ``2 * amplitude``.

    A trombone loop adds ``2 * amplitude``.  The loop count is the fewest
    whose loops at ``max_amplitude`` cover the need, and the amplitude is then
    shrunk so those loops add the need and no more.  A single loop keeps the
    pre-#6788 sizing (``need / 2``) bit for bit; multi-loop sizing is nudged
    up by 1e-9 so ``ceil`` in the generator lands on the same loop count.
    """
    if config is not None and config.corner_chamfer_widths > 0.0:
        # Issue #7546: chamfered loops add less; size them with the same
        # model the generator uses.
        per_loop = config.loop_length_added(width, max_amplitude)
        loops = max(1, math.ceil(length_needed / per_loop))
        if loops == 1:
            # Nudged like the multi-loop case so float rounding in the
            # inverse never makes the generator's ``ceil`` add a loop.
            fit = config.amplitude_for(width, length_needed) * (1.0 + 1e-9)
            return 1, min(max_amplitude, fit)
        return loops, config.amplitude_for(width, length_needed / loops) * (1.0 + 1e-9)
    loops = max(1, math.ceil(length_needed / (2.0 * max_amplitude)))
    if loops == 1:
        return 1, min(max_amplitude, length_needed / 2.0)
    return loops, length_needed / (2.0 * loops) * (1.0 + 1e-9)


def _pattern_then_plain_options(
    base_config: SerpentineConfig,
    guarded: bool,
    placement_kwargs: dict[str, Any],
    *,
    plain_only: bool = False,
    plain_chamfer_widths: float = SKEW_BUMP_CHAMFER_WIDTHS,
) -> Iterator[tuple[Route, SerpentineResult, bool]]:
    """Yield ``(route, serpentine, as_pattern)`` placements (Issue #7546).

    ``plain_only`` (Issue #7521, operator ruling Q32) skips the tuning-pattern
    options: the gated bumps are 45-degree-chamfered plain copper, which
    KiCad's uncoupled figure counts as just the bump's two legs, so nothing is
    excluded by labelling copper as a pattern.

    First every :func:`_placement_options` placement as a tuning pattern
    (``base_config``'s loops).  When the uncoupled guard is active
    (``guarded``) the same search follows as plain copper with chamfered
    corners, which read less uncoupled than square loops on thin traces (kct
    and pinned kicad-cli 10.0.6 measure plain bumps identically); the caller
    only pulls it once every pattern option was rejected.
    """
    if not plain_only:
        for route, serp in _placement_options(base_config=base_config, **placement_kwargs):
            yield route, serp, True
    if not guarded and not plain_only:
        return
    plain_config = dataclasses.replace(
        base_config,
        corner_chamfer_widths=max(base_config.corner_chamfer_widths, plain_chamfer_widths),
    )
    for route, serp in _placement_options(base_config=plain_config, **placement_kwargs):
        yield route, serp, False
    if plain_only:
        # Issue #7521: last, bumps sized to the host they fit (partial ones).
        for route, serp in _partial_placement_options(base_config=plain_config, **placement_kwargs):
            yield route, serp, False


#: Issue #7521: where along its host a partial bump may sit, as the fraction of
#: the host's slack left before it (0 seats it at the host's start, 1 at its
#: end).  A neighbour that crowds only part of the host -- a via beside it, a
#: pad at one end -- leaves the bump room at another position.
PARTIAL_BUMP_ANCHORS: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)


def _partial_placement_options(
    *,
    base_config: SerpentineConfig,
    current_shorter: Route,
    first_host: tuple[int, Segment],
    first_hint: tuple[float, float],
    longer_route: Route,
    length_needed: float,
    grid: RoutingGrid | None,
    prefer_reserved_slack: bool,
    shorter_id: int,
    fixed_segment_ids: set[int] | None,
    serpentine_target: float,
    measure: Callable[[Route], float],
) -> Iterator[tuple[Route, SerpentineResult]]:
    """Yield bumps sized to the host they fit, partial ones included (issue #7521).

    Under the excursion cap (2x the pair gap, issue #7649) a bump that closes
    the whole skew can need more loops than any free stretch of host holds:
    board 06's USB2_D+ has a single 7.75 mm host with a foreign via beside its
    middle and a pad beside its start.  :func:`_placement_options` only offers
    full-need bumps (the start-anchored one trimmed to the loops that fit), so
    the cascade stopped there.  These options are the
    fallback, pulled only once every full-need option was rejected: per host
    and side, the most loops the host can hold (at most the need's worth, the
    loops that cover it sized to it) first, then fewer at the capped
    amplitude, each slid along the host (:data:`PARTIAL_BUMP_ANCHORS`).  The
    cascade commits the first one that passes every gate and clearance
    check and spends its next insert on the rest of the need, typically on the
    host left beside the bump.  Nothing is loosened: each option is gated like
    any other bump.
    """
    from .match_group_tuning import _centered_sub_host, _rank_candidate_segments
    from .primitives import Route

    hosts = [first_host]
    for idx, seg in _rank_candidate_segments(
        current_shorter,
        min_segment_length=base_config.min_segment_length,
        max_candidates=MAX_HOST_CANDIDATES + 1,
        fixed_segment_ids=fixed_segment_ids,
    ):
        if idx != first_host[0] and len(hosts) < MAX_HOST_CANDIDATES:
            hosts.append((idx, seg))

    cap = base_config.amplitude
    for host_no, (idx, host) in enumerate(hosts):
        length = math.hypot(host.x2 - host.x1, host.y2 - host.y1)
        spaced = jog_spaced(base_config, host.width)
        gap = spaced.min_spacing * spaced.gap_factor
        per_loop = spaced.loop_length_added(host.width, cap)
        if per_loop <= 0.0 or length <= 0.0:
            continue
        # The most loops a host this long holds (``_centered_sub_host``'s
        # sizing, the generator's 90% margin included), and no more than
        # the need takes.
        fit = int((0.9 * length - gap) / (2.0 * gap + 4.0 * spaced.chamfer_mm(host.width, cap)))
        most = min(fit, math.ceil(length_needed / per_loop), base_config.max_iterations)
        base_hint = first_hint if host_no == 0 else _outer_normal_hint(host, longer_route)
        for hint in (base_hint, (-base_hint[0], -base_hint[1])):
            for loops in range(most, 0, -1):
                if loops * per_loop >= length_needed:
                    # These loops cover the rest of the need: size them to
                    # it (as ``exact_fit_amplitude`` does), not past it.
                    amp = min(cap, spaced.amplitude_for(host.width, length_needed / loops))
                    target = length_needed
                else:
                    # Loops at the cap; just under their worth, so the
                    # generator's ``ceil`` lands on exactly ``loops``.
                    amp = cap
                    target = loops * per_loop * (1.0 - 1e-9)
                config = SerpentineConfig(
                    style=base_config.style,
                    amplitude=amp,
                    min_spacing=spaced.min_spacing,
                    min_segment_length=0.0,
                    gap_factor=base_config.gap_factor,
                    max_iterations=base_config.max_iterations,
                    side="outer",
                    outer_normal_hint=hint,
                    corner_chamfer_widths=base_config.corner_chamfer_widths,
                )
                seen: set[tuple[float, float]] = set()
                for anchor in PARTIAL_BUMP_ANCHORS:
                    sub_host, lead_in, lead_out = _centered_sub_host(
                        host,
                        loops=loops,
                        gap_mm=gap,
                        min_length_mm=0.0,
                        chamfer_mm=config.chamfer_mm(host.width, amp),
                        anchor=anchor,
                    )
                    key = (round(sub_host.x1, 6), round(sub_host.y1, 6))
                    if key in seen:
                        continue
                    seen.add(key)
                    serp = SerpentineGenerator(config).generate_trombone(sub_host, target)
                    if serp.success:
                        serp.new_segments = lead_in + serp.new_segments + lead_out
                    yield (
                        Route(
                            net=current_shorter.net,
                            net_name=current_shorter.net_name,
                            segments=current_shorter.segments[:idx]
                            + serp.new_segments
                            + current_shorter.segments[idx + 1 :],
                            vias=current_shorter.vias.copy(),
                            is_escape=current_shorter.is_escape,
                        ),
                        serp,
                    )


def _min_chamfered_amplitude(config: SerpentineConfig, width: float) -> float:
    """Smallest amplitude a chamfered bump may have (Issue #7546).

    A chamfered loop reads less uncoupled than a square one only when its
    corner chamfer is at least half a trace width (in kct and pinned kicad-cli
    10.0.6 alike), which needs an amplitude of at least one width (the
    chamfer is clamped to half the amplitude).  A
    smaller need is met by a one-width bump instead: it adds
    ``2 * width * (sqrt(2) - 1)`` and the skew lands inside the tolerance
    window on the other side.  ``0`` for square loops.

    Single-loop bumps only (Issue #7521): raising each of N loops to the
    floor multiplies the overshoot by N, and with the 2x excursion cap
    (issue #7649) a one-width loop's ``~0.83 W`` step is coarser than most
    pair tolerances, so multi-loop bumps keep their exact-fit amplitude.
    """
    # Issue #7521: never above the (excursion-capped) amplitude itself.
    return min(width, config.amplitude) if config.corner_chamfer_widths > 0.0 else 0.0


def _placement_options(
    *,
    base_config: SerpentineConfig,
    current_shorter: Route,
    first_host: tuple[int, Segment],
    first_hint: tuple[float, float],
    longer_route: Route,
    length_needed: float,
    grid: RoutingGrid | None,
    prefer_reserved_slack: bool,
    shorter_id: int,
    fixed_segment_ids: set[int] | None,
    serpentine_target: float,
    measure: Callable[[Route], float],
):
    """Yield ``(candidate_route, SerpentineResult)`` placements, best first.

    The first option is the pre-#6788 attempt (best host, outer-normal side,
    start-anchored) with exact-fit amplitude.  Later options are only pulled
    when the caller rejected the earlier ones: per ranked host, a meander
    centred on the host, then the opposite side, then the amplitude ladder.
    """
    from .length import LengthTracker  # avoid cycle
    from .match_group_tuning import _centered_sub_host, _rank_candidate_segments
    from .primitives import Route

    def config_for(
        amplitude: float,
        hint: tuple[float, float],
        width: float,
        min_segment_length: float | None = None,
    ) -> SerpentineConfig:
        # Issue #7521 (AC3): loop spacing wide enough for 3x-width jogs.
        spaced = jog_spaced(base_config, width)
        return SerpentineConfig(
            style=base_config.style,
            amplitude=amplitude,
            min_spacing=spaced.min_spacing,
            min_segment_length=(
                base_config.min_segment_length if min_segment_length is None else min_segment_length
            ),
            gap_factor=base_config.gap_factor,
            max_iterations=base_config.max_iterations,
            side="outer",
            outer_normal_hint=hint,
            corner_chamfer_widths=base_config.corner_chamfer_widths,
        )

    # First option: identical to the original attempt apart from sizing.
    first_loops, amplitude = exact_fit_amplitude(
        length_needed, base_config.amplitude, base_config, first_host[1].width
    )
    if first_loops == 1:
        amplitude = max(amplitude, _min_chamfered_amplitude(base_config, first_host[1].width))
    generator = SerpentineGenerator(config_for(amplitude, first_hint, first_host[1].width))
    seg_idx, host = first_host
    if fixed_segment_ids:
        serp = generator.generate_trombone(host, length_needed)
        yield (
            Route(
                net=current_shorter.net,
                net_name=current_shorter.net_name,
                segments=current_shorter.segments[:seg_idx]
                + serp.new_segments
                + current_shorter.segments[seg_idx + 1 :],
                vias=current_shorter.vias.copy(),
                is_escape=current_shorter.is_escape,
            ),
            serp,
        )
    else:
        # ``add_serpentine`` targets segment length; offset the
        # (insertion-invariant) via length out of the target.
        via_extra = measure(current_shorter) - LengthTracker.calculate_route_length(current_shorter)
        yield generator.add_serpentine(current_shorter, serpentine_target - via_extra)

    hosts = [first_host]
    for idx, seg in _rank_candidate_segments(
        current_shorter,
        min_segment_length=base_config.min_segment_length,
        max_candidates=MAX_HOST_CANDIDATES + 1,
        fixed_segment_ids=fixed_segment_ids,
    ):
        if idx != first_host[0] and len(hosts) < MAX_HOST_CANDIDATES:
            hosts.append((idx, seg))

    for host_no, (idx, host) in enumerate(hosts):
        base_hint = first_hint if host_no == 0 else _outer_normal_hint(host, longer_route)
        sides = [base_hint, (-base_hint[0], -base_hint[1])]
        # Anchors: ``None`` is the start-anchored host; 0.5 centres the
        # trombone and 1.0 seats it at the host's far end, which clears a
        # neighbour that only runs alongside the first part of the host
        # (Issue #7548: MIPI_RST ends 2.2 mm before MIPI_D0+'s corner).
        for anchor in (None, 0.5, 1.0):
            for hint in sides:
                for factor in AMPLITUDE_LADDER:
                    if host_no == 0 and anchor is None and hint == first_hint and factor == 1.0:
                        continue  # already yielded as the first option
                    loops, amp = exact_fit_amplitude(
                        length_needed, base_config.amplitude * factor, base_config, host.width
                    )
                    if loops == 1:
                        amp = max(amp, _min_chamfered_amplitude(base_config, host.width))
                    lead_in: list[Segment] = []
                    lead_out: list[Segment] = []
                    sub_host = host
                    # The end-anchored trombone is cut to just the length its
                    # loops need (no ``min_segment_length`` floor) so it sits
                    # as close to the host's end as a loop can (Issue #7548).
                    tight = anchor == 1.0
                    if anchor is not None:
                        sub_host, lead_in, lead_out = _centered_sub_host(
                            host,
                            loops=loops,
                            gap_mm=jog_spaced(base_config, host.width).min_spacing
                            * base_config.gap_factor,
                            min_length_mm=0.0 if tight else base_config.min_segment_length,
                            chamfer_mm=base_config.chamfer_mm(host.width, amp),
                            anchor=anchor,
                        )
                        if sub_host is host:
                            continue  # no slack: same as start-anchored
                    serp = SerpentineGenerator(
                        config_for(amp, hint, host.width, 0.0 if tight else None)
                    ).generate_trombone(sub_host, length_needed)
                    if serp.success:
                        serp.new_segments = lead_in + serp.new_segments + lead_out
                    yield (
                        Route(
                            net=current_shorter.net,
                            net_name=current_shorter.net_name,
                            segments=current_shorter.segments[:idx]
                            + serp.new_segments
                            + current_shorter.segments[idx + 1 :],
                            vias=current_shorter.vias.copy(),
                            is_escape=current_shorter.is_escape,
                        ),
                        serp,
                    )


# ---------------------------------------------------------------------------
# Outer-normal calculation
# ---------------------------------------------------------------------------


def _outer_normal_hint(
    segment: Segment,
    partner_route: Route,
) -> tuple[float, float]:
    """Return a unit vector pointing AWAY from the partner trace at ``segment``.

    The hint is computed at the midpoint of ``segment``: we find the
    closest point on the partner route, then return the unit vector from
    that closest point to the midpoint (i.e. AWAY from the partner).
    This guarantees the trombone will bulge into the outer half-plane
    of the pair, never toward the partner.

    Args:
        segment: The segment selected for trombone insertion on the
            shorter half.
        partner_route: The longer half (the partner trace).

    Returns:
        A unit vector ``(rx, ry)`` in the segment's layer plane.  When
        the partner has no segments (unusual but possible -- empty
        ``Route``), the segment's own geometric perpendicular is
        returned as a fallback so the trombone is still single-sided.
    """
    import math

    # Segment midpoint.
    mx = (segment.x1 + segment.x2) / 2.0
    my = (segment.y1 + segment.y2) / 2.0

    if not partner_route.segments:
        # Fallback: use the segment's own geometric perpendicular.  This
        # mirrors what the trombone would do without a hint, but the
        # caller still gets a single-sided bulge (``side="outer"``).
        dx = segment.x2 - segment.x1
        dy = segment.y2 - segment.y1
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0.0:
            return (0.0, 1.0)
        return (-dy / length, dx / length)

    # Find the closest point on the partner route to the midpoint.
    best_dist_sq = float("inf")
    best_cx, best_cy = 0.0, 0.0
    for pseg in partner_route.segments:
        cx, cy = _closest_point_on_segment(mx, my, pseg.x1, pseg.y1, pseg.x2, pseg.y2)
        d2 = (cx - mx) ** 2 + (cy - my) ** 2
        if d2 < best_dist_sq:
            best_dist_sq = d2
            best_cx, best_cy = cx, cy

    # Unit vector from closest partner point to segment midpoint.
    rx = mx - best_cx
    ry = my - best_cy
    mag = math.sqrt(rx * rx + ry * ry)
    if mag == 0.0:
        # The partner crosses the midpoint exactly; pick the segment's
        # own perpendicular as a fallback.
        dx = segment.x2 - segment.x1
        dy = segment.y2 - segment.y1
        length = math.sqrt(dx * dx + dy * dy)
        if length == 0.0:
            return (0.0, 1.0)
        return (-dy / length, dx / length)
    return (rx / mag, ry / mag)


def _closest_point_on_segment(
    px: float,
    py: float,
    sx1: float,
    sy1: float,
    sx2: float,
    sy2: float,
) -> tuple[float, float]:
    """Return the point on segment ``(sx1,sy1)->(sx2,sy2)`` closest to ``(px,py)``."""
    dx = sx2 - sx1
    dy = sy2 - sy1
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0.0:
        return (sx1, sy1)
    t = ((px - sx1) * dx + (py - sy1) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    return (sx1 + t * dx, sy1 + t * dy)


# ---------------------------------------------------------------------------
# Post-insertion DRC self-check
# ---------------------------------------------------------------------------


def _segments_within(a: Segment, b: Segment, margin: float) -> bool:
    """Cheap axis-aligned-bounding-box overlap test with ``margin``.

    Mirrors ``diffpair_routing._segments_within`` (issue #4460): when the two
    segments' AABBs do not come within ``margin`` of each other, no point of
    ``a`` can be within ``margin`` of ``b``, so the expensive
    ``segment_clearance`` distance computation can be skipped entirely.
    Never rejects a genuinely-close pair (the AABB is a superset of the
    segment), so this is a pure pre-filter -- it cannot change the result of
    :func:`_post_insertion_clearance_ok`, only how many segment pairs pay for
    the full geometric distance calculation.  Duplicated locally (rather than
    imported from ``diffpair_routing``) to avoid a module-load-time import
    cycle -- ``diffpair_routing`` already imports from this module.
    """
    if min(a.x1, a.x2) - margin > max(b.x1, b.x2):
        return False
    if max(a.x1, a.x2) + margin < min(b.x1, b.x2):
        return False
    if min(a.y1, a.y2) - margin > max(b.y1, b.y2):
        return False
    if max(a.y1, a.y2) + margin < min(b.y1, b.y2):
        return False
    return True


def _replaced_segments(before: Route, after: Route) -> list[Segment]:
    """Segments of ``before`` that ``after`` no longer carries (the bump's hosts)."""
    kept = {id(s) for s in after.segments}
    return [s for s in before.segments if id(s) not in kept]


def _post_insertion_clearance_ok(
    *,
    new_segments: list[Segment],
    shorter_net_id: int,
    longer_net_id: int,
    routes_by_net: dict[int, Route],
    intra_pair_clearance_mm: float,
) -> bool:
    """Return True if the proposed serpentine segments are DRC-safe.

    The check is two-pronged:

    1. Intra-pair clearance: every new segment is checked against every
       segment of the partner trace (``longer_net_id``).  Threshold is
       ``intra_pair_clearance_mm`` (the per-pair value the router used,
       passed in explicitly -- the curator's "don't go through
       check_diffpair_clearance_intra" guidance is satisfied because we
       use the same threshold without rebuilding the per-pair map).

    2. Inter-net clearance: every new segment is checked against every
       segment of every *other* routed net (excluding the shorter half
       itself, since the new segments will replace the original
       segment).  Threshold is ``intra_pair_clearance_mm`` as a
       conservative floor -- the architect's spec uses the same value
       so that bulging into a neighbor is rejected at least as
       aggressively as bulging into the partner.

    Both checks pre-filter each segment pair with a cheap AABB overlap test
    (:func:`_segments_within`, mirroring the ``diffpair_routing`` pattern
    from issue #4460) before paying for the full ``segment_clearance``
    distance computation -- on a board with many routed nets this loop is
    otherwise O(new_segments x total_other_segments) full geometric
    distance calls per insertion attempt, most of which are nowhere near
    the candidate trombone (issue #5240).

    Args:
        new_segments: The trombone segments produced by
            :meth:`SerpentineGenerator.generate_trombone`.
        shorter_net_id: Net id of the trace receiving the trombone.
        longer_net_id: Net id of the partner trace.
        routes_by_net: ``{net_id: Route}`` lookup for all routed nets.
        intra_pair_clearance_mm: Edge-to-edge clearance floor in mm.

    Returns:
        ``True`` if no clearance violation is introduced; ``False``
        otherwise (the caller must roll back).
    """
    from kicad_tools.core.geometry import segment_clearance

    # Pair-internal check.
    partner = routes_by_net.get(longer_net_id)
    if partner is not None:
        for new_seg in new_segments:
            for pseg in partner.segments:
                if pseg.layer != new_seg.layer:
                    continue
                # Margin must cover both trace half-widths in addition to the
                # clearance floor -- segment_clearance compares CENTER-line
                # distance minus both half-widths against the floor, so a
                # centerline-only margin could wrongly skip a pair the exact
                # check would have flagged.
                margin = intra_pair_clearance_mm + new_seg.width / 2 + pseg.width / 2
                if not _segments_within(new_seg, pseg, margin):
                    continue
                clearance = segment_clearance(
                    new_seg.x1,
                    new_seg.y1,
                    new_seg.x2,
                    new_seg.y2,
                    new_seg.width,
                    pseg.x1,
                    pseg.y1,
                    pseg.x2,
                    pseg.y2,
                    pseg.width,
                )
                if clearance + 1e-9 < intra_pair_clearance_mm:
                    return False

    # Neighbor check (all other nets).
    for other_net_id, other_route in routes_by_net.items():
        if other_net_id in (shorter_net_id, longer_net_id):
            continue
        for new_seg in new_segments:
            for oseg in other_route.segments:
                if oseg.layer != new_seg.layer:
                    continue
                margin = intra_pair_clearance_mm + new_seg.width / 2 + oseg.width / 2
                if not _segments_within(new_seg, oseg, margin):
                    continue
                clearance = segment_clearance(
                    new_seg.x1,
                    new_seg.y1,
                    new_seg.x2,
                    new_seg.y2,
                    new_seg.width,
                    oseg.x1,
                    oseg.y1,
                    oseg.x2,
                    oseg.y2,
                    oseg.width,
                )
                if clearance + 1e-9 < intra_pair_clearance_mm:
                    return False

    return True


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _empty_route(net_id: int, net_name: str) -> Route:
    """Return an empty :class:`Route` shell for an unrouted net.

    Used as a placeholder in the return value of
    :func:`tune_diff_pair_skew` when one half of the pair is unrouted --
    the caller can detect the situation via ``result.reason == "unrouted"``
    and the empty ``.segments`` list.
    """
    from .primitives import Route

    return Route(net=net_id, net_name=net_name, segments=[], vias=[])


# Re-export for ``from kicad_tools.router.diffpair_length_tuning import *``.
__all__ = [
    "MAX_INSERTS_PER_PAIR",
    "MAX_INSERT_BUDGET_SCALE",
    "SERPENTINE_TARGET_HEADROOM_FRACTION",
    "DiffPairTuneResult",
    "tune_diff_pair_skew",
]


# Reference suppression for static linters: segment_length is used as a
# debugging aid in interactive exploration and re-imported for callers
# (Phase 3I tests import it from this module to avoid two import paths).
_ = segment_length  # noqa: F401
