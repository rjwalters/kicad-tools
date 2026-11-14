"""Buttress-wire reinforcement: same-net PTH anchors along a routed net.

Unit A of the #4218 buttress-wire reinforcement design (Part 2 of the #4215
ampacity feature), reworked for the owner's intended build in #8741. This is
a **post-route** pass that reads an already-routed board, walks the routed
copper of one named net into ordered polylines, and emits **same-net**
plated through-hole (PTH) anchor vias for a buttress wire.

The intended build: a hand-bent solid-core copper wire (default 16 AWG) is
laid along a high-current trace and soldered to that trace along its whole
length, held by **exactly two anchor holes, one at each end of the wire**.
One wire stays on one copper layer, so a current path that changes layer is
two wires.

Two placement modes (``anchor_mode``, Issue #8742):

* ``"ends"`` (the default): the net's copper is split into **legs** -- a
  maximal run on ONE copper layer with no junction inside it -- and each leg
  gets exactly two anchors, one near each end, none in between. Each end
  anchor's pad edge sits at least ``end_offset_mm`` clear, in straight-line
  distance, of the same-net copper the leg end lands on: a component pad or
  via the end of its track overlaps, or the whole trunk a branch tees off (a
  leg that bends near its end moves the anchor further inward until that
  holds). Both anchors are decided together: a leg with no room for two (too
  short, or folding back so they would overlap) is reported ``too_short``
  and gets none, and a leg ends with one anchor (``partial``) only when a
  clearance / hole-to-hole rule refused the other. A Y junction is a trunk (the longest
  through-path) plus a branch; loops and higher-degree junctions are
  reported ``unhandled``, never guessed. The per-leg result
  (:class:`LegSummary`, on :attr:`ReinforceResult.legs`) keeps each leg's
  polyline and its two anchor positions. A leg shorter than
  :data:`DEFAULT_MIN_WIRE_LENGTH_MM` (15.0 mm, the owner's ruling on #8741)
  gets no wire at all by default (Issue #8915): it is reported
  ``unanchored``, is omitted from the wire-list sidecar, and only an explicit
  ``min_wire_length_mm=0`` anchors it.
* ``"spaced"``: the historical row of anchors every ``spacing_mm`` along the
  longest run (or every run with ``all_runs``), unchanged.

``strategy`` (Issue #8916) chooses per leg between that buttress wire
(``"wire"``, the default) and **parallel copper** (``"parallel"``): a same-net,
same-width, vertex-for-vertex copy of the leg on the opposite outer layer, tied
to it at both ends and by stitching vias, which ``path_ampacity`` credits as a
parallel group on its summed cross-section. ``"auto"`` copies a leg below the
minimum wire length and wires the rest. A copy is refused whole, never written
in part (:class:`RefusedCopy`).

Until #8743 lands, anchors are still plain same-net vias that inherit the
board's via tenting, and no solder-mask opening is emitted along the run, so
the trace is not yet exposed for soldering.

Structural template: ``kct stitch`` (``cli/stitch_cmd.py``) -- a
load -> add-geometry-to-a-named-net -> save pass. Anchors are emitted via
:meth:`kicad_tools.schema.pcb.PCB.add_via` (a large plated via IS a PTH).

Key properties:

* **Same-net, no shorts.** Anchors carry the target net, so they never
  short the trace to itself.
* **Clearance-refuse, not silent short.** Before committing each anchor,
  the pass runs
  :func:`kicad_tools.router.via_clearance.point_clear_of_copper` against
  all *other*-net copper (tracks / vias / pads / filled zone polygons) and
  the manufacturer hole-to-hole floor. A colliding anchor is REFUSED and
  reported -- never silently placed (a short) and never silently dropped.
* **Junction-aware chaining.** Segment chaining reuses the hardened
  junction-splitting chainer
  (:func:`kicad_tools.router.optimizer.chain.sort_into_chains`) via a thin
  adapter from :class:`kicad_tools.schema.pcb.Segment` to
  :class:`kicad_tools.router.primitives.Segment`.

Out of scope (later units, tracked on #4218 / #8741): mask-opening channel
and mask-open anchors (#8743), wire-list sidecar (#8744), net-class
``reinforced`` wiring (Unit C), ampacity-credit coupling in #4217 (Unit E).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from kicad_tools.core.geometry import rotate_pad_offset
from kicad_tools.core.types import CopperLayer
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.physics.wire_gauge import (
    DEFAULT_WIRE_GAUGE_AWG,
    anchor_drill_for_awg,
    anchor_pad_for_drill,
    bare_copper_diameter_mm,
)
from kicad_tools.router.board_clearance_rules import BoardClearanceRules
from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    reinforcement_eligible_segment_ids,
    reinforcement_path_resolutions,
)
from kicad_tools.router.optimizer.algorithms import merge_collinear
from kicad_tools.router.optimizer.chain import sort_into_chains
from kicad_tools.router.optimizer.config import OptimizationConfig
from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable
from kicad_tools.router.primitives import Segment as RouterSegment
from kicad_tools.router.via_clearance import (
    FilledPolygonLike,
    ForeignPadTuple,
    TrackSegmentLike,
    drill_hole_to_hole_clear,
    point_clear_of_copper,
)
from kicad_tools.schema.pcb import PCB, Pad, Segment

# Default spacing between mid-run anchors (mm). Matches the architect's
# ``ReinforcementSpec.anchor_spacing_mm`` default.
DEFAULT_ANCHOR_SPACING_MM = 15.0

# Tier-2 nudge-fallback bound (#4319). When a candidate anchor collides with
# other-net copper, the pass searches along the run's arc-length axis by up to
# ``NUDGE_MAX_FRACTION_OF_SPACING * spacing_mm`` (symmetric ±), in
# ``NUDGE_STEPS`` increments each side, and places the first clear position
# before recording a refusal. A genuinely blocked position (nothing clear in
# the whole window) is still hard-refused -- the search is finite and cheap.
NUDGE_MAX_FRACTION_OF_SPACING = 0.5
NUDGE_STEPS = 4

# Anchor placement modes (Issue #8742). ``ends`` is the default: two anchors
# per single-layer leg, one at each end. ``spaced`` is the historical row.
ANCHOR_MODE_ENDS = "ends"
ANCHOR_MODE_SPACED = "spaced"
ANCHOR_MODES = (ANCHOR_MODE_ENDS, ANCHOR_MODE_SPACED)
DEFAULT_ANCHOR_MODE = ANCHOR_MODE_ENDS

# ``ends`` mode: minimum straight-line clearance (mm) between an end anchor's
# pad edge and the same-net copper the leg end lands on (component pad, via,
# or the trunk a branch tees off). Proposed default pending the
# operator's answer on #8741; a one-line change to override.
DEFAULT_END_OFFSET_MM = 1.0

# ``ends`` mode nudge: a refused end anchor is searched INWARD along the leg
# only (never outward onto the pad), by at most ``END_NUDGE_MAX_MM`` in
# ``END_NUDGE_STEPS`` increments, and never past the other end's anchor.
END_NUDGE_MAX_MM = 2.0
END_NUDGE_STEPS = 8

# ``ends`` mode: default minimum buttress-wire length (mm) -- the owner's
# ruling on #8741. A buttress wire needs a through-hole at each end, and each
# hole needs both outer layers (and the inner planes) clear; on a shorter link
# the two holes rarely fit, so no wire is wanted there at all. A leg below this
# is reported and left unanchored rather than anchored badly.
#
# ``spaced`` mode does NOT apply this default (its output must not change);
# only an explicit ``min_wire_length_mm`` / ``min_run_length_mm`` filters there.
# ``--strategy auto`` (#8916) takes its threshold from
# :func:`resolve_min_wire_length_mm`, which applies this default.
DEFAULT_MIN_WIRE_LENGTH_MM = 15.0

# Reinforcement strategies per leg (Issue #8916). ``wire`` is the buttress
# wire (two end anchors and a mask opening) and stays the default.
# ``parallel`` copies the leg onto the opposite outer layer and pins the copy
# to it with stitching vias. ``auto`` gives ``parallel`` to a leg shorter than
# the minimum wire length and ``wire`` to every other leg.
STRATEGY_WIRE = "wire"
STRATEGY_PARALLEL = "parallel"
STRATEGY_AUTO = "auto"
STRATEGIES = (STRATEGY_WIRE, STRATEGY_PARALLEL, STRATEGY_AUTO)
DEFAULT_STRATEGY = STRATEGY_WIRE

#: Nominal pitch (mm) of the stitching vias along a parallel copy; floored by
#: the same-net via spacing ``kct stitch`` uses (Issue #8916).
STITCH_PITCH_MM = 5.0

#: Copper layer -> the outer layer that takes its parallel copy (#8916).
OPPOSITE_OUTER_LAYER = {"F.Cu": "B.Cu", "B.Cu": "F.Cu"}

#: ``LegSummary.copy_status`` values (#8916).
COPY_PLACED = "placed"
COPY_REFUSED = "refused"

# Issue #8797: how far inward a refused end candidate may be searched. The
# default bound is a fraction of the leg's length (never less than
# ``END_NUDGE_MAX_MM``) so a near-miss refusal (one candidate 0.009 mm short
# of a clearance) slides the hole to the first clear position instead of
# leaving the leg ``partial``. ``--max-end-shift`` / ``max_end_shift_mm``
# overrides it in mm. The checker (``reinforcement_incomplete``) uses the same
# function for its end window, so a slid anchor is never reported as stray.
END_SHIFT_LEG_FRACTION = 0.25


def end_shift_bound(leg_length_mm: float, max_end_shift_mm: float | None = None) -> float:
    """Inward search bound (mm) for one end anchor of a leg ``leg_length_mm`` long."""
    if max_end_shift_mm is not None:
        return max_end_shift_mm
    return max(END_NUDGE_MAX_MM, END_SHIFT_LEG_FRACTION * leg_length_mm)


def _end_shift_offsets(bound_mm: float) -> list[float]:
    """Inward offsets (mm) tried for one end, at the nudge step, up to the bound."""
    step = END_NUDGE_MAX_MM / END_NUDGE_STEPS if END_NUDGE_STEPS > 0 else 0.0
    if step <= 0 or bound_mm <= 0:
        return [0.0]
    return [k * step for k in range(int(bound_mm / step + 1e-9) + 1)]


# ``ends`` mode: an end anchor that had to move more than this far inward
# along the leg (to get ``end_offset_mm`` of straight-line clearance from the
# end copper) makes the leg's ``reason`` say so (Issue #8781). 5x the default
# end offset: a bend just after a pad shifts an anchor by a few mm (the
# bent-exit fixtures move <= ~2 mm), while a leg running alongside its own end
# copper moves it by tens of mm.
_ANCHOR_SHIFT_NOTE_MM = 5.0

# Leg kinds and statuses reported on :class:`LegSummary` (Issue #8742).
LEG_KIND_RUN = "run"
LEG_KIND_TRUNK = "trunk"
LEG_KIND_BRANCH = "branch"
LEG_FULL = "full"
LEG_PARTIAL = "partial"
LEG_UNANCHORED = "unanchored"
LEG_TOO_SHORT = "too_short"
LEG_UNHANDLED = "unhandled"
#: Reinforced by a parallel copy on the opposite outer layer (#8916).
LEG_PARALLEL = "parallel"
#: This leg IS the parallel copy of another leg of the same pass (#8916): it
#: is reinforced together with that leg and gets no copy of its own.
LEG_COPY = "copy"

# Step (mm) of the march that finds where a leg leaves the copper it starts
# on; the crossing is then bisected to well below a micron.
_EXIT_MARCH_STEP_MM = 0.02

# Config used for the tier-3 within-run collinear merge (#4319). Only
# ``tolerance`` is consulted by :func:`merge_collinear`'s connectivity /
# direction predicates; the rest are inert here.
_MERGE_CONFIG = OptimizationConfig()

# Fallback design rules when no manufacturer profile is supplied. These are
# JLCPCB-tier-typical values; the annular-ring floor is what actually sizes
# the anchor pad. The reinforce pass NEVER hardcodes the annular ring at a
# call site -- it always reads it from these rules (real or fallback).
_FALLBACK_DESIGN_RULES = DesignRules(  # noqa: MFR001 reason="intentional manufacturer-agnostic fallback: reinforce is a post-route pass that runs when no manufacturer profile is supplied; a real profile overrides these JLCPCB-tier-typical defaults when threaded in"
    min_trace_width_mm=0.127,
    min_clearance_mm=0.2,
    min_via_drill_mm=0.3,
    min_via_diameter_mm=0.6,
    min_annular_ring_mm=0.25,
    min_hole_to_hole_mm=0.5,
)


# Governing-rule tags carried on :attr:`RefusedAnchor.rule` (#8085).
RULE_HOLE_TO_HOLE = "hole_to_hole"
RULE_DRU_CLASS_FLOOR = "dru_class_floor"
RULE_VOLTAGE_MAP_PAIRWISE = "voltage_map_pairwise"
RULE_DRU_CONDITIONAL = "dru_conditional"
#: ``ends`` mode: the candidate's pad would overlap an anchor already placed
#: for another leg (centre-to-centre below one anchor pad; Issue #8742).
RULE_ANCHOR_SPACING = "anchor_spacing"

#: Mask opening refused because the exposed run would sit closer to other-net
#: copper than the ``--voltage-map`` pairwise creepage requirement (#8743).
RULE_MASK_OPENING_PAIRWISE = "mask_opening_voltage_map_pairwise"
#: Same, when a class floor / custom DRU rule (not the voltage map) governs.
RULE_MASK_OPENING_CLEARANCE = "mask_opening_clearance"
#: A parallel copy's track would sit closer than KiCad's hole-to-copper
#: clearance to another net's drilled hole (#8916).
RULE_HOLE_CLEARANCE = "hole_clearance"
#: A parallel copy has no room: too short between its end ties, or its leg is
#: on an inner layer (no opposite outer layer to copy onto) (#8916).
RULE_COPY_GEOMETRY = "copy_geometry"

#: Copper layer -> the solder-mask layer on the same side (#8743).
MASK_LAYER_FOR_COPPER = {"F.Cu": "F.Mask", "B.Cu": "B.Mask"}

#: Leg ``mask_status`` values (#8743).
MASK_OPENED = "opened"
MASK_REFUSED = "refused"
MASK_SKIPPED = "skipped"

# Sample spacing (mm) when testing an exposed run against other-net copper;
# also capped at the exposed half-width so the disc chain leaves no gap.
_MASK_SAMPLE_STEP_MM = 0.25

_RULE_EPS = 1e-9


@dataclass
class RefusedAnchor:
    """An anchor position that was refused for clearance reasons."""

    x: float
    y: float
    reason: str
    #: Governing rule (``hole_to_hole`` / ``dru_class_floor`` /
    #: ``dru_conditional`` / ``voltage_map_pairwise``, and in ``ends`` mode
    #: ``anchor_spacing``) of the first candidate tried at this position.
    rule: str = ""
    #: Requirement of that rule (mm) and the measured gap (mm); ``0.0`` unknown.
    required_mm: float = 0.0
    actual_mm: float = 0.0
    #: Name of the obstacle net (empty for hole-to-hole against same-net holes).
    obstacle_net: str = ""


@dataclass
class PlacedAnchor:
    """An anchor via that was placed."""

    x: float
    y: float
    #: ``ends`` mode: how far (mm) inward along the leg this anchor was slid
    #: past its first candidate position because that was refused (Issue #8797).
    shift_mm: float = 0.0


@dataclass
class LegSummary:
    """One buttress-wire leg in ``ends`` mode (Issue #8742).

    A leg is a maximal run of the net's eligible copper on ONE copper layer
    with no junction inside it except at its ends -- one hand-bent wire. This
    is the public seam later slices consume (#8743 opens the solder mask
    along :attr:`polyline`; #8744 lists the wires), so the polyline is kept,
    not only the anchors.

    Coordinates are board-relative, like :class:`PlacedAnchor`.
    """

    net: str
    layer: str
    #: ``run`` (a plain leg), ``trunk`` (the longest through-path across a Y
    #: junction) or ``branch`` (the third arm of that Y, oriented from the
    #: junction to its free end).
    kind: str
    #: Ordered centreline vertices, collinear interior vertices removed.
    polyline: list[tuple[float, float]]
    length_mm: float
    #: The anchors actually placed on this leg this pass, in leg order
    #: (0, 1 or 2). In ``full`` status: ``[start-end anchor, far-end anchor]``.
    anchors: list[PlacedAnchor] = field(default_factory=list)
    #: ``full`` (2 placed) / ``partial`` (1: the other refused by a rule,
    #: listed in ``refused``) / ``unanchored`` (0: refused or
    #: not selected) / ``too_short`` / ``unhandled`` (loop, degree>=4 or
    #: multi-junction topology, or copper on a layer this call does not
    #: process).
    status: str = LEG_UNANCHORED
    #: Why the leg is not ``full``; for a ``full`` leg, non-empty only when
    #: an end anchor moved far inward (the wire is much shorter than the leg).
    reason: str = ""
    #: Arc length (mm) between the two placed end anchors, i.e. the span the
    #: wire covers (0.0 unless both anchors were placed). Later slices
    #: (#8743, #8744) read this rather than recomputing it.
    wire_span_mm: float = 0.0
    #: Leg length (mm) the wire does not span: ``length_mm - wire_span_mm`` for
    #: a ``full`` leg (0.0 otherwise; Issue #8797).
    uncovered_mm: float = 0.0
    #: Widest track width (mm) on the leg.
    width_mm: float = 0.0
    #: Names of the declared current paths covering this leg (empty without
    #: ``current_paths``).
    paths: list[str] = field(default_factory=list)
    #: Solder-mask opening outcome (#8743): ``opened`` / ``refused`` /
    #: ``skipped`` (empty when mask openings are off or the leg was not
    #: fully anchored) and why when not ``opened``.
    mask_status: str = ""
    mask_reason: str = ""
    #: True when this leg was left unanchored *because* it is shorter than the
    #: effective minimum buttress-wire length (Issue #8915) -- i.e. the owner
    #: does not want a wire here at all. Distinguishes that case from every
    #: other ``unanchored`` leg (refused by a rule, not selected without
    #: ``--all-runs``, excluded by a declared path), which is why
    #: :func:`kicad_tools.pcb.reinforcement_sidecar.build_wire_records` can
    #: omit exactly these legs from the wire list and no others.
    below_min_wire_length: bool = False
    #: Strategy this leg was reinforced with (Issue #8916): ``wire`` (two end
    #: anchors and a mask opening) or ``parallel`` (a copy on the opposite
    #: outer layer). Legs no strategy reached (unhandled, not selected) keep
    #: the pass's default ``wire``.
    strategy: str = DEFAULT_STRATEGY
    #: ``parallel`` legs: ``placed`` or ``refused`` (empty otherwise), why it
    #: was refused, and the copy itself (``None`` unless placed).
    copy_status: str = ""
    copy_reason: str = ""
    copy: ParallelCopy | None = None


@dataclass
class ParallelCopy:
    """A leg's same-net copy on the opposite outer layer (Issue #8916).

    Coordinates are board-relative. ``segments`` follow the leg vertex for
    vertex (``(start, end, width)`` each), so the path model sees the two as
    one doubled conductor; at an end that is not a through-hole pad (or a
    through via) the copy stops at the tie via there.
    """

    layer: str
    segments: list[tuple[tuple[float, float], tuple[float, float], float]]
    #: End ties and interior stitching vias, in leg order.
    stitch_vias: list[tuple[float, float]]
    via_size_mm: float
    via_drill_mm: float
    #: How many of the segments / vias were already on the board (re-run).
    existing_segments: int = 0
    existing_vias: int = 0


@dataclass
class RefusedCopy:
    """A parallel copy refused whole by a clearance or geometry rule (#8916)."""

    leg_layer: str
    copy_layer: str
    #: Where the copy (or its via) failed, board-relative.
    x: float
    y: float
    rule: str
    required_mm: float
    actual_mm: float
    obstacle_net: str = ""
    reason: str = ""


@dataclass
class MaskOpening:
    """One solder-mask opening graphic (a board-level ``gr_line``; #8743).

    Coordinates are board-relative. A bent leg yields one per straight piece.
    ``existing`` is True when an identical graphic was already on the board
    (idempotent re-run), so nothing new was written for it.
    """

    layer: str
    start: tuple[float, float]
    end: tuple[float, float]
    width_mm: float
    existing: bool = False


@dataclass
class RefusedOpening:
    """A leg whose mask opening was refused by a creepage/clearance rule (#8743)."""

    copper_layer: str
    start: tuple[float, float]
    end: tuple[float, float]
    rule: str
    required_mm: float
    actual_mm: float
    obstacle_net: str = ""


@dataclass
class UnresolvedPath:
    """A declared current path on the net that could not be used (#8742)."""

    path: str
    status: str
    reason: str


@dataclass
class RunSummary:
    """Per-run anchoring accounting (#4319, tier 2).

    One entry per chained run on the target layer -- including runs that were
    NOT anchored (filtered by the minimum run/wire length, or, in default
    mode, every run but the longest). This lets an agent distinguish "fully reinforced"
    from "2 of 31 runs reinforced" straight from ``--format json``.

    ``segment_count`` and ``length_mm`` reflect the *merged* geometric run
    (contiguous collinear fragments coalesced -- tier 3), not the raw
    fragment count.
    """

    run_index: int
    length_mm: float
    segment_count: int
    #: Number of anchor positions this run demands (endpoints + spacing).
    anchors_needed: int
    #: Anchor positions actually covered on this run (placed this pass, or
    #: already covered by a coincident anchor from another run).
    anchors_placed: int
    #: Anchor positions on this run that were hard-refused (no clear nudge).
    anchors_refused: int
    #: Set (Issue #4980) to a human-readable reason when a ``current_paths``
    #: declaration excluded this run from anchoring -- it was not fully
    #: covered by a resolved, reinforcement-eligible ``CurrentPathSpec``
    #: (e.g. a Kelvin sense tap, an unresolved trunk declaration, or copper
    #: no declared path covers at all). ``None`` when the run was not
    #: excluded -- either no ``current_paths`` were supplied to
    #: :func:`reinforce_net` at all (legacy, ungated behavior), or the run
    #: passed the gate.
    path_excluded_reason: str | None = None

    @property
    def fully_reinforced(self) -> bool:
        return self.anchors_needed > 0 and self.anchors_placed >= self.anchors_needed

    @property
    def partial(self) -> bool:
        return 0 < self.anchors_placed < self.anchors_needed

    @property
    def unanchored(self) -> bool:
        return self.anchors_placed == 0


@dataclass
class ReinforceResult:
    """Outcome of a :func:`reinforce_net` pass."""

    net_name: str
    wire_gauge_awg: int
    anchor_drill_mm: float
    anchor_pad_mm: float
    spacing_mm: float
    layer: str = ""
    #: ``ends`` or ``spaced`` (Issue #8742).
    anchor_mode: str = DEFAULT_ANCHOR_MODE
    #: ``ends`` mode end offset (mm); reported in ``spaced`` mode too.
    end_offset_mm: float = DEFAULT_END_OFFSET_MM
    #: ``ends`` mode: the inward-search bound override (mm), ``None`` = default
    #: fraction of the leg (Issue #8797).
    max_end_shift_mm: float | None = None
    #: Segments chained into the primary anchored (longest anchored) run,
    #: after the tier-3 collinear merge.
    run_segment_count: int = 0
    #: Cumulative arc length of the primary anchored run, in mm.
    run_length_mm: float = 0.0
    #: Number of chained runs on this net that were NOT anchored (branches /
    #: disconnected / below-threshold runs -- reported, not silently dropped).
    unhandled_runs: int = 0
    #: Per-run anchoring accounting (one entry per chained run). Additive over
    #: the legacy whole-net fields above.
    runs: list[RunSummary] = field(default_factory=list)
    placed: list[PlacedAnchor] = field(default_factory=list)
    refused: list[RefusedAnchor] = field(default_factory=list)
    #: ``ends`` mode: one entry per leg (empty in ``spaced`` mode).
    legs: list[LegSummary] = field(default_factory=list)
    #: Declared current paths on this net that did not resolve (both modes).
    unresolved_paths: list[UnresolvedPath] = field(default_factory=list)
    #: Mask openings are emitted (``ends`` mode default; #8743).
    mask_opening: bool = False
    #: Opening width override (mm); ``None`` = the leg's trace width.
    mask_width_mm: float | None = None
    #: Mask-opening graphics planned this pass (written unless ``dry_run``).
    openings: list[MaskOpening] = field(default_factory=list)
    #: Legs whose opening was refused (never a partial opening).
    refused_openings: list[RefusedOpening] = field(default_factory=list)
    #: ``wire`` / ``parallel`` / ``auto`` (Issue #8916).
    strategy: str = DEFAULT_STRATEGY
    #: Parallel copies planned this pass (written unless ``dry_run``) and the
    #: ones refused whole (never a partial copy).
    copies: list[ParallelCopy] = field(default_factory=list)
    refused_copies: list[RefusedCopy] = field(default_factory=list)

    @property
    def placed_count(self) -> int:
        return len(self.placed)

    @property
    def copies_placed_count(self) -> int:
        return len(self.copies)

    @property
    def refused_count(self) -> int:
        return len(self.refused)

    @property
    def runs_fully_reinforced(self) -> int:
        return sum(1 for r in self.runs if r.fully_reinforced)

    @property
    def runs_partial(self) -> int:
        return sum(1 for r in self.runs if r.partial)

    @property
    def runs_unanchored(self) -> int:
        return sum(1 for r in self.runs if r.unanchored)


class ReinforceError(ValueError):
    """Raised when the reinforce pass cannot proceed (e.g. unknown net)."""


# --------------------------------------------------------------------------
# ``--clear`` / ``--replace`` (Issue #8747): remove previously placed anchors
# --------------------------------------------------------------------------

#: Tolerance (mm) when matching a via's drill / pad to the anchor size.
_ANCHOR_SIZE_TOL_MM = 1e-3
#: Sidecar anchors are rounded to 4 decimals; board files to ~2.
_SIDECAR_ANCHOR_TOL_MM = 0.02
#: A mask-opening endpoint counts as on the net's copper within this distance.
_OPENING_ON_COPPER_TOL_MM = 0.02

#: Reason on a :class:`RefusedClear` whose removal would sever the net.
CLEAR_REFUSED_SEVERS_NET = (
    "removal would leave the net without its only layer link (copper that this "
    "anchor joins would be disconnected)"
)


@dataclass
class ClearedAnchor:
    """An anchor via removed (or, in a dry run, that would be removed)."""

    x: float
    y: float


@dataclass
class RefusedClear:
    """An anchor-sized via that was left in place by the connectivity guard."""

    x: float
    y: float
    reason: str = CLEAR_REFUSED_SEVERS_NET


@dataclass
class ClearedCopy:
    """A recorded parallel copy (Issue #8916) removed by ``--clear`` / ``--replace``.

    ``key`` is the sidecar record's :func:`~kicad_tools.pcb.reinforcement_sidecar.wire_key`,
    so the record can be dropped with its copper. Counts are of the board
    items matched to the record (a copy someone already deleted counts 0).
    """

    layer: str
    key: tuple
    segments: int = 0
    vias: int = 0
    reason: str = ""


@dataclass
class ClearResult:
    """Outcome of a :func:`clear_anchors` pass."""

    net_name: str
    wire_gauge_awg: int
    anchor_drill_mm: float
    anchor_pad_mm: float
    removed: list[ClearedAnchor] = field(default_factory=list)
    refused: list[RefusedClear] = field(default_factory=list)
    #: Solder-mask opening graphics removed with the anchors (#8743, #8747).
    openings_removed: list[MaskOpening] = field(default_factory=list)
    #: ``"sidecar"`` when the wire-list sidecar named the anchors, else ``"size"``.
    identified_by: str = "size"
    #: Parallel copies removed (Issue #8916), from the sidecar's records only.
    copies_removed: list[ClearedCopy] = field(default_factory=list)
    #: Parallel copies left in place by the connectivity guard.
    copies_refused: list[ClearedCopy] = field(default_factory=list)

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    @property
    def refused_count(self) -> int:
        return len(self.refused)


def _copper_component_count(model, removed: set[int]) -> int:
    """Connected groups of the net's copper, ignoring isolated vias.

    A group made only of vias (a stray via nothing is joined to) is not copper
    anybody relies on, so it never counts -- removing a leaf or isolated
    anchor must not look like a split.
    """
    comp = model.components(removed)
    groups: set[int] = set()
    for i, c in enumerate(comp):
        if c != -1 and model.nodes[i].kind != "via":
            groups.add(c)
    return len(groups)


def clear_anchors(
    pcb: PCB,
    net_name: str,
    *,
    wire_gauge_awg: int = DEFAULT_WIRE_GAUGE_AWG,
    design_rules: DesignRules | None = None,
    dry_run: bool = False,
    sidecar_wires: list[dict] | None = None,
) -> ClearResult:
    """Remove the buttress-wire anchors previously placed for ``net_name``.

    When ``sidecar_wires`` (the #8744 wire-list records) has wires for the net,
    the anchors they list identify the vias exactly. Otherwise an anchor is
    identified by size: a through via on ``net_name`` whose drill and pad equal
    :func:`anchor_drill_for_awg` / :func:`anchor_pad_for_drill` for
    ``wire_gauge_awg``. Vias on any other net are never touched.

    The net's solder-mask openings (#8743) go with the anchors, so no exposed
    run is left behind (:attr:`ClearResult.openings_removed`).

    Each removal goes through the connectivity model of
    :mod:`kicad_tools.pcb_prune`: an anchor whose removal would split the net's
    copper into more groups than before -- it is the net's only link between
    layers or copper groups -- is refused and reported in
    :attr:`ClearResult.refused`, and stays on the board. Candidates are
    judged one at a time against the removals already accepted, so when several
    anchors share a link, enough of them stay to keep it.

    With ``dry_run`` the board is not modified.

    Raises:
        ReinforceError: If the net does not exist on the board.
    """
    from kicad_tools.pcb_prune import _build_net_models

    rules = design_rules or _FALLBACK_DESIGN_RULES
    net_obj = pcb.get_net_by_name(net_name)
    if net_obj is None and not any(s.net_name == net_name for s in pcb.segments):
        raise ReinforceError(f"net {net_name!r} not found on board")
    net_number = net_obj.number if net_obj is not None else None

    drill = anchor_drill_for_awg(wire_gauge_awg)
    pad = anchor_pad_for_drill(drill, rules.min_annular_ring_mm)
    result = ClearResult(
        net_name=net_name,
        wire_gauge_awg=wire_gauge_awg,
        anchor_drill_mm=drill,
        anchor_pad_mm=pad,
    )

    ox, oy = getattr(pcb, "board_origin", None) or (0.0, 0.0)
    on_net = [
        v
        for v in pcb.vias
        if v.via_type is None
        and (
            (net_number is not None and v.net_number == net_number)
            or (net_number is None and v.net_name == net_name)
        )
    ]
    # The wire-list sidecar (#8744) names the anchors exactly (sheet-absolute);
    # without wires for this net fall back to the size rule.
    listed = [
        (a["x"], a["y"])
        for w in (sidecar_wires or [])
        if w.get("net") == net_name
        for a in w.get("anchors", [])
    ]
    copies = _recorded_copies(pcb, net_name, net_number, sidecar_wires or [])
    if listed or copies:
        result.identified_by = "sidecar"
        candidates = [
            v
            for v in on_net
            if any(
                abs(v.position[0] + ox - ax) <= _SIDECAR_ANCHOR_TOL_MM
                and abs(v.position[1] + oy - ay) <= _SIDECAR_ANCHOR_TOL_MM
                for ax, ay in listed
            )
        ]
    else:
        candidates = [
            v
            for v in on_net
            if abs(v.drill - drill) <= _ANCHOR_SIZE_TOL_MM
            and abs(v.size - pad) <= _ANCHOR_SIZE_TOL_MM
        ]
    if not candidates and not copies:
        return result

    model = _build_net_models(pcb).get(net_number) if net_number is not None else None
    if model is None and candidates:
        model = _build_net_models(pcb).get(candidates[0].net_number)
    node_of = {}
    if model is not None:
        node_of = {id(n.ref): i for i, n in enumerate(model.nodes) if n.kind in ("via", "seg")}

    gone: set[int] = set()
    baseline = _copper_component_count(model, set()) if model is not None else 0
    # Issue #8916: a recorded parallel copy goes whole (its tracks and its
    # stitching vias) or not at all, under the same connectivity guard.
    copy_segments: list[Segment] = []
    copy_vias: list = []
    for cleared, segs, vias in copies:
        idxs = {node_of[id(x)] for x in (*segs, *vias) if id(x) in node_of}
        if model is not None and _copper_component_count(model, gone | idxs) > baseline:
            cleared.reason = CLEAR_REFUSED_SEVERS_NET
            result.copies_refused.append(cleared)
            continue
        gone |= idxs
        copy_segments.extend(segs)
        copy_vias.extend(vias)
        result.copies_removed.append(cleared)
    if not dry_run:
        pcb.remove_segments(copy_segments)
        pcb.remove_vias(copy_vias)

    accepted = []
    for via in candidates:
        idx = node_of.get(id(via))
        if model is not None and idx is not None:
            if _copper_component_count(model, gone | {idx}) > baseline:
                result.refused.append(RefusedClear(x=via.position[0], y=via.position[1]))
                continue
            gone.add(idx)
        accepted.append(via)
        result.removed.append(ClearedAnchor(x=via.position[0], y=via.position[1]))

    if accepted:
        stale = _stale_mask_openings(pcb, net_name, result)
        result.openings_removed = [o for o, _ in stale]
        if not dry_run:
            pcb.remove_vias(accepted)
            pcb.remove_graphic_lines([g for _, g in stale])
    return result


def _recorded_copies(
    pcb: PCB, net_name: str, net_number: int | None, wires: list[dict]
) -> list[tuple[ClearedCopy, list[Segment], list]]:
    """Board copper of the sidecar's parallel-copy records for ``net_name`` (#8916).

    Only what a record names is matched -- a same-net track on the copy layer
    whose two ends equal one recorded copy segment's, and a same-net via at a
    recorded stitching position with the recorded size -- never copper found
    by shape alone.
    """
    from kicad_tools.pcb.reinforcement_sidecar import is_parallel, wire_key

    ox, oy = getattr(pcb, "board_origin", None) or (0.0, 0.0)
    tol = _SIDECAR_ANCHOR_TOL_MM

    def on_net(item) -> bool:
        if net_number is not None:
            return bool(item.net_number == net_number)
        return bool(item.net_name == net_name)

    def near(p, rec) -> bool:
        return bool(abs(p[0] + ox - rec["x"]) <= tol and abs(p[1] + oy - rec["y"]) <= tol)

    out = []
    for w in wires:
        if w.get("net") != net_name or not is_parallel(w) or not w.get("copy_layer"):
            continue
        layer = w["copy_layer"]
        try:
            recs = [(r["start"], r["end"], r.get("width_mm")) for r in w.get("copy_segments") or []]
            stitches = list(w.get("stitch_vias") or [])
            size = w.get("stitch_via_size_mm")
            drill = w.get("stitch_via_drill_mm")
            key = wire_key(w)
        except (KeyError, TypeError):
            continue
        segs = [
            s
            for s in pcb.segments
            if s.layer == layer
            and on_net(s)
            and any(
                ((near(s.start, a) and near(s.end, b)) or (near(s.start, b) and near(s.end, a)))
                and (width is None or abs(s.width - float(width)) <= _ANCHOR_SIZE_TOL_MM)
                for a, b, width in recs
            )
        ]
        vias = [
            v
            for v in pcb.vias
            if on_net(v)
            and any(near(v.position, r) for r in stitches)
            and (size is None or abs(v.size - float(size)) <= _ANCHOR_SIZE_TOL_MM)
            and (drill is None or abs(v.drill - float(drill)) <= _ANCHOR_SIZE_TOL_MM)
        ]
        out.append(
            (ClearedCopy(layer=layer, key=key, segments=len(segs), vias=len(vias)), segs, vias)
        )
    return out


def _dist_to_segment(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _stale_mask_openings(pcb: PCB, net_name: str, result: ClearResult) -> list:
    """Mask-opening ``gr_line``s of ``net_name`` left without their anchors (#8747).

    An opening (#8743) is a ``gr_line`` on F.Mask/B.Mask whose endpoints both lie
    on the net's copper on the matching layer. One touching an anchor that
    stays (refused by the connectivity guard) is kept: its wire is still in use.
    Returns ``(MaskOpening, GraphicLine)`` pairs, board-relative coordinates.
    """
    ox, oy = getattr(pcb, "board_origin", None) or (0.0, 0.0)
    kept = [(r.x, r.y) for r in result.refused]
    out = []
    for copper, mask in MASK_LAYER_FOR_COPPER.items():
        segs = [
            (s.start, s.end) for s in pcb.segments if s.layer == copper and s.net_name == net_name
        ]
        if not segs:
            continue

        def on_copper(pt: tuple[float, float]) -> bool:
            return any(_dist_to_segment(pt, a, b) <= _OPENING_ON_COPPER_TOL_MM for a, b in segs)

        for g in pcb.graphic_lines_on(mask):
            a = (g.start[0] - ox, g.start[1] - oy)
            b = (g.end[0] - ox, g.end[1] - oy)
            if not (on_copper(a) and on_copper(b)):
                continue
            if any(
                math.dist(a, k) <= _SIDECAR_ANCHOR_TOL_MM
                or math.dist(b, k) <= _SIDECAR_ANCHOR_TOL_MM
                for k in kept
            ):
                continue
            out.append((MaskOpening(mask, a, b, g.width), g))
    return out


def _seg_length(seg: Segment) -> float:
    (x1, y1), (x2, y2) = seg.start, seg.end
    return math.hypot(x2 - x1, y2 - y1)


def _to_router_segment(seg: Segment) -> RouterSegment:
    """Adapt a schema ``Segment`` to the chainer's ``router`` ``Segment``.

    The junction-aware chainer (:func:`sort_into_chains`) operates on
    :class:`kicad_tools.router.primitives.Segment` (``x1/y1/x2/y2``), while
    a parsed board yields :class:`kicad_tools.schema.pcb.Segment`
    (``start``/``end`` tuples). This trivial field mapping lets the pass
    reuse the single hardened chaining implementation rather than fork it.
    """
    (x1, y1), (x2, y2) = seg.start, seg.end
    # ``sort_into_chains`` only reads the ``x1/y1/x2/y2`` endpoints; the
    # ``layer`` field (a ``CopperLayer`` enum on the router type) is carried
    # along but never consulted, so a placeholder is fine. Callers pre-filter
    # segments to a single layer before chaining.
    return RouterSegment(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        width=seg.width,
        layer=CopperLayer.F_CU,
        net=seg.net_number,
        net_name=seg.net_name,
    )


def _chain_polylines(
    segments: list[Segment], *, original_ids: dict[int, int] | None = None
) -> list[list[Segment]]:
    """Chain schema segments into ordered, junction-split polylines.

    Returns ordered schema segments walked endpoint-to-endpoint, copying
    segments that need reversing. When supplied, ``original_ids`` maps each
    returned object's id to its original PCB object's id. Junctions
    (degree>=3) split runs so branched nets are not silently merged.
    """
    if not segments:
        return []

    router_segs = [_to_router_segment(s) for s in segments]
    chains = sort_into_chains(router_segs)

    # Map each returned (possibly flipped) router segment back to the
    # original schema segment by index. ``sort_into_chains`` preserves the
    # ``width``/``layer``/``net`` fields and only ever flips endpoints, so we
    # match on the unordered endpoint pair + width to recover the index.
    remaining = list(range(len(segments)))

    def _match(rseg: RouterSegment) -> int:
        target_pts = frozenset(
            {
                (round(rseg.x1, 6), round(rseg.y1, 6)),
                (round(rseg.x2, 6), round(rseg.y2, 6)),
            }
        )
        for idx in remaining:
            s = segments[idx]
            pts = frozenset(
                {
                    (round(s.start[0], 6), round(s.start[1], 6)),
                    (round(s.end[0], 6), round(s.end[1], 6)),
                }
            )
            if pts == target_pts and abs(s.width - rseg.width) < 1e-9:
                return idx
        # Fallback: endpoint-only match (width drift tolerance).
        for idx in remaining:
            s = segments[idx]
            pts = frozenset(
                {
                    (round(s.start[0], 6), round(s.start[1], 6)),
                    (round(s.end[0], 6), round(s.end[1], 6)),
                }
            )
            if pts == target_pts:
                return idx
        return -1

    result: list[list[Segment]] = []
    for chain in chains:
        run: list[Segment] = []
        for rseg in chain:
            idx = _match(rseg)
            if idx < 0:
                continue
            remaining.remove(idx)
            # Re-orient the original schema segment to match the chained
            # walk direction so the arc-length walk is continuous.
            orig = segments[idx]
            if abs(orig.start[0] - rseg.x1) < 1e-6 and abs(orig.start[1] - rseg.y1) < 1e-6:
                run.append(orig)
            else:
                run.append(
                    Segment(
                        start=orig.end,
                        end=orig.start,
                        width=orig.width,
                        layer=orig.layer,
                        net_number=orig.net_number,
                        net_name=orig.net_name,
                        uuid=orig.uuid,
                    )
                )
            if original_ids is not None:
                original_ids[id(run[-1])] = id(orig)
        if run:
            result.append(run)
    return result


def _run_length(run: list[Segment]) -> float:
    return sum(_seg_length(s) for s in run)


def _cumulative_arc(points: list[tuple[float, float]]) -> list[float]:
    """Cumulative arc length at each vertex of a polyline."""
    cum: list[float] = [0.0]
    for i in range(1, len(points)):
        (x0, y0), (x1, y1) = points[i - 1], points[i]
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    return cum


def _anchor_arc_targets(total: float, spacing: float) -> list[float]:
    """Arc-length sample points: 0, spacing, 2*spacing, ... and the endpoint.

    Always includes the final endpoint (so both true endpoints are anchored
    even if the last interval is shorter than ``spacing``).
    """
    if total <= 0:
        return [0.0]
    targets: list[float] = []
    d = 0.0
    while d < total - 1e-9:
        targets.append(d)
        d += spacing
    targets.append(total)
    return targets


def _point_at_arc(
    points: list[tuple[float, float]], cum: list[float], t: float
) -> tuple[float, float]:
    """Interpolate the point at arc length ``t`` along a polyline."""
    if len(points) < 2:
        return points[0]
    total = cum[-1]
    t = max(0.0, min(total, t))
    seg_i = 0
    while seg_i < len(cum) - 2 and cum[seg_i + 1] < t:
        seg_i += 1
    seg_len = cum[seg_i + 1] - cum[seg_i]
    if seg_len <= 1e-12:
        return points[seg_i]
    frac = max(0.0, min(1.0, (t - cum[seg_i]) / seg_len))
    (x0, y0), (x1, y1) = points[seg_i], points[seg_i + 1]
    return (x0 + frac * (x1 - x0), y0 + frac * (y1 - y0))


def _anchor_positions(
    points: list[tuple[float, float]], spacing: float
) -> list[tuple[float, float]]:
    """Sample anchor positions along a polyline by arc length.

    Places anchors at arc-length 0, ``spacing``, ``2*spacing``, ... and
    always at the final endpoint (so both true endpoints are anchored even
    if the last interval is shorter than ``spacing``).
    """
    if len(points) < 2:
        return list(points)
    if spacing <= 0:
        raise ReinforceError(f"spacing must be positive, got {spacing}")
    cum = _cumulative_arc(points)
    return [_point_at_arc(points, cum, t) for t in _anchor_arc_targets(cum[-1], spacing)]


@dataclass
class _RunGeom:
    """Merged geometry of one chained run (tier-3 collinear-coalesced)."""

    points: list[tuple[float, float]]
    segment_count: int
    length_mm: float


def _run_geometry(run: list[Segment]) -> _RunGeom:
    """Collapse contiguous collinear fragments within an oriented run (#4319).

    Tier 3: the junction-aware chainer (:func:`sort_into_chains`) already
    walks collinear degree-2 fragments head-to-tail into ONE run, but leaves
    them as separate ``Segment`` objects, so ``run_segment_count`` reports the
    raw fragment count (e.g. a straight branch expressed as many short
    octilinear pieces). This step reuses
    :func:`kicad_tools.router.optimizer.algorithms.merge_collinear` to coalesce
    those fragments so the reported ``segment_count`` reflects the true
    geometric run.

    The merge operates ONLY within an already-split run, so it never bridges a
    junction (degree>=3) -- the #2389 no-branch-drop guarantee that
    ``sort_into_chains`` provides is preserved. It removes only *collinear*
    interior vertices, so the arc-length geometry (and therefore every anchor
    position) is byte-for-byte unchanged.
    """
    if not run:
        return _RunGeom(points=[], segment_count=0, length_mm=0.0)
    router_segs = [_to_router_segment(s) for s in run]
    merged = merge_collinear(router_segs, _MERGE_CONFIG)
    points: list[tuple[float, float]] = [(merged[0].x1, merged[0].y1)]
    length = 0.0
    for m in merged:
        points.append((m.x2, m.y2))
        length += math.hypot(m.x2 - m.x1, m.y2 - m.y1)
    return _RunGeom(points=points, segment_count=len(merged), length_mm=length)


def _nudge_arc_candidates(t: float, total: float, spacing: float):
    """Yield the base arc length then symmetric bounded nudges (#4319, tier 2).

    Order: the base position first, then +/- increments outward, up to
    ``NUDGE_MAX_FRACTION_OF_SPACING * spacing`` on each side. Candidates are
    clamped to ``[0, total]``.
    """
    yield max(0.0, min(total, t))
    max_off = spacing * NUDGE_MAX_FRACTION_OF_SPACING
    if max_off <= 0 or NUDGE_STEPS <= 0:
        return
    step = max_off / NUDGE_STEPS
    for k in range(1, NUDGE_STEPS + 1):
        off = step * k
        for cand in (t + off, t - off):
            if -1e-9 <= cand <= total + 1e-9:
                yield max(0.0, min(total, cand))


def _too_close(p: tuple[float, float], placed: list[tuple[float, float]], floor: float) -> bool:
    """True when ``p`` is within ``floor`` (center-to-center) of a placed anchor."""
    return any(math.hypot(p[0] - q[0], p[1] - q[1]) < floor - 1e-9 for q in placed)


@dataclass(frozen=True)
class _Violation:
    """The rule an anchor candidate breaks, with required vs measured gap."""

    rule: str
    required_mm: float
    actual_mm: float
    obstacle_net: str = ""


@dataclass
class _ClearancePolicy:
    """Per-obstacle-net required clearance for the target net (#8085).

    ``required = max(fab floor, target class floor, obstacle class floor,
    pairwise voltage-map requirement)``; the pairwise term is only present
    when a :class:`PairwiseClearanceTable` was supplied.
    """

    fab_floor: float
    target_net: str
    class_floors: Mapping[str, float]
    pairwise: PairwiseClearanceTable | None
    #: The board's ``.kicad_pro`` / ``.kicad_dru`` resolver (#8494); its
    #: per-pair requirement (incl. condition-based custom rules) is max-ed in.
    board_rules: BoardClearanceRules | None = None

    def required(self, obstacle_net: str) -> tuple[float, str]:
        floor = max(
            self.fab_floor,
            self.class_floors.get(self.target_net, 0.0),
            self.class_floors.get(obstacle_net, 0.0),
        )
        pair = (
            self.pairwise.required_clearance(self.target_net, obstacle_net)
            if self.pairwise is not None and obstacle_net
            else 0.0
        )
        dru = (
            self.board_rules.net_requirement(self.target_net, [obstacle_net])
            if self.board_rules is not None
            else 0.0
        )
        if dru > max(floor, pair) + _RULE_EPS:
            return dru, RULE_DRU_CONDITIONAL
        if pair > floor + _RULE_EPS:
            return pair, RULE_VOLTAGE_MAP_PAIRWISE
        return floor, RULE_DRU_CLASS_FLOOR


def _group_clear(
    x: float, y: float, anchor_pad: float, clearance: float, group: _NetCopper
) -> bool:
    return point_clear_of_copper(
        x,
        y,
        via_size=anchor_pad,
        clearance=clearance,
        other_net_tracks=group.tracks,
        other_net_vias=group.vias,
        other_net_pads=group.pads,
        other_net_filled_polygons=group.filled_polygons,
    )


def _actual_gap(x: float, y: float, anchor_pad: float, required: float, group: _NetCopper) -> float:
    """Measured copper gap (mm) to ``group``, by bisecting the shared predicate."""
    lo, hi = 0.0, required
    if not _group_clear(x, y, anchor_pad, 0.0, group):
        return 0.0
    for _ in range(16):
        mid = (lo + hi) / 2.0
        if _group_clear(x, y, anchor_pad, mid, group):
            lo = mid
        else:
            hi = mid
    return lo


def _position_violation(
    x: float,
    y: float,
    anchor_pad: float,
    anchor_drill: float,
    rules: DesignRules,
    other: _OtherNetCopper,
    policy: _ClearancePolicy,
) -> _Violation | None:
    """Clearance re-check of a candidate anchor; ``None`` when it is clear.

    Hole-to-hole is checked against every drill on the board, same-net PTH
    pads and vias included (a same-net hole is still a separate drilled hole).
    Copper clearance is then checked per obstacle net against that net's own
    requirement (:class:`_ClearancePolicy`).
    """
    if other.all_drills and not drill_hole_to_hole_clear(
        x,
        y,
        anchor_drill,
        [(dx, dy, dd) for dx, dy, dd in other.all_drills],
        min_hole_to_hole=rules.min_hole_to_hole_mm,
    ):
        gap = min(
            math.hypot(dx - x, dy - y) - anchor_drill / 2.0 - dd / 2.0
            for dx, dy, dd in other.all_drills
        )
        return _Violation(RULE_HOLE_TO_HOLE, rules.min_hole_to_hole_mm, max(0.0, gap))

    for net_number, group in other.by_net.items():
        net = other.net_names.get(net_number, "")
        required, rule = policy.required(net)
        if not _group_clear(x, y, anchor_pad, required, group):
            return _Violation(rule, required, _actual_gap(x, y, anchor_pad, required, group), net)
    return None


class _TrackObstacle:
    """Minimal duck-typed track for ``point_clear_of_copper``."""

    __slots__ = ("start_x", "start_y", "end_x", "end_y", "width")

    def __init__(self, sx: float, sy: float, ex: float, ey: float, width: float) -> None:
        self.start_x = sx
        self.start_y = sy
        self.end_x = ex
        self.end_y = ey
        self.width = width


class _FilledPolyObstacle:
    """Minimal duck-typed filled polygon for ``point_clear_of_copper``."""

    __slots__ = ("points", "min_x", "min_y", "max_x", "max_y")

    def __init__(self, points: list[tuple[float, float]]) -> None:
        self.points = points
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        self.min_x = min(xs)
        self.min_y = min(ys)
        self.max_x = max(xs)
        self.max_y = max(ys)


@dataclass
class _OtherNetCopper:
    """Other-net copper grouped by net, plus every drill on the board."""

    by_net: dict[int, _NetCopper] = field(default_factory=dict)
    net_names: dict[int, str] = field(default_factory=dict)
    #: ``(x, y, drill_mm)`` of every via and PTH pad, same-net included.
    all_drills: list[tuple[float, float, float]] = field(default_factory=list)


@dataclass
class _NetCopper:
    """Copper of one obstacle net for the clearance re-check."""

    tracks: list[TrackSegmentLike] = field(default_factory=list)
    vias: list[tuple[float, float, float, int]] = field(default_factory=list)
    pads: list[ForeignPadTuple] = field(default_factory=list)
    filled_polygons: list[FilledPolygonLike] = field(default_factory=list)


def _collect_other_net_copper(
    pcb: PCB, target_net: int, anchor_drill: float, anchor_pad: float
) -> _OtherNetCopper:
    """Build the obstacle set: other-net copper by net, all drills (any net).

    A same-net via that already has this pass's anchor geometry is a previous
    anchor, not an obstacle: skipping it keeps a re-run idempotent (the same
    candidates are chosen and ``add_via(dedupe=True)`` drops the duplicates).
    """
    other = _OtherNetCopper()
    other.net_names = {n.number: n.name for n in pcb.nets.values()}

    def group(net_number: int) -> _NetCopper:
        return other.by_net.setdefault(net_number, _NetCopper())

    for seg in pcb.segments:
        if seg.net_number == target_net:
            continue
        (sx, sy), (ex, ey) = seg.start, seg.end
        group(seg.net_number).tracks.append(_TrackObstacle(sx, sy, ex, ey, seg.width))

    for via in pcb.vias:
        vx, vy = via.position
        own_anchor = (
            via.net_number == target_net
            and abs(via.drill - anchor_drill) < 1e-6
            and abs(via.size - anchor_pad) < 1e-6
        )
        if via.drill > 0 and not own_anchor:
            other.all_drills.append((vx, vy, via.drill))
        if via.net_number == target_net:
            continue
        group(via.net_number).vias.append((vx, vy, via.size, via.net_number))

    for fp in pcb.footprints:
        for pad in fp.pads:
            pos = pcb.get_pad_position(fp.reference, pad.number)
            if pos is None:
                continue
            px, py = pos
            if pad.drill > 0:
                other.all_drills.append((px, py, pad.drill))
            if pad.net_number == target_net:
                continue
            # Effective bounding radius of the pad (disc model).
            radius = max(pad.size) / 2.0 if pad.size else 0.0
            group(pad.net_number).pads.append((px, py, radius, pad.net_number))

    for zone in pcb.zones:
        if zone.net_number == target_net:
            continue
        for poly in zone.filled_polygons:
            if len(poly) >= 3:
                group(zone.net_number).filled_polygons.append(_FilledPolyObstacle(list(poly)))

    return other


def resolve_min_wire_length_mm(
    min_wire_length_mm: float | None,
    min_run_length_mm: float | None,
    anchor_mode: str = DEFAULT_ANCHOR_MODE,
) -> float | None:
    """Effective minimum wire/run length (mm) for a pass, or ``None`` for no filter.

    ``--min-wire-length`` (Issue #8915) and the older ``--min-run-length``
    (#4319) name the same quantity, so either sets it and, when both are
    given, **the larger wins** -- neither flag may quietly lower a minimum the
    other asked for.

    The difference is the *default*. In ``ends`` mode, giving neither yields
    :data:`DEFAULT_MIN_WIRE_LENGTH_MM` (15.0): a buttress wire needs a hole at
    each end and they rarely fit on a shorter link. Passing ``0`` explicitly
    restores the pre-#8915 "no minimum" behaviour -- which is why the default
    is resolved here rather than in argparse, where ``0`` and "flag absent"
    would be indistinguishable.

    In ``spaced`` mode there is **no** default (``None`` stays ``None``), so an
    invocation that does not pass either flag is byte-identical to pre-#8915.
    An explicit value still applies, in both modes.

    Args:
        min_wire_length_mm: ``--min-wire-length``, or ``None`` if not given.
        min_run_length_mm: ``--min-run-length``, or ``None`` if not given.
        anchor_mode: ``"ends"`` or ``"spaced"``; only ``ends`` has a default.

    Returns:
        The minimum length in mm, or ``None`` when no minimum applies.
    """
    given = [v for v in (min_wire_length_mm, min_run_length_mm) if v is not None]
    if given:
        return max(given)
    if anchor_mode == ANCHOR_MODE_ENDS:
        return DEFAULT_MIN_WIRE_LENGTH_MM
    return None


def reinforce_net(
    pcb: PCB,
    net_name: str,
    *,
    wire_gauge_awg: int = DEFAULT_WIRE_GAUGE_AWG,
    spacing_mm: float = DEFAULT_ANCHOR_SPACING_MM,
    design_rules: DesignRules | None = None,
    layer: str | None = None,
    dry_run: bool = False,
    all_runs: bool = False,
    min_run_length_mm: float | None = None,
    min_wire_length_mm: float | None = None,
    current_paths: Sequence[CurrentPathSpec] | None = None,
    net_class_clearances: Mapping[str, float] | None = None,
    pairwise_clearance: PairwiseClearanceTable | None = None,
    board_rules: BoardClearanceRules | None = None,
    anchor_mode: str = DEFAULT_ANCHOR_MODE,
    end_offset_mm: float = DEFAULT_END_OFFSET_MM,
    max_end_shift_mm: float | None = None,
    mask_opening: bool | None = None,
    mask_width_mm: float | None = None,
    strategy: str = DEFAULT_STRATEGY,
    sidecar_wires: list[dict] | None = None,
) -> ReinforceResult:
    """Emit same-net PTH buttress-wire anchors along a routed net's trace.

    Post-route pass: chains the target net's routed segments into ordered
    polylines and places gauge-sized plated-PTH anchor vias. Each candidate
    anchor is clearance-checked against all other-net copper; a colliding
    candidate is nudged a bounded distance along the run and, only if no
    clear position is found, refused (reported, not placed).

    ``anchor_mode="ends"`` (default, Issue #8742) places exactly two anchors
    per single-layer leg, one near each end and none between, for a wire
    soldered along the trace and held only at its ends; see the module
    docstring and :class:`LegSummary`. With ``current_paths`` declared for the
    net, every leg of every resolved eligible path is anchored (on any copper
    layer, one leg per layer); without, one layer is processed per call and
    only the longest leg is anchored unless ``all_runs``.

    ``anchor_mode="spaced"`` is the historical behaviour: anchors at both
    endpoints and every ``spacing_mm`` along the selected run(s) of one layer.
    By default (``all_runs=False``) only the single longest run is anchored; a
    multi-branch net (T/Y fan-out at junctions) splits into several runs and
    ``all_runs=True`` anchors *every* run (#4319).

    Args:
        pcb: A loaded :class:`~kicad_tools.schema.pcb.PCB`.
        net_name: Target net name (must have routed copper).
        wire_gauge_awg: Buttress-wire gauge (default 16 AWG). Sizes the
            anchor drill/pad.
        spacing_mm: Arc-length spacing between mid-run anchors (mm);
            ``spaced`` mode only.
        design_rules: Manufacturer :class:`DesignRules` supplying
            ``min_annular_ring_mm`` (anchor pad sizing), ``min_clearance_mm``
            (other-net clearance), and ``min_hole_to_hole_mm``. Falls back to
            JLCPCB-tier-typical values when ``None``.
        layer: Restrict to this copper layer. When ``None``, the layer with
            the most cumulative segment length for the net is chosen.
        dry_run: When True, compute the placed/refused plan without
            mutating the board (no vias added).
        all_runs: When True, anchor every chained run on the target layer
            (subject to ``min_run_length_mm``) instead of only the longest.
        min_run_length_mm: When set, only runs at least this long (mm) are
            anchored; shorter runs are still reported (never silently
            dropped). Composes with ``all_runs`` -- filter first, then anchor
            all that survive. Same quantity as ``min_wire_length_mm``; when
            both are given the larger wins.
        min_wire_length_mm: Minimum buttress-wire length (mm), Issue #8915.
            In ``ends`` mode this defaults to
            :data:`DEFAULT_MIN_WIRE_LENGTH_MM` (15.0) when neither this nor
            ``min_run_length_mm`` is given -- a wire needs a through-hole at
            each end and they rarely fit on a shorter leg. Pass ``0.0`` for no
            minimum (the pre-#8915 behaviour). ``spaced`` mode has no default;
            an explicit value still applies there. See
            :func:`resolve_min_wire_length_mm`.
        current_paths: Optional declared branch-specific current-path
            intent (Issue #4980; see
            :mod:`kicad_tools.router.current_paths`). Specs not on
            ``net_name`` are ignored. When at least one spec targets
            ``net_name``, reinforcement gates to an **allow-list**: only
            runs fully covered by a resolved, reinforcement-eligible
            ``CurrentPathSpec`` may be anchored -- a declared sense/
            measurement/Kelvin branch (``reinforcement_eligible=False``),
            copper an ineligible-or-unresolved declaration cannot verify,
            and copper no declared path covers at all are all excluded and
            reported via :attr:`RunSummary.path_excluded_reason`, never
            silently anchored. ``None`` (the default) preserves the
            pre-#4980 ungated behavior.
        net_class_clearances: ``{net_name: mm}`` authored net-class clearance
            floors (the source ``kct check`` uses, #8085). The clearance to an
            other-net obstacle is at least the larger of the two nets' floors.
        board_rules: Optional :class:`BoardClearanceRules` (#8494): the board's
            ``.kicad_dru`` custom rules incl. condition-based ones, max-ed
            with the floors above; never lowers a requirement.
        pairwise_clearance: Optional ``--voltage-map`` pairwise table (#8085);
            when given, the requirement for (target, obstacle) is raised to
            the table's value. ``None`` keeps the scalar-floor behavior.
        anchor_mode: ``"ends"`` (default) or ``"spaced"`` (Issue #8742).
        end_offset_mm: ``ends`` mode: minimum straight-line clearance in mm
            from an end anchor's pad edge to the same-net copper the leg end
            lands on (default 1.0): a pad or via its track's end overlaps, or
            the whole trunk for a branch. The anchor starts that far along the
            leg past the copper edge and moves further inward while a bend
            brings it closer than that; a leg with no room for two such
            anchors, measured along the leg and straight-line, is
            ``too_short`` and gets none.
        mask_opening: Issue #8743. Open the solder mask along each fully
            anchored leg (a ``gr_line`` on the leg's own side's mask layer,
            anchor to anchor) and write the anchors with per-via tenting off.
            ``None`` (default) = on in ``ends`` mode, off in ``spaced``.
        mask_width_mm: Opening width override (default: the leg's trace width).
        strategy: Issue #8916. ``"wire"`` (default): the buttress wire above.
            ``"parallel"``: every selected leg instead gets a same-net,
            same-width copy of its polyline on the opposite outer layer, tied
            to it at both ends (the through-hole pad or through via the leg
            ends on, else a stitching via) and by stitching vias every
            :data:`STITCH_PITCH_MM` (floored by the ``kct stitch`` same-net
            spacing) -- no anchors, no mask opening. The minimum wire length
            does not filter ``parallel`` legs: it exists because two
            through-holes rarely fit on a short leg, and a copy needs none.
            ``"auto"``: ``parallel`` for a leg shorter than the minimum wire
            length (:func:`resolve_min_wire_length_mm`, so 15 mm unless
            ``min_wire_length_mm`` / ``min_run_length_mm`` say otherwise;
            ``0`` makes every leg a wire), ``wire`` for the rest. Every copy
            track and via is checked against the same clearance rules as an
            anchor; a copy that cannot be placed whole is refused and
            reported (:attr:`ReinforceResult.refused_copies`), never written
            in part. ``parallel`` and ``auto`` need ``anchor_mode="ends"``:
            ``spaced`` mode has no legs, and has no default minimum for
            ``auto`` to compare against.
        sidecar_wires: Issue #8916. The board's wire-list records (the
            #8744 sidecar) from earlier passes. They are the only evidence
            that a same-net via or copy-layer track is this tool's own
            parallel copper: a re-run recognises (and reuses) exactly the
            copper a record names. Every other via or track -- even one of
            the stitching-via size -- predates the tool: a leg end on such a
            via is already tied, and it is never written into a new record,
            so ``--clear`` can never remove it. Without the records a
            re-run treats the earlier copy as pre-existing copper.

    Returns:
        A :class:`ReinforceResult` summarising placed/refused anchors, the
        primary anchored run, a per-run :class:`RunSummary` list, and (in
        ``ends`` mode) a per-leg :class:`LegSummary` list.

    Raises:
        ReinforceError: If the net does not exist, has no routed copper,
            ``spacing_mm`` is non-positive, ``end_offset_mm`` is negative or
            ``anchor_mode`` is unknown.
    """
    if spacing_mm <= 0:
        raise ReinforceError(f"spacing_mm must be positive, got {spacing_mm}")
    if anchor_mode not in ANCHOR_MODES:
        raise ReinforceError(
            f"anchor_mode must be one of {', '.join(ANCHOR_MODES)}, got {anchor_mode!r}"
        )
    if end_offset_mm < 0 or not math.isfinite(end_offset_mm):
        raise ReinforceError(f"end_offset_mm must be >= 0, got {end_offset_mm}")
    if max_end_shift_mm is not None and (
        max_end_shift_mm < 0 or not math.isfinite(max_end_shift_mm)
    ):
        raise ReinforceError(f"max_end_shift_mm must be >= 0, got {max_end_shift_mm}")
    if mask_width_mm is not None and (mask_width_mm <= 0 or not math.isfinite(mask_width_mm)):
        raise ReinforceError(f"mask_width_mm must be positive, got {mask_width_mm}")
    if strategy not in STRATEGIES:
        raise ReinforceError(f"strategy must be one of {', '.join(STRATEGIES)}, got {strategy!r}")
    if strategy != STRATEGY_WIRE and anchor_mode != ANCHOR_MODE_ENDS:
        # Issue #8916: a copy is planned per leg, and spaced mode has none.
        # For ``auto`` it also has no threshold: resolve_min_wire_length_mm()
        # gives spaced mode no default minimum. Refuse rather than guess.
        raise ReinforceError(
            f"strategy {strategy!r} needs anchor_mode 'ends': spaced mode has no "
            "per-leg layout"
            + (
                " and no default minimum wire length for auto to compare against"
                if strategy == STRATEGY_AUTO
                else ""
            )
        )
    if mask_opening is None:
        mask_opening = anchor_mode == ANCHOR_MODE_ENDS

    rules = design_rules or _FALLBACK_DESIGN_RULES

    # Resolve the net. Accept a net that exists in the net table OR that has
    # segments carrying its name (some boards carry name-only segments).
    net_obj = pcb.get_net_by_name(net_name)
    net_segments = [
        s
        for s in pcb.segments
        if s.net_name == net_name or (net_obj is not None and s.net_number == net_obj.number)
    ]
    if net_obj is None and not net_segments:
        raise ReinforceError(f"net {net_name!r} not found on board")

    target_net_number = (
        net_obj.number
        if net_obj is not None
        else (net_segments[0].net_number if net_segments else 0)
    )

    anchor_drill = anchor_drill_for_awg(wire_gauge_awg)
    anchor_pad = anchor_pad_for_drill(anchor_drill, rules.min_annular_ring_mm)

    result = ReinforceResult(
        net_name=net_name,
        wire_gauge_awg=wire_gauge_awg,
        anchor_drill_mm=anchor_drill,
        anchor_pad_mm=anchor_pad,
        spacing_mm=spacing_mm,
        anchor_mode=anchor_mode,
        end_offset_mm=end_offset_mm,
        max_end_shift_mm=max_end_shift_mm,
        mask_opening=bool(mask_opening) and anchor_mode == ANCHOR_MODE_ENDS,
        mask_width_mm=mask_width_mm,
        strategy=strategy,
    )

    if not net_segments:
        raise ReinforceError(f"net {net_name!r} has no routed copper to reinforce")

    # Declarations on this net that cannot be used are reported in both
    # modes, never silently skipped (Issue #8742).
    net_path_specs = [spec for spec in (current_paths or ()) if spec.net_name == net_name]
    partition = reinforcement_path_resolutions(pcb, net_path_specs) if net_path_specs else None
    if partition is not None:
        result.unresolved_paths = [
            UnresolvedPath(path=res.spec.name, status=res.status, reason=res.reason)
            for res in partition.unresolved
        ]

    if anchor_mode == ANCHOR_MODE_ENDS:
        if layer is not None and layer not in {s.layer for s in net_segments}:
            raise ReinforceError(f"net {net_name!r} has no routed copper on layer {layer!r}")
        return _reinforce_ends(
            pcb,
            result,
            net_segments=net_segments,
            target_net_number=target_net_number,
            eligible_resolutions=(partition.eligible if partition is not None else None),
            layer=layer,
            all_runs=all_runs,
            min_wire_length_mm=resolve_min_wire_length_mm(
                min_wire_length_mm, min_run_length_mm, ANCHOR_MODE_ENDS
            ),
            rules=rules,
            policy=_ClearancePolicy(
                fab_floor=rules.min_clearance_mm,
                target_net=net_name,
                class_floors=net_class_clearances or {},
                pairwise=pairwise_clearance,
                board_rules=board_rules,
            ),
            dry_run=dry_run,
            sidecar_wires=sidecar_wires,
        )

    # Group by layer; pick the layer with the most cumulative length (MVP).
    by_layer: dict[str, list[Segment]] = {}
    for s in net_segments:
        by_layer.setdefault(s.layer, []).append(s)

    if layer is not None:
        if layer not in by_layer:
            raise ReinforceError(f"net {net_name!r} has no routed copper on layer {layer!r}")
        target_layer = layer
    else:
        target_layer = max(by_layer, key=lambda lay: sum(_seg_length(s) for s in by_layer[lay]))
    result.layer = target_layer

    # Chaining can reverse a segment by copying it. Carry its original PCB
    # identity through the walk so path eligibility remains physical.
    original_ids: dict[int, int] = {}
    runs = _chain_polylines(by_layer[target_layer], original_ids=original_ids)
    classified_runs: list[tuple[list[Segment], str | None]] = []
    if net_path_specs:
        allowed_ids = reinforcement_eligible_segment_ids(pcb, net_path_specs)
        excluded_reason = (
            "excluded by declared current-path intent (Issue #4980): not fully "
            "covered by a resolved, reinforcement-eligible CurrentPathSpec for "
            f"net {net_name!r}"
        )
        for run in runs:
            parts: list[tuple[list[Segment], str | None]] = []
            for seg in run:
                reason = None if original_ids[id(seg)] in allowed_ids else excluded_reason
                # A degree-two force pad can continue into a sense stub.
                # Split that eligibility boundary while retaining all of the
                # chainer's existing physical-junction boundaries.
                if not parts or parts[-1][1] != reason:
                    parts.append(([], reason))
                parts[-1][0].append(seg)
            classified_runs.extend(parts)
    else:
        classified_runs = [(run, None) for run in runs]
    if not classified_runs:
        raise ReinforceError(f"net {net_name!r} produced no walkable polyline")

    classified_runs.sort(key=lambda item: _run_length(item[0]), reverse=True)
    runs = [run for run, _ in classified_runs]
    geoms = [_run_geometry(run) for run in runs]
    path_excluded_reasons = {
        index: reason for index, (_, reason) in enumerate(classified_runs) if reason is not None
    }

    # Selection: filter by min length (report -- do not drop -- shorter runs),
    # then by the current-path gate (report -- do not drop -- excluded runs),
    # then anchor either all survivors (all_runs) or just the longest.
    # ``spaced`` takes NO default minimum (#8915): without either flag this is
    # ``None`` and the output is byte-identical to pre-#8915.
    spaced_min_mm = resolve_min_wire_length_mm(
        min_wire_length_mm, min_run_length_mm, ANCHOR_MODE_SPACED
    )
    if spaced_min_mm is not None:
        eligible = [i for i, g in enumerate(geoms) if g.length_mm >= spaced_min_mm]
    else:
        eligible = list(range(len(geoms)))
    eligible = [i for i in eligible if i not in path_excluded_reasons]
    anchored_idx: set[int] = set(eligible if all_runs else eligible[:1])

    # Backward-compat whole-net fields describe the primary (longest anchored)
    # run; zero when everything was filtered out.
    if anchored_idx:
        primary = geoms[min(anchored_idx)]
        result.run_segment_count = primary.segment_count
        result.run_length_mm = primary.length_mm
    result.unhandled_runs = len(geoms) - len(anchored_idx)

    other = _collect_other_net_copper(pcb, target_net_number, anchor_drill, anchor_pad)
    policy = _ClearancePolicy(
        fab_floor=rules.min_clearance_mm,
        target_net=net_name,
        class_floors=net_class_clearances or {},
        pairwise=pairwise_clearance,
        board_rules=board_rules,
    )

    # Cross-run dedup floor: two placed anchors must be at least a pad diameter
    # apart center-to-center (no overlapping copper), which also satisfies the
    # hole-to-hole floor. Shared junction vertices between fanned-out runs land
    # at the same coordinate and are anchored once.
    dedup_floor = max(anchor_pad, anchor_drill + rules.min_hole_to_hole_mm)
    placed_positions: list[tuple[float, float]] = []

    for i, g in enumerate(geoms):
        cum = _cumulative_arc(g.points)
        targets = _anchor_arc_targets(g.length_mm, spacing_mm)
        run_placed = 0
        run_refused = 0

        if i in anchored_idx:
            for t in targets:
                base = _point_at_arc(g.points, cum, t)
                # Already covered by a coincident anchor (e.g. a shared
                # junction vertex from another run) -- count as reinforced,
                # not a new placement and not a refusal.
                if _too_close(base, placed_positions, dedup_floor):
                    run_placed += 1
                    continue

                chosen: tuple[float, float] | None = None
                first_violation: _Violation | None = None
                for cand_t in _nudge_arc_candidates(t, g.length_mm, spacing_mm):
                    cx, cy = _point_at_arc(g.points, cum, cand_t)
                    if _too_close((cx, cy), placed_positions, dedup_floor):
                        continue
                    violation = _position_violation(
                        cx, cy, anchor_pad, anchor_drill, rules, other, policy
                    )
                    if violation is None:
                        chosen = (cx, cy)
                        break
                    if first_violation is None:
                        first_violation = violation

                if chosen is None:
                    bx, by = base
                    v = first_violation
                    result.refused.append(
                        RefusedAnchor(
                            x=bx,
                            y=by,
                            reason="other-net clearance/hole-to-hole violation",
                            rule=v.rule if v else "",
                            required_mm=v.required_mm if v else 0.0,
                            actual_mm=v.actual_mm if v else 0.0,
                            obstacle_net=v.obstacle_net if v else "",
                        )
                    )
                    run_refused += 1
                    continue

                cx, cy = chosen
                if not dry_run:
                    pcb.add_via(
                        cx,
                        cy,
                        size=anchor_pad,
                        drill=anchor_drill,
                        layers=("F.Cu", "B.Cu"),
                        net=net_name,
                        dedupe=True,
                    )
                result.placed.append(PlacedAnchor(x=cx, y=cy))
                placed_positions.append((cx, cy))
                run_placed += 1

        result.runs.append(
            RunSummary(
                run_index=i,
                length_mm=g.length_mm,
                segment_count=g.segment_count,
                anchors_needed=len(targets),
                anchors_placed=run_placed,
                anchors_refused=run_refused,
                path_excluded_reason=path_excluded_reasons.get(i),
            )
        )

    return result


# --------------------------------------------------------------------------
# ``ends`` mode (Issue #8742): two anchors per single-layer leg
# --------------------------------------------------------------------------


@dataclass
class _LegCandidate:
    """A leg before placement: oriented segments plus topology verdict."""

    layer: str
    kind: str
    run: list[Segment]
    #: ``LEG_UNHANDLED`` when topology (or the layer selection) rules the leg
    #: out before placement; ``None`` when it may be anchored.
    status: str | None = None
    reason: str = ""
    #: Unhandled only because this call processes another copper layer.
    off_layer: bool = False


def _run_points(run: list[Segment]) -> list[tuple[float, float]]:
    return [run[0].start] + [s.end for s in run]


def _reverse_run(run: list[Segment], original_ids: dict[int, int]) -> list[Segment]:
    """Walk ``run`` the other way, keeping each copy's original PCB identity."""
    out: list[Segment] = []
    for s in reversed(run):
        flipped = Segment(
            start=s.end,
            end=s.start,
            width=s.width,
            layer=s.layer,
            net_number=s.net_number,
            net_name=s.net_name,
            uuid=s.uuid,
        )
        original_ids[id(flipped)] = original_ids.get(id(s), id(s))
        out.append(flipped)
    return out


def _vertex(p: tuple[float, float]) -> tuple[float, float]:
    return (round(p[0], 4), round(p[1], 4))


def _derive_legs(
    segments: list[Segment], layer: str, original_ids: dict[int, int]
) -> list[_LegCandidate]:
    """Split one layer's copper into wire legs (Issue #8742).

    The junction-aware chainer already cuts the copper at every degree>=3
    vertex, so each chain is a junction-free run. Chains are then grouped
    into connected components by their end vertices:

    * a component that is a single chain is one ``run`` leg;
    * a single Y (exactly one degree-3 junction and three chains) becomes a
      ``trunk`` -- the two longest arms joined through the junction, i.e. the
      longest simple through-path -- plus one ``branch`` oriented from the
      junction to its free end;
    * a loop (a closed chain, or a component with as many chains as end
      vertices), a degree>=4 junction, or more than one junction is reported
      ``unhandled`` chain by chain: no single wire layout is implied, so none
      is guessed.
    """
    runs = _chain_polylines(segments, original_ids=original_ids)
    if not runs:
        return []

    ends = [(_vertex(r[0].start), _vertex(r[-1].end)) for r in runs]
    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def find(v: tuple[float, float]) -> tuple[float, float]:
        parent.setdefault(v, v)
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    for a, b in ends:
        parent[find(a)] = find(b)

    components: dict[tuple[float, float], list[int]] = {}
    for i, (a, _) in enumerate(ends):
        components.setdefault(find(a), []).append(i)

    legs: list[_LegCandidate] = []
    for idxs in components.values():
        degree: dict[tuple[float, float], int] = {}
        for i in idxs:
            for v in ends[i]:
                degree[v] = degree.get(v, 0) + 1
        closed = any(ends[i][0] == ends[i][1] for i in idxs)
        junctions = [v for v, d in degree.items() if d >= 3]
        reason = ""
        if closed or len(idxs) >= len(degree):
            reason = "copper loop on this layer: a closed ring has no two wire ends"
        elif max(degree.values()) >= 4:
            reason = (
                f"junction of degree {max(degree.values())} on this layer: only a "
                "single Y (one trunk plus one branch) is handled"
            )
        elif len(junctions) > 1:
            reason = (
                f"{len(junctions)} junctions on one connected copper tree: only a "
                "single Y (one trunk plus one branch) is handled"
            )
        elif len(junctions) == 1 and len(idxs) != 3:
            reason = "unrecognised junction topology"
        if reason:
            legs.extend(
                _LegCandidate(layer, LEG_KIND_RUN, runs[i], status=LEG_UNHANDLED, reason=reason)
                for i in idxs
            )
            continue
        if not junctions:
            legs.extend(_LegCandidate(layer, LEG_KIND_RUN, runs[i]) for i in idxs)
            continue

        junction = junctions[0]
        # Every arm walked AWAY from the junction; the two longest form the
        # trunk (ties keep chainer order, so the choice is deterministic).
        arms = [
            runs[i]
            if _vertex(runs[i][0].start) == junction
            else _reverse_run(runs[i], original_ids)
            for i in idxs
        ]
        arms.sort(key=_run_length, reverse=True)
        trunk = _reverse_run(arms[0], original_ids) + arms[1]
        legs.append(_LegCandidate(layer, LEG_KIND_TRUNK, trunk))
        # The branch starts on the trunk: its end copper there is the WHOLE
        # trunk, found by ``_track_end_features`` (it touches the junction).
        legs.append(_LegCandidate(layer, LEG_KIND_BRANCH, arms[2]))
    return legs


def _pad_copper_distance(
    pad: Pad, center: tuple[float, float], point: tuple[float, float]
) -> float | None:
    """Signed straight-line distance (mm) from ``point`` to ``pad``'s copper.

    Positive outside the copper, ``<= 0`` on or inside it (board frame).
    ``None`` for a pad with no usable size.

    ``pad.rotation`` is absolute. Board offsets come from pad-local ones by
    :func:`~kicad_tools.core.geometry.rotate_pad_offset` (KiCad's negated-angle
    convention), so the inverse -- board to pad-local -- is the same function
    at the negated angle. Circle / oval (capsule) / rect / roundrect are exact;
    trapezoid and chamfered rect use their bounding rectangle, and custom pads
    the disc circumscribing the anchor rectangle -- both over-estimate the
    copper, which only moves the anchor further inward.
    """
    try:
        half_w = float(pad.size[0]) / 2.0
        half_h = float(pad.size[1]) / 2.0
    except (TypeError, ValueError, IndexError):
        return None
    if half_w <= 0.0 or half_h <= 0.0:
        return None
    lx, ly = rotate_pad_offset(
        point[0] - center[0], point[1] - center[1], -float(pad.rotation or 0.0)
    )
    shape = pad.shape
    if shape == "circle":
        return math.hypot(lx, ly) - max(half_w, half_h)
    radius = 0.0
    if shape == "oval":
        radius = min(half_w, half_h)
    elif shape == "roundrect":
        ratio = min(max(float(pad.roundrect_rratio), 0.0), 0.5)
        radius = 2.0 * min(half_w, half_h) * ratio
    elif shape not in ("rect", "trapezoid", "chamfered_rect"):
        return math.hypot(lx, ly) - math.hypot(half_w, half_h)
    # Rounded-box signed distance (radius 0 is the plain rectangle).
    qx = abs(lx) - (half_w - radius)
    qy = abs(ly) - (half_h - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def _pad_copper_contains(pad: Pad, center: tuple[float, float], point: tuple[float, float]) -> bool:
    """True when ``point`` lies on ``pad``'s copper (board frame)."""
    d = _pad_copper_distance(pad, center, point)
    return d is not None and d <= 1e-6


def _segment_distance(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Distance from ``p`` to the segment ``a``-``b``."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    seg2 = dx * dx + dy * dy
    if seg2 <= 0.0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    u = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / seg2))
    return math.hypot(p[0] - (a[0] + u * dx), p[1] - (a[1] + u * dy))


@dataclass
class _EndFeature:
    """Same-net copper a leg end can land on.

    A pad, a non-anchor via, or (for a branch's junction end) one trunk
    segment, modelled as a capsule from ``(x, y)`` to ``seg_end`` of
    half-width ``via_radius``.
    """

    x: float
    y: float
    #: Bounding radius, for a cheap reject before the exact test.
    bound: float
    layers: tuple[str, ...]
    pad: Pad | None = None
    via_radius: float = 0.0
    seg_end: tuple[float, float] | None = None

    def on_layer(self, layer: str) -> bool:
        return not self.layers or layer in self.layers or "*.Cu" in self.layers

    def distance(self, p: tuple[float, float]) -> float:
        """Straight-line distance (mm) from ``p`` to this copper; <= 0 inside."""
        if self.seg_end is not None:
            return _segment_distance(p, (self.x, self.y), self.seg_end) - self.via_radius
        if self.pad is not None:
            d = _pad_copper_distance(self.pad, (self.x, self.y), p)
            return math.inf if d is None else d
        return math.hypot(p[0] - self.x, p[1] - self.y) - self.via_radius

    def contains(self, p: tuple[float, float]) -> bool:
        if self.seg_end is None and math.hypot(p[0] - self.x, p[1] - self.y) > self.bound + 1e-6:
            return False
        return self.distance(p) <= 1e-6


def _same_net_end_features(
    pcb: PCB, target_net: int, anchor_drill: float, anchor_pad: float
) -> list[_EndFeature]:
    """Same-net pads and vias (previous anchors excluded) for end offsets."""
    features: list[_EndFeature] = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number != target_net or pad.type == "np_thru_hole":
                continue
            off = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            try:
                bound = math.hypot(float(pad.size[0]), float(pad.size[1])) / 2.0
            except (TypeError, ValueError, IndexError):
                continue
            # A plated through-hole pad has copper on every layer.
            layers = () if pad.type == "thru_hole" else tuple(pad.layers)
            features.append(
                _EndFeature(fp.position[0] + off[0], fp.position[1] + off[1], bound, layers, pad)
            )
    for via in pcb.vias:
        if via.net_number != target_net:
            continue
        if abs(via.drill - anchor_drill) < 1e-6 and abs(via.size - anchor_pad) < 1e-6:
            continue  # a previous anchor, not the copper a leg ends on
        vx, vy = via.position
        features.append(_EndFeature(vx, vy, via.size / 2.0, (), via_radius=via.size / 2.0))
    return features


def _exit_arc(points: list[tuple[float, float]], contains) -> float | None:
    """Arc length at which the polyline from ``points[0]`` leaves ``contains``.

    ``None`` when the copper covers the whole polyline.
    """
    cum = _cumulative_arc(points)
    total = cum[-1]
    prev = 0.0
    t = 0.0
    while t < total - 1e-12:
        t = min(total, t + _EXIT_MARCH_STEP_MM)
        if not contains(_point_at_arc(points, cum, t)):
            lo, hi = prev, t
            for _ in range(40):
                mid = (lo + hi) / 2.0
                if contains(_point_at_arc(points, cum, mid)):
                    lo = mid
                else:
                    hi = mid
            return hi
        prev = t
    return None


def _track_components(segments: list[Segment]) -> list[list[_EndFeature]]:
    """Same-net track copper as capsules, grouped into connected components.

    Two segments are connected when they share an end vertex or when one's
    end lies on the other's copper (a T onto a segment's middle). A leg end
    that touches any segment of a component lands on that whole component,
    so a branch's junction anchor is kept clear of the entire trunk, not
    just the trunk pieces that meet the junction (PR #8751 review).
    """
    feats = [
        _EndFeature(
            s.start[0], s.start[1], 0.0, (), via_radius=s.width / 2.0, seg_end=(s.end[0], s.end[1])
        )
        for s in segments
    ]
    parent = list(range(len(feats)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_vertex: dict[tuple[float, float], int] = {}
    for i, s in enumerate(segments):
        for v in (_vertex(s.start), _vertex(s.end)):
            j = by_vertex.setdefault(v, i)
            parent[find(i)] = find(j)
    # T-joins: bucket each capsule's bounding box on a coarse grid so an end
    # point is only tested against the segments near it.
    cell = 5.0
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, s in enumerate(segments):
        r = s.width / 2.0
        x0, x1 = sorted((s.start[0], s.end[0]))
        y0, y1 = sorted((s.start[1], s.end[1]))
        for cx in range(math.floor((x0 - r) / cell), math.floor((x1 + r) / cell) + 1):
            for cy in range(math.floor((y0 - r) / cell), math.floor((y1 + r) / cell) + 1):
                buckets.setdefault((cx, cy), []).append(i)
    for i, s in enumerate(segments):
        for p in (s.start, s.end):
            for j in buckets.get((math.floor(p[0] / cell), math.floor(p[1] / cell)), ()):
                if find(i) != find(j) and feats[j].contains(p):
                    parent[find(i)] = find(j)
    groups: dict[int, list[_EndFeature]] = {}
    for i, f in enumerate(feats):
        groups.setdefault(find(i), []).append(f)
    return list(groups.values())


def _end_copper(
    points: list[tuple[float, float]],
    layer: str,
    features: list[_EndFeature],
    tracks: list[list[_EndFeature]],
    cap_radius: float,
) -> list[_EndFeature] | None:
    """The same-net copper on ``layer`` that the leg end ``points[0]`` lands on.

    "Lands on" is copper overlap, not centreline containment: the leg's
    track ends in a round cap of ``cap_radius`` (half its width) around
    ``points[0]``, so a pad or via whose copper comes within that radius is
    connected to it even when the centreline stops just outside the pad
    (PR #8751 review). Same-net track copper of another leg (``tracks``,
    one list per connected component) is taken WHOLE when any piece of it
    touches the cap: an anchor beside a trunk must clear every trunk
    segment, including fragments and bends away from the junction.

    ``None`` when a pad or via covers the whole leg.
    """
    start = points[0]
    found: list[_EndFeature] = []
    for feat in features:
        if not feat.on_layer(layer) or feat.distance(start) > cap_radius + 1e-6:
            continue
        if feat.contains(start) and _exit_arc(points, feat.contains) is None:
            return None
        found.append(feat)
    for component in tracks:
        if any(f.distance(start) <= cap_radius + 1e-6 for f in component):
            found.extend(component)
    return found


def _end_extent(points: list[tuple[float, float]], copper: list[_EndFeature]) -> float:
    """Arc length from ``points[0]`` to the edge of the end ``copper``.

    The largest exit, measured along the leg, from every feature in
    ``copper`` that contains the start point; ``0.0`` for a free track end
    or one that only overlaps the copper. This is only the starting guess:
    the straight-line clearance test then moves the anchor further inward.
    """
    extent = 0.0
    for feat in copper:
        if not feat.contains(points[0]):
            continue
        t = _exit_arc(points, feat.contains)
        if t is not None:
            extent = max(extent, t)
    return extent


def _clear_arc(
    points: list[tuple[float, float]],
    cum: list[float],
    t_from: float,
    t_limit: float,
    clear,
) -> float | None:
    """First arc from ``t_from`` towards ``t_limit`` where ``clear`` holds.

    Walks in ``_EXIT_MARCH_STEP_MM`` steps (either direction, so it serves
    both ends: ``t_limit < t_from`` walks backwards), then bisects onto the
    boundary. ``None`` when ``clear`` never holds in that range.
    """
    if clear(_point_at_arc(points, cum, t_from)):
        return t_from
    sign = 1.0 if t_limit >= t_from else -1.0
    span = abs(t_limit - t_from)
    prev = t_from
    walked = 0.0
    while walked < span - 1e-12:
        walked = min(span, walked + _EXIT_MARCH_STEP_MM)
        t = t_from + sign * walked
        if clear(_point_at_arc(points, cum, t)):
            bad, good = prev, t
            for _ in range(40):
                mid = (bad + good) / 2.0
                if clear(_point_at_arc(points, cum, mid)):
                    good = mid
                else:
                    bad = mid
            return good
        prev = t
    return None


def _arc_of_point(
    points: list[tuple[float, float]], cum: list[float], p: tuple[float, float]
) -> float:
    """Arc length of the polyline point nearest ``p``."""
    best_t, best_d = 0.0, float("inf")
    for i in range(len(points) - 1):
        (x0, y0), (x1, y1) = points[i], points[i + 1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        u = 0.0
        if seg2 > 1e-18:
            u = max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / seg2))
        d = math.hypot(p[0] - (x0 + u * dx), p[1] - (y0 + u * dy))
        if d < best_d:
            best_d, best_t = d, cum[i] + u * math.sqrt(seg2)
    return best_t


def _sub_polyline(
    points: list[tuple[float, float]], cum: list[float], t0: float, t1: float
) -> list[tuple[float, float]]:
    """The polyline between arc lengths ``t0 <= t1`` (interpolated ends)."""
    out = [_point_at_arc(points, cum, t0)]
    out.extend(p for p, c in zip(points, cum, strict=True) if t0 + 1e-9 < c < t1 - 1e-9)
    out.append(_point_at_arc(points, cum, t1))
    return out


def _opening_violation(
    run: list[tuple[float, float]],
    exposed_width: float,
    other: _OtherNetCopper,
    policy: _ClearancePolicy,
) -> _Violation | None:
    """Creepage test of an exposed run, modelled as a chain of discs.

    The exposed copper has half-width ``exposed_width / 2``; each obstacle
    net must keep ``policy.required(net)`` (``max(class floor, creepage @
    |dV|)`` with a voltage map) from it. Layer-agnostic like the anchor test:
    other-net copper on the far side is treated as an obstacle too
    (conservative).
    """
    step = max(0.05, min(_MASK_SAMPLE_STEP_MM, exposed_width / 2.0))
    samples: list[tuple[float, float]] = []
    for (x0, y0), (x1, y1) in zip(run, run[1:], strict=False):
        n = max(1, math.ceil(math.hypot(x1 - x0, y1 - y0) / step))
        samples.extend((x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n))
    samples.append(run[-1])
    for net_number, group in other.by_net.items():
        net = other.net_names.get(net_number, "")
        required, rule = policy.required(net)
        for x, y in samples:
            if not _group_clear(x, y, exposed_width, required, group):
                tag = (
                    RULE_MASK_OPENING_PAIRWISE
                    if rule == RULE_VOLTAGE_MAP_PAIRWISE
                    else RULE_MASK_OPENING_CLEARANCE
                )
                gap = _actual_gap(x, y, exposed_width, required, group)
                return _Violation(tag, required, gap, net)
    return None


def _plan_mask_opening(
    pcb: PCB,
    result: ReinforceResult,
    leg: LegSummary,
    points: list[tuple[float, float]],
    cum: list[float],
    other: _OtherNetCopper,
    policy: _ClearancePolicy,
    dry_run: bool,
) -> None:
    """Open the solder mask along a fully anchored leg, anchor to anchor (#8743)."""
    mask_layer = MASK_LAYER_FOR_COPPER.get(leg.layer)
    if mask_layer is None:
        leg.mask_status = MASK_SKIPPED
        leg.mask_reason = f"{leg.layer} is an inner layer: no solder mask on it"
        return
    ta, tb = sorted(_arc_of_point(points, cum, (a.x, a.y)) for a in leg.anchors)
    run = _sub_polyline(points, cum, ta, tb)
    width = result.mask_width_mm if result.mask_width_mm is not None else leg.width_mm
    # Voltage-map creepage test: the exposed run counts as copper of
    # half-width max(opening, wire diameter) / 2.
    if policy.pairwise is not None:
        exposed = max(width, bare_copper_diameter_mm(result.wire_gauge_awg))
        v = _opening_violation(run, exposed, other, policy)
        if v is not None:
            leg.mask_status = MASK_REFUSED
            leg.mask_reason = (
                f"exposed run would be {v.actual_mm:.3f} mm from net {v.obstacle_net!r} "
                f"(need {v.required_mm:.3f} mm, {v.rule}); no opening written"
            )
            result.refused_openings.append(
                RefusedOpening(
                    copper_layer=leg.layer,
                    start=run[0],
                    end=run[-1],
                    rule=v.rule,
                    required_mm=v.required_mm,
                    actual_mm=v.actual_mm,
                    obstacle_net=v.obstacle_net,
                )
            )
            return
    for a, b in zip(run, run[1:], strict=False):
        if math.dist(a, b) <= 1e-9:
            continue
        if dry_run:
            existing = pcb.has_graphic_line(a, b, mask_layer)
        else:
            existing = pcb.add_graphic_line(a, b, mask_layer, width=width) is None
        result.openings.append(MaskOpening(mask_layer, a, b, width, existing))
    leg.mask_status = MASK_OPENED


def _courtyard_overlaps(
    pcb: PCB, layer: str, points: list[tuple[float, float]], gauge_awg: int
) -> list[tuple[str, float]]:
    """Same-side bodies a wire along ``points`` would cross: ``[(ref, mm), ...]``.

    Reuses the geometry of the ``wire_run_courtyard_overlap`` check (Issue
    #8821) -- no second implementation -- so reinforce never plans a run the
    check then rejects. Empty for an inner layer or a degenerate polyline.
    """
    side = "F" if layer == "F.Cu" else "B" if layer == "B.Cu" else None
    if side is None or len(points) < 2:
        return []
    from kicad_tools.physics.wire_gauge import bare_copper_diameter_mm
    from kicad_tools.validate.rules.reinforcement_courtyard import (
        RUN_CLEARANCE_MM,
        run_courtyard_overlaps,
    )

    inflate = bare_copper_diameter_mm(gauge_awg) / 2.0 + RUN_CLEARANCE_MM
    return [
        (fp.reference, length)
        for fp, length in run_courtyard_overlaps(points, side, pcb.footprints, inflate)
    ]


# --------------------------------------------------------------------------
# ``--strategy parallel`` (Issue #8916): copy a leg onto the opposite outer layer
# --------------------------------------------------------------------------

#: Margin (mm) around a copy's bounding box inside which other-net copper is
#: gathered for its clearance test; generous enough for any creepage figure.
_COPY_OBSTACLE_MARGIN_MM = 25.0


def stitch_via_geometry(rules: DesignRules) -> tuple[float, float]:
    """``(pad, drill)`` of the stitching vias on a parallel copy (Issue #8916).

    The board's default via as ``kct stitch`` derives it from the same design
    rules: the minimum drill, and the larger of the minimum via diameter and
    that drill plus two minimum annular rings.
    """
    drill = rules.min_via_drill_mm
    return max(rules.min_via_diameter_mm, drill + 2.0 * rules.min_annular_ring_mm), drill


def _same_net_stitch_spacing(via_size: float, via_drill: float, rules: DesignRules) -> float:
    """Minimum centre distance of two stitching vias: ``kct stitch``'s own rule."""
    from kicad_tools.cli.stitch_cmd import _same_net_via_spacing

    return _same_net_via_spacing(
        via_size, rules.min_clearance_mm, via_drill, rules.min_hole_to_hole_mm
    )


def _clip_run(
    run: list[Segment], t0: float, t1: float
) -> list[tuple[tuple[float, float], tuple[float, float], float]]:
    """The oriented ``run`` between arc lengths ``t0 <= t1``, vertex for vertex.

    Each piece keeps its own segment's width; only the two ends are cut.
    """
    out: list[tuple[tuple[float, float], tuple[float, float], float]] = []
    arc = 0.0
    for seg in run:
        length = _seg_length(seg)
        s0, s1 = arc, arc + length
        arc = s1
        lo, hi = max(s0, t0), min(s1, t1)
        if hi - lo <= 1e-6 or length <= 1e-12:
            continue
        (x0, y0), (x1, y1) = seg.start, seg.end

        def at(t: float, x0=x0, y0=y0, x1=x1, y1=y1, s0=s0, length=length):
            f = (t - s0) / length
            return (x0 + f * (x1 - x0), y0 + f * (y1 - y0))

        out.append(
            (
                seg.start if lo <= s0 + 1e-9 else at(lo),
                seg.end if hi >= s1 - 1e-9 else at(hi),
                seg.width,
            )
        )
    return out


def _close(p: tuple[float, float], q: tuple[float, float]) -> bool:
    """Same point within the sidecar's matching tolerance (what ``--clear`` uses)."""
    return abs(p[0] - q[0]) <= _SIDECAR_ANCHOR_TOL_MM and abs(p[1] - q[1]) <= _SIDECAR_ANCHOR_TOL_MM


def _coincident(
    points: list[tuple[float, float]], other: list[tuple[float, float]], tol: float
) -> bool:
    """True when polyline ``points`` lies within ``tol`` of polyline ``other`` along its length.

    Not just its vertices: each segment of ``points`` is sampled at most
    ``tol`` apart, so a straight chord whose two ends sit on a bent ``other``
    (its body crossing open board) is not coincident (PR #8945 review).
    Sampling at ``tol`` bounds the error between samples by ``tol / 2``.
    """
    if len(other) < 2 or not points:
        return False
    pairs = list(zip(other, other[1:], strict=False))

    def on_other(p: tuple[float, float]) -> bool:
        return min(_segment_distance(p, a, b) for a, b in pairs) <= tol

    if len(points) == 1:
        return on_other(points[0])
    step = max(tol, 1e-3)
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        n = max(1, math.ceil(math.hypot(x1 - x0, y1 - y0) / step))
        for k in range(n + 1):
            if not on_other((x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n)):
                return False
    return True


def _layer_obstacles(
    pcb: PCB,
    target_net: int,
    layer: str,
    bbox: tuple[float, float, float, float],
) -> tuple[_OtherNetCopper, list[tuple[float, float, float, int]]]:
    """Other-net copper ON ``layer`` near ``bbox``, plus other nets' drilled holes.

    The copy's own obstacle set (Issue #8916). Unlike the layer-agnostic
    anchor set (:func:`_collect_other_net_copper`), a track on one layer only
    meets copper on that layer -- tracks and zone fills of ``layer``, pads with
    copper there (every plated through-hole pad), and the vias that span it --
    and the other nets' holes (plus unplated holes), for KiCad's
    hole-to-copper clearance. Pads are discs of the circumscribed radius (an
    over-estimate for a rectangle, so never a short).
    """
    from kicad_tools.core.layers import via_spans_layer

    x0, y0, x1, y1 = bbox

    def near(xa: float, ya: float, xb: float, yb: float) -> bool:
        return xb >= x0 and xa <= x1 and yb >= y0 and ya <= y1

    other = _OtherNetCopper()
    other.net_names = {n.number: n.name for n in pcb.nets.values()}
    drills: list[tuple[float, float, float, int]] = []

    def group(net_number: int) -> _NetCopper:
        return other.by_net.setdefault(net_number, _NetCopper())

    for seg in pcb.segments:
        if seg.net_number == target_net or seg.layer != layer:
            continue
        (sx, sy), (ex, ey) = seg.start, seg.end
        if near(min(sx, ex), min(sy, ey), max(sx, ex), max(sy, ey)):
            group(seg.net_number).tracks.append(_TrackObstacle(sx, sy, ex, ey, seg.width))
    for via in pcb.vias:
        if via.net_number == target_net:
            continue
        vx, vy = via.position
        if not near(vx, vy, vx, vy):
            continue
        if via.drill > 0:
            drills.append((vx, vy, via.drill, via.net_number))
        if not via.layers or via_spans_layer(via.layers, layer):
            group(via.net_number).vias.append((vx, vy, via.size, via.net_number))
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number == target_net and pad.type != "np_thru_hole":
                continue
            pos = pcb.get_pad_position(fp.reference, pad.number)
            if pos is None or not near(pos[0], pos[1], pos[0], pos[1]):
                continue
            px, py = pos
            if pad.drill > 0:
                drills.append((px, py, pad.drill, pad.net_number))
            if pad.type == "np_thru_hole":
                continue
            if pad.type != "thru_hole" and layer not in pad.layers and "*.Cu" not in pad.layers:
                continue
            try:
                w, h = float(pad.size[0]), float(pad.size[1])
            except (TypeError, ValueError, IndexError):
                continue
            radius = max(w, h) / 2.0 if pad.shape == "circle" else math.hypot(w, h) / 2.0
            group(pad.net_number).pads.append((px, py, radius, pad.net_number))
    for zone in pcb.zones:
        if zone.net_number == target_net:
            continue
        for i, poly in enumerate(zone.filled_polygons):
            if len(poly) < 3 or zone.filled_polygon_layer(i) != layer:
                continue
            obstacle = _FilledPolyObstacle(list(poly))
            if near(obstacle.min_x, obstacle.min_y, obstacle.max_x, obstacle.max_y):
                group(zone.net_number).filled_polygons.append(obstacle)
    return other, drills


def _copy_track_violation(
    segments: list[tuple[tuple[float, float], tuple[float, float], float]],
    other: _OtherNetCopper,
    drills: list[tuple[float, float, float, int]],
    policy: _ClearancePolicy,
) -> tuple[_Violation, tuple[float, float]] | None:
    """Clearance test of a parallel copy's tracks; the first breach and where.

    Copper: each track is a chain of discs of its width (the anchor/opening
    predicate), and the chain's shortfall against the true capsule is added
    to every requirement, so a breach is never missed between two discs.
    Holes: the exact segment distance against KiCad's hole-to-copper
    clearance.
    """
    from kicad_tools.cli.stitch_cmd import KICAD_HOLE_TO_COPPER_CLEARANCE

    for a, b, width in segments:
        r = width / 2.0
        step = max(0.02, min(_MASK_SAMPLE_STEP_MM, r))
        deficit = r - math.sqrt(max(0.0, r * r - (step / 2.0) ** 2))
        n = max(1, math.ceil(math.dist(a, b) / step))
        samples = [
            (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)
        ]
        for net_number, grp in other.by_net.items():
            net = other.net_names.get(net_number, "")
            required, rule = policy.required(net)
            for x, y in samples:
                if not _group_clear(x, y, width, required + deficit, grp):
                    gap = max(0.0, _actual_gap(x, y, width, required + deficit, grp) - deficit)
                    return _Violation(rule, required, gap, net), (x, y)
        for hx, hy, drill, net_number in drills:
            gap = _segment_distance((hx, hy), a, b) - r - drill / 2.0
            if gap < KICAD_HOLE_TO_COPPER_CLEARANCE - _RULE_EPS:
                return (
                    _Violation(
                        RULE_HOLE_CLEARANCE,
                        KICAD_HOLE_TO_COPPER_CLEARANCE,
                        max(0.0, gap),
                        other.net_names.get(net_number, ""),
                    ),
                    (hx, hy),
                )
    return None


def _reinforce_ends(
    pcb: PCB,
    result: ReinforceResult,
    *,
    net_segments: list[Segment],
    target_net_number: int,
    eligible_resolutions: Sequence | None,
    layer: str | None,
    all_runs: bool,
    min_wire_length_mm: float | None,
    rules: DesignRules,
    policy: _ClearancePolicy,
    dry_run: bool,
    sidecar_wires: list[dict] | None = None,
) -> ReinforceResult:
    """``anchor_mode="ends"``: two anchors per leg (Issue #8742)."""
    net_name = result.net_name
    anchor_pad = result.anchor_pad_mm
    anchor_drill = result.anchor_drill_mm
    end_offset = result.end_offset_mm
    gated = eligible_resolutions is not None

    by_layer: dict[str, list[Segment]] = {}
    for s in net_segments:
        # A zero-length segment carries no copper of its own (its round cap
        # sits inside its neighbours'), but as a self-loop vertex it made the
        # chainer's component look closed and the whole leg ``unhandled``.
        if _seg_length(s) <= 1e-9:
            continue
        by_layer.setdefault(s.layer, []).append(s)

    if not by_layer:
        # Only zero-length (or no) segments: nothing to walk. Fail like the
        # later guard instead of crashing in ``max()`` below (Issue #8780).
        raise ReinforceError(f"net {net_name!r} produced no walkable polyline")

    original_ids: dict[int, int] = {}
    candidates: list[_LegCandidate] = []
    excluded_runs: list[list[Segment]] = []
    path_names: dict[int, list[str]] = {}
    target_layer: str | None = layer

    if gated:
        for res in eligible_resolutions or ():
            for seg in res.segments:
                covering = path_names.setdefault(id(seg), [])
                if res.spec.name not in covering:
                    covering.append(res.spec.name)
        for lay in sorted(by_layer):
            segs = by_layer[lay]
            eligible = [s for s in segs if id(s) in path_names]
            excluded_runs.extend(_chain_polylines([s for s in segs if id(s) not in path_names]))
            legs = _derive_legs(eligible, lay, original_ids)
            if layer is not None and lay != layer:
                for c in legs:
                    if c.status is None:
                        c.status = LEG_UNHANDLED
                        c.reason = f"on {lay}: --layer restricts this call to {layer}"
                        c.off_layer = True
            candidates.extend(legs)
    else:
        if target_layer is None:
            # Prefer a side whose runs cross no same-side courtyard (#8821);
            # among equals, the layer with the most copper, as before.
            def _layer_key(lay: str) -> tuple[bool, float]:
                blocked = any(
                    _courtyard_overlaps(
                        pcb,
                        lay,
                        _run_points(c.run),
                        result.wire_gauge_awg,
                    )
                    for c in _derive_legs(by_layer[lay], lay, {})
                    if c.status is None
                )
                return (blocked, -sum(_seg_length(s) for s in by_layer[lay]))

            target_layer = min(by_layer, key=_layer_key)
        for lay in sorted(by_layer):
            legs = _derive_legs(by_layer[lay], lay, original_ids)
            if lay != target_layer:
                for c in legs:
                    if c.status is None:
                        c.status = LEG_UNHANDLED
                        c.off_layer = True
                        c.reason = (
                            f"on {lay}: without --current-paths one copper layer is "
                            f"processed per call (this call: {target_layer}; pass "
                            f"--layer {lay})"
                        )
            candidates.extend(legs)

    if not candidates and not excluded_runs:
        raise ReinforceError(f"net {net_name!r} produced no walkable polyline")

    geoms = [_run_geometry(c.run) for c in candidates]

    # Selection mirrors ``spaced``: the minimum wire length reports (never
    # drops) short legs; without declared paths only the longest leg is
    # anchored unless --all-runs. With declared paths the declaration IS the
    # selection: every eligible leg is anchored.
    not_selected: dict[int, str] = {}
    #: Legs skipped by the length minimum specifically (#8915) -- not the
    #: other ``not_selected`` reasons, which still earn a wire record.
    below_min: set[int] = set()
    selectable = [i for i, c in enumerate(candidates) if c.status is None]
    strategy = result.strategy
    if min_wire_length_mm is not None:
        for i in selectable:
            if geoms[i].length_mm < min_wire_length_mm:
                below_min.add(i)
                # Issue #8916: under ``parallel`` / ``auto`` a short leg is
                # still selected -- it is exactly the leg a copy is for.
                if strategy == STRATEGY_WIRE:
                    not_selected[i] = (
                        f"shorter than the {min_wire_length_mm:g} mm minimum buttress-wire "
                        "length (--min-wire-length): a wire needs a through-hole at each "
                        "end and the two rarely fit on a shorter link"
                    )
        selectable = [i for i in selectable if i not in not_selected]
    if not gated and not all_runs and selectable:
        longest = max(selectable, key=lambda i: (geoms[i].length_mm, -i))
        for i in selectable:
            if i != longest:
                not_selected[i] = (
                    "not selected: without --all-runs only the longest leg is anchored"
                )

    # Issue #8916: the strategy each selected leg gets. ``auto`` reuses the
    # minimum's own verdict (``below_min``) rather than a second comparison.
    def leg_strategy(i: int) -> str:
        if strategy == STRATEGY_PARALLEL or (strategy == STRATEGY_AUTO and i in below_min):
            return STRATEGY_PARALLEL
        return STRATEGY_WIRE

    # A leg already doubled by an earlier pass is two selected legs, one per
    # outer layer, that coincide. The shorter one (on a tie the later) is the
    # copy: it is reinforced with the other and must not be copied back, so a
    # re-run adds no copper and no second wire-list record.
    copy_of: dict[int, int] = {}
    if strategy != STRATEGY_WIRE:
        par = [
            i for i in selectable if i not in not_selected and leg_strategy(i) == STRATEGY_PARALLEL
        ]
        for i in par:
            for j in par:
                if j <= i or i in copy_of or j in copy_of:
                    continue
                if OPPOSITE_OUTER_LAYER.get(candidates[i].layer) != candidates[j].layer:
                    continue
                tol = min(s.width for s in candidates[i].run + candidates[j].run) / 2.0
                keep, dup = (i, j) if geoms[j].length_mm <= geoms[i].length_mm + 1e-6 else (j, i)
                if _coincident(geoms[dup].points, geoms[keep].points, tol):
                    copy_of[dup] = keep
        # The copy is on the layer this call does not process: still the copy.
        for i in par:
            for j, c in enumerate(candidates):
                if not c.off_layer or j in copy_of:
                    continue
                if OPPOSITE_OUTER_LAYER.get(candidates[i].layer) != c.layer:
                    continue
                tol = min(s.width for s in candidates[i].run + c.run) / 2.0
                if geoms[j].length_mm <= geoms[i].length_mm + 1e-6 and _coincident(
                    geoms[j].points, geoms[i].points, tol
                ):
                    copy_of[j] = i

    # Trunks / plain runs before branches, so a branch's junction-side anchor
    # steers clear of the trunk anchors; longest first within each.
    order = sorted(
        range(len(candidates)),
        key=lambda i: (candidates[i].kind == LEG_KIND_BRANCH, -geoms[i].length_mm, i),
    )

    other = _collect_other_net_copper(pcb, target_net_number, anchor_drill, anchor_pad)
    features = _same_net_end_features(pcb, target_net_number, anchor_drill, anchor_pad)
    dedup_floor = max(anchor_pad, anchor_drill + rules.min_hole_to_hole_mm)
    placed_positions: list[tuple[float, float]] = []
    arc_of: dict[tuple[float, float], float] = {}

    def violation_at(p: tuple[float, float]) -> _Violation | None:
        """Rule check of one anchor candidate; ``None`` when it may be placed.

        An anchor already placed for another leg is a drilled hole and a pad
        too: a hole gap below the hole-to-hole minimum is ``hole_to_hole``,
        otherwise overlapping anchor pads are ``anchor_spacing``.
        """
        if placed_positions:
            near = min(math.hypot(p[0] - q[0], p[1] - q[1]) for q in placed_positions)
            if near < dedup_floor - 1e-9:
                hole_gap = near - anchor_drill
                if hole_gap < rules.min_hole_to_hole_mm - 1e-9:
                    return _Violation(
                        RULE_HOLE_TO_HOLE, rules.min_hole_to_hole_mm, max(0.0, hole_gap)
                    )
                return _Violation(RULE_ANCHOR_SPACING, anchor_pad, near)
        for hx, hy, hd in pass_stitch_holes:
            gap = math.hypot(p[0] - hx, p[1] - hy) - anchor_drill / 2.0 - hd / 2.0
            if gap < rules.min_hole_to_hole_mm - 1e-9:
                return _Violation(RULE_HOLE_TO_HOLE, rules.min_hole_to_hole_mm, max(0.0, gap))
        return _position_violation(p[0], p[1], anchor_pad, anchor_drill, rules, other, policy)

    # ---- parallel copies (Issue #8916) ----------------------------------
    via_size, via_drill = stitch_via_geometry(rules)
    #: Stitching vias decided this pass (anchors are in ``placed_positions``).
    pass_stitch_holes: list[tuple[float, float, float]] = []
    stitch_floor = _same_net_stitch_spacing(via_size, via_drill, rules)
    board_drills: list[tuple[float, float, float, bool]] = []
    #: Copper an earlier pass recorded as its own (the sidecar, never size or
    #: shape): ``id()`` s of the vias and copy-layer tracks a record names.
    owned_vias: set[int] = set()
    owned_segments: set[int] = set()
    if strategy != STRATEGY_WIRE:
        for _cleared, segs, vias in _recorded_copies(
            pcb, net_name, target_net_number, sidecar_wires or []
        ):
            owned_segments.update(id(s) for s in segs)
            owned_vias.update(id(v) for v in vias)
        for via in pcb.vias:
            if via.drill > 0:
                own_stitch = id(via) in owned_vias
                board_drills.append((via.position[0], via.position[1], via.drill, own_stitch))
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.drill > 0:
                    pos = pcb.get_pad_position(fp.reference, pad.number)
                    if pos is not None:
                        board_drills.append((pos[0], pos[1], pad.drill, False))

    def stitch_violation(p: tuple[float, float], planned) -> _Violation | None:
        """Rule check of one stitching-via candidate; ``None`` when clear.

        A via an earlier pass recorded (``owned_vias``) already at ``p`` is
        this tool's own via (idempotent re-run), not an obstacle. Any other
        via, whatever its size, is an obstacle, so a stitch is never planned
        on top of copper that predates the tool.
        """
        holes = [(q[0], q[1], anchor_drill) for q in placed_positions]
        holes += pass_stitch_holes + [(q[0], q[1], via_drill) for q in planned]
        for hx, hy, hd in holes:
            gap = math.hypot(p[0] - hx, p[1] - hy) - via_drill / 2.0 - hd / 2.0
            if gap < rules.min_hole_to_hole_mm - 1e-9:
                return _Violation(RULE_HOLE_TO_HOLE, rules.min_hole_to_hole_mm, max(0.0, gap))
        drills = [
            (x, y, d)
            for x, y, d, own in board_drills
            if not (own and math.hypot(p[0] - x, p[1] - y) <= 0.01)
        ]
        view = _OtherNetCopper(by_net=other.by_net, net_names=other.net_names, all_drills=drills)
        return _position_violation(p[0], p[1], via_size, via_drill, rules, view, policy)

    def end_tie(points, cum, layer: str, cap: float):
        """How the copy is tied at the leg end ``points[0]``.

        ``(False, 0.0)``: the end lands on a plated through-hole pad or a
        through via that predates this tool (any via no sidecar record names,
        whatever its size), whose barrel already joins the layers -- the copy
        runs to the vertex and nothing is placed or recorded there. ``(True, arc)``: a stitching via ties it at ``arc`` -- the
        vertex itself for a free track end or a junction, or the first point
        whose via clears the SMD pad the leg ends on (no via in an SMD pad).
        ``None``: a pad covers the whole leg.
        """
        copper = _end_copper(points, layer, features, [], cap)
        if copper is None:
            return None
        start = points[0]
        for feat in copper:
            if feat.pad is not None and feat.pad.type == "thru_hole":
                return False, 0.0
        for via in pcb.vias:
            if via.net_number != target_net_number or via.via_type not in (None, "through"):
                continue
            if id(via) in owned_vias:
                continue  # an earlier pass's recorded tie via: re-planned, not relied on
            if math.dist(via.position, start) <= via.size / 2.0 + cap + 1e-6:
                return False, 0.0
        smd = [f for f in copper if f.pad is not None]
        if not smd:
            return True, 0.0
        extent = _end_extent(points, smd)

        def clear(p, _smd=smd):
            return all(
                f.distance(p) - via_size / 2.0 >= rules.min_clearance_mm - 1e-6 for f in _smd
            )

        t = _clear_arc(points, cum, extent, cum[-1], clear)
        return (True, t) if t is not None else None

    def refuse_copy(leg: LegSummary, copy_layer: str, pos, violation: _Violation, why: str) -> None:
        leg.status = LEG_UNANCHORED
        leg.copy_status = COPY_REFUSED
        detail = (
            f" ({violation.rule}: need {violation.required_mm:.3f} mm, have "
            f"{violation.actual_mm:.3f} mm"
            + (f", vs net {violation.obstacle_net!r}" if violation.obstacle_net else "")
            + ")"
            if violation.rule != RULE_COPY_GEOMETRY
            else ""
        )
        leg.copy_reason = f"parallel copy on {copy_layer or 'another layer'} refused: {why}{detail}"
        leg.reason = leg.copy_reason + "; nothing written for this leg"
        result.refused_copies.append(
            RefusedCopy(
                leg_layer=leg.layer,
                copy_layer=copy_layer,
                x=pos[0],
                y=pos[1],
                rule=violation.rule,
                required_mm=violation.required_mm,
                actual_mm=violation.actual_mm,
                obstacle_net=violation.obstacle_net,
                reason=leg.copy_reason,
            )
        )

    def plan_copy(cand: _LegCandidate, g: _RunGeom, leg: LegSummary) -> None:
        """Copy one leg onto the opposite outer layer, whole or not at all."""
        leg.strategy = STRATEGY_PARALLEL
        points = g.points
        cum = _cumulative_arc(points)
        total = cum[-1]
        copy_layer = OPPOSITE_OUTER_LAYER.get(cand.layer, "")
        no_room = _Violation(RULE_COPY_GEOMETRY, 0.0, 0.0)
        if not copy_layer:
            refuse_copy(leg, "", points[0], no_room, f"{cand.layer} is an inner layer")
            return
        ties = [
            end_tie(points, cum, cand.layer, cand.run[0].width / 2.0),
            end_tie(points[::-1], cum, cand.layer, cand.run[-1].width / 2.0),
        ]
        if ties[0] is None or ties[1] is None:
            refuse_copy(leg, copy_layer, points[0], no_room, "same-net pad copper covers the leg")
            return
        planned: list[tuple[float, float]] = []
        span = [ties[0][1], total - ties[1][1]]
        reach = STITCH_PITCH_MM / 2.0
        steps = max(1, round(reach / (END_NUDGE_MAX_MM / END_NUDGE_STEPS)))
        for end, sign in ((0, 1.0), (1, -1.0)):
            needs_via, _ = ties[end]
            if not needs_via:
                continue
            base = span[end]
            first: tuple[tuple[float, float], _Violation] | None = None
            chosen = None
            for k in range(steps + 1):
                t = base + sign * reach * k / steps
                if not 0.0 <= t <= total:
                    break
                p = _point_at_arc(points, cum, t)
                v = stitch_violation(p, planned)
                if v is None:
                    chosen = (t, p)
                    break
                first = first or (p, v)
            if chosen is None:
                assert first is not None
                refuse_copy(
                    leg, copy_layer, first[0], first[1], "no clear position for an end tie via"
                )
                return
            span[end] = chosen[0]
            planned.append(chosen[1])
        lo, hi = span
        if hi - lo < stitch_floor - 1e-9:
            refuse_copy(
                leg,
                copy_layer,
                points[0],
                no_room,
                f"{max(0.0, hi - lo):.3f} mm between its end ties, need {stitch_floor:.3f} mm",
            )
            return
        n = max(0, math.ceil((hi - lo) / STITCH_PITCH_MM - 1e-9) - 1)
        while n > 0 and (hi - lo) / (n + 1) < stitch_floor - 1e-9:
            n -= 1
        pitch = (hi - lo) / (n + 1)
        for k in range(1, n + 1):
            target = lo + k * pitch
            first = None
            chosen_p = None
            for t in _nudge_arc_candidates(target, total, pitch):
                if not lo + stitch_floor - 1e-9 <= t <= hi - stitch_floor + 1e-9:
                    continue
                p = _point_at_arc(points, cum, t)
                if _too_close(p, planned, stitch_floor):
                    continue
                v = stitch_violation(p, planned)
                if v is None:
                    chosen_p = p
                    break
                first = first or (p, v)
            if chosen_p is None:
                pos, v = (
                    first if first is not None else (_point_at_arc(points, cum, target), no_room)
                )
                refuse_copy(leg, copy_layer, pos, v, "no clear position for a stitching via")
                return
            planned.append(chosen_p)
        planned.sort(key=lambda p: _arc_of_point(points, cum, p))
        segments = _clip_run(cand.run, lo, hi)
        xs = [c for a, b, _ in segments for c in (a[0], b[0])]
        ys = [c for a, b, _ in segments for c in (a[1], b[1])]
        m = _COPY_OBSTACLE_MARGIN_MM
        obstacles, drills = _layer_obstacles(
            pcb, target_net_number, copy_layer, (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)
        )
        hit = _copy_track_violation(segments, obstacles, drills, policy)
        if hit is not None:
            v, pos = hit
            refuse_copy(leg, copy_layer, pos, v, "its track would break a clearance rule")
            return
        # A piece already on the copy layer that no record names predates the
        # tool: it is the copy's copper there, but neither written again nor
        # recorded, so ``--clear`` can never remove it.
        foreign = [
            (a, b, width)
            for a, b, width in segments
            if any(
                id(s) not in owned_segments
                and s.layer == copy_layer
                and s.net_number == target_net_number
                and abs(s.width - width) <= _ANCHOR_SIZE_TOL_MM
                and (
                    (_close(s.start, a) and _close(s.end, b))
                    or (_close(s.start, b) and _close(s.end, a))
                )
                for s in pcb.segments
            )
        ]
        copy = ParallelCopy(
            layer=copy_layer,
            segments=[piece for piece in segments if piece not in foreign],
            stitch_vias=planned,
            via_size_mm=via_size,
            via_drill_mm=via_drill,
            existing_segments=len(foreign),
        )
        for a, b, width in copy.segments:
            if dry_run:
                continue
            if not pcb.add_trace(a, b, width=width, layer=copy_layer, net=net_name, dedupe=True):
                copy.existing_segments += 1
        for p in planned:
            pass_stitch_holes.append((p[0], p[1], via_drill))
            if dry_run:
                continue
            if (
                pcb.add_via(
                    p[0],
                    p[1],
                    size=via_size,
                    drill=via_drill,
                    layers=("F.Cu", "B.Cu"),
                    net=net_name,
                    dedupe=True,
                )
                is None
            ):
                copy.existing_vias += 1
        leg.status = LEG_PARALLEL
        leg.copy_status = COPY_PLACED
        leg.copy = copy
        result.copies.append(copy)
        ends = " / ".join("its through hole" if not tie[0] else "a tie via" for tie in ties)
        # Same text on a re-run (copper already present is counted on the
        # copy, not here), so the wire-list record stays byte-identical.
        leg.reason = (
            f"parallel copy on {copy_layer} (ends tied by {ends}), "
            f"{len(planned)} stitching via(s) of {via_size:.2f} mm pad / {via_drill:.2f} mm drill"
        )

    def plan_pair(points, cum, t0: float, t1: float, clear):
        """Decide BOTH end anchors before either is written (PR #8751 review).

        Start candidates step inward from ``t0``, far candidates inward from
        ``t1`` (``nudges``), each kept only where ``clear`` holds. A pair is
        geometrically compatible when the far anchor is at least the anchor
        spacing floor beyond the start one ALONG the leg (never past it) and
        at least that far apart STRAIGHT-LINE (a leg that folds back can
        meet the first and fail the second). Returns ``(pair, refusals)``:

        * ``pair is None`` and no refusals -- no compatible pair exists, the
          leg is ``too_short`` and nothing is placed;
        * both positions set -- the first compatible pair with no rule
          violation at either end;
        * one position set -- every far (start) candidate compatible with the
          placed start (far) anchor broke a clearance / hole-to-hole rule:
          a rule-based refusal, listed in ``refused``;
        * neither set, two refusals -- both ends refused by rules.
        """
        total = cum[-1]
        nudges = _end_shift_offsets(end_shift_bound(total, result.max_end_shift_mm))

        def side(base: float, sign: float):
            out = []
            for d in nudges:
                t = base + sign * d
                if t < -1e-9 or t > total + 1e-9:
                    continue
                pos = _point_at_arc(points, cum, t)
                if clear(pos):
                    out.append((t, pos))
                    arc_of[pos] = t
            return out

        starts, ends = side(t0, 1.0), side(t1, -1.0)

        def compatible(s, e) -> bool:
            return (
                e[0] - s[0] >= dedup_floor - 1e-9
                and math.hypot(e[1][0] - s[1][0], e[1][1] - s[1][1]) >= dedup_floor - 1e-9
            )

        pairs = [
            (i, j)
            for i in range(len(starts))
            for j in range(len(ends))
            if compatible(starts[i], ends[j])
        ]
        if not pairs:
            return None, []
        cache: dict[tuple[str, int], _Violation | None] = {}

        def v(which: str, k: int) -> _Violation | None:
            if (which, k) not in cache:
                cache[(which, k)] = violation_at((starts if which == "s" else ends)[k][1])
            return cache[(which, k)]

        for i, j in pairs:
            if v("s", i) is None and v("e", j) is None:
                return (starts[i][1], ends[j][1]), []
        # No clean pair. One anchor stands only when every counterpart that
        # could pair with it is refused by a rule.
        for i in dict.fromkeys(i for i, _ in pairs):
            if v("s", i) is None:
                j = next(j for ii, j in pairs if ii == i)
                return (starts[i][1], None), [(ends[j][1], v("e", j))]
        for j in dict.fromkeys(j for _, j in pairs):
            if v("e", j) is None:
                i = next(i for i, jj in pairs if jj == j)
                return (None, ends[j][1]), [(starts[i][1], v("s", i))]
        i, j = pairs[0]
        return (None, None), [(starts[i][1], v("s", i)), (ends[j][1], v("e", j))]

    def refuse(pos: tuple[float, float], violation: _Violation) -> None:
        result.refused.append(
            RefusedAnchor(
                x=pos[0],
                y=pos[1],
                reason=(
                    "anchor pad overlaps another leg's anchor"
                    if violation.rule == RULE_ANCHOR_SPACING
                    else "other-net clearance/hole-to-hole violation"
                ),
                rule=violation.rule,
                required_mm=violation.required_mm,
                actual_mm=violation.actual_mm,
                obstacle_net=violation.obstacle_net,
            )
        )

    attempted_flags: list[bool] = []
    for index in order:
        cand = candidates[index]
        g = geoms[index]
        names: list[str] = []
        for s in cand.run:
            for name in path_names.get(original_ids.get(id(s), id(s)), ()):
                if name not in names:
                    names.append(name)
        leg = LegSummary(
            net=net_name,
            layer=cand.layer,
            kind=cand.kind,
            polyline=list(g.points),
            length_mm=g.length_mm,
            width_mm=max(s.width for s in cand.run),
            paths=names,
        )
        refused_here = 0
        attempted_here = False
        if index in copy_of:
            # Issue #8916: the copy an earlier pass made; reinforced with its twin.
            twin = candidates[copy_of[index]]
            leg.strategy = STRATEGY_PARALLEL
            leg.below_min_wire_length = index in below_min
            leg.status = LEG_COPY
            leg.reason = (
                f"the parallel copy of the {twin.layer} leg it coincides with; "
                "reinforced together with it, so it gets no copy of its own"
            )
        elif cand.status is not None:
            leg.status, leg.reason = cand.status, cand.reason
        elif index in not_selected:
            leg.status, leg.reason = LEG_UNANCHORED, not_selected[index]
            leg.below_min_wire_length = index in below_min
        elif leg_strategy(index) == STRATEGY_PARALLEL:
            attempted_here = True
            leg.below_min_wire_length = index in below_min
            plan_copy(cand, g, leg)
        elif overlaps := _courtyard_overlaps(pcb, cand.layer, g.points, result.wire_gauge_awg):
            attempted_here = True
            leg.status = LEG_UNANCHORED
            leg.reason = (
                "refused: wire run would overlap the same-side courtyard of "
                + ", ".join(f"{ref} ({mm:.2f} mm)" for ref, mm in overlaps)
                + " (wire_run_courtyard_overlap)"
            )
        else:
            attempted_here = True
            points = g.points
            cum = _cumulative_arc(points)
            total = cum[-1]
            own = {original_ids.get(id(s), id(s)) for s in cand.run}
            tracks = _track_components([s for s in by_layer[cand.layer] if id(s) not in own])
            c0 = _end_copper(points, cand.layer, features, tracks, cand.run[0].width / 2.0)
            c1 = _end_copper(points[::-1], cand.layer, features, tracks, cand.run[-1].width / 2.0)
            half = anchor_pad / 2.0
            if c0 is None or c1 is None:
                leg.status = LEG_TOO_SHORT
                leg.reason = "same-net pad/via copper covers the whole leg"
            else:
                e0 = _end_extent(points, c0)
                e1 = _end_extent(points[::-1], c1)
                # The along-leg target alone is not enough: a leg that bends
                # soon after leaving the pad (or an oblique branch leaving the
                # trunk) brings that point back near the end copper. Keep every
                # anchor's straight-line copper gap to BOTH ends' copper at
                # least ``end_offset``, moving inward only.
                end_copper = c0 + c1
                # Any other same-net SMD pad on this layer is end copper too
                # for the clearance test: a pad mid-leg, or the end pad of an
                # adjacent leg of a Y, is not what THIS leg ends on, yet an
                # anchor must not land in it (Issue #8772). THT pads and vias
                # stay with the hole-to-hole check in ``_position_violation``.
                seen = {id(f) for f in end_copper}
                end_copper = end_copper + [
                    f
                    for f in features
                    if f.pad is not None
                    and f.layers
                    and id(f) not in seen
                    and f.on_layer(cand.layer)
                ]

                def clear(p, _copper=end_copper):
                    return all(f.distance(p) - half >= end_offset - 1e-6 for f in _copper)

                t0_arc = e0 + end_offset + half
                t1_arc = total - (e1 + end_offset + half)
                t0 = _clear_arc(points, cum, t0_arc, total, clear) if t0_arc <= total else None
                t1 = _clear_arc(points, cum, t1_arc, 0.0, clear) if t1_arc >= 0.0 else None
                pair = None
                refusals: list = []
                if t0 is not None and t1 is not None:
                    pair, refusals = plan_pair(points, cum, t0, t1, clear)
                if pair is None:
                    # Never one anchor for geometry: the leg gets none.
                    leg.status = LEG_TOO_SHORT
                    if t0 is None or t1 is None:
                        leg.reason = (
                            f"{total:.3f} mm leg has no point whose {anchor_pad:.3f} mm anchor "
                            f"is {end_offset:g} mm clear of the end copper"
                        )
                    elif t1 - t0 < dedup_floor - 1e-9:
                        leg.reason = (
                            f"{total:.3f} mm leg needs "
                            f"{total - (t1 - t0) + dedup_floor:.3f} mm for two "
                            f"{anchor_pad:.3f} mm anchors {end_offset:g} mm clear of the "
                            "end copper"
                        )
                    else:
                        p0 = _point_at_arc(points, cum, t0)
                        p1 = _point_at_arc(points, cum, t1)
                        leg.reason = (
                            f"leg folds back: its two end anchors, {end_offset:g} mm clear "
                            f"of the end copper, would be {math.dist(p0, p1):.3f} mm apart "
                            f"(need {dedup_floor:.3f} mm between {anchor_pad:.3f} mm anchors)"
                        )
                else:
                    for pos in pair:
                        if pos is not None:
                            leg.anchors.append(PlacedAnchor(x=pos[0], y=pos[1]))
                            placed_positions.append(pos)
                    for pos, violation in refusals:
                        refuse(pos, violation)
                        refused_here += 1
                    for a in leg.anchors:
                        if not dry_run:
                            pcb.add_via(
                                a.x,
                                a.y,
                                size=anchor_pad,
                                drill=anchor_drill,
                                layers=("F.Cu", "B.Cu"),
                                net=net_name,
                                dedupe=True,
                                tenting=("no", "no") if result.mask_opening else None,
                            )
                        result.placed.append(PlacedAnchor(x=a.x, y=a.y))
                    if len(leg.anchors) == 2:
                        leg.status = LEG_FULL
                        # Two anchors imply a plan_pair result, so both are set.
                        assert t0 is not None and t1 is not None
                        # Placed arcs, not the pre-search ``t0``/``t1``: a refused
                        # end candidate slides the anchor inward (Issue #8797).
                        a0 = arc_of.get(pair[0], t0)
                        a1 = arc_of.get(pair[1], t1)
                        leg.anchors[0].shift_mm = max(0.0, a0 - t0)
                        leg.anchors[1].shift_mm = max(0.0, t1 - a1)
                        leg.wire_span_mm = a1 - a0
                        leg.uncovered_mm = max(0.0, total - leg.wire_span_mm)
                        shifts = [
                            (name, d)
                            for name, d in (("start", a0 - t0_arc), ("far", t1_arc - a1))
                            if d > _ANCHOR_SHIFT_NOTE_MM
                        ]
                        slid = [
                            (name, a.shift_mm)
                            for name, a in (("start", leg.anchors[0]), ("far", leg.anchors[1]))
                            if a.shift_mm > 1e-6
                        ]
                        parts: list[str] = []
                        if shifts:
                            moved = "; ".join(
                                f"{name} anchor moved {d:.1f} mm inward" for name, d in shifts
                            )
                            parts.append(f"{moved} to stay clear of the end copper")
                        if slid:
                            parts.append(
                                "; ".join(
                                    f"{name} anchor slid {d:.2f} mm inward past a refused "
                                    "end position"
                                    for name, d in slid
                                )
                            )
                        if parts:
                            leg.reason = (
                                f"wire spans {leg.wire_span_mm:.1f} of {total:.1f} mm "
                                f"({leg.uncovered_mm:.1f} mm uncovered): " + "; ".join(parts)
                            )
                        if result.mask_opening:
                            _plan_mask_opening(
                                pcb, result, leg, points, cum, other, policy, dry_run
                            )
                    elif leg.anchors:
                        leg.status = LEG_PARTIAL
                        bound = end_shift_bound(total, result.max_end_shift_mm)
                        leg.reason = (
                            "1 of 2 end anchors refused by a clearance/hole-to-hole rule "
                            f"at every position within {bound:.1f} mm of the end "
                            "(see refused; raise --max-end-shift to search further)"
                        )
                    else:
                        leg.status = LEG_UNANCHORED
                        leg.reason = "both end anchors refused (see refused)"
        result.legs.append(leg)
        attempted_flags.append(attempted_here)
        result.runs.append(
            RunSummary(
                run_index=len(result.runs),
                length_mm=g.length_mm,
                segment_count=g.segment_count,
                # A parallel copy needs no anchors (Issue #8916).
                anchors_needed=0 if leg.strategy == STRATEGY_PARALLEL else 2,
                anchors_placed=len(leg.anchors),
                anchors_refused=refused_here,
            )
        )

    excluded_reason = (
        "excluded by declared current-path intent (Issue #4980): not covered by a "
        f"resolved, reinforcement-eligible CurrentPathSpec for net {net_name!r}"
    )
    for run in sorted(excluded_runs, key=_run_length, reverse=True):
        g = _run_geometry(run)
        result.runs.append(
            RunSummary(
                run_index=len(result.runs),
                length_mm=g.length_mm,
                segment_count=g.segment_count,
                anchors_needed=0,
                anchors_placed=0,
                anchors_refused=0,
                path_excluded_reason=excluded_reason,
            )
        )

    attempted = [(leg, result.runs[i]) for i, leg in enumerate(result.legs) if attempted_flags[i]]
    if attempted:
        primary_leg, primary_run = max(attempted, key=lambda item: item[0].length_mm)
        result.run_segment_count = primary_run.segment_count
        result.run_length_mm = primary_run.length_mm
        result.layer = target_layer if target_layer is not None else primary_leg.layer
    else:
        result.layer = target_layer or (result.legs[0].layer if result.legs else "")
    result.unhandled_runs = len(result.runs) - len(attempted)
    return result
