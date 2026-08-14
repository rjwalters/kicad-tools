"""Differential pair routing integration for the autorouter.

This module provides differential pair-aware routing functionality
that coordinates differential pair routing with the main autorouter.

Key features:
- Coupled A* pathfinding that routes both traces simultaneously
- Maintains constant spacing between P/N traces
- Length matching with serpentine compensation
"""

from __future__ import annotations

import collections
import heapq
import itertools
import logging
import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from .core import Autorouter
    from .cpp_backend import CppCoupledPathfinder
    from .grid import RoutingGrid
    from .rules import DesignRules, NetClassRouting

import contextlib

from kicad_tools.core.geometry import (
    segment_to_segment_distance as _segment_to_segment_distance,
)

from .crosstail_census import (
    CENSUS_COLLECTOR,
    CrossingTailCensusRecord,
    CrossingTailCensusSummary,
)
from .diffpair import (
    DifferentialPair,
    DifferentialPairConfig,
    LengthMismatchWarning,
    analyze_differential_pairs,
    should_engage_coupled,
)
from .diffpair_detection import (
    detect_diff_pairs as _layered_detect_diff_pairs,
)
from .diffpair_length import DiffPairLengthTracker
from .layers import Layer
from .observability import validate_net_connectivity
from .path import calculate_route_length
from .primitives import Pad, Route, Segment, Via
from .quantize import (
    OffAngleSegmentError,
    dogleg_points,
    verify_segment_45,
)
from .via_clearance import segment_via_deficit

logger = logging.getLogger(__name__)

# Issue #3508: dev-only periodic A* pop trace (KCT_COUPLED_TRACE=1).
_COUPLED_TRACE = bool(os.environ.get("KCT_COUPLED_TRACE"))

# Issue #3508: weighted-A* factor used by ``route_differential_pair_coupled``
# when constructing the per-pair :class:`CoupledPathfinder`.  See the
# ``heuristic_weight`` rationale in ``CoupledPathfinder.__init__``.
# Env-overridable for experimentation (KCT_COUPLED_HEURISTIC_WEIGHT).
COUPLED_HEURISTIC_WEIGHT: float = float(os.environ.get("KCT_COUPLED_HEURISTIC_WEIGHT", "1.5"))

# Issue #3547: default per-pair ITERATION budget for the coupled search
# when the shadow constructor is OFF (the default) and the caller plumbed
# no explicit budget.  The weighted-A* upgrade (#3508) is gated behind
# ``enable_shadow_construction``; with the flag off the search falls back
# to classic optimal A* (``heuristic_weight=1.0``), which floods the
# cost_turn-deep f-plateaus and explores far more joint states than the
# weighted search before reaching the ``cols * rows * 4`` memory backstop.
# On a deferring fixture (e.g. the DQS-like polarity-swap) classic A*
# grinds the full backstop -- ~75k iterations, ~2x the weighted search's
# wall-clock -- pushing existing flag-off tests to the CI 60s timeout.
# The contract for the flag-off path is "may only DEFER" (it never
# attempts coupled convergence -- that is #3508/#3542), so an unbounded
# grind is itself a smell: cap the classic-A* search at a budget that
# lets genuinely fast-converging pairs succeed while a deferring pair
# bails promptly (sets ``last_timeout_exceeded`` -> independent
# fallback), restoring the pre-#3508 budget-exit behaviour.  Env-
# overridable for experimentation (KCT_COUPLED_FLAGOFF_MAX_ITERS).  Only
# applied when no explicit ``per_pair_max_iterations`` was plumbed --
# callers that set a budget (the re-route gate, board configs) keep it.
#
# Issue #3547 (doctor follow-up): the first cap (16000 total, 8000/phase)
# was BELOW the classic-A* convergence floor for at least one flag-off
# pair the search CAN solve -- the pitch-mismatch USB fixture
# (test_diffpair_npad.py::test_pitch_mismatch_diff_pair_routes,
# ``coupled_only=True`` so NO independent fallback) reached
# ``best_progress=2`` (two grid cells from the joint goal) then bailed at
# the 8000-iter corridor cap, returning ``[]``.  That changed the OUTCOME
# (a pair that used to route coupled produced no routes) rather than
# merely DEFERRING -- a flag-off contract violation.  Empirically the npad
# fixture converges between 9000 and 12000 iters/phase; the DQS-like
# deferring fixture grinds the full ``cols*rows*4`` (~75k) and lands ~5s
# on the CI no-coverage path regardless.  40000 total (20000/phase) sits
# in that wide window: ~1.7x above npad's convergence floor (npad passes
# in ~1.3s) while DQS still bails comfortably under the 60s CI timeout.
COUPLED_FLAGOFF_MAX_ITERATIONS: int = int(os.environ.get("KCT_COUPLED_FLAGOFF_MAX_ITERS", "40000"))

# Issue #3508: max joint remaining Manhattan distance (grid cells, max
# over the two heads) at which a budget-exited coupled search qualifies
# for the near-miss rescue (commit the coupled body, finish each side
# single-ended).  60 cells = 3 mm on a 0.05 mm grid -- generous against
# the measured 5-21-cell stalls, small against the 30-50 mm pair routes
# so the coupled-length fraction stays >> every continuity threshold.
NEAR_MISS_RESCUE_CELLS: int = int(os.environ.get("KCT_COUPLED_RESCUE_CELLS", "60"))

# Issue #3508: maximum length (mm) the shadow constructor may trim from
# EACH end of the offset polyline before tail-connecting to the pads.
# Endpoint zones are always contested by neighbour-pad clearance halos
# (connector pin rows, QFN/QFP rings), so some trim is expected; a trim
# beyond this bound means a mid-route obstacle wall and the shadow is
# declined for that side.  5 mm against board 06's 18-45 mm pair runs
# keeps the coupled-length fraction comfortably above the 0.7-0.9
# continuity thresholds.
_SHADOW_MAX_TRIM_MM: float = float(os.environ.get("KCT_SHADOW_MAX_TRIM_MM", "5.0"))

# Issue #4462: the largest separation between two consecutive parallel-offset
# endpoints that may be treated as "already coincident" and joined by snapping
# rather than by emitting a connector.  One 0.1 um serialization quantum: below
# it the two endpoints round to the same 4-dp coordinate in the .kicad_pcb, so
# snapping is exact and free.  ANY larger separation must be joined with real
# copper -- an unjoined sub-grid-cell step is a break in the polyline, which
# costs the whole coupled pair via the #3540 transactional strand guard.
_OFFSET_JOIN_COINCIDENT_MM: float = 1e-4

# Issue #3987 (unit 2a of #3921): hard per-pair wall-clock budget for the
# shadow-construction coupled attempt.  When ``enable_shadow_construction``
# is on, a pair is routed either as a validated parallel shadow (ms) or it
# is DEFERRED to the uncoupled fallback -- it must NOT fall through to the
# open joint-state A* search, which floods the cost_turn f-plateaus and
# drove the >1200s tail the #3986 board-06 measurements documented (6 of 9
# failed-shadow pairs each burned a ~45s corridor probe plus the negotiated
# 360s backstop).  This budget bounds the corridor probe + shadow
# construction per pair; on shadow failure the search fails fast to the
# uncoupled fallback without re-flooding the open A*.  Env-overridable for
# experimentation (KCT_SHADOW_PER_PAIR_BUDGET_S).
_SHADOW_PER_PAIR_BUDGET_S: float = float(os.environ.get("KCT_SHADOW_PER_PAIR_BUDGET_S", "30.0"))

# Issue #4463: budgets for the corridor-yield recovery
# (``DiffPairRouter._plan_corridor_yields`` / ``_apply_corridor_yields``),
# which runs after the main strategy when shadow construction is ON.  On
# board 06 shadow-ON (seed 42) three nets sealed in by committed coupled
# bodies cost the negotiated loop its full 10-iteration ceiling -- 362.3s to
# arrive at the same 18/21 it had after iteration 1 -- because that copper is
# non-rippable there.
#
#   * ``_CORRIDOR_GUARD_PROBE_S`` bounds one routability probe.  A probe that
#     succeeds does so in well under a second; only the hopeless ones need
#     bounding.
#   * ``_CORRIDOR_GUARD_BUDGET_S`` bounds the whole planning pass -- past it
#     the plan is truncated and the board keeps what the strategy produced.
#   * ``_CORRIDOR_YIELD_RERUN_S`` caps the ONE strategy re-run performed after
#     the yield.  Without a cap the re-run spends the recipe's full negotiated
#     backstop a second time (measured: 363s, taking the job from 770s to
#     1376s).  Applied via ``Autorouter._negotiated_timeout_cap`` and removed
#     as soon as the re-run returns.
#   * ``KCT_CORRIDOR_YIELD=0`` is the kill-switch for the whole #4463
#     behaviour (both the negotiated loop's fixed-point exit and the
#     plan/yield/re-run recovery); it restores the pre-#4463 shadow-ON
#     pipeline exactly, which is how the before/after numbers above were
#     measured on one build.  Shadow-OFF runs never reach either mechanism.
_CORRIDOR_GUARD_PROBE_S: float = float(os.environ.get("KCT_CORRIDOR_GUARD_PROBE_S", "8.0"))
_CORRIDOR_GUARD_BUDGET_S: float = float(os.environ.get("KCT_CORRIDOR_GUARD_BUDGET_S", "120.0"))
_CORRIDOR_YIELD_RERUN_S: float = float(os.environ.get("KCT_CORRIDOR_YIELD_RERUN_S", "300.0"))
_CORRIDOR_YIELD_ENABLED: bool = os.environ.get("KCT_CORRIDOR_YIELD", "1").strip() != "0"

# Issue #3990 (unit 2b of #3921): variable-gap parallel offset.  The
# geometric shadow constructor historically offset the WHOLE guide by a
# single constant center-to-center gap ``d = spacing_cells * resolution``.
# At the tightened 0.225-0.275 mm coupled widths this fixed gap is
# infeasible for 6/9 board-06 pairs: on inside curves the offset overlaps
# the partner (the -0.165..-0.275 mm ``self-check overlap`` events) and
# where the guide threaded a gap only wide enough for a zero-width
# centerline the offset crosses an obstacle (the ``mid-route blockage``
# events).  Because the diff-pair coupling constraint is a clearance BAND
# (a floor set by ``effective_intra_pair_clearance()`` and a ceiling set by
# the impedance tolerance), the offset gap may be varied PER SECTION within
# ``[d_min, d_max]`` to dodge both failure modes -- tighten toward
# ``d_min`` on an inside curve that would self-overlap, widen toward
# ``d_max`` to step around an obstacle -- while both legs stay inside the
# impedance band.  ``_SHADOW_GAP_BAND_STEPS`` is the number of candidate
# gaps probed per section (a small linear ladder from ``d_min`` to
# ``d_max``); the tightest feasible gap that clears both the partner and
# the grid is kept.  ``_SHADOW_GAP_MAX_TOL_FRAC`` caps how far above the
# nominal gap the ceiling may reach when the net class exposes no explicit
# impedance tolerance.  Env-overridable for bench experimentation.
_SHADOW_GAP_BAND_STEPS: int = int(os.environ.get("KCT_SHADOW_GAP_BAND_STEPS", "5"))
_SHADOW_GAP_MAX_TOL_FRAC: float = float(os.environ.get("KCT_SHADOW_GAP_MAX_TOL_FRAC", "0.15"))

# Issue #4460 (approach 2): a guide polyline "self-approach" is a non-adjacent
# loop-back -- two segments whose midpoints are close in space but far apart
# along the path.  The primary gate is the proximity window plus an
# ``arc_sep >= proximity`` floor: on an open (non-looping) curve, points far
# enough apart in arc are also far in space, so only a genuine fold back within
# the offset clearance satisfies both.  This ratio is a mild extra guard that
# rejects near-straight runs whose arc barely exceeds their chord (ratio ~1);
# a fold within the offset clearance has arc/chord >= ~2 (USB3_RX1's offending
# pair: ~0.9mm of arc over ~0.4mm of chord, ratio ~2.25).
_GUIDE_SELF_APPROACH_DETOUR_RATIO: float = float(
    os.environ.get("KCT_GUIDE_SELF_APPROACH_DETOUR_RATIO", "1.5")
)
# Issue #4460 (approach 2): how many times to stack the per-site avoidance
# boost when re-routing a guide away from a shadow pinch.  Each pass adds a
# fixed cost over the boosted neighbourhood; one pass is too weak to divert the
# A* out of a dense escape fan, so accumulate several.
_GUIDE_BIAS_BOOST_REPEATS: int = int(os.environ.get("KCT_GUIDE_BIAS_BOOST_REPEATS", "6"))
# Issue #4460: opt-in verbose provenance for shadow self-check declines.  When
# set, every ``self-check overlap`` decline prints WHICH part of the assembled
# shadow (start tail / body / end tail) produced the offending segment and where
# it sits relative to the guide.  Diagnostic-only; off by default.
_SHADOW_DEBUG: bool = os.environ.get("KCT_SHADOW_DEBUG", "0") == "1"

# A bare (x, y) via site in the crossover candidate lattice (issue #4580).
_XY = tuple[float, float]

# Issue #4580: opt-in crossover LEGALITY census.  See
# ``DiffPairRouter._synthesize_crossing_tail`` for what it measures and why the
# first-legal loop cannot answer the question on its own.  Diagnostic-only, off
# by default, and observation-only when on -- it never changes which route
# ships.  Costs a full 225-candidate sweep per crossover.
#
# Issue #4635 -- STATE-neutral is not BUDGET-neutral.  PR #4611's prose claimed
# the census "cannot" cause a routing difference because it is "observation-only
# by construction".  That is true of router STATE: every gate the scan calls
# past the accept point is mutation-free, so continuing the sweep cannot perturb
# anything.  It was NOT true of the wall-clock BUDGET: the census runs inside the
# window ``spec_t0`` opens in ``route_differential_pair_coupled``, and every
# downstream deadline there is computed as ``<budget> - (now - spec_t0)``, so
# census seconds were silently deducted from the probes that follow.  A census-on
# run could therefore differ from a census-off run through budget pressure alone
# -- with no state mutation anywhere.  It now credits its own INCREMENTAL cost
# (the sweep after the first legal candidate) back via
# ``DiffPairRouter._census_elapsed_s``, so census-on and census-off runs get the
# same effective downstream budget and are genuinely comparable.  The credit
# means a census-on pair's TRUE wall clock may exceed
# ``_SHADOW_PER_PAIR_BUDGET_S`` by the census's own cost -- a deliberate trade
# for a default-off diagnostic; the true elapsed time stays visible in the
# per-pair timing log and in the ``census_s=`` field of each census header.
_CROSSTAIL_CENSUS: bool = os.environ.get("KCT_CROSSTAIL_CENSUS", "0") == "1"
# How many legal candidates the census lists per crossover before truncating.
# The count in the header line is always the true total, never the listed one.
_CROSSTAIL_CENSUS_LIST: int = 12

# Issue #4553: guide pre-simplification.  The C++ per-net A* emits one segment
# per grid cell, so a board-06 guide is a ~400-segment staircase whose longest
# contiguous collinear run is ~0.3 mm.  Every micro-corner costs the parallel
# offset a miter join, and the fragmentation starves every length tuner (which
# needs a straight run of ``SerpentineConfig.min_segment_length`` = 2.0 mm to
# insert a trombone).  Compressing the guide polyline BEFORE offsetting fixes
# both at once -- and, unlike running the trace optimizer AFTER the pair is
# committed (which board-06's recipe deliberately skips for diff-pair nets
# because it straightens one leg and destroys the constant-gap geometry), it
# keeps the pair coupled: the shadow is offset from the ALREADY-simplified
# guide, so both legs get the same long runs.  ``MAX_DEV`` bounds how far the
# compressed path may stray from the corridor the router chose.
_SHADOW_SIMPLIFY_GUIDE: bool = os.environ.get("KCT_SHADOW_SIMPLIFY_GUIDE", "1") == "1"
_SHADOW_SIMPLIFY_MAX_DEV_MM: float = float(os.environ.get("KCT_SHADOW_SIMPLIFY_MAX_DEV_MM", "0.75"))
_SHADOW_SIMPLIFY_MAX_WINDOW: int = int(os.environ.get("KCT_SHADOW_SIMPLIFY_MAX_WINDOW", "80"))

# Issue #4553: construction-time length symmetry.  Measured on board-06
# (shadow-ON, seed 42), the constructed shadow is ALWAYS the LONGER leg --
# ``shadow - guide`` decomposes as (parallel-offset excess +0.7..2.1 mm) +
# (landing tails 1.0..9.8 mm) - (body trims 0..5.2 mm) + (dogleg quantize tax
# 0.0..0.36 mm).  The tails dominate; the quantize tax is under 1%, so the
# curated "dogleg tax is the dominant driver" hypothesis does NOT hold on the
# measured data.  Two construction-time levers close the gap instead:
#
#   1. Guide compression (above) -- most of the tail/trim excess comes from
#      shadow bodies that could not be offset cleanly off a 400-corner
#      staircase, so the constructor fell back to deep trims and long rescue
#      tails.  Offsetting a compressed guide removes that at the source.
#   2. A BOUNDED LATERAL-JOG MEANDER on the SHORTER leg (see
#      :func:`lateral_jog_polyline`), applied while the pair is still under
#      construction and before the clearance self-check gates, for whatever
#      residual remains.
#
# The meander is capped at ``MAX_FRAC`` of the leg it lengthens so a pair can
# never be turned mostly-meander (which would sink the coupled fraction the
# ``diffpair_routing_continuity`` rule measures).
_SHADOW_LENGTH_MATCH: bool = os.environ.get("KCT_SHADOW_LENGTH_MATCH", "1") == "1"
_SHADOW_MEANDER_MAX_FRAC: float = float(os.environ.get("KCT_SHADOW_MEANDER_MAX_FRAC", "0.25"))
# Largest / smallest per-tooth lateral excursion, in grid cells.
_SHADOW_MEANDER_MAX_CELLS: int = int(os.environ.get("KCT_SHADOW_MEANDER_MAX_CELLS", "12"))
_SHADOW_MEANDER_MIN_CELLS: int = int(os.environ.get("KCT_SHADOW_MEANDER_MIN_CELLS", "3"))

# Issue #4571: exact foreign-pad clearance gate for constructed copper.
#
# Every shadow validation gate rasterises against ``_is_cell_blocked``, whose
# pad halo is INTENTIONALLY shrunk in fine-pitch corridors (``RoutingGrid.
# _clearance_for_pin_pitch``, "full manufacturer clearance is validated in
# post-routing DRC").  For single-ended nets that promise is kept by
# ``Autorouter._demote_pad_clearance_violation_nets`` (exact, but only demotes
# above one grid resolution) plus ``drc_verify_and_nudge`` (the sub-resolution
# remainder) -- and ``drc_verify_and_nudge`` unconditionally SKIPS diff-pair
# nets (#3508: the nudge helpers are not partner-aware).  So constructed
# coupled copper had no stage, coarse or exact, that agreed with the
# ``clearance_pad_segment`` DRC predicate, and shipped sub-resolution pad
# grazes and outright pad overlaps (measured on board-06 shadow-ON, seed 42:
# MIPI_CLK- over MIPI_D0+/MIPI_D0- pads at 0.037 mm and over its own partner's
# MIPI_CLK+ pads at 0.015 mm).
#
# The gate below re-uses the exact-geometry primitives the single-ended
# backstop uses (``RoutingGrid.worst_segment_pad_deficit`` /
# ``worst_via_pad_deficit``) at construction time, with ``exclude_net`` ONLY --
# no ``exclude_refs``.  Passing the net's own component refs (the way the
# single-ended backstop does) would hand the pads of a P/N pair that shares one
# connector ref straight to the #3545 same-component carve-out and silently
# re-exempt the partner's pad on exactly the fine-pitch connectors this gate
# exists for.
#
# ``_SHADOW_PAD_DEFICIT_EPS`` is a geometric noise floor (a serialization
# quantum), NOT a clearance relaxation: it must stay far below the grid
# resolution, because unlike the single-ended path there is no downstream
# repair pass to absorb whatever this gate lets through.
_SHADOW_PAD_DEFICIT_EPS: float = 1e-4
# Chunk length used to localise WHERE along a body segment a pad deficit sits,
# so the existing end-trim machinery can shave a grazing landing instead of
# declining the whole side.
_SHADOW_PAD_PROBE_STEP_MM: float = 0.2

# Issue #4575: the segment-vs-VIA quadrant of the same validation gap.  The
# constructor's only via-aware self-check is ``_pair_has_physical_overlap``,
# which is an OVERLAP detector (``via_r + seg_w/2``, no clearance term), and the
# partner universe handed to every tail screen is ``list(guide.segments)`` -- it
# contains no vias at all.  So a constructed leg passing 0.090 mm from the
# partner leg's barrel is accepted by every construction-time gate and then
# reported by ``clearance_segment_via`` at 0.102 mm (measured on board-06
# shadow-ON seed 42: USB3_TX1+ copper vs a USB3_TX1- barrel).  A diff-pair net
# is excluded from ``drc_verify_and_nudge`` (#3508), so nothing downstream
# repairs it.
#
# THRESHOLD (load-bearing): ``ClearanceRule`` exempts a declared diff pair from
# the generic clearance check ONLY when both elements are SEGMENTS
# (``validate/rules/clearance.py`` -- the #2560 scoping).  A segment-vs-via or
# via-vs-via pair between P and N is therefore checked at the full board
# minimum.  This gate must use ``rules.trace_clearance`` /
# ``rules.via_clearance``, NOT ``_pair_seg_clearance``'s intra-pair relaxation:
# the relaxed bound is deliberately tighter than the manufacturer clearance, so
# a gate built on it could never fire on the very finding it exists to close.
#
# Same noise floor as the pad quadrant (one serialization quantum, and the same
# value as ``DRC_TOLERANCE``) so the gate and the checker agree at the boundary.
_SHADOW_VIA_DEFICIT_EPS: float = _SHADOW_PAD_DEFICIT_EPS
# Mirrors ``validate.rules.clearance._COLOCATION_EPSILON_MM`` (#2706): the
# in-pad-escape router places segment endpoints EXACTLY at via centres, and the
# DRC skips such pairs.  A gate without the same carve-out would be stricter
# than the checker and decline sides over geometry that is never reported --
# pure reach loss for zero DRC gain.
_SHADOW_VIA_COLOCATION_EPS: float = 1e-4

# Issue #4574: the constructed crossover's via sites are chosen FIRST-LEGAL out
# of a fixed 3x5 lattice, so the winning site carries no information about what
# the rest of the board still has to route.  An all-layer F.Cu<->B.Cu barrel is
# an obstacle on EVERY layer for EVERY later net, and the lattice's preferred
# sites sit -- by construction -- in the middle of the connector's escape field.
# On board-06 that seals MIPI_D0's only channel (its corridor probe returns
# ``guide_route=FAILED segments=0``, i.e. sealed rather than congested).
#
# The remedy is a PREFERENCE, never a new gate: every legality check keeps its
# place and its veto, and the candidate lattice is merely re-ORDERED so that,
# among sites that are already legal, one that leaves a not-yet-routed pad's
# direct escape open is tried before one that plugs it.
#
# ``_ESCAPE_CHANNEL_REACH_PITCHES`` bounds how far out from a pad the escape is
# treated as un-detourable, in multiples of that pad's own component pitch.
# Close to a fine-pitch pad the neighbouring pads' halos leave no room to route
# around a barrel; a few pitches out the net has the whole board to detour
# through, so scoring there would be noise rather than signal.
_ESCAPE_CHANNEL_REACH_PITCHES: float = 3.0


class _EscapeChannel(NamedTuple):
    """A not-yet-routed pad's direct escape ray (issue #4574).

    ``(x, y)`` is the pad centre, ``(ux, uy)`` the unit direction toward the
    rest of that net's pads (the shortest way out is the way it has to go),
    and ``reach`` how far along that ray the channel is considered sealed by
    a barrel rather than merely inconvenienced by one.
    """

    x: float
    y: float
    ux: float
    uy: float
    reach: float


def _channel_seal_penalty(
    x: float,
    y: float,
    keepout: float,
    channels: list[_EscapeChannel],
) -> float:
    """How deeply a via barrel at ``(x, y)`` plugs other nets' escapes (#4574).

    ``keepout`` is the centre-to-centre distance a foreign trace must keep
    from the barrel (``via_diameter/2 + trace_clearance + trace_width/2``), so
    a barrel closer than that to a channel's ray makes the pad's direct escape
    illegal.  The penalty is the summed intrusion depth over every channel the
    barrel reaches into -- ``0.0`` means the barrel leaves every modelled
    escape exactly as it found it, which is the overwhelmingly common case and
    the one that preserves today's first-legal behaviour verbatim.
    """
    penalty = 0.0
    for ch in channels:
        dx = x - ch.x
        dy = y - ch.y
        along = dx * ch.ux + dy * ch.uy
        if along <= 0.0 or along >= ch.reach:
            continue  # behind the pad, or far enough out to be detoured
        lateral = abs(dx * ch.uy - dy * ch.ux)
        if lateral >= keepout:
            continue  # the direct escape still fits past this barrel
        penalty += keepout - lateral
    return penalty


class _ShadowForeignCopper(NamedTuple):
    """Copper the constructed leg must keep VIA clearance from (issue #4575).

    ``vias`` and ``segments`` are flat snapshots unioning the board's
    committed routes with the pair's own SIBLING legs -- the guide, and
    (during the late one-leg-at-a-time mutations of #4553's length matcher
    and #4570's via mirror) the other constructed leg.  The sibling legs are
    never in the routing grid during shadow construction, which is exactly
    why no raster check can see them.

    Elements carry their own ``net``, so same-net filtering is a per-element
    comparison at query time -- ``exclude_net`` semantics with no
    ``exclude_refs`` anywhere (the #3545 same-component carve-out would
    exempt the partner's barrel whenever P and N share a fine-pitch
    connector ref, which is exactly the board-06 case this gate exists for).

    See :meth:`DiffPairRouter._shadow_foreign_copper` for how the snapshot is
    built and why ``autorouter.routes`` (not ``grid.routes``) is the source.
    """

    vias: tuple[Via, ...]
    segments: tuple[Segment, ...]


# Issue #4572: the constructor's landing tails are the pair's uncoupled copper.
#
# A constructed pair is guide + parallel shadow, but the shadow BODY is trimmed
# at both ends (endpoint zones are contested by connector/IC pad halos) and
# reconnected to the real pads by ``_synthesize_tail`` -- which enumerated
# candidates by SHAPE (direct, two doglegs, then widening U-detours) and
# returned the FIRST one that was legal.  Legality means "clears the raster",
# "keeps ``partner_clearance`` from the guide" (a minimum-distance FLOOR) and,
# since #4571, "clears exact foreign-pad geometry" -- nothing ever preferred a
# tail that ran ALONGSIDE the partner.  Both legs pay for that: the tail is
# uncoupled copper on its own leg, and the partner copper it failed to follow
# is uncoupled on the OTHER leg, because ``diffpair_routing_continuity`` scores
# a pair as ``(frac_a + frac_b) / 2`` (measured on board-06 shadow-ON seed 42:
# 7.2 mm of uncoupled copper on USB3_TX1's un-meandered leg).
#
# These two constants are the exact predicate that rule measures against.  They
# are mirrored here (rather than imported) to keep ``router`` free of a
# module-level ``validate`` dependency; ``tests/test_diffpair_shadow.py``
# asserts they still equal ``DEFAULT_COUPLING_WINDOW_MM`` /
# ``DEFAULT_PARALLEL_TOLERANCE_DEG`` so the two can never drift.
_COUPLING_WINDOW_MM: float = 0.5
_COUPLING_PARALLEL_TOL_DEG: float = 15.0
# Largest perpendicular excursion a partner-parallel tail candidate may take
# from the head->goal corridor.  Matches the widest existing U-detour offset so
# the ``near_partner`` envelope filter stays a superset of every candidate.
_TAIL_PARALLEL_MAX_EXCURSION_MM: float = 3.2
# Extra centre-to-centre spacing rungs (beyond the tight intra-pair floor) tried
# when placing a partner-parallel tail run.  The tight rung couples hardest; the
# wider rungs are the escape hatch when the tight one is inside a pad halo.
#
# The tightest rung deliberately keeps ~0.05 mm of headroom over the floor
# rather than hugging it: the constructor's own screen runs BEFORE the 45-degree
# dogleg quantizer moves copper, and a run laid exactly at the floor emits
# ``diffpair_clearance_intra`` at the 0.1000-vs-0.1016 margin (measured: 5 extra
# USB2_D violations with a 0.001 mm rung).
_TAIL_PARALLEL_GAP_RUNGS_MM: tuple[float, ...] = (0.05, 0.12, 0.25, 0.40)
# Extra centre-to-centre clearance a FOLLOW run must keep over the intra-pair
# floor, for the same post-quantize-movement reason.
_TAIL_FOLLOW_CLEARANCE_MARGIN_MM: float = 0.03
# Cap on the number of partner-derived tail candidates, so a 900-segment guide
# cannot turn tail synthesis into the constructor's hot loop.
_TAIL_PARALLEL_MAX_CANDIDATES: int = 24
# A guide-following tail buys coupling by walking the partner's own landing
# path instead of cutting straight to the pad.  Bound how much longer than the
# direct hop that is allowed to be: beyond this it is a detour (which the pair
# would pay for in length skew), not a landing tail.
_TAIL_FOLLOW_LENGTH_FACTOR: float = 2.0
_TAIL_FOLLOW_LENGTH_SLACK_MM: float = 1.5
# Minimum coupled MILLIMETRES a guide-following tail must buy to displace
# whatever the existing chain would have produced.  Coupled length (not
# coupled fraction) is what the continuity rule integrates over the leg.
_TAIL_FOLLOW_MIN_COUPLED_MM: float = 0.5
# ...and, when there is no incumbent to compare against (the axis-aligned
# synthesizer declined outright), a minimum coupled FRACTION as well.  A long
# weakly-coupled follow displacing the crossing tail is a net loss: its extra
# uncoupled copper lowers the leg's own fraction (measured: USB2_D fell 0.626 ->
# 0.596 when a 5.05 mm / 27%-coupled follow displaced a 4.20 mm crossing tail).
_TAIL_FOLLOW_MIN_FRACTION: float = 0.30
# How much of the partner's landing run a following tail may give up (the last
# stretch is the part most likely to be walled in by neighbour-pad halos).
_TAIL_FOLLOW_KEEP_FRACTIONS: tuple[float, ...] = (1.0, 0.85, 0.7, 0.55, 0.4, 0.25)
# ...and how much of its HEAD end (issue #4577).  ``_TAIL_FOLLOW_KEEP_FRACTIONS``
# is applied by ``_truncate_spans``, a head-ANCHORED prefix, so ``run[0]`` is
# identical at every rung -- and ``run[0]`` is the coordinate the lead-in fails
# on in 22 of the 38 numeric board-06 declines.  These are that ladder's mirror
# image: walk the entry point forward along the run, giving up a prefix of the
# coupled copper to buy a bridge the axis-aligned synthesizer can actually draw.
# The first rung is 0.0, so the pre-#4577 attempt is always tried first and a
# tail that already bracketed is produced unchanged.
_TAIL_FOLLOW_ENTRY_FRACTIONS: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6)
# How far OUTSIDE the coupling window the fallback A*'s soft cost fence sits
# (issue #4577).  Big enough that the fence never penalises a cell the
# continuity rule would score as coupled, small enough that the channel it
# forms is still narrow enough to steer a shortest-path search.
_TAIL_CHANNEL_WALL_MARGIN_MM: float = 0.15
# Guardrails on the slice itself: too short to be worth following, or so many
# partner segments that offsetting them would dominate construction time.
_TAIL_FOLLOW_MIN_SLICE_MM: float = 0.3
_TAIL_FOLLOW_MAX_SPANS: int = 400
# How many of the legal offset runs found along one side of the slice are worth
# bracketing with a lead-in / landing (they are tried longest-first).
_TAIL_FOLLOW_MAX_RUNS_PER_SIDE: int = 2

# Issue #4570: via-count symmetry between the two legs of a constructed pair.
#
# ``diffpair_length_skew`` measures ELECTRICAL length -- segment copper PLUS
# every via's drilled length (``DiffPairLengthTracker._via_length``:
# ``board_thickness_mm * |delta stack index| / (num_copper_layers - 1)``, with a
# non-micro via promoted to the full thickness on a board without blind/buried
# drilling, #4007).  The constructor's length matcher
# (``_length_match_constructed_pair`` on ``_route_copper_length``) is PLANAR
# only, so a pair whose two legs carry different vias can be driven to a 0.012
# mm planar delta and still ship a 3.19 mm skew violation (measured on board-06
# shadow-ON seed 42: PCIE_RX and USB3_TX1, 0 vias on the guide leg and two full
# F.Cu->B.Cu through-vias on the shadow leg = 2 x 1.6 mm).
#
# The fix is symmetry, not compensation.  Compensating the z-length inside the
# meander was measured as a wash (3.2 mm of extra UNCOUPLED meander converts a
# skew error into a ``diffpair_routing_continuity`` error), and an unmatched via
# is a mode-conversion / impedance discontinuity in its own right -- not merely
# a length error.  So the constructor instead makes the two legs carry the same
# vias, which cancels the drilled term exactly.
#
# The gate is purely COMBINATORIAL and needs no board thickness: the per-leg
# z-length difference is ``thickness * (per-leg sum |delta stack index|
# difference) / (n - 1)``, which is zero exactly when the two legs'
# ``(via count, sum |delta stack index|)`` signatures match, for ANY thickness.
_SHADOW_VIA_SYMMETRY: bool = os.environ.get("KCT_SHADOW_VIA_SYMMETRY", "1") == "1"
# What to do with a pair for which NEITHER offset side is via-symmetric and no
# remediation is legal.
#
# ``STRICT`` drops it (the electrically pure answer: an unmatched via is a
# mode-conversion and impedance discontinuity, not just a length error).  The
# DEFAULT keeps the best legal candidate and says so loudly, because the strict
# policy was measured on board-06 seed 42 shadow-ON and costs reach: 33 -> 26
# total DRC errors but 19 -> 18 signal nets routed, and part of that "-7" is
# vacuity -- ``diffpair_length_skew`` only fires on an ENGAGED pair, so a pair
# that is dropped stops being measured (USB3_TX1's 3.412 mm skew error was
# replaced by two ``connectivity`` errors and two LVS opens).  An unrouted net
# is a worse ship than a skew error the checker names in full, so the default
# ships the pair and the DRC keeps flagging it.
#
# Why no remediation is legal on this board is worth recording: the landing
# tails must CROSS the guide (``_synthesize_crossing_tail``'s deliberate
# two-via crossover is the only legal tail -- all 12 layer-locked planar probes
# on the seed-42 run are rejected for breaking the intra-pair clearance against
# the partner), and mirroring the pair of vias onto the guide leg is blocked
# because the coupled gap (0.075-0.15 mm) is far below the
# via-barrel-to-partner-copper bound (~0.6 mm), so a centreline-preserving
# z-jog has nowhere legal to sit along the coupled body.  A LATERALLY OFFSET
# mirrored jog (the ``lateral_jog_polyline`` machinery the #4553 meander
# already uses, pushed away from the partner) is the remaining lever and is
# deliberately left as follow-up work.
_SHADOW_VIA_SYMMETRY_STRICT: bool = os.environ.get("KCT_SHADOW_VIA_SYMMETRY_STRICT", "0") == "1"
# Remediation B (``_mirror_z_jog``): give the leg that is SHORT of vias the same
# vias as its partner, as a centreline-preserving z-jog.
#
# Implemented and unit-tested, but OFF by default on the strength of its own
# board-06 seed-42 measurement: exactly one pair (USB2_D) had a legal mirror
# site, and taking it moved that pair onto geometry sitting exactly on the
# 0.100-vs-0.1016 mm intra-pair rung -- ``diffpair_clearance_intra`` 7 -> 19 and
# its own skew 5.430 -> 6.348 mm.  The mirror needs a site *selection* policy
# (prefer an uncoupled stretch, and re-check the pair's marginal rungs after
# splitting a segment) before it can be trusted by default; the gate and the
# tail-side remediation do not depend on it.
_SHADOW_VIA_MIRROR: bool = os.environ.get("KCT_SHADOW_VIA_MIRROR", "0") == "1"


# ---------------------------------------------------------------------------
# Issue #3023 Phase A: intra-pair clearance violation detection
# ---------------------------------------------------------------------------
#
# Phase A is observability-only.  After ``CoupledPathfinder`` produces a
# (p_route, n_route) for a diff pair, we re-check every same-layer
# segment-pair using ``segment_clearance`` against the per-pair
# ``NetClassRouting.effective_intra_pair_clearance()`` and emit a
# structured record (and a ``logger.info`` line) for any pair whose
# routed clearance is below the threshold.
#
# This is the SAME idiom ``match_pair_lengths`` already uses at
# diffpair_routing.py:1033-1053 to reject a serpentine bulge that would
# violate the partner; here we apply it to the post-coupling route as a
# diagnostic so Phase B (the fine-grid repair pass, separate PR) has a
# reproducible target list.
#
# Phase A explicitly does NOT modify any route.  All it does is:
#   1. compute per-segment-pair clearance,
#   2. report violations,
#   3. expose a public accessor for Phase B to consume.


# Issue #4459: diff-pair Phase 1 ground-truth taxonomy.  Every coupled pair
# that fails to couple falls into exactly one of these classes; Phases 2-5 of
# the #4409 epic each target a specific class, so this per-pair classification
# is the measurement harness that lets a later phase verify it fixed the class
# it claims to.  Diagnostic-only -- classifying a pair changes no geometry.
COUPLED_OUTCOME_GUIDE_MISSING = "guide-missing"
COUPLED_OUTCOME_SHADOW_OVERLAP = "shadow-declined-overlap"
COUPLED_OUTCOME_SHADOW_BLOCKAGE = "shadow-declined-blockage"
COUPLED_OUTCOME_JOINT_PLATEAU = "joint-A*-plateau"
COUPLED_OUTCOME_LANDING_STALL = "landing-stall"


def dominant_rejection(rejections: Mapping[str, int] | None) -> str | None:
    """Return the single most-frequent rejection reason, or ``None``.

    Issue #4459: the coupled search's per-reason rejection histogram
    (``CoupledPathfinder.last_rejections``) tells us WHICH guard pruned the
    frontier most often on a budget-exit.  Ties break alphabetically for a
    deterministic diagnostic.  An empty / ``None`` histogram returns ``None``
    (the search popped no neighbours -- e.g. the start state was the goal).
    """
    if not rejections:
        return None
    # max by (count, then reverse-alpha) so the highest count wins and ties
    # resolve to the alphabetically-first key deterministically.
    return max(sorted(rejections), key=lambda k: rejections[k])


@dataclass
class CoupledPairReport:
    """Structured per-pair ground-truth for one coupled diff-pair attempt.

    Issue #4459 (Phase 1 of #4409): emitted for every pair the coupled router
    attempts so Phases 2-5 have a per-pair failure classification to verify
    against.  Diagnostic-only -- constructing this record changes no routing
    behaviour or geometry.

    Attributes:
        pair_name: Base name of the diff pair (e.g. ``"MIPI_CLK"``).
        classification: One of the ``COUPLED_OUTCOME_*`` taxonomy strings, or
            ``"coupled-ok"`` when the pair actually coupled.
        coupled: Whether the pair coupled (a route was produced).
        backend: Which search served the attempt (``"cpp"`` / ``"python"``).
        coupled_phase: The phase label the attempt reached (``open`` /
            ``corridor`` / ``shadow`` / ``shadow-swapped`` / ...).
        guide_ok: Whether the single-ended guide probe produced segments.
        best_progress: Smallest joint remaining Manhattan distance any popped
            joint state reached (``inf`` when nothing popped / not applicable).
        dominant_rejection: The most-frequent frontier-pruning reason, or
            ``None``.
        start_pitch_cells: P/N start-pad pitch in grid cells.
        end_pitch_cells: P/N end-pad pitch in grid cells.
        target_spacing_cells: The coupled search's target center spacing.
        off_angle_segments: Count of guide segments that are neither
            axis-aligned nor exactly 45 degrees (an off-angle proxy Phase 4
            targets).
        shadow_enabled: Whether shadow construction was active for this pair.
    """

    pair_name: str
    classification: str
    coupled: bool
    backend: str
    coupled_phase: str
    guide_ok: bool
    best_progress: float
    dominant_rejection: str | None
    start_pitch_cells: float
    end_pitch_cells: float
    target_spacing_cells: int
    off_angle_segments: int
    shadow_enabled: bool

    def format_line(self) -> str:
        """One-line ``[coupled-pair-report]`` rendering for stdout logs."""
        bp = "inf" if self.best_progress == float("inf") else f"{self.best_progress:.0f}"
        return (
            f"    [coupled-pair-report] pair={self.pair_name} "
            f"class={self.classification} coupled={self.coupled} "
            f"backend={self.backend} phase={self.coupled_phase} "
            f"guide_ok={self.guide_ok} best_progress={bp} "
            f"dominant_rejection={self.dominant_rejection} "
            f"start_pitch={self.start_pitch_cells:.1f} "
            f"end_pitch={self.end_pitch_cells:.1f} "
            f"target_spacing={self.target_spacing_cells} "
            f"off_angle_segs={self.off_angle_segments} "
            f"shadow={self.shadow_enabled}"
        )


def _route_copper_length(route: Route | None) -> float:
    """Total centreline copper length (mm) of a route's segments.

    Issue #4553: the shadow constructor's length-symmetry work needs a single
    definition of "how long is this leg" shared by the constructor, its
    diagnostics and the unit tests.  Vias contribute no planar length.

    Issue #4570: this stays deliberately PLANAR.  The z-length a via adds is
    real (``diffpair_length_skew`` measures it), but compensating for it here
    -- by meandering the shorter leg by the drilled difference -- was measured
    as a wash: the compensation is uncoupled copper, so it merely converts a
    ``diffpair_length_skew`` error into a ``diffpair_routing_continuity`` one.
    The drilled term is cancelled instead, by
    :func:`_route_via_signature` / the constructor's via-symmetry gate, which
    makes the two legs carry the same vias in the first place.
    """
    if route is None:
        return 0.0
    return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)


def _via_stack_span(via: Via, num_copper_layers: int) -> int:
    """``|delta stack index|`` spanned by ``via`` in an N-layer stack (issue #4570).

    Delegates the layer -> stack-position mapping to
    :meth:`DiffPairLengthTracker._stack_position`, the audited model the
    ``diffpair_length_skew`` checker itself uses, so the constructor and the
    checker can never drift apart on what a via's drilled span is.
    """
    layer_start, layer_end = via.layers
    return abs(
        DiffPairLengthTracker._stack_position(layer_start, num_copper_layers)
        - DiffPairLengthTracker._stack_position(layer_end, num_copper_layers)
    )


def _route_via_signature(route: Route | None, num_copper_layers: int) -> tuple[int, int]:
    """``(via count, sum |delta stack index|)`` for one leg of a pair (issue #4570).

    The thickness-free proxy for the via drilled length
    ``diffpair_length_skew`` adds to each leg.  ``_via_length`` is
    ``board_thickness_mm * |delta stack index| / (num_copper_layers - 1)``, and
    a non-micro via is promoted to the full ``board_thickness_mm`` when the
    board does not support blind/buried drilling (#4007).  Two legs with EQUAL
    signatures therefore contribute equal via length under BOTH models, for any
    ``board_thickness_mm`` -- so the constructor can enforce z-length symmetry
    without a thickness source (the router's ``DesignRules`` carries none).

    The count is kept alongside the span sum because the promoted model depends
    only on the count while the un-promoted one depends only on the span sum;
    requiring both to match makes the signature sufficient for either.
    """
    if route is None:
        return (0, 0)
    return (
        len(route.vias),
        sum(_via_stack_span(v, num_copper_layers) for v in route.vias),
    )


Point = tuple[float, float]

# Issue #4553: the eight grid directions a 45-legal displacement may take.
# Axis moves keep one coordinate; diagonal moves move both by the SAME
# magnitude, so a translation along any of these is 45-legal by construction.
_GRID_DIRS: tuple[Point, ...] = (
    (1.0, 0.0),
    (0.0, 1.0),
    (-1.0, 0.0),
    (0.0, -1.0),
    (1.0, 1.0),
    (1.0, -1.0),
    (-1.0, 1.0),
    (-1.0, -1.0),
)


def _point_seg_distance(px: float, py: float, a: Point, b: Point) -> float:
    """Distance from ``(px, py)`` to the finite segment ``a -> b``."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    if den < 1e-18:
        return math.hypot(px - a[0], py - a[1])
    t = max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / den))
    return math.hypot(px - (a[0] + dx * t), py - (a[1] + dy * t))


def _polyline_points(segments: list[Segment]) -> list[Point] | None:
    """Ordered vertex list of a head-to-tail segment chain, or ``None``.

    Issue #4553.  The shadow constructor's geometry passes operate on a
    POLYLINE (a vertex list), not on a segment list: both the 45-legal
    compression and the length-matching lateral jog are defined by moving
    vertices.  Returns ``None`` when the input is not a single contiguous
    chain (a branch or a break), so callers degrade to leaving it alone.
    """
    if not segments:
        return None
    pts: list[Point] = [(segments[0].x1, segments[0].y1)]
    for seg in segments:
        if math.hypot(seg.x1 - pts[-1][0], seg.y1 - pts[-1][1]) > 1e-6:
            return None
        pts.append((seg.x2, seg.y2))
    return pts


def _route_span(segments: list[Segment]) -> float:
    """Total centreline length of a segment list (mm)."""
    return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segments)


def _polyline_length(points: list[Point]) -> float:
    """Total length of a polyline given as an ordered vertex list."""
    return sum(
        math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
        for i in range(len(points) - 1)
    )


def _max_deviation(points: list[Point], polyline: list[Point]) -> float:
    """Largest distance from any of ``points`` to ``polyline``."""
    worst = 0.0
    for px, py in points:
        best = float("inf")
        for k in range(len(polyline) - 1):
            best = min(best, _point_seg_distance(px, py, polyline[k], polyline[k + 1]))
        worst = max(worst, best)
    return worst


def simplify_45_polyline(
    points: list[Point],
    max_deviation: float,
    is_clear: Callable[[Point, Point], bool] | None = None,
    max_window: int = 80,
) -> list[Point]:
    """Compress a fine 45-legal staircase into long straight runs.

    Issue #4553.  The C++ per-net A* emits one segment per grid cell, so a
    guide route is a 400-segment staircase whose longest CONTIGUOUS collinear
    run is a fraction of a millimetre.  Every one of those micro-corners costs
    the parallel-offset shadow a miter join (``_offset_corner_join``), and the
    fragmentation starves every downstream length tuner, which needs a
    straight run of ``SerpentineConfig.min_segment_length`` to act.

    This walks the polyline greedily and replaces the longest run
    ``points[i..j]`` it can with the SHORTEST 45-legal path between the same
    two vertices (:func:`quantize.dogleg_points` -- one exact diagonal leg plus
    one exact axis leg, so the result is census-clean by construction), subject
    to two bounds:

    * every replaced interior vertex stays within ``max_deviation`` of the
      replacement, so the compressed path never leaves the corridor the router
      chose (a global shortcut could cut a corner the offset needs), and
    * ``is_clear(a, b)`` accepts both replacement legs (obstacle validation;
      when ``None`` no obstacle check is performed -- the pure-geometry mode
      the unit tests use).

    The shortest 45-legal path is never longer than the run it replaces, so
    this pass can only SHORTEN a route; endpoints are always preserved.
    """
    if len(points) < 3:
        return list(points)

    out: list[Point] = [points[0]]
    n = len(points)
    i = 0
    while i < n - 1:
        best_j = -1
        best_repl: list[Point] | None = None
        j = i + 2
        while j < n and (j - i) <= max_window:
            repl: list[Point] | None = None
            for axis_first in (False, True):
                cand = dogleg_points(
                    points[i][0], points[i][1], points[j][0], points[j][1], axis_first=axis_first
                )
                if _max_deviation(points[i + 1 : j], cand) > max_deviation:
                    continue
                if is_clear is not None and not all(
                    is_clear(cand[k], cand[k + 1]) for k in range(len(cand) - 1)
                ):
                    continue
                repl = cand
                break
            if repl is None:
                break
            best_j, best_repl = j, repl
            j += 1
        if best_repl is None:
            out.append(points[i + 1])
            i += 1
        else:
            out.extend(best_repl[1:])
            i = best_j
    return out


def densify_polyline(points: list[Point], step: float) -> list[Point]:
    """Insert collinear vertices so no edge is longer than ``step``.

    Issue #4553.  The lateral-jog meander picks its window from the VERTEX
    list, so a compressed polyline made of a few multi-millimetre edges would
    offer nowhere to put a tooth.  Inserted vertices lie exactly on the edge
    they subdivide, so the geometry -- and its 45-legality -- is unchanged.
    """
    if step <= 0.0 or len(points) < 2:
        return list(points)
    out: list[Point] = [points[0]]
    for a, b in zip(points, points[1:], strict=False):
        seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
        n = int(seg_len // step)
        for k in range(1, n + 1):
            t = (k * step) / seg_len
            if t >= 1.0 - 1e-9:
                break
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        out.append(b)
    return out


def merge_collinear_points(points: list[Point]) -> list[Point]:
    """Drop vertices that do not change the polyline's direction."""
    if len(points) < 3:
        return list(points)
    out: list[Point] = [points[0]]
    for k in range(1, len(points) - 1):
        ax, ay = out[-1]
        bx, by = points[k]
        cx, cy = points[k + 1]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        dot = (bx - ax) * (cx - bx) + (by - ay) * (cy - by)
        if abs(cross) > 1e-9 or dot < 0.0:
            out.append(points[k])
    out.append(points[-1])
    return out


def lateral_jog_polyline(points: list[Point], i: int, j: int, dx: float, dy: float) -> list[Point]:
    """Translate the window ``points[i..j]`` by ``(dx, dy)``, adding length.

    Issue #4553.  This is the construction-time length-matching primitive: the
    sub-polyline between vertices ``i`` and ``j`` is displaced sideways and
    rejoined to the untouched spine by two connector legs, so the result adds
    EXACTLY ``2 * hypot(dx, dy)`` of copper.  Every displaced segment keeps its
    original displacement vector (translation is direction-preserving), so a
    45-legal window stays 45-legal; the two connector legs are 45-legal exactly
    when ``(dx, dy)`` is itself axis-aligned or a true diagonal -- which is why
    callers pick the displacement from :data:`_GRID_DIRS`.

    Unlike a serpentine, this needs no straight SEGMENT -- only a window whose
    endpoints are far enough apart for the two connector legs not to touch --
    so it works on the fine staircases the router actually emits.
    """
    if not (0 <= i < j < len(points)):
        raise ValueError(f"invalid jog window [{i}, {j}] for {len(points)} points")
    moved = [(p[0] + dx, p[1] + dy) for p in points[i : j + 1]]
    return points[: i + 1] + moved + points[j:]


def _count_off_angle_segments(route: Route | None) -> int:
    """Count guide segments that are neither axis-aligned nor exactly 45 deg.

    Issue #4459: an off-angle proxy for the Phase-4 failure class.  A segment
    is on-angle when it is horizontal (``dy == 0``), vertical (``dx == 0``) or
    a true 45 (``|dx| == |dy|``); anything else is off-angle.  ``None`` / empty
    routes contribute zero.
    """
    if route is None:
        return 0
    off = 0
    for seg in route.segments:
        dx = abs(seg.x2 - seg.x1)
        dy = abs(seg.y2 - seg.y1)
        if dx < 1e-9 or dy < 1e-9:
            continue  # axis-aligned
        if abs(dx - dy) < 1e-6:
            continue  # exact 45
        off += 1
    return off


def _segments_within(a: Segment, b: Segment, margin: float) -> bool:
    """Cheap axis-aligned-bounding-box overlap test with ``margin`` (issue #4460).

    A conservative pre-filter for the sampled centreline-distance checks: when
    the two segments' AABBs do not come within ``margin``, no point of ``a`` can
    be within ``margin`` of ``b``, so the expensive sampling is skipped.  Never
    rejects a genuinely-close pair (the AABB is a superset of the segment).
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


def _span_angle_deg(x1: float, y1: float, x2: float, y2: float) -> float:
    """Orientation of a span in ``[0, 180)`` degrees (issue #4572).

    Mirrors ``diffpair_routing_continuity._segment_angle_deg``: coupling is
    direction-agnostic, so anti-parallel counts as parallel.
    """
    raw = math.degrees(math.atan2(y2 - y1, x2 - x1))
    if raw < 0.0:
        raw += 180.0
    if raw >= 180.0:
        raw -= 180.0
    return raw


def _angle_delta_deg(a: float, b: float) -> float:
    """Smaller of the two angular differences, in ``[0, 90]`` (issue #4572).

    Mirrors ``diffpair_routing_continuity._angle_difference_deg``.
    """
    raw = abs(a - b)
    if raw > 90.0:
        raw = 180.0 - raw
    return raw


def _span_is_coupled_to(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float,
    prepared_partner: list[tuple[Segment, float]],
) -> bool:
    """Would ``diffpair_routing_continuity`` score this span as coupled?

    Issue #4572.  Exact mirror of that rule's per-segment predicate
    (``_segment_coupled_overlap``): same layer (the caller pre-filters),
    parallel within :data:`_COUPLING_PARALLEL_TOL_DEG`, and edge-to-edge
    clearance at or below :data:`_COUPLING_WINDOW_MM`.  Uses the same exact
    ``segment_to_segment_distance`` primitive the rule uses -- not the sampled
    ``_min_distance_to_partner`` -- so the constructor's preference and the DRC
    measurement agree segment for segment.

    ``prepared_partner`` is a list of ``(segment, precomputed_angle)`` already
    filtered to the candidate's layer.
    """
    if math.hypot(x2 - x1, y2 - y1) < 1e-9 or not prepared_partner:
        return False
    ang = _span_angle_deg(x1, y1, x2, y2)
    lo_x, hi_x = (x1, x2) if x1 <= x2 else (x2, x1)
    lo_y, hi_y = (y1, y2) if y1 <= y2 else (y2, y1)
    for ps, ps_ang in prepared_partner:
        if _angle_delta_deg(ang, ps_ang) > _COUPLING_PARALLEL_TOL_DEG:
            continue
        half = (width + ps.width) / 2.0
        reach = _COUPLING_WINDOW_MM + half
        # Cheap AABB reject before the exact distance (guides reach ~900
        # segments and this runs once per candidate span).
        if lo_x - reach > max(ps.x1, ps.x2) or hi_x + reach < min(ps.x1, ps.x2):
            continue
        if lo_y - reach > max(ps.y1, ps.y2) or hi_y + reach < min(ps.y1, ps.y2):
            continue
        centre = _segment_to_segment_distance(x1, y1, x2, y2, ps.x1, ps.y1, ps.x2, ps.y2)
        if centre - half <= _COUPLING_WINDOW_MM + 1e-6:
            return True
    return False


def _spans_coupled_fraction(
    spans: list[tuple[float, float, float, float]],
    width: float,
    layer: Layer,
    partner_segments: list[Segment],
) -> float:
    """Fraction of ``spans``' length the continuity rule would score coupled.

    Issue #4572.  The rule's ``_coupled_length`` credits a segment's FULL
    length once it finds any parallel partner inside the coupling window, so
    this aggregates the same all-or-nothing per-span verdict weighted by
    length.  Returns ``0.0`` for a degenerate (zero-length) candidate.
    """
    prepared = [
        (ps, _span_angle_deg(ps.x1, ps.y1, ps.x2, ps.y2))
        for ps in partner_segments
        if ps.layer == layer
    ]
    total = 0.0
    coupled = 0.0
    for x1, y1, x2, y2 in spans:
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 1e-9:
            continue
        total += length
        if _span_is_coupled_to(x1, y1, x2, y2, width, prepared):
            coupled += length
    if total <= 0.0:
        return 0.0
    return coupled / total


def classify_coupled_pair_outcome(
    *,
    coupled: bool,
    coupled_phase: str,
    guide_ok: bool,
    best_progress: float,
    shadow_enabled: bool,
    shadow_decline_reason: str | None,
    near_miss_cells: int = NEAR_MISS_RESCUE_CELLS,
) -> str:
    """Classify a coupled-pair attempt into the #4459 failure taxonomy.

    Diagnostic-only.  Returns ``"coupled-ok"`` when the pair coupled, else one
    of the ``COUPLED_OUTCOME_*`` classes:

    * ``guide-missing`` -- the single-ended guide probe produced no path, so
      neither the shadow constructor nor the corridor search had a seed.
    * ``shadow-declined-overlap`` / ``shadow-declined-blockage`` -- shadow
      construction was active and declined (only reachable with shadow ON);
      the sub-reason is the shadow's last decline cause.
    * ``landing-stall`` -- the joint A* got within ``near_miss_cells`` of the
      goal (the pad-landing needle-eye stall, Phase 5's class).
    * ``joint-A*-plateau`` -- the joint A* stalled far from the goal (overlap /
      blockage / off-angle body failures, Phases 2-4's classes).
    """
    if coupled:
        return "coupled-ok"
    if not guide_ok:
        return COUPLED_OUTCOME_GUIDE_MISSING
    if shadow_enabled and shadow_decline_reason is not None:
        if shadow_decline_reason == "overlap":
            return COUPLED_OUTCOME_SHADOW_OVERLAP
        return COUPLED_OUTCOME_SHADOW_BLOCKAGE
    if best_progress != float("inf") and best_progress <= near_miss_cells:
        return COUPLED_OUTCOME_LANDING_STALL
    return COUPLED_OUTCOME_JOINT_PLATEAU


@dataclass
class IntraPairClearanceViolation:
    """A routed intra-pair clearance violation on a differential pair.

    Phase A diagnostic record (Issue #3023): emitted when the routed
    edge-to-edge clearance between a P-segment and an N-segment on the
    same layer falls below the per-pair
    :meth:`NetClassRouting.effective_intra_pair_clearance`.

    Attributes:
        pair_name: Base name of the violating diff pair (e.g. ``"DQS0"``).
        positive_net_name: Net name of the P trace (for log grep-ability).
        negative_net_name: Net name of the N trace.
        expected_clearance_mm: The per-pair threshold (from
            ``NetClassRouting.effective_intra_pair_clearance()``).
        actual_clearance_mm: The minimum edge-to-edge clearance found
            across all same-layer segment pairs.  ``< expected_clearance_mm``.
        violation_magnitude_mm: ``expected_clearance_mm - actual_clearance_mm``
            (always positive when a violation is recorded).
        layer: KiCad layer name where the worst violation occurred.
        p_segment: The P-side segment involved in the worst violation.
        n_segment: The N-side segment involved in the worst violation.
        segment_violations: All same-layer (p_seg, n_seg, clearance) triples
            that fell below ``expected_clearance_mm``.  Phase B (repair
            pass) consumes this list to scope the corridor for the
            fine-grid sub-search.
    """

    pair_name: str
    positive_net_name: str
    negative_net_name: str
    expected_clearance_mm: float
    actual_clearance_mm: float
    violation_magnitude_mm: float
    layer: str
    p_segment: Segment
    n_segment: Segment
    segment_violations: list[tuple[Segment, Segment, float]] = field(default_factory=list)


def find_intra_pair_clearance_violations(
    p_route: Route,
    n_route: Route,
    threshold_mm: float,
    pair_name: str = "",
) -> IntraPairClearanceViolation | None:
    """Detect intra-pair clearance violations on a routed differential pair.

    Walks every same-layer (p-segment, n-segment) pair and computes the
    edge-to-edge clearance via :func:`core.geometry.segment_clearance`.
    Returns ``None`` when no violation is found, otherwise a single
    :class:`IntraPairClearanceViolation` summarising the worst case and
    listing every offending segment pair for downstream consumption.

    This is the SAME segment-clearance idiom ``match_pair_lengths`` uses
    at ``diffpair_routing.py:1033-1053`` to reject would-be serpentine
    bulges, lifted into a reusable detector so the route-time check in
    ``route_differential_pair_coupled`` and the post-route audit in
    :meth:`DiffPairRouter.intra_clearance_violations` share one
    implementation.

    Args:
        p_route: The positive trace route.
        n_route: The negative trace route.
        threshold_mm: The per-pair clearance floor.  Pass
            ``NetClassRouting.effective_intra_pair_clearance()`` from
            the route's net class -- NOT the global
            ``DifferentialPairRules.spacing``, which is a heuristic
            default that does not reflect the per-pair override.
        pair_name: Base pair name for the returned record (e.g.
            ``"DQS0"``).  Defaults to the empty string when the caller
            doesn't have a structured pair handy.

    Returns:
        ``None`` when every same-layer segment-pair meets the threshold;
        otherwise an :class:`IntraPairClearanceViolation` whose
        ``segment_violations`` list contains every offending pair and
        whose top-level fields summarise the worst case.
    """
    from kicad_tools.core.geometry import segment_clearance

    if p_route is None or n_route is None:
        return None
    if not p_route.segments or not n_route.segments:
        return None

    offenders: list[tuple[Segment, Segment, float]] = []
    worst_clearance = float("inf")
    worst_pair: tuple[Segment, Segment] | None = None

    for pseg in p_route.segments:
        for nseg in n_route.segments:
            if pseg.layer != nseg.layer:
                continue
            clearance = segment_clearance(
                pseg.x1,
                pseg.y1,
                pseg.x2,
                pseg.y2,
                pseg.width,
                nseg.x1,
                nseg.y1,
                nseg.x2,
                nseg.y2,
                nseg.width,
            )
            # 1e-9 tolerance matches the serpentine self-check at
            # diffpair_routing.py:1052 -- floating-point equality at the
            # threshold counts as compliant.
            if clearance + 1e-9 < threshold_mm:
                offenders.append((pseg, nseg, clearance))
                if clearance < worst_clearance:
                    worst_clearance = clearance
                    worst_pair = (pseg, nseg)

    if not offenders or worst_pair is None:
        return None

    worst_p, worst_n = worst_pair
    return IntraPairClearanceViolation(
        pair_name=pair_name,
        positive_net_name=p_route.net_name,
        negative_net_name=n_route.net_name,
        expected_clearance_mm=threshold_mm,
        actual_clearance_mm=worst_clearance,
        violation_magnitude_mm=threshold_mm - worst_clearance,
        layer=worst_p.layer.kicad_name
        if hasattr(worst_p.layer, "kicad_name")
        else str(worst_p.layer),
        p_segment=worst_p,
        n_segment=worst_n,
        segment_violations=offenders,
    )


class PairOrientation(Enum):
    """Orientation of the differential pair traces."""

    HORIZONTAL = "horizontal"  # P above N (or vice versa), traces run horizontally
    VERTICAL = "vertical"  # P left of N (or vice versa), traces run vertically


@dataclass
class GridPos:
    """Grid position for coupled routing."""

    x: int
    y: int
    layer: int

    def __hash__(self) -> int:
        return hash((self.x, self.y, self.layer))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GridPos):
            return NotImplemented
        return self.x == other.x and self.y == other.y and self.layer == other.layer

    def __add__(self, other: tuple[int, int, int]) -> GridPos:
        return GridPos(self.x + other[0], self.y + other[1], self.layer + other[2])


@dataclass
class CoupledState:
    """State for coupled differential pair A* search.

    Represents the position of both P and N traces simultaneously.
    Both traces must move together to maintain constant spacing.
    """

    p_pos: GridPos  # Positive trace position
    n_pos: GridPos  # Negative trace position
    direction: tuple[int, int]  # Current routing direction (dx, dy)

    def __hash__(self) -> int:
        return hash((self.p_pos, self.n_pos, self.direction))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CoupledState):
            return NotImplemented
        return (
            self.p_pos == other.p_pos
            and self.n_pos == other.n_pos
            and self.direction == other.direction
        )

    @property
    def spacing(self) -> float:
        """Calculate current spacing between P and N traces."""
        dx = self.p_pos.x - self.n_pos.x
        dy = self.p_pos.y - self.n_pos.y
        return math.sqrt(dx * dx + dy * dy)


@dataclass
class CoupledSegmentSpec:
    """Specification for a single coupled-routing segment.

    Issue #2473: For N-pad differential pairs (e.g., USB-C connectors),
    the pair-up step now produces a list of these specs rather than a
    flat 4-tuple, so the driver can run the coupled pathfinder for each
    spec and feed the leftover stub edges to the independent router.
    """

    p_start: Pad
    p_end: Pad
    n_start: Pad
    n_end: Pad
    # ``True`` when the orientation of (p_start, n_start) is the
    # mirror of (p_end, n_end) — i.e., the pair must perform a
    # coordinated layer-swap crossover during routing.  The
    # CoupledPathfinder consumes this hint to enable swap-via moves.
    polarity_swap: bool = False


@dataclass
class StubEdgeSpec:
    """Specification for a single-net stub edge.

    Issue #2473: When a differential pair has more than 2 pads on
    a side (e.g., USB-C A6 + B6 paralleled into the same net),
    the extra pads connect to the main coupled run via short
    independent stubs.  These are routed via the standard
    autorouter rather than the CoupledPathfinder.
    """

    start: Pad
    end: Pad


@dataclass(order=True)
class CoupledNode:
    """Node for coupled A* priority queue.

    Issue #3144: ``seq`` is a monotonic insertion counter assigned at
    push-time from a search-local counter.  It serves as a secondary
    tie-break key after ``f_score`` so that nodes with identical f-scores
    pop in a deterministic, insertion-order-preserving order.  Without
    this, ``heapq`` falls through to structural comparison on the next
    compared field, and a ``dataclass(order=True)`` containing
    ``CoupledState`` values with no explicit ``__lt__`` would raise
    ``TypeError``.  Previously all fields after ``f_score`` were
    ``compare=False``, which left the heap-ordering invariant entirely
    dependent on push order -- a non-deterministic property under CI
    load (the Python interpreter's heap reshuffle visits sibling
    f_score-equal nodes in an order that can vary with allocator state).

    A monotonic counter is cheap (one ``int`` compare per heap op) and
    eliminates this entire class of non-determinism without changing
    A*'s optimality guarantees.  The convention "lower seq wins on
    f_score tie" mirrors the C++ ``AStarNode::operator>`` invariant
    introduced in the same fix.

    Callers MUST pass ``seq`` explicitly at construction time
    (typically ``seq=next(seq_iter)`` where ``seq_iter = itertools.count()``
    is search-local).  A default of ``0`` is provided only to keep the
    dataclass declaration well-formed; constructing two nodes with the
    same default ``seq`` value would re-introduce the ordering ambiguity
    this field exists to eliminate.
    """

    f_score: float
    g_score: float = field(compare=False)
    state: CoupledState = field(compare=False)
    parent: CoupledNode | None = field(compare=False, default=None)
    via_from_parent: bool = field(compare=False, default=False)
    seq: int = field(compare=True, default=0)


def build_corridor_mask(
    grid: RoutingGrid,
    guide_route: Route,
    radius_cells: int,
    extra_cells: tuple[tuple[int, int], ...] = (),
) -> frozenset[tuple[int, int]]:
    """Build a layer-agnostic corridor mask from a single-ended guide route.

    Issue #3439: the coupled diff-pair A* searches the joint
    ``(p_pos, n_pos)`` product state space, which is roughly quadratic
    in the open-grid single-net search and intractable in pure Python
    on a 4-layer 110x95 mm grid (~14k iterations/min vs the millions
    needed).  Restricting both traces to a corridor dilated around a
    known-routable single-ended path (found by the C++-accelerated
    per-net router) converts the open 2D search into a near-1D one,
    which the pure-Python coupled search completes in seconds.

    The mask is intentionally layer-agnostic: the coupled search keeps
    full freedom to choose layers (paired vias) within the spatial
    tube.  This avoids over-constraining the pair to the guide path's
    exact layer sequence, which was computed for a single trace and may
    not have room for two.

    Args:
        grid: The routing grid (used for world->grid conversion and
            bounds clamping).
        guide_route: A single-ended :class:`Route` whose segments trace
            the spatial path to dilate (typically the P-side of the
            pair, routed by the standard per-net pathfinder WITHOUT
            being committed to the grid).
        radius_cells: Chebyshev dilation radius in grid cells.  Must be
            at least the pair's center-to-center spacing plus a few
            cells of maneuvering slack so the N trace fits alongside
            the guide path and the search can detour around local
            obstacles.
        extra_cells: Additional ``(x, y)`` grid cells to include (with
            the same dilation), e.g. the four pad endpoint cells so the
            N-side endpoints are always inside the corridor even when
            the start/end pad pitch exceeds ``radius_cells``.

    Returns:
        Frozenset of in-bounds ``(x, y)`` grid cells forming the
        corridor.
    """
    base_cells: set[tuple[int, int]] = set(extra_cells)

    for seg in guide_route.segments:
        gx1, gy1 = grid.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = grid.world_to_grid(seg.x2, seg.y2)
        steps = max(abs(gx2 - gx1), abs(gy2 - gy1))
        if steps == 0:
            base_cells.add((gx1, gy1))
            continue
        for i in range(steps + 1):
            t = i / steps
            base_cells.add(
                (
                    int(round(gx1 + (gx2 - gx1) * t)),
                    int(round(gy1 + (gy2 - gy1) * t)),
                )
            )

    for via in guide_route.vias:
        base_cells.add(grid.world_to_grid(via.x, via.y))

    radius = max(0, int(radius_cells))
    corridor: set[tuple[int, int]] = set()
    cols, rows = grid.cols, grid.rows
    for cx, cy in base_cells:
        for dx in range(-radius, radius + 1):
            x = cx + dx
            if x < 0 or x >= cols:
                continue
            for dy in range(-radius, radius + 1):
                y = cy + dy
                if 0 <= y < rows:
                    corridor.add((x, y))

    return frozenset(corridor)


class CoupledPathfinder:
    """A* pathfinder for coupled differential pair routing.

    Routes both P and N traces simultaneously, maintaining constant
    spacing between them throughout the path.
    """

    # Issue #3089: how often (in A* iterations) the wall-clock budget is
    # consulted inside ``route_coupled``.  ``time.monotonic()`` is fast
    # (~100 ns) but the coupled A* iteration body is heavy (path-history
    # walk + neighbour generation + heap push), so the per-iter cost is
    # 1-10 ms on board 06's BGA-49 escape.  Checking every 64 iterations
    # keeps the overhead < 0.01 % while bounding the late-exit lateness
    # to a few hundred milliseconds on any reasonable workload.  Must be
    # a power of two (the check uses a bitmask).
    _TIMEOUT_CHECK_INTERVAL = 64

    def __init__(
        self,
        grid: RoutingGrid,
        rules: DesignRules,
        target_spacing_cells: int,
        net_class_map: dict[str, NetClassRouting] | None = None,
        allow_swap_via: bool = False,
        min_spacing_cells: int = 0,
        heuristic_mode: Literal["manhattan_sum", "partner_aware"] = "partner_aware",
        spacing_penalty_factor: float = 0.25,
        heuristic_weight: float = 1.0,
    ):
        """Initialize coupled pathfinder.

        Args:
            grid: The routing grid
            rules: Design rules for routing
            target_spacing_cells: Target spacing between P/N in grid cells
            net_class_map: Optional net class map for per-net trace widths
            allow_swap_via: Issue #2473: When True, the pathfinder may
                place a paired layer-swap that exchanges the P/N grid
                positions across an inner layer.  Used when source and
                sink polarity orientations are mirrored (USB-C-shaped
                pads).
            min_spacing_cells: Issue #3012: Hard floor on the center-to-
                center spacing (in grid cells) the search will tolerate
                between P and N positions.  Derived in
                ``route_differential_pair_coupled`` from
                ``(trace_width + intra_pair_clearance) / grid.resolution``
                so that within-pair edge-to-edge clearance is preserved
                even when the approach-phase tolerance widens or the
                asymmetric "converge" moves fire.  Defaults to ``0``
                (legacy permissive behaviour) so callers that don't
                supply per-pair NetClassRouting are unaffected.
                Endpoint cells (start and goal pad positions) are
                exempt from the floor -- those are the cells the pads
                themselves occupy and the floor would otherwise
                disqualify the search's only chance to land.
            heuristic_mode: Issue #3115 (angle #5): Selects the A* heuristic
                used by :meth:`_heuristic`.

                * ``"manhattan_sum"`` (legacy):
                  ``(p_dist + n_dist) * cost_straight + layer_cost``.
                  Sums the Manhattan distance from each trace to its
                  goal -- biases the search away from partner-
                  synchronised moves because every symmetric step
                  reduces the sum by 2 while every asymmetric (P-only
                  or N-only) step reduces it by 1, even though both
                  cost the same on a coupled run.
                * ``"partner_aware"`` (default, Issue #3115): uses
                  ``max(p_dist, n_dist) * cost_straight + spacing_penalty
                  + layer_cost``.  Still admissible (every real path
                  must advance the slower trace at least ``max(p_dist,
                  n_dist)`` cells), but ranks partner-synchronised
                  moves higher in the priority queue.  Reduces the
                  asymmetric-escape pathology that produces the
                  ``diffpair_clearance_intra`` cluster on board 06.
                  ``spacing_penalty`` is a sub-cost-per-step term that
                  penalises states whose current center-to-center
                  spacing diverges from ``target_spacing_cells`` (see
                  ``spacing_penalty_factor``).
            spacing_penalty_factor: Issue #3115: Multiplier applied to
                the ``abs(current_spacing - target_spacing_cells) *
                cost_straight`` spacing penalty term in the
                ``"partner_aware"`` heuristic.  Bounded above by ``1.0``
                analytically (each cell-of-spacing-divergence costs at
                most one ``cost_straight`` of true path cost to correct
                via the asymmetric converge move), so any factor ``<=
                1.0`` keeps the heuristic admissible.  Default ``0.25``
                is the smallest value the synthetic-pair regression
                test under :mod:`tests.test_diffpair_phase_b` empirically
                showed lifts the asymmetric-escape case.  Ignored when
                ``heuristic_mode == "manhattan_sum"``.
        """
        self.grid = grid
        self.rules = rules
        self.target_spacing_cells = target_spacing_cells
        self.net_class_map = net_class_map or {}
        self.allow_swap_via = allow_swap_via
        # Issue #3012: store the within-pair spacing floor.  ``0`` means
        # no floor (legacy behaviour).
        self.min_spacing_cells = max(0, int(min_spacing_cells))
        # Issue #3115: heuristic mode + spacing-penalty factor.  Clamp
        # the factor to [0, 1] to preserve admissibility -- any value
        # > 1 risks ranking states whose required correction cost
        # exceeds the true remaining-path cost, which would let A*
        # return a sub-optimal route.
        if heuristic_mode not in ("manhattan_sum", "partner_aware"):
            raise ValueError(
                f"heuristic_mode must be 'manhattan_sum' or 'partner_aware'; got {heuristic_mode!r}"
            )
        self.heuristic_mode: Literal["manhattan_sum", "partner_aware"] = heuristic_mode
        self.spacing_penalty_factor = max(0.0, min(1.0, float(spacing_penalty_factor)))

        # Issue #3508: weighted-A* factor applied to the heuristic when
        # computing ``f = g + weight * h``.  ``1.0`` is classic optimal
        # A*.  Values > 1 trade path-cost optimality for search effort:
        # the coupled joint-state space has DEEP f-plateaus (a single
        # ``cost_turn`` = 5 shell can hold ~90k states on board 06's
        # 0.05 mm grid -- measured: MIPI_CLK needed ~95k iterations to
        # flood ONE such basin before escaping, then cruised 350 cells
        # in <17k).  Weighting the heuristic makes goal-ward gradient
        # dominate shell-flooding so converging pairs land within a
        # CI-affordable iteration budget.  Slightly longer paths are an
        # acceptable trade: pair length-matching is enforced post-route
        # (serpentine / Phase 3I tuner), and the corridor mask already
        # bounds how far from the guide path the route can wander.
        self.heuristic_weight = max(1.0, float(heuristic_weight))
        # Issue #3089: set when the most-recent ``route_coupled`` call
        # exited early due to ``timeout_seconds`` being exceeded.
        # Callers (``route_differential_pair_coupled``) read this to
        # distinguish a budget-exit (where the slow per-net independent
        # fallback would also blow the budget) from a true "no path
        # found" exit (where independent routing is still worth trying).
        # Reset to ``False`` at the start of every ``route_coupled``
        # invocation.
        self.last_timeout_exceeded: bool = False

        # Issue #3921: disambiguates WHICH budget fired when
        # ``last_timeout_exceeded`` is True.  ``route_coupled`` sets the
        # shared ``last_timeout_exceeded`` flag for both the iteration
        # budget (``max_iterations_budget``) and the wall-clock budget
        # (``timeout_seconds``), so the caller's budget-exit diagnostic
        # cannot tell a 0.3s iteration bail from a 120s wall-clock bail.
        # ``True`` means the ITERATION budget was the binding constraint;
        # ``False`` (with ``last_timeout_exceeded`` True) means the
        # wall-clock budget fired.  Reset to ``False`` at the start of
        # every ``route_coupled`` invocation.
        self.last_iteration_limited: bool = False

        # Issue #3473 (review of #3439): number of A* iterations the
        # most recent ``route_coupled`` call consumed.  The two-phase
        # corridor-then-open caller charges the corridor attempt's
        # iterations against the shared per-pair iteration budget,
        # mirroring the wall-clock split -- otherwise a failing pair
        # receives the FULL ``max_iterations_budget`` twice (observed
        # 4000+4000 on board 06, doubling the diff-pair phase).
        self.last_iterations: int = 0

        # Issue #3508: progress diagnostics for the most recent
        # ``route_coupled`` call (smallest joint remaining Manhattan
        # distance any popped state achieved, and the state/node
        # achieving it).  Lets budget-exit handlers distinguish
        # "almost converged" from "structurally stuck", and gives the
        # near-miss rescue (``_rescue_near_miss_coupled``) a parent
        # chain to reconstruct the partial coupled route from.
        self.last_best_progress: float = float("inf")
        self.last_best_state: CoupledState | None = None
        self.last_best_node: CoupledNode | None = None
        # Issue #4459: backend that served the most-recent coupled search
        # ("python" or "cpp").  Lets the ``[coupled-timing]`` diagnostic report
        # ``best_state=n/a (cpp)`` instead of a misleading ``best_state=None``
        # for the C++ path, which carries no Python ``CoupledState`` object.
        self.last_coupled_backend: str = "python"

        # Issue #3508: per-search move-rejection counters keyed by
        # rejection reason (sym/asym x blocked/spacing/floor/trail,
        # plus corridor pruning).  Reset by ``route_coupled``;
        # surfaced by the caller's budget-exit diagnostics so a
        # stalled search reports WHAT is pruning its frontier.
        self.last_rejections: dict[str, int] = collections.defaultdict(int)

        # Pre-calculate trace clearance radius
        self._trace_half_width_cells = max(
            1,
            math.ceil(
                (self.rules.trace_width / 2 + self.rules.trace_clearance) / self.grid.resolution
            ),
        )

        # Pre-calculate via blocking radius
        self._via_half_cells = max(
            1,
            math.ceil(
                (self.rules.via_diameter / 2 + self.rules.via_clearance) / self.grid.resolution
            ),
        )

        # Issue #3508: extra radial slack a via needs BEYOND the trace
        # envelope the grid cells already encode (see _is_via_blocked).
        self._via_extra_cells = max(
            1,
            math.ceil(
                max(
                    0.0,
                    (self.rules.via_diameter / 2 + self.rules.via_clearance)
                    - (self.rules.trace_width / 2 + self.rules.trace_clearance),
                )
                / self.grid.resolution
            ),
        )

        # Orthogonal moves only for differential pairs (diagonal moves
        # would complicate spacing maintenance)
        self.directions = [
            (1, 0),  # Right
            (-1, 0),  # Left
            (0, 1),  # Down
            (0, -1),  # Up
        ]

        # Issue #4065: opt-in flag for the C++ coupled joint-state A*.
        # Default ON when a C++ backend is present; the search still falls
        # back to pure Python for the v1-deferred features
        # (``allow_swap_via``, ``manhattan_sum``) and whenever construction
        # of the C++ pathfinder raises.  Env-overridable
        # (KCT_COUPLED_CPP=0) so measurement / parity tests can force the
        # Python path without monkeypatching.
        self._use_cpp_coupled = os.environ.get("KCT_COUPLED_CPP", "1") != "0"
        # Cached (CppGrid, CppCoupledPathfinder) keyed by grid identity so
        # the one-time grid marshalling + pathfinder construction is reused
        # across route_coupled calls on the same pathfinder instance
        # (mirrors how CppPathfinder is constructed once and reused).
        self._cpp_coupled_impl: CppCoupledPathfinder | None = None
        self._cpp_coupled_grid: RoutingGrid | None = None

    def _is_cell_blocked(self, gx: int, gy: int, layer: int, net: int) -> bool:
        """Check if a cell is blocked for this net.

        Issue #3508: own-net cells are passable, matching the per-net
        pathfinder's ``different_net = cell.net != routing_net``
        convention.  The previous implementation additionally rejected
        any ``is_obstacle`` cell -- but ``RoutingGrid._add_pad_unsafe``
        sets ``is_obstacle = True`` on a pad's OWN clearance-halo and
        metal cells (it exists to defeat the negotiated-mode
        ``static_blocks`` release loophole, #2915/#2940, with own-net
        passability explicitly preserved via the ``cell.net`` check in
        the main pathfinder).  ORing ``is_obstacle`` here made every
        pad unreachable for its own coupled route: the joint search
        stalled at exactly the halo boundary (5-7 cells out) on all 9
        board 06 pairs, which is why coupled convergence was 0/9 at
        ANY budget.  True obstacles (board edge, keepouts,
        ``add_obstacle`` regions) carry ``cell.net == 0`` and remain
        blocked for every signal net.
        """
        if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
            return True
        if layer < 0 or layer >= self.grid.num_layers:
            return True

        cell = self.grid.grid[layer][gy][gx]
        if cell.blocked and cell.net != net:
            return True
        return False

    def _is_trace_blocked(self, gx: int, gy: int, layer: int, net: int) -> bool:
        """Check if placing a trace centerline at this cell would conflict.

        Issue #3508: this checks ONLY the head cell, matching the per-net
        pathfinder's convention.  The grid already encodes the full
        centerline clearance envelope at obstacle-marking time: pads are
        dilated by ``trace_clearance + trace_width / 2``
        (``RoutingGrid._clearance_for_pin_pitch``) and committed routes
        by the equivalent trace halo, so a cell is unblocked exactly
        when a trace centerline there satisfies clearance.  The previous
        implementation swept an ADDITIONAL ``(2 * half_width + 1)^2``
        square (+/- ``ceil((trace_width/2 + clearance)/resolution)`` =
        5 cells on board 06) around the head -- double-counting the
        clearance envelope to an effective ~0.45 mm.  In open field that
        was merely conservative; entering any fine-pitch pad
        neighbourhood (QFN-32/QFN-24/BGA-49/USB-C/FFC -- BOTH endpoints
        of every board 06 pair) it formed an impassable wall ~1-2.5 mm
        short of the pads, which is why every coupled search stalled at
        an identical frontier regardless of iteration budget or
        tie-break order (best_progress 34-49 on PCIE/USB2 across
        FIFO/LIFO and 10k/90k-iteration runs).
        """
        return self._is_cell_blocked(gx, gy, layer, net)

    def _is_via_blocked(self, gx: int, gy: int, net: int) -> bool:
        """Check if placing a via at this position would conflict on any layer.

        Issue #3508: the swept radius is now the DIFFERENCE between the
        via envelope and the trace envelope the grid cells already
        carry (see ``_is_trace_blocked``), not the full via envelope.
        A grid cell is unblocked when a TRACE centerline is legal
        there; a via additionally needs
        ``(via_diameter/2 + via_clearance) - (trace_width/2 +
        trace_clearance)`` of extra radial slack, which is what
        ``_via_extra_cells`` captures.  The previous full-envelope
        sweep (+/-8 cells on every layer on board 06) double-counted
        the marked halo exactly like the trace check did.

        Issue #3508 (second pass): a via must additionally never land
        on PAD METAL -- even its OWN net's pad.  Own-net passability
        (see ``_is_cell_blocked``) made pad cells legal for the via
        predicate too, and the crossing-tail synthesizer promptly
        placed vias exactly on its own goal pads (measured: 2
        ``via_in_pad`` errors at J3-9 / J4-2 on the first #3508
        re-route; the jlcpcb standard tier does not support
        via-in-pad).  ``cell.pad_blocked`` marks cells whose extent
        overlaps continuous pad metal (#3233), so reject the via when
        any cell under its DRILL footprint is pad metal on any layer.
        """
        drill_cells = max(0, int(math.ceil((self.rules.via_drill / 2) / self.grid.resolution)))
        for layer in range(self.grid.num_layers):
            for dy in range(-self._via_extra_cells, self._via_extra_cells + 1):
                for dx in range(-self._via_extra_cells, self._via_extra_cells + 1):
                    if self._is_cell_blocked(gx + dx, gy + dy, layer, net):
                        return True
            # Issue #3508: no via-in-pad regardless of net ownership.
            for dy in range(-drill_cells, drill_cells + 1):
                for dx in range(-drill_cells, drill_cells + 1):
                    cgx, cgy = gx + dx, gy + dy
                    if not (0 <= cgx < self.grid.cols and 0 <= cgy < self.grid.rows):
                        return True
                    if self.grid.grid[layer][cgy][cgx].pad_blocked:
                        return True
        return False

    def _is_at_goal(self, pos: GridPos, goal: GridPos | None) -> bool:
        """Check if a grid position is at the goal (ignoring layer)."""
        if goal is None:
            return False
        return pos.x == goal.x and pos.y == goal.y

    def _get_coupled_neighbors(
        self,
        state: CoupledState,
        p_net: int,
        n_net: int,
        p_goal: GridPos | None = None,
        n_goal: GridPos | None = None,
        p_start: GridPos | None = None,
        n_start: GridPos | None = None,
        target_spacing_cells: int | None = None,
        approach_radius_override: int | None = None,
        departure_radius_override: int | None = None,
        p_visited: frozenset[tuple[int, int, int]] | None = None,
        n_visited: frozenset[tuple[int, int, int]] | None = None,
        p_trail_buckets: dict[tuple[int, int], list[tuple[int, int, int]]] | None = None,
        n_trail_buckets: dict[tuple[int, int], list[tuple[int, int, int]]] | None = None,
    ) -> list[tuple[CoupledState, float, bool]]:
        """Generate valid coupled moves maintaining spacing.

        Args:
            state: Current coupled search state.
            p_net: Net id for the positive trace.
            n_net: Net id for the negative trace.
            p_goal: Goal grid position for the positive trace.
            n_goal: Goal grid position for the negative trace.
            p_start: Start grid position for the positive trace.
            n_start: Start grid position for the negative trace.
            target_spacing_cells: Per-call effective target spacing
                in grid cells.  When ``None``, falls back to
                ``self.target_spacing_cells``.  Issue #2484: the
                effective spacing is threaded as a kwarg so that
                ``route_coupled`` can widen it for a single call
                (e.g. when start pads sit further apart than the
                configured spacing) without mutating instance state.
            approach_radius_override: Per-call effective approach
                radius (Manhattan distance, in grid cells, from each
                trace to its goal at which the pair-spacing tolerance
                is widened).  When ``None``, falls back to
                ``max(target_spacing_cells, 6)``.  Issue #2490: scaled
                up by ``route_coupled`` when start and end pad pitches
                differ so the search has room to converge from the
                wider start spacing to the narrower goal spacing
                (USB-C vs MCU).
            departure_radius_override: Issue #3508: mirror of
                ``approach_radius_override`` for the START side.
                Within this Manhattan radius of the start pads the
                spacing tolerance is widened the same way, so the
                pair can transition from the physical start pad
                pitch to the configured coupled spacing (the
                mid-route target no longer inherits the start
                pitch).  When ``None``, falls back to
                ``max(target_spacing_cells, 6)``.
            p_visited: Issue #3078: optional set of grid cells the
                positive trace has already occupied along the current
                A* parent chain.  Cells are encoded as
                ``(x, y, layer)`` tuples.  Used as a path-history
                self-intersection guard so the asymmetric
                P-advance/N-advance moves cannot let one trace loop
                around and re-cross either its own trail or the
                partner trace's trail at full spacing -- the failure
                mode behind the 36k ``diffpair_clearance_intra``
                regression on board 06 (Issue #3078).  When ``None``
                or empty, no path-history check fires (legacy
                permissive behaviour).
            n_visited: Issue #3078: companion to ``p_visited`` for the
                negative trace.  Same encoding and semantics.

        Returns list of (new_state, cost, is_via) tuples.
        """
        if target_spacing_cells is None:
            target_spacing_cells = self.target_spacing_cells

        # Issue #3078: path-history check helpers.  Cells are encoded
        # as ``(x, y, layer)`` tuples (matching the parent-chain
        # bookkeeping in ``route_coupled``).  An empty/None set means
        # "no history to check" -- legacy permissive behaviour for
        # callers that did not opt in.
        p_visited_set = p_visited if p_visited else frozenset()
        n_visited_set = n_visited if n_visited else frozenset()

        # Issue #3508: trail PROXIMITY guard.  The exact-cell guard
        # below only rejects landing ON the partner's trail; a landing
        # ONE cell away from it (0.05 mm centerline distance on board
        # 06) is copper overlap that the #3320 gate later rejects
        # wholesale (measured: MIPI_CLK converged but committed copper
        # had P passing 1-2 cells from N's earlier fan-out trail --
        # worst -0.175 mm with 5802 offending segment pairs).  The
        # spacing floor cannot catch this because it constrains the
        # SIMULTANEOUS head positions, not head-vs-historical-trail
        # distance.  Reject any advancing landing within
        # ``min_spacing_cells`` (Euclidean, same layer) of the
        # PARTNER's accumulated trail, using the spatial buckets the
        # caller built during its parent-chain walk.
        prox_r = self.min_spacing_cells
        prox_bucket = max(1, prox_r)
        prox_r_sq = float(prox_r * prox_r)

        def _too_close_to_trail(
            cell: tuple[int, int, int],
            buckets: dict[tuple[int, int], list[tuple[int, int, int]]] | None,
        ) -> bool:
            if not buckets or prox_r <= 1:
                return False
            cx, cy, clayer = cell
            bx, by = cx // prox_bucket, cy // prox_bucket
            for dbx in (-1, 0, 1):
                for dby in (-1, 0, 1):
                    for tx, ty, tlayer in buckets.get((bx + dbx, by + dby), ()):
                        if tlayer != clayer:
                            continue
                        dx = tx - cx
                        dy = ty - cy
                        if float(dx * dx + dy * dy) < prox_r_sq - 1e-9:
                            return True
            return False

        def _self_intersects(
            new_p_pos: GridPos,
            new_n_pos: GridPos,
            p_advances: bool,
            n_advances: bool,
            p_is_endpoint_cell: bool,
            n_is_endpoint_cell: bool,
        ) -> bool:
            """Reject moves that put one trace onto a cell the other
            (or it itself) has already occupied.

            Endpoint cells (start/goal pads) are exempt from the
            cross-trail check because the pad footprint legitimately
            sits on those cells regardless of routing history.  The
            self-loop check still fires at non-endpoint cells.
            """
            if not p_visited_set and not n_visited_set:
                return False
            p_key = (new_p_pos.x, new_p_pos.y, new_p_pos.layer)
            n_key = (new_n_pos.x, new_n_pos.y, new_n_pos.layer)
            # Cross-trail: the advancing trace lands on the partner's
            # accumulated path.  Skip when the landing cell is an
            # endpoint (pad cells are shared geometry, not a routing
            # collision).
            if p_advances and not p_is_endpoint_cell and p_key in n_visited_set:
                return True
            if n_advances and not n_is_endpoint_cell and n_key in p_visited_set:
                return True
            # Issue #3508: proximity variant of the cross-trail check.
            if (
                p_advances
                and not p_is_endpoint_cell
                and _too_close_to_trail(p_key, n_trail_buckets)
            ):
                return True
            if (
                n_advances
                and not n_is_endpoint_cell
                and _too_close_to_trail(n_key, p_trail_buckets)
            ):
                return True
            # Self-loop: the advancing trace re-enters a cell it has
            # already occupied on its own trail.  This is the
            # mechanism behind the 7-vs-1061 segment asymmetry on
            # USB3_RX1 (Issue #3078).
            if p_advances and not p_is_endpoint_cell and p_key in p_visited_set:
                return True
            if n_advances and not n_is_endpoint_cell and n_key in n_visited_set:
                return True
            return False

        neighbors: list[tuple[CoupledState, float, bool]] = []

        # Issue #2473: Relax the spacing constraint when both traces
        # are within an "approach radius" of their goal pads.  This
        # lets a coupled run that started at one pad pitch converge
        # onto a goal pair with a different pad pitch (e.g., USB-C
        # 0.5mm pad spacing reached from MCU 0.8mm pad spacing).  The
        # relaxation only fires near the goals so the bulk of the
        # run still maintains constant spacing.
        approach_relaxed = False
        if p_goal is not None and n_goal is not None:
            p_dist_to_goal = abs(state.p_pos.x - p_goal.x) + abs(state.p_pos.y - p_goal.y)
            n_dist_to_goal = abs(state.n_pos.x - n_goal.x) + abs(state.n_pos.y - n_goal.y)
            # Issue #2490: ``approach_radius`` is sized to admit the
            # *full* spacing transition between the start and goal
            # pad pitches.  When they match (e.g., both at the
            # connector), the legacy default of ``max(target, 6)``
            # is retained.  When they differ (USB device side: MCU
            # 0.8mm pitch vs USB-C 0.5mm pitch), ``route_coupled``
            # passes a wider override so the convergence zone has
            # room to reduce spacing one cell at a time without
            # exceeding the approach tolerance per step.
            if approach_radius_override is not None:
                approach_radius = approach_radius_override
            else:
                approach_radius = max(target_spacing_cells, 6)
            if p_dist_to_goal <= approach_radius and n_dist_to_goal <= approach_radius:
                approach_relaxed = True

        # Issue #3508: departure-phase relaxation -- the mirror of the
        # approach phase, anchored on the START pads.  The mid-route
        # spacing target is now the configured coupled spacing (it no
        # longer inherits the start pad pitch), so a pair leaving
        # wide-pitch connector pads needs the same widened tolerance
        # near the start that the approach phase grants near the goal,
        # or the very first symmetric step away from the pads would be
        # rejected for |pitch - target| > 1.
        departure_relaxed = False
        if p_start is not None and n_start is not None:
            p_dist_from_start = abs(state.p_pos.x - p_start.x) + abs(state.p_pos.y - p_start.y)
            n_dist_from_start = abs(state.n_pos.x - n_start.x) + abs(state.n_pos.y - n_start.y)
            if departure_radius_override is not None:
                departure_radius = departure_radius_override
            else:
                departure_radius = max(target_spacing_cells, 6)
            if p_dist_from_start <= departure_radius and n_dist_from_start <= departure_radius:
                departure_relaxed = True

        spacing_relaxed = approach_relaxed or departure_relaxed

        # Issue #3508: the relaxed tolerance must cover the FULL pitch
        # transition of whichever phase is active.  The legacy
        # ``max(1, target)`` only worked because the target itself had
        # been widened to the start pitch; with the configured coupled
        # target restored, a 20-cell USB-C pitch against a 7-cell
        # target needs tolerance >= 13 inside the endpoint zones.  The
        # phase radii are already sized as ``delta * 2 + 4`` so they
        # dominate the pitch delta by construction.
        relaxed_tolerance = max(1, target_spacing_cells)
        if approach_relaxed:
            relaxed_tolerance = max(relaxed_tolerance, approach_radius)
        if departure_relaxed:
            relaxed_tolerance = max(relaxed_tolerance, departure_radius)

        # Try moving both traces in the same direction
        for dx, dy in self.directions:
            new_p = GridPos(
                state.p_pos.x + dx,
                state.p_pos.y + dy,
                state.p_pos.layer,
            )
            new_n = GridPos(
                state.n_pos.x + dx,
                state.n_pos.y + dy,
                state.n_pos.layer,
            )

            # Check if both new positions are valid.  Issue #2473:
            # Skip the trace-blocked check when stepping into a
            # known goal or start cell — those cells host the pad
            # we are explicitly trying to land on, so the
            # half-width footprint of the partner pad must not
            # disqualify the move.
            p_is_endpoint = self._is_at_goal(new_p, p_goal) or self._is_at_goal(new_p, p_start)
            n_is_endpoint = self._is_at_goal(new_n, n_goal) or self._is_at_goal(new_n, n_start)
            if not p_is_endpoint and self._is_trace_blocked(new_p.x, new_p.y, new_p.layer, p_net):
                self.last_rejections["sym_blocked_p"] += 1
                continue
            if not n_is_endpoint and self._is_trace_blocked(new_n.x, new_n.y, new_n.layer, n_net):
                self.last_rejections["sym_blocked_n"] += 1
                continue

            # Calculate spacing between new positions
            spacing_dx = new_p.x - new_n.x
            spacing_dy = new_p.y - new_n.y
            new_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)

            # Only accept moves that maintain target spacing (within tolerance).
            # Issue #2473: When the search is in the "approach" phase
            # near the goal pads (or, issue #3508, the "departure"
            # phase near the start pads), allow wider spacing variation
            # so mismatched pad pitches can converge to / diverge from
            # the coupled target.
            tolerance = relaxed_tolerance if spacing_relaxed else 1
            if abs(new_spacing - target_spacing_cells) > tolerance:
                self.last_rejections["sym_spacing"] += 1
                continue

            # Issue #3012: Hard floor on within-pair spacing.  Independent
            # of the approach-phase tolerance, the search must not place
            # P and N centerlines closer than
            # ``(trace_width + intra_pair_clearance) / grid.resolution``
            # cells apart, or post-route the partner-net edges overlap.
            # The floor is bypassed when BOTH new positions sit on
            # endpoint cells (start or goal pads) -- those cells are
            # owned by the pad footprints whose own spacing is set by
            # the physical board geometry, not the router.
            if self.min_spacing_cells > 0 and not (p_is_endpoint and n_is_endpoint):
                # Use a small epsilon so a Euclidean spacing of exactly
                # min_spacing_cells (axis-aligned) is accepted.
                if new_spacing + 1e-9 < self.min_spacing_cells:
                    self.last_rejections["sym_floor"] += 1
                    continue

            # Issue #3078: path-history self-intersection guard.  Both
            # traces advance in a symmetric move, so both are checked.
            if _self_intersects(
                new_p,
                new_n,
                p_advances=True,
                n_advances=True,
                p_is_endpoint_cell=p_is_endpoint,
                n_is_endpoint_cell=n_is_endpoint,
            ):
                self.last_rejections["sym_trail"] += 1
                continue

            # Calculate cost
            new_direction = (dx, dy)
            cost = self.rules.cost_straight

            # Add turn penalty if direction changed
            if state.direction != (0, 0) and state.direction != new_direction:
                cost += self.rules.cost_turn

            # Issue #4080: corridor attractor for the coupled joint-state
            # loop.  A symmetric move advances BOTH nets, so query each
            # landing cell against its own net (mirrors the single-ended
            # attractor in ``pathfinder.py``).  Clamped at 0 so the joint
            # step cost stays non-negative and A* admissibility holds.
            attractor_bonus = self.grid.get_corridor_attractor_bonus(
                new_p.layer, new_p.x, new_p.y, p_net, self.rules.cost_corridor_attractor
            ) + self.grid.get_corridor_attractor_bonus(
                new_n.layer, new_n.x, new_n.y, n_net, self.rules.cost_corridor_attractor
            )
            if attractor_bonus > 0.0:
                cost = max(0.0, cost - attractor_bonus)

            new_state = CoupledState(new_p, new_n, new_direction)
            neighbors.append((new_state, cost, False))

        # Issue #2490: Asymmetric "converge" moves.  When start and
        # goal pad pitches differ (e.g., USB device-side: MCU 0.8mm
        # pitch -> USB-C 0.5mm pitch), the symmetric step moves above
        # preserve spacing exactly, so the search can never land both
        # traces on endpoint cells whose pitch is narrower than the
        # start pitch.  Allowing one trace to advance while the other
        # holds closes the spacing one cell at a time.
        #
        # Issue #3508: asymmetric moves are now allowed MID-ROUTE too
        # (originally #2490 restricted them to the approach phase).
        # Symmetric moves translate both heads by the same vector, so
        # the P->N offset vector is FROZEN for the whole mid-route --
        # the pair physically cannot turn a corner whose leg runs
        # parallel to that offset: the trailing trace must ride the
        # leading trace's trail and the #3078 path-history guard
        # (correctly) rejects it.  On board 06 this made 9/9 pairs
        # structurally infeasible for the coupled search (MIPI_CLK
        # burned 90k corridor-bounded iterations without converging;
        # the L-shaped FFC->IC route needs a leg parallel to the pad
        # offset).  Concentric corners need the offset vector to
        # ROTATE, which only asymmetric moves can do (the outer trace
        # walks a discrete arc around the holding inner trace).
        #
        # The #2490 restriction predates the guards that make
        # mid-route asymmetry safe: the ``min_spacing_cells`` hard
        # floor (#3012) prevents the spacing collapse, the
        # path-history guard (#3078) prevents the loop-around
        # pathology, and the mid-route tolerance here stays TIGHT
        # (+/-1 cell, same as the symmetric branch) -- the wide
        # ``max(1, target)`` tolerance still applies only inside the
        # approach radius.
        if p_goal is not None and n_goal is not None:
            asym_tolerance = relaxed_tolerance if spacing_relaxed else 1
            for dx, dy in self.directions:
                # P advances, N holds.
                cand_p = GridPos(state.p_pos.x + dx, state.p_pos.y + dy, state.p_pos.layer)
                cand_n = state.n_pos
                p_is_endpoint = self._is_at_goal(cand_p, p_goal) or self._is_at_goal(
                    cand_p, p_start
                )
                if not (
                    p_is_endpoint
                    or not self._is_trace_blocked(cand_p.x, cand_p.y, cand_p.layer, p_net)
                ):
                    self.last_rejections["asym_blocked_p"] += 1
                else:
                    spacing_dx = cand_p.x - cand_n.x
                    spacing_dy = cand_p.y - cand_n.y
                    new_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)
                    if abs(new_spacing - target_spacing_cells) > asym_tolerance:
                        self.last_rejections["asym_spacing_p"] += 1
                    else:
                        # Issue #3012: enforce the within-pair spacing
                        # floor in the asymmetric P-advance move.  The
                        # asymmetric moves only fire in the approach
                        # phase, where the legacy tolerance was wide
                        # enough to let centerlines coincide; without
                        # this floor we observe -0.150mm overlap on
                        # board 07 diff pairs.  Endpoint cells (P at
                        # its pad AND N at its pad) bypass the floor
                        # since the pads themselves define the spacing.
                        n_is_endpoint = self._is_at_goal(cand_n, n_goal) or self._is_at_goal(
                            cand_n, n_start
                        )
                        bypass_floor = p_is_endpoint and n_is_endpoint
                        if (
                            self.min_spacing_cells > 0
                            and not bypass_floor
                            and new_spacing + 1e-9 < self.min_spacing_cells
                        ):
                            self.last_rejections["asym_floor_p"] += 1
                        elif _self_intersects(
                            cand_p,
                            cand_n,
                            p_advances=True,
                            n_advances=False,
                            p_is_endpoint_cell=p_is_endpoint,
                            n_is_endpoint_cell=n_is_endpoint,
                        ):
                            # Issue #3078: P-advance must not land on
                            # N's accumulated trail or on its own
                            # accumulated trail.  Without this gate the
                            # asymmetric move lets P loop around N and
                            # re-converge from the opposite side,
                            # producing the centerline-coincident
                            # routes that DRC reports as -0.2mm
                            # intra-pair clearance.
                            self.last_rejections["asym_trail_p"] += 1
                        else:
                            # Direction tracking only reflects P's motion;
                            # tag with the new direction so the cost-of-turn
                            # logic still fires when the path bends.
                            cost = self.rules.cost_straight
                            if state.direction != (0, 0) and state.direction != (dx, dy):
                                cost += self.rules.cost_turn
                            # Issue #4080: corridor attractor (per-net) for
                            # the asymmetric P-advance move.  See the
                            # symmetric-move comment above.
                            attractor_bonus = self.grid.get_corridor_attractor_bonus(
                                cand_p.layer,
                                cand_p.x,
                                cand_p.y,
                                p_net,
                                self.rules.cost_corridor_attractor,
                            ) + self.grid.get_corridor_attractor_bonus(
                                cand_n.layer,
                                cand_n.x,
                                cand_n.y,
                                n_net,
                                self.rules.cost_corridor_attractor,
                            )
                            if attractor_bonus > 0.0:
                                cost = max(0.0, cost - attractor_bonus)
                            new_state = CoupledState(cand_p, cand_n, (dx, dy))
                            neighbors.append((new_state, cost, False))

                # N advances, P holds.
                cand_p2 = state.p_pos
                cand_n2 = GridPos(state.n_pos.x + dx, state.n_pos.y + dy, state.n_pos.layer)
                n_is_endpoint = self._is_at_goal(cand_n2, n_goal) or self._is_at_goal(
                    cand_n2, n_start
                )
                if not n_is_endpoint and self._is_trace_blocked(
                    cand_n2.x, cand_n2.y, cand_n2.layer, n_net
                ):
                    self.last_rejections["asym_blocked_n"] += 1
                    continue
                spacing_dx = cand_p2.x - cand_n2.x
                spacing_dy = cand_p2.y - cand_n2.y
                new_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)
                if abs(new_spacing - target_spacing_cells) > asym_tolerance:
                    self.last_rejections["asym_spacing_n"] += 1
                    continue
                # Issue #3012: same within-pair spacing floor as the
                # P-advance branch.  P holds at its current position
                # here; the floor is bypassed only when P happens to
                # already be at its pad AND the candidate N is at its
                # pad.
                p_is_endpoint_held = self._is_at_goal(cand_p2, p_goal) or self._is_at_goal(
                    cand_p2, p_start
                )
                bypass_floor = p_is_endpoint_held and n_is_endpoint
                if (
                    self.min_spacing_cells > 0
                    and not bypass_floor
                    and new_spacing + 1e-9 < self.min_spacing_cells
                ):
                    self.last_rejections["asym_floor_n"] += 1
                    continue
                # Issue #3078: N-advance must not land on P's
                # accumulated trail or on its own accumulated trail.
                # See the P-advance branch above for the failure mode
                # this prevents (board 06 USB3_TX1+ 1063-segment
                # loop-around).
                if _self_intersects(
                    cand_p2,
                    cand_n2,
                    p_advances=False,
                    n_advances=True,
                    p_is_endpoint_cell=p_is_endpoint_held,
                    n_is_endpoint_cell=n_is_endpoint,
                ):
                    self.last_rejections["asym_trail_n"] += 1
                    continue
                cost = self.rules.cost_straight
                if state.direction != (0, 0) and state.direction != (dx, dy):
                    cost += self.rules.cost_turn
                # Issue #4080: corridor attractor (per-net) for the
                # asymmetric N-advance move.  See the symmetric-move
                # comment above.
                attractor_bonus = self.grid.get_corridor_attractor_bonus(
                    cand_p2.layer,
                    cand_p2.x,
                    cand_p2.y,
                    p_net,
                    self.rules.cost_corridor_attractor,
                ) + self.grid.get_corridor_attractor_bonus(
                    cand_n2.layer,
                    cand_n2.x,
                    cand_n2.y,
                    n_net,
                    self.rules.cost_corridor_attractor,
                )
                if attractor_bonus > 0.0:
                    cost = max(0.0, cost - attractor_bonus)
                new_state = CoupledState(cand_p2, cand_n2, (dx, dy))
                neighbors.append((new_state, cost, False))

        # Issue #2490: Endpoint via exception.  When the current state
        # sits exactly on a start or goal pad, the pad's footprint is
        # already part of the board geometry — the same cells that
        # ``_is_via_blocked`` would inspect are occupied by the pad
        # whose net we are trying to drop a via for.  Without this
        # exception, ``_is_via_blocked`` rejects via placement at the
        # source pad of the coupled run on dense pad fields (e.g.,
        # USB-C 0.5mm pitch), trapping the search on layer 0 even when
        # an inner/back layer is wide open.  We mirror the existing
        # trace-blocked exception at endpoints (lines 311-316).
        p_at_endpoint = self._is_at_goal(state.p_pos, p_goal) or self._is_at_goal(
            state.p_pos, p_start
        )
        n_at_endpoint = self._is_at_goal(state.n_pos, n_goal) or self._is_at_goal(
            state.n_pos, n_start
        )

        # Try layer change (via) - both traces must change layer together
        routable_layers = self.grid.get_routable_indices()
        for new_layer in routable_layers:
            if new_layer == state.p_pos.layer:
                continue

            # Check if vias can be placed at both positions.  Skip the
            # via-blocked check at endpoint pads — see comment above.
            if not p_at_endpoint and self._is_via_blocked(state.p_pos.x, state.p_pos.y, p_net):
                continue
            if not n_at_endpoint and self._is_via_blocked(state.n_pos.x, state.n_pos.y, n_net):
                continue

            new_p = GridPos(state.p_pos.x, state.p_pos.y, new_layer)
            new_n = GridPos(state.n_pos.x, state.n_pos.y, new_layer)

            # Check if new layer positions are valid
            if not p_at_endpoint and self._is_trace_blocked(new_p.x, new_p.y, new_p.layer, p_net):
                continue
            if not n_at_endpoint and self._is_trace_blocked(new_n.x, new_n.y, new_n.layer, n_net):
                continue

            # Via cost for both traces
            cost = self.rules.cost_via * 2

            # Issue #4080: corridor attractor on the via-drop destination
            # cells -- the reservation is what makes the coupled router
            # prefer to actually via-hop INTO the reserved channel
            # (mirrors the single-ended via-drop attractor in
            # ``pathfinder.py``).  Per-net query, clamped at 0.
            attractor_bonus = self.grid.get_corridor_attractor_bonus(
                new_p.layer, new_p.x, new_p.y, p_net, self.rules.cost_corridor_attractor
            ) + self.grid.get_corridor_attractor_bonus(
                new_n.layer, new_n.x, new_n.y, n_net, self.rules.cost_corridor_attractor
            )
            if attractor_bonus > 0.0:
                cost = max(0.0, cost - attractor_bonus)

            new_state = CoupledState(new_p, new_n, state.direction)
            neighbors.append((new_state, cost, True))

        # Issue #2473: Swap-via move for polarity-swap crossover.  Both
        # traces drop a via at their current location, then re-emerge on
        # an inner layer with their grid positions exchanged.  This
        # supports USB-C-shaped pad layouts where the connector inverts
        # the differential polarity (D+/D- swap rows between A and B).
        if self.allow_swap_via:
            for new_layer in routable_layers:
                if new_layer == state.p_pos.layer:
                    continue

                # Both pads must be able to host a via at their current
                # position on every layer (the via spans through-hole).
                # Issue #2490: Endpoint pads are exempt — the pad
                # footprint already occupies the cells the via would
                # span.
                if not p_at_endpoint and self._is_via_blocked(state.p_pos.x, state.p_pos.y, p_net):
                    continue
                if not n_at_endpoint and self._is_via_blocked(state.n_pos.x, state.n_pos.y, n_net):
                    continue

                # After the swap, the P-trace continues from where N was,
                # and vice versa, on the new layer.
                swapped_p = GridPos(state.n_pos.x, state.n_pos.y, new_layer)
                swapped_n = GridPos(state.p_pos.x, state.p_pos.y, new_layer)

                if self._is_trace_blocked(swapped_p.x, swapped_p.y, swapped_p.layer, p_net):
                    continue
                if self._is_trace_blocked(swapped_n.x, swapped_n.y, swapped_n.layer, n_net):
                    continue

                # Higher cost than a normal via to discourage gratuitous
                # swaps when a straight path would suffice.
                cost = self.rules.cost_via * 3

                # Reset direction after the swap — the orientation has
                # inverted, so any prior straight-line streak is broken.
                new_state = CoupledState(swapped_p, swapped_n, (0, 0))
                neighbors.append((new_state, cost, True))

        return neighbors

    def _heuristic(
        self,
        state: CoupledState,
        p_goal: GridPos,
        n_goal: GridPos,
    ) -> float:
        """Calculate heuristic for coupled A* search.

        Two modes (selected at construction via ``heuristic_mode``):

        * ``"manhattan_sum"`` (legacy): returns
          ``(p_dist + n_dist) * cost_straight + layer_cost``.  This
          biases the priority queue against partner-synchronised
          moves: a symmetric step reduces the sum by 2 cost units,
          while an asymmetric P-only-or-N-only step reduces it by 1,
          even though both cost the same.  Net effect: A* preferentially
          extends asymmetric escape stubs that produce the
          ``diffpair_clearance_intra`` violations on board 06.
        * ``"partner_aware"`` (Issue #3115, angle #5, default):
          returns ``max(p_dist, n_dist) * cost_straight +
          spacing_penalty + layer_cost``.

          Admissibility argument (informal): every real path that
          reaches the goal must advance the *slower* of P/N by at
          least ``max(p_dist, n_dist)`` cells, so the ``max`` term
          never exceeds the true remaining path cost.  The
          ``spacing_penalty`` term costs at most
          ``abs(current_spacing - target_spacing_cells) *
          cost_straight * spacing_penalty_factor`` and the true cost
          of correcting that divergence requires at least
          ``abs(current_spacing - target_spacing_cells) *
          cost_straight`` (one asymmetric converge move per cell of
          divergence), so any ``spacing_penalty_factor <= 1.0`` keeps
          the heuristic admissible.  The ``layer_cost`` term is the
          same admissible per-trace via-cost the legacy heuristic
          uses.
        """
        # Manhattan distance for both traces
        p_dist = abs(state.p_pos.x - p_goal.x) + abs(state.p_pos.y - p_goal.y)
        n_dist = abs(state.n_pos.x - n_goal.x) + abs(state.n_pos.y - n_goal.y)

        # Layer change cost if needed
        layer_cost = 0.0
        if state.p_pos.layer != p_goal.layer:
            layer_cost += self.rules.cost_via
        if state.n_pos.layer != n_goal.layer:
            layer_cost += self.rules.cost_via

        if self.heuristic_mode == "manhattan_sum":
            return (p_dist + n_dist) * self.rules.cost_straight + layer_cost

        # heuristic_mode == "partner_aware"
        max_dist = max(p_dist, n_dist)
        # Spacing penalty -- ranks states whose current center-to-
        # center spacing diverges from the target lower in the heap.
        # We compute the Euclidean spacing rather than Manhattan
        # because the coupled-move tolerance check uses Euclidean
        # (see _get_coupled_neighbors at line ~604).
        spacing_dx = state.p_pos.x - state.n_pos.x
        spacing_dy = state.p_pos.y - state.n_pos.y
        current_spacing = math.sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy)
        spacing_divergence = abs(current_spacing - self.target_spacing_cells)
        spacing_penalty = (
            spacing_divergence * self.rules.cost_straight * self.spacing_penalty_factor
        )
        return max_dist * self.rules.cost_straight + spacing_penalty + layer_cost

    def _cpp_coupled_available(self) -> bool:
        """Whether the C++ coupled search may handle THIS pathfinder.

        v1 scope (Issue #4065): the C++ port covers the ``partner_aware``
        heuristic with ``allow_swap_via`` off.  The legacy ``manhattan_sum``
        heuristic and the USB-C polarity-swap ``allow_swap_via`` move are
        deferred and stay on the pure-Python search.
        """
        if not self._use_cpp_coupled:
            return False
        if self.allow_swap_via:
            return False
        if self.heuristic_mode != "partner_aware":
            return False
        from .cpp_backend import is_cpp_available

        return is_cpp_available()

    def _get_cpp_coupled_impl(self) -> CppCoupledPathfinder | None:
        """Build (once) and return the cached C++ coupled pathfinder.

        Mirrors the ``CppPathfinder`` lifecycle: the ``CppGrid`` is
        marshalled from ``self.grid`` once and the C++ pathfinder is
        constructed once, then reused across ``route_coupled`` calls.
        Returns ``None`` when the backend is unavailable or construction
        raises (the caller then falls back to pure Python).
        """
        if self._cpp_coupled_impl is not None and self._cpp_coupled_grid is self.grid:
            return self._cpp_coupled_impl
        try:
            from .cpp_backend import CppCoupledPathfinder, CppGrid

            # Issue #4065 (reach-regression root cause): ``from_routing_grid``
            # unconditionally reassigns ``grid._cpp_grid = <new CppGrid>``
            # (cpp_backend.py, #2481 back-reference).  That back-reference is
            # the ONE the single-ended ``CppPathfinder`` relies on for rip-up
            # invalidation: ``RoutingGrid.unmark_route`` calls
            # ``self._cpp_grid.invalidate_stored_routes()`` so the C++
            # ``stored_vias_`` / ``stored_segments_`` snapshot no longer
            # references a ripped-up route.  The single-ended router marks its
            # routes on its OWN grid (``RoutingCore.router._grid``), a
            # DIFFERENT object.  When the coupled pre-phase builds its private
            # CppGrid here it HIJACKS ``grid._cpp_grid`` to point at the
            # coupled snapshot, so every subsequent negotiated-loop rip-up
            # invalidates the wrong grid and the single-ended router keeps
            # consulting stale via/segment blockers -- which is exactly why the
            # board-06 negotiated loop re-routed only 2/4 (vs 3/4 on the Python
            # baseline) at iter 2 and dropped USB3_RX1- (20/21 instead of
            # 21/21).  The coupled pathfinder needs its own CppGrid but must
            # NOT steal the single-ended router's paired back-reference, so
            # snapshot ``grid._cpp_grid`` and restore it afterwards.
            saved_cpp_grid = getattr(self.grid, "_cpp_grid", None)
            # Restore the single-ended router's back-reference (or ``None`` if
            # it had none) in a ``finally`` so it is restored even when
            # ``from_routing_grid`` raises AFTER hijacking ``grid._cpp_grid``
            # mid-copy (e.g. during the bulk cell copy or the Issue #4071
            # corridor-reservation marshalling): the coupled pathfinder keeps
            # ``cpp_grid`` in ``impl`` below, but the Python grid's
            # ``_cpp_grid`` invalidation hook must continue to target the
            # single-ended router's grid.  The outer ``try/except Exception``
            # still routes any raised exception to the Python fallback.
            try:
                cpp_grid = CppGrid.from_routing_grid(self.grid)
            finally:
                self.grid._cpp_grid = saved_cpp_grid
            impl = CppCoupledPathfinder(
                cpp_grid,
                self.rules,
                target_spacing_cells=self.target_spacing_cells,
                min_spacing_cells=self.min_spacing_cells,
                trace_half_width_cells=self._trace_half_width_cells,
                via_extra_cells=self._via_extra_cells,
                via_drill_cells=max(
                    0, int(math.ceil((self.rules.via_drill / 2) / self.grid.resolution))
                ),
                spacing_penalty_factor=self.spacing_penalty_factor,
                heuristic_weight=self.heuristic_weight,
            )
        except Exception:
            logger.debug("C++ coupled pathfinder construction failed; using Python", exc_info=True)
            self._use_cpp_coupled = False
            return None
        self._cpp_coupled_impl = impl
        self._cpp_coupled_grid = self.grid
        return impl

    def _try_cpp_route_coupled(
        self,
        *,
        p_start_pos: GridPos,
        n_start_pos: GridPos,
        p_goal_pos: GridPos,
        n_goal_pos: GridPos,
        start_layer: int,
        end_layer: int,
        p_net: int,
        n_net: int,
        effective_target_spacing: int,
        effective_approach_radius: int,
        effective_departure_radius: int,
        corridor: frozenset[tuple[int, int]] | None,
        timeout_seconds: float | None,
        max_iterations_budget: int | None,
    ) -> tuple[bool, tuple[Route, Route] | None] | None:
        """Attempt the coupled search via the C++ backend (Issue #4065).

        Returns ``None`` when the C++ path does not apply (backend absent /
        deferred feature / construction failed) -- the caller then runs the
        pure-Python A*.  Otherwise returns ``(handled=True, result)`` where
        ``result`` is the reconstructed ``(p_route, n_route)`` tuple or
        ``None`` (search failed / budget exit); the C++ diagnostics are
        written to ``self.last_*`` so budget-exit handling is unchanged.
        """
        if not self._cpp_coupled_available():
            return None
        impl = self._get_cpp_coupled_impl()
        if impl is None:
            return None

        # Marshal the corridor frozenset -> flat cols*rows bitset for O(1)
        # C++ membership (diffpair_routing.py:446 build_corridor_mask churn
        # -> a byte array here).  Empty list = no corridor.
        corridor_bitset: list[int] = []
        if corridor is not None:
            cols, rows = self.grid.cols, self.grid.rows
            bitset = bytearray(cols * rows)
            for cx, cy in corridor:
                if 0 <= cx < cols and 0 <= cy < rows:
                    bitset[cy * cols + cx] = 1
            corridor_bitset = list(bitset)

        routable_layers = list(self.grid.get_routable_indices())

        path, diagnostics = impl.route(
            p_start_xy=(p_start_pos.x, p_start_pos.y),
            n_start_xy=(n_start_pos.x, n_start_pos.y),
            start_layer=start_layer,
            p_goal_xy=(p_goal_pos.x, p_goal_pos.y),
            n_goal_xy=(n_goal_pos.x, n_goal_pos.y),
            end_layer=end_layer,
            p_net=p_net,
            n_net=n_net,
            effective_target_spacing=effective_target_spacing,
            effective_approach_radius=effective_approach_radius,
            effective_departure_radius=effective_departure_radius,
            routable_layers=routable_layers,
            corridor_bitset=corridor_bitset,
            max_iterations_budget=(
                max_iterations_budget
                if max_iterations_budget is not None and max_iterations_budget > 0
                else 0
            ),
            timeout_seconds=(
                float(timeout_seconds)
                if timeout_seconds is not None and timeout_seconds > 0
                else 0.0
            ),
        )

        # Mirror the diagnostic bookkeeping the Python loop maintains.
        self.last_iterations = int(diagnostics["iterations"])
        bp = diagnostics["best_progress"]
        self.last_best_progress = float("inf") if bp < 0 else float(bp)
        # Issue #4459: the C++ joint-state search carries no Python
        # ``CoupledState`` object, so ``last_best_state`` / ``last_best_node``
        # are genuinely unavailable on this path -- but ``last_best_progress``
        # above IS the real progress signal.  The old ``[coupled-timing]``
        # print read ``last_best_state`` (hard-set to ``None`` here) and
        # printed ``best_state=None`` for EVERY C++ pair, implying "never
        # moved" even when the joint A* made progress.  Record the backend so
        # the diagnostic can print ``best_state=n/a (cpp)`` and lean on
        # ``best_progress`` / ``rejections`` instead of the None red herring.
        self.last_best_state = None
        self.last_best_node = None
        self.last_coupled_backend = "cpp"
        self.last_timeout_exceeded = bool(diagnostics["timeout_exceeded"])
        self.last_iteration_limited = bool(diagnostics["iteration_limited"])
        # Issue #4459: populate the rejection histogram from the C++ search
        # (was hard-emptied here, so ``last_rejections`` was categorically
        # empty on the C++ path and no guard-pruning signal survived).
        self.last_rejections = collections.defaultdict(int, diagnostics.get("rejections", {}) or {})

        if path is None:
            return True, None

        return True, self._reconstruct_coupled_routes_from_cpp_path(path)

    def _reconstruct_coupled_routes_from_cpp_path(
        self,
        path: list[tuple[int, int, int, int, int, int, bool]],
    ) -> tuple[Route, Route]:
        """Build (p_route, n_route) from a C++ joint grid-cell path.

        Produces the exact same ``p_path`` / ``n_path`` world-coordinate
        lists that ``_reconstruct_coupled_routes`` builds from the Python
        parent chain, then feeds them to the UNCHANGED
        ``_build_route_from_path`` -- so C++ and Python routes are
        byte-identical for the same joint path (Issue #4065).  The Pad
        identity for width/net/name is recovered from the endpoint cells
        via the stored ``_cpp_reconstruct_pads`` set by the caller.
        """
        p_start, p_end, n_start, n_end = self._cpp_reconstruct_pads
        p_route = Route(net=p_start.net, net_name=p_start.net_name)
        n_route = Route(net=n_start.net, net_name=n_start.net_name)

        p_path: list[tuple[float, float, int, bool]] = []
        n_path: list[tuple[float, float, int, bool]] = []
        for p_x, p_y, p_layer, n_x, n_y, n_layer, via_from_parent in path:
            p_wx, p_wy = self.grid.grid_to_world(p_x, p_y)
            n_wx, n_wy = self.grid.grid_to_world(n_x, n_y)
            p_path.append((p_wx, p_wy, p_layer, via_from_parent))
            n_path.append((n_wx, n_wy, n_layer, via_from_parent))

        self._build_route_from_path(p_route, p_path, p_start, p_end)
        self._build_route_from_path(n_route, n_path, n_start, n_end)
        return p_route, n_route

    def route_coupled(
        self,
        p_start: Pad,
        p_end: Pad,
        n_start: Pad,
        n_end: Pad,
        timeout_seconds: float | None = None,
        max_iterations_budget: int | None = None,
        corridor: frozenset[tuple[int, int]] | None = None,
    ) -> tuple[Route, Route] | None:
        """Route a differential pair with coupled pathfinding.

        Args:
            p_start: Positive trace start pad
            p_end: Positive trace end pad
            n_start: Negative trace start pad
            n_end: Negative trace end pad
            timeout_seconds: Issue #3089: Optional wall-clock budget (in
                seconds) for the A* search.  When set, the
                ``while open_set`` loop checks ``time.monotonic()`` every
                ``_TIMEOUT_CHECK_INTERVAL`` iterations and returns
                ``None`` once the elapsed time exceeds the budget.  This
                lets callers (``route_differential_pair_coupled`` /
                ``route_all_with_diffpairs``) bound the per-pair cost
                without changing the algorithm.  The caller is expected
                to handle the ``None`` result the same way it handles
                an exhausted-search ``None`` (fall back to independent
                routing or log a skipped-budget diagnostic).
                ``None`` (default) preserves the legacy unbounded
                behaviour.
            max_iterations_budget: Issue #3144: Optional **iteration**
                budget.  When set, the search aborts (returns ``None``,
                sets ``last_timeout_exceeded=True``) once
                ``iterations >= max_iterations_budget``.  Unlike
                ``timeout_seconds``, the iteration budget is
                independent of CPU speed -- the same pair always exits
                the same way on a 2-core CI runner as on an 8-core
                development machine.  This eliminates the
                timing-dependent budget-classification non-determinism
                described in #3144 (different pairs land in coupled-vs-
                deferred buckets on different runs because the
                wall-clock deadline lands at different points in the
                search depending on runner load).  Whichever of
                ``timeout_seconds`` and ``max_iterations_budget`` fires
                first triggers the exit.  ``None`` (default) preserves
                wall-clock-only behaviour.  Note this is distinct from
                the unconditional ``max_iterations`` floor at line
                ``self.grid.cols * self.grid.rows * 4`` which is the
                memory backstop; ``max_iterations_budget`` is the
                user-tunable classifier and is expected to fire much
                earlier than the backstop.
            corridor: Issue #3439: Optional layer-agnostic corridor
                mask (set of ``(x, y)`` grid cells, typically built by
                :func:`build_corridor_mask` from a single-ended guide
                route).  When set, every generated neighbor state must
                place BOTH the P and N head positions inside the
                corridor; states outside are pruned before they reach
                the open set.  This converts the open joint-state
                search (quadratic in the single-net state space and
                intractable in pure Python on large boards) into a
                corridor-bounded near-1D search that completes in
                seconds.  Start/goal endpoint cells are exempt so an
                under-dilated corridor can never disqualify the only
                landing cells.  ``None`` (default) preserves the
                unconstrained legacy search.

        Returns:
            Tuple of (p_route, n_route) or None if routing failed (no
            path found, ``max_iterations`` exhausted,
            ``max_iterations_budget`` exceeded, or ``timeout_seconds``
            wall-clock budget exceeded).
        """
        # Issue #3089: reset the timeout-exit flag.  Callers consult
        # this immediately after ``route_coupled`` returns ``None`` to
        # decide whether to attempt an independent-routing fallback.
        self.last_timeout_exceeded = False
        # Issue #3921: reset the iteration-vs-wall-clock discriminator.
        self.last_iteration_limited = False
        # Issue #3473: reset the iteration counter for this call.
        self.last_iterations = 0
        # Issue #3508: best progress-toward-goal (joint Manhattan
        # remaining distance, max over the two heads) any popped state
        # achieved, plus the state that achieved it.  Consumed by the
        # caller's budget-exit diagnostics to distinguish "almost
        # converged, budget-starved" from "structurally stuck".
        self.last_best_progress: float = float("inf")
        self.last_best_state: CoupledState | None = None
        self.last_best_node: CoupledNode | None = None
        # Issue #4459: which backend served the most-recent search.  Defaults
        # to ``"python"`` here; ``_try_cpp_route_coupled`` overrides it to
        # ``"cpp"`` when the C++ joint-state search handles the pair.  The
        # ``[coupled-timing]`` diagnostic uses this to avoid reading the
        # C++-path ``last_best_state=None`` as "never moved".
        self.last_coupled_backend = "python"
        # Issue #3508: reset the per-search rejection counters.
        self.last_rejections = collections.defaultdict(int)

        # Issue #4065: stash the pads for the C++-path reconstruction helper
        # (it recovers width/net/name from the same Pad objects the Python
        # reconstruction uses, so routes are byte-identical).
        self._cpp_reconstruct_pads = (p_start, p_end, n_start, n_end)

        # Convert to grid coordinates
        p_start_gx, p_start_gy = self.grid.world_to_grid(p_start.x, p_start.y)
        p_end_gx, p_end_gy = self.grid.world_to_grid(p_end.x, p_end.y)
        n_start_gx, n_start_gy = self.grid.world_to_grid(n_start.x, n_start.y)
        n_end_gx, n_end_gy = self.grid.world_to_grid(n_end.x, n_end.y)

        # Determine start layer
        start_layer = self.grid.layer_to_index(p_start.layer.value)
        end_layer = self.grid.layer_to_index(p_end.layer.value)

        # Create start and goal states
        p_start_pos = GridPos(p_start_gx, p_start_gy, start_layer)
        n_start_pos = GridPos(n_start_gx, n_start_gy, start_layer)
        p_goal_pos = GridPos(p_end_gx, p_end_gy, end_layer)
        n_goal_pos = GridPos(n_end_gx, n_end_gy, end_layer)

        # Issue #2473: Derive the actual target spacing from the start
        # pad pair on the grid.  Real-world differential pairs (USB-C,
        # USB device-side connectors) often have pad spacing that
        # exceeds the manufacturer-minimum spacing configured on the
        # rules.  Using the configured spacing as a hard target prevents
        # the search from leaving the start state.  We honor the larger
        # of the configured spacing and the actual start-pad distance,
        # which keeps clearance valid while letting the coupled run
        # follow the natural pad pitch.
        #
        # Issue #2484: Keep this widened value as a per-call local
        # rather than mutating ``self.target_spacing_cells``.  The
        # previous implementation permanently widened the instance
        # attribute on the first wide-pad call and leaked the new
        # spacing into every subsequent ``route_coupled`` invocation
        # on the same pathfinder.
        actual_start_spacing = math.sqrt(
            (p_start_gx - n_start_gx) ** 2 + (p_start_gy - n_start_gy) ** 2
        )
        actual_end_spacing = math.sqrt((p_end_gx - n_end_gx) ** 2 + (p_end_gy - n_end_gy) ** 2)

        # Issue #3508: the mid-route spacing target is the CONFIGURED
        # coupled spacing, NOT the start pad pitch.  The legacy code
        # (#2473) widened ``effective_target_spacing`` to the start-pad
        # distance, which forced the pair to fly the ENTIRE route at
        # connector pitch (0.75-1.0 mm on board 06's FFC / USB-C
        # sources -- not electrically coupled at all) and then made the
        # endgame infeasible: a 16-20-cell-wide pair cannot thread the
        # dense pad field around the destination IC, and the
        # ``_heuristic`` spacing penalty (which uses the configured
        # ``self.target_spacing_cells``) actively fought the move
        # filter the whole way.  Instead, keep the configured target
        # and let the DEPARTURE phase below absorb the start-pitch
        # mismatch, exactly mirroring how the approach phase absorbs
        # the goal-pitch mismatch.
        effective_target_spacing = self.target_spacing_cells

        # Issue #2490: Size the approach radius to accommodate the
        # full pitch transition between the coupled target and the
        # goal pads.  The legacy ``max(target, 6)`` radius can be
        # smaller than the number of single-cell spacing reductions
        # required to converge, leaving the search no room to relax
        # spacing without exceeding the per-step tolerance.  Scale the
        # radius with the absolute spacing difference plus a small
        # buffer so each cell of the approach can change spacing by
        # at most one cell.
        end_spacing_delta = int(round(abs(actual_end_spacing - effective_target_spacing)))
        effective_approach_radius = max(effective_target_spacing, 6, end_spacing_delta * 2 + 4)

        # Issue #3508: departure radius -- the mirror of the approach
        # radius, sized by the start-pitch transition.  Within this
        # radius of the start pads the spacing tolerance is widened so
        # the pair can converge from the physical pad pitch down to
        # the coupled target one cell per step.
        start_spacing_delta = int(round(abs(actual_start_spacing - effective_target_spacing)))
        effective_departure_radius = max(effective_target_spacing, 6, start_spacing_delta * 2 + 4)

        # Issue #4065: try the C++ coupled joint-state A* first.  The C++
        # search consumes the SAME Grid3D the single-ended C++ pathfinder
        # uses and returns a joint grid-cell path we reconstruct below with
        # the UNCHANGED ``_build_route_from_path`` -- so C++ and Python
        # produce byte-identical Routes for the same joint path.  Preserved
        # as an optional accelerator: the pure-Python A* below is the
        # fallback, exercised when the backend is absent/stale, when the
        # v1-deferred features are requested (``allow_swap_via`` /
        # ``manhattan_sum``), or when ``_use_cpp_coupled`` is disabled.
        cpp_path = self._try_cpp_route_coupled(
            p_start_pos=p_start_pos,
            n_start_pos=n_start_pos,
            p_goal_pos=p_goal_pos,
            n_goal_pos=n_goal_pos,
            start_layer=start_layer,
            end_layer=end_layer,
            p_net=p_start.net,
            n_net=n_start.net,
            effective_target_spacing=effective_target_spacing,
            effective_approach_radius=effective_approach_radius,
            effective_departure_radius=effective_departure_radius,
            corridor=corridor,
            timeout_seconds=timeout_seconds,
            max_iterations_budget=max_iterations_budget,
        )
        if cpp_path is not None:
            handled, cpp_result = cpp_path
            if handled:
                # C++ owns this search (backend available + no deferred
                # feature).  ``cpp_result`` is the reconstructed
                # (p_route, n_route) tuple or None (search failed / budget
                # exit); either way we do NOT run the Python A* -- the
                # diagnostics on ``self`` were set by the wrapper.
                return cpp_result

        start_state = CoupledState(p_start_pos, n_start_pos, (0, 0))

        # Issue #3439: endpoint cells are exempt from the corridor
        # check -- the corridor builder includes them by construction,
        # but an under-dilated mask must never disqualify the search's
        # only landing cells.
        corridor_exempt: frozenset[tuple[int, int]] = frozenset(
            (
                (p_start_pos.x, p_start_pos.y),
                (p_goal_pos.x, p_goal_pos.y),
                (n_start_pos.x, n_start_pos.y),
                (n_goal_pos.x, n_goal_pos.y),
            )
        )

        # A* setup
        open_set: list[CoupledNode] = []
        closed_set: set[tuple[GridPos, GridPos]] = set()
        g_scores: dict[tuple[GridPos, GridPos], float] = {}

        # Issue #3144: monotonic insertion counter for deterministic
        # tie-breaking when ``f_score`` is equal between heap entries.
        # See ``CoupledNode`` docstring for the full rationale.  Using
        # ``itertools.count()`` keeps the hot path branch-free: every
        # ``CoupledNode`` constructor reads ``next(seq_counter)`` once.
        seq_counter = itertools.count()

        start_h = self.heuristic_weight * self._heuristic(start_state, p_goal_pos, n_goal_pos)
        # Issue #3508: LIFO tie-break (note the NEGATED counter).  The
        # #3144 fix introduced the monotonic counter for determinism
        # with FIFO semantics (oldest equal-f node pops first).  FIFO
        # explores f-score plateaus breadth-first: on a corridor-
        # bounded coupled search the plateau is the whole tube
        # cross-section x direction-history product, so the frontier
        # saturates laterally and the search burns its entire
        # iteration budget mid-tube (board 06: every pair, including
        # 90k-iteration corridor runs, exhausted budgets without
        # converging).  Popping the NEWEST equal-f node instead dives
        # depth-first along the most recently extended path -- on a
        # plateau this beelines toward the goal and only falls back
        # to sibling states when the dive hits an obstacle.  Equally
        # deterministic (the counter is still search-local and
        # monotonic); A* optimality is unaffected (tie-break order
        # among equal-f nodes never changes the returned path cost
        # with an admissible heuristic).
        start_node = CoupledNode(start_h, 0.0, start_state, seq=-next(seq_counter))
        heapq.heappush(open_set, start_node)
        g_scores[(p_start_pos, n_start_pos)] = 0.0

        max_iterations = self.grid.cols * self.grid.rows * 4
        iterations = 0

        # Issue #3089: wall-clock budget bookkeeping.  When
        # ``timeout_seconds`` is None, ``deadline`` stays None and the
        # branch below is skipped for every iteration.
        deadline: float | None = None
        if timeout_seconds is not None and timeout_seconds > 0:
            deadline = time.monotonic() + float(timeout_seconds)

        # Issue #3144: optional iteration budget for deterministic
        # budget classification.  ``None`` preserves wall-clock-only
        # behaviour; a positive int aborts the search the same way
        # the wall-clock branch does once ``iterations`` reaches it.
        iter_budget: int | None = (
            max_iterations_budget
            if max_iterations_budget is not None and max_iterations_budget > 0
            else None
        )

        while open_set and iterations < max_iterations:
            iterations += 1
            # Issue #3473: keep the public counter current on every
            # iteration so EVERY exit path (budget, timeout, goal,
            # exhausted open set, exceptions) reports the true cost.
            # A single attribute store is noise next to the heap and
            # parent-chain work in this loop body.
            self.last_iterations = iterations

            # Issue #3144: iteration budget classifier check.  Sits
            # adjacent to the wall-clock check so a single ``if`` body
            # owns the "abandon search and let caller dispatch
            # fallback" exit path.  Checked on every iteration (no
            # gating) because the check is a single integer compare.
            if iter_budget is not None and iterations >= iter_budget:
                logger.warning(
                    "CoupledPathfinder.route_coupled iteration budget "
                    "exceeded after %d iterations; abandoning "
                    "search (p_net=%r n_net=%r)",
                    iterations,
                    p_start.net_name,
                    n_start.net_name,
                )
                self.last_timeout_exceeded = True
                # Issue #3921: mark the ITERATION budget as the binding
                # constraint so the caller's diagnostic reports iteration
                # counts, not a misleading wall-clock-seconds figure.
                self.last_iteration_limited = True
                return None

            # Issue #3089: periodic wall-clock check.  Exits with ``None``
            # so the caller can fall through to its existing "coupled
            # routing failed" handler (which logs a structured message
            # and either tries independent routing or marks the pair as
            # skipped, depending on ``coupled_only``).
            if (
                deadline is not None
                and (iterations & (self._TIMEOUT_CHECK_INTERVAL - 1)) == 0
                and time.monotonic() >= deadline
            ):
                logger.warning(
                    "CoupledPathfinder.route_coupled wall-clock budget "
                    "exceeded after %.2fs (%d iterations); abandoning "
                    "search (p_net=%r n_net=%r)",
                    float(timeout_seconds),
                    iterations,
                    p_start.net_name,
                    n_start.net_name,
                )
                self.last_timeout_exceeded = True
                return None

            current = heapq.heappop(open_set)
            current_key = (current.state.p_pos, current.state.n_pos)

            if current_key in closed_set:
                continue
            closed_set.add(current_key)

            if _COUPLED_TRACE and iterations % 1000 == 0:
                print(
                    f"      [trace] it={iterations} f={current.f_score:.1f} "
                    f"g={current.g_score:.1f} open={len(open_set)} "
                    f"closed={len(closed_set)} p=({current.state.p_pos.x},"
                    f"{current.state.p_pos.y},{current.state.p_pos.layer}) "
                    f"n=({current.state.n_pos.x},{current.state.n_pos.y},"
                    f"{current.state.n_pos.layer})"
                )

            # Goal check - both traces must reach their goals
            p_at_goal = (
                current.state.p_pos.x == p_goal_pos.x and current.state.p_pos.y == p_goal_pos.y
            )
            n_at_goal = (
                current.state.n_pos.x == n_goal_pos.x and current.state.n_pos.y == n_goal_pos.y
            )

            if p_at_goal and n_at_goal:
                return self._reconstruct_coupled_routes(current, p_start, p_end, n_start, n_end)

            # Issue #3508: progress tracking for budget-exit diagnostics.
            # ``last_best_progress`` is the smallest joint remaining
            # distance any popped state achieved; a budget exit at high
            # remaining distance means the search is structurally stuck
            # (pinch point / frozen-offset corner), while a near-zero
            # value means it almost converged and a budget bump would
            # likely land it.
            _progress = max(
                abs(current.state.p_pos.x - p_goal_pos.x)
                + abs(current.state.p_pos.y - p_goal_pos.y),
                abs(current.state.n_pos.x - n_goal_pos.x)
                + abs(current.state.n_pos.y - n_goal_pos.y),
            )
            if _progress < self.last_best_progress:
                self.last_best_progress = _progress
                self.last_best_state = current.state
                self.last_best_node = current

            # Issue #3078: build path-history sets for the current
            # node by walking its parent chain.  These let
            # ``_get_coupled_neighbors`` reject moves that would put
            # one trace onto a cell the other (or it itself) has
            # already occupied -- the failure mode behind the
            # 36k-violation board 06 regression where asymmetric
            # moves let one trace loop around its partner.
            p_visited_cells: set[tuple[int, int, int]] = set()
            n_visited_cells: set[tuple[int, int, int]] = set()
            # Issue #3508: spatial buckets over the SAME trail cells for
            # the proximity guard (see ``_too_close_to_trail``).  Bucket
            # size = the proximity radius so any cell within the radius
            # of a candidate lives in one of the 3x3 neighbouring
            # buckets.
            prox_radius = self.min_spacing_cells
            p_trail_buckets: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
            n_trail_buckets: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
            bucket = max(1, prox_radius)
            walker: CoupledNode | None = current
            while walker is not None:
                p_cell = (walker.state.p_pos.x, walker.state.p_pos.y, walker.state.p_pos.layer)
                n_cell = (walker.state.n_pos.x, walker.state.n_pos.y, walker.state.n_pos.layer)
                p_visited_cells.add(p_cell)
                n_visited_cells.add(n_cell)
                if prox_radius > 1:
                    p_trail_buckets.setdefault(
                        (p_cell[0] // bucket, p_cell[1] // bucket), []
                    ).append(p_cell)
                    n_trail_buckets.setdefault(
                        (n_cell[0] // bucket, n_cell[1] // bucket), []
                    ).append(n_cell)
                walker = walker.parent
            # Endpoint pads are legitimate landing cells regardless of
            # history -- strip them so the check doesn't disqualify a
            # via at the source pad or a same-cell re-entry into the
            # goal pad.  (The neighbor-check helper also has an
            # endpoint exemption, but pre-filtering keeps the set
            # smaller and the intent more explicit.)
            for ep in (
                (p_start_pos.x, p_start_pos.y, p_start_pos.layer),
                (p_goal_pos.x, p_goal_pos.y, p_goal_pos.layer),
            ):
                p_visited_cells.discard(ep)
            for ep in (
                (n_start_pos.x, n_start_pos.y, n_start_pos.layer),
                (n_goal_pos.x, n_goal_pos.y, n_goal_pos.layer),
            ):
                n_visited_cells.discard(ep)
            p_visited_frozen = frozenset(p_visited_cells)
            n_visited_frozen = frozenset(n_visited_cells)

            # Explore neighbors
            for new_state, cost, is_via in self._get_coupled_neighbors(
                current.state,
                p_start.net,
                n_start.net,
                p_goal_pos,
                n_goal_pos,
                p_start_pos,
                n_start_pos,
                target_spacing_cells=effective_target_spacing,
                approach_radius_override=effective_approach_radius,
                departure_radius_override=effective_departure_radius,
                p_visited=p_visited_frozen,
                n_visited=n_visited_frozen,
                p_trail_buckets=p_trail_buckets,
                n_trail_buckets=n_trail_buckets,
            ):
                # Issue #3439: corridor-bounded search.  Prune any
                # state whose P or N head leaves the corridor mask
                # (endpoint cells exempt).  Layer is intentionally
                # ignored -- the corridor constrains the spatial tube,
                # not the layer choice.
                if corridor is not None:
                    p_xy = (new_state.p_pos.x, new_state.p_pos.y)
                    n_xy = (new_state.n_pos.x, new_state.n_pos.y)
                    if (p_xy not in corridor and p_xy not in corridor_exempt) or (
                        n_xy not in corridor and n_xy not in corridor_exempt
                    ):
                        self.last_rejections["corridor"] += 1
                        continue

                neighbor_key = (new_state.p_pos, new_state.n_pos)
                if neighbor_key in closed_set:
                    continue

                new_g = current.g_score + cost

                if neighbor_key not in g_scores or new_g < g_scores[neighbor_key]:
                    g_scores[neighbor_key] = new_g
                    h = self._heuristic(new_state, p_goal_pos, n_goal_pos)
                    # Issue #3508: weighted A* (see ``heuristic_weight``).
                    f = new_g + self.heuristic_weight * h

                    # Issue #3508: negated counter = LIFO tie-break on
                    # equal f -- see the start-node comment.
                    neighbor_node = CoupledNode(
                        f, new_g, new_state, current, is_via, seq=-next(seq_counter)
                    )
                    heapq.heappush(open_set, neighbor_node)

        # No path found
        return None

    def _reconstruct_coupled_routes(
        self,
        end_node: CoupledNode,
        p_start: Pad,
        p_end: Pad,
        n_start: Pad,
        n_end: Pad,
    ) -> tuple[Route, Route]:
        """Reconstruct both routes from A* result."""
        p_route = Route(net=p_start.net, net_name=p_start.net_name)
        n_route = Route(net=n_start.net, net_name=n_start.net_name)

        # Collect path points
        p_path: list[tuple[float, float, int, bool]] = []
        n_path: list[tuple[float, float, int, bool]] = []

        node: CoupledNode | None = end_node
        while node:
            p_wx, p_wy = self.grid.grid_to_world(node.state.p_pos.x, node.state.p_pos.y)
            n_wx, n_wy = self.grid.grid_to_world(node.state.n_pos.x, node.state.n_pos.y)

            p_path.append((p_wx, p_wy, node.state.p_pos.layer, node.via_from_parent))
            n_path.append((n_wx, n_wy, node.state.n_pos.layer, node.via_from_parent))

            node = node.parent

        p_path.reverse()
        n_path.reverse()

        # Convert to segments and vias for P trace
        self._build_route_from_path(p_route, p_path, p_start, p_end)

        # Convert to segments and vias for N trace
        self._build_route_from_path(n_route, n_path, n_start, n_end)

        # Issue #3078: order-of-magnitude segment-count asymmetry
        # invariant.  When the A* asymmetric P/N-advance moves let one
        # trace loop around the other (the failure mode behind the 36k
        # ``diffpair_clearance_intra`` regression on board 06), the
        # reconstructed routes show segment counts that differ by
        # 100x or more (USB3_RX1: 7 vs 1061 in the bug report).  The
        # path-history guard added in this issue is supposed to make
        # that impossible -- this log line is a runtime canary that
        # surfaces a regression in the guard itself.  We log at WARN
        # (not raise) so a defect in the guard during production
        # routing does NOT crash the whole pipeline; the post-route
        # Phase A audit will still detect the resulting clearance
        # violations.
        p_seg_count = len(p_route.segments)
        n_seg_count = len(n_route.segments)
        if p_seg_count > 0 and n_seg_count > 0:
            ratio = max(p_seg_count, n_seg_count) / min(p_seg_count, n_seg_count)
            if ratio > 10.0:
                logger.warning(
                    "coupled-route segment-count asymmetry "
                    "(possible self-intersection bug): "
                    "p_net=%r segs=%d, n_net=%r segs=%d, ratio=%.1fx",
                    p_start.net_name,
                    p_seg_count,
                    n_start.net_name,
                    n_seg_count,
                    ratio,
                )

        return p_route, n_route

    def _get_trace_width_for_net(self, net_name: str) -> float:
        """Get the trace width for a net based on its net class.

        Args:
            net_name: Name of the net

        Returns:
            Trace width in mm
        """
        if self.net_class_map and net_name in self.net_class_map:
            return self.net_class_map[net_name].trace_width
        return self.rules.trace_width

    def _build_route_from_path(
        self,
        route: Route,
        path: list[tuple[float, float, int, bool]],
        start_pad: Pad,
        end_pad: Pad,
    ) -> None:
        """Build route segments and vias from path points."""
        if len(path) < 2:
            return

        # Issue #1543: Use net-class-aware trace width
        trace_width = self._get_trace_width_for_net(start_pad.net_name)
        current_x, current_y = start_pad.x, start_pad.y
        current_layer_idx = self.grid.layer_to_index(start_pad.layer.value)

        for wx, wy, layer_idx, is_via in path:
            if is_via:
                # Add via
                via = Via(
                    x=current_x,
                    y=current_y,
                    drill=self.rules.via_drill,
                    diameter=self.rules.via_diameter,
                    layers=(
                        Layer(self.grid.index_to_layer(current_layer_idx)),
                        Layer(self.grid.index_to_layer(layer_idx)),
                    ),
                    net=start_pad.net,
                    net_name=start_pad.net_name,
                )
                route.vias.append(via)
                current_layer_idx = layer_idx
            else:
                # Add segment if we've moved
                if abs(wx - current_x) > 0.01 or abs(wy - current_y) > 0.01:
                    seg = Segment(
                        x1=current_x,
                        y1=current_y,
                        x2=wx,
                        y2=wy,
                        width=trace_width,
                        layer=Layer(self.grid.index_to_layer(layer_idx)),
                        net=start_pad.net,
                        net_name=start_pad.net_name,
                    )
                    route.segments.append(seg)
                    current_x, current_y = wx, wy
                    current_layer_idx = layer_idx

        # Final segment to end pad
        if abs(end_pad.x - current_x) > 0.01 or abs(end_pad.y - current_y) > 0.01:
            seg = Segment(
                x1=current_x,
                y1=current_y,
                x2=end_pad.x,
                y2=end_pad.y,
                width=trace_width,
                layer=Layer(self.grid.index_to_layer(current_layer_idx)),
                net=start_pad.net,
                net_name=start_pad.net_name,
            )
            route.segments.append(seg)


def create_serpentine(
    route: Route,
    length_to_add: float,
    min_amplitude: float = 0.3,
    min_segment_length: float = 1.0,
    partner_route: Route | None = None,
    intra_pair_clearance_mm: float | None = None,
    grid: RoutingGrid | None = None,
) -> bool:
    """Add serpentine meander to a route to increase its length.

    Finds a suitable straight segment and replaces it with a serpentine
    pattern to add the required length.

    When ``partner_route`` and ``intra_pair_clearance_mm`` are both
    provided, the serpentine bulges AWAY from the partner trace (using
    the same ``_outer_normal_hint`` logic as the audited Phase 3I
    tuner) and is rejected via a DRC self-check (mirrors
    ``_post_insertion_clearance_ok``) before being committed to the
    route.  This prevents the inline shim from introducing
    ``diffpair_clearance_intra`` violations on tightly-spaced pairs
    (Issue #3003).

    Args:
        route: The route to modify
        length_to_add: Additional length needed in mm
        min_amplitude: Minimum serpentine amplitude in mm
        min_segment_length: Minimum segment length for serpentine in mm
        partner_route: Optional partner trace; when provided alongside
            ``intra_pair_clearance_mm`` the bulge direction is chosen
            away from the partner and a clearance check is run before
            committing.
        intra_pair_clearance_mm: Optional edge-to-edge clearance floor
            in mm.  Required for the clearance-aware path; ignored when
            ``partner_route`` is ``None``.
        grid: Issue #3508: optional routing grid.  When provided, every
            proposed serpentine segment is rasterised against the grid's
            clearance envelope and the serpentine is REJECTED if any
            covered cell is blocked for a foreign net.  The partner
            self-check below only protects against the partner's
            SEGMENTS; the grid check additionally protects against the
            partner's vias, other nets' committed copper, and pad
            clearance halos -- the measured failure mode on board 06 was
            one-sided serpentine combs landing on the partner's shadow
            vias (8 ``clearance_segment_via`` at -0.038 mm) and grazing
            neighbour pads (Issue #3508 first re-route attempt).

    Returns:
        True if serpentine was added, False if no suitable segment
        found OR the proposed bulge would violate the partner clearance.
    """
    if length_to_add <= 0:
        return False

    # Find the longest straight horizontal or vertical segment
    best_segment = None
    best_segment_idx = -1
    best_length = 0.0

    for i, seg in enumerate(route.segments):
        seg_dx = seg.x2 - seg.x1
        seg_dy = seg.y2 - seg.y1
        seg_length = math.sqrt(seg_dx * seg_dx + seg_dy * seg_dy)

        # Only consider segments long enough for serpentine
        if seg_length < min_segment_length:
            continue

        # Prefer horizontal or vertical segments
        is_horizontal = abs(seg_dy) < 0.01
        is_vertical = abs(seg_dx) < 0.01

        if (is_horizontal or is_vertical) and seg_length > best_length:
            best_length = seg_length
            best_segment = seg
            best_segment_idx = i

    if best_segment is None:
        return False

    # Calculate serpentine parameters.
    #
    # Issue #3508: the bulges this function emits are TRIANGULAR (two
    # diagonal segments out to the bulge apex and back), so the length
    # a bend ADDS over the straight step it replaces is
    #
    #     added_per_bend = 2 * hypot(step/2, amplitude) - step
    #
    # The legacy ``amplitude = length_to_add / (2 * num_bends)``
    # assumed SQUARE bulges (added = 2 * amplitude per bend) and
    # under-delivered by up to an order of magnitude on long steps
    # (e.g. step 4 mm, amplitude 0.5 mm adds 0.124 mm/bend, not
    # 1.0 mm/bend) -- the shim then "succeeded" while leaving the pair
    # skewed.  Invert the triangular formula instead, and scale the
    # bend count with the available segment so amplitudes stay small.
    seg_len_initial = math.hypot(
        best_segment.x2 - best_segment.x1, best_segment.y2 - best_segment.y1
    )
    num_bends = max(2, min(12, int(seg_len_initial / 1.0)))
    step_est = seg_len_initial / (num_bends + 1)
    added_per_bend = length_to_add / num_bends
    amplitude = max(
        min_amplitude,
        math.sqrt(max(0.0, ((added_per_bend + step_est) / 2.0) ** 2 - (step_est / 2.0) ** 2)),
    )

    # Determine serpentine direction (perpendicular to segment)
    seg_dx = best_segment.x2 - best_segment.x1
    seg_dy = best_segment.y2 - best_segment.y1
    seg_length = math.sqrt(seg_dx * seg_dx + seg_dy * seg_dy)

    # Normalize direction
    dir_x = seg_dx / seg_length
    dir_y = seg_dy / seg_length

    # Perpendicular direction for serpentine waves
    perp_x = -dir_y
    perp_y = dir_x

    # Issue #3003: when a partner trace is available, bias ``current_side``
    # so the bulge points AWAY from the partner.  The default of +1 (the
    # hardcoded pre-#3003 value) bulges blindly toward whichever side the
    # perpendicular happens to face, which on a tight diff pair lands the
    # serpentine right on top of the partner.  We reuse the same outer-
    # normal heuristic as the audited Phase 3I tuner
    # (``_outer_normal_hint`` in ``diffpair_length_tuning``): dot the
    # perpendicular against the unit vector from the partner's closest
    # point to the insertion segment's midpoint.  A positive dot means
    # +1 already points outward; a negative dot means we must start at
    # -1 to bulge outward.
    initial_side = 1
    if partner_route is not None and partner_route.segments:
        from .diffpair_length_tuning import _outer_normal_hint

        hint_x, hint_y = _outer_normal_hint(best_segment, partner_route)
        # Dot the (segment-frame) perpendicular against the outer normal.
        # Use the side whose perpendicular projection is non-negative.
        if perp_x * hint_x + perp_y * hint_y < 0.0:
            initial_side = -1

    # Create serpentine segments
    new_segments: list[Segment] = []
    step_length = seg_length / (num_bends + 1)

    current_x = best_segment.x1
    current_y = best_segment.y1
    current_side = initial_side  # Alternates between +1 and -1

    for bend in range(num_bends + 1):
        # Move to next point along the segment direction
        next_x = best_segment.x1 + dir_x * step_length * (bend + 1)
        next_y = best_segment.y1 + dir_y * step_length * (bend + 1)

        if bend < num_bends:
            # Add serpentine bulge
            bulge_x = current_x + dir_x * step_length / 2 + perp_x * amplitude * current_side
            bulge_y = current_y + dir_y * step_length / 2 + perp_y * amplitude * current_side

            # Segment to bulge
            new_segments.append(
                Segment(
                    x1=current_x,
                    y1=current_y,
                    x2=bulge_x,
                    y2=bulge_y,
                    width=best_segment.width,
                    layer=best_segment.layer,
                    net=best_segment.net,
                    net_name=best_segment.net_name,
                )
            )

            # Segment from bulge to next point
            new_segments.append(
                Segment(
                    x1=bulge_x,
                    y1=bulge_y,
                    x2=next_x,
                    y2=next_y,
                    width=best_segment.width,
                    layer=best_segment.layer,
                    net=best_segment.net,
                    net_name=best_segment.net_name,
                )
            )

            # Issue #3508: keep ALL bulges on the outward side when a
            # partner constraint is in play.  The legacy alternating
            # serpentine sends every other bulge TOWARD the partner;
            # for a coupled pair at the intentionally-tight intra gap
            # (0.075-0.1 mm edge-to-edge) any inward bulge violates the
            # clearance self-check below, so the shim always failed on
            # exactly the pairs that need it most (board 06 shadow
            # pairs, 2.5-4.5 mm trim/tail skew).  A one-sided comb adds
            # the same length per bend without ever approaching the
            # partner.
            if partner_route is None or intra_pair_clearance_mm is None:
                current_side *= -1  # Flip side for next bend
        else:
            # Final segment to end point
            new_segments.append(
                Segment(
                    x1=current_x,
                    y1=current_y,
                    x2=best_segment.x2,
                    y2=best_segment.y2,
                    width=best_segment.width,
                    layer=best_segment.layer,
                    net=best_segment.net,
                    net_name=best_segment.net_name,
                )
            )

        current_x = next_x
        current_y = next_y

    # Issue #3003: DRC self-check before committing.  If the caller
    # supplied a partner route and an intra_pair_clearance threshold,
    # verify the new bulges do not violate that threshold against the
    # partner.  On rejection, leave the route untouched and report
    # failure -- the caller (match_pair_lengths -> route_differential_
    # pair_coupled) will fall through to the length-warning path, which
    # is a valid output (matches the no-suitable-segment branch).
    if partner_route is not None and intra_pair_clearance_mm is not None:
        from kicad_tools.core.geometry import segment_clearance

        for new_seg in new_segments:
            for pseg in partner_route.segments:
                if pseg.layer != new_seg.layer:
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

    # Issue #3508: grid self-check.  Rasterise every proposed segment
    # against the grid's clearance envelope (the same convention as
    # ``CoupledPathfinder._is_trace_blocked``: the grid already encodes
    # the full centerline clearance envelope at marking time, so a cell
    # is legal exactly when a trace centerline there satisfies
    # clearance).  Own-net cells are passable; anything else (partner
    # copper INCLUDING vias, other nets, pad halos, keepouts) rejects
    # the serpentine -- leaving the pair with a length-mismatch warning
    # is strictly better than committing clearance violations.
    if grid is not None:
        for new_seg in new_segments:
            li = grid.layer_to_index(new_seg.layer.value)
            sgx1, sgy1 = grid.world_to_grid(new_seg.x1, new_seg.y1)
            sgx2, sgy2 = grid.world_to_grid(new_seg.x2, new_seg.y2)
            steps = max(abs(sgx2 - sgx1), abs(sgy2 - sgy1))
            for i in range(steps + 1):
                t = i / steps if steps else 0.0
                gx = int(round(sgx1 + (sgx2 - sgx1) * t))
                gy = int(round(sgy1 + (sgy2 - sgy1) * t))
                if not (0 <= gx < grid.cols and 0 <= gy < grid.rows):
                    return False
                cell = grid.grid[li][gy][gx]
                if cell.blocked and cell.net != route.net:
                    return False

    # Replace the original segment with serpentine segments
    route.segments = (
        route.segments[:best_segment_idx] + new_segments + route.segments[best_segment_idx + 1 :]
    )

    return True


def match_pair_lengths(
    p_route: Route,
    n_route: Route,
    max_delta: float,
    add_serpentines: bool = True,
    intra_pair_clearance_mm: float | None = None,
    grid: RoutingGrid | None = None,
) -> bool:
    """Match lengths of differential pair traces.

    Adds serpentine meander to the shorter trace to match lengths.

    Issue #3003: when ``intra_pair_clearance_mm`` is provided, the
    serpentine generator runs the clearance-aware path
    (bulge-away-from-partner + DRC self-check).  When omitted, the
    legacy unconditional bulge path is preserved for backward
    compatibility with callers that have no notion of intra-pair
    clearance.

    Args:
        p_route: Positive trace route
        n_route: Negative trace route
        max_delta: Maximum allowed length difference in mm
        add_serpentines: Whether to add serpentines (if False, just check)
        intra_pair_clearance_mm: Optional intra-pair clearance floor in
            mm; when provided the serpentine is bulged away from the
            partner and DRC-checked against it before commit.

    Returns:
        True if lengths are matched (within tolerance), False otherwise
        (either lengths still mismatched OR the proposed serpentine
        would violate the partner clearance and was rejected).
    """
    p_length = calculate_route_length([p_route])
    n_length = calculate_route_length([n_route])
    delta = abs(p_length - n_length)

    if delta <= max_delta:
        return True  # Already matched

    if not add_serpentines:
        return False  # Cannot match without serpentines

    # Add serpentine to shorter trace
    length_to_add = delta - max_delta * 0.5  # Leave some margin

    if p_length < n_length:
        return create_serpentine(
            p_route,
            length_to_add,
            partner_route=n_route if intra_pair_clearance_mm is not None else None,
            intra_pair_clearance_mm=intra_pair_clearance_mm,
            grid=grid,
        )
    else:
        return create_serpentine(
            n_route,
            length_to_add,
            partner_route=p_route if intra_pair_clearance_mm is not None else None,
            intra_pair_clearance_mm=intra_pair_clearance_mm,
            grid=grid,
        )


class DiffPairRouter:
    """Differential pair routing coordinator for the autorouter.

    Supports two routing modes:
    1. Coupled routing: Both traces routed simultaneously maintaining spacing
    2. Independent routing: Traces routed separately (fallback)
    """

    def __init__(self, autorouter: Autorouter):
        """Initialize differential pair router.

        Args:
            autorouter: Parent autorouter instance
        """
        self.autorouter = autorouter
        # Issue #3023 Phase A: rolling buffer of routed intra-pair
        # clearance violations detected during
        # ``route_differential_pair_coupled``.  Phase A is
        # observability-only; Phase B (separate PR) will consume this
        # list to drive a fine-grid repair sub-pass.
        self._intra_clearance_violations: list[IntraPairClearanceViolation] = []
        # Issue #4459 (Phase 1 of #4409): ground-truth instrumentation for the
        # coupled diff-pair search.  ``_last_shadow_decline_reason`` records the
        # LAST reason the geometric shadow constructor declined a side
        # ("overlap" -- self-check / physical P/N overlap, "blockage" --
        # no legal via site / mid-route obstacle / unreachable pad tail, or
        # "pad-clearance" -- constructed copper inside a foreign pad's
        # clearance halo, #4571, or "via-clearance" -- constructed copper
        # inside a foreign VIA BARREL's clearance halo, #4575);
        # reset per spec, set inside ``_shadow_route_pair``.
        # ``_last_coupled_pair_report`` holds the most-recent per-pair
        # taxonomy classification (a :class:`CoupledPairReport`).  Both are
        # diagnostic-only and never influence routing decisions.
        self._last_shadow_decline_reason: str | None = None
        # Issue #4460 (approach 2): world-coordinate midpoints where the most
        # recent ``_shadow_route_pair`` attempt's parallel offset overlapped
        # the guide (the self-check pinch points).  ``_shadow_with_guide_bias``
        # boosts the guide A*'s cost at these sites to steer the re-route away.
        self._last_shadow_overlap_locations: list[tuple[float, float]] = []
        self._last_coupled_pair_report: CoupledPairReport | None = None
        # Issue #4575: foreign-copper universe for the exact segment-vs-via /
        # via-vs-via clearance gate, armed for the duration of ONE
        # ``_shadow_route_pair`` call by ``_shadow_foreign_copper``.  ``None``
        # means the gate is DISARMED and every one of its screens is a no-op --
        # which is precisely the state on the shadow-construction-OFF path and
        # for the ordinary (non-shadow) ``_tail_route`` calls, so this change
        # cannot alter either.
        self._shadow_foreign_universe: _ShadowForeignCopper | None = None
        # Issue #4575 (observability, in the spirit of #4459): how many
        # CANDIDATES the foreign-via gate rejected during the most recent
        # ``_shadow_route_pair`` call.  A gate that only ever declines whole
        # sides trades DRC errors for coupling; this counter is the evidence
        # that the per-candidate screens are doing the repair instead.
        # Diagnostic only -- never read by a routing decision.
        self._shadow_via_gate_rejections: int = 0
        # Issue #4635: wall-clock seconds the #4580 crossover census spent
        # INSIDE the current spec's budgeted window, beyond what the
        # un-instrumented first-legal loop would have spent.  The census is
        # state-neutral (proven in #4580) but it is NOT budget-neutral: it runs
        # inside the window ``spec_t0`` opens, so without this credit every
        # census second is silently deducted from the downstream probe
        # deadlines.  Accumulated in ``_synthesize_crossing_tail``, reset per
        # spec beside ``spec_t0``, and subtracted from the elapsed term at each
        # deadline computation.  Exactly ``0.0`` whenever the census is off, so
        # the default path's timing arithmetic is bit-identical.
        self._census_elapsed_s: float = 0.0
        # Issue #4799: structured capture of the #4580 census.  One
        # ``CrossingTailCensusRecord`` per crossover the census scanned, in
        # scan order, for THIS router instance (the process-wide twin lives in
        # ``crosstail_census.CENSUS_COLLECTOR``, which is what the JSON report
        # is written from).  Appended to only when the census is on, and only
        # AFTER the #4635 credit has been stamped -- so, like the census's own
        # ``print`` calls, it is outside every budgeted window and cannot move
        # a deadline.  Never read by a routing decision.
        self._census_records: list[CrossingTailCensusRecord] = []
        # Issue #3089: True iff the most-recent call to
        # ``route_differential_pair_coupled`` returned because the
        # inner ``CoupledPathfinder.route_coupled`` exceeded its
        # wall-clock budget (``per_pair_timeout``).  Used by
        # ``route_all_with_diffpairs`` to distinguish a budget-exit
        # (where the pair's nets should be deferred to the main
        # strategy) from a genuine no-path-found exit (where the
        # caller's existing handling is unchanged).
        self._last_pair_budget_exit: bool = False
        # Issue #4095: names of the differential pairs whose coupled
        # search budget-exited (per-pair or aggregate) during the most
        # recent ``route_all_with_diffpairs`` call and were consequently
        # deferred to the single-ended main strategy.  Local
        # ``budget_exit_diff_nets`` bookkeeping drives fallback + the
        # #3270 net-priority promotion but is discarded at function exit;
        # this attribute surfaces the same information (by human-readable
        # pair name) so the CLI can warn the operator that
        # ``--differential-pairs`` fell back to single-ended routing --
        # which on bundle-dense boards can *regress* completion / DRC
        # vs. a plain single-ended route (board 07: 34 vs 13 DRC errors,
        # 22/31 vs 26/31 nets; epic #4049 closeout).  Reset at the start
        # of every ``route_all_with_diffpairs`` call so it reflects only
        # the latest invocation.
        self._last_budget_exit_pair_names: list[str] = []
        # Issue #4095 instrumentation: monotonic counters for a future
        # checkpoint-and-compare follow-up to key on.  ``coupled_attempted``
        # counts pairs that reached the coupled A* (engaged, within
        # budget); ``budget_exited`` counts pairs deferred to the main
        # strategy via the budget-exit path (per-pair or aggregate).
        self._last_coupled_attempted_count: int = 0
        self._last_budget_exit_count: int = 0
        # Issue #3508: opt-in gate for the geometric shadow
        # constructor (see
        # ``DifferentialPairConfig.enable_shadow_construction`` for
        # the full rationale and the board 06 run-4 integration
        # measurements that keep this defaulted OFF).  Set from the
        # config by ``route_all_with_diffpairs``; tests may set it
        # directly.
        self.enable_shadow_construction: bool = False

    def _collect_existing_drills(self) -> list[tuple[float, float, float]]:
        """Assemble a board-wide drill registry for the hole-to-hole guard.

        Issue #3855: returns ``(x, y, drill_diameter)`` for every drilled
        hole the diff-pair fan-out via must keep clear of:

        * every through-hole pad (any net) -- ``pad.through_hole`` with a
          positive ``pad.drill``;
        * every via already committed to ``self.autorouter.routes`` (any
          net), including fan-out vias placed by earlier crossovers.

        The list is consulted edge-to-edge by
        :func:`kicad_tools.router.via_clearance.drill_hole_to_hole_clear`.
        Cheap to assemble (the fan-out path already iterates pads/routes
        for other reasons) and rebuilt per crossover so vias placed by
        prior crossovers are visible.
        """
        drills: list[tuple[float, float, float]] = []
        for pad in self.autorouter.pads.values():
            if getattr(pad, "through_hole", False) and pad.drill > 0:
                drills.append((pad.x, pad.y, pad.drill))
        for route in self.autorouter.routes:
            for via in route.vias:
                if via.drill > 0:
                    drills.append((via.x, via.y, via.drill))
        return drills

    def _escape_channel_registry(self, exclude_nets: frozenset[int]) -> list[_EscapeChannel]:
        """Direct escape rays of pads whose net still has no copper (#4574).

        Assembled ONCE per crossover -- the same shape as
        :meth:`_collect_existing_drills` -- and then consulted per candidate
        via site, so the 225-pair candidate loop never re-scans the pad list.

        A net qualifies when it has at least two pads and no committed route
        in ``autorouter.routes``: it still has to get out of its pads, and any
        all-layer barrel dropped across that exit is a wall on every layer.
        The "has no committed route" test is deliberately the cheap one --
        :meth:`_net_is_connected` runs full connectivity validation per net,
        which is far too heavy here, and over-reporting a net as unrouted only
        adds a preference term, never a veto.

        Each pad's escape direction is taken toward the mean of the REST of
        its own net's pads: that is where the net has to go, it needs no
        footprint-body geometry, and it is sign-unambiguous (unlike a
        principal-axis normal, which cannot tell which side of a single pad
        row is the outside).  The channel length is bounded by
        ``_ESCAPE_CHANNEL_REACH_PITCHES`` pitches of the pad's own component,
        so the notion of "sealed" scales with the pad field it belongs to.

        Returns an empty list -- i.e. degrades to today's exact first-legal
        behaviour -- whenever the autorouter state this needs is unavailable
        (test doubles, the stub-edge caller's throwaway pathfinder, boards
        with no pitch information).
        """
        autorouter = self.autorouter
        pads = getattr(autorouter, "pads", None)
        nets = getattr(autorouter, "nets", None)
        if not pads or not nets:
            return []
        pitches = self._pad_component_pitches()
        if not pitches:
            return []
        routed_nets = {route.net for route in getattr(autorouter, "routes", None) or ()}
        channels: list[_EscapeChannel] = []
        # ``sorted`` keeps the registry -- and therefore every penalty sum --
        # independent of dict iteration order (AC-6 / #4536). This fixed one
        # source of board-06 full-regen nondeterminism, not all of it -- see
        # boards/06-diffpair-test/README.md "Measuring Changes" for the
        # deterministic shadow-phase measurement surface and the two
        # remaining stable downstream modes.
        for net_id in sorted(nets):
            if net_id in exclude_nets or net_id in routed_nets:
                continue
            members = [pads[key] for key in nets[net_id] if key in pads]
            if len(members) < 2:
                continue
            others = len(members) - 1
            sum_x = sum(pad.x for pad in members)
            sum_y = sum(pad.y for pad in members)
            for pad in members:
                pitch = pitches.get(pad.ref, 0.0)
                if pitch <= 0.0:
                    continue  # no pitch known -> no fine-pitch escape to model
                dx = (sum_x - pad.x) / others - pad.x
                dy = (sum_y - pad.y) / others - pad.y
                span = math.hypot(dx, dy)
                if span < 1e-9:
                    continue
                channels.append(
                    _EscapeChannel(
                        x=pad.x,
                        y=pad.y,
                        ux=dx / span,
                        uy=dy / span,
                        reach=min(span, _ESCAPE_CHANNEL_REACH_PITCHES * pitch),
                    )
                )
        return channels

    def _resolve_detection_inputs(
        self,
    ) -> tuple[dict | None, dict[str, str] | None, list | None]:
        """Pull layered-detection context off the autorouter.

        Returns the ``(net_class_routing, net_to_class, kicad_groups)``
        triple needed by :func:`_layered_detect_diff_pairs`.  Supports
        both attribute conventions:

        * ``net_class_routing`` + ``net_to_class`` -- preferred (set by
          callers that have built a class-name-keyed map).
        * ``net_class_map`` -- the autorouter's per-net-name map; when
          present, we synthesise a ``net_to_class`` map from it so the
          explicit declaration path can be consulted.

        Issue #2638 / Epic #2556 Phase 2E: the explicit-declaration
        path in ``_gather_explicit_pairs`` was previously dead for
        callers that only set ``autorouter.net_class_map`` (the common
        case).  Phase 2E plumbs the fallback through so the
        engagement-layer single-ended refusal -- which depends on
        explicit pairs being detected -- can fire.
        """
        net_class_routing = getattr(self.autorouter, "net_class_routing", None)
        net_to_class = getattr(self.autorouter, "net_to_class", None)
        kicad_groups = getattr(self.autorouter, "kicad_diff_pair_groups", None)

        if net_class_routing is None:
            net_class_map = getattr(self.autorouter, "net_class_map", None)
            if net_class_map:
                net_class_routing = net_class_map
                if net_to_class is None:
                    # Synthesise a net_name -> class_name map.  We use
                    # the NetClassRouting.name attribute so the
                    # class-name-keyed lookup in _gather_explicit_pairs
                    # can find each entry under a stable key.  Because
                    # multiple net names may map to the same
                    # NetClassRouting instance, we also register each
                    # NetClassRouting under its own .name so
                    # _gather_explicit_pairs' subsequent lookup
                    # ``net_class_routing.get(class_name)`` succeeds.
                    synth_routing: dict = dict(net_class_map)
                    synth_to_class: dict[str, str] = {}
                    for net_name, nc in net_class_map.items():
                        cls_name = nc.name
                        synth_to_class[net_name] = cls_name
                        synth_routing.setdefault(cls_name, nc)
                    net_class_routing = synth_routing
                    net_to_class = synth_to_class

        return net_class_routing, net_to_class, kicad_groups

    def detect_differential_pairs(self) -> list[DifferentialPair]:
        """Detect differential pairs from net names.

        Issue #2558, Epic #2556 Phase 1B: this delegates to the layered
        detector (``diffpair_detection.detect_diff_pairs``) which
        consults explicit ``NetClassRouting.diffpair_partner`` and
        KiCad-group declarations in priority order before falling back
        to suffix inference.

        Issue #2638, Phase 2E: the layered-detection inputs are now
        pulled from either explicit ``net_class_routing`` /
        ``net_to_class`` attributes OR the autorouter's
        ``net_class_map`` (see :meth:`_resolve_detection_inputs`), so
        explicit declarations are honoured for both attribute shapes.
        """
        net_class_routing, net_to_class, kicad_groups = self._resolve_detection_inputs()

        detected = _layered_detect_diff_pairs(
            self.autorouter.net_names,
            net_class_routing=net_class_routing,
            net_to_class=net_to_class,
            kicad_groups=kicad_groups,
        )
        return [d.pair for d in detected]

    def detect_differential_pairs_with_source(self) -> list[tuple[DifferentialPair, str]]:
        """Like :meth:`detect_differential_pairs`, but also report the
        detection source for each pair.

        Returns a list of ``(pair, source)`` tuples where ``source`` is
        one of ``"explicit"``, ``"kicad_group"``, ``"suffix"``.
        """
        net_class_routing, net_to_class, kicad_groups = self._resolve_detection_inputs()

        detected = _layered_detect_diff_pairs(
            self.autorouter.net_names,
            net_class_routing=net_class_routing,
            net_to_class=net_to_class,
            kicad_groups=kicad_groups,
        )
        return [(d.pair, d.source.value) for d in detected]

    def analyze_differential_pairs(self) -> dict[str, any]:
        """Analyze net names for differential pairs."""
        return analyze_differential_pairs(self.autorouter.net_names)

    def _resolve_engagement(self, pair: DifferentialPair) -> tuple[bool, str]:
        """Resolve whether ``pair`` should engage CoupledPathfinder.

        Issue #2638, Epic #2556 Phase 2E: thin wrapper that pulls
        net-class context off the autorouter via
        :meth:`_resolve_detection_inputs` and defers to
        :func:`should_engage_coupled`.

        Returns:
            ``(engaged, reason)`` from :func:`should_engage_coupled`.
        """
        # Issue #3508: refuse coupled routing when either net already
        # carries committed copper (e.g. the recipe pre-routed a
        # chronically-stranded single before the pre-phase).  Coupled
        # routing would lay a SECOND copy of the pre-routed side; the
        # main strategy is the right tool for whatever remains.
        p_id, n_id = pair.get_net_ids()
        existing_nets = {r.net for r in self.autorouter.routes}
        if p_id in existing_nets or n_id in existing_nets:
            return False, "pre-routed copper present on a pair net"
        net_class_routing, net_to_class, _ = self._resolve_detection_inputs()
        return should_engage_coupled(pair, net_class_routing, net_to_class)

    def _get_pair_pads(self, pair: DifferentialPair) -> tuple[list[Pad], list[Pad]] | None:
        """Get pads for P and N nets of a differential pair.

        Returns:
            Tuple of (p_pads, n_pads) or None if pads not found
        """
        p_net_id = pair.positive.net_id
        n_net_id = pair.negative.net_id

        if p_net_id not in self.autorouter.nets:
            return None
        if n_net_id not in self.autorouter.nets:
            return None

        p_pad_keys = self.autorouter.nets[p_net_id]
        n_pad_keys = self.autorouter.nets[n_net_id]

        if len(p_pad_keys) < 2 or len(n_pad_keys) < 2:
            return None

        p_pads = [self.autorouter.pads[k] for k in p_pad_keys]
        n_pads = [self.autorouter.pads[k] for k in n_pad_keys]

        return p_pads, n_pads

    def _pair_pads_for_coupled_routing(
        self, p_pads: list[Pad], n_pads: list[Pad]
    ) -> list[tuple[Pad, Pad, Pad, Pad]]:
        """Pair up P and N pads for coupled routing.

        Matches P/N pads that are closest together as start/end pairs.

        Issue #2473: For pairs with more than 2 pads per net, this is now
        a thin wrapper around :meth:`_pair_pads_for_coupled_routing_npad`,
        which returns ``CoupledSegmentSpec`` and ``StubEdgeSpec`` objects.
        Callers that only need 2-pad behavior continue to receive a list
        of plain 4-tuples for backward compatibility.

        Returns:
            List of (p_start, p_end, n_start, n_end) tuples for the
            coupled segments only.  Stub edges (intra-net hops) are
            available via :meth:`_pair_pads_for_coupled_routing_npad`.
        """
        if len(p_pads) < 2 or len(n_pads) < 2:
            # Need at least one pad on each side to form a pair.
            return []

        if len(p_pads) == 2 and len(n_pads) == 2:
            # Fast path for the common 2-pad case — preserves the
            # exact pre-#2473 ordering for the regression test fixture.
            p0, p1 = p_pads[0], p_pads[1]
            n0, n1 = n_pads[0], n_pads[1]

            d_p0_n0 = math.sqrt((p0.x - n0.x) ** 2 + (p0.y - n0.y) ** 2)
            d_p0_n1 = math.sqrt((p0.x - n1.x) ** 2 + (p0.y - n1.y) ** 2)

            if d_p0_n0 < d_p0_n1:
                return [(p0, p1, n0, n1)]
            else:
                return [(p0, p1, n1, n0)]

        # N-pad path: build coupled segments via MST-style pairing.
        coupled, _stubs = self._pair_pads_for_coupled_routing_npad(p_pads, n_pads)
        return [(c.p_start, c.p_end, c.n_start, c.n_end) for c in coupled]

    @staticmethod
    def _pad_distance(a: Pad, b: Pad) -> float:
        """Euclidean distance between two pads (ignoring layer)."""
        return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2)

    def _cluster_pads(self, pads: list[Pad], threshold: float) -> list[list[Pad]]:
        """Group pads into connected clusters by Euclidean proximity.

        Two pads are placed in the same cluster when their pad-center
        distance is below ``threshold`` (mm).  Used to identify "groups"
        of pads that share a side of the diff pair (e.g., the four
        USB-C pads on the connector are all within ~1 mm of each other,
        whereas the MCU pin is several mm away).
        """
        if not pads:
            return []

        # Union-find by index.
        parent = list(range(len(pads)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        for i in range(len(pads)):
            for j in range(i + 1, len(pads)):
                if self._pad_distance(pads[i], pads[j]) <= threshold:
                    union(i, j)

        groups: dict[int, list[Pad]] = {}
        for i, pad in enumerate(pads):
            r = find(i)
            groups.setdefault(r, []).append(pad)
        return list(groups.values())

    @staticmethod
    def _cluster_centroid(pads: list[Pad]) -> tuple[float, float]:
        """Return the centroid (mean position) of a pad cluster."""
        if not pads:
            return (0.0, 0.0)
        cx = sum(p.x for p in pads) / len(pads)
        cy = sum(p.y for p in pads) / len(pads)
        return (cx, cy)

    @staticmethod
    def _polarity_swap_between(p_start: Pad, n_start: Pad, p_end: Pad, n_end: Pad) -> bool:
        """Detect whether the orientation of the start pair is mirrored at the end.

        The differential pair ``(p, n)`` defines an oriented vector
        from N to P at each endpoint.  When the two oriented vectors
        point in opposite directions (dot product < 0), the pair must
        execute a coordinated layer-swap to maintain coupling.
        """
        sx = p_start.x - n_start.x
        sy = p_start.y - n_start.y
        ex = p_end.x - n_end.x
        ey = p_end.y - n_end.y
        dot = sx * ex + sy * ey
        return dot < 0.0

    def _pair_pads_for_coupled_routing_npad(
        self, p_pads: list[Pad], n_pads: list[Pad]
    ) -> tuple[list[CoupledSegmentSpec], list[StubEdgeSpec]]:
        """MST-based pad pairing for N-pad differential pairs.

        Issue #2473: When a differential-pair net has more than two
        pads (e.g., USB-C connectors paralleling top/bottom-side pads),
        this method:

        1. Clusters pads on each net by spatial proximity.  Pads
           within the same cluster are connected by short stub edges.
        2. Selects one "representative" pad per cluster (closest to
           the centroid of the corresponding cluster on the other net).
        3. Computes a minimum spanning tree over the representative
           pads' centroids to produce coupled segments connecting the
           clusters.

        Stub edges within a cluster are returned separately and
        routed independently after the coupled pass.

        Returns:
            Tuple of ``(coupled_segments, stub_edges)`` where each
            ``CoupledSegmentSpec`` is a side-to-side coupled run and
            each ``StubEdgeSpec`` is a single-net intra-cluster hop.
        """
        if len(p_pads) < 2 or len(n_pads) < 2:
            return [], []

        # Cluster threshold: pads within this distance share a "side".
        # USB-C A6 (y=105) and B6 (y=106) are 1 mm apart; the MCU pin is
        # 10+ mm away.  A 3 mm threshold cleanly separates them.
        cluster_threshold = 3.0

        p_clusters = self._cluster_pads(p_pads, cluster_threshold)
        n_clusters = self._cluster_pads(n_pads, cluster_threshold)

        # Each side must form the same number of clusters; otherwise
        # we cannot reliably pair them up and fall back to "treat
        # every pad as its own cluster" (still produces an MST).
        if len(p_clusters) != len(n_clusters):
            p_clusters = [[p] for p in p_pads]
            n_clusters = [[n] for n in n_pads]

        # Need at least two clusters per side to form a coupled run.
        if len(p_clusters) < 2 or len(n_clusters) < 2:
            return [], []

        # Compute centroids for matching clusters across nets.
        p_centroids = [self._cluster_centroid(c) for c in p_clusters]
        n_centroids = [self._cluster_centroid(c) for c in n_clusters]

        # Greedy match P-cluster -> nearest N-cluster centroid.  For
        # the test fixtures this is optimal (clusters are well
        # separated) and avoids the O(n!) cost of optimal assignment.
        n_assigned = [False] * len(n_clusters)
        cluster_pairs: list[tuple[list[Pad], list[Pad]]] = []
        for pi, (px, py) in enumerate(p_centroids):
            best_ni = -1
            best_dist = float("inf")
            for ni, (nx, ny) in enumerate(n_centroids):
                if n_assigned[ni]:
                    continue
                dist = math.sqrt((px - nx) ** 2 + (py - ny) ** 2)
                if dist < best_dist:
                    best_dist = dist
                    best_ni = ni
            if best_ni >= 0:
                n_assigned[best_ni] = True
                cluster_pairs.append((p_clusters[pi], n_clusters[best_ni]))

        # Within each matched cluster pair, pick the "representative"
        # P pad and N pad (closest pair across the two clusters) as the
        # endpoint of the coupled run.  Other pads in the cluster
        # become stub edges back to the representative.
        rep_pads: list[tuple[Pad, Pad]] = []  # (p_rep, n_rep)
        stub_edges: list[StubEdgeSpec] = []

        for p_cluster, n_cluster in cluster_pairs:
            best_pair: tuple[Pad, Pad] | None = None
            best_dist = float("inf")
            for p in p_cluster:
                for n in n_cluster:
                    d = self._pad_distance(p, n)
                    if d < best_dist:
                        best_dist = d
                        best_pair = (p, n)
            if best_pair is None:  # pragma: no cover — defensive
                continue
            p_rep, n_rep = best_pair
            rep_pads.append((p_rep, n_rep))

            for p in p_cluster:
                if p is not p_rep:
                    stub_edges.append(StubEdgeSpec(start=p_rep, end=p))
            for n in n_cluster:
                if n is not n_rep:
                    stub_edges.append(StubEdgeSpec(start=n_rep, end=n))

        if len(rep_pads) < 2:
            return [], stub_edges

        # MST over representative pad pairs.  Edge weight = sum of
        # P-trace and N-trace lengths for the coupled segment.  This
        # is the metric the test plan asks us to minimize ("greedy
        # nearest-neighbor pairing would lose").
        n_reps = len(rep_pads)
        edges: list[tuple[float, int, int]] = []
        for i in range(n_reps):
            for j in range(i + 1, n_reps):
                p_i, n_i = rep_pads[i]
                p_j, n_j = rep_pads[j]
                weight = self._pad_distance(p_i, p_j) + self._pad_distance(n_i, n_j)
                edges.append((weight, i, j))
        edges.sort(key=lambda e: e[0])

        # Kruskal's algorithm with union-find.
        parent = list(range(n_reps))

        def find(k: int) -> int:
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        coupled_segments: list[CoupledSegmentSpec] = []
        for weight, i, j in edges:
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            parent[ri] = rj
            p_i, n_i = rep_pads[i]
            p_j, n_j = rep_pads[j]
            polarity_swap = self._polarity_swap_between(p_i, n_i, p_j, n_j)
            coupled_segments.append(
                CoupledSegmentSpec(
                    p_start=p_i,
                    p_end=p_j,
                    n_start=n_i,
                    n_end=n_j,
                    polarity_swap=polarity_swap,
                )
            )
            if len(coupled_segments) == n_reps - 1:
                break

        return coupled_segments, stub_edges

    def _route_stub_edges(self, stubs: list[StubEdgeSpec]) -> list[Route]:
        """Route intra-net stub edges via the autorouter's pad-to-pad pathfinder.

        Issue #2473: Stub edges are short single-net hops between pads
        in the same cluster (e.g., USB-C A6 -> B6 within USB_D+).  They
        do not need coupled routing because they are not coupled with
        any other net — they are a short continuation of a single
        polarity that has already been routed via the coupled run.

        Routes that fail are silently dropped: this is best-effort
        completion of the stub, and the main strategy can still pick
        them up afterwards.
        """
        results: list[Route] = []
        for stub in stubs:
            try:
                route = self.autorouter.router.route(stub.start, stub.end)
            except Exception as exc:  # pragma: no cover — defensive
                print(f"    WARNING: stub route raised: {exc}")
                route = None

            if route is None:
                # Issue #3508: synthesized-tail fallback.  The per-net
                # ``route()`` machinery declines sub-millimetre hops
                # whose endpoints sit inside pad clearance halos (e.g.
                # USB-C A6 -> B6 within a coupled pair's net) -- but
                # when the pre-phase claims the net as routed, the
                # negotiated main strategy SKIPS it (#2464) and the
                # stub is never completed (measured: USB2_D+ incomplete
                # at 18/21 reach on the first #3508 re-route).  The
                # geometric tail constructor validates straight /
                # dogleg / U-shaped candidates cell-by-cell against the
                # grid, which handles exactly this pad-halo geometry.
                try:
                    grid = self.autorouter.grid
                    pf = CoupledPathfinder(grid, self.autorouter.rules, 1)
                    layer_idx = grid.layer_to_index(stub.start.layer.value)
                    route = self._synthesize_tail(pf, stub.start, stub.end, layer_idx)
                    if route is None:
                        # Planar candidates exhausted (USB-C A6 -> B6 is
                        # fully fenced by the neighbouring pin halos on
                        # the surface layer): try the two-via
                        # layer-change tail.  ``partner_segments=[]``
                        # because a stub has no coupled partner to keep
                        # clear of -- the grid validation still applies.
                        route = self._synthesize_crossing_tail(
                            pf, stub.start, stub.end, layer_idx, []
                        )
                except Exception as exc:  # pragma: no cover — defensive
                    print(f"    WARNING: stub tail synthesis raised: {exc}")
                    route = None
                if route is not None:
                    print(
                        f"    Stub edge {stub.start.net_name} "
                        f"{stub.start.ref}.{stub.start.pin} -> "
                        f"{stub.end.ref}.{stub.end.pin} completed via "
                        f"synthesized tail (issue #3508)"
                    )

            if route is None:
                print(
                    f"    WARNING: stub edge {stub.start.net_name} "
                    f"{stub.start.ref}.{stub.start.pin} -> "
                    f"{stub.end.ref}.{stub.end.pin} failed (deferred to main strategy)"
                )
                continue

            # Use the autorouter's unified marking helper so both the
            # Python and C++ grids stay synchronized.
            self.autorouter._mark_route(route)
            self.autorouter.routes.append(route)
            results.append(route)
        return results

    def _remark_route_cells(self, route: Route) -> None:
        """Re-mark an ALREADY-COMMITTED route's cell envelope (issue #3508).

        Used after an in-place geometry mutation (the inline serpentine
        shim) on a route that ``autorouter._mark_route`` has already
        committed.  Cell marking is idempotent, so re-rasterising every
        segment simply adds the NEW copper's envelope; the
        non-idempotent bookkeeping (``grid.routes`` append, R-tree
        insertion) is intentionally NOT repeated because the route
        object is already registered.  The replaced straight chord's
        cells stay marked -- conservative (own-net) and harmless.

        Note the R-tree keeps the pre-mutation segment set; the grid
        CELLS are the collision source of truth for the negotiated
        router and ``GridCollisionChecker``, which is what the
        downstream passes use.
        """
        grid = self.autorouter.grid
        for seg in route.segments:
            total_clearance = seg.width / 2 + grid.rules.trace_clearance
            clearance_cells = int(total_clearance / grid.resolution) + 1
            # Mirror the #1666 grid-quantization safety margin used by
            # ``RoutingGrid.mark_route``.
            clearance_cells += 1
            grid._mark_segment(seg, clearance_cells=clearance_cells)
        self.autorouter._mark_route_on_cpp_grid(route)

    def _virtual_pad_at(self, template: Pad, wx: float, wy: float, layer_idx: int) -> Pad:
        """Virtual pad at an arbitrary board position (issue #3508).

        Used as the start/end anchor for synthesized tail routes and as
        the reconstruction end pad (so ``_build_route_from_path`` does
        not force a straight final jump onto the real pad).
        """
        grid = self.autorouter.grid
        return Pad(
            x=wx,
            y=wy,
            width=template.width,
            height=template.height,
            net=template.net,
            net_name=template.net_name,
            layer=Layer(grid.index_to_layer(layer_idx)),
            ref=template.ref,
            pin=template.pin,
        )

    def _segment_cells_clear(
        self,
        pathfinder: CoupledPathfinder,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer_idx: int,
        net: int,
    ) -> bool:
        """True when every grid cell under the segment is legal for ``net``.

        Issue #3508: the grid encodes the full centerline clearance
        envelope at obstacle-marking time (see ``_is_trace_blocked``),
        so a clear rasterisation implies a clearance-clear segment.
        """
        grid = self.autorouter.grid
        gx1, gy1 = grid.world_to_grid(x1, y1)
        gx2, gy2 = grid.world_to_grid(x2, y2)
        steps = max(abs(gx2 - gx1), abs(gy2 - gy1))
        for i in range(steps + 1):
            t = i / steps if steps else 0.0
            gx = int(round(gx1 + (gx2 - gx1) * t))
            gy = int(round(gy1 + (gy2 - gy1) * t))
            if pathfinder._is_cell_blocked(gx, gy, layer_idx, net):
                return False
        return True

    # ------------------------------------------------------------------
    # Issue #4571: exact foreign-pad clearance gate for constructed copper
    # ------------------------------------------------------------------

    def _pad_component_pitches(self) -> dict[str, float] | None:
        """Ref -> min pin pitch map for the exact pad-clearance primitives.

        Mirrors what ``Autorouter._demote_pad_clearance_violation_nets``
        feeds ``worst_segment_pad_deficit`` so the constructor's gate and
        the single-ended finalization backstop agree on the REQUIRED
        clearance for every component (fine-pitch relaxations included).
        """
        try:
            return self.autorouter.component_pitches
        except Exception:  # pragma: no cover - defensive (test doubles)
            return None

    def _span_pad_deficit(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer_idx: int,
        net: int,
        width: float,
    ) -> float:
        """Worst exact clearance deficit of a candidate span vs FOREIGN pads.

        Issue #4571.  ``> 0`` means the span would violate the
        ``clearance_pad_segment`` predicate against some pad that is not on
        ``net`` -- including the diff-pair PARTNER's pads, which are a
        foreign net and therefore fully checked.

        Deliberately called with ``exclude_net`` only and NO
        ``exclude_refs``: the #3545 same-component carve-out would otherwise
        exempt the partner's pad whenever P and N share a fine-pitch
        connector ref (the board-06 FFC case this gate exists for).
        """
        grid = self.autorouter.grid
        probe = Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=width,
            layer=Layer(grid.index_to_layer(layer_idx)),
            net=net,
            net_name="",
        )
        deficit, _loc = grid.worst_segment_pad_deficit(
            probe,
            exclude_net=net,
            component_pitches=self._pad_component_pitches(),
        )
        return deficit

    def _span_pad_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer_idx: int,
        net: int,
        width: float,
    ) -> bool:
        """True when a candidate span keeps foreign-pad clearance (#4571)."""
        return (
            self._span_pad_deficit(x1, y1, x2, y2, layer_idx, net, width) <= _SHADOW_PAD_DEFICIT_EPS
        )

    def _segment_pad_clear(self, seg: Segment) -> bool:
        """True when an assembled segment keeps foreign-pad clearance (#4571)."""
        grid = self.autorouter.grid
        return self._span_pad_clear(
            seg.x1,
            seg.y1,
            seg.x2,
            seg.y2,
            grid.layer_to_index(seg.layer.value),
            seg.net,
            seg.width,
        )

    def _via_pad_deficit(self, via: Via) -> float:
        """Worst exact clearance deficit of a via vs FOREIGN pads (#4571)."""
        deficit, _loc = self.autorouter.grid.worst_via_pad_deficit(
            via,
            exclude_net=via.net,
            component_pitches=self._pad_component_pitches(),
        )
        return deficit

    def _route_pad_violation(
        self,
        route: Route,
    ) -> tuple[float, tuple[float, float] | None]:
        """Worst foreign-pad clearance deficit over an assembled route (#4571).

        Returns ``(worst_deficit, worst_pad_location)``; ``worst_deficit <=
        _SHADOW_PAD_DEFICIT_EPS`` means the route is clean.
        """
        grid = self.autorouter.grid
        pitches = self._pad_component_pitches()
        worst = 0.0
        worst_loc: tuple[float, float] | None = None
        for seg in route.segments:
            deficit, loc = grid.worst_segment_pad_deficit(
                seg,
                exclude_net=seg.net,
                component_pitches=pitches,
            )
            if deficit > worst:
                worst, worst_loc = deficit, loc
        for via in route.vias:
            deficit, loc = grid.worst_via_pad_deficit(
                via,
                exclude_net=via.net,
                component_pitches=pitches,
            )
            if deficit > worst:
                worst, worst_loc = deficit, loc
        return worst, worst_loc

    # ------------------------------------------------------------------
    # Issue #4575: exact foreign-VIA clearance gate for constructed copper
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _shadow_foreign_copper(self, *partners: Route) -> Iterator[None]:
        """Arm the segment-vs-via clearance gate for one pair attempt (#4575).

        The gate is deliberately AMBIENT rather than threaded through the
        eight-deep tail-synthesis call chain (``_tail_route`` ->
        ``_synthesize_following_tail`` -> ``_bridge_to`` /
        ``_follow_partner_runs`` -> ``_synthesize_tail`` /
        ``_synthesize_crossing_tail`` -> ``_fallback_tail_route`` ->
        ``_planar_tail_probe``).  Two properties follow from that and both
        are load-bearing:

        * **No screen can be missed.**  Every candidate produced anywhere in
          that chain is measured, including the last-resort A* tail whose
          partner screen (``_tail_partner_clear`` with ``clearance=0.0``)
          checks barrels for physical OVERLAP only.
        * **It is inert everywhere else.**  Outside this context manager
          ``_shadow_foreign_universe`` is ``None`` and every screen below
          returns ``0.0``, so the ordinary (non-shadow) ``_tail_route``
          calls and the whole shadow-construction-OFF path are unchanged.

        The universe unions two sources:

        * every route already committed to ``autorouter.routes`` -- the
          AUTHORITATIVE list, deliberately NOT ``grid.routes``, which can
          hold a stale best-iteration geometry after a restore (see the #3486
          warning on ``RoutingGrid.worst_via_segment_deficit``); and
        * ``partners`` -- the pair's own sibling legs, which are never in the
          grid during shadow construction and are therefore invisible to
          every raster check.  The guide is registered here for the whole
          construction, and the post-assembly gate re-registers the FINAL
          guide geometry (the #4553 length matcher and the #4570 via mirror
          both mutate a leg after the first snapshot was taken).

        NOTHING is filtered out by net at snapshot time.  The
        ``exclude_net`` discipline is applied per ELEMENT at query time
        (``via.net == seg.net`` -> skip), which is the same
        ``exclude_net``-only rule #4571 uses and which
        :meth:`_collect_existing_drills` already applies to the drill
        registry -- and never ``exclude_refs``, whose #3545 same-component
        carve-out would exempt the partner's barrel on exactly the fine-pitch
        connectors this gate exists for.

        Measured scope note (board-06 shadow-ON seed 42): every candidate
        rejection this gate makes traces to the PARTNER leg -- a run with the
        committed half of the universe removed produces the identical
        per-pair rejection counts.  That is consistent with the board having
        no constructed-copper-vs-third-party-barrel finding at baseline, so
        the committed half is defence-in-depth for other boards rather than
        the thing that closes #4575.

        Nesting is supported (``_shadow_route_pair`` re-enters itself for the
        uncompressed-guide retry, and the post-assembly gate re-arms with the
        FINAL guide geometry) by save/restore.
        """
        prev = self._shadow_foreign_universe
        vias: list[Via] = []
        segments: list[Segment] = []
        for route in list(getattr(self.autorouter, "routes", None) or []):
            vias.extend(route.vias)
            segments.extend(route.segments)
        for partner in partners:
            vias.extend(partner.vias)
            segments.extend(partner.segments)
        self._shadow_foreign_universe = _ShadowForeignCopper(tuple(vias), tuple(segments))
        try:
            yield
        finally:
            self._shadow_foreign_universe = prev

    @contextlib.contextmanager
    def _shadow_foreign_copper_extended(self, *extra: Route) -> Iterator[None]:
        """Temporarily widen the armed universe with more copper (#4575).

        Used where the constructor mutates ONE leg while the OTHER leg has
        already been built -- the length matcher's meander teeth and the
        #4570 via-signature mirror.  At that point the sibling leg is
        constructed copper that exists nowhere in the ambient universe (it is
        neither committed nor the pre-assembly guide), so without this the
        tooth screens would be blind to the very barrels the pair just gained
        (measured on board-06 seed 42: a guide-leg meander tooth dipped to
        0.090 mm from the shadow leg's own landing via).

        A no-op when the gate is disarmed, so the shadow-OFF path is unchanged.
        """
        prev = self._shadow_foreign_universe
        if prev is None:
            yield
            return
        vias = list(prev.vias)
        segments = list(prev.segments)
        for route in extra:
            vias.extend(route.vias)
            segments.extend(route.segments)
        self._shadow_foreign_universe = _ShadowForeignCopper(tuple(vias), tuple(segments))
        try:
            yield
        finally:
            self._shadow_foreign_universe = prev

    @staticmethod
    def _seg_via_colocated(seg: Segment, via: Via) -> bool:
        """#2706 co-location carve-out, matching ``ClearanceRule`` (#4575).

        The in-pad-escape router places segment endpoints EXACTLY at via
        centres, and the DRC skips any segment/via pair whose segment
        endpoint is within ``_COLOCATION_EPSILON_MM`` of the via centre.
        Without the same carve-out this gate would be STRICTER than the
        checker and decline sides over geometry the checker never reports.
        """
        return (
            math.hypot(seg.x1 - via.x, seg.y1 - via.y) < _SHADOW_VIA_COLOCATION_EPS
            or math.hypot(seg.x2 - via.x, seg.y2 - via.y) < _SHADOW_VIA_COLOCATION_EPS
        )

    def _segment_via_deficit(self, seg: Segment) -> tuple[float, tuple[float, float] | None]:
        """Worst exact clearance deficit of a segment vs FOREIGN vias (#4575).

        ``> _SHADOW_VIA_DEFICIT_EPS`` means the segment would violate the
        ``clearance_segment_via`` predicate against some via that is not on
        ``seg.net`` -- INCLUDING the diff-pair partner's barrels, which the
        DRC checks at the full board minimum because its diff-pair exemption
        covers segment-to-SEGMENT edges only.

        Returns ``(0.0, None)`` when the gate is disarmed.
        """
        universe = self._shadow_foreign_universe
        if universe is None:
            return 0.0, None
        clearance = self.autorouter.rules.trace_clearance
        worst = 0.0
        worst_loc: tuple[float, float] | None = None
        for via in universe.vias:
            if via.net == seg.net:
                continue  # own-net copper may touch (a tail lands on it)
            if self._seg_via_colocated(seg, via):
                continue
            deficit = segment_via_deficit(seg, via, clearance)
            if deficit > worst:
                worst, worst_loc = deficit, (via.x, via.y)
        return worst, worst_loc

    def _span_via_deficit(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer_idx: int,
        net: int,
        width: float,
    ) -> float:
        """Worst foreign-via clearance deficit of a candidate span (#4575)."""
        if self._shadow_foreign_universe is None:
            return 0.0
        grid = self.autorouter.grid
        probe = Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=width,
            layer=Layer(grid.index_to_layer(layer_idx)),
            net=net,
            net_name="",
        )
        return self._segment_via_deficit(probe)[0]

    def _span_via_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer_idx: int,
        net: int,
        width: float,
    ) -> bool:
        """True when a candidate span keeps foreign-via clearance (#4575)."""
        ok = (
            self._span_via_deficit(x1, y1, x2, y2, layer_idx, net, width) <= _SHADOW_VIA_DEFICIT_EPS
        )
        if not ok:
            self._shadow_via_gate_rejections += 1
        return ok

    def _via_copper_deficit(self, via: Via) -> tuple[float, tuple[float, float] | None]:
        """Worst deficit of a via vs FOREIGN segments and vias (#4575).

        The mirror direction of :meth:`_segment_via_deficit`: a constructed
        barrel is copper on every layer it spans, so it must keep
        ``rules.via_clearance`` from foreign trace centrelines (the exact
        threshold ``RoutingGrid.worst_via_segment_deficit`` and
        ``via_clears_foreign_segment`` use) and from foreign barrels.
        """
        universe = self._shadow_foreign_universe
        if universe is None:
            return 0.0, None
        clearance = self.autorouter.rules.via_clearance
        worst = 0.0
        worst_loc: tuple[float, float] | None = None
        for seg in universe.segments:
            if seg.net == via.net:
                continue
            if self._seg_via_colocated(seg, via):
                continue
            deficit = segment_via_deficit(seg, via, clearance)
            if deficit > worst:
                worst, worst_loc = deficit, (via.x, via.y)
        v_lo = min(via.layers[0].value, via.layers[1].value)
        v_hi = max(via.layers[0].value, via.layers[1].value)
        for other in universe.vias:
            if other.net == via.net:
                continue
            o_lo = min(other.layers[0].value, other.layers[1].value)
            o_hi = max(other.layers[0].value, other.layers[1].value)
            if o_hi < v_lo or o_lo > v_hi:
                continue  # barrels never share a layer
            dist = math.hypot(via.x - other.x, via.y - other.y)
            deficit = via.diameter / 2 + other.diameter / 2 + clearance - dist
            if deficit > worst:
                worst, worst_loc = deficit, (other.x, other.y)
        return worst, worst_loc

    def _route_via_violation(
        self,
        route: Route,
    ) -> tuple[float, tuple[float, float] | None]:
        """Worst foreign-via clearance deficit over an assembled route (#4575).

        Sibling of :meth:`_route_pad_violation` for the via quadrant.  Both
        directions of the P/N interaction are covered by measuring the
        CONSTRUCTED leg only, because the partner is registered as foreign
        copper: the leg's segments are checked against the partner's barrels
        AND the leg's barrels against the partner's segments and barrels.

        Only the CONSTRUCTED leg is ever passed in.  The guide leg is not
        measured against third-party copper: it is ordinary single-ended
        router output that keeps the single-ended finalization backstops, so
        declining a side over a pre-existing guide graze would cost coupling
        without fixing anything the constructor introduced (the #4571
        rationale, one quadrant over).  Guide copper the CONSTRUCTOR added --
        the length matcher's meander teeth, the via mirror's z-jog -- is
        still covered, because it is screened as it is built (against a
        universe widened with the sibling leg) and because the post-assembly
        call re-registers the final guide as foreign copper.

        Returns ``(worst_deficit, worst_location)``; ``worst_deficit <=
        _SHADOW_VIA_DEFICIT_EPS`` means the route is clean.  Always
        ``(0.0, None)`` when the gate is disarmed.
        """
        if self._shadow_foreign_universe is None:
            return 0.0, None
        worst = 0.0
        worst_loc: tuple[float, float] | None = None
        for seg in route.segments:
            deficit, loc = self._segment_via_deficit(seg)
            if deficit > worst:
                worst, worst_loc = deficit, loc
        for via in route.vias:
            deficit, loc = self._via_copper_deficit(via)
            if deficit > worst:
                worst, worst_loc = deficit, loc
        return worst, worst_loc

    def _route_via_clear(self, route: Route | None) -> bool:
        """True when an assembled candidate keeps foreign-via clearance (#4575)."""
        ok = route is not None and self._route_via_violation(route)[0] <= _SHADOW_VIA_DEFICIT_EPS
        if not ok and route is not None:
            self._shadow_via_gate_rejections += 1
        return ok

    def _pad_deficit_arcs(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer_idx: int,
        net: int,
        width: float,
        arc0: float,
    ) -> list[float]:
        """Arc positions along a span whose copper violates a foreign pad.

        Issue #4571.  The blockage scan in :meth:`_shadow_route_pair`
        localises RASTER blockages by arc length so the existing end-trim
        machinery can shave a grazing landing (and only an INTERIOR blockage
        fails the side).  This is the exact-pad-clearance sibling: the whole
        span is probed first (one pass over the pad list) and only a span
        that actually violates is subdivided, so the common clean case costs
        a single call.
        """
        if self._span_pad_clear(x1, y1, x2, y2, layer_idx, net, width):
            return []
        span_len = math.hypot(x2 - x1, y2 - y1)
        n_chunks = max(1, int(math.ceil(span_len / _SHADOW_PAD_PROBE_STEP_MM)))
        arcs: list[float] = []
        for i in range(n_chunks):
            t0 = i / n_chunks
            t1 = (i + 1) / n_chunks
            cx1 = x1 + (x2 - x1) * t0
            cy1 = y1 + (y2 - y1) * t0
            cx2 = x1 + (x2 - x1) * t1
            cy2 = y1 + (y2 - y1) * t1
            if not self._span_pad_clear(cx1, cy1, cx2, cy2, layer_idx, net, width):
                arcs.append(arc0 + span_len * (t0 + t1) / 2.0)
        # A violation the chunking could not localise (e.g. a span shorter
        # than one chunk) still has to be reported, or the caller would treat
        # the span as clean.
        if not arcs:
            arcs.append(arc0 + span_len / 2.0)
        return arcs

    def _synthesize_tail(
        self,
        pathfinder: CoupledPathfinder,
        head: Pad,
        goal: Pad,
        layer_idx: int,
        partner_segments: list[Segment] | None = None,
        partner_clearance: float = 0.0,
    ) -> Route | None:
        """Geometric head->pad tail on the head's layer (issue #3508).

        The per-net ``route()`` machinery declines sub-millimetre hops
        whose endpoints sit inside pad clearance halos (measured: every
        board 06 rescue tail it was offered), so we draw the tail
        directly -- a straight segment, or an axis-aligned dogleg --
        and validate every covered grid cell with
        :meth:`_segment_cells_clear`.

        Issue #4460: when ``partner_segments`` is supplied the partner
        (diff-pair guide) copper is part of the CANDIDATE FILTER, not a
        post-hoc veto on the single winner.  The partner is not in the grid,
        so ``_segment_cells_clear`` cannot see it; screening only the first
        obstacle-clear candidate threw away the 20+ remaining U-detours
        whenever that one candidate happened to hug the guide, and the caller
        fell through to a partner-BLIND A* tail that ran on top of the guide
        (measured on board 06: every shadow ``self-check overlap`` decline
        traced to such a tail, centre-to-centre 0.018-0.035 mm).  Requiring
        ``partner_clearance`` centre-to-centre inside the loop makes the
        detour repertoire actually reachable.  With no partner supplied
        (``partner_clearance`` 0) the selection is unchanged.

        Issue #4572: the candidate list is also ORDERED by coupling.  Every
        gate above is a legality FLOOR -- none of them prefers a tail that
        runs alongside the partner, so the first legal candidate won even when
        a later one would have stayed inside the continuity rule's coupling
        window for its whole length.  Landing tails are 4-7 mm of board-06's
        12-53 mm constructed legs and cost BOTH legs (the tail is uncoupled on
        its own leg, and the partner copper it fails to follow is uncoupled on
        the other), so the enumeration is now extended with partner-parallel
        candidates and sorted by :func:`_spans_coupled_fraction` -- the exact
        fraction ``diffpair_routing_continuity`` would score.  The sort is
        STABLE and the legality gates below are untouched, so a tail region
        with no partner copper to follow (every score 0) keeps the historical
        shape order byte-for-byte.
        """
        grid = self.autorouter.grid
        goal_layer_idx = grid.layer_to_index(goal.layer.value)
        if goal_layer_idx != layer_idx:
            return None  # layer mismatch: leave to the A* fallback
        width = pathfinder._get_trace_width_for_net(head.net_name)
        layer = Layer(grid.index_to_layer(layer_idx))

        # Issue #4460: narrow the partner list ONCE to same-layer copper inside
        # the candidate envelope (the widest U-detour is +/-3.2 mm).  Guides
        # reach ~900 segments; screening 27 candidates against all of them
        # would dominate the constructor's runtime.
        near_partner: list[Segment] = []
        if partner_segments and partner_clearance > 0.0:
            reach = 3.2 + partner_clearance + grid.resolution
            lo_x, hi_x = min(head.x, goal.x) - reach, max(head.x, goal.x) + reach
            lo_y, hi_y = min(head.y, goal.y) - reach, max(head.y, goal.y) + reach
            near_partner = [
                ps
                for ps in partner_segments
                if ps.layer == layer
                and min(ps.x1, ps.x2) <= hi_x
                and max(ps.x1, ps.x2) >= lo_x
                and min(ps.y1, ps.y2) <= hi_y
                and max(ps.y1, ps.y2) >= lo_y
            ]

        candidates: list[list[tuple[float, float, float, float]]] = [
            [(head.x, head.y, goal.x, goal.y)],  # direct
            [  # dogleg via (goal.x, head.y)
                (head.x, head.y, goal.x, head.y),
                (goal.x, head.y, goal.x, goal.y),
            ],
            [  # dogleg via (head.x, goal.y)
                (head.x, head.y, head.x, goal.y),
                (head.x, goal.y, goal.x, goal.y),
            ],
        ]
        # Issue #3508: U-shaped detours.  Neighbour-pad halos often
        # block both straight doglegs (e.g. a partner pad sitting
        # between the shadow head and its goal pad on a connector pin
        # row); a small perpendicular excursion around the blocker is
        # routinely legal.
        # Issue #3508 (second pass): offsets extended to +/-3.2 mm so
        # intra-connector stubs (USB-C A6 -> B6) can wrap around the
        # full pin-row halo band; every candidate is still validated
        # cell-by-cell so larger detours are safe.
        for off in (0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.6, -1.6, 2.4, -2.4, 3.2, -3.2):
            wy = head.y + off
            candidates.append(
                [
                    (head.x, head.y, head.x, wy),
                    (head.x, wy, goal.x, wy),
                    (goal.x, wy, goal.x, goal.y),
                ]
            )
            wx = head.x + off
            candidates.append(
                [
                    (head.x, head.y, wx, head.y),
                    (wx, head.y, wx, goal.y),
                    (wx, goal.y, goal.x, goal.y),
                ]
            )
        # Issue #4572: extend the repertoire with U-detours whose long run is
        # placed at a COUPLED offset from a nearby partner run, then order the
        # whole list by the coupled fraction the continuity rule would score.
        # The shape lattice above is anchored on the HEAD (``head.y + off``),
        # so whether any of its rungs lands inside the 0.5 mm coupling window
        # of the guide is an accident of where the trim happened to end; these
        # candidates are anchored on the PARTNER instead, which is the thing
        # the rule measures against.
        if near_partner:
            candidates.extend(
                self._partner_parallel_tail_candidates(
                    head, goal, near_partner, width, partner_clearance
                )
            )
            scores = [_spans_coupled_fraction(c, width, layer, near_partner) for c in candidates]
            candidates = [
                candidates[i] for i in sorted(range(len(candidates)), key=lambda i: (-scores[i], i))
            ]
        for segs in candidates:
            if all(
                self._segment_cells_clear(pathfinder, x1, y1, x2, y2, layer_idx, head.net)
                for x1, y1, x2, y2 in segs
            ):
                # Issue #4460: the partner (guide) is not in the grid -- screen
                # it here so a candidate that hugs it loses to a later detour.
                if near_partner and any(
                    self._min_distance_to_partner(x1, y1, x2, y2, near_partner, layer)
                    < partner_clearance
                    for x1, y1, x2, y2 in segs
                ):
                    continue
                # Issue #4571: the grid raster's pad halo is shrunk in
                # fine-pitch corridors, so a cell-clear landing tail can still
                # sit inside (or on top of) a foreign pad -- and a diff-pair
                # net gets no downstream nudge repair.  Screen every candidate
                # with the exact DRC-equivalent predicate here, INSIDE the
                # loop, so the remaining detours stay reachable instead of the
                # constructor accepting the first raster-clear candidate and
                # shipping a short.
                if any(
                    not self._span_pad_clear(x1, y1, x2, y2, layer_idx, head.net, width)
                    for x1, y1, x2, y2 in segs
                ):
                    continue
                # Issue #4575: the partner universe this method screens against
                # is ``partner_segments`` -- SEGMENTS.  A landing tail drawn
                # 0.090 mm from the partner leg's via BARREL therefore passes
                # every check above and ships (board-06 seed 42, USB3_TX1).
                # Screen it here, inside the loop, so a grazing candidate loses
                # to a later detour instead of declining the side.
                if any(
                    not self._span_via_clear(x1, y1, x2, y2, layer_idx, head.net, width)
                    for x1, y1, x2, y2 in segs
                ):
                    continue
                route = Route(net=head.net, net_name=head.net_name)
                for x1, y1, x2, y2 in segs:
                    if abs(x2 - x1) < 0.01 and abs(y2 - y1) < 0.01:
                        continue
                    route.segments.append(
                        Segment(
                            x1=x1,
                            y1=y1,
                            x2=x2,
                            y2=y2,
                            width=width,
                            layer=layer,
                            net=head.net,
                            net_name=head.net_name,
                        )
                    )
                # Issue #4572: the sub-0.01 mm drop above is harmless at a PAD
                # landing (the pad's own copper absorbs it) but leaves a real
                # hole when the dropped span sat BETWEEN two kept ones -- and
                # the follow path splices this route between a lead-in and an
                # offset run, where such a hole strands the net outright.  The
                # coupling-preference order makes more of these candidates
                # reachable, so reject a broken one and let the next candidate
                # compete instead of shipping a break.
                if len(route.segments) > 1 and not self._route_is_chained(route):
                    continue
                if route.segments:
                    return route
        return None

    def _partner_parallel_tail_candidates(
        self,
        head: Pad,
        goal: Pad,
        near_partner: list[Segment],
        width: float,
        partner_clearance: float,
    ) -> list[list[tuple[float, float, float, float]]]:
        """U-detour tails whose long run parallels a nearby partner run (#4572).

        The historic shape lattice offsets the detour wall from the HEAD
        (``head.y + off`` for a fixed ladder of ``off``), so it only lands
        inside the continuity rule's coupling window by coincidence.  These
        candidates are anchored on the PARTNER: for every near-axis-aligned
        partner segment in the tail's neighbourhood, place the detour wall at
        a centre-to-centre spacing that is simultaneously

        * at or above ``partner_clearance`` (the intra-pair clearance floor
          the candidate loop already enforces -- these candidates are held to
          exactly the same legality gates as every other one), and
        * at or below ``_COUPLING_WINDOW_MM`` edge-to-edge (so the rule counts
          the run as coupled).

        A spacing ladder is emitted per partner run because the tightest rung
        couples hardest but is also the most likely to sit inside a pad halo.
        Returns candidates only; NOTHING here is emitted without passing the
        raster / partner-clearance / foreign-pad gates in
        :meth:`_synthesize_tail`.
        """
        out: list[list[tuple[float, float, float, float]]] = []
        seen: set[tuple[str, int]] = set()
        for ps in near_partner:
            ang = _span_angle_deg(ps.x1, ps.y1, ps.x2, ps.y2)
            horizontal = ang <= _COUPLING_PARALLEL_TOL_DEG or ang >= (
                180.0 - _COUPLING_PARALLEL_TOL_DEG
            )
            vertical = _angle_delta_deg(ang, 90.0) <= _COUPLING_PARALLEL_TOL_DEG
            if not horizontal and not vertical:
                continue
            half = (width + ps.width) / 2.0
            gap_ceiling = _COUPLING_WINDOW_MM + half
            base = (ps.y1 + ps.y2) / 2.0 if horizontal else (ps.x1 + ps.x2) / 2.0
            anchor = head.y if horizontal else head.x
            for rung in _TAIL_PARALLEL_GAP_RUNGS_MM:
                gap = partner_clearance + rung
                if gap > gap_ceiling:
                    break
                for sign in (1.0, -1.0):
                    wall = base + sign * gap
                    if abs(wall - anchor) > _TAIL_PARALLEL_MAX_EXCURSION_MM:
                        continue
                    key = ("h" if horizontal else "v", int(round(wall * 1000.0)))
                    if key in seen:
                        continue
                    seen.add(key)
                    if horizontal:
                        out.append(
                            [
                                (head.x, head.y, head.x, wall),
                                (head.x, wall, goal.x, wall),
                                (goal.x, wall, goal.x, goal.y),
                            ]
                        )
                    else:
                        out.append(
                            [
                                (head.x, head.y, wall, head.y),
                                (wall, head.y, wall, goal.y),
                                (wall, goal.y, goal.x, goal.y),
                            ]
                        )
                    if len(out) >= _TAIL_PARALLEL_MAX_CANDIDATES:
                        return out
        return out

    @staticmethod
    def _closest_on_polyline(px: float, py: float, segs: list[Segment]) -> tuple[int, float, float]:
        """``(segment index, parameter, distance)`` of the closest point (#4572)."""
        best_i, best_t, best_d = 0, 0.0, float("inf")
        for i, s in enumerate(segs):
            vx, vy = s.x2 - s.x1, s.y2 - s.y1
            den = vx * vx + vy * vy
            t = (
                0.0
                if den < 1e-12
                else max(0.0, min(1.0, ((px - s.x1) * vx + (py - s.y1) * vy) / den))
            )
            d = math.hypot(px - (s.x1 + t * vx), py - (s.y1 + t * vy))
            if d < best_d:
                best_i, best_t, best_d = i, t, d
        return best_i, best_t, best_d

    def _guide_follow_slice(
        self,
        head: Pad,
        goal: Pad,
        partner_segments: list[Segment],
        layer: Layer,
    ) -> list[tuple[float, float, float, float, float]]:
        """The partner's own run between the tail's head and its goal (#4572).

        ``partner_segments`` arrives in PATH order, so the guide copper between
        the point nearest ``head`` and the point nearest ``goal`` is a
        contiguous slice.  That slice is the shape a coupled tail should have:
        it is a legal route through the same pad field, it ends at the
        partner's own landing, and following it is exactly what makes BOTH
        legs' landing copper count as coupled.

        The cut is taken at the closest POINT (segment index plus parameter),
        not at segment boundaries -- #4553's guide compression leaves runs
        several millimetres long, so an index-only slice collapses to empty
        precisely on the compressed guides this is for.  Returned head-first as
        ``(x1, y1, x2, y2, width)`` spans; empty when there is nothing usable
        to follow.
        """
        same = [ps for ps in partner_segments if ps.layer == layer]
        if not same:
            return []
        ih, th, _dh = self._closest_on_polyline(head.x, head.y, same)
        ig, tg, _dg = self._closest_on_polyline(goal.x, goal.y, same)
        forward = (ih, th) <= (ig, tg)
        lo, lo_t, hi, hi_t = (ih, th, ig, tg) if forward else (ig, tg, ih, th)
        if hi - lo > _TAIL_FOLLOW_MAX_SPANS:
            return []
        out: list[tuple[float, float, float, float, float]] = []
        for i in range(lo, hi + 1):
            s = same[i]
            t0 = lo_t if i == lo else 0.0
            t1 = hi_t if i == hi else 1.0
            if t1 <= t0 + 1e-9:
                continue
            vx, vy = s.x2 - s.x1, s.y2 - s.y1
            out.append(
                (
                    s.x1 + vx * t0,
                    s.y1 + vy * t0,
                    s.x1 + vx * t1,
                    s.y1 + vy * t1,
                    s.width,
                )
            )
        if not out:
            return []
        if (
            sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2, _w in out)
            < _TAIL_FOLLOW_MIN_SLICE_MM
        ):
            return []
        if not forward:
            out = [(x2, y2, x1, y1, w) for x1, y1, x2, y2, w in reversed(out)]
        return out

    def _synthesize_following_tail(
        self,
        pathfinder: CoupledPathfinder,
        head: Pad,
        goal: Pad,
        layer_idx: int,
        partner_segments: list[Segment],
        partner_clearance: float,
        allow_expensive_landing: bool = False,
        prefer_planar: bool = False,
    ) -> Route | None:
        """Landing tail built as a parallel OFFSET of the partner's own run.

        Issue #4572.  ``_synthesize_tail``'s repertoire is axis-aligned
        (direct, two doglegs, U-detours), so in a dense pad field it routinely
        finds nothing and the caller falls through to the partner-BLIND A*
        probe -- which emits a per-cell staircase that shares no orientation
        or proximity with the partner (measured on board-06 shadow-ON seed 42:
        USB3_TX1's 4.87 mm and USB3_TX2's 5.21 mm landing tails, 84 and 92
        segments, 100% uncoupled on BOTH legs).

        The partner, however, already IS a legal path through that pad field
        and it ends at the pair's own landing.  Offsetting its slice between
        the tail's head and goal by a coupled centre-to-centre gap therefore
        produces a tail that is coupled by construction -- the same trick the
        shadow BODY is built with, applied to the trimmed ends the body gave
        up on.  It also re-couples the partner's landing copper, which is the
        other half of the ``(frac_a + frac_b) / 2`` score.

        Construction mirrors the BODY's own variable-gap offset
        (:meth:`_shadow_select_gap`): the spacing is chosen PER partner
        segment from a small ladder, so one walled-in stretch narrows or
        widens instead of killing the whole tail.  Where NO rung is legal the
        follow simply breaks, and the LONGEST surviving run is used -- that
        matters because the slice a landing tail gets is exactly the stretch
        the body already trimmed for being blocked, so its first millimetre is
        the single most likely part to be unusable.

        The run is then bracketed by the ordinary tail machinery: a lead-in
        from ``head`` to the run's start and a landing from its end onto the
        pad, both produced by :meth:`_synthesize_tail` (and, only when the
        caller was already going to pay for it, the crossing / A* fallbacks).
        So the coupled middle is new, while both hops keep every gate and
        detour the existing synthesizer has.

        Issue #4577: that lead-in is the measured blocker, not the budget.
        Instrumenting the candidate loop on board-06 shadow-ON seed 42 puts
        ``lead`` -- ``_bridge_to`` found no route to ``run[0]`` at all -- as the
        SOLE reason in 22 of the 38 numeric declines, and a participant in 27;
        "the slice was mostly unoffsettable" accounts for 4.  Two changes
        address it, both of which only ADD candidates:

        * the expensive lead-in probe now also reaches the longest run on the
          head's OWN side of the partner, because ordering purely by length
          routinely aims it at a run across the guide, where no legal lead-in
          can exist (a lead-in must keep ``partner_clearance``, so it cannot
          cross the partner's copper); and
        * the bridge point is negotiable -- :data:`_TAIL_FOLLOW_ENTRY_FRACTIONS`
          walks the entry forward along the run, giving up a prefix.  This is
          the mirror of the keep-fraction ladder, which is head-anchored and so
          can never move ``run[0]``.

        Every span is held to exactly the gates ``_synthesize_tail`` applies
        (raster cells, ``partner_clearance`` centre-to-centre, and #4573's
        exact foreign-pad predicate).  Returns ``None`` when no side / run /
        entry / truncation produces a legal tail, and the caller's existing
        fallback chain then runs unchanged; under ``KCT_SHADOW_DEBUG`` the
        decline line names WHICH bracketing stage rejected each candidate.
        """
        grid = self.autorouter.grid
        if grid.layer_to_index(goal.layer.value) != layer_idx:
            return None
        if not partner_segments or partner_clearance <= 0.0:
            return None
        width = pathfinder._get_trace_width_for_net(head.net_name)
        layer = Layer(grid.index_to_layer(layer_idx))
        slice_ = self._guide_follow_slice(head, goal, partner_segments, layer)
        if not slice_:
            if _SHADOW_DEBUG:
                print("    [coupled-follow] declined: no followable partner slice")
            return None
        # Bound the copper: a tail far longer than the direct hop is a detour,
        # not a landing, and would pay for its coupling in length skew.
        direct = math.hypot(goal.x - head.x, goal.y - head.y)
        budget = _TAIL_FOLLOW_LENGTH_FACTOR * direct + _TAIL_FOLLOW_LENGTH_SLACK_MM
        followed_best = 0.0

        candidates: list[list[tuple[float, float, float, float]]] = []
        sides: list[float] = []
        for side in (1.0, -1.0):
            runs = self._follow_partner_runs(
                pathfinder,
                slice_,
                side,
                layer_idx,
                layer,
                head.net,
                width,
                partner_segments,
                partner_clearance,
                budget,
            )
            for run in runs:
                followed_best = max(
                    followed_best,
                    sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in run),
                )
            for run in runs[:_TAIL_FOLLOW_MAX_RUNS_PER_SIDE]:
                candidates.append(run)
                sides.append(side)
        order = sorted(
            range(len(candidates)),
            key=lambda i: -sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in candidates[i]),
        )
        candidates = [candidates[i] for i in order]
        sides = [sides[i] for i in order]
        # Issue #4577: which SIDE of the partner the tail's head already sits
        # on.  ``_follow_partner_runs`` offsets the slice on both sides
        # blindly, and the longest surviving run is routinely the one on the
        # FAR side -- measured on board-06 seed 42, USB3_TX1: head
        # (149.055, 63.490), partner (148.772, 63.772), winning run start
        # (148.472, 64.073), i.e. the partner's own copper lies between the two.
        # A lead-in to that run must cross the guide, which every gate in
        # ``_synthesize_tail`` / ``_bridge_to`` forbids by construction, so the
        # expensive probe is spent on a bridge that cannot exist (32 of the 50
        # instrumented lead attempts returned no route at all, none of them a
        # chain break).  Knowing the head's side lets the expensive probe also
        # reach the best NEAR-side run.
        head_side = self._head_partner_side(head, slice_)
        near_rank = next(
            (i for i, s in enumerate(sides) if head_side != 0.0 and s == head_side),
            None,
        )
        # Issue #4577 (Gate 1): WHICH bracketing stage rejected each candidate.
        # The pre-#4577 decline line reported only the slice/followed/budget
        # arithmetic, which invited the wrong attribution -- the filed root
        # cause blamed the budget, while the measured blocker on 22 of 38
        # numeric board-06 declines is the LEAD-IN alone.  Counting the stages
        # makes the next lever choice evidence-driven instead of narrative.
        why: dict[str, int] = {}
        for rank, full_run in enumerate(candidates[: 2 * _TAIL_FOLLOW_MAX_RUNS_PER_SIDE]):
            full_len = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in full_run)
            # The slice a landing tail gets is the stretch the body trimmed for
            # being BLOCKED, so the usable offset run often starts past that
            # blockage -- which the axis-aligned lead-in then cannot reach
            # either.  Spend the expensive probes on the single longest run
            # only, and only when the caller was already going to pay for them
            # -- plus (issue #4577) on the longest run that at least sits on
            # the head's own side of the partner, since the longest run overall
            # is frequently across the guide and so unreachable by any legal
            # lead-in at all.
            expensive = allow_expensive_landing and rank in (0, near_rank)
            lead_found = False
            for entry_frac in _TAIL_FOLLOW_ENTRY_FRACTIONS:
                # Issue #4577: the bridge point is NEGOTIABLE.  ``_truncate_spans``
                # is a head-anchored prefix, so every rung of
                # ``_TAIL_FOLLOW_KEEP_FRACTIONS`` leaves ``run[0]`` exactly where
                # it was -- and ``run[0]`` is the one coordinate the lead-in is
                # failing on.  Walking the entry point forward along the run is
                # that ladder's mirror image: it gives up a PREFIX of the coupled
                # copper to buy a bridge the synthesizer can actually draw.
                run = (
                    full_run
                    if entry_frac <= 0.0
                    else self._suffix_spans(full_run, full_len * (1.0 - entry_frac))
                )
                run_len = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in run)
                if not run or run_len < _TAIL_FOLLOW_MIN_SLICE_MM:
                    break  # every later entry point is shorter still
                lead = self._bridge_to(
                    pathfinder,
                    head,
                    run[0][0],
                    run[0][1],
                    layer_idx,
                    partner_segments,
                    partner_clearance,
                    # Only the run's own start is worth an expensive probe: the
                    # stepped-in entries exist to find a bridge the CHEAP
                    # synthesizer can draw, so the worst-case probe count per
                    # candidate is unchanged.
                    allow_expensive=expensive and entry_frac <= 0.0,
                )
                if lead is None:
                    continue
                lead_found = True
                for keep_frac in _TAIL_FOLLOW_KEEP_FRACTIONS:
                    kept = self._truncate_spans(run, run_len * keep_frac)
                    if not kept:
                        why["truncate"] = why.get("truncate", 0) + 1
                        continue
                    kept_len = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in kept)
                    anchor = self._virtual_pad_at(goal, kept[-1][2], kept[-1][3], layer_idx)
                    landing = self._synthesize_tail(
                        pathfinder,
                        anchor,
                        goal,
                        layer_idx,
                        partner_segments=partner_segments,
                        partner_clearance=partner_clearance,
                    )
                    if landing is None and allow_expensive_landing:
                        landing = self._synthesize_crossing_tail(
                            pathfinder, anchor, goal, layer_idx, partner_segments
                        ) or self._fallback_tail_route(
                            anchor,
                            goal,
                            partner_segments,
                            partner_clearance,
                            # Issue #4570: the follow tail's own last-resort A*
                            # lander is a second via-inventing source; bias it
                            # the same way as the caller's.
                            prefer_planar_layer=layer_idx if prefer_planar else None,
                        )
                    if landing is None:
                        why["landing"] = why.get("landing", 0) + 1
                        continue
                    total = _route_copper_length(lead) + kept_len + _route_copper_length(landing)
                    if total > budget:
                        why["budget"] = why.get("budget", 0) + 1
                        continue
                    route = Route(net=head.net, net_name=head.net_name)
                    route.segments.extend(lead.segments)
                    route.vias.extend(lead.vias)
                    for x1, y1, x2, y2 in kept:
                        route.segments.append(
                            Segment(
                                x1=x1,
                                y1=y1,
                                x2=x2,
                                y2=y2,
                                width=width,
                                layer=layer,
                                net=head.net,
                                net_name=head.net_name,
                            )
                        )
                    route.segments.extend(landing.segments)
                    route.vias.extend(landing.vias)
                    # Issue #4572 (and the #4462 lesson): a three-piece tail is
                    # only copper if the three pieces MEET.  ``_synthesize_tail``
                    # silently drops a sub-0.01 mm span, which is harmless at a
                    # pad landing but leaves a real break when the piece is
                    # spliced between a lead-in and a follow run -- and the
                    # #3540 transactional strand guard then rips the whole pair
                    # (measured: PCIE_RX stranded at U3.32).  Verify the chain
                    # end-to-end and drop the candidate rather than emit a break.
                    if not route.segments or not self._route_is_chained(route, head, goal):
                        why["chain"] = why.get("chain", 0) + 1
                        continue
                    return route
            if not lead_found:
                # Issue #4577: count the lead-in blocker ONCE per candidate run,
                # not once per entry point, so the decline histogram stays
                # comparable across the entry ladder's introduction.
                why["lead"] = why.get("lead", 0) + 1
        if _SHADOW_DEBUG:
            slice_len = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2, _w in slice_)
            reasons = ",".join(f"{k}={why[k]}" for k in sorted(why)) or "no-surviving-run"
            print(
                f"    [coupled-follow] declined: slice={slice_len:.3f} "
                f"followed={followed_best:.3f} direct={direct:.3f} budget={budget:.3f} "
                f"runs={len(candidates)} why={reasons}"
            )
        return None

    @staticmethod
    def _route_is_chained(
        route: Route,
        head: Pad | None = None,
        goal: Pad | None = None,
        tol: float = _OFFSET_JOIN_COINCIDENT_MM,
    ) -> bool:
        """Do the route's segments form one unbroken chain? (issue #4572)

        ``head`` / ``goal``, when given, additionally pin the two ends.
        ``tol`` is the serialization quantum used everywhere else in the
        constructor (#4462): below it two endpoints round to the same 4-dp
        coordinate in the ``.kicad_pcb`` and are genuinely the same point;
        above it the emitted polyline has a hole and the net splits.
        """
        segs = route.segments
        if not segs:
            return False
        if head is not None and math.hypot(segs[0].x1 - head.x, segs[0].y1 - head.y) > tol:
            return False
        for a, b in zip(segs, segs[1:], strict=False):
            if math.hypot(b.x1 - a.x2, b.y1 - a.y2) > tol:
                return False
        return goal is None or math.hypot(segs[-1].x2 - goal.x, segs[-1].y2 - goal.y) <= tol

    def _bridge_to(
        self,
        pathfinder: CoupledPathfinder,
        head: Pad,
        tx: float,
        ty: float,
        layer_idx: int,
        partner_segments: list[Segment],
        partner_clearance: float,
        allow_expensive: bool = False,
    ) -> Route | None:
        """Short lead-in from ``head`` onto a follow run's start (#4572).

        Empty (but non-``None``) when the run already starts at ``head``.
        ``allow_expensive`` opens the same crossing / A* fallbacks the landing
        hop uses; the caller gates it so the worst-case probe count matches
        the pre-#4572 chain.
        """
        route = Route(net=head.net, net_name=head.net_name)
        if math.hypot(tx - head.x, ty - head.y) <= 1e-6:
            return route
        target = self._virtual_pad_at(head, tx, ty, layer_idx)
        lead = self._synthesize_tail(
            pathfinder,
            head,
            target,
            layer_idx,
            partner_segments=partner_segments,
            partner_clearance=partner_clearance,
        )
        if lead is None and allow_expensive:
            lead = self._synthesize_crossing_tail(
                pathfinder, head, target, layer_idx, partner_segments
            ) or self._fallback_tail_route(head, target, partner_segments, partner_clearance)
        # A lead-in is spliced BETWEEN the body anchor and the offset run, so
        # (unlike a pad landing) a hole at either end strands the net.
        if lead is not None and not self._route_is_chained(lead, head, target):
            return None
        return lead

    def _follow_partner_runs(
        self,
        pathfinder: CoupledPathfinder,
        slice_: list[tuple[float, float, float, float, float]],
        side: float,
        layer_idx: int,
        layer: Layer,
        net: int,
        width: float,
        partner_segments: list[Segment],
        partner_clearance: float,
        budget: float,
    ) -> list[list[tuple[float, float, float, float]]]:
        """Maximal legal offset runs along the partner slice, longest first.

        Issue #4572.  Per-segment spacing selection, exactly like
        :meth:`_shadow_select_gap` does for the body: the first ladder rung
        whose offset span clears the raster, the intra-pair clearance floor
        and the exact foreign-pad predicate wins.  A segment with no legal
        rung BREAKS the run rather than ending the search, because the slice
        handed to a landing tail is precisely the stretch the body trimmed for
        being blocked -- the usable copper is often past the blockage, not
        before it.
        """
        gap_ceiling = _COUPLING_WINDOW_MM + width
        runs: list[list[tuple[float, float, float, float]]] = []
        cur: list[tuple[float, float, float, float]] = []
        prev: tuple[float, float] | None = None
        used = 0.0
        for sx1, sy1, sx2, sy2, _w in slice_:
            ux, uy = sx2 - sx1, sy2 - sy1
            seg_len = math.hypot(ux, uy)
            if seg_len < 1e-9:
                continue
            chosen: list[tuple[float, float, float, float]] | None = None
            for rung in _TAIL_PARALLEL_GAP_RUNGS_MM:
                gap = partner_clearance + rung
                if gap > gap_ceiling:
                    break
                nx, ny = -uy / seg_len * side, ux / seg_len * side
                ax, ay = sx1 + gap * nx, sy1 + gap * ny
                bx, by = sx2 + gap * nx, sy2 + gap * ny
                cand: list[tuple[float, float, float, float]] = []
                if prev is not None and math.hypot(ax - prev[0], ay - prev[1]) > 1e-6:
                    cand.append((prev[0], prev[1], ax, ay))
                cand.append((ax, ay, bx, by))
                cand = [c for c in cand if math.hypot(c[2] - c[0], c[3] - c[1]) > 1e-6]
                if not cand:
                    continue
                if used + sum(math.hypot(c[2] - c[0], c[3] - c[1]) for c in cand) > budget:
                    continue
                if not self._spans_are_legal(
                    pathfinder,
                    cand,
                    layer_idx,
                    layer,
                    net,
                    width,
                    partner_segments,
                    partner_clearance + _TAIL_FOLLOW_CLEARANCE_MARGIN_MM,
                ):
                    continue
                chosen = cand
                break
            if chosen is None:
                if cur:
                    runs.append(cur)
                cur, prev, used = [], None, 0.0
                continue
            cur.extend(chosen)
            used += sum(math.hypot(c[2] - c[0], c[3] - c[1]) for c in chosen)
            prev = (chosen[-1][2], chosen[-1][3])
        if cur:
            runs.append(cur)
        runs.sort(key=lambda r: -sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in r))
        return [
            r
            for r in runs
            if sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in r)
            >= _TAIL_FOLLOW_MIN_SLICE_MM
        ]

    def _spans_are_legal(
        self,
        pathfinder: CoupledPathfinder,
        spans: list[tuple[float, float, float, float]],
        layer_idx: int,
        layer: Layer,
        net: int,
        width: float,
        partner_segments: list[Segment],
        partner_clearance: float,
    ) -> bool:
        """The four gates every synthesized tail span must pass (#4572).

        Deliberately the SAME four, in the same order, as
        :meth:`_synthesize_tail`'s candidate loop: grid raster, partner
        clearance floor (#4460), exact foreign-pad clearance (#4571/PR #4573)
        and exact foreign-via clearance (#4575).
        """
        for x1, y1, x2, y2 in spans:
            if not self._segment_cells_clear(pathfinder, x1, y1, x2, y2, layer_idx, net):
                return False
        for x1, y1, x2, y2 in spans:
            if (
                self._min_distance_to_partner(x1, y1, x2, y2, partner_segments, layer)
                < partner_clearance
            ):
                return False
        for x1, y1, x2, y2 in spans:
            if not self._span_pad_clear(x1, y1, x2, y2, layer_idx, net, width):
                return False
        for x1, y1, x2, y2 in spans:
            if not self._span_via_clear(x1, y1, x2, y2, layer_idx, net, width):
                return False
        return True

    @staticmethod
    def _truncate_spans(
        spans: list[tuple[float, float, float, float]],
        keep_len: float,
    ) -> list[tuple[float, float, float, float]]:
        """Head-anchored prefix of a span chain, at most ``keep_len`` long."""
        if keep_len <= 1e-9:
            return []
        out: list[tuple[float, float, float, float]] = []
        used = 0.0
        for x1, y1, x2, y2 in spans:
            seg_len = math.hypot(x2 - x1, y2 - y1)
            if seg_len < 1e-9:
                continue
            if used + seg_len <= keep_len + 1e-9:
                out.append((x1, y1, x2, y2))
                used += seg_len
                continue
            t = (keep_len - used) / seg_len
            if t > 1e-6:
                out.append((x1, y1, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
            break
        return out

    @staticmethod
    def _suffix_spans(
        spans: list[tuple[float, float, float, float]],
        keep_len: float,
    ) -> list[tuple[float, float, float, float]]:
        """Tail-anchored suffix of a span chain, at most ``keep_len`` long.

        Issue #4577.  The mirror of :meth:`_truncate_spans`: that one gives up
        the run's LAST stretch (walled-in by neighbour-pad halos), this one
        gives up its FIRST -- which is what a lead-in that cannot reach
        ``run[0]`` needs, since the run a landing tail gets typically starts on
        the far side of the very blockage that broke it.  The run's END is
        preserved, so the landing hop the caller then synthesizes is
        unaffected.
        """
        if keep_len <= 1e-9:
            return []
        out: list[tuple[float, float, float, float]] = []
        used = 0.0
        for x1, y1, x2, y2 in reversed(spans):
            seg_len = math.hypot(x2 - x1, y2 - y1)
            if seg_len < 1e-9:
                continue
            if used + seg_len <= keep_len + 1e-9:
                out.append((x1, y1, x2, y2))
                used += seg_len
                continue
            t = (keep_len - used) / seg_len
            if t > 1e-6:
                out.append((x2 - (x2 - x1) * t, y2 - (y2 - y1) * t, x2, y2))
            break
        out.reverse()
        return out

    @staticmethod
    def _head_partner_side(
        head: Pad,
        slice_: list[tuple[float, float, float, float, float]],
    ) -> float:
        """Which offset side of the partner slice the tail's head sits on (#4577).

        ``_guide_follow_slice`` cuts the slice AT the partner point closest to
        ``head``, so ``slice_[0]``'s start is that point and the head->point
        vector is (up to the cut's own error) the local normal.  Returns
        ``+1.0`` / ``-1.0`` in ``_follow_partner_runs``' own side convention
        (``n = (-uy, ux) / |u| * side``), or ``0.0`` when the head is on the
        partner's centreline and neither side is nearer -- in which case the
        caller must not express a preference.
        """
        if not slice_:
            return 0.0
        x1, y1, x2, y2, _w = slice_[0]
        ux, uy = x2 - x1, y2 - y1
        norm = math.hypot(ux, uy)
        if norm < 1e-9:
            return 0.0
        proj = (head.x - x1) * (-uy / norm) + (head.y - y1) * (ux / norm)
        if abs(proj) < 1e-9:
            return 0.0
        return 1.0 if proj > 0.0 else -1.0

    def _pair_seg_clearance(self, pathfinder: CoupledPathfinder, net_name: str) -> float:
        """Centerline distance bound between pair partners (issue #3508).

        Same-layer P/N copper must keep ``width/2 + intra_pair_clearance
        + width/2`` of centerline separation -- the intra-pair bound the
        diffpair DRC family checks, NOT the inter-net manufacturer
        clearance (using the latter rejects legitimately-coupled
        geometry: the coupled gap is intentionally tighter).
        """
        net_class_map = getattr(self.autorouter, "net_class_map", None) or {}
        nc = net_class_map.get(net_name)
        intra = (
            nc.effective_intra_pair_clearance()
            if nc is not None
            else self.autorouter.rules.trace_clearance
        )
        width = pathfinder._get_trace_width_for_net(net_name)
        return width + float(intra)

    @staticmethod
    def _point_segment_distance(px: float, py: float, seg: Segment) -> float:
        """Euclidean distance from a point to a segment's centerline."""
        vx = seg.x2 - seg.x1
        vy = seg.y2 - seg.y1
        wx = px - seg.x1
        wy = py - seg.y1
        denom = vx * vx + vy * vy
        if denom < 1e-12:
            return math.hypot(wx, wy)
        t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
        return math.hypot(px - (seg.x1 + t * vx), py - (seg.y1 + t * vy))

    def _min_distance_to_partner(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        partner_segments: list[Segment],
        layer: Layer | None,
        sample_step: float = 0.05,
    ) -> float:
        """Min centerline distance from a segment to partner copper.

        ``layer`` of ``None`` compares against ALL partner segments
        (used for via barrels, which span every layer); otherwise only
        same-layer partner segments are considered.
        """
        best = float("inf")
        seg_len = math.hypot(x2 - x1, y2 - y1)
        n_steps = max(1, int(math.ceil(seg_len / sample_step)))
        for ps in partner_segments:
            if layer is not None and ps.layer != layer:
                continue
            for i in range(n_steps + 1):
                t = i / n_steps
                d = self._point_segment_distance(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, ps)
                if d < best:
                    best = d
        return best

    def _pair_has_physical_overlap(self, p_route: Route, n_route: Route) -> bool:
        """True when P/N copper PHYSICALLY intersects (issue #3508).

        Mirrors the recipe-side shapely detector
        (``_repair_pair_overlap_solo``'s ``_sides_overlap``) that rips
        pairs in board 06's 6b repair: same-layer seg/seg overlap,
        via-barrel vs any-layer segments, and via-vs-via.  The #3320
        gate only sees same-layer SEGMENT pairs, so a crossing tail or
        shadow via overlapping the partner sailed through it and was
        ripped (de-coupled) downstream.  Running the full check at
        construction time keeps the committed pre-phase output
        rip-proof.
        """
        for a, b in ((p_route, n_route), (n_route, p_route)):
            for via in a.vias:
                bound_any = via.diameter / 2
                for seg in b.segments:
                    if self._point_segment_distance(via.x, via.y, seg) < bound_any + seg.width / 2:
                        return True
                for w in b.vias:
                    if math.hypot(via.x - w.x, via.y - w.y) < bound_any + w.diameter / 2:
                        return True
        for ps in p_route.segments:
            for ns in n_route.segments:
                if ps.layer != ns.layer:
                    continue
                if (
                    self._min_distance_to_partner(ps.x1, ps.y1, ps.x2, ps.y2, [ns], ps.layer)
                    < (ps.width + ns.width) / 2
                ):
                    return True
        return False

    def _synthesize_crossing_tail(
        self,
        pathfinder: CoupledPathfinder,
        head: Pad,
        goal: Pad,
        layer_idx: int,
        partner_segments: list[Segment],
    ) -> Route | None:
        """Two-via layer-change tail that may cross the partner guide.

        Issue #3508: polarity-swap pairs (and in-line connector exits)
        require the tail to cross the partner's path.  A same-layer
        crossing is a short, so the crossing portion dives to another
        routable layer between two vias.  Each candidate is validated
        cell-by-cell per layer, via positions are checked with the
        pathfinder's via predicate, and -- because the partner guide is
        NOT in the grid -- explicit geometric clearance against the
        partner segments is enforced: same-layer segment portions and
        via barrels keep ``via_diameter/2 + trace_clearance +
        partner_width/2`` of centerline distance.
        """
        grid = self.autorouter.grid
        rules = self.autorouter.rules
        goal_layer_idx = grid.layer_to_index(goal.layer.value)
        if goal_layer_idx != layer_idx:
            return None
        width = pathfinder._get_trace_width_for_net(head.net_name)
        partner_width = max((ps.width for ps in partner_segments), default=width)
        # Via barrel vs partner trace clearance bound (vias are not
        # pair members; the standard manufacturer clearance applies).
        via_clear = rules.via_diameter / 2 + rules.trace_clearance + partner_width / 2
        # Same-layer trace vs partner trace: the intra-pair bound.
        seg_clear = self._pair_seg_clearance(pathfinder, head.net_name)

        surface = Layer(grid.index_to_layer(layer_idx))
        routable = [li for li in grid.get_routable_indices() if li != layer_idx]
        if not routable:
            return None

        dx = goal.x - head.x
        dy = goal.y - head.y
        run = math.hypot(dx, dy)
        if run < 1e-9:
            return None
        ux, uy = dx / run, dy / run
        nxp, nyp = -uy, ux  # unit normal

        # Candidate via sites around each endpoint.  Tails are short
        # (sub-2 mm), so the two vias generally cannot sit ON the
        # head->goal line; lateral offsets give the crossover room.
        def _via_candidates(cx: float, cy: float, toward: float) -> list[tuple[float, float]]:
            out = []
            for a in (0.0, 0.5, 1.0):
                for b in (0.0, 0.6, -0.6, 1.2, -1.2):
                    out.append((cx + toward * ux * a + nxp * b, cy + toward * uy * a + nyp * b))
            return out

        # Issue #3855: board-wide drill registry (through-hole pads + all
        # committed vias, any net) so each fan-out via candidate can be
        # rejected when its drill would sit within ``min_hole_to_hole``
        # edge-to-edge of an existing drill.  Assembled once per crossover.
        from .via_clearance import drill_hole_to_hole_clear

        existing_drills = self._collect_existing_drills()
        min_h2h = getattr(rules, "min_hole_to_hole", 0.5)

        # Issue #4574: the candidate lattice is enumerated in a fixed order
        # that carries no information about the board, so a site that happens
        # to plug a neighbouring unrouted pad's only escape wins purely by
        # being earlier in the tuple literals.  Re-ORDER the pairs by how
        # deeply their barrels intrude on those escapes -- lowest intrusion
        # first -- and leave every legality gate below exactly where it is.
        #
        # ``sort`` is stable, so pairs with equal penalty (and, when no
        # channel is modelled at all, ALL pairs) keep their enumeration order:
        # with an empty or uninformative registry this returns bit-for-bit
        # what the un-scored first-legal loop returned.
        #
        # Scoring is confined to the shadow constructor -- the one caller
        # whose barrels are placed speculatively into a live pad field.  The
        # stub-edge completion path (issue #3508) runs with construction OFF
        # and keeps its untouched default behaviour.
        candidate_pairs = [
            (v1, v2)
            for v1 in _via_candidates(head.x, head.y, 1.0)
            for v2 in _via_candidates(goal.x, goal.y, -1.0)
        ]
        # Issue #4580: the crossover LEGALITY census.  ``_synthesize_crossing_tail``
        # returns the first legal candidate and says nothing about the rest, so
        # "the site that ships seals a neighbour" is indistinguishable from two
        # very different worlds: an ORDERING problem (many sites are legal, a
        # better key would pick a kinder one) or a SATURATION one (almost
        # nothing is legal, and no key can help).  #4574 and #4580 were both
        # filed as the former; measuring board-06 showed the latter.  Telling
        # them apart needs the size and membership of the legal set, which no
        # amount of ``KCT_SHADOW_DEBUG`` output can supply.
        #
        # ``KCT_CROSSTAIL_CENSUS=1`` scans the WHOLE lattice instead of
        # stopping at the first legal candidate and reports what it found.  It
        # is observation only -- the route returned is still the first legal
        # one in sorted order, i.e. exactly what the un-instrumented loop
        # returns -- and it costs a full 225-candidate sweep per crossover, so
        # it is opt-in and off in every normal run.
        census_on = _CROSSTAIL_CENSUS
        census_enum = {id(pair): i for i, pair in enumerate(candidate_pairs)}
        census_legal: list[tuple[int, int, float, _XY, _XY]] = []
        census_key: Callable[[tuple[_XY, _XY]], float] | None = None
        census_first: Route | None = None
        # Issue #4635: the census's sweep is spent inside the shadow spec's
        # WALL-CLOCK budget, so it is state-neutral but not budget-neutral.
        # Stamp the moment the un-instrumented loop would have RETURNED (set
        # with ``census_first`` below) and charge only the sweep AFTER it to
        # ``self._census_elapsed_s``, which the deadline arithmetic in
        # ``route_differential_pair_coupled`` credits back.  Charging the whole
        # loop would over-credit: on a saturated lattice the un-instrumented
        # loop already scans most of the 225 candidates before it finds
        # anything, and a crossover with NO legal candidate scanned the entire
        # lattice in both modes -- so it correctly credits zero.
        census_extra_t0: float | None = None
        if self.enable_shadow_construction:
            exclude = {head.net}
            exclude.update(ps.net for ps in partner_segments)
            channels = self._escape_channel_registry(frozenset(exclude))
            if channels:
                seal_keepout = rules.via_diameter / 2 + rules.trace_clearance + width / 2
                seal_cache: dict[tuple[float, float], float] = {}

                def _site_penalty(site: tuple[float, float]) -> float:
                    cached = seal_cache.get(site)
                    if cached is None:
                        cached = _channel_seal_penalty(site[0], site[1], seal_keepout, channels)
                        seal_cache[site] = cached
                    return cached

                def _pair_penalty(pair: tuple[_XY, _XY]) -> float:
                    return _site_penalty(pair[0]) + _site_penalty(pair[1])

                candidate_pairs.sort(key=_pair_penalty)
                census_key = _pair_penalty

        for rank, (v1, v2) in enumerate(candidate_pairs):
            # Issue #3855: replace the hardcoded 0.6mm center-to-center
            # via-to-via check with an edge-to-edge ``min_hole_to_hole``
            # check.  This single crossover's two vias must clear each
            # other AND every other existing drill (other crossovers'
            # fan-out vias + through-hole pad drills, any net).
            edge_v1v2 = (
                math.hypot(v2[0] - v1[0], v2[1] - v1[1]) - rules.via_drill / 2 - rules.via_drill / 2
            )
            if edge_v1v2 < min_h2h:
                continue  # the two crossover vias too close drill-to-drill
            if not drill_hole_to_hole_clear(
                v1[0], v1[1], rules.via_drill, existing_drills, min_h2h
            ):
                continue
            if not drill_hole_to_hole_clear(
                v2[0], v2[1], rules.via_drill, existing_drills, min_h2h
            ):
                continue
            g1 = grid.world_to_grid(*v1)
            g2 = grid.world_to_grid(*v2)
            if pathfinder._is_via_blocked(g1[0], g1[1], head.net):
                continue
            if pathfinder._is_via_blocked(g2[0], g2[1], head.net):
                continue
            # Via barrels: distance to partner copper on ANY layer.
            if (
                self._min_distance_to_partner(v1[0], v1[1], v1[0], v1[1], partner_segments, None)
                < via_clear
            ):
                continue
            if (
                self._min_distance_to_partner(v2[0], v2[1], v2[0], v2[1], partner_segments, None)
                < via_clear
            ):
                continue
            # Surface stubs must stay clear of same-layer partner copper.
            if (
                self._min_distance_to_partner(
                    head.x, head.y, v1[0], v1[1], partner_segments, surface
                )
                < seg_clear
            ):
                continue
            if (
                self._min_distance_to_partner(
                    v2[0], v2[1], goal.x, goal.y, partner_segments, surface
                )
                < seg_clear
            ):
                continue
            if not self._segment_cells_clear(
                pathfinder, head.x, head.y, v1[0], v1[1], layer_idx, head.net
            ):
                continue
            if not self._segment_cells_clear(
                pathfinder, v2[0], v2[1], goal.x, goal.y, layer_idx, head.net
            ):
                continue
            for alt in routable:
                if not self._segment_cells_clear(
                    pathfinder, v1[0], v1[1], v2[0], v2[1], alt, head.net
                ):
                    continue
                alt_layer = Layer(grid.index_to_layer(alt))
                # Issue #3508 (second pass): the partner guide is
                # NOT in the grid, so the alt-layer crossover must
                # also be checked geometrically against partner
                # copper ON THAT LAYER -- a via-bearing partner
                # guide has inner-layer segments the cell check
                # cannot see (measured: USB3_RX1/RX2 "physically
                # overlapping copper" rips in the recipe's 6b
                # repair even with nudge protection).
                if (
                    self._min_distance_to_partner(
                        v1[0], v1[1], v2[0], v2[1], partner_segments, alt_layer
                    )
                    < seg_clear
                ):
                    continue
                route = Route(net=head.net, net_name=head.net_name)
                if math.hypot(v1[0] - head.x, v1[1] - head.y) > 0.01:
                    route.segments.append(
                        Segment(
                            x1=head.x,
                            y1=head.y,
                            x2=v1[0],
                            y2=v1[1],
                            width=width,
                            layer=surface,
                            net=head.net,
                            net_name=head.net_name,
                        )
                    )
                route.vias.append(
                    Via(
                        x=v1[0],
                        y=v1[1],
                        drill=rules.via_drill,
                        diameter=rules.via_diameter,
                        layers=(surface, alt_layer),
                        net=head.net,
                        net_name=head.net_name,
                    )
                )
                route.segments.append(
                    Segment(
                        x1=v1[0],
                        y1=v1[1],
                        x2=v2[0],
                        y2=v2[1],
                        width=width,
                        layer=alt_layer,
                        net=head.net,
                        net_name=head.net_name,
                    )
                )
                route.vias.append(
                    Via(
                        x=v2[0],
                        y=v2[1],
                        drill=rules.via_drill,
                        diameter=rules.via_diameter,
                        layers=(alt_layer, surface),
                        net=head.net,
                        net_name=head.net_name,
                    )
                )
                if math.hypot(goal.x - v2[0], goal.y - v2[1]) > 0.01:
                    route.segments.append(
                        Segment(
                            x1=v2[0],
                            y1=v2[1],
                            x2=goal.x,
                            y2=goal.y,
                            width=width,
                            layer=surface,
                            net=head.net,
                            net_name=head.net_name,
                        )
                    )
                # Issue #4571: the crossover's stubs, its alt-layer
                # crossing and BOTH via barrels are only raster-validated
                # above, and the raster's pad halo is shrunk in fine-pitch
                # corridors.  Screen the assembled candidate with the exact
                # DRC-equivalent pad predicate so a violating crossover
                # loses to a later via-site candidate instead of shipping.
                if self._route_pad_violation(route)[0] > _SHADOW_PAD_DEFICIT_EPS:
                    continue
                # Issue #4575: the barrel screens above measure this crossover
                # against the partner's SEGMENTS only, and the raster cannot
                # see the partner at all.  Hold the assembled crossover to the
                # exact ``clearance_segment_via`` / via-vs-via predicates too,
                # so a via site that grazes the partner's barrel loses to the
                # next candidate pair rather than shipping.
                if not self._route_via_clear(route):
                    continue
                if census_on:
                    census_legal.append(
                        (
                            census_enum.get(id(candidate_pairs[rank]), -1),
                            rank,
                            census_key((v1, v2)) if census_key is not None else 0.0,
                            v1,
                            v2,
                        )
                    )
                    if census_first is None:
                        census_first = route  # what the un-instrumented loop returns
                        # Issue #4635: everything past this point is the
                        # census's own cost, not the router's.
                        census_extra_t0 = time.monotonic()
                    break  # legal -- record it and keep scanning the lattice
                return route
        if census_on:
            # Issue #4635: credit the INCREMENTAL sweep (post-first-legal) back
            # to the spec's wall-clock budget.  ``census_extra_t0 is None``
            # means no candidate was ever legal, i.e. both modes scanned the
            # whole lattice -- nothing to credit.  The report's own stdout cost
            # is deliberately not credited: it is bounded (at most
            # ``_CROSSTAIL_CENSUS_LIST`` + 2 lines) and crediting it would make
            # the ``census_s=`` field it prints self-referential.
            census_extra_s = 0.0 if census_extra_t0 is None else time.monotonic() - census_extra_t0
            self._census_elapsed_s += census_extra_s
            # Issue #4799: capture the same numbers the header prints into a
            # structured record, for the aggregate report.  Deliberately AFTER
            # the credit above -- this append is part of the census's
            # uncredited, bounded per-crossover tail, exactly like the prints.
            self._collect_crossing_tail_census(
                head, goal, census_legal, len(candidate_pairs), census_extra_s
            )
            self._report_crossing_tail_census(
                head, goal, census_legal, len(candidate_pairs), census_extra_s
            )
            return census_first  # observation only: the first legal candidate
        return None

    def _collect_crossing_tail_census(
        self,
        head: Pad,
        goal: Pad,
        legal: list[tuple[int, int, float, _XY, _XY]],
        total: int,
        census_s: float = 0.0,
    ) -> CrossingTailCensusRecord:
        """Record one crossover's census result for the aggregate report (#4799).

        The free-text header printed by :meth:`_report_crossing_tail_census`
        answers one crossover at a time; a board's actual question is a
        distribution ("what fraction of crossovers had nothing legal at all?").
        Reconstructing that from stdout means scraping, so the same figures are
        captured here as data, on the router instance *and* in the process-wide
        :data:`~kicad_tools.router.crosstail_census.CENSUS_COLLECTOR` the JSON
        report is written from.

        Report-only: the returned record is never consulted by a routing
        decision, and this method is only reached on the census path, which
        already returns the first legal candidate regardless.
        """
        record = CrossingTailCensusRecord(
            net_name=head.net_name or "",
            head=(head.x, head.y),
            goal=(goal.x, goal.y),
            legal=len(legal),
            total=total,
            # Same expression the header prints, so the two can never disagree.
            distinct_v1=len({site[3] for site in legal}),
            census_s=census_s,
        )
        self._census_records.append(record)
        CENSUS_COLLECTOR.add(record)
        return record

    @staticmethod
    def _report_crossing_tail_census(
        head: Pad,
        goal: Pad,
        legal: list[tuple[int, int, float, _XY, _XY]],
        total: int,
        census_s: float = 0.0,
    ) -> None:
        """Print one crossover's legality census (issue #4580).

        The header's ``legal=`` count is the true size of the legal set; only
        the per-candidate listing truncates, and it says so when it does.  The
        distinction that matters when reading this is whether the legal
        candidates share a via SITE: distinct sites mean the ordering has a
        real choice to make, while a legal set that is a singleton in ``v1``
        (board-06's MIPI_CLK- landing) means no ordering key can move the
        result and the constraint lives upstream in placement / escape
        planning.

        Issue #4635: ``census_s`` is this crossover's INCREMENTAL wall-clock
        cost -- the sweep after the first legal candidate, i.e. exactly what
        the un-instrumented loop would not have paid, and exactly what is
        credited back to the shadow spec's budget.  Zero when no candidate was
        legal (both modes scanned the whole lattice).  It is appended LAST so
        the pre-existing fields keep their names and positions for anything
        parsing this header.
        """
        print(
            f"    [crosstail-census] net={head.net_name} "
            f"head=({head.x:.5f},{head.y:.5f}) goal=({goal.x:.5f},{goal.y:.5f}) "
            f"legal={len(legal)}/{total} "
            f"distinct_v1={len({site[3] for site in legal})} "
            f"census_s={census_s:.4f}",
            flush=True,
        )
        for enum_i, rank, penalty, v1, v2 in legal[:_CROSSTAIL_CENSUS_LIST]:
            print(
                f"    [crosstail-census]   rank={rank} enum={enum_i} pen={penalty:.4f} "
                f"v1=({v1[0]:.5f},{v1[1]:.5f}) v2=({v2[0]:.5f},{v2[1]:.5f})",
                flush=True,
            )
        if len(legal) > _CROSSTAIL_CENSUS_LIST:
            print(
                f"    [crosstail-census]   ... {len(legal) - _CROSSTAIL_CENSUS_LIST} "
                f"further legal candidate(s) not listed",
                flush=True,
            )

    def _tail_partner_clear(
        self,
        tail: Route,
        partner_segments: list[Segment],
        clearance: float,
    ) -> bool:
        """Does ``tail`` keep clear of the partner (guide) copper? (issue #4460)

        Two bounds, selected by ``clearance``:

        * ``clearance > 0`` -- the intra-pair CLEARANCE bound (centre-to-centre
          ``trace_width + intra_pair_clearance``), i.e. a properly-coupled
          tail.  Via barrels additionally keep the manufacturer trace
          clearance to partner copper on any layer.
        * ``clearance <= 0`` -- the PHYSICAL-overlap bound only (copper edges
          must not intersect).  This is exactly what the constructor's
          self-check gate (``find_intra_pair_clearance_violations`` /
          ``_pair_has_physical_overlap``) demands, so a tail passing it can
          never be the reason a side is declined.

        The partner is never in the routing grid during shadow construction,
        so this geometric screen is the only thing standing between a
        partner-blind A* tail and copper drawn on top of the guide.
        """
        rules = self.autorouter.rules
        strict = clearance > 0.0
        for seg in tail.segments:
            for ps in partner_segments:
                if ps.layer != seg.layer:
                    continue
                need = clearance if strict else (seg.width + ps.width) / 2.0
                # Cheap AABB reject first: guides run to 900 segments and the
                # sampled distance below is the hot loop of every tail screen.
                if not _segments_within(seg, ps, need):
                    continue
                if (
                    self._min_distance_to_partner(seg.x1, seg.y1, seg.x2, seg.y2, [ps], seg.layer)
                    < need
                ):
                    return False
        for via in tail.vias:
            bound = via.diameter / 2.0 + (rules.trace_clearance if strict else 0.0)
            for ps in partner_segments:
                if self._point_segment_distance(via.x, via.y, ps) < bound + ps.width / 2.0:
                    return False
        return True

    def _partner_boost_sites(
        self,
        head: Pad,
        goal: Pad,
        partner_segments: list[Segment],
        seg_clear: float,
        max_sites: int = 40,
    ) -> list[tuple[float, float]]:
        """Sample partner copper inside the tail's neighbourhood (issue #4460).

        Returned world points feed ``_single_ended_guide_route``'s
        ``avoid_locations`` so the fallback tail's A* pays to run along the
        guide instead of treating it as free space.  Only partner copper near
        the head->goal corridor is sampled (a whole 900-segment escape guide
        would boost the entire board), points are spaced by ``seg_clear`` so
        the boosted neighbourhoods tile rather than stack, and the count is
        capped so the boost pass stays cheap.
        """
        if not partner_segments or seg_clear <= 0.0:
            return []
        pad = 2.0 * seg_clear + 0.5
        lo_x, hi_x = min(head.x, goal.x) - pad, max(head.x, goal.x) + pad
        lo_y, hi_y = min(head.y, goal.y) - pad, max(head.y, goal.y) + pad
        step = max(self.autorouter.grid.resolution, seg_clear)
        sites: list[tuple[float, float]] = []
        for ps in partner_segments:
            seg_len = math.hypot(ps.x2 - ps.x1, ps.y2 - ps.y1)
            n_steps = max(1, int(math.ceil(seg_len / step)))
            for i in range(n_steps + 1):
                t = i / n_steps
                px = ps.x1 + (ps.x2 - ps.x1) * t
                py = ps.y1 + (ps.y2 - ps.y1) * t
                if not (lo_x <= px <= hi_x and lo_y <= py <= hi_y):
                    continue
                if any(math.hypot(px - sx, py - sy) < step for sx, sy in sites):
                    continue
                sites.append((px, py))
                if len(sites) >= max_sites:
                    return sites
        return sites

    def _partner_channel_wall_sites(
        self,
        head: Pad,
        goal: Pad,
        partner_segments: list[Segment],
        seg_clear: float,
        max_sites: int = 40,
    ) -> list[tuple[float, float]]:
        """Cost walls just OUTSIDE the coupling window (issue #4577).

        :meth:`_partner_boost_sites` samples the partner itself, so it only
        ever pushes the fallback A* AWAY from the guide.  Nothing rewards the
        band the continuity rule actually measures, and the probe -- which is a
        shortest-path search over free cells -- has no reason to stay in it:
        measured on board-06 seed 42, USB3_TX1's shipped landing tail is
        4.865 mm over 84 grid steps at **0.000** coupled.

        These sites are the missing other wall.  Offsetting each partner
        segment by ``_COUPLING_WINDOW_MM + partner width + margin``
        centre-to-centre on BOTH sides puts a soft cost fence at the far edge
        of the coupled band, so the two cheapest lanes through the corridor are
        the ones between the guide's own halo and that fence -- i.e. exactly
        the offsets a coupled tail wants.  Combined with the repelling sites
        the caller already has, the pair forms a channel rather than a
        gradient.

        ``avoid_locations`` only ADDS cost (``GridCell.avoidance_cost``), so
        the fence is a preference, never a barrier: a tail whose only path
        leaves the channel still routes, it just pays.  Nothing is written to
        the grid's corridor reservations, which are global and shared with the
        escape router (``grid.py`` ``clear_corridor_reservations`` / #4071).
        """
        if not partner_segments or seg_clear <= 0.0:
            return []
        reach = _COUPLING_WINDOW_MM + _TAIL_CHANNEL_WALL_MARGIN_MM
        pad = 2.0 * seg_clear + reach + 0.5
        lo_x, hi_x = min(head.x, goal.x) - pad, max(head.x, goal.x) + pad
        lo_y, hi_y = min(head.y, goal.y) - pad, max(head.y, goal.y) + pad
        step = max(self.autorouter.grid.resolution, seg_clear)
        sites: list[tuple[float, float]] = []
        for ps in partner_segments:
            seg_len = math.hypot(ps.x2 - ps.x1, ps.y2 - ps.y1)
            if seg_len < 1e-9:
                continue
            wall = reach + ps.width
            nx, ny = -(ps.y2 - ps.y1) / seg_len, (ps.x2 - ps.x1) / seg_len
            n_steps = max(1, int(math.ceil(seg_len / step)))
            for i in range(n_steps + 1):
                t = i / n_steps
                px = ps.x1 + (ps.x2 - ps.x1) * t
                py = ps.y1 + (ps.y2 - ps.y1) * t
                for sign in (1.0, -1.0):
                    wx, wy = px + wall * nx * sign, py + wall * ny * sign
                    if not (lo_x <= wx <= hi_x and lo_y <= wy <= hi_y):
                        continue
                    if any(math.hypot(wx - sx, wy - sy) < step for sx, sy in sites):
                        continue
                    sites.append((wx, wy))
                    if len(sites) >= max_sites:
                        return sites
        return sites

    @contextlib.contextmanager
    def _layer_locked_router(self, layer_idx: int) -> Iterator[bool]:
        """Narrow the shared pathfinder to one copper layer (issue #4570).

        Yields ``True`` when the lock is in force.  With the layer vector
        narrowed to ``layer_idx`` the A* has no via expansion available, so any
        route it returns is PLANAR by construction -- which is how the
        constructor gets a landing tail that does not invent a via its partner
        leg has no counterpart for.

        Yields ``False`` (and changes nothing) when the active pathfinder does
        not expose ``set_routable_layers`` -- the pure-Python backend.  Callers
        must then treat the planar attempt as unavailable rather than assume
        the result is planar.

        The narrowing is restored unconditionally: ``routable_layers_`` is
        shared by every net on the board, so leaking it would silently forbid
        vias for the rest of the run.
        """
        router = getattr(self.autorouter, "router", None)
        setter = getattr(router, "set_routable_layers", None)
        if setter is None or router is None:
            yield False
            return
        saved = list(getattr(router, "_routable_layers", []) or [])
        if layer_idx not in saved:
            # The lock layer is not routable at all (plane / disallowed): a
            # planar tail on it is impossible, so do not disturb the router.
            yield False
            return
        if saved == [layer_idx]:
            # Already single-layer -- nothing to narrow, nothing to restore.
            yield True
            return
        setter([layer_idx])
        try:
            yield True
        finally:
            setter(saved)

    def _planar_tail_probe(
        self,
        head: Pad,
        goal: Pad,
        layer_idx: int,
        partner_segments: list[Segment] | None,
        seg_clear: float,
    ) -> Route | None:
        """A landing tail that is PLANAR by construction (issue #4570).

        Runs the per-net A* with the router's layer vector locked to
        ``layer_idx``, so the search has no via expansion available at all.
        The result is then held to exactly the gates every other tail
        candidate passes -- the exact foreign-pad predicate (#4571) and the
        partner screen (#4460) -- so preferring it can never smuggle copper
        past a check the diving alternative would have failed.

        The partner screen is the STRICT intra-pair bound, not the
        physical-overlap floor the legacy fallback settles for: this probe runs
        ahead of ``_synthesize_crossing_tail``, whose stubs already honour the
        strict bound, so accepting a merely-non-overlapping planar tail here
        would buy via symmetry with intra-pair clearance (measured on board-06
        seed 42: ``diffpair_clearance_intra`` 7 -> 19).

        Returns ``None`` when the lock is unavailable (pure-Python backend),
        when no planar path exists, or when the candidate fails a gate.
        """
        with self._layer_locked_router(layer_idx) as locked:
            cand = (
                self._single_ended_guide_route(head, goal, per_net_timeout=10.0) if locked else None
            )
        why: str | None = None
        if not locked:
            why = "no-layer-lock"
        elif cand is None or not cand.segments:
            why = "no-planar-path"
        elif cand.vias:
            # Defensive: a lock that did not actually suppress via expansion
            # buys nothing here.
            why = "lock-leaked-a-via"
        elif self._route_pad_violation(cand)[0] > _SHADOW_PAD_DEFICIT_EPS:
            why = "pad-deficit"
        elif not self._route_via_clear(cand):
            why = "via-deficit"  # issue #4575
        elif partner_segments and not self._tail_partner_clear(cand, partner_segments, seg_clear):
            why = "partner-clearance"
        if _SHADOW_DEBUG:
            print(f"    [coupled-planar-probe] layer={layer_idx} result={why or 'ok'}")
        return None if why else cand

    def _via_layer_multiset(
        self, route: Route, num_copper_layers: int
    ) -> collections.Counter[tuple]:
        """Vias keyed by their (stack-ordered) layer pair (issue #4570)."""
        out: collections.Counter[tuple] = collections.Counter()
        for v in route.vias:
            la, lb = v.layers
            key = tuple(
                sorted(
                    (la, lb),
                    key=lambda lay: DiffPairLengthTracker._stack_position(lay, num_copper_layers),
                )
            )
            out[key] += 1
        return out

    def _mirror_z_jog(
        self,
        leg: Route,
        layer_pair: tuple,
        pathfinder: CoupledPathfinder,
        partner_segments: list[Segment],
        net: int,
        net_name: str,
    ) -> bool:
        """Add a matching two-via z-jog to ``leg`` in place (issue #4570).

        The other half of the via-symmetry fix: when the constructed leg needs
        a via the partner cannot give up -- board-06's landing tails must
        CROSS the guide, so ``_synthesize_crossing_tail``'s deliberate two-via
        crossover is the only legal tail and no planar alternative exists --
        the drilled length is equalised by giving the partner leg the same two
        vias instead of taking them away.

        The jog is deliberately **centreline-preserving**: a window in the
        middle of one existing segment is re-emitted on the other layer of
        ``layer_pair``, bracketed by two vias at the window's ends.  Plan-view
        copper, and therefore the planar length the length matcher works on,
        is unchanged; only the layer of a ~1 mm stretch moves.  That also
        means no new angle is introduced, so the 45-census cannot regress.

        Every piece is held to the SAME gates as all other constructed copper
        before anything is mutated:

        * ``drill_hole_to_hole_clear`` against the board-wide drill registry
          (and between the jog's own two vias),
        * ``_is_via_blocked`` (raster + drill envelope + no via-in-pad),
        * ``_via_pad_deficit`` -- the exact foreign-pad predicate the raster
          cannot see (the #4571 lesson),
        * the raster and the exact pad predicate for the relocated stretch,
        * via-barrel clearance to the partner on ANY layer.

        Returns ``True`` only when the jog was applied.
        """
        from .via_clearance import drill_hole_to_hole_clear

        grid = self.autorouter.grid
        rules = self.autorouter.rules
        la, lb = layer_pair
        try:
            idx_a = grid.layer_to_index(la.value)
            idx_b = grid.layer_to_index(lb.value)
        except Exception:  # pragma: no cover — defensive (unpopulated layer)
            return False
        routable = set(grid.get_routable_indices())
        if idx_a not in routable or idx_b not in routable:
            return False

        min_h2h = getattr(rules, "min_hole_to_hole", 0.5)
        # The window must be long enough for the two barrels to clear each
        # other drill-to-drill, plus a little slack for the raster scan.
        window = max(rules.via_drill + min_h2h + 2.0 * grid.resolution, 4.0 * grid.resolution)
        existing = self._collect_existing_drills()
        guide_width = max((s.width for s in leg.segments), default=rules.trace_width)
        via_clear = rules.via_diameter / 2 + rules.trace_clearance + guide_width / 2

        order = sorted(
            range(len(leg.segments)),
            key=lambda i: (
                -math.hypot(
                    leg.segments[i].x2 - leg.segments[i].x1, leg.segments[i].y2 - leg.segments[i].y1
                )
            ),
        )
        for i in order:
            seg = leg.segments[i]
            length = math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)
            if length < window + 4.0 * grid.resolution:
                break  # sorted longest-first: nothing after this fits either
            try:
                seg_idx = grid.layer_to_index(seg.layer.value)
            except Exception:  # pragma: no cover — defensive
                continue
            if seg_idx == idx_a:
                jog_idx = idx_b
            elif seg_idx == idx_b:
                jog_idx = idx_a
            else:
                continue
            ux = (seg.x2 - seg.x1) / length
            uy = (seg.y2 - seg.y1) / length
            t0 = (length - window) / 2.0
            ax, ay = seg.x1 + ux * t0, seg.y1 + uy * t0
            bx, by = seg.x1 + ux * (t0 + window), seg.y1 + uy * (t0 + window)

            if not drill_hole_to_hole_clear(ax, ay, rules.via_drill, existing, min_h2h):
                continue
            if not drill_hole_to_hole_clear(bx, by, rules.via_drill, existing, min_h2h):
                continue
            ga = grid.world_to_grid(ax, ay)
            gb = grid.world_to_grid(bx, by)
            if pathfinder._is_via_blocked(ga[0], ga[1], net) or pathfinder._is_via_blocked(
                gb[0], gb[1], net
            ):
                continue
            vias = [
                Via(
                    x=vx,
                    y=vy,
                    drill=rules.via_drill,
                    diameter=rules.via_diameter,
                    layers=(la, lb),
                    net=net,
                    net_name=net_name,
                )
                for vx, vy in ((ax, ay), (bx, by))
            ]
            if any(self._via_pad_deficit(v) > _SHADOW_PAD_DEFICIT_EPS for v in vias):
                continue
            # Issue #4575: the mirrored jog drills TWO new barrels into copper
            # the raster does not fully model (the partner is not in the grid
            # at all).  Hold them to the exact via-vs-foreign-segment /
            # via-vs-via predicates before anything is mutated, exactly as the
            # pad predicate above -- otherwise the symmetry repair itself
            # becomes a new source of ``clearance_segment_via`` findings.
            if any(self._via_copper_deficit(v)[0] > _SHADOW_VIA_DEFICIT_EPS for v in vias):
                continue
            if not self._segment_cells_clear(pathfinder, ax, ay, bx, by, jog_idx, net):
                continue
            jog_seg = Segment(
                x1=ax,
                y1=ay,
                x2=bx,
                y2=by,
                width=seg.width,
                layer=Layer(grid.index_to_layer(jog_idx)),
                net=net,
                net_name=net_name,
            )
            if not self._segment_pad_clear(jog_seg):
                continue
            if self._segment_via_deficit(jog_seg)[0] > _SHADOW_VIA_DEFICIT_EPS:
                continue  # issue #4575: relocated copper vs foreign barrels
            if partner_segments and any(
                self._min_distance_to_partner(v.x, v.y, v.x, v.y, partner_segments, None)
                < via_clear
                for v in vias
            ):
                continue
            # The relocated stretch is copper on a layer this leg was not on.
            # It must keep the manufacturer clearance from the PARTNER's copper
            # THERE -- the partner's own crossing tail dives to exactly these
            # inner/bottom layers, and skipping this screen was measured as 12
            # extra ``diffpair_clearance_intra`` errors on board-06 seed 42.
            if partner_segments:
                partner_w = max((p.width for p in partner_segments), default=rules.trace_width)
                jog_bound = (seg.width + partner_w) / 2.0 + rules.trace_clearance
                if (
                    self._min_distance_to_partner(ax, ay, bx, by, partner_segments, jog_seg.layer)
                    < jog_bound
                ):
                    continue

            def _piece(x1: float, y1: float, x2: float, y2: float, layer) -> Segment:
                return Segment(
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    width=seg.width,
                    layer=layer,
                    net=net,
                    net_name=net_name,
                )

            leg.segments[i : i + 1] = [
                _piece(seg.x1, seg.y1, ax, ay, seg.layer),
                jog_seg,
                _piece(bx, by, seg.x2, seg.y2, seg.layer),
            ]
            leg.vias.extend(vias)
            return True
        return False

    def _match_pair_via_signature(
        self,
        guide_route: Route,
        shadow_route: Route,
        pathfinder: CoupledPathfinder,
        num_copper_layers: int,
    ) -> bool:
        """Equalise the two legs' via signatures in place (issue #4570).

        Returns ``True`` when the pair already matched or was remediated, and
        ``False`` when it could not be -- in which case the caller declines the
        side rather than shipping copper whose skew the planar length matcher
        cannot see.

        Only the leg that is SHORT of vias grows; the excess is never removed
        here (the tail-side planar preference already tried that, upstream and
        more cheaply).  An odd excess is refused outright: a z-jog adds vias in
        pairs, and a pad-to-pad route on one surface layer cannot legally carry
        an odd count, so an odd difference means something upstream is wrong
        and must not be papered over.
        """
        guide_ms = self._via_layer_multiset(guide_route, num_copper_layers)
        shadow_ms = self._via_layer_multiset(shadow_route, num_copper_layers)
        if guide_ms == shadow_ms:
            return True
        for deficient, surplus in (
            (guide_route, shadow_ms - guide_ms),
            (shadow_route, guide_ms - shadow_ms),
        ):
            partner = shadow_route if deficient is guide_route else guide_route
            for layer_pair, count in surplus.items():
                if count % 2:
                    return False
                for _ in range(count // 2):
                    # Issue #4575: the mirror drills into the pair's OTHER
                    # constructed leg's neighbourhood, and that leg is in
                    # neither the committed routes nor the pre-assembly guide.
                    # Widen the armed universe so ``_mirror_z_jog``'s barrel
                    # screens can see the partner's own barrels and traces.
                    with self._shadow_foreign_copper_extended(partner):
                        mirrored = self._mirror_z_jog(
                            deficient,
                            layer_pair,
                            pathfinder,
                            list(partner.segments),
                            deficient.net,
                            deficient.net_name,
                        )
                    if not mirrored:
                        return False
        return self._via_layer_multiset(guide_route, num_copper_layers) == (
            self._via_layer_multiset(shadow_route, num_copper_layers)
        )

    def _fallback_tail_route(
        self,
        head: Pad,
        goal: Pad,
        partner_segments: list[Segment] | None,
        seg_clear: float,
        prefer_planar_layer: int | None = None,
    ) -> Route | None:
        """Last-resort A* tail, with a coupling-aware retry (issues #4460/#4577).

        The whole legacy chain is :meth:`_fallback_tail_route_body`, unchanged.
        This wrapper adds ONE thing: when that chain's winner carries **zero**
        coupled millimetres -- the 100%-uncoupled grid staircase issue #4577 is
        about -- a single extra probe is spent with the guide's coupling band
        fenced in on both sides (:meth:`_partner_channel_wall_sites`), and it is
        preferred only when :meth:`_tail_is_better_coupled` says it beats the
        incumbent on the same coupled-millimetres doctrine the follow tail is
        judged by, at the same partner-clearance floor the incumbent met.

        Restricting the retry to a zero-coupled incumbent is what keeps the
        probe count bounded: every tail that already runs alongside its partner
        (and every tail reached with no partner supplied) returns from the body
        without the extra search.

        **Measured outcome on board-06 shadow-ON seed 42: it fires 10 times and
        upgrades 0 tails.**  It is kept because it is the only path that can
        exploit a cheap coupled corridor, and because its instrumentation is
        what PRICES the alternative: on USB3_TX1's landing it found a genuinely
        coupled route (2.058 coupled mm against the incumbent's 0.000) that is
        10.764 mm long against the incumbent's 4.865 -- 5.9 mm of extra copper
        for 2.1 mm of coupling.  Swapping it in would have LOWERED that leg's
        ``diffpair_routing_continuity`` (0.867 -> 0.815), which is exactly what
        :meth:`_tail_is_better_coupled` exists to prevent.  The lever the issue
        named is therefore not blocked by a missing attractor; it is blocked by
        the price of the detour.
        """
        winner = self._fallback_tail_route_body(
            head, goal, partner_segments, seg_clear, prefer_planar_layer
        )
        if winner is None or not partner_segments or seg_clear <= 0.0:
            return winner
        if self._tail_coupled_mm(winner, partner_segments)[0] > 0.0:
            return winner
        return self._channel_biased_tail(
            head, goal, partner_segments, seg_clear, prefer_planar_layer, winner
        )

    def _channel_biased_tail(
        self,
        head: Pad,
        goal: Pad,
        partner_segments: list[Segment],
        seg_clear: float,
        prefer_planar_layer: int | None,
        incumbent: Route,
    ) -> Route:
        """One channel-fenced A* retry for a 0%-coupled tail (issue #4577).

        Returns ``incumbent`` unchanged unless the retry produces copper that

        * passes the same exact foreign-pad / foreign-via gates
          (:meth:`_route_pad_violation`, :meth:`_route_via_clear`),
        * meets the SAME partner-clearance floor the incumbent met (the strict
          intra-pair bound when the incumbent met it, the physical-overlap
          floor otherwise -- so the retry can never smuggle copper past a check
          the incumbent passed),
        * does not invent a via when ``prefer_planar_layer`` says the partner
          leg has none to match (#4570), and
        * beats the incumbent under :meth:`_tail_is_better_coupled`: at least
          ``_TAIL_FOLLOW_MIN_COUPLED_MM`` more coupled copper, and no more
          extra length than the coupling it bought.
        """
        strict_ok = self._tail_partner_clear(incumbent, partner_segments, seg_clear)
        floor = seg_clear if strict_ok else 0.0
        sites = self._partner_boost_sites(head, goal, partner_segments, seg_clear)
        sites = sites + self._partner_channel_wall_sites(head, goal, partner_segments, seg_clear)
        if not sites:
            return incumbent
        radius_cells = max(
            1, int(round(seg_clear / max(self.autorouter.grid.resolution, 1e-9) / 3.0))
        )
        if prefer_planar_layer is not None:
            with self._layer_locked_router(prefer_planar_layer) as locked:
                cand = (
                    self._single_ended_guide_route(
                        head,
                        goal,
                        per_net_timeout=10.0,
                        avoid_locations=sites,
                        avoid_radius_cells=radius_cells,
                    )
                    if locked
                    else None
                )
            if cand is not None and cand.vias:
                cand = None
        else:
            cand = self._single_ended_guide_route(
                head,
                goal,
                per_net_timeout=10.0,
                avoid_locations=sites,
                avoid_radius_cells=radius_cells,
            )
        # Issue #4577 (Gate 1): report WHICH gate the channel-fenced retry lost
        # to, on the same principle as the ``[coupled-follow] declined:``
        # attribution -- a retry that silently does nothing is indistinguishable
        # from one that is not wired up.
        why: str | None = None
        got = tot = 0.0
        if cand is None or not cand.segments:
            why = "no-path" if prefer_planar_layer is None else "no-planar-path"
        elif self._route_pad_violation(cand)[0] > _SHADOW_PAD_DEFICIT_EPS:
            why = "pad-deficit"
        elif not self._route_via_clear(cand):
            why = "via-deficit"
        elif not self._tail_partner_clear(cand, partner_segments, floor):
            why = "partner-clearance"
        else:
            got, tot = self._tail_coupled_mm(cand, partner_segments)
            if not self._tail_is_better_coupled(cand, incumbent, partner_segments):
                why = "not-better-coupled"
        if _SHADOW_DEBUG:
            print(
                f"    [coupled-channel] {why or 'upgraded'}: sites={len(sites)} "
                f"len={tot:.3f} coupled={got:.3f} "
                f"segs={len(cand.segments) if cand is not None else 0} "
                f"(was len={_route_copper_length(incumbent):.3f} "
                f"segs={len(incumbent.segments)})"
            )
        return incumbent if (why is not None or cand is None) else cand

    def _fallback_tail_route_body(
        self,
        head: Pad,
        goal: Pad,
        partner_segments: list[Segment] | None,
        seg_clear: float,
        prefer_planar_layer: int | None = None,
    ) -> Route | None:
        """Partner-aware last-resort A* tail (issue #4460).

        Order is deliberately conservative: the UNBIASED probe runs first and
        is returned unchanged whenever it already keeps the intra-pair
        clearance, so every tail that was acceptable before this change is
        byte-identical.  Only a tail that fails that bound triggers the
        guide-biased re-route, and only a tail that additionally fails the
        PHYSICAL-overlap bound is discarded outright -- handing the decision
        back to the caller's anchor-stepping retry instead of emitting copper
        the self-check gate is guaranteed to reject.

        Issue #4570: this per-net A* is the constructor's only via-inventing
        tail source -- it is free to change layer, and nothing on the partner
        leg mirrors the via it drills, so its dive shows up as pure skew the
        planar length matcher cannot see (measured: 2 x 1.6 mm on board-06's
        PCIE_RX / USB3_TX1).  When ``prefer_planar_layer`` names the layer the
        partner's corresponding copper occupies, ONE extra probe is spent with
        the router locked to that layer, and its result is preferred whenever
        it passes the same ``_copper_ok`` / ``_tail_partner_clear`` gates as the
        unbiased probe.  ``None`` (the default, and the only value reachable
        with shadow construction off) skips the extra probe entirely, so the
        legacy chain below is untouched.
        """

        def _probe(avoid: list[tuple[float, float]] | None, radius: int) -> Route | None:
            r = self._single_ended_guide_route(
                head,
                goal,
                per_net_timeout=10.0,
                avoid_locations=avoid,
                avoid_radius_cells=radius,
            )
            return r if (r is not None and r.segments) else None

        # Issue #4571: the A* probe validates against the grid raster, whose
        # pad halo is shrunk in fine-pitch corridors, and a diff-pair net is
        # excluded from ``drc_verify_and_nudge`` -- so a raster-clear fallback
        # tail is the constructor's most direct route to a pad short.  Discard
        # any probe result whose copper violates the exact pad predicate; the
        # caller's anchor-stepping retry then gets the attempt instead.
        # Issue #4575: the same argument one quadrant over, and it is the
        # sharper one HERE.  The last-resort acceptance below settles for
        # ``_tail_partner_clear(..., 0.0)``, whose barrel bound drops the
        # clearance term entirely -- copper is only required not to physically
        # intersect the partner.  Rejecting a via-violating probe up front (so
        # both ``plain`` and ``biased`` are already ``None`` by the time that
        # loop runs) keeps the last-resort path consistent with the
        # post-assembly gate: the caller's anchor-stepping retry gets the
        # attempt instead of the constructor emitting copper the gate is
        # guaranteed to reject.
        def _copper_ok(cand: Route | None) -> bool:
            return (
                cand is not None
                and self._route_pad_violation(cand)[0] <= _SHADOW_PAD_DEFICIT_EPS
                and self._route_via_clear(cand)
            )

        plain = _probe(None, 1)
        if not _copper_ok(plain):
            plain = None

        # Issue #4570: the unbiased probe stays FIRST CHOICE whenever it is
        # already planar, so nothing that was acceptable before changes.  Only
        # a probe that dived through the board (and so would leave the pair
        # via-asymmetric) is displaced -- and only by a layer-locked probe that
        # clears the very same gates.
        if prefer_planar_layer is not None and (plain is None or plain.vias):
            planar = self._planar_tail_probe(
                head, goal, prefer_planar_layer, partner_segments, seg_clear
            )
            if planar is not None:
                plain = planar

        if not partner_segments:
            return plain
        if plain is not None and self._tail_partner_clear(plain, partner_segments, seg_clear):
            return plain
        biased: Route | None = None
        sites = self._partner_boost_sites(head, goal, partner_segments, seg_clear)
        if sites:
            radius_cells = max(
                1, int(round(seg_clear / max(self.autorouter.grid.resolution, 1e-9) / 3.0))
            )
            # Issue #4570: when a planar tail is wanted, the guide-biased
            # re-route is locked too -- otherwise the constructor would trade
            # the planar candidate found above for a better-coupled DIVE and
            # re-open the very via asymmetry this pass exists to close.
            if prefer_planar_layer is not None and (plain is None or not plain.vias):
                with self._layer_locked_router(prefer_planar_layer) as locked:
                    biased = _probe(sites, radius_cells) if locked else None
                if biased is not None and biased.vias:
                    biased = None
            else:
                biased = _probe(sites, radius_cells)
            if not _copper_ok(biased):
                biased = None
            if biased is not None and self._tail_partner_clear(biased, partner_segments, seg_clear):
                return biased
        # Neither reaches the coupled clearance: settle for any tail that at
        # least does not physically overlap the guide (the self-check bound).
        for cand in (plain, biased):
            if cand is not None and self._tail_partner_clear(cand, partner_segments, 0.0):
                return cand
        return None

    def _tail_route(
        self,
        pathfinder: CoupledPathfinder,
        head: Pad,
        goal: Pad,
        layer_idx: int,
        label: str,
        pair_name: str,
        partner_segments: list[Segment] | None = None,
        prefer_planar: bool = False,
    ) -> Route | None:
        """Head->pad completion: synthesized tail, then per-net A* fallback.

        Issue #3508: when ``partner_segments`` is provided, planar
        candidates whose copper would overlap the partner (which is NOT
        in the grid) are rejected geometrically, and a two-via crossing
        tail is attempted before giving up -- the polarity-swap pairs'
        terminal crossover.

        Issue #4460: the last-resort per-net A* fallback is now partner-AWARE
        too.  The guide is not grid copper, so an unbiased A* tail routes
        straight along (and through) it; every board-06 shadow ``self-check
        overlap`` decline was such a tail.  The fallback is therefore
        re-routed with the guide's neighbourhood cost-boosted and the result
        is VALIDATED against the partner: a tail that physically overlaps the
        guide is discarded rather than emitted, which lets the caller's
        anchor-stepping loop retry from a deeper body anchor instead of
        spending the attempt on copper the self-check will reject anyway.

        Issue #4570: ``prefer_planar`` states that the partner leg has no via
        for this tail to be matched against, so a tail that changes layer would
        leave the pair via-asymmetric -- i.e. skewed by the vias' drilled
        length, which the constructor's planar length matcher cannot see.  It
        biases the A* fallback (the only via-inventing source here) toward
        ``layer_idx``.  It is a PREFERENCE, not a veto: a tail that can only be
        reached through a via is still returned, and the caller's post-assembly
        symmetry gate decides whether the resulting pair is shippable.  The
        deliberate two-via crossing tail (the polarity-swap crossover) is left
        alone -- it exists precisely because no planar tail was legal.
        """
        seg_clear = self._pair_seg_clearance(pathfinder, head.net_name) if partner_segments else 0.0
        tail = self._synthesize_tail(
            pathfinder,
            head,
            goal,
            layer_idx,
            partner_segments=partner_segments,
            partner_clearance=seg_clear,
        )
        # Issue #4572: a tail that FOLLOWS the partner's own landing run.  The
        # axis-aligned repertoire above is blind to the partner's shape, so in
        # a dense pad field it either lands an uncoupled dogleg or finds
        # nothing at all and the partner-blind A* probe below emits a per-cell
        # staircase.  Both outcomes are 100%-uncoupled copper on BOTH legs.
        # The offset-of-the-partner candidate is tried whenever a partner is
        # supplied and only displaces the tail above when it is legal AND
        # materially better coupled, so the cheaper axis-aligned tail keeps
        # winning wherever it was already good enough.
        source = "synth" if tail is not None else "none"
        following: Route | None = None
        # Issue #4577: a follow candidate that lost only to the "no incumbent"
        # stand-in bound, kept for a second, fair comparison once the tail that
        # actually ships exists.
        deferred_follow: Route | None = None
        if partner_segments:
            following = self._synthesize_following_tail(
                pathfinder,
                head,
                goal,
                layer_idx,
                partner_segments,
                seg_clear,
                # Only let the follow reach for the crossing / A* landers when
                # the cheap synthesizer already declined -- i.e. when THIS call
                # was going to pay for them anyway.  Keeps the worst-case cost
                # of a coupled tail identical to the pre-#4572 chain.
                allow_expensive_landing=tail is None,
                prefer_planar=prefer_planar,
            )
            if following is not None and not (
                # Issue #4570: never let a via-carrying follow displace a
                # PLANAR incumbent when the partner leg has no via to match.
                prefer_planar and following.vias and tail is not None and not tail.vias
            ):
                if self._tail_is_better_coupled(following, tail, partner_segments):
                    tail = following
                    source = "follow"
                elif tail is None:
                    # Issue #4577: with no incumbent to measure the trade
                    # against, ``_tail_is_better_coupled`` falls back to
                    # ``_TAIL_FOLLOW_MIN_FRACTION`` -- but "no incumbent" is not
                    # true here, it is merely not KNOWN YET.  When the cheap
                    # synthesizer declined, what actually ships is the
                    # partner-BLIND probe further down, and on board-06 seed 42
                    # that is USB3_TX1's 4.865 mm / 84-segment / 0.000-coupled
                    # A* staircase.  Judged against THAT, a 3.6 mm tail carrying
                    # 0.85 coupled mm is both shorter and better coupled -- it
                    # wins on exactly the coupled-millimetres doctrine this
                    # method documents, and loses the fraction test only because
                    # the comparison ran too early.  Hold the candidate and
                    # re-judge it below, against the tail that will really ship.
                    deferred_follow = following
        # Issue #4570: the LAYER-LOCKED A* runs before the crossing tail when
        # the partner leg has no via to match.  ``_synthesize_crossing_tail``
        # is a deliberate TWO-via construction (the polarity-swap crossover),
        # and it -- not the free-layer A* fallback -- is the dominant via
        # source in the constructed tails measured on board-06: two promoted
        # through-vias on one leg is exactly the 2 x 1.6 = 3.2 mm of invisible
        # skew this issue is about.  Trying a planar tail first keeps the
        # crossover available for the cases that genuinely need it (no planar
        # path exists) while removing it wherever one does.
        planar_probed = False
        if prefer_planar and (tail is None or tail.vias):
            planar = self._planar_tail_probe(head, goal, layer_idx, partner_segments, seg_clear)
            planar_probed = True
            if planar is not None:
                tail, source = planar, "astar-planar"
        if tail is None and partner_segments:
            tail = self._synthesize_crossing_tail(
                pathfinder, head, goal, layer_idx, partner_segments
            )
            source = "crossing" if tail is not None else source
        if tail is None:
            tail = self._fallback_tail_route(
                head,
                goal,
                partner_segments,
                seg_clear,
                # A planar probe that already failed will not succeed a second
                # time; do not pay for it twice.
                prefer_planar_layer=(layer_idx if (prefer_planar and not planar_probed) else None),
            )
            source = "astar" if tail is not None else source
        if tail is None and following is not None:
            # Nothing else reached the pad: a weakly-coupled following tail is
            # still legal copper and still better than declining the side.
            tail, source = following, "follow-lastresort"
        elif (
            deferred_follow is not None
            and tail is not None
            and partner_segments
            and source != "follow"
        ):
            # Issue #4577: the deferred re-judgement.  Exactly the same
            # coupled-millimetres predicate, and the same #4570 via guard, now
            # applied against the tail this call really produced instead of
            # against ``None``.  It can only displace a tail the candidate beats
            # on BOTH counts -- at least ``_TAIL_FOLLOW_MIN_COUPLED_MM`` more
            # coupled copper, and no more extra length than the coupling bought
            # -- so a follow that merely detours still loses.
            if not (
                prefer_planar and deferred_follow.vias and not tail.vias
            ) and self._tail_is_better_coupled(deferred_follow, tail, partner_segments):
                tail, source = deferred_follow, "follow-deferred"
            elif _SHADOW_DEBUG:
                # Gate 1 again: a deferred candidate that keeps losing is the
                # single most useful number for the NEXT lever choice -- it says
                # how much coupled copper the follow machinery could actually
                # find, independent of why it declined.
                got, tot = self._tail_coupled_mm(deferred_follow, partner_segments)
                inc_got, inc_tot = self._tail_coupled_mm(tail, partner_segments)
                print(
                    f"    [coupled-follow] deferred candidate lost: "
                    f"cand len={tot:.3f} coupled={got:.3f} vs "
                    f"incumbent len={inc_tot:.3f} coupled={inc_got:.3f} "
                    f"(floor={_TAIL_FOLLOW_MIN_COUPLED_MM:.3f})"
                )
        if tail is None:
            print(
                f"    [coupled-rescue] {label} tail unroutable for {pair_name} "
                f"(head {head.x:.2f},{head.y:.2f} -> pad {goal.x:.2f},{goal.y:.2f})"
            )
        elif _SHADOW_DEBUG and partner_segments:
            print(
                f"    [coupled-tail] {pair_name} {label} src={source} "
                f"len={_route_copper_length(tail):.3f} segs={len(tail.segments)} "
                f"coupled={self._tail_coupled_fraction(tail, partner_segments):.3f}"
            )
        return tail

    @staticmethod
    def _tail_coupled_mm(tail: Route, partner_segments: list[Segment]) -> tuple[float, float]:
        """``(coupled_mm, total_mm)`` of a tail under the continuity predicate."""
        by_layer: dict[Layer, list[tuple[float, float, float, float]]] = {}
        width = 0.0
        for s in tail.segments:
            by_layer.setdefault(s.layer, []).append((s.x1, s.y1, s.x2, s.y2))
            width = max(width, s.width)
        total = 0.0
        coupled = 0.0
        for lay, spans in by_layer.items():
            layer_len = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in spans)
            total += layer_len
            coupled += layer_len * _spans_coupled_fraction(spans, width, lay, partner_segments)
        return coupled, total

    def _tail_coupled_fraction(self, tail: Route, partner_segments: list[Segment]) -> float:
        """Fraction of a tail's copper the continuity rule would score coupled."""
        coupled, total = self._tail_coupled_mm(tail, partner_segments)
        return coupled / total if total > 0.0 else 0.0

    def _tail_is_better_coupled(
        self,
        candidate: Route,
        incumbent: Route | None,
        partner_segments: list[Segment],
    ) -> bool:
        """Does ``candidate`` beat ``incumbent`` on the continuity metric? (#4572)

        Judged in coupled MILLIMETRES, not in fraction-of-this-tail, because
        that is what ``diffpair_routing_continuity`` actually integrates over
        the whole leg -- a 19%-coupled 5 mm tail contributes more coupled
        copper than a 0%-coupled 4 mm one.

        Two guards keep the trade honest:

        * the gain must be at least :data:`_TAIL_FOLLOW_MIN_COUPLED_MM`, so a
          rounding sliver never displaces the incumbent (and, with a ``None``
          incumbent, never displaces the crossing-tail synthesizer that
          polarity-swap terminations need -- the caller still keeps the
          candidate as a last resort), and
        * the EXTRA copper must not exceed the coupled millimetres bought.
          Uncoupled copper lowers the leg's own fraction and is paid for again
          in length skew, so a tail that adds more length than coupling is a
          net loss even though it "couples more".  With no incumbent to
          measure the trade against, :data:`_TAIL_FOLLOW_MIN_FRACTION` stands
          in for the same bound.
        """
        cand_coupled, cand_len = self._tail_coupled_mm(candidate, partner_segments)
        if incumbent is None:
            return cand_coupled >= _TAIL_FOLLOW_MIN_COUPLED_MM and (
                cand_len <= 0.0 or cand_coupled / cand_len >= _TAIL_FOLLOW_MIN_FRACTION
            )
        inc_coupled, inc_len = self._tail_coupled_mm(incumbent, partner_segments)
        gain = cand_coupled - inc_coupled
        if gain < _TAIL_FOLLOW_MIN_COUPLED_MM:
            return False
        return (cand_len - inc_len) <= gain

    def _rebuild_section(self, points: list[Point], template: Segment) -> list[Segment]:
        """Materialise a polyline as segments carrying ``template``'s attributes."""
        out: list[Segment] = []
        for k in range(len(points) - 1):
            ax, ay = points[k]
            bx, by = points[k + 1]
            if math.hypot(bx - ax, by - ay) < 1e-9:
                continue
            out.append(
                Segment(
                    x1=ax,
                    y1=ay,
                    x2=bx,
                    y2=by,
                    width=template.width,
                    layer=template.layer,
                    net=template.net,
                    net_name=template.net_name,
                )
            )
        return out

    @staticmethod
    def _segments_census_clean(segments: list[Segment]) -> bool:
        """True when every segment passes the #3975 emission census AS WRITTEN."""
        for seg in segments:
            try:
                verify_segment_45(
                    round(seg.x1, 4),
                    round(seg.y1, 4),
                    round(seg.x2, 4),
                    round(seg.y2, 4),
                    strict=True,
                )
            except OffAngleSegmentError:
                return False
        return True

    def _guide_sections(self, guide: Route) -> list[tuple[int, list[Segment]]]:
        """Split a guide route into ordered single-layer sections."""
        grid = self.autorouter.grid
        sections: list[tuple[int, list[Segment]]] = []
        for seg in guide.segments:
            li = grid.layer_to_index(seg.layer.value)
            if not sections or sections[-1][0] != li:
                sections.append((li, []))
            sections[-1][1].append(seg)
        return sections

    def _simplify_guide_route(self, guide: Route, pathfinder: CoupledPathfinder) -> Route:
        """Compress the guide's per-cell staircase into long straight runs.

        Issue #4553.  Runs :func:`simplify_45_polyline` over each single-layer
        section of the guide, obstacle-validated against the same
        ``_is_cell_blocked`` predicate the rest of the constructor uses, and
        returns a NEW route (the caller's guide object is never mutated -- it
        is reused by the swapped-role and guide-biased retries).

        Why here and not in the trace optimizer: board-06's recipe deliberately
        excludes diff-pair nets from ``optimize_routes_grid_synced`` because
        straightening one leg of an already-committed pair destroys the
        constant-gap geometry.  Simplifying the guide BEFORE the shadow is
        offset from it has the opposite effect -- the shadow inherits the same
        long runs, so the pair stays coupled AND both legs gain the straight
        runs a downstream skew tuner needs.

        A section whose compression is not census-clean or whose endpoints
        moved is kept verbatim: graceful degradation, never a silent break.
        """
        if not _SHADOW_SIMPLIFY_GUIDE or not guide.segments:
            return guide
        simplified: list[Segment] = []
        changed = False
        for li, sec in self._guide_sections(guide):
            pts = _polyline_points(sec)
            if pts is None or len(pts) < 3:
                simplified.extend(sec)
                continue
            net = sec[0].net

            def _clear(a: Point, b: Point, _li: int = li, _net: int = net) -> bool:
                return self._segment_cells_clear(pathfinder, a[0], a[1], b[0], b[1], _li, _net)

            new_pts = simplify_45_polyline(
                pts,
                _SHADOW_SIMPLIFY_MAX_DEV_MM,
                _clear,
                max_window=_SHADOW_SIMPLIFY_MAX_WINDOW,
            )
            new_segs = self._rebuild_section(new_pts, sec[0])
            if (
                len(new_pts) >= 2
                and new_pts[0] == pts[0]
                and new_pts[-1] == pts[-1]
                and new_segs
                and self._segments_census_clean(new_segs)
            ):
                simplified.extend(new_segs)
                changed = changed or len(new_segs) != len(sec)
            else:
                simplified.extend(sec)
        if not changed:
            return guide
        out = Route(net=guide.net, net_name=guide.net_name)
        out.segments.extend(simplified)
        out.vias.extend(guide.vias)
        return out

    @staticmethod
    def _away_from_partner(mid: Point, partner_segments: list[Segment]) -> Point | None:
        """Unit vector pointing from the nearest partner copper toward ``mid``."""
        if not partner_segments:
            return None
        near = min(
            partner_segments,
            key=lambda s: _point_seg_distance(mid[0], mid[1], (s.x1, s.y1), (s.x2, s.y2)),
        )
        ax, ay, bx, by = near.x1, near.y1, near.x2, near.y2
        den = (bx - ax) ** 2 + (by - ay) ** 2
        if den < 1e-18:
            cx, cy = ax, ay
        else:
            t = max(0.0, min(1.0, ((mid[0] - ax) * (bx - ax) + (mid[1] - ay) * (by - ay)) / den))
            cx, cy = ax + (bx - ax) * t, ay + (by - ay) * t
        vx, vy = mid[0] - cx, mid[1] - cy
        norm = math.hypot(vx, vy)
        if norm < 1e-9:
            return None
        return (vx / norm, vy / norm)

    def _length_match_constructed_pair(
        self,
        pair: DifferentialPair,
        guide_route: Route,
        shadow_route: Route,
        pathfinder: CoupledPathfinder,
        min_partner_center: float,
    ) -> float:
        """Equalise a freshly-constructed pair's two legs (issue #4553).

        Called from :meth:`_shadow_route_pair` while both routes are still
        UNCOMMITTED, so lengthening either leg is transactional -- if the
        clearance gates that run immediately afterwards reject the side, the
        meandered copper goes away with it.

        Meanders the shorter leg toward the longer one, leaving half the pair's
        ``max_length_delta`` as headroom for the downstream Phase-3I tuner, and
        never adds more than ``_SHADOW_MEANDER_MAX_FRAC`` of that leg's own
        length.  Returns the millimetres actually added.
        """
        tol = float(getattr(getattr(pair, "rules", None), "max_length_delta", 0.0) or 0.0)
        headroom = max(0.05, tol * 0.5)
        g_len = _route_copper_length(guide_route)
        s_len = _route_copper_length(shadow_route)
        delta = s_len - g_len
        if abs(delta) <= headroom:
            return 0.0
        shorter, partner = (
            (guide_route, shadow_route) if delta > 0.0 else (shadow_route, guide_route)
        )
        need = min(
            abs(delta) - headroom,
            _SHADOW_MEANDER_MAX_FRAC * _route_copper_length(shorter),
        )
        if need <= 1e-6:
            return 0.0
        # Issue #4575: ``partner`` here is the pair's OTHER constructed leg --
        # copper that exists in neither the committed route list nor the
        # pre-assembly guide, so the foreign-via gate armed for this
        # construction cannot see its barrels.  A tooth is displaced AWAY from
        # the partner's traces, which says nothing about the partner's VIAS
        # (board-06 seed 42: a guide-leg tooth landed 0.090 mm from the shadow
        # leg's own landing via).  Widen the universe for the duration so each
        # candidate tooth is screened against them and a violating tooth simply
        # loses to the next window.
        with self._shadow_foreign_copper_extended(partner):
            added = self._meander_route_to_length(
                shorter,
                list(partner.segments),
                pathfinder,
                need,
                min_partner_center,
            )
        if _SHADOW_DEBUG:
            print(
                f"    [coupled-shadow-match] {pair.name} delta={delta:+.3f} "
                f"need={need:.3f} added={added:.3f} "
                f"residual={abs(delta) - added:.3f}"
            )
        return added

    def _meander_route_to_length(
        self,
        route: Route,
        partner_segments: list[Segment],
        pathfinder: CoupledPathfinder,
        target_add: float,
        min_partner_center: float,
    ) -> float:
        """Add ``target_add`` mm to ``route`` with bounded lateral-jog teeth.

        Issue #4553 (the construction-time half of the fix).  The measured
        board-06 asymmetry is 0.8-6.8 mm of EXCESS on the constructed shadow,
        which no downstream trombone tuner can absorb -- and the tuner cannot
        even engage, because neither leg carries a straight SEGMENT long enough
        to host a serpentine.  This closes the gap from the other side: it
        lengthens the SHORTER leg while the pair is still under construction,
        using :func:`lateral_jog_polyline` teeth that need only a window whose
        endpoints are far enough apart -- no straight segment required.

        Every tooth is validated before it is kept:

        * the 45-census (:func:`verify_segment_45`, strict) on the connector
          legs and the whole displaced window,
        * the grid raster (``_is_cell_blocked``) for foreign copper, pad halos
          and keepouts,
        * the partner-clearance floor (``min_partner_center``, centre-to-centre)
          against the partner polyline -- teeth are pushed AWAY from the
          partner, so the coupled gap can only widen, and
        * self-clearance against the parts of this route the tooth did not
          move.

        Teeth are spaced by at least one trace pitch so the comb never shorts
        to itself, and the total added length is capped by the caller.  Returns
        the length actually added (0.0 when nothing legal was found -- graceful
        degradation, the pair simply stays skewed as it does today).
        """
        if target_add <= 1e-6 or not route.segments:
            return 0.0
        grid = self.autorouter.grid
        res = grid.resolution
        rules = self.autorouter.rules
        # Longest sections first: teeth belong on the coupled body, not on a
        # short landing tail crammed into a pad halo.
        sections = self._guide_sections(route)
        order = sorted(range(len(sections)), key=lambda k: -_route_span(sections[k][1]))
        added_total = 0.0
        for idx in order:
            if added_total >= target_add - 1e-6:
                break
            layer_idx, sec = sections[idx]
            raw_pts = _polyline_points(sec)
            if raw_pts is None or len(raw_pts) < 2:
                continue
            width = sec[0].width
            net = sec[0].net
            template = sec[0]
            pitch = width + rules.trace_clearance
            # A compressed body is a handful of multi-millimetre edges; densify
            # so the tooth walker has vertices to anchor windows on.
            pts = densify_polyline(raw_pts, max(res, pitch / 2.0))
            if len(pts) < 3:
                continue
            others = [s for k, (_, ss) in enumerate(sections) if k != idx for s in ss]

            def _tooth_ok(
                new_pts: list[Point],
                lo: int,
                hi: int,
                _t: Segment = template,
                _li: int = layer_idx,
                _net: int = net,
                _others: list[Segment] = others,
                _w: float = width,
            ) -> bool:
                probe = self._rebuild_section(new_pts[lo : hi + 1], _t)
                if not probe or not self._segments_census_clean(probe):
                    return False
                for s in probe:
                    if not self._segment_cells_clear(pathfinder, s.x1, s.y1, s.x2, s.y2, _li, _net):
                        return False
                    # Issue #4571: a meander tooth is constructed copper on a
                    # net the downstream nudge repair skips -- validate it
                    # against the exact foreign-pad predicate, not only the
                    # (fine-pitch-shrunk) raster halo.
                    if not self._span_pad_clear(s.x1, s.y1, s.x2, s.y2, _li, _net, _w):
                        return False
                    # Issue #4575: and against the exact foreign-VIA predicate.
                    # A tooth is pushed AWAY from the partner, straight into
                    # whatever barrels sit on the far side.
                    if not self._span_via_clear(s.x1, s.y1, s.x2, s.y2, _li, _net, _w):
                        return False
                    if (
                        self._min_distance_to_partner(
                            s.x1, s.y1, s.x2, s.y2, partner_segments, _t.layer
                        )
                        < min_partner_center
                    ):
                        return False
                    for o in _others:
                        if o.layer != _t.layer:
                            continue
                        if self._point_segment_distance(s.x1, s.y1, o) < _w:
                            return False
                        if self._point_segment_distance(s.x2, s.y2, o) < _w:
                            return False
                return True

            out: list[Point] = [pts[0]]
            n = len(pts)
            i = 0
            section_added = 0.0
            while i < n - 1 and added_total < target_add - 1e-6:
                j = i + 1
                while (
                    j < n - 1 and math.hypot(pts[j][0] - pts[i][0], pts[j][1] - pts[i][1]) < pitch
                ):
                    j += 1
                if j >= n - 1:
                    break
                mid = pts[(i + j) // 2]
                away = self._away_from_partner(mid, partner_segments)
                # The tooth must actually leave the spine: a displacement along
                # the window's own chord folds the "tooth" back onto the trace
                # instead of adding usable copper.
                chord = math.hypot(pts[j][0] - pts[i][0], pts[j][1] - pts[i][1])
                cux = (pts[j][0] - pts[i][0]) / chord if chord > 1e-9 else 0.0
                cuy = (pts[j][1] - pts[i][1]) / chord if chord > 1e-9 else 0.0
                chosen: tuple[float, float] | None = None
                if away is not None:
                    # Most-outward grid direction first (no closure over the
                    # loop variable: score eagerly, then sort the tuples).
                    scored = sorted(
                        (-(u[0] * away[0] + u[1] * away[1]) / math.hypot(u[0], u[1]), u)
                        for u in _GRID_DIRS
                    )
                    for ux, uy in [u for _, u in scored[:3]]:
                        if ux * away[0] + uy * away[1] <= 0.0:
                            continue
                        norm = math.hypot(ux, uy)
                        if abs(ux / norm * cuy - uy / norm * cux) < 0.3:
                            continue  # too nearly along the spine
                        unit = norm * res
                        want = int(round((target_add - added_total) / (2.0 * unit)))
                        hi_cells = max(
                            _SHADOW_MEANDER_MIN_CELLS, min(_SHADOW_MEANDER_MAX_CELLS, want)
                        )
                        for cells in range(hi_cells, _SHADOW_MEANDER_MIN_CELLS - 1, -1):
                            dx, dy = ux * cells * res, uy * cells * res
                            cand = lateral_jog_polyline(pts, i, j, dx, dy)
                            if _tooth_ok(cand, i, j + 2):
                                chosen = (dx, dy)
                                break
                        if chosen is not None:
                            break
                if chosen is None:
                    out.append(pts[i + 1])
                    i += 1
                    continue
                dx, dy = chosen
                out.extend((p[0] + dx, p[1] + dy) for p in pts[i : j + 1])
                out.append(pts[j])
                section_added += 2.0 * math.hypot(dx, dy)
                added_total += 2.0 * math.hypot(dx, dy)
                # Skip one pitch of spine before the next tooth so the comb
                # cannot short to itself.
                k = j
                while (
                    k < n - 1 and math.hypot(pts[k][0] - pts[j][0], pts[k][1] - pts[j][1]) < pitch
                ):
                    k += 1
                out.extend(pts[j + 1 : k + 1])
                i = k
            out.extend(pts[i + 1 :])
            # Undo the densification: collinear runs collapse back to single
            # segments so the meandered leg keeps the long straight runs the
            # downstream tuner needs.
            merged = merge_collinear_points(out)
            new_segs = self._rebuild_section(merged, template)
            if new_segs and self._segments_census_clean(new_segs):
                sections[idx] = (layer_idx, new_segs)
                # Report the MEASURED gain, not the per-tooth bookkeeping, so
                # the caller's residual is the truth even if a tooth collapsed
                # during the collinear merge.
                added_total = (
                    added_total
                    - section_added
                    + (_polyline_length(merged) - _polyline_length(raw_pts))
                )
        rebuilt: list[Segment] = []
        for _, sec in sections:
            rebuilt.extend(sec)
        if added_total > 1e-6:
            route.segments[:] = rebuilt
        return added_total

    def _close_shadow_chain(
        self,
        route: Route,
        pathfinder: CoupledPathfinder,
    ) -> int:
        """Enforce chain-connectivity on an assembled shadow polyline (#4462).

        The constructed shadow is ``start tail + trimmed body + end tail``, and
        every one of those pieces is built independently.  A gap of even a few
        microns between two consecutive segments is invisible to the clearance
        and census gates but fatal downstream: the #3540 transactional strand
        guard runs :func:`validate_net_connectivity`, which unions segment
        endpoints snapped onto a 0.01 mm lattice, so a broken chain reads as
        "1/2 pads reached" and the ENTIRE coupled pair is ripped and re-routed
        single-ended -- losing a pair whose copper was otherwise good (measured
        on board-06: MIPI_CLK, MIPI_D0 and PCIE_RX, all three already
        length-matched to under 0.45 mm by #4553's constructor).

        Two repairs, both conservative:

        * a gap within one serialization quantum is closed by SNAPPING the
          following segment's start onto the previous segment's end, so the two
          coordinates are bit-identical and no rounding boundary can separate
          them; and
        * a larger gap, up to one grid cell, is closed by inserting a real
          connector segment -- but only when that connector is obstacle-clear
          and planar (same layer).

        Anything else is left exactly as it is today (the strand guard remains
        the backstop).  Returns the number of repairs made.
        """
        segs = route.segments
        if len(segs) < 2:
            return 0
        res = self.autorouter.grid.resolution
        out: list[Segment] = [segs[0]]
        repairs = 0
        for cur in segs[1:]:
            prev = out[-1]
            gap = math.hypot(cur.x1 - prev.x2, cur.y1 - prev.y2)
            if gap <= _OFFSET_JOIN_COINCIDENT_MM:
                if gap > 0.0:
                    cur.x1, cur.y1 = prev.x2, prev.y2
                    repairs += 1
                out.append(cur)
                continue
            if cur.layer == prev.layer and gap <= res:
                li = self.autorouter.grid.layer_to_index(cur.layer.value)
                # Issue #4571 / #4575: the inserted connector is real copper --
                # hold it to the exact foreign-pad AND foreign-via predicates
                # too, not just the raster.
                if (
                    self._segment_cells_clear(
                        pathfinder, prev.x2, prev.y2, cur.x1, cur.y1, li, cur.net
                    )
                    and self._span_pad_clear(
                        prev.x2, prev.y2, cur.x1, cur.y1, li, cur.net, cur.width
                    )
                    and self._span_via_clear(
                        prev.x2, prev.y2, cur.x1, cur.y1, li, cur.net, cur.width
                    )
                ):
                    out.append(
                        Segment(
                            x1=prev.x2,
                            y1=prev.y2,
                            x2=cur.x1,
                            y2=cur.y1,
                            width=cur.width,
                            layer=cur.layer,
                            net=cur.net,
                            net_name=cur.net_name,
                        )
                    )
                    repairs += 1
            out.append(cur)
        if repairs:
            route.segments[:] = out
        return repairs

    def _quantize_shadow_segments(
        self,
        route: Route,
        pathfinder: CoupledPathfinder,
    ) -> None:
        """Rewrite off-angle shadow segments as 45-legal doglegs, in place.

        Issue #3987 (unit 2a of #3921).  The shadow guide is the C++
        on-grid per-net router's output, so every guide segment is already
        45-aligned; off-angle shadow copper comes only from three
        non-offset construction sites in :meth:`_shadow_route_pair`:

        1. the raw miter-apex join at guide corners (acute / mixed
           axis-diagonal turns land the apex off-grid, 3.7-11.9 deg off),
        2. the shadow-via jog segments (the via site is chosen from a
           lateral/stagger lattice that is not on the 8-direction set), and
        3. the pad-approach rescue tails (off-grid pad centres).

        Rather than dogleg each site individually, this pass lifts the
        battle-tested file-layer transform (:func:`quantize.dogleg_points`,
        #3532 / #3907) to the route layer: it walks the assembled
        ``route.segments`` once and replaces any segment whose displacement
        is off the {0, 45, 90, 135} set with a two-leg dogleg that shares
        both endpoints exactly.  This makes shadow copper 45-compliant by
        construction (census-clean, no ``OffAngleSegmentWarning``) with a
        single transform covering all three sites -- the #3975 pattern
        lifted from the file layer to the route layer.

        The pass is OBSTACLE-AWARE (mirroring the subgrid escape doglegs of
        #3975): a dogleg's perpendicular bulge is bounded by
        ``min(|dx|, |dy|)`` but can still touch copper, so each candidate
        variant's legs are re-rastered against ``pathfinder._is_cell_blocked``
        and the first variant whose legs are both clear is kept.  When
        neither the default nor the ``axis_first`` (outboard-bulge) variant
        clears, the original segment is left untouched -- the downstream
        self-check / physical-overlap gates and the emission census still
        apply, so a residual off-angle segment degrades gracefully rather
        than shipping a short.

        The alignment decision is made on the SERIALIZED (4-decimal) copper
        via :func:`quantize.verify_segment_45` -- the same predicate the
        emission census reads -- not on the raw analytic displacement.  A
        shadow body offset can be exactly 45-aligned analytically yet round
        to a 2-quantum-asymmetric diagonal (``dx=0.0501, dy=0.0499``, 0.11
        deg off), which the census flags; deciding on the raw floats would
        skip it.  Doglegging on the rounded endpoints yields one exact axis
        leg + one exact diagonal leg that both pass the census.
        """
        grid = self.autorouter.grid

        def _is_census_clean(x1: float, y1: float, x2: float, y2: float) -> bool:
            # True iff the SERIALIZED segment passes the emission census
            # (``verify_segment_45`` accepts axis/diagonal within one 0.1 um
            # quantum).  Forced strict so it raises rather than warns.
            try:
                verify_segment_45(x1, y1, x2, y2, strict=True)
            except OffAngleSegmentError:
                return False
            return True

        new_segments: list[Segment] = []
        for seg in route.segments:
            # Decide on the serialized (4dp) copper the census governs.
            rx1, ry1 = round(seg.x1, 4), round(seg.y1, 4)
            rx2, ry2 = round(seg.x2, 4), round(seg.y2, 4)
            if _is_census_clean(rx1, ry1, rx2, ry2):
                new_segments.append(seg)
                continue
            li = grid.layer_to_index(seg.layer.value)

            def _legs_clear(mid: tuple[float, float], _li: int = li, _seg: Segment = seg) -> bool:
                for x1, y1, x2, y2 in (
                    (_seg.x1, _seg.y1, mid[0], mid[1]),
                    (mid[0], mid[1], _seg.x2, _seg.y2),
                ):
                    seg_len = math.hypot(x2 - x1, y2 - y1)
                    if seg_len < 1e-9:
                        continue
                    n_steps = max(1, int(math.ceil(seg_len / grid.resolution)))
                    for i in range(n_steps + 1):
                        t = i / n_steps
                        gx, gy = grid.world_to_grid(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
                        if pathfinder._is_cell_blocked(gx, gy, _li, _seg.net):
                            return False
                    # Issue #4571: the dogleg's perpendicular bulge is NEW
                    # copper on a net the downstream nudge repair skips, and
                    # the raster it was just checked against carries the
                    # shrunk fine-pitch pad halo.  A variant that swings a leg
                    # into a foreign pad's clearance halo loses to the other
                    # variant (or the segment is kept as-is, unchanged).
                    if not self._span_pad_clear(x1, y1, x2, y2, _li, _seg.net, _seg.width):
                        return False
                    # Issue #4575: same argument for a foreign VIA barrel --
                    # the bulge is new copper the raster's via halo does not
                    # measure at the exact ``clearance_segment_via`` threshold.
                    if not self._span_via_clear(x1, y1, x2, y2, _li, _seg.net, _seg.width):
                        return False
                return True

            chosen_mid: tuple[float, float] | None = None
            for axis_first in (False, True):
                # Dogleg on the ROUNDED endpoints so the two legs the census
                # reads are exactly axis / diagonal.
                pts = dogleg_points(rx1, ry1, rx2, ry2, axis_first=axis_first)
                if len(pts) != 3:
                    # Already aligned after rounding: keep as-is.
                    break
                mid = pts[1]
                # Only accept a variant whose BOTH legs are census-clean AND
                # obstacle-clear.
                if (
                    _is_census_clean(rx1, ry1, mid[0], mid[1])
                    and _is_census_clean(mid[0], mid[1], rx2, ry2)
                    and _legs_clear(mid)
                ):
                    chosen_mid = mid
                    break
            if chosen_mid is None:
                # No clean+clear dogleg variant -- keep the original segment;
                # the self-check / overlap gates and the emission census
                # still apply.  Graceful degradation, not a silent short.
                new_segments.append(seg)
                continue
            for x1, y1, x2, y2 in (
                (rx1, ry1, chosen_mid[0], chosen_mid[1]),
                (chosen_mid[0], chosen_mid[1], rx2, ry2),
            ):
                if math.hypot(x2 - x1, y2 - y1) < 1e-9:
                    continue
                new_segments.append(
                    Segment(
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        width=seg.width,
                        layer=seg.layer,
                        net=seg.net,
                        net_name=seg.net_name,
                    )
                )
        route.segments[:] = new_segments

    @staticmethod
    def _shadow_gap_ladder(d: float, d_min: float, d_max: float) -> list[float]:
        """Ordered candidate offset gaps for the variable-gap parallel offset.

        Issue #3990 (unit 2b of #3921).  Returns a list of center-to-center
        gaps to try for a single guide section, ordered by PREFERENCE:

        1. the nominal ``d`` first -- a section feasible at nominal keeps
           the exact fixed-gap geometry (so the easy pairs are unchanged),
        2. then TIGHTER gaps stepping down toward ``d_min`` -- the fix for
           inside-curve self-overlap (a smaller gap pulls the offset off the
           partner), tried before widening so the coupled gap stays as
           close to nominal as feasibility allows,
        3. then WIDER gaps stepping up toward ``d_max`` -- the fix for
           obstacle blockage (a larger gap steps the offset around copper
           the guide only cleared for a zero-width centerline).

        All returned gaps lie in ``[d_min, d_max]`` (the impedance band).
        ``_SHADOW_GAP_BAND_STEPS`` sets the ladder density.  When the band
        is degenerate (``d_max <= d_min`` or steps <= 1) only ``d`` is
        returned, collapsing to the fixed-gap constructor.
        """
        steps = max(1, _SHADOW_GAP_BAND_STEPS)
        if steps <= 1 or d_max - d_min < 1e-6:
            return [d]
        tighter: list[float] = []
        wider: list[float] = []
        # Uniform ladder resolution across the whole band.
        span = d_max - d_min
        inc = span / steps
        # Tighter rungs: from just below nominal down to d_min.
        g = d - inc
        while g >= d_min - 1e-9:
            tighter.append(max(d_min, g))
            g -= inc
        # Wider rungs: from just above nominal up to d_max.
        g = d + inc
        while g <= d_max + 1e-9:
            wider.append(min(d_max, g))
            g += inc
        ladder = [d]
        # Interleave tighter-first (prefer holding the coupling as tight as
        # feasibility allows), then wider fallbacks for obstacle stepping.
        ladder.extend(tighter)
        ladder.extend(wider)
        # De-dup while preserving order (float rungs can coincide at bounds).
        seen: list[float] = []
        for gv in ladder:
            if all(abs(gv - s) > 1e-9 for s in seen):
                seen.append(gv)
        return seen

    def _shadow_select_gap(
        self,
        seg: Segment,
        nx: float,
        ny: float,
        gap_ladder: list[float],
        layer_idx: int,
        s_net: int,
        pathfinder: CoupledPathfinder,
        guide_segs: list[Segment],
        min_center_dist: float,
    ) -> float:
        """Choose the per-section parallel-offset gap from the impedance band.

        Issue #3990 (unit 2b of #3921).  ``gap_ladder`` is the preference-
        ordered list of candidate center-to-center gaps (nominal first, then
        tighter, then wider -- all inside the impedance band).  This walks
        the ladder and returns the FIRST gap whose offset of ``seg`` (by
        ``gap * (nx, ny)``) is BOTH:

        * obstacle-clear -- every rastered cell of the offset segment is
          unblocked for ``s_net`` (dodges the ``mid-route blockage`` events
          where the guide threaded a zero-width-centerline gap the offset
          cannot fit through), AND
        * partner-clear -- the offset segment's minimum distance to the
          guide (partner) copper on this layer stays at or above
          ``min_center_dist`` (center-to-center), i.e. the coupled EDGE gap
          holds at or above the intra-pair clearance floor (dodges the
          inside-curve ``self-check overlap`` events).

        Because the ladder tries the nominal gap first and tighter rungs
        before wider ones, an easy segment keeps the exact nominal geometry,
        an inside-curve segment tightens just enough to pull off the
        partner, and an obstructed segment widens just enough to step
        around the obstacle -- always within ``[d_min, d_max]``.

        When NO ladder rung is feasible the nominal gap (the ladder head) is
        returned unchanged: the downstream self-check / physical-overlap
        gates and the trim logic still apply, so an infeasible section
        degrades to today's fixed-gap behaviour rather than shipping a
        violation.
        """
        grid = self.autorouter.grid
        step = grid.resolution
        for gap in gap_ladder:
            ax, ay = seg.x1 + gap * nx, seg.y1 + gap * ny
            bx, by = seg.x2 + gap * nx, seg.y2 + gap * ny
            # Obstacle raster over the candidate offset segment.
            seg_len = math.hypot(bx - ax, by - ay)
            if seg_len < 1e-9:
                return gap
            n_steps = max(1, int(math.ceil(seg_len / step)))
            blocked = False
            for i in range(n_steps + 1):
                t = i / n_steps
                gx, gy = grid.world_to_grid(ax + (bx - ax) * t, ay + (by - ay) * t)
                if pathfinder._is_cell_blocked(gx, gy, layer_idx, s_net):
                    blocked = True
                    break
            if blocked:
                continue
            # Partner-clearance: keep the coupled edge gap >= intra floor.
            if (
                self._min_distance_to_partner(ax, ay, bx, by, guide_segs, seg.layer)
                < min_center_dist
            ):
                continue
            return gap
        # No feasible rung: keep nominal (ladder head), degrade gracefully.
        return gap_ladder[0]

    @staticmethod
    def _offset_corner_join(
        pseg_start: tuple[float, float],
        pseg_end: tuple[float, float],
        a: tuple[float, float],
        b: tuple[float, float],
        side: float,
        d: float,
        resolution: float,
    ) -> tuple[str, tuple[float, float] | None]:
        """Decide how two consecutive parallel-offset segments join at a corner.

        Issue #4460 (Phase 2 of #4409): a fixed-side parallel offset of a
        bending guide self-overlaps on the INSIDE of every bend.  ``pseg_*``
        are the endpoints of the previously emitted offset segment (its end
        ``pseg_end`` is the running ``prev_pt``); ``a``/``b`` are the current
        offset segment's endpoints.  ``side`` is the lateral offset sign and
        ``d`` the nominal center spacing (used only to bound convex spikes).

        Returns ``(mode, point)``:

        * ``("none", None)`` -- the offset endpoints already meet to within one
          serialization quantum; the caller snaps the current segment's start
          onto ``pseg_end`` (an exact-coordinate no-op join) and appends it.
        * ``("miter", mx)`` -- clip/extend the previous segment's END to
          ``mx`` and start the current segment at ``mx``.  This is the correct
          join for a CONCAVE (inside) corner: the offset lines cross and the
          intersection de-folds the overlap.  The concave miter apex always
          lies on the offset side of the guide, so it can never dive across
          the guide centerline the way a straight bevel chord does at a sharp
          inside bend (the pre-fix bevel fallback dropped a fold segment clear
          across the guide, a full-trace-width local self-cross).  For CONVEX
          (outside) corners the miter is still used, but only when its spike
          stays within ``2*d + gap`` (a sharp outside turn otherwise grows an
          unbounded spike).
        * ``("bevel", None)`` -- insert a straight chord ``pseg_end -> a``.
          Reached for convex corners whose miter spike is out of bounds (the
          chord stays on the offset side there), for degenerate geometry, and
          for SUB-CELL steps (below).

        Pure geometry -- no grid / obstacle state -- so it is unit-testable in
        isolation (see ``tests/test_diffpair_shadow.py``).
        """
        gap = math.hypot(a[0] - pseg_end[0], a[1] - pseg_end[1])
        if gap <= _OFFSET_JOIN_COINCIDENT_MM:
            return ("none", None)
        if gap <= resolution / 2:
            # Issue #4462: a SUB-CELL step, not a coincidence.  Consecutive
            # collinear guide segments that select different rungs of the
            # variable-gap ladder (``_shadow_select_gap``, #3990) offset to
            # endpoints separated by the rung difference -- perpendicular to
            # travel and, on board-06's 0.05 mm grid, 0.024 mm.  This used to
            # return ``"none"``, and the caller then appended the current
            # segment starting at ``a`` while the previous one ended at
            # ``pseg_end``: a literal 0.024 mm BREAK in the emitted polyline.
            # The pair still constructed (both landing tails reached their
            # pads), but ``validate_net_connectivity`` snaps endpoints onto a
            # 0.01 mm lattice, so the net split into two components and the
            # #3540 transactional strand guard ripped the whole coupled pair
            # (measured: MIPI_CLK, MIPI_D0 and PCIE_RX, all three with
            # construction-time skew already under 0.45 mm).
            #
            # Join it with a chord rather than a miter: at this scale the two
            # offset lines are near-parallel, so the miter apex is numerically
            # unbounded, while the chord is exactly the (typically axis-
            # aligned) step itself.
            return ("bevel", None)

        d1x, d1y = pseg_end[0] - pseg_start[0], pseg_end[1] - pseg_start[1]
        d2x, d2y = b[0] - a[0], b[1] - a[1]
        # Turn handedness: cross(prev_dir, cur_dir).  The offset lies on the
        # INSIDE of the turn (segments fold/overlap) when the guide bends
        # toward the offset side, i.e. ``cross * side > 0``.
        cross = d1x * d2y - d1y * d2x
        concave = cross * side > 1e-12

        denom = d1x * d2y - d1y * d2x
        if abs(denom) <= 1e-9:
            # Parallel offset lines: no intersection to miter to.
            return ("bevel", None)
        t = ((a[0] - pseg_end[0]) * d2y - (a[1] - pseg_end[1]) * d2x) / denom
        cand = (pseg_end[0] + d1x * t, pseg_end[1] + d1y * t)

        if concave:
            # De-fold unconditionally (no spike bound): the concave miter
            # apex stays on the offset side and clipping to it removes the
            # self-crossing fold.  Guard only against the pathological
            # near-reversal where the intersection would push BEHIND the
            # previous segment's start (which would flip its direction);
            # there, fall back to the bevel rather than emit a reversed spur.
            plen2 = d1x * d1x + d1y * d1y
            if plen2 > 1e-18:
                tp = ((cand[0] - pseg_start[0]) * d1x + (cand[1] - pseg_start[1]) * d1y) / plen2
                if tp > 1e-6:
                    return ("miter", cand)
            return ("bevel", None)

        # Convex corner: bound the miter spike (sharp outside turns).
        if math.hypot(cand[0] - pseg_end[0], cand[1] - pseg_end[1]) <= 2.0 * d + gap:
            return ("miter", cand)
        return ("bevel", None)

    def _shadow_with_guide_bias(
        self,
        pair: DifferentialPair,
        spec: CoupledSegmentSpec,
        pathfinder: CoupledPathfinder,
        guide: Route,
        start_pad: Pad,
        end_pad: Pad,
        spacing_cells: int,
        swap_roles: bool,
        probe_timeout: float | None,
        overlap_sites: list[tuple[float, float]] | None = None,
    ) -> tuple[Route, Route] | None:
        """Retry shadow construction after biasing the guide away from a pinch.

        Issue #4460 (approach 2).  Called as a LAST resort, only when both
        normal shadow attempts (guide + swapped guide) have already failed, so
        it can never displace or starve a working construction.  It re-routes
        ``guide`` with the per-net A* avoidance cost boosted at two kinds of
        site, then retries the parallel-shadow construction on the new guide:

          1. GUIDE self-approaches -- non-adjacent polyline loop-backs the
             offset has no room to shadow (generic; empty on guides that do not
             fold, e.g. board 06's escape fans -- see
             :meth:`_guide_self_approaches`).
          2. ``overlap_sites`` -- the MEASURED pinch points where the failed
             attempt's parallel offset overlapped the guide (sharp inside
             bends, wrong-side tails), recorded by :meth:`_shadow_route_pair`.

        Returns the constructed ``(p_route, n_route)`` or ``None`` when there
        are no boost sites, the biased re-route fails, or the shadow still
        cannot be built.  Fully gated behind the caller's
        ``enable_shadow_construction`` check, so with shadow construction off
        (the default) this never runs and committed artifacts are
        byte-identical.
        """
        approaches = list(self._guide_self_approaches(guide, spacing_cells))
        if overlap_sites:
            approaches.extend(overlap_sites)
        if not approaches:
            return None
        # Boost radius sized to the offset gap so the re-routed guide clears
        # the parallel offset.  ``_boost_avoidance_at`` triples the passed
        # cell count internally; target a boosted radius of ~``d``.
        grid = self.autorouter.grid
        d = spacing_cells * grid.resolution
        # ``_boost_avoidance_at`` triples this internally; target a boosted
        # radius of ~2*d so the re-route is pushed clear of the offset band.
        radius_cells = max(1, int(round(2.0 * d / grid.resolution / 3.0)))
        biased_guide = self._single_ended_guide_route(
            start_pad,
            end_pad,
            per_net_timeout=probe_timeout,
            avoid_locations=approaches,
            avoid_radius_cells=radius_cells,
        )
        if biased_guide is None or not biased_guide.segments:
            return None
        return self._shadow_route_pair(
            pair, spec, pathfinder, biased_guide, spacing_cells, swap_roles=swap_roles
        )

    def _guide_self_approaches(
        self,
        guide: Route,
        spacing_cells: int,
        proximity: float | None = None,
    ) -> list[tuple[float, float]]:
        """Locate non-adjacent self-approaches in a single-ended guide.

        Issue #4460 (approach 2).  The geometric shadow constructor offsets
        each guide section perpendicular by the coupled center-to-center
        spacing ``d``.  Where the guide polyline LOOPS BACK on itself -- two
        segments far apart in path order but geometrically within ``~d`` of
        each other -- the inward offset lands on top of the distant leg (a
        full-trace-width local self-cross), and neither global offset side is
        clean because the fold bends both ways.  The per-vertex miter clip of
        #4490 (adjacent segments only) structurally cannot separate legs that
        are far apart in path order; only the GUIDE giving the offset room
        fixes it.

        This detector returns the world-coordinate midpoints of each such
        loop-back so the caller can boost the A* avoidance cost there and
        re-route the guide (see :meth:`_single_ended_guide_route`'s
        ``avoid_locations``).  It is GENERIC: any pair whose guide loops back
        within the offset clearance benefits, not a board-specific hack.  (On
        board 06's escape-fan guides this returns nothing -- those guides do
        not fold; their shadow failures are sharp-bend pinches caught instead
        by the measured ``overlap_sites`` the caller also boosts.)

        Two segments count as a self-approach when they are on the same layer,
        their centerline midpoints come within ``proximity`` center-to-center
        (``proximity`` defaults to ``d + trace_width`` -- below which the
        inward offset would overlap the far leg), AND the path DETOURS between
        them: the along-path arc separation exceeds
        ``_GUIDE_SELF_APPROACH_DETOUR_RATIO`` times the straight-line distance
        (and an absolute ``proximity`` floor).  The detour ratio distinguishes
        a genuine loop-back (arc >> chord) from an ordinary gentle bend (arc ~
        chord, ratio ~1) and from the immediately-adjacent corner the #4490
        join already handles (ratio ~1).  Returned locations are clustered so a
        long loop-back collapses to a handful of boost sites.
        """
        segs = list(guide.segments)
        if len(segs) < 3:
            return []
        grid = self.autorouter.grid
        d = spacing_cells * grid.resolution
        s_width = max((s.width for s in segs), default=0.2)
        if proximity is None:
            proximity = d + s_width
        # Cumulative arc length at each segment midpoint.
        mids: list[tuple[float, float, float, int]] = []  # (mx, my, arc, layer_idx)
        arc = 0.0
        for s in segs:
            seg_len = math.hypot(s.x2 - s.x1, s.y2 - s.y1)
            mx = (s.x1 + s.x2) / 2.0
            my = (s.y1 + s.y2) / 2.0
            mids.append((mx, my, arc + seg_len / 2.0, grid.layer_to_index(s.layer.value)))
            arc += seg_len
        approaches: list[tuple[float, float]] = []
        n = len(mids)
        for i in range(n):
            mxi, myi, arci, li = mids[i]
            for j in range(i + 1, n):
                mxj, myj, arcj, lj = mids[j]
                if li != lj:
                    continue
                if abs(mxi - mxj) > proximity or abs(myi - myj) > proximity:
                    continue
                straight = math.hypot(mxi - mxj, myi - myj)
                if straight >= proximity:
                    continue
                arc_sep = arcj - arci
                # Detour test: the path must wander much farther between the
                # two points than their direct spacing (a real loop-back), and
                # be at least a proximity-width apart along the path (excludes
                # the adjacent corner the #4490 join owns).
                if arc_sep < proximity:
                    continue
                if arc_sep < _GUIDE_SELF_APPROACH_DETOUR_RATIO * straight:
                    continue
                loc = ((mxi + mxj) / 2.0, (myi + myj) / 2.0)
                # Cluster: skip a site within ``proximity`` of one already kept
                # so a long loop-back yields a handful of boost sites, not one
                # per segment pair.
                if any(math.hypot(loc[0] - kx, loc[1] - ky) < proximity for kx, ky in approaches):
                    continue
                approaches.append(loc)
        return approaches

    def _debug_strand(
        self,
        pair_name: str,
        net_id: int,
        info: dict,
        routes: list[Route],
        net_pads: dict[int, list[Pad]],
    ) -> None:
        """Print why a committed pair net failed the #3540 connectivity claim."""
        net_routes = [r for r in routes if r.net == net_id]
        print(
            f"    [coupled-strand] {pair_name} net={net_id} "
            f"stranded={info.get('stranded_pads', [])} routes={len(net_routes)}"
        )
        for pad in net_pads.get(net_id, []):
            best = float("inf")
            for r in net_routes:
                for s in r.segments:
                    for px, py in ((s.x1, s.y1), (s.x2, s.y2)):
                        best = min(best, math.hypot(px - pad.x, py - pad.y))
            print(
                f"      pad {getattr(pad, 'ref', '?')}.{getattr(pad, 'pin', '?')} "
                f"@({pad.x:.3f},{pad.y:.3f}) nearest_endpoint={best:.4f}mm"
            )
        for ri, r in enumerate(net_routes):
            if not r.segments:
                continue
            first, last = r.segments[0], r.segments[-1]
            print(
                f"      route[{ri}] segs={len(r.segments)} vias={len(r.vias)} "
                f"start=({first.x1:.3f},{first.y1:.3f}) end=({last.x2:.3f},{last.y2:.3f})"
            )
            # Report every chain break inside the route (the union-find only
            # links endpoints that snap to the same 0.01 mm bucket).
            for k in range(len(r.segments) - 1):
                a, b = r.segments[k], r.segments[k + 1]
                gap = math.hypot(b.x1 - a.x2, b.y1 - a.y2)
                if gap > 0.005:
                    on_via = any(
                        math.hypot(v.x - a.x2, v.y - a.y2) < 0.005
                        and math.hypot(v.x - b.x1, v.y - b.y1) < 0.005
                        for v in r.vias
                    )
                    print(
                        f"        break@seg{k} gap={gap:.4f}mm "
                        f"({a.x2:.3f},{a.y2:.3f})->({b.x1:.3f},{b.y1:.3f}) "
                        f"layers={a.layer.value}/{b.layer.value} via_bridged={on_via}"
                    )

    def _debug_shadow_overlap(
        self,
        pair_name: str,
        side: float,
        violation: IntraPairClearanceViolation,
        shadow_route: Route,
        guide: Route,
        n_start_tail: int,
        n_body: int,
        n_end_tail: int,
        swap_roles: bool,
    ) -> None:
        """Print provenance for a shadow self-check overlap (``KCT_SHADOW_DEBUG``)."""
        shadow_seg = violation.p_segment if swap_roles else violation.n_segment
        guide_seg = violation.n_segment if swap_roles else violation.p_segment

        def _key(s):
            return (round(s.x1, 6), round(s.y1, 6), round(s.x2, 6), round(s.y2, 6))

        idx = -1
        for k, s in enumerate(shadow_route.segments):
            if _key(s) == _key(shadow_seg):
                idx = k
                break
        if idx < 0:
            part = "post-quantize(unmatched)"
        elif idx < n_start_tail:
            part = f"start-tail[{idx}/{n_start_tail}]"
        elif idx < n_start_tail + n_body:
            part = f"body[{idx - n_start_tail}/{n_body}]"
        else:
            part = f"end-tail[{idx - n_start_tail - n_body}/{n_end_tail}]"
        gidx = -1
        garc = 0.0
        arc = 0.0
        for k, s in enumerate(guide.segments):
            if _key(s) == _key(guide_seg):
                gidx = k
                garc = arc
            arc += math.hypot(s.x2 - s.x1, s.y2 - s.y1)
        smid = ((shadow_seg.x1 + shadow_seg.x2) / 2, (shadow_seg.y1 + shadow_seg.y2) / 2)
        # Nearest guide segment (by arc) to the shadow midpoint, to tell an
        # ADJACENT pinch (the offset's own guide segment) from a NON-ADJACENT
        # collision (a distant leg of the same guide).
        best = (float("inf"), -1, 0.0)
        arc = 0.0
        for k, s in enumerate(guide.segments):
            if s.layer == shadow_seg.layer:
                dd = self._point_segment_distance(smid[0], smid[1], s)
                if dd < best[0]:
                    best = (dd, k, arc)
            arc += math.hypot(s.x2 - s.x1, s.y2 - s.y1)
        print(
            f"    [shadow-debug] {pair_name} side={side:+.0f} "
            f"worst={violation.actual_clearance_mm:+.3f}mm part={part} "
            f"shadow_seg=({shadow_seg.x1:.3f},{shadow_seg.y1:.3f})->"
            f"({shadow_seg.x2:.3f},{shadow_seg.y2:.3f}) L={shadow_seg.layer.value} "
            f"w={shadow_seg.width:.3f} | guide_seg_idx={gidx}/{len(guide.segments)} "
            f"arc={garc:.2f} | nearest_guide_idx={best[1]} nearest_arc={best[2]:.2f} "
            f"nearest_d={best[0]:.3f}"
        )

    def _shadow_route_pair(
        self,
        pair: DifferentialPair,
        spec: CoupledSegmentSpec,
        pathfinder: CoupledPathfinder,
        guide: Route,
        spacing_cells: int,
        swap_roles: bool = False,
        simplify_guide: bool = True,
    ) -> tuple[Route, Route] | None:
        """Construct the pair as guide + validated parallel shadow.

        Issue #3508: the joint-state coupled A* cannot afford board
        06's geometry even corridor-bounded and weighted (measured: a
        clearance-clean MIPI_CLK search exceeds 80k iterations without
        converging; the dirty 2.7k-iteration solution is rejected by
        the #3320 gate).  This constructor sidesteps the search
        entirely:

        1. One side is the single-ended guide route (C++-accelerated
           per-net A*; the P side by default, the N side when
           ``swap_roles``).
        2. The partner side is built GEOMETRICALLY: each single-layer
           SECTION of the guide polyline is offset perpendicular by
           the coupled center-to-center spacing (both lateral sides
           tried).  Where the guide changes layers, the shadow places
           its own via at a laterally-widened, longitudinally-staggered
           site so both the via-to-via (0.6 mm) and via-to-partner-
           trace (~0.49 mm) clearance bounds hold by construction.
           Every shadow segment is rasterised against the grid's
           clearance envelope; shadow via sites are checked with the
           pathfinder's via predicate.
        3. The body is TRIMMED at the two route ends (endpoint zones
           are always contested by connector/IC neighbour-pad halos)
           up to ``_SHADOW_MAX_TRIM_MM`` per end, and connects to the
           real pads via the rescue tail machinery (partner-aware,
           with a two-via crossing fallback for polarity-swap
           terminations).

        Returns ``(p_route, n_route)`` -- NOT committed; the caller
        runs the #3320 severe-overlap gate and the normal commit path.
        """
        # Issue #4460 (approach 2): fresh overlap-location log for this attempt.
        self._last_shadow_overlap_locations = []
        if not guide.segments:
            return None
        # Issue #4553: compress the guide's per-cell staircase FIRST, so the
        # parallel offset is taken from a polyline with long straight runs.
        # The caller's ``guide`` object is left untouched (it is reused by the
        # swapped-role and guide-biased retries); everything below reads the
        # simplified copy.  If NO offset side survives on the compressed guide,
        # the whole construction is retried verbatim on the original polyline
        # (see the tail of this method), so compression can never cost a pair
        # that used to construct.
        raw_guide = guide
        if simplify_guide:
            guide = self._simplify_guide_route(guide, pathfinder)
        guide_was_simplified = guide is not raw_guide
        if not guide.segments:
            return None
        # Issue #4575: register the guide (the PARTNER leg) plus every
        # committed route as foreign copper for the whole construction, so the
        # exact segment-vs-via / via-vs-via predicates are reachable from every
        # tail-synthesis screen below and from the post-assembly gate.  Armed
        # here -- after simplification -- so the registered partner segments are
        # exactly the ones the tail screens receive as ``partner_segs``.  The
        # gate is a no-op outside this block, which is what keeps the
        # shadow-construction-OFF path unchanged.
        outermost = self._shadow_foreign_universe is None
        if outermost:
            self._shadow_via_gate_rejections = 0
        with self._shadow_foreign_copper(guide):
            result = self._shadow_route_pair_body(
                pair,
                spec,
                pathfinder,
                guide,
                raw_guide,
                guide_was_simplified,
                spacing_cells,
                swap_roles,
            )
        if outermost and self._shadow_via_gate_rejections:
            # One line, only when the gate actually did something: N candidates
            # were rejected for grazing a foreign barrel and lost to a LATER
            # candidate (a repair), which is the outcome that does not cost
            # reach.  A side that could not be repaired says so separately, via
            # the ``via-clearance`` decline line inside the body.
            print(
                f"    [coupled-shadow] {pair.name} foreign-via gate rejected "
                f"{self._shadow_via_gate_rejections} candidate(s) (issue #4575)"
            )
        return result

    def _shadow_route_pair_body(
        self,
        pair: DifferentialPair,
        spec: CoupledSegmentSpec,
        pathfinder: CoupledPathfinder,
        guide: Route,
        raw_guide: Route,
        guide_was_simplified: bool,
        spacing_cells: int,
        swap_roles: bool,
    ) -> tuple[Route, Route] | None:
        """Body of :meth:`_shadow_route_pair` (split out for issue #4575).

        Separated ONLY so the caller can arm ``_shadow_foreign_copper``
        around the whole construction without re-indenting it; the guide has
        already been simplified (or not) by the caller.
        """
        grid = self.autorouter.grid
        rules = self.autorouter.rules
        if swap_roles:
            shadow_start, shadow_end = spec.p_start, spec.p_end
        else:
            shadow_start, shadow_end = spec.n_start, spec.n_end
        s_net = shadow_start.net
        s_net_name = shadow_start.net_name
        s_width = pathfinder._get_trace_width_for_net(s_net_name)
        d = spacing_cells * grid.resolution

        # Issue #3990 (unit 2b of #3921): the parallel-offset gap is a BAND,
        # not a single value.  ``d`` above is the nominal center-to-center
        # spacing; the offset for each guide section may vary within
        # ``[d_min, d_max]`` to dodge inside-curve self-overlap (tighten
        # toward ``d_min``) and obstacle blockages (widen toward ``d_max``)
        # while both legs stay inside the impedance tolerance band.
        #
        # Band source (authoritative):
        #   * ``d_min`` -- the intra-pair clearance FLOOR.  The center-to-
        #     center spacing must keep at least
        #     ``trace_width + effective_intra_pair_clearance()`` so the
        #     within-pair EDGE clearance holds; this is exactly the
        #     ``required_center_spacing`` the caller derives for
        #     ``min_spacing_cells`` (``route_differential_pair_coupled``).
        #     Read from the pair's ``NetClassRouting`` when available.
        #   * ``d_max`` -- the impedance CEILING.  Widening the gap lowers
        #     coupling and raises the differential impedance; the net
        #     class' ``impedance_tolerance_percent`` (the same tolerance the
        #     ``ImpedanceRule`` DRC fires on) bounds how far.  Differential
        #     impedance is monotone-increasing and near-linear in the gap
        #     for small deviations, so bounding the GAP deviation by that
        #     percentage is a conservative proxy that keeps the pair inside
        #     the impedance band.  Capped at ``_SHADOW_GAP_MAX_TOL_FRAC``.
        pair_nc = (getattr(self.autorouter, "net_class_map", None) or {}).get(spec.p_start.net_name)
        if pair_nc is not None:
            nc_trace_width = float(pair_nc.trace_width)
            intra_floor = float(pair_nc.effective_intra_pair_clearance())
            tol_frac = min(
                _SHADOW_GAP_MAX_TOL_FRAC,
                max(0.0, float(pair_nc.impedance_tolerance_percent) / 100.0),
            )
        else:
            nc_trace_width = float(s_width)
            intra_floor = float(rules.trace_clearance)
            tol_frac = _SHADOW_GAP_MAX_TOL_FRAC
        # Never let the floor exceed the nominal (a class whose min-spacing
        # already equals the nominal collapses the band to the single ``d``).
        d_min = min(d, nc_trace_width + intra_floor)
        d_max = d * (1.0 + tol_frac)
        # Candidate gaps: a linear ladder from ``d_min`` up to ``d_max``,
        # always including the nominal ``d`` and preferring the nominal so a
        # section that is feasible at nominal is unchanged (byte-for-byte
        # stable relative to the fixed-gap constructor for the easy pairs).
        gap_ladder = self._shadow_gap_ladder(d, d_min, d_max)
        # Shadow via lateral offset: the barrel must clear the guide
        # trace (via_r + clearance + guide_width/2), independent of the
        # tighter coupled gap d.
        guide_width = max((g.width for g in guide.segments), default=s_width)
        # Issue #3541: the perpendicular distance from the shadow via to
        # the partner (guide) copper must keep this bound everywhere, not
        # just at the projected guide-via point ``gv``.  The guide BENDS
        # at the layer change, so a nominal ``via_lateral`` offset taken
        # against the incoming leg's normal can still let the barrel
        # intersect the outgoing leg (measured: ~0.04 mm intersection at
        # board 06's 0.075-0.15 mm coupled gaps).  ``via_clear`` is the
        # same via-barrel-vs-partner bound the crossing-tail synthesizer
        # enforces (see ``_synthesize_crossing_tail``); we validate each
        # candidate site against the guide polyline with it and widen the
        # perpendicular spread (the ``lat_mult`` lattice) until it holds.
        via_clear = rules.via_diameter / 2 + rules.trace_clearance + guide_width / 2
        via_lateral = max(d, via_clear + 0.05)
        guide_segs = list(guide.segments)
        # Longitudinal stagger so shadow-via-to-guide-via >= via pitch.
        via_pitch = rules.via_diameter + rules.via_clearance
        stagger = max(0.0, math.sqrt(max(0.0, via_pitch**2 - via_lateral**2))) + 0.05

        # ------------------------------------------------------------
        # Parse the guide into ordered single-layer sections.  Route
        # segments/vias are emitted in path order by both
        # ``_build_route_from_path`` and the per-net pathfinder.
        # ------------------------------------------------------------
        sections: list[tuple[int, list[Segment]]] = []
        for seg in guide.segments:
            li = grid.layer_to_index(seg.layer.value)
            if not sections or sections[-1][0] != li:
                sections.append((li, []))
            sections[-1][1].append(seg)
        max_trim = _SHADOW_MAX_TRIM_MM
        # Issue #4570: best legal-but-via-asymmetric candidate seen across both
        # offset sides.  Only used once every side has failed to produce a
        # symmetric pair -- see the end of this method.
        asym_fallback: tuple[Route, Route] | None = None

        for side in (1.0, -1.0):
            elements: list[tuple] = []  # ('seg', x1,y1,x2,y2,layer) | ('via', x,y,l0,l1)
            ok = True
            prev_pt: tuple[float, float] | None = None
            prev_layer: int | None = None
            prev_dir: tuple[float, float] | None = None
            for sec_layer, segs in sections:
                first_in_section = True
                for seg in segs:
                    ux = seg.x2 - seg.x1
                    uy = seg.y2 - seg.y1
                    length = math.hypot(ux, uy)
                    if length < 1e-9:
                        continue
                    ux /= length
                    uy /= length
                    nx = -uy * side
                    ny = ux * side
                    # Issue #3990 (unit 2b): pick the per-section offset gap
                    # from the impedance band.  Prefer the nominal ``d``;
                    # tighten (dodges inside-curve self-overlap) or widen
                    # (dodges obstacle blockage) only when the nominal offset
                    # is infeasible for THIS segment.  Feasibility is judged
                    # on the offset segment's grid raster (obstacle-clear)
                    # and its distance to the guide/partner copper (>= the
                    # intra-pair clearance floor, so the coupled edge gap
                    # holds).  Both bounds keep the pair inside the impedance
                    # band by construction (``gap_ladder`` rungs are all in
                    # ``[d_min, d_max]``).
                    seg_gap = self._shadow_select_gap(
                        seg,
                        nx,
                        ny,
                        gap_ladder,
                        sec_layer,
                        s_net,
                        pathfinder,
                        guide_segs,
                        intra_floor + s_width / 2.0 + guide_width / 2.0,
                    )
                    a = (seg.x1 + seg_gap * nx, seg.y1 + seg_gap * ny)
                    b = (seg.x2 + seg_gap * nx, seg.y2 + seg_gap * ny)
                    if prev_pt is not None and prev_layer is not None:
                        if first_in_section and prev_layer != sec_layer:
                            # Guide layer change: place the shadow via.
                            # Site: widen laterally to ``via_lateral``
                            # and stagger back along the incoming
                            # direction.
                            pux, puy = prev_dir if prev_dir else (ux, uy)
                            pnx, pny = -puy * side, pux * side
                            gv = (seg.x1, seg.y1)  # guide via position
                            placed = False
                            # Issue #3508 (second pass): widened site
                            # lattice -- lateral multipliers beyond 1.0
                            # rescue guide-via neighbourhoods where every
                            # minimum-lateral site is inside a pad halo
                            # (the FFC/J1 "no legal shadow-via site"
                            # failures).  Sites are still validated by
                            # the via predicate, and larger laterals only
                            # ADD clearance to the guide copper.
                            for lat_mult in (1.0, 1.5, 2.2):
                                for stag_mult in (1.0, 1.6, -1.0, -1.6, 2.4, -2.4):
                                    vx = (
                                        gv[0]
                                        + via_lateral * lat_mult * pnx
                                        - stagger * stag_mult * pux
                                    )
                                    vy = (
                                        gv[1]
                                        + via_lateral * lat_mult * pny
                                        - stagger * stag_mult * puy
                                    )
                                    gvx, gvy = grid.world_to_grid(vx, vy)
                                    if pathfinder._is_via_blocked(gvx, gvy, s_net):
                                        continue
                                    # Issue #3541: the guide is NOT in the
                                    # grid, so ``_is_via_blocked`` cannot
                                    # see a barrel grazing the partner.
                                    # The barrel offset is taken against
                                    # the INCOMING leg's normal, but the
                                    # guide BENDS at the via -- so a site
                                    # that clears the incoming leg can
                                    # still intersect the OUTGOING leg
                                    # when the guide turns toward the
                                    # shadow side (measured: ~0.04 mm
                                    # overlap at board 06's 0.075-0.15 mm
                                    # gaps).  Reject any candidate whose
                                    # barrel violates the via-vs-partner
                                    # clearance against the WHOLE guide
                                    # polyline (any layer -- the barrel
                                    # spans all layers); the lattice then
                                    # widens the perpendicular spread
                                    # (larger ``lat_mult``) until a site
                                    # clears every guide segment.
                                    if (
                                        self._min_distance_to_partner(
                                            vx, vy, vx, vy, guide_segs, None
                                        )
                                        < via_clear
                                    ):
                                        continue
                                    elements.append(
                                        ("seg", prev_pt[0], prev_pt[1], vx, vy, prev_layer)
                                    )
                                    elements.append(("via", vx, vy, prev_layer, sec_layer))
                                    elements.append(("seg", vx, vy, a[0], a[1], sec_layer))
                                    prev_pt = a
                                    placed = True
                                    break
                                if placed:
                                    break
                            if not placed:
                                ok = False
                                break
                        else:
                            # Issue #3508 / #4460: join the corner between the
                            # previous offset segment and this one.  A straight
                            # bevel chord between the two offset endpoints cuts
                            # INSIDE the corner (d*cos(45deg) ~ 0.75*d at a 90
                            # degree turn), shaving the coupled gap below the
                            # intra-pair clearance; worse, at a SHARP inside
                            # (concave) bend the offset polyline folds over
                            # itself and the bevel dives a fold segment clear
                            # across the guide centerline (a full-trace-width
                            # local self-cross).  ``_offset_corner_join`` mitres
                            # inside corners to the offset-line intersection
                            # (de-folding without ever crossing the guide) and
                            # keeps the spike bound only for convex corners.
                            if elements and elements[-1][0] == "seg":
                                pseg = elements[-1]
                                mode, mx = self._offset_corner_join(
                                    (pseg[1], pseg[2]),
                                    (pseg[3], pseg[4]),
                                    a,
                                    b,
                                    side,
                                    d,
                                    grid.resolution,
                                )
                                if mode == "miter" and mx is not None:
                                    elements[-1] = ("seg", pseg[1], pseg[2], mx[0], mx[1], pseg[5])
                                    a = mx
                                elif mode == "bevel":
                                    elements.append(
                                        ("seg", prev_pt[0], prev_pt[1], a[0], a[1], sec_layer)
                                    )
                                else:
                                    # Issue #4462: ``"none"`` means the two
                                    # endpoints agree to within a serialization
                                    # quantum -- make them agree EXACTLY, so the
                                    # polyline is chain-connected bit-for-bit
                                    # and no endpoint-snapping connectivity
                                    # check can split the net at a rounding
                                    # boundary.
                                    a = prev_pt
                            elif (
                                math.hypot(a[0] - prev_pt[0], a[1] - prev_pt[1])
                                > _OFFSET_JOIN_COINCIDENT_MM
                            ):
                                # Issue #4462: the post-via re-entry join had the
                                # same sub-cell hole as the corner join above --
                                # a step under half a grid cell was silently left
                                # unjoined.  Emit the connector for any real gap.
                                elements.append(
                                    ("seg", prev_pt[0], prev_pt[1], a[0], a[1], sec_layer)
                                )
                            else:
                                a = prev_pt
                    elements.append(("seg", a[0], a[1], b[0], b[1], sec_layer))
                    prev_pt = b
                    prev_layer = sec_layer
                    prev_dir = (ux, uy)
                    first_in_section = False
                if not ok:
                    break
            if not ok or prev_pt is None:
                print(
                    f"    [coupled-shadow] side={side:+.0f} no legal shadow-via "
                    f"site for {pair.name}"
                )
                self._last_shadow_decline_reason = "blockage"  # #4459
                continue

            # ------------------------------------------------------------
            # Validate + trim.  Blocked cells are tolerated only within
            # ``max_trim`` of either END of the whole shadow polyline;
            # any blockage in the interior (including via jogs) fails
            # this side.
            # ------------------------------------------------------------
            arc_total = sum(math.hypot(e[3] - e[1], e[4] - e[2]) for e in elements if e[0] == "seg")
            arc = 0.0
            interior_block = False
            step = grid.resolution
            blocked_arcs: list[float] = []
            for e in elements:
                if e[0] == "via":
                    # Issue #4571: a shadow via is exempt from the raster scan
                    # (its site was chosen with the via predicate), but that
                    # predicate reads the same shrunk pad halo.  Report a via
                    # whose barrel violates the exact pad predicate at ITS arc
                    # position, so the trim/interior logic below treats it like
                    # any other blockage.
                    _, vx0, vy0, vl0, vl1 = e
                    probe_via = Via(
                        x=vx0,
                        y=vy0,
                        drill=rules.via_drill,
                        diameter=rules.via_diameter,
                        layers=(
                            Layer(grid.index_to_layer(vl0)),
                            Layer(grid.index_to_layer(vl1)),
                        ),
                        net=s_net,
                        net_name=s_net_name,
                    )
                    if self._via_pad_deficit(probe_via) > _SHADOW_PAD_DEFICIT_EPS:
                        blocked_arcs.append(arc)
                    continue
                _, x1, y1, x2, y2, li = e
                seg_len = math.hypot(x2 - x1, y2 - y1)
                if seg_len < 1e-9:
                    continue
                n_steps = max(1, int(math.ceil(seg_len / step)))
                for i in range(n_steps + 1):
                    t = i / n_steps
                    gx, gy = grid.world_to_grid(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
                    if pathfinder._is_cell_blocked(gx, gy, li, s_net):
                        blocked_arcs.append(arc + seg_len * t)
                # Issue #4571: exact pad-clearance sibling of the raster scan.
                # Reported as ARCS (not as an outright rejection) so a body
                # end grazing a connector pad field is TRIMMED into the landing
                # tail -- the same graceful path the raster blockages take --
                # and only an interior pad violation fails the side.
                blocked_arcs.extend(self._pad_deficit_arcs(x1, y1, x2, y2, li, s_net, s_width, arc))
                arc += seg_len
            trim_start = 0.0
            trim_end = 0.0
            for ba in blocked_arcs:
                if ba <= max_trim and ba >= trim_start:
                    if ba <= max_trim:
                        trim_start = max(trim_start, ba)
                if ba >= arc_total - max_trim:
                    trim_end = max(trim_end, arc_total - ba)
            for ba in blocked_arcs:
                if ba > trim_start + 1e-9 and ba < arc_total - trim_end - 1e-9:
                    interior_block = True
                    print(
                        f"    [coupled-shadow] side={side:+.0f} mid-route "
                        f"blockage for {pair.name} at arc {ba:.2f}/"
                        f"{arc_total:.2f}mm"
                    )
                    break
            if interior_block:
                self._last_shadow_decline_reason = "blockage"  # #4459
                continue
            a0 = trim_start + 2 * step if trim_start > 0 else 0.0
            a1 = arc_total - trim_end - (2 * step if trim_end > 0 else 0.0)
            if a1 - a0 < 2.0:
                print(
                    f"    [coupled-shadow] side={side:+.0f} clear run too "
                    f"short for {pair.name} ({a1 - a0:.2f}mm)"
                )
                self._last_shadow_decline_reason = "blockage"  # #4459
                continue

            # Slice elements to [lo, hi] by arc length.  Vias are kept
            # only if inside the kept interval (vias always are: the
            # interior is blockage-free and trims are confined to the
            # ends, which lie in the first/last sections).
            def _slice_kept(lo_arc: float, hi_arc: float) -> list[tuple]:
                kept_: list[tuple] = []
                arc_ = 0.0
                for e_ in elements:
                    if e_[0] == "via":
                        if lo_arc <= arc_ <= hi_arc:
                            kept_.append(e_)
                        continue
                    _, ex1, ey1, ex2, ey2, eli = e_
                    sl = math.hypot(ex2 - ex1, ey2 - ey1)
                    if sl < 1e-9:
                        continue
                    lo_ = max(lo_arc, arc_)
                    hi_ = min(hi_arc, arc_ + sl)
                    if hi_ > lo_:
                        t_lo = (lo_ - arc_) / sl
                        t_hi = (hi_ - arc_) / sl
                        kept_.append(
                            (
                                "seg",
                                ex1 + (ex2 - ex1) * t_lo,
                                ey1 + (ey2 - ey1) * t_lo,
                                ex1 + (ex2 - ex1) * t_hi,
                                ey1 + (ey2 - ey1) * t_hi,
                                eli,
                            )
                        )
                    arc_ += sl
                return kept_

            # Issue #3508: anchor-stepping.  When the tail from the
            # body end to the pad is unroutable (the trimmed body end
            # can sit flush against a halo wall, leaving the tail no
            # legal first step), consume more of the body into the
            # tail and retry from a deeper anchor.
            partner_segs = list(guide.segments)
            # Issue #4570: the shadow BODY mirrors the guide's vias one-for-one
            # (the ``"via"`` branch below re-emits each kept guide via at the
            # offset position), but the landing TAILS are synthesized
            # independently -- so a tail that dives through the board has no
            # counterpart on the guide leg and shows up as pure electrical
            # skew.  When the guide carries no via at all, every via the tails
            # invent is therefore unmatched by construction; ask for planar
            # tails in that case.  (A guide WITH vias may legitimately want a
            # tail via -- e.g. one the body trim dropped -- so the preference is
            # not applied there; the post-assembly symmetry gate below is what
            # actually decides either way.)
            planar_tails = _SHADOW_VIA_SYMMETRY and not guide.vias
            start_tail = None
            a0_eff = a0
            for extra0 in (0.0, 0.7, 1.5, 3.0):
                if a0 + extra0 >= a1 - 2.0:
                    break
                kept_probe = _slice_kept(a0 + extra0, a1)
                seg_probe = [e for e in kept_probe if e[0] == "seg"]
                if not seg_probe:
                    break
                bh = (seg_probe[0][1], seg_probe[0][2], seg_probe[0][5])
                anchor = self._virtual_pad_at(shadow_start, bh[0], bh[1], bh[2])
                start_tail = self._tail_route(
                    pathfinder,
                    anchor,
                    shadow_start,
                    bh[2],
                    "shadow-start",
                    pair.name,
                    partner_segments=partner_segs,
                    prefer_planar=planar_tails,
                )
                if start_tail is not None:
                    a0_eff = a0 + extra0
                    break
            if start_tail is None:
                self._last_shadow_decline_reason = "blockage"  # #4459
                continue
            end_tail = None
            a1_eff = a1
            for extra1 in (0.0, 0.7, 1.5, 3.0):
                if a0_eff >= a1 - extra1 - 2.0:
                    break
                kept_probe = _slice_kept(a0_eff, a1 - extra1)
                seg_probe = [e for e in kept_probe if e[0] == "seg"]
                if not seg_probe:
                    break
                bt = (seg_probe[-1][3], seg_probe[-1][4], seg_probe[-1][5])
                anchor = self._virtual_pad_at(shadow_end, bt[0], bt[1], bt[2])
                end_tail = self._tail_route(
                    pathfinder,
                    anchor,
                    shadow_end,
                    bt[2],
                    "shadow-end",
                    pair.name,
                    partner_segments=partner_segs,
                    prefer_planar=planar_tails,
                )
                if end_tail is not None:
                    a1_eff = a1 - extra1
                    break
            if end_tail is None:
                self._last_shadow_decline_reason = "blockage"  # #4459
                continue
            kept = _slice_kept(a0_eff, a1_eff)
            seg_elements = [e for e in kept if e[0] == "seg"]
            if not seg_elements:
                self._last_shadow_decline_reason = "blockage"  # #4459
                continue

            shadow_route = Route(net=s_net, net_name=s_net_name)
            shadow_route.segments.extend(
                Segment(
                    x1=s.x2,
                    y1=s.y2,
                    x2=s.x1,
                    y2=s.y1,
                    width=s.width,
                    layer=s.layer,
                    net=s.net,
                    net_name=s.net_name,
                )
                for s in reversed(start_tail.segments)
            )
            shadow_route.vias.extend(start_tail.vias)
            for e in kept:
                if e[0] == "via":
                    _, vx, vy, l0, l1 = e
                    shadow_route.vias.append(
                        Via(
                            x=vx,
                            y=vy,
                            drill=rules.via_drill,
                            diameter=rules.via_diameter,
                            layers=(
                                Layer(grid.index_to_layer(l0)),
                                Layer(grid.index_to_layer(l1)),
                            ),
                            net=s_net,
                            net_name=s_net_name,
                        )
                    )
                    continue
                _, x1, y1, x2, y2, li = e
                if math.hypot(x2 - x1, y2 - y1) < 1e-6:
                    continue
                shadow_route.segments.append(
                    Segment(
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        width=s_width,
                        layer=Layer(grid.index_to_layer(li)),
                        net=s_net,
                        net_name=s_net_name,
                    )
                )
            shadow_route.segments.extend(end_tail.segments)
            shadow_route.vias.extend(end_tail.vias)

            # Issue #3987 (unit 2a of #3921): make the assembled shadow
            # copper 45-compliant BY CONSTRUCTION.  The guide (P side, or N
            # when swapped) is the C++ on-grid router's output and is
            # already 45-aligned; the geometric shadow (miter apex, via
            # jogs, pad-approach tails) is the only off-angle source.  Run
            # the dogleg pass over the assembled shadow segments BEFORE the
            # self-check / overlap gates below, so the doglegged geometry is
            # what those gates -- and the downstream emission census
            # (#3975) -- validate.  A residual off-angle segment (no clear
            # dogleg variant) degrades gracefully; it is not silently
            # shipped as a short.
            # Issue #4462: close any residual break BEFORE the dogleg pass, so
            # the quantizer (which preserves each segment's own endpoints) is
            # working on an already-connected chain.
            chain_repairs = self._close_shadow_chain(shadow_route, pathfinder)
            pre_quant_len = _route_copper_length(shadow_route)
            self._quantize_shadow_segments(shadow_route, pathfinder)
            if _SHADOW_DEBUG and chain_repairs:
                print(
                    f"    [coupled-shadow-chain] {pair.name} side={side:+.0f} "
                    f"closed {chain_repairs} polyline break(s)"
                )
            if _SHADOW_DEBUG:
                guide_len = sum(math.hypot(g.x2 - g.x1, g.y2 - g.y1) for g in guide.segments)
                print(
                    f"    [coupled-shadow-len] {pair.name} side={side:+.0f} "
                    f"swap={swap_roles} guide={guide_len:.3f} "
                    f"shadow={_route_copper_length(shadow_route):.3f} "
                    f"(pre_quant={pre_quant_len:.3f}) "
                    f"body_raw={arc_total:.3f} body_kept={a1_eff - a0_eff:.3f} "
                    f"trim0={a0_eff:.3f} trim1={arc_total - a1_eff:.3f} "
                    f"tail0={_route_copper_length(start_tail):.3f} "
                    f"tail1={_route_copper_length(end_tail):.3f}"
                )

            guide_net_pad = spec.n_start if swap_roles else spec.p_start
            guide_route_obj = Route(net=guide_net_pad.net, net_name=guide_net_pad.net_name)
            guide_route_obj.segments.extend(guide.segments)
            guide_route_obj.vias.extend(guide.vias)

            # Issue #4570: VIA-SYMMETRY gate.  ``diffpair_length_skew`` measures
            # electrical length -- planar copper PLUS each via's drilled length
            # -- while ``_length_match_constructed_pair`` below is planar-only.
            # A pair whose legs carry different vias can therefore be driven to
            # a ~0.01 mm planar delta and still ship a multi-millimetre skew
            # violation (board-06 seed 42: 2 x 1.6 mm on PCIE_RX / USB3_TX1).
            # Gate here -- after assembly and chain repair, before the
            # clearance gates and before the planar length match -- so that
            # (a) remediation stays transactional with the rest of the side and
            # (b) the length match runs on the FINAL geometry.
            #
            # Two remediations, in cost order:
            #
            #   A. the tails were already asked for PLANAR routing
            #      (``planar_tails``, upstream) -- the cheapest fix, because it
            #      removes copper instead of adding it;
            #   B. when no planar tail is legal -- board-06's landing tails must
            #      CROSS the guide, so the two-via crossover is the only tail
            #      that exists -- the partner leg is given the SAME two vias, as
            #      a centreline-preserving z-jog that clears every gate other
            #      constructed copper clears.
            #
            # A side that is still asymmetric after both is REJECTED here and
            # fails over to the other offset side (``via_symmetric`` below
            # gates the ``return``).  Whether a pair with no symmetric side at
            # all is dropped entirely or kept as an explicit last resort is
            # ``_SHADOW_VIA_SYMMETRY_STRICT`` -- see the constant's comment for
            # the board-06 measurement behind that default.
            via_symmetric = True
            if _SHADOW_VIA_SYMMETRY:
                n_layers = getattr(grid, "num_layers", 0) or 2
                guide_sig = _route_via_signature(guide_route_obj, n_layers)
                shadow_sig = _route_via_signature(shadow_route, n_layers)
                if guide_sig != shadow_sig:
                    via_symmetric = _SHADOW_VIA_MIRROR and self._match_pair_via_signature(
                        guide_route_obj, shadow_route, pathfinder, n_layers
                    )
                    print(
                        f"    [coupled-shadow] side={side:+.0f} via-signature "
                        f"mismatch for {pair.name} (guide={guide_sig} "
                        f"shadow={shadow_sig}); "
                        f"{'mirrored' if via_symmetric else 'unrepaired'}"
                    )
                    if not via_symmetric:
                        self._last_shadow_decline_reason = "via-skew"  # #4570
                    if _SHADOW_DEBUG:
                        print(
                            f"    [coupled-shadow-via] {pair.name} "
                            f"tail0_vias={len(start_tail.vias)} "
                            f"tail1_vias={len(end_tail.vias)} "
                            f"body_vias={len([e for e in kept if e[0] == 'via'])} "
                            f"planar_tails={planar_tails}"
                        )

            # Issue #4553: make the pair length-symmetric BY CONSTRUCTION.
            # The parallel offset, the shadow's via jogs and its independently
            # synthesized landing tails leave the constructed shadow 0.8-6.8 mm
            # LONGER than the guide on board-06 -- a construction-time deficit
            # no bounded downstream trombone can absorb.  Close it here, while
            # the pair is still uncommitted and before the clearance gates
            # below run, by meandering the SHORTER leg away from its partner.
            # Bounded by ``_SHADOW_MEANDER_MAX_FRAC`` of that leg so a pair can
            # never become mostly-meander (the coupled fraction the
            # ``diffpair_routing_continuity`` rule measures stays healthy), and
            # every tooth is 45-census / grid / partner validated, so a pair
            # with no legal room simply stays skewed as it does today.
            if _SHADOW_LENGTH_MATCH:
                self._length_match_constructed_pair(
                    pair,
                    guide_route_obj,
                    shadow_route,
                    pathfinder,
                    intra_floor + s_width / 2.0 + guide_width / 2.0,
                )

            if swap_roles:
                p_route, n_route = shadow_route, guide_route_obj
            else:
                p_route, n_route = guide_route_obj, shadow_route

            # Issue #3508: in-loop severity self-check (the same metric
            # as the caller's #3320 gate).  Tails are routed without
            # partner awareness, so a wrong-side body forces the tail
            # to cross the guide; instead of letting the caller's gate
            # reject the whole pair, fail THIS side over to the other.
            net_class_map = getattr(self.autorouter, "net_class_map", None) or {}
            nc = net_class_map.get(spec.p_start.net_name)
            threshold = (
                nc.effective_intra_pair_clearance()
                if nc is not None
                else self.autorouter.rules.trace_clearance
            )
            violation = find_intra_pair_clearance_violations(
                p_route, n_route, threshold_mm=threshold, pair_name=pair.name
            )
            if violation is not None and violation.actual_clearance_mm < 0.0:
                print(
                    f"    [coupled-shadow] side={side:+.0f} self-check overlap "
                    f"for {pair.name} "
                    f"(worst={violation.actual_clearance_mm:+.3f}mm); trying "
                    f"other side"
                )
                self._last_shadow_decline_reason = "overlap"  # #4459
                if _SHADOW_DEBUG:
                    self._debug_shadow_overlap(
                        pair.name,
                        side,
                        violation,
                        shadow_route,
                        guide,
                        len(start_tail.segments),
                        len([e for e in kept if e[0] == "seg"]),
                        len(end_tail.segments),
                        swap_roles,
                    )
                # Issue #4460 (approach 2): record WHERE the offset overlapped
                # the guide so a guide re-route can be biased away from it.  The
                # midpoint of the two offending segments localises the pinch;
                # ``_shadow_with_guide_bias`` boosts the A* cost there.
                vp, vn = violation.p_segment, violation.n_segment
                self._last_shadow_overlap_locations.append(
                    (
                        (vp.x1 + vp.x2 + vn.x1 + vn.x2) / 4.0,
                        (vp.y1 + vp.y2 + vn.y1 + vn.y2) / 4.0,
                    )
                )
                continue
            # Issue #3508 (second pass): full physical-overlap check
            # (via-vs-seg / via-vs-via / seg-vs-seg) mirroring the
            # recipe's 6b rip detector -- the segments-only check above
            # cannot see a crossing-tail via overlapping the partner.
            if self._pair_has_physical_overlap(p_route, n_route):
                print(
                    f"    [coupled-shadow] side={side:+.0f} physical "
                    f"P/N overlap (via-aware) for {pair.name}; trying "
                    f"other side"
                )
                self._last_shadow_decline_reason = "overlap"  # #4459
                continue
            # Issue #4571 (third pass): FOREIGN-PAD clearance self-check.  The
            # two gates above compare copper to copper only -- segment to
            # segment and via to segment/via -- so neither can see a landing
            # tail drawn across the sibling pin's PAD (the board-06 FFC case:
            # MIPI_CLK- copper over MIPI_D0+/MIPI_D0- pads at 0.037 mm and over
            # its own partner MIPI_CLK+'s pads at 0.015 mm).  The exact
            # ``clearance_pad_segment``-equivalent primitives close that
            # quadrant here, at the same "fail this side over to the other"
            # point, using ``exclude_net`` only so the partner's pads on a
            # shared fine-pitch connector ref are never carve-out-exempted.
            #
            # Only the CONSTRUCTED leg is gated: the guide is ordinary
            # single-ended copper produced (and validated) by the per-net
            # router, and it keeps the single-ended finalization backstops.
            # Declining a side over a pre-existing guide graze would cost
            # coupling without fixing anything the constructor introduced.
            pad_deficit, pad_loc = self._route_pad_violation(shadow_route)
            if pad_deficit > _SHADOW_PAD_DEFICIT_EPS:
                loc_str = f" near ({pad_loc[0]:.3f}, {pad_loc[1]:.3f})" if pad_loc else ""
                print(
                    f"    [coupled-shadow] side={side:+.0f} foreign-pad "
                    f"clearance deficit {pad_deficit:.3f}mm{loc_str} for "
                    f"{pair.name}; trying other side"
                )
                self._last_shadow_decline_reason = "pad-clearance"  # #4571
                continue
            # Issue #4575 (fourth pass): FOREIGN-VIA clearance self-check, the
            # quadrant adjacent to #4571's.  ``_pair_has_physical_overlap``
            # above does look at barrels, but only at the bodies-intersect
            # threshold (``via_r + seg_w/2``, no clearance term) -- so a
            # constructed leg passing 0.090 mm from the partner's barrel is
            # "clean" to it and is then reported by ``clearance_segment_via``
            # at 0.102 mm (board-06 seed 42, USB3_TX1+ vs USB3_TX1-).  The
            # exact predicates close it here, at the same failover point.
            #
            # Both directions of the P/N interaction are covered by measuring
            # the CONSTRUCTED leg alone, because the guide is registered as
            # foreign copper: shadow segments vs guide barrels, and shadow
            # barrels vs guide segments/barrels.  The guide leg is never gated
            # against THIRD-PARTY copper (the #4571 rationale above).
            #
            # The universe is re-armed on ``guide_route_obj`` -- the FINAL
            # guide geometry -- rather than reusing the ambient snapshot: the
            # #4570 via mirror and the #4553 length matcher both mutate the
            # guide leg after that snapshot was taken, and it was exactly one
            # of those late guide-leg meander teeth that grazed the shadow's
            # landing barrel at 0.090 mm on board-06 seed 42.
            with self._shadow_foreign_copper(guide_route_obj):
                via_deficit, via_loc = self._route_via_violation(shadow_route)
            if via_deficit > _SHADOW_VIA_DEFICIT_EPS:
                loc_str = f" near ({via_loc[0]:.3f}, {via_loc[1]:.3f})" if via_loc else ""
                print(
                    f"    [coupled-shadow] side={side:+.0f} foreign-via "
                    f"clearance deficit {via_deficit:.3f}mm{loc_str} for "
                    f"{pair.name}; trying other side"
                )
                self._last_shadow_decline_reason = "via-clearance"  # #4575
                continue
            if not via_symmetric:
                # Issue #4570: this side is otherwise legal but its two legs
                # carry different vias.  Hold it back so the OTHER offset side
                # gets its chance to produce a symmetric pair;
                # ``asym_fallback`` keeps it as the explicit last resort
                # described just below the loop.
                if asym_fallback is None:
                    asym_fallback = (p_route, n_route)
                continue
            return p_route, n_route

        # Issue #4570: no side of THIS guide is via-symmetric.  Take the
        # asymmetric candidate here rather than after the uncompressed-guide
        # retry below: the retry re-derives the whole body from a different
        # corridor, and on board-06 letting it displace an already-legal
        # candidate moved PCIE_RX onto geometry with 7 extra
        # ``clearance_segment_via`` violations and a hole-to-hole failure.
        # Symmetry is worth preferring a SIDE for; it is not worth trading a
        # different pair's clearance for.
        if asym_fallback is not None and not _SHADOW_VIA_SYMMETRY_STRICT:
            print(
                f"    [coupled-shadow] {pair.name} WARNING: shipping a "
                f"via-ASYMMETRIC pair (no symmetric side and no legal mirror); "
                f"expect a diffpair_length_skew error of the vias' drilled "
                f"length -- issue #4570"
            )
            return asym_fallback

        # Issue #4553: both offset sides declined on the COMPRESSED guide.
        # Retry once on the original per-cell polyline so the construction
        # rate can only ever improve -- the compressed guide is a different
        # (usually easier, occasionally tighter) corridor, never a mandate.
        if guide_was_simplified:
            retry = self._shadow_route_pair(
                pair,
                spec,
                pathfinder,
                raw_guide,
                spacing_cells,
                swap_roles=swap_roles,
                simplify_guide=False,
            )
            if retry is not None:
                return retry

        # Issue #4570: ``STRICT`` reaches here with an asymmetric candidate in
        # hand and DROPS it (the electrically pure answer: an unmatched via is
        # a mode-conversion and impedance discontinuity, not merely a length
        # error).  See ``_SHADOW_VIA_SYMMETRY_STRICT`` for why that is not the
        # default.
        if asym_fallback is not None and _SHADOW_VIA_SYMMETRY_STRICT:
            print(
                f"    [coupled-shadow] {pair.name} declined: no offset side "
                f"is via-symmetric (KCT_SHADOW_VIA_SYMMETRY_STRICT=1)"
            )
        return None

    def _rescue_near_miss_coupled(
        self,
        pair: DifferentialPair,
        spec: CoupledSegmentSpec,
        pathfinder: CoupledPathfinder,
    ) -> tuple[Route, Route] | None:
        """Complete a budget-exited coupled search that stalled near goal.

        Issue #3508: reconstructs the partial coupled route up to the
        search's best state (``pathfinder.last_best_node``) and routes
        the two remaining head->pad tails with the single-ended per-net
        router.  Returns ``(p_route, n_route)`` with the tails merged
        in, or ``None`` when either tail cannot be routed (callers then
        fall through to the legacy budget-exit handling).

        Nothing is committed to the grid here -- the caller runs the
        returned routes through the same #3320 severe-overlap gate and
        commit path as a normally-converged coupled result, so a rescue
        that produced crossing tails is rejected transactionally.
        """
        best = pathfinder.last_best_node
        if best is None:
            return None

        grid = self.autorouter.grid
        p_pos = best.state.p_pos
        n_pos = best.state.n_pos
        p_wx, p_wy = grid.grid_to_world(p_pos.x, p_pos.y)
        n_wx, n_wy = grid.grid_to_world(n_pos.x, n_pos.y)

        p_head = self._virtual_pad_at(spec.p_end, p_wx, p_wy, p_pos.layer)
        n_head = self._virtual_pad_at(spec.n_end, n_wx, n_wy, n_pos.layer)

        try:
            p_route, n_route = pathfinder._reconstruct_coupled_routes(
                best, spec.p_start, p_head, spec.n_start, n_head
            )
        except Exception as exc:  # pragma: no cover - defensive
            print(f"    [coupled-rescue] reconstruction failed for {pair.name}: {exc}")
            return None

        p_tail = self._tail_route(pathfinder, p_head, spec.p_end, p_pos.layer, "P", pair.name)
        if p_tail is None:
            return None
        n_tail = self._tail_route(pathfinder, n_head, spec.n_end, n_pos.layer, "N", pair.name)
        if n_tail is None:
            return None

        p_route.segments.extend(p_tail.segments)
        p_route.vias.extend(p_tail.vias)
        n_route.segments.extend(n_tail.segments)
        n_route.vias.extend(n_tail.vias)
        return p_route, n_route

    def _single_ended_guide_route(
        self,
        start_pad: Pad,
        end_pad: Pad,
        per_net_timeout: float | None = None,
        avoid_locations: list[tuple[float, float]] | None = None,
        avoid_radius_cells: int = 1,
    ) -> Route | None:
        """Route one side of a pair single-ended to seed a corridor mask.

        Issue #3439: the corridor-bounded coupled search needs a
        known-routable spatial path to dilate.  We use the autorouter's
        standard per-net pathfinder (C++-accelerated when available,
        10-100x faster than the pure-Python coupled A*) to find the
        P-side path.  The returned route is NOT committed to the grid
        or the route list -- it exists only to bound the coupled
        search's state space and is discarded afterwards.

        Issue #3473 (review of #3439): the probe is bounded by
        ``per_net_timeout``.  It is only a guide route -- if the
        single-ended path cannot be found quickly, the corridor
        attempt is skipped and the legacy open search gets the budget
        instead.  Without this, a hard P side (C++ search fails ->
        unbounded Python fallback) consumed nearly the whole per-pair
        budget on board 06 BEFORE either coupled attempt ran, leaving
        the open fallback just the 1.0s floor.

        Returns ``None`` when no single-ended path exists within the
        deadline (in which case the caller falls back to the
        unconstrained coupled search) or when the pathfinder raises.
        """
        try:
            router = self.autorouter.router
            # Issue #4460 (approach 2): when the caller has identified where a
            # PRIOR guide self-approached within the shadow-offset clearance
            # (a non-adjacent polyline loop-back the #4490 per-vertex miter
            # cannot fix), boost the A* cost around those locations so this
            # re-route steers the guide's own legs apart and leaves the
            # parallel offset room.  The costs are transient -- the ``finally``
            # clause below clears them, so a boosted probe cannot leak
            # cost-shaping into later searches.  With ``avoid_locations`` unset
            # (the default and the only path when shadow construction is off)
            # this is a no-op and the probe is byte-identical to before.
            if avoid_locations and hasattr(router, "_boost_avoidance_at"):
                # ``_boost_avoidance_at`` adds a fixed cost per call over a
                # ``3 * radius_cells`` neighbourhood; repeat it to accumulate a
                # penalty strong enough to divert the A* out of a congested
                # pinch (a single pass is too weak on board 06's escape fan).
                for loc in avoid_locations:
                    for _ in range(_GUIDE_BIAS_BOOST_REPEATS):
                        router._boost_avoidance_at(loc, avoid_radius_cells)
            return router.route(start_pad, end_pad, per_net_timeout=per_net_timeout)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "corridor guide route raised for %r -> %r: %s",
                start_pad.net_name,
                end_pad.net_name,
                exc,
            )
            return None
        finally:
            # Issue #3473 (judge note on #3439): the C++ backend's
            # validation-failure path mutates persistent avoidance
            # costs (``_boost_avoidance_at``); normally cleared in
            # ``Autorouter._route_net``, which this probe bypasses.
            # Clear them here so a failed probe cannot leak
            # cost-shaping into subsequent searches.
            router = self.autorouter.router
            if hasattr(router, "clear_avoidance_costs"):
                router.clear_avoidance_costs()

    def route_differential_pair_coupled(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
        coupled_only: bool = False,
        extra_spacing_cells: int = 0,
        per_pair_timeout: float | None = None,
        per_pair_max_iterations: int | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair using coupled pathfinding.

        Routes both P and N traces simultaneously while maintaining
        constant spacing between them.

        Args:
            pair: The differential pair to route.
            spacing: Optional spacing override.
            coupled_only: Issue #2464: When True, do not fall back to
                independent routing if the coupled pathfinder cannot
                handle the pair (e.g., 3-pad nets, no path found).
                Returns ``([], None)`` instead.  Used by the diff-pair
                pre-pass so that pairs that cannot be coupled are left
                for the main strategy to route normally.
            extra_spacing_cells: Issue #3040 Phase B: additional grid
                cells to add to both the target ``spacing_cells`` and
                ``min_spacing_cells`` floor passed to the
                :class:`CoupledPathfinder`.  Used by the Phase B repair
                pass to widen the search's spacing target on retry when
                the first attempt produced an intra-pair clearance
                violation due to grid quantisation.  Each additional
                cell adds one ``grid.resolution`` of edge-to-edge
                separation, which is normally enough to push the
                routed clearance above the per-pair threshold.
                Default ``0`` preserves legacy behaviour.
            per_pair_timeout: Issue #3089: Optional wall-clock budget
                (seconds) passed through to
                :meth:`CoupledPathfinder.route_coupled` for each
                coupled-segment search this pair triggers.  ``None``
                preserves the legacy unbounded behaviour.  When the
                budget is exceeded the coupled search returns ``None``
                and this method falls through to the same
                "coupled routing failed" handler used for genuine
                no-path-found results (independent routing fallback
                when ``coupled_only=False``; ``([], None)`` return
                otherwise), so callers do not need a separate
                code-path for budget exits.
        """
        # Issue #3089: reset the budget-exit flag at the start of each
        # call so callers see only the most-recent invocation's state.
        self._last_pair_budget_exit = False

        if pair.rules is None:
            return [], None

        if spacing is None:
            spacing = pair.rules.spacing

        print(f"\n  Routing differential pair {pair} (coupled mode)")
        print(f"    Type: {pair.pair_type.value}")
        print(f"    Spacing: {spacing}mm, Max delta: {pair.rules.max_length_delta}mm")

        # Get pads
        pad_result = self._get_pair_pads(pair)
        if pad_result is None:
            print("    ERROR: Could not find pads for differential pair")
            return [], None

        p_pads, n_pads = pad_result

        # Issue #2473: Pair pads using the MST-based N-pad helper.
        # For 2-pad nets this still produces a single coupled segment
        # with no stubs; for 3+ pad nets (USB-C) it returns one or
        # more coupled segments plus the intra-cluster stub edges that
        # the independent router will handle after the coupled pass.
        if len(p_pads) == 2 and len(n_pads) == 2:
            # Backward-compatible fast path.
            legacy = self._pair_pads_for_coupled_routing(p_pads, n_pads)
            coupled_specs = []
            for ps, pe, ns, ne in legacy:
                # Issue #3012: detect polarity-swap in the 2-pad fast
                # path.  ``_pair_pads_for_coupled_routing`` returns the
                # pair ordered by start-pad proximity, but the *end*
                # pads can still flip polarity (board-07 DDR test
                # footprint inverts P/N row positions between the two
                # QFNs).  Without this detection the coupled search
                # tries to maintain constant spacing on a path whose
                # endpoints sit in mirror orientations -- impossible
                # without a swap-via -- and the search collapses the
                # spacing to 0 mid-run instead.  Mirrors the existing
                # detection in the npad path (line 1424).
                polarity_swap = self._polarity_swap_between(ps, ns, pe, ne)
                coupled_specs.append(
                    CoupledSegmentSpec(
                        p_start=ps,
                        p_end=pe,
                        n_start=ns,
                        n_end=ne,
                        polarity_swap=polarity_swap,
                    )
                )
            stub_specs: list[StubEdgeSpec] = []
        else:
            coupled_specs, stub_specs = self._pair_pads_for_coupled_routing_npad(p_pads, n_pads)

        if not coupled_specs:
            if coupled_only:
                print(
                    "    Skipping diff-pair pre-pass: complex pad configuration "
                    "(coupled pathfinder could not pair pads)"
                )
                return [], None
            print("    WARNING: Complex pad configuration, falling back to independent routing")
            return self.route_differential_pair_independent(pair, spacing)

        # Issue #3012: Calculate the effective spacing.  The legacy
        # behaviour used ``int(spacing / resolution)`` where ``spacing``
        # came from the per-type ``DifferentialPairRules.spacing``
        # default (0.15-0.2 mm) -- which is an EDGE-TO-EDGE clearance,
        # not a CENTER-TO-CENTER target.  When the pair's
        # ``NetClassRouting`` declares a richer ``intra_pair_clearance``
        # (board 07: 0.1 mm with 0.15 mm trace width), we derive the
        # center-to-center floor as ``trace_width + intra_pair_clearance``
        # and feed that as the spacing target so the search lays the
        # centerlines far enough apart for the partner-edge clearance to
        # hold post-route.  Use ``math.ceil`` instead of ``int`` so we
        # never round DOWN below the threshold.
        net_class_map = self.autorouter.net_class_map or {}
        pair_net_class = net_class_map.get(pair.positive.net_name)
        if pair_net_class is not None:
            pair_trace_width = float(pair_net_class.trace_width)
            pair_intra_clearance = float(pair_net_class.effective_intra_pair_clearance())
        else:
            pair_trace_width = float(self.autorouter.rules.trace_width)
            # Without a per-pair class, fall back to the legacy edge-to-
            # edge ``pair.rules.spacing`` interpretation by treating it
            # as the intra clearance.  This preserves the pre-#3012
            # behaviour for callers that don't supply a net_class_map.
            pair_intra_clearance = float(spacing)

        # Issue #4052: clamp the impedance-coupling gap out of the coupled
        # spacing target.  When a net class carries a ``target_diff_impedance``
        # the impedance resolver (``diffpair_impedance.py:524``) OVERWRITES
        # ``intra_pair_clearance`` with the physics-derived edge-to-edge
        # coupling gap needed to hit that impedance on the board's stackup
        # (board 07: 8.425 mm for loosely-coupled 100 ohm on a thick
        # 4-layer stack).  That gap is a *stackup impedance* quantity, NOT
        # a within-pair spacing floor: fed straight into the coupled
        # search's ``min_spacing_cells`` it demands the two centerlines sit
        # ~8.6 mm apart (87 cells at 0.1 mm), so every move from the
        # physical pad pitch (~1 mm) is rejected by the spacing floor and
        # the joint-state search dies at the start state in 4 iterations
        # (measured: ALL board-07 coupled pairs, ``sym_floor`` /
        # ``asym_floor_p`` / ``asym_floor_n`` rejections only,
        # ``best_progress`` never improving).
        #
        # Clamp is gated on ``target_diff_impedance`` being set -- that is
        # the SIGNAL that ``intra_pair_clearance`` was overwritten with a
        # stackup gap (``diffpair_impedance.resolve_impedance_driven_sizing``
        # only replaces the field when a target impedance drove the sizing,
        # ``used_target=True``).  A pair that legitimately DECLARES a wider
        # ``intra_pair_clearance`` (the #3012 case: board 07's 0.1 mm
        # within-pair clearance with a 0.15 mm trace) has no
        # ``target_diff_impedance`` and is left untouched, so its floor
        # still holds the declared within-pair separation post-route.  When
        # gated, clamp to the geometric ``trace_clearance`` floor (the
        # tightest DRC-legal within-pair separation), mirroring the
        # match-group tuner's identical impedance-gap mis-read fix in #3440.
        if getattr(pair_net_class, "target_diff_impedance", None) is not None:
            within_pair_clearance_floor = float(self.autorouter.rules.trace_clearance)
            pair_intra_clearance = min(pair_intra_clearance, within_pair_clearance_floor)

        required_center_spacing = pair_trace_width + pair_intra_clearance
        min_spacing_cells = max(
            1, math.ceil(required_center_spacing / self.autorouter.grid.resolution)
        )

        # Target spacing in grid cells.  Use the larger of the legacy
        # ``spacing/resolution`` value (which historically governed) and
        # the new floor so wider edge-to-edge targets still win when
        # set, and the floor never under-counts.
        legacy_spacing_cells = math.ceil(spacing / self.autorouter.grid.resolution)
        spacing_cells = max(legacy_spacing_cells, min_spacing_cells)

        # Issue #3040 Phase B: widen both the floor and the target by
        # the caller-supplied ``extra_spacing_cells`` on retry attempts.
        # Each additional cell maps to one ``grid.resolution`` of
        # additional center-to-center spacing, which directly translates
        # to that much extra edge-to-edge clearance once the route is
        # quantised back to world coordinates.
        if extra_spacing_cells > 0:
            min_spacing_cells += extra_spacing_cells
            spacing_cells += extra_spacing_cells

        # If any segment requires polarity-swap, enable swap-via moves.
        #
        # Issue #3508: swap-via moves are DISABLED.  The swap move
        # exchanges the two heads' exact grid positions onto one shared
        # new layer, so reconstruction emits the SAME A->B segment for
        # both nets (P: A->B, N: B->A) -- coincident copper, i.e. a
        # short.  Every swap-containing result was therefore rejected
        # by the #3320 severe-overlap gate at exactly
        # ``-trace_width`` (board 06 PCIE/USB3, board 07 DQS -- the
        # "swap-overlap gate" rejection documented in #3473).  With
        # mid-route asymmetric moves now enabled (this issue), a
        # polarity swap is achievable WITHOUT vias: the advancing
        # trace walks a discrete arc around its holding partner
        # (offset-vector rotation through 180 degrees), which the
        # trail-proximity guard keeps clearance-legal.  Re-enable only
        # after the swap reconstruction emits a genuine two-layer
        # crossover (staggered vias, partner segments on different
        # layers).
        any_polarity_swap = any(s.polarity_swap for s in coupled_specs)
        del any_polarity_swap  # documented above; swap moves disabled

        # Create coupled pathfinder.
        # Issue #3508: heuristic_weight > 1 (weighted A*) -- without it
        # the joint-state search floods cost_turn-deep f-plateaus
        # (~90k iterations for ONE 5-point shell on board 06) and no
        # CI-affordable iteration budget converges.  See the
        # ``heuristic_weight`` rationale in ``CoupledPathfinder``.
        #
        # Issue #3547: the weighted-A* search upgrade is gated behind
        # ``enable_shadow_construction``.  Weighting the heuristic changes
        # WHICH joint states the always-running coupled pre-phase explores
        # (goal-ward gradient dominates shell-flooding), so a search that
        # DEFERRED on the pre-#3508 baseline can CONVERGE with the flag
        # off -- committing a route where main deferred re-exposes the
        # gated hazards (#3542 corridor competition, #3544 pre-phase
        # seg-seg violations).  With the shadow constructor disabled
        # (default) fall back to classic optimal A* (``heuristic_weight=
        # 1.0``), the pre-#3508 search behaviour, so a flag-off run keeps
        # recipes on their pre-#3508 budget-exit path.
        coupled_heuristic_weight = (
            COUPLED_HEURISTIC_WEIGHT if self.enable_shadow_construction else 1.0
        )

        # Issue #3547: bound the flag-off classic-A* search so it DEFERS
        # promptly instead of grinding the ``cols * rows * 4`` memory
        # backstop.  With the shadow constructor OFF (default) the search
        # uses ``heuristic_weight=1.0`` (classic optimal A*), which on a
        # deferring fixture explores ~2x the joint states the weighted
        # search (#3508) did, pushing existing flag-off tests to the CI
        # 60s timeout (Judge note on #3547).  The flag-off contract is
        # "may only DEFER", so when the caller plumbed no explicit
        # iteration budget we supply ``COUPLED_FLAGOFF_MAX_ITERATIONS``:
        # fast-converging pairs (boards 03/06's open search) finish well
        # within it, while a deferring pair bails (sets
        # ``last_timeout_exceeded`` -> independent fallback) instead of
        # running the full optimal search to ~60s.  Flag-ON is UNCHANGED:
        # the shadow path keeps whatever budget the caller plumbed (the
        # re-route gate's per-pair budget), so this default never narrows
        # an opt-in run.  An explicit ``per_pair_max_iterations`` (board
        # configs, the re-route gate) always takes precedence.
        #
        # Issue #3921 (investigation): the curation comment proposed
        # raising this flag-off default to a FLOOR so board 06's explicit
        # ``per_pair_max_iterations=2000`` would be lifted to 40000.  That
        # was VERIFIED against the actual seed-42 bench and does NOT
        # restore convergence: at 20000 iters/phase the joint search's
        # best-progress plateaus identically to the 1000-iter run
        # (398->398, 61->64 cells from goal) while wall-time balloons
        # 562s -> >600s.  The reason is the ``heuristic_weight`` note
        # above: classic optimal A* (weight=1.0, the flag-off search)
        # floods cost_turn f-plateaus and "no CI-affordable iteration
        # budget converges".  The historical 6/9 convergence came from the
        # geometric SHADOW CONSTRUCTOR (``enable_shadow_construction=
        # True``), not the joint A* search -- so a budget floor is the
        # wrong lever and was dropped.  See the #3921 PR body for the
        # three-way measurement (floor / weighted / shadow).
        if (
            not self.enable_shadow_construction
            and (per_pair_max_iterations is None or per_pair_max_iterations <= 0)
            and COUPLED_FLAGOFF_MAX_ITERATIONS > 0
        ):
            per_pair_max_iterations = COUPLED_FLAGOFF_MAX_ITERATIONS

        pathfinder = CoupledPathfinder(
            self.autorouter.grid,
            self.autorouter.rules,
            spacing_cells,
            net_class_map=self.autorouter.net_class_map,
            allow_swap_via=False,  # Issue #3508: see rationale above
            min_spacing_cells=min_spacing_cells,
            heuristic_weight=coupled_heuristic_weight,
        )

        routes: list[Route] = []
        p_routes: list[Route] = []
        n_routes: list[Route] = []

        for spec in coupled_specs:
            polarity_marker = " (polarity-swap)" if spec.polarity_swap else ""
            print(
                f"    Routing {pair.positive.net_name}/{pair.negative.net_name}{polarity_marker}..."
            )

            # Issue #3439: corridor-bounded attempt first.  The open
            # joint-state coupled A* is pure Python and intractable on
            # large boards (~14k iterations/min on board 07's 4-layer
            # 110x95mm grid -- every pair blew its 60s budget).  Route
            # the P side single-ended via the (C++-accelerated) per-net
            # pathfinder, dilate its path into a spatial corridor, and
            # run the coupled search restricted to that corridor.  This
            # converts the open 2D product-space search into a near-1D
            # one that completes in seconds.  The guide route is NEVER
            # committed to the grid.  When the corridor attempt fails
            # (no guide path, corridor too tight for two traces, or
            # corridor budget exceeded) we fall back to the legacy
            # unconstrained search with the remaining per-pair budget,
            # preserving behaviour on boards where the open search
            # already converged (boards 03/06).
            spec_t0 = time.monotonic()
            result: tuple[Route, Route] | None = None
            coupled_phase = "open"
            # Issue #4459: reset the shadow-decline reason for this spec so a
            # prior pair's decline cannot leak into this pair's per-pair
            # classification.  ``_shadow_route_pair`` sets it at each decline.
            self._last_shadow_decline_reason = None
            # Issue #4635: reset the #4580 census's budget credit for this spec.
            # Census time recorded OUTSIDE a spec (the escape-stub call site)
            # pressured no spec budget, so discarding it here is correct.  With
            # ``KCT_CROSSTAIL_CENSUS`` unset this stays exactly ``0.0`` for the
            # whole spec and every deadline below is bit-identical to pre-#4635.
            self._census_elapsed_s = 0.0
            # Issue #3473: iterations the corridor attempt consumed,
            # charged against the shared per-pair iteration budget so
            # the open fallback gets the REMAINDER (not a fresh full
            # budget -- the 4000+4000 double-spend on board 06).
            corridor_iterations_used = 0

            # Issue #3473: bound the probe.  It is only a guide route;
            # give it a small slice of the corridor half-budget (an
            # eighth of the per-pair budget, e.g. 7.5s of 60s).  If
            # the P side cannot be routed single-ended that quickly,
            # skip the corridor and hand the budget to the legacy
            # open search instead of burning it before either coupled
            # attempt runs.
            # Issue #3508: floor the probe budget at 45s (clamped to half
            # the per-pair budget) -- but ONLY when the shadow
            # constructor is enabled.  The #3473 eighth-of-budget bound
            # starved board 06's USB3 probes: their single-ended guide
            # routes need 30-37s (the C++ validation falls back to the
            # Python pathfinder on the J1 fan-out geometry), so at the
            # 60s per-pair budget the probe deadline (7.5s) always
            # fired, no corridor existed, and the USB3 pairs ran the
            # intractable open search only.  The expensive probe exists
            # to feed the shadow guide; with the shadow gated off
            # (default -- see ``enable_shadow_construction``) keep the
            # legacy eighth-of-budget bound so deferring pairs exit
            # quickly and the per-pair wall-clock matches the
            # pre-#3508 budget-exit behaviour (matters on slow 2-core
            # CI runners: 9 pairs x 45s probes is most of the re-route
            # gate's wall-clock budget).
            if per_pair_timeout is None:
                probe_timeout = None
            elif self.enable_shadow_construction:
                # Issue #3987 (unit 2a of #3921): a hard per-pair shadow
                # budget.  When shadow is ON the pair is shadow-or-uncoupled
                # (the joint-state fallback is gated OFF below), so the whole
                # coupled attempt must fit a small budget: cap the P guide
                # probe at ``_SHADOW_PER_PAIR_BUDGET_S`` (clamped to
                # ``per_pair_timeout``).  This bounds the >1200s #3986 tail
                # -- 6/9 failed-shadow pairs previously each burned a ~45s
                # probe before falling through to the flooded A*.
                probe_timeout = min(_SHADOW_PER_PAIR_BUDGET_S, per_pair_timeout)
            else:
                probe_timeout = per_pair_timeout * 0.125
            probe_t0 = time.monotonic()
            guide_route = self._single_ended_guide_route(
                spec.p_start, spec.p_end, per_net_timeout=probe_timeout
            )
            print(
                f"    [corridor-probe] guide_route="
                f"{'ok' if guide_route is not None and guide_route.segments else 'FAILED'} "
                f"elapsed={time.monotonic() - probe_t0:.2f}s "
                f"segments={len(guide_route.segments) if guide_route is not None else 0}"
            )
            # Issue #3508: geometric shadow construction FIRST.  When
            # the guide exists, building N as a validated parallel
            # offset of the guide is deterministic, takes milliseconds,
            # and produces coupled geometry by construction -- the
            # joint-state search below is the fallback for guides the
            # shadow cannot legally parallel (e.g., via-bearing guides
            # or one-sided obstacle walls).
            #
            # OPT-IN (``self.enable_shadow_construction``, default
            # False): the constructor's committed geometry is not yet
            # artifact-quality -- see the field rationale on
            # ``DifferentialPairConfig.enable_shadow_construction``
            # for the board 06 run-4 measurements (stranded shadow
            # tails, via-on-partner intersections, corridor
            # competition stranding later single-ended nets).
            # Issue #4460 (approach 2): the P/N guides and the per-side overlap
            # pinch points from the normal shadow attempts, captured so a
            # guide-biased re-route can run as a LAST-resort fallback (after
            # both normal attempts fail) without displacing a working attempt
            # or spending its per-pair budget.
            n_guide: Route | None = None
            p_overlap_sites: list[tuple[float, float]] = []
            n_overlap_sites: list[tuple[float, float]] = []
            if self.enable_shadow_construction and guide_route is not None and guide_route.segments:
                shadow = self._shadow_route_pair(pair, spec, pathfinder, guide_route, spacing_cells)
                p_overlap_sites = list(self._last_shadow_overlap_locations)
                if shadow is not None:
                    result = shadow
                    coupled_phase = "shadow"
                    print("    [coupled-shadow] pair constructed as guide + parallel shadow")

            if (
                result is None
                and self.enable_shadow_construction
                and guide_route is not None
                and guide_route.segments
            ):
                # Issue #3508: role-swapped shadow retry.  P's guide may
                # carry vias or hug a one-sided obstacle wall; the N
                # side's single-ended route can be shadowable when P's
                # is not (board 06 MIPI_D0 / USB2_D: the P guide takes a
                # 2-via detour while the N guide is planar).  Gated on
                # the P probe having SUCCEEDED: when the P side cannot
                # be single-ended-routed within the probe budget at all,
                # the N side (same endpoints geometry) will not be
                # either, and the retry would just burn a second probe
                # budget per deferred pair.
                # Issue #3987: bound the N (swapped) probe by the REMAINDER
                # of the hard per-pair shadow budget so the two probes
                # together cannot exceed ``_SHADOW_PER_PAIR_BUDGET_S`` --
                # the fail-fast contract is per PAIR, not per probe.
                # Issue #4635: minus the #4580 census's incremental cost, so a
                # census-on run hands this probe the same deadline a census-off
                # run would (``0.0`` and therefore bit-identical when off).
                n_probe_timeout = probe_timeout
                if per_pair_timeout is not None and probe_timeout is not None:
                    n_probe_timeout = max(
                        0.5,
                        min(
                            probe_timeout,
                            _SHADOW_PER_PAIR_BUDGET_S
                            - (time.monotonic() - spec_t0 - self._census_elapsed_s),
                        ),
                    )
                n_guide = self._single_ended_guide_route(
                    spec.n_start, spec.n_end, per_net_timeout=n_probe_timeout
                )
                if n_guide is not None and n_guide.segments:
                    shadow = self._shadow_route_pair(
                        pair, spec, pathfinder, n_guide, spacing_cells, swap_roles=True
                    )
                    n_overlap_sites = list(self._last_shadow_overlap_locations)
                    if shadow is not None:
                        result = shadow
                        coupled_phase = "shadow-swapped"
                        print(
                            "    [coupled-shadow] pair constructed as N guide + parallel P shadow"
                        )

            # Issue #4460 (approach 2): guide-biased re-route, LAST resort.
            # Runs only when BOTH normal shadow attempts have failed, so it can
            # never displace or starve a working (swapped) construction.  It
            # re-routes the guide with the A* cost boosted at (a) any
            # non-adjacent guide self-approach and (b) the measured overlap
            # pinch points from the failed attempt, so the parallel offset gets
            # room, then retries the shadow.  Generic: any pair whose offset
            # pinched its guide benefits.  Fully gated on shadow construction,
            # so flag-off behaviour is unchanged.
            if (
                result is None
                and self.enable_shadow_construction
                and guide_route is not None
                and guide_route.segments
            ):
                # Bound the biased re-route probe by the REMAINING per-pair
                # shadow budget so this last-resort pass cannot extend the
                # per-pair wall-clock past ``_SHADOW_PER_PAIR_BUDGET_S`` (the
                # normal attempts may already have spent most of it).
                # Issue #4635: less the census's incremental cost (see above).
                bias_timeout = probe_timeout
                if per_pair_timeout is not None and probe_timeout is not None:
                    bias_timeout = max(
                        0.5,
                        min(
                            probe_timeout,
                            _SHADOW_PER_PAIR_BUDGET_S
                            - (time.monotonic() - spec_t0 - self._census_elapsed_s),
                        ),
                    )
                biased = self._shadow_with_guide_bias(
                    pair,
                    spec,
                    pathfinder,
                    guide_route,
                    spec.p_start,
                    spec.p_end,
                    spacing_cells,
                    False,
                    bias_timeout,
                    p_overlap_sites,
                )
                phase_label = "shadow-guide-biased"
                if biased is None and n_guide is not None and n_guide.segments:
                    biased = self._shadow_with_guide_bias(
                        pair,
                        spec,
                        pathfinder,
                        n_guide,
                        spec.n_start,
                        spec.n_end,
                        spacing_cells,
                        True,
                        bias_timeout,
                        n_overlap_sites,
                    )
                    phase_label = "shadow-swapped-guide-biased"
                if biased is not None:
                    result = biased
                    coupled_phase = phase_label
                    print(
                        "    [coupled-shadow] pair constructed via guide-biased "
                        f"re-route ({phase_label})"
                    )

            # Issue #3987 (unit 2a of #3921): when the shadow constructor is
            # ON, a pair is EITHER a validated parallel shadow (ms) OR it is
            # deferred to the uncoupled fallback.  It must NOT fall through
            # to the corridor / open joint-state A* below: those flood the
            # cost_turn f-plateaus (the #3954 bench disproved convergence at
            # 20x iterations) and the 6/9 failed-shadow pairs each burning a
            # corridor budget + the negotiated backstop is exactly the
            # >1200s tail the #3986 board-06 measurements documented.  A hard
            # per-pair shadow budget (``_SHADOW_PER_PAIR_BUDGET_S``) already
            # bounds the corridor probe + shadow construction above; here we
            # fail FAST to the uncoupled fallback -- shadow-or-uncoupled,
            # never shadow-then-flooded-A*.
            shadow_fail_fast = self.enable_shadow_construction
            if (
                result is None
                and not shadow_fail_fast
                and guide_route is not None
                and guide_route.segments
            ):
                grid = self.autorouter.grid
                resolution = grid.resolution
                start_spacing_cells = (
                    math.dist(
                        (spec.p_start.x, spec.p_start.y),
                        (spec.n_start.x, spec.n_start.y),
                    )
                    / resolution
                )
                end_spacing_cells = (
                    math.dist(
                        (spec.p_end.x, spec.p_end.y),
                        (spec.n_end.x, spec.n_end.y),
                    )
                    / resolution
                )
                # The corridor must admit the N trace alongside the
                # guide path at the WIDEST spacing the run will see
                # (start/end pad pitch can exceed the target), plus
                # maneuvering slack for local detours.
                corridor_radius = int(
                    math.ceil(max(spacing_cells, start_spacing_cells, end_spacing_cells))
                ) + max(6, spacing_cells)
                corridor = build_corridor_mask(
                    grid,
                    guide_route,
                    corridor_radius,
                    extra_cells=(
                        grid.world_to_grid(spec.p_start.x, spec.p_start.y),
                        grid.world_to_grid(spec.p_end.x, spec.p_end.y),
                        grid.world_to_grid(spec.n_start.x, spec.n_start.y),
                        grid.world_to_grid(spec.n_end.x, spec.n_end.y),
                    ),
                )
                # Half the per-pair budget for probe + corridor
                # attempt combined; the rest is reserved for the
                # open-search fallback so a corridor pathology can
                # never starve the legacy path entirely.  Issue #3473:
                # the probe's elapsed time is deducted from the
                # corridor half (it already counts against the pair
                # via ``spec_t0``), keeping probe+corridor <= ~50% of
                # the per-pair budget instead of 62.5%.
                # Issue #4635: the #4580 census's incremental cost is NOT the
                # probe's, so it is credited back here too.  This site is
                # reachable with the census ON and shadow construction OFF
                # (``_CROSSTAIL_CENSUS`` is independent of
                # ``enable_shadow_construction``), so leaving it uncorrected
                # would make the fix partial.
                corridor_budget: float | None = None
                if per_pair_timeout is not None:
                    corridor_budget = max(
                        0.5,
                        per_pair_timeout * 0.5
                        - (time.monotonic() - spec_t0 - self._census_elapsed_s),
                    )
                # Issue #3473: split the ITERATION budget the same way
                # as the wall-clock budget -- the corridor attempt gets
                # at most half, so a failing pair cannot spend the full
                # budget twice (4000 corridor + 4000 open on board 06).
                corridor_iteration_budget: int | None = None
                if per_pair_max_iterations is not None and per_pair_max_iterations > 0:
                    corridor_iteration_budget = max(1, per_pair_max_iterations // 2)
                result = pathfinder.route_coupled(
                    spec.p_start,
                    spec.p_end,
                    spec.n_start,
                    spec.n_end,
                    timeout_seconds=corridor_budget,
                    max_iterations_budget=corridor_iteration_budget,
                    corridor=corridor,
                )
                corridor_iterations_used = pathfinder.last_iterations
                if result is not None:
                    coupled_phase = "corridor"

            if result is None and not shadow_fail_fast:
                remaining_budget = per_pair_timeout
                if per_pair_timeout is not None:
                    # Issue #4635: same census credit as the corridor budget
                    # above -- this open fallback shares the ``spec_t0`` window.
                    remaining_budget = max(
                        1.0,
                        per_pair_timeout - (time.monotonic() - spec_t0 - self._census_elapsed_s),
                    )
                # Issue #3473: the open fallback gets the REMAINDER of
                # the shared iteration budget.  Because the corridor
                # attempt was capped at half, the fallback always
                # retains at least ~half -- mirroring the wall-clock
                # arithmetic above.
                remaining_iterations = per_pair_max_iterations
                if per_pair_max_iterations is not None and per_pair_max_iterations > 0:
                    remaining_iterations = max(
                        1, per_pair_max_iterations - corridor_iterations_used
                    )
                result = pathfinder.route_coupled(
                    spec.p_start,
                    spec.p_end,
                    spec.n_start,
                    spec.n_end,
                    timeout_seconds=remaining_budget,
                    max_iterations_budget=remaining_iterations,
                )

            # Issue #4635: deliberately NOT census-adjusted.  The deadlines
            # above credit the census's cost back so census-on and census-off
            # runs are comparable; this is the pair's TRUE wall clock, and
            # subtracting the census here would hide exactly the cost the credit
            # makes the pair spend beyond ``_SHADOW_PER_PAIR_BUDGET_S``.
            spec_elapsed = time.monotonic() - spec_t0
            logger.info(
                "diffpair coupled timing: pair=%r phase=%s elapsed=%.2fs success=%s",
                pair.name,
                coupled_phase,
                spec_elapsed,
                result is not None,
            )
            # Issue #3508: stdout visibility for the per-pair outcome.
            # The board recipes are print-based (INFO logging is not
            # configured), so without this line the only stdout signal
            # for a failing pair is the budget-exceeded warning -- the
            # corridor/open phase split and the iteration cost (the two
            # knobs recipe authors tune) were invisible in CI logs.
            # Issue #4459: kill the ``best_state=None`` red herring.  The C++
            # joint-state search (``_try_cpp_route_coupled``) carries no Python
            # ``CoupledState`` object, so ``last_best_state`` is genuinely
            # ``None`` on that path -- but the OLD line printed ``best_state=
            # None`` for EVERY C++ pair, implying "never moved" even when the
            # joint A* made real progress.  The true signal is
            # ``last_best_progress`` (the smallest joint remaining distance any
            # popped state reached) plus the dominant frontier-pruning reason.
            # Report ``n/a (cpp)`` on the C++ path so the line stops implying a
            # stall; keep the real state repr on the Python path.
            backend = getattr(pathfinder, "last_coupled_backend", "python")
            best_state = pathfinder.last_best_state
            if best_state is None and backend == "cpp":
                best_state_repr = "n/a (cpp)"
            else:
                best_state_repr = str(best_state)
            dominant = dominant_rejection(pathfinder.last_rejections)
            print(
                f"    [coupled-timing] phase={coupled_phase} "
                f"backend={backend} "
                f"elapsed={spec_elapsed:.2f}s "
                f"corridor_iters={corridor_iterations_used} "
                f"last_iters={pathfinder.last_iterations} "
                f"best_progress={pathfinder.last_best_progress} "
                f"best_state={best_state_repr} "
                f"dominant_rejection={dominant} "
                f"rejections={dict(pathfinder.last_rejections)} "
                f"success={result is not None}"
            )

            # Issue #4459: structured per-pair ground-truth report.  Classify
            # this attempt into the #4409 failure taxonomy so Phases 2-5 can
            # verify a fix targeted the class it claims.  Emitted for EVERY
            # pair (coupled or not) under both shadow-OFF (default) and
            # shadow-ON (``KCT_BOARD06_SHADOW=1``) so board-06's 9 pairs each
            # get a classification.  Diagnostic-only: building/printing this
            # record changes no routing behaviour or geometry.
            resolution = self.autorouter.grid.resolution
            start_pitch_cells = (
                math.dist((spec.p_start.x, spec.p_start.y), (spec.n_start.x, spec.n_start.y))
                / resolution
            )
            end_pitch_cells = (
                math.dist((spec.p_end.x, spec.p_end.y), (spec.n_end.x, spec.n_end.y)) / resolution
            )
            guide_ok = guide_route is not None and bool(guide_route.segments)
            shadow_decline_reason = (
                getattr(self, "_last_shadow_decline_reason", None)
                if self.enable_shadow_construction
                else None
            )
            classification = classify_coupled_pair_outcome(
                coupled=result is not None,
                coupled_phase=coupled_phase,
                guide_ok=guide_ok,
                best_progress=pathfinder.last_best_progress,
                shadow_enabled=self.enable_shadow_construction,
                shadow_decline_reason=shadow_decline_reason,
            )
            report = CoupledPairReport(
                pair_name=pair.name,
                classification=classification,
                coupled=result is not None,
                backend=backend,
                coupled_phase=coupled_phase,
                guide_ok=guide_ok,
                best_progress=pathfinder.last_best_progress,
                dominant_rejection=dominant,
                start_pitch_cells=start_pitch_cells,
                end_pitch_cells=end_pitch_cells,
                target_spacing_cells=spacing_cells,
                off_angle_segments=_count_off_angle_segments(guide_route),
                shadow_enabled=self.enable_shadow_construction,
            )
            self._last_coupled_pair_report = report
            print(report.format_line())

            # Issue #3508: near-miss rescue.  The weighted corridor-
            # bounded coupled search reliably traverses the route body
            # but stalls in the final pad-landing needle-eye: the heads
            # arrive within a few-hundred-micron Manhattan distance of
            # the goal pads, where interleaved foreign-pad clearance
            # halos leave only one runway per pad, the pair must
            # asymmetrically spread from the coupled spacing back to
            # the goal pad pitch inside that lattice, and the #3078
            # path-history guard turns every runway probe into a
            # dead-end (backing out retraces the head's own trail).
            # Measured on board 06: 8/9 pairs stall at best_progress
            # 5-21 cells after covering 95%+ of the route.  Rather
            # than make the joint search solve the landing, commit the
            # coupled body to the best state and finish each side with
            # the single-ended per-net router, which lands on pads
            # routinely.  The resulting tail (<= ~2 mm of a 30-50 mm
            # route) keeps the coupled-length fraction far above every
            # ``coupled_continuity_threshold`` in use (0.7-0.9).
            # Issue #3547: the near-miss rescue commits a coupled body +
            # single-ended tails for a search that DEFERRED on the
            # pre-#3508 baseline.  Committing where main deferred
            # re-exposes the exact hazards the gate exists to suppress
            # (#3542 corridor competition stranding singles, #3544
            # pre-phase copper seg-seg violations).  Gate the rescue on
            # ``enable_shadow_construction`` so a flag-off run never
            # invokes it -- the pre-phase may only defer, matching the
            # pre-#3508 budget-exit behaviour.
            if (
                self.enable_shadow_construction
                and result is None
                and pathfinder.last_best_node is not None
            ):
                if pathfinder.last_best_progress <= NEAR_MISS_RESCUE_CELLS:
                    rescue = self._rescue_near_miss_coupled(pair, spec, pathfinder)
                    if rescue is not None:
                        result = rescue
                        coupled_phase += "+rescue"
                        print(
                            f"    [coupled-rescue] completed pair via "
                            f"near-miss rescue (progress="
                            f"{pathfinder.last_best_progress} cells)"
                        )

            if result is None:
                # Issue #3089: ``None`` may indicate (a) no path found,
                # (b) max-iterations exhausted, or (c) the new per-pair
                # wall-clock budget was exceeded.  CoupledPathfinder.
                # route_coupled emits a structured ``logger.warning``
                # for case (c) and sets ``last_timeout_exceeded=True``.
                #
                # When the budget fired, do NOT attempt an independent-
                # routing fallback: the per-net A* on the same congested
                # BGA-49 escape geometry is the slowest single-net case
                # in the router (the per-net router has its own internal
                # timeout but it is much larger than the coupled
                # budget) and will blow the whole-run wall-clock budget.
                # Instead, surface a clean "skipped: budget exceeded"
                # diagnostic via the intra-clearance-violation buffer
                # (so Phase B's repair pass still sees a buffer entry)
                # and return ``([], None)`` so the main strategy picks
                # up these nets normally.  This mirrors the AC of
                # #3089: "with at least one pair surfacing a clean
                # 'skipped: budget exceeded' diagnostic and continuing".
                # Issue #3547: the "skip the independent fallback" exit
                # below exists to protect a caller-supplied WALL-CLOCK
                # budget (``per_pair_timeout``): the per-net A* on a
                # congested BGA-escape pair is the slowest single-net
                # case and would blow the whole-run budget.  But when the
                # only budget in force is the flag-off iteration default
                # (``COUPLED_FLAGOFF_MAX_ITERATIONS``, no
                # ``per_pair_timeout``), there is no whole-run wall-clock
                # contract to protect, and the pre-#3508 behaviour on a
                # deferring fixture was to fall through to the independent
                # fallback (the DQS-like polarity-swap test asserts this).
                # So only take the skip-fallback exit when a wall-clock
                # budget was actually plumbed; otherwise let the search
                # DEFER to the independent fallback below.
                if pathfinder.last_timeout_exceeded and per_pair_timeout is not None:
                    # Issue #3921: report WHICH budget actually fired.
                    # ``route_coupled`` raises ``last_timeout_exceeded``
                    # for both the iteration budget and the wall-clock
                    # budget, so the old message hard-coded the
                    # ``per_pair_timeout`` seconds ("budget exceeded
                    # (120s)") even when the iteration budget bailed the
                    # search in 0.3s.  ``last_iteration_limited``
                    # disambiguates; surface the actual iteration count
                    # and the per-phase split so the exit reason is not
                    # opaque.
                    if pathfinder.last_iteration_limited:
                        # ``per_pair_max_iterations`` is the total budget;
                        # the two-phase caller splits it ~half corridor /
                        # half open (see ``corridor_iteration_budget``).
                        total_budget = per_pair_max_iterations
                        phase_budget = (
                            max(1, total_budget // 2)
                            if total_budget is not None and total_budget > 0
                            else None
                        )
                        budget_desc = (
                            f"iteration budget exceeded "
                            f"({pathfinder.last_iterations} iters; "
                            f"phase cap {phase_budget}, total {total_budget}) "
                            f"in {spec_elapsed:.1f}s"
                            if phase_budget is not None
                            else f"iteration budget exceeded "
                            f"({pathfinder.last_iterations} iters) "
                            f"in {spec_elapsed:.1f}s"
                        )
                    else:
                        budget_desc = (
                            f"wall-clock budget exceeded "
                            f"({per_pair_timeout:.0f}s; "
                            f"{pathfinder.last_iterations} iters)"
                        )
                    print(
                        f"    WARNING: Coupled routing {budget_desc}; "
                        "skipping diff-pair and leaving nets for the "
                        "main strategy."
                    )
                    logger.warning(
                        "diffpair coupled-routing budget exceeded: pair=%r "
                        "p_net=%r n_net=%r reason=%s iters=%d "
                        "wall_budget=%.1fs elapsed=%.2fs",
                        pair.name,
                        pair.positive.net_name,
                        pair.negative.net_name,
                        "iteration" if pathfinder.last_iteration_limited else "wall-clock",
                        pathfinder.last_iterations,
                        float(per_pair_timeout) if per_pair_timeout else -1.0,
                        spec_elapsed,
                    )
                    self._last_pair_budget_exit = True
                    return [], None
                if coupled_only:
                    print("    Skipping diff-pair pre-pass: coupled pathfinder found no path")
                    return [], None
                print("    WARNING: Coupled routing failed, falling back to independent routing")
                return self.route_differential_pair_independent(pair, spacing)

            p_route, n_route = result

            # Issue #3320: Pre-mark intra-pair clearance audit.  Before
            # we commit the coupled-route to the grid and the route list,
            # check whether the reconstructed geometry actually meets the
            # per-pair clearance threshold.  A SEVERE violation
            # (centerlines overlap by more than ``trace_width / 2`` --
            # i.e., the partner trace's centerline lies on or inside our
            # trace's body) means the coupled search produced an
            # unrouteable swap-via / crossover geometry that the
            # ``min_spacing_cells`` floor (PR #3022) could not prevent.
            # The canonical failure mode is the board-07 DQS_N/DQS_P
            # pair: the polarity-swap-via at the U1 vias places both
            # traces with swapped y-coordinates on the same inner layer,
            # producing a long diagonal that crosses the partner's start
            # cell with -0.150 mm edge-to-edge clearance (the full trace
            # width).  When this happens we reject the coupled route,
            # do NOT commit it to the grid, and fall back to the
            # independent router which routes P and N as separate
            # single-ended nets -- a worse outcome for skew but a
            # routable one that doesn't produce shorting overlaps.
            violation = find_intra_pair_clearance_violations(
                p_route,
                n_route,
                threshold_mm=pair_intra_clearance,
                pair_name=pair.name,
            )
            # Severity gate: any actual centerline overlap (negative
            # edge-to-edge clearance) is "severe" -- the partner trace's
            # body literally intersects ours.  Pure quantization slack
            # (clearance in ``[0, threshold)``) is logged but kept
            # because the trace-optimizer / serpentine shim can still
            # nudge it into compliance.
            severe_violation = violation is not None and violation.actual_clearance_mm < 0.0
            # Issue #3508 (second pass): the segments-only check above
            # cannot see via-vs-segment / via-vs-via physical overlap
            # (e.g. a crossing-tail via on the partner's inner-layer
            # copper).  Treat those as severe too -- the recipe's 6b
            # repair would otherwise rip one side and de-couple the
            # pair downstream.
            if not severe_violation and self._pair_has_physical_overlap(p_route, n_route):
                print(
                    "    WARNING: Coupled route has via-aware physical "
                    "P/N overlap; rejecting coupled route."
                )
                severe_violation = True
                if violation is None:
                    # Synthesize nothing -- the handler below only needs
                    # the flag; guard its violation-specific logging.
                    pass
            if severe_violation:
                # Issue #3508: ``violation`` may be ``None`` when the
                # rejection came from the via-aware physical-overlap
                # check (no same-layer segment pair under threshold).
                worst = violation.actual_clearance_mm if violation is not None else float("nan")
                print(
                    f"    WARNING: Coupled route produced centerline overlap "
                    f"(worst={worst:+.3f}mm < 0); "
                    "rejecting coupled route and falling back to independent "
                    "routing."
                )
                if violation is not None:
                    logger.warning(
                        "diffpair coupled-route REJECTED due to centerline overlap: "
                        "pair=%r p_net=%r n_net=%r worst_clearance=%.4fmm "
                        "threshold=%.4fmm offending_segments=%d",
                        violation.pair_name,
                        violation.positive_net_name,
                        violation.negative_net_name,
                        violation.actual_clearance_mm,
                        violation.expected_clearance_mm,
                        len(violation.segment_violations),
                    )
                if _COUPLED_TRACE and violation is not None:
                    print(
                        f"      [overlap-debug] layer={violation.layer} "
                        f"p_seg=({violation.p_segment.x1:.2f},"
                        f"{violation.p_segment.y1:.2f})->"
                        f"({violation.p_segment.x2:.2f},{violation.p_segment.y2:.2f}) "
                        f"n_seg=({violation.n_segment.x1:.2f},"
                        f"{violation.n_segment.y1:.2f})->"
                        f"({violation.n_segment.x2:.2f},{violation.n_segment.y2:.2f})"
                    )
                # Do NOT commit p_route/n_route to grid or
                # ``autorouter.routes``.  Fall back to independent
                # routing for the whole pair (single source of truth
                # for the fallback path).  For the n-pad case where
                # earlier specs in this loop may already have committed
                # routes, unmark them and remove from the autorouter's
                # route list so the independent router starts from a
                # clean grid state for this pair.  ``coupled_only``
                # callers short-circuit out without a fallback -- they
                # will see the pair as unrouted and the negotiated
                # strategy picks it up on the main pass.
                for prev_route in routes:
                    with contextlib.suppress(Exception):
                        self.autorouter.grid.unmark_route(prev_route)
                    if prev_route in self.autorouter.routes:
                        self.autorouter.routes.remove(prev_route)
                if coupled_only:
                    print(
                        "    Skipping diff-pair pre-pass: coupled route "
                        "rejected (centerline overlap) and ``coupled_only`` "
                        "is set."
                    )
                    return [], None
                return self.route_differential_pair_independent(pair, spacing)

            # Mark routes on grid (use the unified helper that updates
            # both the Python and C++ grids — issue #1250).
            self.autorouter._mark_route(p_route)
            self.autorouter._mark_route(n_route)
            self.autorouter.routes.append(p_route)
            self.autorouter.routes.append(n_route)

            p_routes.append(p_route)
            n_routes.append(n_route)
            routes.extend([p_route, n_route])

            # Issue #3023 Phase A: per-spec intra-pair clearance audit.
            # The CoupledPathfinder's ``min_spacing_cells`` floor is a
            # center-to-center grid-cell count -- it does NOT guarantee
            # edge-to-edge clearance once the route is quantised back to
            # world coordinates (the 434-violation residual on board 07
            # is this quantisation gap).  Re-check the actual routed
            # segments against the per-pair threshold and emit a
            # diagnostic so Phase B (fine-grid repair, separate PR) can
            # rip-and-replace just the offenders.  No behavioural change
            # here -- detection only.  Note: severe overlaps that would
            # have triggered the #3320 rejection above never reach this
            # diagnostic because they fall back to independent routing.
            if violation is not None:
                logger.info(
                    "diffpair intra-clearance violation: pair=%r "
                    "p_net=%r n_net=%r threshold=%.4fmm "
                    "worst_actual=%.4fmm magnitude=%.4fmm "
                    "layer=%r offending_segments=%d",
                    violation.pair_name,
                    violation.positive_net_name,
                    violation.negative_net_name,
                    violation.expected_clearance_mm,
                    violation.actual_clearance_mm,
                    violation.violation_magnitude_mm,
                    violation.layer,
                    len(violation.segment_violations),
                )
                self._intra_clearance_violations.append(violation)

        # Issue #2473: Route stub edges (intra-net hops within a
        # cluster, e.g., USB-C A6 -> B6) using the independent router.
        # These are short, no coupling required, and the autorouter has
        # better access to obstacle-aware A* than the coupled pathfinder.
        #
        # Issue #3508: failed stub edges are recorded in
        # ``self._last_stub_failed_nets`` so the pre-pass aggregator can
        # leave the affected NET in the main strategy's routable set.
        # Previously the "deferred to main strategy" warning was a lie:
        # the net was still claimed as coupled-routed (#2464 reserve),
        # the negotiated loop skipped it, and the stub hop was never
        # routed (measured: USB2_D+ incomplete -> 18/21 reach; the
        # committed-artifact solution for A6->B6 is a ~12mm
        # under-connector wrap on In1.Cu that only the main strategy's
        # full A* can produce).
        self._last_stub_failed_nets: set[int] = set()
        if stub_specs:
            stub_routes = self._route_stub_edges(stub_specs)
            expected_per_net = collections.Counter(s.start.net for s in stub_specs)
            routed_per_net = collections.Counter(r.net for r in stub_routes)
            for stub_net, expected_count in expected_per_net.items():
                if routed_per_net.get(stub_net, 0) < expected_count:
                    self._last_stub_failed_nets.add(stub_net)
            for r in stub_routes:
                if r.net == pair.positive.net_id:
                    p_routes.append(r)
                elif r.net == pair.negative.net_id:
                    n_routes.append(r)
                routes.append(r)

        # Issue #3540: transactional pad-connectivity claim.  The shadow
        # constructor (and its rescue-tail / stub-edge machinery) can
        # commit copper that fails to actually REACH a goal pad -- a
        # parallel-offset tail that exhausts its anchor-stepping budget,
        # or a stub edge that the independent router could not land --
        # while the per-spec commit above has already marked that copper
        # on the grid.  Left as-is the caller claims the pair's nets
        # (#2464 reserve), the negotiated main strategy skips them, and
        # the stranded pads are unreachable for the rest of the pipeline
        # (measured board 06 run-4: USB3_RX1+/USB3_RX2+ shipped "1 of 2
        # pads stranded" with no warning).  A pair that claims-but-strands
        # costs REACH; a pair that defers cleanly costs only QUALITY --
        # and reach is the asserted contract.  So before returning the
        # pair's routes (which is what the caller turns into a net claim),
        # verify every pad of BOTH nets is in a single connected component
        # of the committed copper.  On any gap, rip the pair's copper off
        # the grid + route list and defer the WHOLE pair: return
        # ``([], None)`` (the caller never claims) or fall through to the
        # single-ended independent router (``coupled_only=False``).
        #
        # Gated on ``enable_shadow_construction`` so a flag-off run -- whose
        # contract is "may only defer, never commit where main would
        # defer" -- is behaviourally unchanged: with the flag off the
        # shadow/rescue paths are inert, so the committed routes here are
        # the coupled body that already passed the #3320 severe-overlap
        # gate, and re-deferring a clean body would needlessly lose reach.
        if self.enable_shadow_construction and routes:
            net_pads_for_check: dict[int, list[Pad]] = {}
            for net_id in (pair.positive.net_id, pair.negative.net_id):
                pad_keys = self.autorouter.nets.get(net_id, [])
                net_pads_for_check[net_id] = [
                    self.autorouter.pads[k] for k in pad_keys if k in self.autorouter.pads
                ]
            conn = validate_net_connectivity(routes, net_pads_for_check)
            stranded_nets = [
                net_id for net_id, info in conn.items() if not info.get("connected", False)
            ]
            if stranded_nets:
                for net_id in stranded_nets:
                    info = conn[net_id]
                    print(
                        f"    WARNING: [coupled-shadow] pair {pair.name} net "
                        f"{net_id} stranded "
                        f"({info.get('connected_pads', 0)}/"
                        f"{info.get('total_pads', 0)} pads reached); "
                        "ripping pair copper and deferring the whole pair."
                    )
                    logger.warning(
                        "diffpair shadow claim NOT transactional -- rolling back: "
                        "pair=%r net=%r connected_pads=%d total_pads=%d",
                        pair.name,
                        net_id,
                        info.get("connected_pads", 0),
                        info.get("total_pads", 0),
                    )
                    if _SHADOW_DEBUG:
                        self._debug_strand(pair.name, net_id, info, routes, net_pads_for_check)
                # Roll back: unmark every committed route for this pair and
                # drop it from the autorouter's route list, leaving a clean
                # grid for whichever fallback handles the pair next.  No
                # net is claimed because we return without the pair's
                # routes.
                for committed in routes:
                    with contextlib.suppress(Exception):
                        self.autorouter.grid.unmark_route(committed)
                    if committed in self.autorouter.routes:
                        self.autorouter.routes.remove(committed)
                if coupled_only:
                    print(
                        "    Skipping diff-pair pre-pass: shadow-constructed "
                        "pair stranded goal pads (transactional rollback)."
                    )
                    return [], None
                return self.route_differential_pair_independent(pair, spacing)

        # Calculate lengths
        p_length = calculate_route_length(p_routes)
        n_length = calculate_route_length(n_routes)
        pair.routed_length_p = p_length
        pair.routed_length_n = n_length

        print(f"      P length: {p_length:.3f}mm")
        print(f"      N length: {n_length:.3f}mm")

        # Check and apply length matching
        delta = pair.length_delta
        warning = None

        if delta > pair.rules.max_length_delta:
            # Issue #3003: gate the inline serpentine shim on
            # ``length_critical=True``.  The intent is that length
            # matching for length-critical pairs is performed by the
            # audited Phase 3I tuner (``tune_diff_pair_skew``), which
            # already runs an outer-normal bulge + post-insertion DRC
            # self-check.  For pairs that are NOT length_critical the
            # shim used to bulge blindly into the partner trace,
            # producing ``diffpair_clearance_intra`` violations on
            # tightly-spaced pairs.
            #
            # Look up the per-pair ``NetClassRouting`` via the autorouter's
            # ``net_class_map`` (keyed by positive net name -- both halves
            # share the same class).  When no class is configured (the
            # synthetic-test case) we default to length_critical=True so
            # the legacy code path remains exercised, but we still pass
            # ``intra_pair_clearance_mm`` so the bulge is partner-aware.
            net_class_map = getattr(self.autorouter, "net_class_map", None) or {}
            net_class = net_class_map.get(pair.positive.net_name)
            if net_class is not None:
                length_critical = bool(net_class.length_critical)
                intra_clearance = net_class.effective_intra_pair_clearance()
            else:
                length_critical = True
                intra_clearance = self.autorouter.rules.trace_clearance

            if not length_critical:
                print(
                    f"    Length mismatch: {delta:.3f}mm; "
                    f"net class {pair.positive.net_name!r} is NOT length_critical, "
                    "skipping inline serpentine (Phase 3I tuner will handle "
                    "this pair if --length-match-diffpairs is enabled)."
                )
            else:
                print(f"    Length mismatch: {delta:.3f}mm, attempting serpentine...")

                # Try to add serpentine to shorter route
                if p_routes and n_routes:
                    matched = match_pair_lengths(
                        p_routes[0],
                        n_routes[0],
                        pair.rules.max_length_delta,
                        add_serpentines=True,
                        intra_pair_clearance_mm=intra_clearance,
                        # Issue #3508: grid-validate the bulges (foreign
                        # copper, partner vias, pad halos) -- see
                        # ``create_serpentine``.
                        grid=self.autorouter.grid,
                    )

                    if matched:
                        # Issue #3508: the serpentine mutated a route
                        # AFTER it was marked on the grid (the commit at
                        # the top of this method), so the grid does not
                        # know about the bulge copper -- the negotiated
                        # main strategy then routes other nets straight
                        # through it (measured: 106 seg-seg violations
                        # across 10 nets on the first #3508 re-route).
                        # Re-mark the cells (idempotent; bookkeeping
                        # like ``grid.routes``/R-tree insertion is NOT
                        # repeated -- the route object is already
                        # registered, only its cell envelope changed).
                        self._remark_route_cells(p_routes[0])
                        self._remark_route_cells(n_routes[0])
                        # Recalculate lengths
                        p_length = calculate_route_length(p_routes)
                        n_length = calculate_route_length(n_routes)
                        pair.routed_length_p = p_length
                        pair.routed_length_n = n_length
                        delta = pair.length_delta
                        print(f"    After serpentine: delta={delta:.3f}mm")
                    else:
                        print(
                            "    Serpentine rejected (no suitable segment OR "
                            "would violate intra-pair clearance); falling through "
                            "to length-mismatch warning."
                        )

        if delta > pair.rules.max_length_delta:
            warning = LengthMismatchWarning(
                pair=pair,
                delta=delta,
                max_allowed=pair.rules.max_length_delta,
            )
            print(f"    WARNING: {warning}")
        else:
            print(f"    Length matched: delta={delta:.3f}mm (within tolerance)")

        return routes, warning

    def route_differential_pair_independent(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair with independent routing (fallback).

        Routes P and N traces separately using the standard router.
        """
        if pair.rules is None:
            return [], None

        if spacing is None:
            spacing = pair.rules.spacing

        routes: list[Route] = []
        print(f"\n  Routing differential pair {pair} (independent mode)")
        print(f"    Type: {pair.pair_type.value}")
        print(f"    Spacing: {spacing}mm, Max delta: {pair.rules.max_length_delta}mm")

        p_net_id = pair.positive.net_id
        n_net_id = pair.negative.net_id

        print(f"    Routing {pair.positive.net_name} (P)...")
        p_routes = self.autorouter.route_net(p_net_id)
        routes.extend(p_routes)

        p_length = calculate_route_length(p_routes)
        pair.routed_length_p = p_length
        print(f"      Length: {p_length:.3f}mm")

        print(f"    Routing {pair.negative.net_name} (N)...")
        n_routes = self.autorouter.route_net(n_net_id)
        routes.extend(n_routes)

        n_length = calculate_route_length(n_routes)
        pair.routed_length_n = n_length
        print(f"      Length: {n_length:.3f}mm")

        delta = pair.length_delta
        warning = None
        if delta > pair.rules.max_length_delta:
            warning = LengthMismatchWarning(
                pair=pair,
                delta=delta,
                max_allowed=pair.rules.max_length_delta,
            )
            print(f"    WARNING: {warning}")
        else:
            print(f"    Length matched: delta={delta:.3f}mm (within tolerance)")

        return routes, warning

    def intra_clearance_violations(self) -> list[IntraPairClearanceViolation]:
        """Return routed intra-pair clearance violations (Issue #3023 Phase A).

        Returns the rolling buffer of violations recorded by
        :meth:`route_differential_pair_coupled` since this
        :class:`DiffPairRouter` was constructed.  Each entry corresponds
        to one ``CoupledPathfinder``-routed (P, N) pair whose post-route
        edge-to-edge clearance dropped below the per-pair
        ``NetClassRouting.effective_intra_pair_clearance()``.

        Phase A is detection-only -- this method exists so Phase B (the
        fine-grid sub-pass, separate PR) and external tooling (DRC
        reports, e2e tests on board 07) can audit how many violations
        the coupled router emitted without re-running the geometry
        check.

        Returns:
            A shallow copy of the violation buffer.  Empty when no
            coupled diff-pair routes have been laid down or when every
            pair satisfied its per-pair clearance threshold.  Callers
            MUST NOT mutate the returned list to clear state; use
            :meth:`reset_intra_clearance_violations` instead.
        """
        return list(self._intra_clearance_violations)

    def reset_intra_clearance_violations(self) -> None:
        """Discard the buffered Phase A clearance-violation records.

        Used by tests that exercise multiple
        ``route_differential_pair_coupled`` calls on a single
        :class:`DiffPairRouter` instance and want a clean baseline
        between cases.  Not intended for production callers; the buffer
        is intentionally additive over a single Autorouter session so
        the post-routing audit sees every coupled pair.
        """
        self._intra_clearance_violations.clear()

    def _route_pair_on_fine_grid(
        self,
        pair: DifferentialPair,
        spacing_override: float | None,
        extra_spacing_cells: int,
        per_pair_timeout: float | None,
        resolution_factor: float = 0.5,
    ) -> tuple[list[Route], object | None]:
        """Issue #3115 Phase B fine-grid sub-pass: re-route a pair on a finer grid.

        Builds a bbox-scoped routing grid whose resolution is
        ``resolution_factor x main_grid.resolution`` (default half-pitch),
        re-marks the obstacles/pads/foreign routes the main grid carries
        in that bounding box, then runs :class:`CoupledPathfinder` against
        the fine grid.  The resulting routes are returned in world
        coordinates so the caller can mark them on the main grid.

        Targets the angle-#1 root cause flagged in Issue #3115: grid
        quantisation of asymmetric escape stubs prevents the main-grid
        ``extra_spacing_cells`` retry from producing equal-length P/N
        landings.  A finer grid resolution gives the coupled search
        sub-cell-aware moves so the partner-aware exit cell can land on
        an evenly-clearance position without changing the corridor A*
        algorithm.

        Args:
            pair: The differential pair to re-route.
            spacing_override: Optional spacing override (from
                ``diffpair_config.spacing``); ``None`` uses the pair's
                own rules.
            extra_spacing_cells: Additional grid cells of spacing
                widening, same semantics as
                :meth:`route_differential_pair_coupled`.  At the
                fine-grid resolution one cell is half as wide as at the
                main-grid resolution, so a value of e.g. ``2`` on a
                half-pitch fine grid only widens by ``1 x main cell``.
                The caller should compensate by passing a larger
                value when re-using the main-grid floor.
            per_pair_timeout: Wall-clock budget forwarded to
                :meth:`CoupledPathfinder.route_coupled`.
            resolution_factor: Fine-grid resolution multiplier.  Default
                ``0.5`` (half-pitch).

        Returns:
            ``(routes, warning)`` matching the legacy
            :meth:`route_differential_pair_coupled` return shape.
            ``([], None)`` if pads cannot be resolved, the fine-grid
            bounding box is degenerate, or the coupled search returns
            no path.  Successful routes are NOT marked on either grid
            by this helper; the caller is responsible for the
            ``autorouter._mark_route()`` + ``autorouter.routes.append()``
            handoff so post-route bookkeeping (intra-clearance audit,
            length matching) stays consistent with the main-grid path.
        """
        from .grid import RoutingGrid
        from .rules import DesignRules

        if pair.rules is None:
            return [], None

        # Resolve the pads we need to route between.
        pad_result = self._get_pair_pads(pair)
        if pad_result is None:
            return [], None
        p_pads, n_pads = pad_result
        if not p_pads or not n_pads:
            return [], None

        main_grid = self.autorouter.grid
        fine_resolution = max(
            main_grid.resolution * resolution_factor,
            # Don't go below 0.01mm; below that the pathfinder cost
            # explodes faster than the resolution helps geometry.
            0.01,
        )

        # Compute bounding box covering the pair's pads with a margin
        # equal to the main-grid spacing target so the search has room
        # to maneuver around adjacent obstacles.
        all_xs = [p.x for p in p_pads + n_pads]
        all_ys = [p.y for p in p_pads + n_pads]
        margin = max(
            2.0,  # at least 2mm of breathing room
            6.0 * main_grid.resolution,  # or six main-grid cells
        )
        bbox_min_x = min(all_xs) - margin
        bbox_min_y = min(all_ys) - margin
        bbox_max_x = max(all_xs) + margin
        bbox_max_y = max(all_ys) + margin

        # Clamp to the main grid's footprint so we don't run off the board.
        bbox_min_x = max(bbox_min_x, main_grid.origin_x)
        bbox_min_y = max(bbox_min_y, main_grid.origin_y)
        bbox_max_x = min(bbox_max_x, main_grid.origin_x + main_grid.width)
        bbox_max_y = min(bbox_max_y, main_grid.origin_y + main_grid.height)

        fine_width = bbox_max_x - bbox_min_x
        fine_height = bbox_max_y - bbox_min_y

        if fine_width <= 0 or fine_height <= 0:
            # Degenerate bounding box; nothing to route on.
            return [], None

        # Safety check on grid size: a half-pitch grid quadruples the
        # cell count, so cap the fine-grid size to avoid pathological
        # memory use on large pairs (e.g., edge-to-edge mini-PCIe).
        # The main-grid fine-grid pass in ``core.py:11652`` uses
        # 16M cells; we use a tighter 4M cap because this is per-pair
        # and runs inside a per-pair timeout.
        num_layers = main_grid.num_layers
        estimated_cells = (
            (fine_width / fine_resolution) * (fine_height / fine_resolution) * num_layers
        )
        max_fine_cells = 4_000_000
        if estimated_cells > max_fine_cells:
            scale = (estimated_cells / max_fine_cells) ** 0.5
            fine_resolution = fine_resolution * scale
            logger.info(
                "Phase B fine-grid: scaling resolution up to %.4fmm to fit %d-cell cap (pair=%r)",
                fine_resolution,
                max_fine_cells,
                pair.name,
            )

        # Build a fresh design rules object that mirrors the main rules
        # but uses the fine resolution.  Mirrors the pattern at
        # ``core.py:11673``.
        main_rules = self.autorouter.rules
        fine_rules = DesignRules(
            grid_resolution=fine_resolution,
            trace_width=main_rules.trace_width,
            trace_clearance=main_rules.trace_clearance,
            via_drill=main_rules.via_drill,
            via_diameter=main_rules.via_diameter,
            via_clearance=main_rules.via_clearance,
            manufacturer=main_rules.manufacturer,
        )

        fine_grid = RoutingGrid(
            width=fine_width,
            height=fine_height,
            rules=fine_rules,
            origin_x=bbox_min_x,
            origin_y=bbox_min_y,
            layer_stack=main_grid.layer_stack,
            resolution_override=fine_resolution,
        )

        # Mirror autorouter pads onto the fine grid so the coupled
        # search sees the same obstacle field.  This includes BOTH the
        # pair's own pads (their cells must be reachable for the same
        # net) and other nets' pads in the bounding box (must be
        # blocked).
        pitches = self.autorouter.component_pitches
        pad_refs_in_pair = {(p.ref, p.pin) for p in p_pads + n_pads}
        # Add the pair's own pads first so net ownership is correct.
        for pad in p_pads + n_pads:
            fine_grid.add_pad(pad, pin_pitch=pitches.get(pad.ref))
        # Add foreign pads that fall in the bounding box.
        for (ref, pin), pad in self.autorouter.pads.items():
            if (ref, pin) in pad_refs_in_pair:
                continue
            if bbox_min_x <= pad.x <= bbox_max_x and bbox_min_y <= pad.y <= bbox_max_y:
                fine_grid.add_pad(pad, pin_pitch=pitches.get(pad.ref))

        # Re-mark all currently-committed routes (foreign nets) on the
        # fine grid so the coupled search avoids them.  The pair's own
        # routes were already ripped up by the caller before invoking
        # this helper.
        pair_p_net, pair_n_net = pair.get_net_ids()
        for route in self.autorouter.routes:
            if route.net == pair_p_net or route.net == pair_n_net:
                continue
            fine_grid.mark_route(route)

        # Compute the same center-to-center spacing the main path uses
        # at line 2095-2140, but in fine-grid cells.
        if spacing_override is None:
            spacing = pair.rules.spacing
        else:
            spacing = spacing_override

        net_class_map = self.autorouter.net_class_map or {}
        pair_net_class = net_class_map.get(pair.positive.net_name)
        if pair_net_class is not None:
            pair_trace_width = float(pair_net_class.trace_width)
            pair_intra_clearance = float(pair_net_class.effective_intra_pair_clearance())
        else:
            pair_trace_width = float(self.autorouter.rules.trace_width)
            pair_intra_clearance = float(spacing)

        required_center_spacing = pair_trace_width + pair_intra_clearance
        min_spacing_cells = max(1, math.ceil(required_center_spacing / fine_resolution))
        legacy_spacing_cells = math.ceil(spacing / fine_resolution)
        spacing_cells = max(legacy_spacing_cells, min_spacing_cells)

        if extra_spacing_cells > 0:
            min_spacing_cells += extra_spacing_cells
            spacing_cells += extra_spacing_cells

        # Build the fine-grid coupled pathfinder.
        pathfinder = CoupledPathfinder(
            fine_grid,
            fine_rules,
            spacing_cells,
            net_class_map=self.autorouter.net_class_map,
            allow_swap_via=False,  # synthetic asymmetric-pad case rarely needs it
            min_spacing_cells=min_spacing_cells,
        )

        # Pair the pads with the same MST/legacy logic as
        # route_differential_pair_coupled so the spec ordering
        # matches what the main-grid path would produce.
        if len(p_pads) == 2 and len(n_pads) == 2:
            legacy_specs = self._pair_pads_for_coupled_routing(p_pads, n_pads)
            specs = []
            for ps, pe, ns, ne in legacy_specs:
                specs.append((ps, pe, ns, ne))
        else:
            coupled_specs, _stub_specs = self._pair_pads_for_coupled_routing_npad(p_pads, n_pads)
            specs = [(s.p_start, s.p_end, s.n_start, s.n_end) for s in coupled_specs]

        if not specs:
            return [], None

        produced_routes: list[Route] = []
        for ps, pe, ns, ne in specs:
            result = pathfinder.route_coupled(ps, pe, ns, ne, timeout_seconds=per_pair_timeout)
            if result is None:
                # Failed on at least one spec -- abandon the fine-grid
                # attempt entirely (the caller will try the next angle
                # or restore the original routes).
                return [], None
            p_route, n_route = result
            produced_routes.append(p_route)
            produced_routes.append(n_route)

        return produced_routes, None

    def repair_intra_clearance_violations(
        self,
        diffpair_config: DifferentialPairConfig | None = None,
        max_retries_per_pair: int = 2,
        enable_fine_grid_pass: bool = True,
    ) -> int:
        """Issue #3040 Phase B: rip-up and retry pairs with intra-clearance violations.

        For each pair recorded in ``self._intra_clearance_violations``
        (Phase A detection), this method:

          1. Removes the offending P/N routes from the autorouter's
             route list and unmarks them from the grid.
          2. Re-invokes :meth:`route_differential_pair_coupled` with a
             progressively wider ``extra_spacing_cells`` (1 cell on
             attempt 1, 2 cells on attempt 2) so the
             :class:`CoupledPathfinder` lays the centerlines further
             apart -- enough additional spacing to recover the
             edge-to-edge clearance lost to grid quantisation in the
             first attempt.
          3. Issue #3115: when the main-grid retries fail and
             ``enable_fine_grid_pass`` is True, runs a fine-grid
             sub-pass (half the main resolution, scoped to the pair's
             bounding box) that re-routes the pair against the
             quantisation-sensitive escape geometry the wider-spacing
             retries cannot fix.  Targets the angle-#1 root cause of
             asymmetric pad heights producing unequal escape stubs
             that the main grid pitch cannot equalise.
          4. Re-checks the new pair via
             ``find_intra_pair_clearance_violations`` and accepts the
             retry only if the violation is resolved.  If every retry
             (main-grid and fine-grid) still violates (or the
             pathfinder finds no path), the original routes are
             restored and the pair remains flagged for the
             :func:`~kicad_tools.router.io.validate_routes` safety net.

        Args:
            diffpair_config: The same configuration used by the original
                ``route_all_with_diffpairs`` call (so per-pair rules and
                spacing carry over).  May be ``None`` if no special
                configuration is in effect.
            max_retries_per_pair: Hard cap on main-grid retry attempts
                per pair to prevent infinite loops on pathologically
                tight escapes.  Default ``2``; each attempt widens
                spacing by one additional grid cell over the prior
                attempt.  The optional fine-grid sub-pass is in
                addition to these main-grid attempts.
            enable_fine_grid_pass: Issue #3115: when True (default),
                perform a fine-grid sub-pass after the main-grid retries
                exhaust.  Set False to retain the legacy Phase B
                behaviour (main-grid retries only) for tests that pin
                that contract.

        Returns:
            The number of pairs whose violation was resolved by the
            repair pass.  ``0`` means either no violations were
            present, or every retry failed to find a compliant route.
        """
        violations = list(self._intra_clearance_violations)
        if not violations:
            return 0

        # Build a lookup from net names back to the DifferentialPair
        # objects so we can re-invoke routing.  Filter to engaged pairs
        # so we don't accidentally re-route a pair that the engagement
        # gate refused.
        diff_pairs_with_source = self.detect_differential_pairs_with_source()
        all_pairs = [p for p, _ in diff_pairs_with_source]

        # Apply diffpair_config rules so the retry uses the same rules
        # as the original pass.
        if diffpair_config is not None and diffpair_config.enabled:
            for pair in all_pairs:
                if pair.rules is not None:
                    pair.rules = diffpair_config.get_rules(pair.pair_type)

        pair_by_net: dict[str, DifferentialPair] = {}
        for pair in all_pairs:
            pair_by_net[pair.positive.net_name] = pair
            pair_by_net[pair.negative.net_name] = pair

        # Group violations by pair (same pair may appear multiple times
        # if there were multiple coupled specs).  Use the pair's positive
        # net name as the stable key.
        violations_by_pair: dict[str, list[IntraPairClearanceViolation]] = {}
        for v in violations:
            key = v.positive_net_name
            violations_by_pair.setdefault(key, []).append(v)

        resolved_pairs = 0

        for p_net_name, pair_violations in violations_by_pair.items():
            pair = pair_by_net.get(p_net_name)
            if pair is None:
                logger.warning(
                    "Phase B repair: cannot find DifferentialPair for net %r; "
                    "leaving violation in place for validate_routes() safety net.",
                    p_net_name,
                )
                continue

            p_id, n_id = pair.get_net_ids()
            n_net_name = pair_violations[0].negative_net_name

            # Issue #3508: defensive pair-identity check.  The lookup
            # above re-runs pair DETECTION, which can disagree with the
            # pairing the violation was recorded against (observed on
            # board 06: a violation recorded for USB3_RX1+/USB3_RX1-
            # resolved to a DifferentialPair object whose negative side
            # was USB3_TX1-).  Re-routing such a cross-pair would rip
            # up and re-couple nets from two DIFFERENT pairs.  Skip and
            # leave the violation for the validate_routes() safety net.
            if {pair.positive.net_name, pair.negative.net_name} != {
                p_net_name,
                n_net_name,
            }:
                logger.warning(
                    "Phase B repair: detection re-paired %r with %r but the "
                    "violation was recorded against %r/%r; skipping repair "
                    "for this pair.",
                    pair.positive.net_name,
                    pair.negative.net_name,
                    p_net_name,
                    n_net_name,
                )
                continue

            # Snapshot current routes for this pair so we can either
            # rip them up cleanly or restore them on failure.
            current_p_routes = [r for r in self.autorouter.routes if r.net == p_id]
            current_n_routes = [r for r in self.autorouter.routes if r.net == n_id]

            if not current_p_routes and not current_n_routes:
                # Nothing to repair (pair must have been ripped up
                # already by some other repair pass).
                continue

            print(
                f"\n  Phase B repair: {p_net_name}/{n_net_name} "
                f"({len(pair_violations)} violation(s), retrying with wider spacing)"
            )

            # Rip up the original routes.
            for route in list(current_p_routes):
                self.autorouter.grid.unmark_route(route)
                if route in self.autorouter.routes:
                    self.autorouter.routes.remove(route)
            for route in list(current_n_routes):
                self.autorouter.grid.unmark_route(route)
                if route in self.autorouter.routes:
                    self.autorouter.routes.remove(route)

            # Remember which violations correspond to this pair so we
            # can prune them from the buffer on success.
            ids_to_remove = {id(v) for v in pair_violations}

            # Bounded retry loop with progressively wider spacing.
            retry_succeeded = False
            spacing_override = (
                diffpair_config.spacing
                if diffpair_config is not None and diffpair_config.enabled
                else None
            )

            # Snapshot the violation count BEFORE the retry so we can
            # detect new violations introduced by this attempt.
            len(self._intra_clearance_violations)

            for attempt in range(1, max_retries_per_pair + 1):
                # Clear violations from prior retry on this pair so the
                # new attempt's audit is the only entry we examine.
                # We restore unrelated entries below.
                snapshot = list(self._intra_clearance_violations)
                # Keep all violations that are NOT from this pair.
                self._intra_clearance_violations = [
                    v for v in snapshot if v.positive_net_name != p_net_name
                ]

                print(
                    f"    Phase B attempt {attempt}/{max_retries_per_pair}: "
                    f"extra_spacing_cells={attempt}"
                )
                # Issue #3089: forward a tightened per-pair wall-clock
                # budget so Phase B retries cannot stall the run with
                # the same BGA-49-escape pathology that triggered the
                # first-pass budget exit.  Phase B retries are
                # known-likely-to-fail when the violation persists
                # across attempts; we cap each retry at half the
                # configured budget so the worst-case repair-loop
                # cost is bounded at
                # ``violating_pairs * max_retries_per_pair * (budget / 2)``.
                phase_b_timeout: float | None = None
                if diffpair_config is not None and diffpair_config.per_pair_timeout:
                    phase_b_timeout = max(2.0, diffpair_config.per_pair_timeout / 2.0)
                retry_routes, _retry_warning = self.route_differential_pair_coupled(
                    pair,
                    spacing=spacing_override,
                    coupled_only=True,
                    extra_spacing_cells=attempt,
                    per_pair_timeout=phase_b_timeout,
                )

                # Capture any new violations the audit recorded for this
                # pair during the retry.
                new_violations_for_pair = [
                    v for v in self._intra_clearance_violations if v.positive_net_name == p_net_name
                ]

                if retry_routes and not new_violations_for_pair:
                    # Retry succeeded and no new violations.
                    print(
                        f"    Phase B succeeded: {p_net_name}/{n_net_name} "
                        f"clean after {attempt} attempt(s)."
                    )
                    retry_succeeded = True
                    # Mark the resolved violations for removal.
                    for v in pair_violations:
                        ids_to_remove.add(id(v))
                    break

                # Retry produced no path or still violates -- rip the
                # retry routes up and try again (or fall through).
                retry_p_routes = [r for r in self.autorouter.routes if r.net == p_id]
                retry_n_routes = [r for r in self.autorouter.routes if r.net == n_id]
                for route in retry_p_routes:
                    self.autorouter.grid.unmark_route(route)
                    if route in self.autorouter.routes:
                        self.autorouter.routes.remove(route)
                for route in retry_n_routes:
                    self.autorouter.grid.unmark_route(route)
                    if route in self.autorouter.routes:
                        self.autorouter.routes.remove(route)

            # Issue #3115 Phase B fine-grid sub-pass: when the main-grid
            # ``extra_spacing_cells`` retries have all failed, give the
            # pair one last chance on a half-pitch grid.  This targets
            # the asymmetric-pad-escape pathology where the main-grid
            # quantisation forces unequal P/N stubs that the
            # wider-spacing search cannot equalise.
            if not retry_succeeded and enable_fine_grid_pass:
                # Clear out this pair's violations from prior attempts
                # so the new audit is the only signal we look at.
                self._intra_clearance_violations = [
                    v for v in self._intra_clearance_violations if v.positive_net_name != p_net_name
                ]

                fine_grid_timeout: float | None = None
                if diffpair_config is not None and diffpair_config.per_pair_timeout:
                    # Reuse the half-budget cap from the main-grid
                    # retries; the fine grid is more expensive but
                    # the bbox is narrowly scoped.
                    fine_grid_timeout = max(2.0, diffpair_config.per_pair_timeout / 2.0)

                print(
                    f"    Phase B fine-grid sub-pass: {p_net_name}/{n_net_name} on half-pitch grid"
                )
                try:
                    fine_routes, _fine_warning = self._route_pair_on_fine_grid(
                        pair,
                        spacing_override=spacing_override,
                        # Modest widening on the fine grid: one main-grid
                        # cell == two fine-grid cells; carry one fine
                        # cell of extra spacing.
                        extra_spacing_cells=1,
                        per_pair_timeout=fine_grid_timeout,
                        resolution_factor=0.5,
                    )
                except Exception as e:
                    # The fine-grid sub-pass is best-effort.  If it
                    # raises (cell-count cap, grid-init failure, etc.)
                    # we fall through to the original-route restore
                    # path so the board state stays consistent.
                    logger.warning(
                        "Phase B fine-grid sub-pass raised an unexpected "
                        "exception (pair=%r): %s; falling back to "
                        "main-grid violation state.",
                        pair.name,
                        e,
                    )
                    fine_routes = []

                if fine_routes:
                    # Audit the fine-grid routes against the same
                    # threshold the original detector used so we don't
                    # accept routes that are STILL in violation.
                    fine_p_routes = [r for r in fine_routes if r.net == p_id]
                    fine_n_routes = [r for r in fine_routes if r.net == n_id]
                    if fine_p_routes and fine_n_routes:
                        # Use the first (longest) p/n routes for the
                        # detector -- matches the 2-pad fast path.
                        fine_violation = find_intra_pair_clearance_violations(
                            fine_p_routes[0],
                            fine_n_routes[0],
                            threshold_mm=pair_violations[0].expected_clearance_mm,
                            pair_name=pair.name,
                        )
                        if fine_violation is None:
                            # Clean!  Mark on the main grid and accept.
                            for route in fine_routes:
                                self.autorouter._mark_route(route)
                                self.autorouter.routes.append(route)
                            print(
                                f"    Phase B fine-grid sub-pass succeeded: "
                                f"{p_net_name}/{n_net_name} clean."
                            )
                            retry_succeeded = True
                            for v in pair_violations:
                                ids_to_remove.add(id(v))
                        else:
                            print(
                                f"    Phase B fine-grid sub-pass still violates: "
                                f"actual={fine_violation.actual_clearance_mm:.4f}mm "
                                f"threshold={fine_violation.expected_clearance_mm:.4f}mm"
                            )

            if retry_succeeded:
                resolved_pairs += 1
                # Remove all original (and replaced) violations for
                # this pair from the buffer.  The retry's audit will
                # have already inserted the new (clean) state.
                self._intra_clearance_violations = [
                    v for v in self._intra_clearance_violations if id(v) not in ids_to_remove
                ]
            else:
                # Restore the original routes so the board is no worse
                # off than before (and the violation remains in the
                # buffer for the validate_routes() safety net).
                logger.warning(
                    "Phase B repair: %s/%s still violates after %d attempt(s); "
                    "restoring original routes and leaving violation for "
                    "validate_routes() safety net.",
                    p_net_name,
                    n_net_name,
                    max_retries_per_pair,
                )
                print(
                    f"    Phase B failed: {p_net_name}/{n_net_name} still violates "
                    f"after {max_retries_per_pair} attempt(s); restoring original routes."
                )
                for route in current_p_routes:
                    self.autorouter._mark_route(route)
                    self.autorouter.routes.append(route)
                for route in current_n_routes:
                    self.autorouter._mark_route(route)
                    self.autorouter.routes.append(route)
                # Restore the original violation records for this pair
                # if the retry attempts removed them.
                for v in pair_violations:
                    if v not in self._intra_clearance_violations:
                        self._intra_clearance_violations.append(v)

        if resolved_pairs:
            print(
                f"\n  Phase B repair complete: {resolved_pairs}/"
                f"{len(violations_by_pair)} pair(s) repaired."
            )

        return resolved_pairs

    def route_differential_pair(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
        use_coupled_routing: bool = True,
        per_pair_timeout: float | None = None,
        per_pair_max_iterations: int | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair.

        Args:
            pair: The differential pair to route
            spacing: Override spacing (uses pair rules if None)
            use_coupled_routing: If True, use coupled A* routing.
                                If False, use independent routing.
            per_pair_timeout: Issue #3089: Optional per-pair wall-clock
                budget (seconds) forwarded to
                :meth:`route_differential_pair_coupled` when
                ``use_coupled_routing`` is ``True``.  Ignored when the
                independent fallback runs.
            per_pair_max_iterations: Issue #3144: Optional per-pair
                iteration budget forwarded the same way; see
                :class:`DifferentialPairConfig` for rationale.

        Returns:
            Tuple of (routes, warning) where warning is set if
            length matching failed.
        """
        if use_coupled_routing:
            return self.route_differential_pair_coupled(
                pair,
                spacing,
                per_pair_timeout=per_pair_timeout,
                per_pair_max_iterations=per_pair_max_iterations,
            )
        else:
            return self.route_differential_pair_independent(pair, spacing)

    def route_diffpair_prepass(
        self,
        diffpair_config: DifferentialPairConfig | None = None,
    ) -> tuple[list[Route], list[LengthMismatchWarning], set[int]]:
        """Route only the differential pairs, leaving other nets to a follow-up strategy.

        Issue #2464: This is a pre-pass that the main routing strategies
        (negotiated, monte-carlo, evolutionary) can run before their normal
        flow.  Diff-pair traces are routed via the CoupledPathfinder and
        marked on the grid, after which the main strategy routes the
        remaining nets.

        Args:
            diffpair_config: Configuration for diff-pair routing.  If None
                or ``enabled`` is False, this method is a no-op.

        Returns:
            ``(routes, warnings, diff_net_ids)`` where:
              - ``routes`` is the list of routes produced for the diff pairs.
              - ``warnings`` is the list of length-mismatch warnings.
              - ``diff_net_ids`` is the set of net IDs that were successfully
                routed (and should therefore be skipped by the follow-up
                strategy).
        """
        if diffpair_config is None or not diffpair_config.enabled:
            return [], [], set()

        diff_pairs_with_source = self.detect_differential_pairs_with_source()
        diff_pairs = [p for p, _ in diff_pairs_with_source]
        if not diff_pairs:
            print("  No differential pairs detected")
            return [], [], set()

        print("\n=== Differential Pair Pre-Pass (Issue #2464) ===")
        print(f"  Detected {len(diff_pairs)} differential pairs:")
        for pair, source in diff_pairs_with_source:
            msg = f"    - {pair}: {pair.pair_type.value} (source: {source})"
            print(msg)
            logger.info("[diffpair-pre-pass] %s", msg.strip())

        for pair in diff_pairs:
            if pair.rules is not None:
                pair.rules = diffpair_config.get_rules(pair.pair_type)

        all_routes: list[Route] = []
        warnings: list[LengthMismatchWarning] = []
        routed_net_ids: set[int] = set()

        for pair in diff_pairs:
            p_id, n_id = pair.get_net_ids()
            # Issue #2638, Epic #2556 Phase 2E: engagement gate.  When the
            # pair's net class has not opted in via ``coupled_routing=True``,
            # or when the pair is single-ended-by-spec (USB-C CC1/CC2,
            # SBU1/SBU2 — the #2527 lesson), refuse coupled routing and
            # let the pair fall through to the main strategy.
            engaged, reason = self._resolve_engagement(pair)
            if not engaged:
                msg = f"[diffpair-engage] refused {pair}: {reason}"
                print(f"  {msg}")
                logger.info(msg)
                continue
            # Issue #2464: Use coupled_only=True so that the pre-pass is a
            # no-op for pairs that the CoupledPathfinder cannot handle.
            # Those pairs are left for the main strategy (negotiated/MC/GA)
            # to route in its normal flow, which avoids producing partial
            # routes that the main strategy would then refuse to complete.
            pair_routes, warning = self.route_differential_pair_coupled(
                pair,
                diffpair_config.spacing,
                coupled_only=True,
            )
            if pair_routes:
                routed_for_net: dict[int, int] = {}
                for r in pair_routes:
                    routed_for_net[r.net] = routed_for_net.get(r.net, 0) + 1
                if routed_for_net.get(p_id, 0) > 0 and routed_for_net.get(n_id, 0) > 0:
                    routed_net_ids.add(p_id)
                    routed_net_ids.add(n_id)
                    # Issue #3508: see route_all_with_diffpairs -- a net
                    # with a failed stub edge stays routable for the
                    # main strategy.
                    for incomplete in getattr(self, "_last_stub_failed_nets", set()) & {p_id, n_id}:
                        routed_net_ids.discard(incomplete)

            all_routes.extend(pair_routes)
            if warning:
                warnings.append(warning)

        unrouted_pairs = [p for p in diff_pairs if p.get_net_ids()[0] not in routed_net_ids]
        if all_routes:
            print(
                f"  Diff-pair pre-pass produced {len(all_routes)} routes "
                f"covering {len(routed_net_ids)} nets"
            )
        if unrouted_pairs:
            print(f"  Diff pairs falling through to main strategy: {len(unrouted_pairs)}")
        if warnings:
            print(f"  Length mismatch warnings: {len(warnings)}")
            for w in warnings:
                print(f"    - {w}")

        return all_routes, warnings, routed_net_ids

    # ------------------------------------------------------------------
    # Issue #4463: corridor-competition recovery
    # ------------------------------------------------------------------

    def _probe_net_routable(
        self,
        net_id: int,
        per_net_timeout: float,
    ) -> list[Route] | None:
        """Probe whether ``net_id`` can be routed on the CURRENT grid.

        Issue #4463.  Routes the net with the ordinary single-ended
        router, checks that every pad of the net ended up in one
        connected component, then **rolls the copper back off the grid**
        so the probe leaves no trace.  Returns the (already-unmarked)
        routes on success and ``None`` when the net could not be routed
        or the produced copper left a pad stranded.

        This is deliberately the same oracle the main strategy uses --
        a real A* through the real obstacle field -- rather than a
        cheaper reachability approximation, because the thing we need to
        know is exactly "would the main strategy strand this net".

        "Leaves no trace" covers ROUTER state as well as copper.  The
        C++ backend memoizes per-net clearance-resume exhaustions and
        skips the Python fallback on the SECOND one (#3923), so a probe
        that legitimately fails against a sealed corridor would silently
        make every later probe of the same net pessimistic -- the net
        would look unrecoverable purely because we asked twice.  The
        memo (and the failure log) is therefore snapshotted and restored
        around every probe, which is what makes repeated probes of one
        net answer the same question the main strategy would.
        """
        autorouter = self.autorouter
        before = len(autorouter.routes)
        failures_before = len(getattr(autorouter, "routing_failures", []))
        backend = getattr(autorouter, "router", None)
        resume_memo = getattr(backend, "_resume_clearance_exhaustions", None)
        saved_memo = dict(resume_memo) if isinstance(resume_memo, dict) else None
        routes: list[Route] = []
        try:
            routes = autorouter.route_net(net_id, per_net_timeout=per_net_timeout)
        except Exception:  # pragma: no cover - defensive: a probe must never raise
            logger.debug("[corridor-yield] probe raised for net %s", net_id, exc_info=True)
            routes = []
        finally:
            if saved_memo is not None and isinstance(resume_memo, dict):
                resume_memo.clear()
                resume_memo.update(saved_memo)
            failures = getattr(autorouter, "routing_failures", None)
            if isinstance(failures, list) and len(failures) > failures_before:
                del failures[failures_before:]

        # Roll back every route the probe committed (both the ones it
        # returned and anything else it appended -- e.g. MST sub-edges).
        committed = list(autorouter.routes[before:])
        del autorouter.routes[before:]
        seen_ids = {id(r) for r in committed}
        for route in routes:
            if id(route) not in seen_ids:
                committed.append(route)
                seen_ids.add(id(route))
        for route in committed:
            with contextlib.suppress(Exception):
                autorouter.grid.unmark_route(route)

        if not routes:
            return None

        pad_keys = autorouter.nets.get(net_id, [])
        net_pads = [autorouter.pads[k] for k in pad_keys if k in autorouter.pads]
        if len(net_pads) >= 2:
            conn = validate_net_connectivity(routes, {net_id: net_pads})
            info = conn.get(net_id, {})
            if not info.get("connected", False):
                return None
        return routes

    @staticmethod
    def _copper_conflicts(
        routes_a: list[Route],
        routes_b: list[Route],
        clearance: float,
    ) -> bool:
        """Return True when two route sets have copper within ``clearance``.

        Issue #4463.  Used to decide which committed coupled bodies stand
        in the way of a stranded net's desired path: to run that path, any
        copper closer to it than the clearance rule has to go, and copper
        that is further away does not.  Vias count on every layer (a
        through via is an obstacle on layers its owner never traced on).
        """

        def _segments(
            routes: list[Route],
        ) -> list[tuple[object | None, float, float, float, float, float]]:
            out: list[tuple[object | None, float, float, float, float, float]] = []
            for route in routes:
                for seg in route.segments:
                    layer = getattr(seg.layer, "value", seg.layer)
                    out.append((layer, seg.x1, seg.y1, seg.x2, seg.y2, seg.width / 2.0))
                for via in route.vias:
                    # layer=None -> "every layer" (through-hole annulus)
                    out.append((None, via.x, via.y, via.x, via.y, via.diameter / 2.0))
            return out

        segs_a = _segments(routes_a)
        segs_b = _segments(routes_b)
        for la, ax1, ay1, ax2, ay2, ra in segs_a:
            for lb, bx1, by1, bx2, by2, rb in segs_b:
                if la is not None and lb is not None and la != lb:
                    continue
                gap = _segment_to_segment_distance(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2)
                if gap - ra - rb < clearance:
                    return True
        return False

    def _net_is_connected(self, net_id: int) -> bool:
        """Whether the copper committed so far connects every pad of a net.

        Issue #4463.  Ground truth for the corridor-yield recovery: a net
        with fewer than two routable pads is trivially "connected"; any
        other net must have copper on the board that unions all of its
        pads into one component (the same
        :func:`validate_net_connectivity` oracle the #3540 transactional
        shadow claim uses).
        """
        autorouter = self.autorouter
        pad_keys = autorouter.nets.get(net_id, [])
        net_pads = [autorouter.pads[k] for k in pad_keys if k in autorouter.pads]
        if len(net_pads) < 2:
            return True
        routes = [r for r in autorouter.routes if r.net == net_id]
        if not routes:
            return False
        conn = validate_net_connectivity(routes, {net_id: net_pads})
        return bool(conn.get(net_id, {}).get("connected", False))

    def _plan_corridor_yields(
        self,
        candidate_nets: list[int],
        committed_pairs: list[tuple[DifferentialPair, list[Route]]],
    ) -> tuple[list[tuple[DifferentialPair, list[Route]]], list[int]]:
        """Which committed coupled bodies seal a still-unconnected net in?

        Issue #4463 (corridor competition).  The coupled pre-phase claims
        its corridors first and its copper is **non-rippable** for the
        rest of the pipeline: the negotiated loop's ``net_routes`` holds
        only the nets it routes itself, so a net whose only corridor a
        committed coupled body sealed can never be recovered there.  Every
        rip-up round, every neighbourhood-radius escalation and every
        relief rescue rolls back with ``blocked only by non-rippable
        copper of <pair nets>`` -- measured on board 06 shadow-ON (seed
        42, main @ 0a8d724e): ``MIPI_D0+``, ``MIPI_D0-`` and ``USB_CC1``
        stranded for all 10 iterations, 362.3 s spent in the negotiated
        loop, final reach 18/21.

        This planner answers the question the loop could not: for each net
        the main strategy actually failed to connect, is there a path once
        the coupled copper is out of the way, and if so which pairs sit on
        it?  It works on GROUND TRUTH (what the strategy produced), it
        commits nothing, and it leaves the board exactly as it found it --
        the caller decides whether to act on the plan.

        Per stranded net it probes at most twice: once with the coupled
        copper lifted, and (only if that fails) once with the other
        candidate nets' rippable copper lifted as well -- the relief
        rescue's "rip the victims" step, except it may also cross the
        coupled boundary the rescue cannot.  The fixed, small probe count
        is deliberate: ``mark_route`` / ``unmark_route`` round-trips are
        not perfectly symmetric in the router's auxiliary state, so a
        design that probed once per (net, pair) combination would slowly
        poison its own oracle.

        Args:
            candidate_nets: Every signal net that may be examined -- the
                main strategy's nets plus the diff-pair nets (a pair the
                pre-phase CLAIMED but left unconnected is itself a
                corridor-competition victim, and it is not in the main
                strategy's net list precisely because the claim removed
                it).
            committed_pairs: ``(pair, routes)`` for every pair whose
                copper the coupled pre-phase committed.

        Returns:
            ``(pairs_to_yield, stranded_nets)`` -- the pairs whose copper
            stands on some stranded net's path, and the stranded nets that
            were examined.
        """
        autorouter = self.autorouter
        grid = autorouter.grid
        if not candidate_nets or not committed_pairs:
            return [], []

        stranded = [n for n in candidate_nets if not self._net_is_connected(n)]
        if not stranded:
            return [], []

        names = getattr(autorouter, "net_names", {}) or {}

        def _name(net_id: int) -> str:
            return str(names.get(net_id, f"Net_{net_id}"))

        def _unmark(routes: list[Route]) -> None:
            for route in routes:
                with contextlib.suppress(Exception):
                    grid.unmark_route(route)

        def _remark(routes: list[Route]) -> None:
            for route in routes:
                autorouter._mark_route(route)

        def _restore(routes: list[Route]) -> None:
            _remark(routes)
            for route in routes:
                if route not in autorouter.routes:
                    autorouter.routes.append(route)

        # The clearance a foreign trace must keep from the desired path,
        # plus one grid cell of slack for the grid's own marking margin.
        clearance = float(getattr(autorouter.rules, "trace_clearance", 0.2)) + float(
            getattr(grid, "resolution", 0.1)
        )
        deadline = time.monotonic() + _CORRIDOR_GUARD_BUDGET_S
        probe_timeout = _CORRIDOR_GUARD_PROBE_S
        t0 = time.monotonic()
        print(
            f"\n  [corridor-yield] {len(stranded)} net(s) left unconnected by the "
            f"main strategy: {', '.join(_name(n) for n in stranded)}"
        )

        planned: dict[int, tuple[DifferentialPair, list[Route]]] = {}
        for net_id in stranded:
            if time.monotonic() > deadline:
                logger.warning(
                    "DIFFPAIR_CORRIDOR_GUARD_BUDGET: corridor-yield planning hit "
                    "its %.0fs budget with %d stranded net(s) unexamined "
                    "(issue #4463)",
                    _CORRIDOR_GUARD_BUDGET_S,
                    len(stranded) - len(planned),
                )
                break

            for _pair, routes in committed_pairs:
                _unmark(routes)
            desired = self._probe_net_routable(net_id, probe_timeout)

            neighbour_routes: dict[int, list[Route]] = {}
            if desired is None:
                coupled_ids = {id(r) for _p, rts in committed_pairs for r in rts}
                for other in candidate_nets:
                    if other == net_id:
                        continue
                    owned = [
                        r for r in autorouter.routes if r.net == other and id(r) not in coupled_ids
                    ]
                    if owned:
                        neighbour_routes[other] = owned
                        _unmark(owned)
                        for route in owned:
                            if route in autorouter.routes:
                                autorouter.routes.remove(route)
                desired = self._probe_net_routable(net_id, probe_timeout)

            # Put the board back exactly as it was -- this is a plan, not a
            # commitment.
            for owned in neighbour_routes.values():
                _restore(owned)
            for _pair, routes in committed_pairs:
                _remark(routes)

            if desired is None:
                print(
                    f"    [corridor-yield] {_name(net_id)} stays unroutable with "
                    f"every coupled corridor lifted -- not a corridor-competition "
                    f"failure"
                )
                continue

            blocking = [
                (pair, routes)
                for pair, routes in committed_pairs
                if self._copper_conflicts(routes, desired, clearance)
            ]
            if not blocking:
                print(
                    f"    [corridor-yield] no coupled copper sits on "
                    f"{_name(net_id)}'s path; nothing to yield for it"
                )
                continue
            for pair, routes in blocking:
                planned[id(pair)] = (pair, routes)
            print(
                f"    [corridor-yield] {_name(net_id)} is sealed in by "
                f"{', '.join(p.name for p, _r in blocking)}"
            )

        print(
            f"  [corridor-yield] planning took {time.monotonic() - t0:.1f}s; "
            f"{len(planned)} pair(s) to yield"
        )
        return list(planned.values()), stranded

    def _apply_corridor_yields(
        self,
        to_yield: list[tuple[DifferentialPair, list[Route]]],
        candidate_nets: list[int],
        non_diffpair_strategy: object,
    ) -> tuple[bool, set[int], list[Route], list[Route]]:
        """Rip the planned pairs, re-run the main strategy, keep only if it paid.

        Issue #4463.  Landing the freed nets one at a time here does NOT
        work: the corridor a coupled body was sealing is contested, so the
        stranded net and the freed pair legs have to be arranged TOGETHER
        -- which is precisely what the negotiated strategy does on a
        shadow-OFF run (it reaches 21/21 on board 06 with these nets routed
        single-ended).  So the pass rips the planned pairs, hands the board
        back to the caller's strategy for one more pass, and scores the
        result.

        The trade is transactional on REACH: if the second pass does not
        connect MORE of ``candidate_nets`` than the first left connected,
        every route it produced is unmarked and the yielded coupled copper
        is put back exactly as it was.  A pair that claims-but-strands
        costs reach, a pair that yields costs only quality, and a yield
        that buys neither is simply undone.

        Returns:
            ``(kept, released_net_ids, removed_routes, added_routes)``.
        """
        autorouter = self.autorouter
        if not to_yield:
            return False, set(), [], []

        reach_before = sum(1 for n in candidate_nets if self._net_is_connected(n))
        snapshot_ids = {id(r) for r in autorouter.routes}
        yielded_routes = [r for _p, routes in to_yield for r in routes]
        released_nets: set[int] = set()
        for pair, routes in to_yield:
            released_nets.update(pair.get_net_ids())
            for route in routes:
                with contextlib.suppress(Exception):
                    autorouter.grid.unmark_route(route)
                if route in autorouter.routes:
                    autorouter.routes.remove(route)

        print(
            f"  [corridor-yield] {len(to_yield)} pair(s) yielded their corridor: "
            f"{', '.join(p.name for p, _r in to_yield)}; re-running the main "
            f"strategy for the freed nets"
        )
        autorouter._negotiated_timeout_cap = _CORRIDOR_YIELD_RERUN_S
        try:
            non_diffpair_strategy()  # type: ignore[operator]
        finally:
            autorouter._negotiated_timeout_cap = None
            autorouter._budget_exit_diff_nets = set()

        added_routes = [r for r in autorouter.routes if id(r) not in snapshot_ids]
        reach_after = sum(1 for n in candidate_nets if self._net_is_connected(n))

        if reach_after > reach_before:
            logger.warning(
                "DIFFPAIR_CORRIDOR_YIELD: %d coupled pair(s) yielded a sealed "
                "corridor; reach %d -> %d of %d net(s): %s (issue #4463)",
                len(to_yield),
                reach_before,
                reach_after,
                len(candidate_nets),
                ", ".join(p.name for p, _r in to_yield),
            )
            print(
                f"  [corridor-yield] reach {reach_before} -> {reach_after} of "
                f"{len(candidate_nets)} net(s); keeping the yield"
            )
            return True, released_nets, yielded_routes, added_routes

        # The trade did not pay -- put the board back as the first pass left it.
        for route in added_routes:
            with contextlib.suppress(Exception):
                autorouter.grid.unmark_route(route)
            if route in autorouter.routes:
                autorouter.routes.remove(route)
        for route in yielded_routes:
            autorouter._mark_route(route)
            if route not in autorouter.routes:
                autorouter.routes.append(route)
        print(
            f"  [corridor-yield] reach {reach_before} -> {reach_after} of "
            f"{len(candidate_nets)} net(s); yield reverted"
        )
        return False, set(), [], []

    def route_all_with_diffpairs(
        self,
        diffpair_config: DifferentialPairConfig | None = None,
        net_order: list[int] | None = None,
        non_diffpair_strategy: object = None,
        coupled_only: bool = False,
        per_pair_timeout: float | None = None,
        per_pair_max_iterations: int | None = None,
        aggregate_timeout: float | None = None,
    ) -> tuple[list[Route], list[LengthMismatchWarning]]:
        """Route all nets with differential pair-aware routing.

        Differential pairs are routed first (they're most constrained),
        then remaining nets are routed using the standard router.

        Args:
            diffpair_config: Configuration for diff-pair routing.
            net_order: Optional explicit net ordering (basic strategy only).
            non_diffpair_strategy: Optional callable that routes non-diff-pair
                nets.  When provided (Issue #2464), the callable is invoked
                after the diff-pair pass and is expected to return a list of
                routes for the remaining nets.  When None, falls back to
                per-net basic routing via :meth:`Autorouter.route_net`.
            coupled_only: Issue #2464: When True, the diff-pair pass only
                produces routes for pairs that the CoupledPathfinder can
                handle; pairs with unsupported pad configurations (e.g.,
                3-pad nets) are deferred to the main strategy.  When
                False (default), preserves the legacy fall-back to
                independent routing.
            per_pair_timeout: Issue #3089: Optional per-pair wall-clock
                budget (seconds) for the inner
                :meth:`CoupledPathfinder.route_coupled` A*.  Forwarded
                through :meth:`route_differential_pair_coupled` (and the
                two-arg ``route_differential_pair`` indirection) so
                callers like ``boards/06-diffpair-test/generate_design.py``
                can bound any single coupled search and fall through to
                independent routing for pairs whose BGA-49 escape (USB3
                SS on board 06's J3/J4) would otherwise consume the
                whole CI budget.  Takes precedence over
                ``DifferentialPairConfig.per_pair_timeout`` when both
                are supplied; ``None`` defers to the config value, and
                if that is also ``None`` the legacy unbounded
                behaviour is preserved.
            aggregate_timeout: Issue #3439: Optional wall-clock budget
                (seconds) for the ENTIRE coupled diff-pair phase.  Once
                exhausted, all remaining pairs are deferred to the main
                strategy WITHOUT attempting coupled routing (the same
                budget-exit path as per-pair exits), so a board full of
                pathological pairs can never burn
                ``num_pairs * per_pair_timeout`` of the outer routing
                budget before the single-ended fallback runs -- the
                board-07 7/31-reach collapse.  Per-pair budgets are
                additionally clamped to the remaining aggregate budget.
                Takes precedence over
                ``DifferentialPairConfig.aggregate_timeout``; ``None``
                defers to the config value, and if that is also
                ``None`` the legacy per-pair-only behaviour is
                preserved.
        """
        # Issue #3089: prefer the explicit kwarg, otherwise fall back to
        # the config field so callers configuring everything via
        # ``DifferentialPairConfig(per_pair_timeout=60.0)`` work without
        # also having to pass the kwarg.
        effective_per_pair_timeout = per_pair_timeout
        if effective_per_pair_timeout is None and diffpair_config is not None:
            effective_per_pair_timeout = diffpair_config.per_pair_timeout
        # Issue #3144: same precedence pattern for the iteration budget.
        effective_per_pair_max_iterations = per_pair_max_iterations
        if effective_per_pair_max_iterations is None and diffpair_config is not None:
            effective_per_pair_max_iterations = diffpair_config.per_pair_max_iterations
        # Issue #3439: same precedence pattern for the aggregate budget.
        effective_aggregate_timeout = aggregate_timeout
        if effective_aggregate_timeout is None and diffpair_config is not None:
            effective_aggregate_timeout = getattr(diffpair_config, "aggregate_timeout", None)
        # Issue #3508: thread the shadow-constructor opt-in through to
        # ``route_differential_pair_coupled`` (instance attribute --
        # the coupled entry point has no config handle).
        if diffpair_config is not None:
            self.enable_shadow_construction = bool(
                getattr(diffpair_config, "enable_shadow_construction", False)
            )
        # Issue #4095: reset the budget-exit surface + instrumentation
        # counters up front (before any early return) so they always
        # describe only the latest invocation and never leak stale data
        # from a prior call on the same router (e.g. a coupled-success run
        # followed by a no-pairs run).
        self._last_budget_exit_pair_names = []
        self._last_coupled_attempted_count = 0
        self._last_budget_exit_count = 0
        # Issue #4463: pairs that yielded their corridor to unblock a
        # single-ended net (empty unless the corridor guard fired).
        self._last_corridor_yield_pair_names: list[str] = []
        # Issue #4799: same "latest invocation only" contract for the
        # per-instance census capture.  The process-wide collector behind the
        # JSON report is deliberately NOT reset here -- a run that routes in
        # several passes should report every crossover it scanned.
        self._census_records = []

        if diffpair_config is None or not diffpair_config.enabled:
            return self.autorouter.route_all(net_order), []

        print("\n=== Differential Pair Routing ===")

        diff_pairs_with_source = self.detect_differential_pairs_with_source()
        diff_pairs = [p for p, _ in diff_pairs_with_source]
        diff_net_ids: set[int] = set()

        if diff_pairs:
            print(f"  Detected {len(diff_pairs)} differential pairs:")
            for pair, source in diff_pairs_with_source:
                msg = f"    - {pair}: {pair.pair_type.value} (source: {source})"
                print(msg)
                logger.info("[diffpair-routing] %s", msg.strip())
                p_id, n_id = pair.get_net_ids()
                diff_net_ids.add(p_id)
                diff_net_ids.add(n_id)
        else:
            print("  No differential pairs detected")
            return self.autorouter.route_all(net_order), []

        for pair in diff_pairs:
            if pair.rules is not None:
                pair.rules = diffpair_config.get_rules(pair.pair_type)

        print("\n--- Routing differential pairs first (most constrained) ---")
        all_routes: list[Route] = []
        warnings: list[LengthMismatchWarning] = []
        # Issue #4095: local collector for the base names of pairs deferred
        # to the main strategy (per-pair or aggregate budget-exit); copied
        # onto the instance attribute after the loop for the CLI to read.
        budget_exit_pair_names: list[str] = []
        coupled_attempted_count = 0
        # Track diff-pair nets that we successfully routed so the
        # caller can decide which nets to leave for the main strategy.
        coupled_routed_nets: set[int] = set()
        # Issue #4463: (pair, routes) for every pair whose copper this
        # pre-phase committed -- the yield candidates for the
        # corridor-yield recovery that runs after the main strategy.
        committed_pair_routes: list[tuple[DifferentialPair, list[Route]]] = []

        refused_diff_nets: set[int] = set()
        # Issue #3089: track diff-pair nets whose coupled search hit the
        # ``per_pair_timeout`` budget.  Same handling as refused-engagement
        # nets: drop them from ``diff_net_ids`` so the main strategy picks
        # them up normally (the per-net C++ A* router is the right tool
        # for any single net the coupled search couldn't converge on).
        budget_exit_diff_nets: set[int] = set()
        # Issue #3439: aggregate coupled-phase deadline.  When set, the
        # whole pair loop must finish by this time; pairs that would
        # start after the deadline (or with <0.5s of budget left) are
        # deferred to the main strategy via the budget-exit path so a
        # failed coupled pre-pass can never starve the single-ended
        # fallback of wall-clock budget (the board-07 7/31 collapse).
        coupled_phase_deadline: float | None = None
        if effective_aggregate_timeout is not None and effective_aggregate_timeout > 0:
            coupled_phase_deadline = time.monotonic() + float(effective_aggregate_timeout)
        aggregate_deferred_pairs = 0
        for pair in diff_pairs:
            p_id, n_id = pair.get_net_ids()
            # Issue #2638, Epic #2556 Phase 2E: engagement gate.  Refuse
            # coupled routing when the pair's net class has not opted in
            # (default ``coupled_routing=False``) or when the pair is
            # single-ended-by-spec (#2527 lesson).  Refused pairs fall
            # through to the main strategy (their net IDs are removed
            # from ``diff_net_ids`` below so the main strategy picks
            # them up normally).
            engaged, reason = self._resolve_engagement(pair)
            if not engaged:
                msg = f"[diffpair-engage] refused {pair}: {reason}"
                print(f"  {msg}")
                logger.info(msg)
                refused_diff_nets.add(p_id)
                refused_diff_nets.add(n_id)
                continue
            # Issue #3439: aggregate budget check + per-pair clamp.
            pair_timeout = effective_per_pair_timeout
            if coupled_phase_deadline is not None:
                aggregate_remaining = coupled_phase_deadline - time.monotonic()
                if aggregate_remaining <= 0.5:
                    if aggregate_deferred_pairs == 0:
                        logger.warning(
                            "DIFFPAIR_AGGREGATE_BUDGET_EXCEEDED: coupled "
                            "diff-pair phase consumed its %.1fs aggregate "
                            "budget; deferring remaining pairs to the main "
                            "strategy (issue #3439)",
                            float(effective_aggregate_timeout),
                        )
                    print(
                        f"  [diffpair-aggregate] budget exhausted; deferring "
                        f"{pair} to main strategy"
                    )
                    budget_exit_diff_nets.add(p_id)
                    budget_exit_diff_nets.add(n_id)
                    # Issue #4095: record the deferred pair by name so the
                    # CLI can surface the aggregate budget-exit fallback.
                    budget_exit_pair_names.append(pair.name)
                    aggregate_deferred_pairs += 1
                    continue
                pair_timeout = (
                    min(pair_timeout, aggregate_remaining)
                    if pair_timeout is not None
                    else aggregate_remaining
                )
            # Issue #4095: this pair passed the engagement gate and the
            # aggregate-budget gate, so the coupled A* is about to run.
            coupled_attempted_count += 1
            if coupled_only:
                pair_routes, warning = self.route_differential_pair_coupled(
                    pair,
                    diffpair_config.spacing,
                    coupled_only=True,
                    per_pair_timeout=pair_timeout,
                    per_pair_max_iterations=effective_per_pair_max_iterations,
                )
            else:
                pair_routes, warning = self.route_differential_pair(
                    pair,
                    diffpair_config.spacing,
                    use_coupled_routing=True,  # Use coupled routing by default
                    per_pair_timeout=pair_timeout,
                    per_pair_max_iterations=effective_per_pair_max_iterations,
                )
            # Issue #3089: detect budget-exit via the pair's last_budget_exit
            # flag (set by route_differential_pair_coupled when the inner
            # CoupledPathfinder's last_timeout_exceeded fired and we
            # returned [], None to skip the slow independent fallback).
            # This is more direct than inferring from pair_routes being
            # empty (which could also mean engagement refusal or
            # independent fallback that found nothing).
            if not pair_routes and self._last_pair_budget_exit:
                budget_exit_diff_nets.add(p_id)
                budget_exit_diff_nets.add(n_id)
                # Issue #4095: record the deferred pair by name so the CLI
                # can surface the per-pair budget-exit fallback.
                budget_exit_pair_names.append(pair.name)
            if pair_routes:
                routed_for_net: dict[int, int] = {}
                for r in pair_routes:
                    routed_for_net[r.net] = routed_for_net.get(r.net, 0) + 1
                if routed_for_net.get(p_id, 0) > 0 and routed_for_net.get(n_id, 0) > 0:
                    coupled_routed_nets.add(p_id)
                    coupled_routed_nets.add(n_id)
                    # Issue #3508: a net whose intra-cluster stub edge
                    # failed is INCOMPLETE -- leave it routable so the
                    # main strategy can finish it (its committed coupled
                    # copper stays on the grid; the negotiated router
                    # connects the remaining pad through/around it).
                    stub_failed = getattr(self, "_last_stub_failed_nets", set())
                    for incomplete in stub_failed & {p_id, n_id}:
                        coupled_routed_nets.discard(incomplete)
                        print(
                            f"    [diffpair-stub] net {incomplete} has an "
                            f"unrouted stub edge; returning it to the main "
                            f"strategy (issue #3508)"
                        )
                # Issue #4463: remember what this pair committed so the
                # corridor-competition guard below can make it yield if its
                # copper seals a corridor a later single-ended net needs.
                committed_pair_routes.append((pair, list(pair_routes)))
            all_routes.extend(pair_routes)
            if warning:
                warnings.append(warning)

        if aggregate_deferred_pairs:
            print(
                f"  [diffpair-aggregate] deferred {aggregate_deferred_pairs} "
                f"pair(s) to main strategy after the aggregate coupled-phase "
                f"budget ({effective_aggregate_timeout:.1f}s) was exhausted"
            )

        # Issue #2464: When coupled_only=True, only the nets actually
        # routed by the CoupledPathfinder are reserved.  Pairs that fell
        # through (e.g., 3-pad nets) remain in the routable set so the
        # main strategy can pick them up.
        if coupled_only:
            diff_net_ids = coupled_routed_nets
        else:
            if refused_diff_nets:
                # Issue #2638 Phase 2E: engagement-refused pairs produced no
                # routes here; drop their nets from ``diff_net_ids`` so the
                # main strategy routes them normally.
                diff_net_ids = diff_net_ids - refused_diff_nets
            if budget_exit_diff_nets:
                # Issue #3089: coupled-routing-budget-exit pairs also
                # produced no routes; drop their nets from
                # ``diff_net_ids`` so the main strategy's per-net A*
                # (C++-accelerated) routes them normally.  Without this
                # the budget-exit nets would be excluded from the
                # main strategy AND have no coupled routes, leaving
                # them unrouted in the final PCB.
                # Issue #3473 (cosmetic): aggregate-deferred pairs are
                # also in ``budget_exit_diff_nets`` but already have
                # their own "[diffpair-aggregate] deferred N pair(s)"
                # print above; report only the genuinely per-pair
                # budget exits under the per-pair label.
                per_pair_deferred = max(
                    0, len(budget_exit_diff_nets) // 2 - aggregate_deferred_pairs
                )
                if per_pair_deferred:
                    print(
                        f"  Diff pairs deferred to main strategy due to "
                        f"per-pair budget: {per_pair_deferred}"
                    )
                diff_net_ids = diff_net_ids - budget_exit_diff_nets

        # Issue #4107 (tier b of #4095): early-abort the collapsed coupled
        # pass at the coupled->single-ended boundary.  When EVERY considered
        # coupled pair budget-exited and NOTHING coupled successfully (the
        # board-07 "total collapse" signature), the coupled pass left the
        # grid pristine -- ``route_differential_pair_coupled`` returns
        # ``[], None`` on budget-exit BEFORE any ``_mark_route`` commit, so
        # there is no partial coupled copper to unwind.  In that state the
        # ONLY divergence from a plain single-ended route is the #3270
        # net-priority promotion below (``_budget_exit_diff_nets`` ->
        # ``complexity_tier = -1``), which reorders the single-ended pass
        # away from its natural (better) ordering and is the measured
        # churn / net-loss driver (board 07: 34/22 vs single-ended 13/26).
        # Skip the promotion on collapse so the single-ended pass runs with
        # default ordering == byte-equivalent to a plain single-ended route.
        #
        # ``considered`` is every pair for which coupled routing was a
        # candidate: pairs that reached the coupled A* (``coupled_attempted_
        # count``) plus pairs deferred before it by the aggregate budget
        # (``aggregate_deferred_pairs``).  Collapse requires the coupled A*
        # to have actually run for >=1 pair (``coupled_attempted_count > 0``)
        # -- an aggregate-timeout-only deferral where no A* ran is NOT the
        # board-07 pathology.  PARTIAL exit (board 06: some pairs couple, or
        # only some exit) leaves ``coupled_routed_nets`` non-empty (or not
        # every considered pair exited) -> ``collapsed`` is False -> the
        # #3270 promotion runs exactly as today (measured beneficial there).
        #
        # Instrumentation is deliberately NOT gated: ``budget_exit_pair_names``
        # (the CLI warning + ``diffpair_budget_exit_pair_names()``) is a
        # separate object populated in the loop above and is left intact --
        # the operator is still told the pairs fell back; only the PROMOTION
        # is skipped.
        considered_coupled_pairs = coupled_attempted_count + aggregate_deferred_pairs
        collapsed = (
            coupled_attempted_count > 0
            and not coupled_routed_nets
            and len(budget_exit_pair_names) == considered_coupled_pairs
        )
        if collapsed:
            budget_exit_diff_nets = set()
            logger.warning(
                "DIFFPAIR_COUPLED_COLLAPSE_SKIP_PROMOTION: all %d considered "
                "coupled pair(s) budget-exited with none coupled; skipping the "
                "#3270 net-priority promotion so the single-ended pass runs "
                "with default ordering (pristine single-ended-equivalent "
                "result; issue #4107)",
                considered_coupled_pairs,
            )

        # Issue #3270: Surface budget-exit diff-pair nets to the
        # Autorouter so ``_get_net_priority`` can promote them to the
        # head of the non-diff main strategy's net order.  Without this
        # the budget-exit pair lands last and routes against a heavily
        # colonised grid; on board 06 seed=42 USB3_TX1+/U2.B2 then
        # bursts the per-net timeout (60s observed vs 30s budget) and
        # exhausts the strategy wall-clock before reaching MIPI_RST.
        # The set is cleared after the strategy returns to keep the
        # promotion local to this invocation.  On the #4107 collapse
        # signature ``budget_exit_diff_nets`` was emptied above, so this
        # assigns an empty set (no promotion).
        self.autorouter._budget_exit_diff_nets = set(budget_exit_diff_nets)

        # Issue #4463: tell the negotiated loop that this run carries coupled
        # pre-phase copper it cannot rip.  With that flag set the loop stops
        # once its stranded set is a zero-overflow fixed point instead of
        # re-deriving the same failure for its whole iteration ceiling
        # (board 06 shadow-ON seed 42: 10 iterations, 362.3s, no change) --
        # the corridor-yield recovery below is what can actually free those
        # nets.  Shadow-OFF runs never set it, so CI is untouched.
        self.autorouter._coupled_prephase_stall_exit = bool(
            _CORRIDOR_YIELD_ENABLED and self.enable_shadow_construction and committed_pair_routes
        )

        non_diff_nets = [n for n in self.autorouter.nets if n not in diff_net_ids and n != 0]
        if non_diff_nets:
            print(f"\n--- Routing {len(non_diff_nets)} non-differential nets ---")
            if non_diffpair_strategy is not None:
                # Issue #2464: Delegate non-diff-pair routing to the caller's
                # strategy (negotiated, MC, GA, etc.).  The callable is
                # responsible for routing every net in self.autorouter.nets;
                # diff-pair nets are filtered by the caller's net selection
                # since their pads are already marked as routed on the grid.
                try:
                    strategy_routes = non_diffpair_strategy()
                finally:
                    # Issue #3270: Clear the budget-exit promotion set so
                    # subsequent ``route_all`` / ``route_all_negotiated``
                    # invocations on the same autorouter inherit the
                    # default priority ordering (no leak across calls).
                    self.autorouter._budget_exit_diff_nets = set()
                # Filter out any routes for diff-pair nets that the strategy
                # may have re-routed (shouldn't happen if grid marking is
                # correct, but defend against it).
                for r in strategy_routes:
                    if r.net not in diff_net_ids:
                        all_routes.append(r)
            else:
                if net_order:
                    non_diff_order = [n for n in net_order if n in non_diff_nets]
                else:
                    non_diff_order = sorted(
                        non_diff_nets, key=lambda n: self.autorouter._get_net_priority(n)
                    )

                try:
                    for net in non_diff_order:
                        routes = self.autorouter.route_net(net)
                        all_routes.extend(routes)
                        if routes:
                            print(
                                f"  Net {net}: {len(routes)} routes, "
                                f"{sum(len(r.segments) for r in routes)} segments"
                            )
                finally:
                    # Issue #3270: clear the promotion set on the
                    # legacy per-net path too -- the priority lift
                    # is meaningful only for this strategy invocation.
                    self.autorouter._budget_exit_diff_nets = set()

        # Issue #4463: corridor-yield recovery.  The main strategy has now
        # told us -- on ground truth, not prediction -- which nets it could
        # not connect.  Any of those that becomes routable once the coupled
        # copper is lifted was stranded by corridor competition, so the pairs
        # standing on its path yield their corridor and the strategy is run
        # ONE more time with those nets back in the routable set.  Re-running
        # the strategy (rather than landing the nets one by one here) is what
        # makes the trade pay: the freed corridor is contested, and only the
        # negotiated loop can arrange the freed pair legs and the stranded net
        # together -- exactly what it does on a shadow-OFF run, which reaches
        # 21/21 with those nets routed single-ended.  The whole trade is
        # transactional on REACH: if the second pass does not connect more
        # nets than the first, every route is restored.  Shadow-OFF runs skip
        # this entirely, so CI and the committed artifacts are untouched.
        if (
            _CORRIDOR_YIELD_ENABLED
            and self.enable_shadow_construction
            and committed_pair_routes
            and non_diffpair_strategy is not None
        ):
            # Candidates are the main strategy's nets PLUS the diff-pair nets:
            # a pair the pre-phase claimed but left unconnected (board 06's
            # MIPI_D0, whose shadow declined and whose legs the negotiated loop
            # then failed) is itself a corridor-competition victim, and it is
            # not in ``non_diff_nets`` precisely because the claim removed it.
            candidate_nets = list(dict.fromkeys([*non_diff_nets, *sorted(diff_net_ids)]))
            to_yield, _stranded = self._plan_corridor_yields(candidate_nets, committed_pair_routes)
            kept, released_nets, removed_routes, added_routes = self._apply_corridor_yields(
                to_yield, candidate_nets, non_diffpair_strategy
            )
            if kept:
                diff_net_ids = diff_net_ids - released_nets
                coupled_routed_nets -= released_nets
                removed_ids = {id(r) for r in removed_routes}
                all_routes = [r for r in all_routes if id(r) not in removed_ids]
                all_routes.extend(added_routes)
                self._last_corridor_yield_pair_names = [p.name for p, _r in to_yield]
        self.autorouter._coupled_prephase_stall_exit = False

        # Issue #4095: surface the budget-exit pair set to the instance so
        # the CLI can warn that ``--differential-pairs`` fell back to
        # single-ended routing for these pairs.  De-duplicate while
        # preserving detection order (a pair can only budget-exit once, but
        # keep this defensive against future double-counting).  This does
        # NOT alter routing behavior -- the fallback + #3270 priority
        # promotion already ran above off the local ``budget_exit_diff_nets``
        # set; this is a pure visibility hook.
        seen: set[str] = set()
        deduped_names: list[str] = []
        for name in budget_exit_pair_names:
            if name not in seen:
                seen.add(name)
                deduped_names.append(name)
        self._last_budget_exit_pair_names = deduped_names
        self._last_coupled_attempted_count = coupled_attempted_count
        self._last_budget_exit_count = len(deduped_names)
        if deduped_names:
            # Instrumentation for a future checkpoint-and-compare follow-up
            # to key on: a structured, greppable log line naming the pairs
            # that fell back and the counts.
            # Denominator: pairs that reached the coupled A* plus pairs
            # deferred before it by the aggregate budget -- i.e. every pair
            # for which coupled routing was considered (engagement-refused
            # pairs are excluded; they were never candidates for coupling).
            considered = coupled_attempted_count + aggregate_deferred_pairs
            logger.warning(
                "DIFFPAIR_BUDGET_EXIT_FALLBACK: %d/%d coupled pair(s) "
                "budget-exited and fell back to single-ended routing: %s "
                "(issue #4095)",
                len(deduped_names),
                considered,
                ", ".join(deduped_names),
            )

        # Issue #4799: with the census on, close the phase with the aggregate
        # the per-crossover headers cannot give you.  Report-only: printed
        # after every routing decision in this phase has already been made.
        if _CROSSTAIL_CENSUS:
            print(CrossingTailCensusSummary.from_records(self._census_records).format_human())

        print("\n=== Differential Pair Routing Complete ===")
        print(f"  Total routes: {len(all_routes)}")
        print(f"  Differential pair nets: {len(diff_net_ids)}")
        print(f"  Other nets: {len(non_diff_nets)}")
        if warnings:
            print(f"  Length mismatch warnings: {len(warnings)}")
            for w in warnings:
                print(f"    - {w}")

        return all_routes, warnings
