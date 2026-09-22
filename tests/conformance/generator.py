"""Seeded generator of small copper configurations (``CopperCase``).

Design notes that matter for the measurement being honest:

**Seeded, not property-based.**  ``random.Random(seed)`` only.  ``hypothesis``
is not a dependency of this repository and Epic #5509 Phase 1a explicitly must
not add one; the precedent for a seeded corpus is
``tests/router/test_pairwise_cpp_parity.py``.

**Analytic gap placement, not rejection sampling.**  The whole point of the
corpus is to probe *near the threshold*.  Sampling two random positions and
keeping the pairs that happen to land near the required clearance produces a
corpus that almost never does, and the measured disagreement rates come out
vacuously zero.  So the generator samples the **target gap first** --
``required + U(-0.05, +0.05)`` mm -- and then places the second object of the
pair analytically to realise exactly that gap.

**Boundary band.**  A draw within +/-0.001 mm (1 um) of the requirement is
re-drawn; the band is 10x kct's ``CLEARANCE_EPSILON_MM = 1e-4`` and ~1000x
KiCad's integer-nm resolution, so what remains is model disagreement rather
than rounding.  :attr:`PairIntent.boundary` still exists because the named
fixtures are hand-placed and may deliberately sit in the band.

**One net per object.**  Every segment, via, pad and zone gets its own net
(``N1``, ``N2``, ...).  That makes a kicad-cli finding map onto a pair by
``frozenset(nets)`` alone -- no geometry matching, no dependence on the
reported ``pos``, no ambiguity when two objects of the same kind are close.

**Well-separated slots.**  Pairs are laid out on a coarse lattice with a pitch
far larger than any clearance under test, so the only near-neighbour
relationship on the board is the intended one.  A stray cross-slot finding
would be a harness bug, and the corpus test asserts it does not happen.

**Zone pairs can only be placed on the clean side, and that is a property of
KiCad, not a gap in this generator.**  A freshly refilled pour is backed off
from every piece of foreign copper by the applied clearance -- measured at
0.2005 mm for a 0.20 mm netclass on kicad-cli 10, and *independent* of the
zone's own local clearance value.  So no placement can put fresh fill copper
closer to a foreign object than the requirement: a sub-threshold zone pair is
unreachable by construction on a refilled run, and the only way to manufacture
one is a stale fill, which measures the fill rather than the clearance model
(Epic #5509's scope guard forbids exactly that).  ``seg-zone`` / ``via-zone``
therefore draw their gap from :func:`_draw_clear_gap` -- strictly *above* the
requirement, and far enough above that the filler leaves the pour outline
untouched, so the realised gap is the declared one in closed form.  Those rows
can show over-rejection and cannot show under-rejection; the ``copper-edge``
kind has no such restriction and probes both sides.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

from kicad_tools.core.geometry import rotate_pad_offset
from tests.conformance.adapters import BOARD_EDGE

__all__ = [
    "BOARD_EDGE",
    "BOUNDARY_BAND_MM",
    "CaseRules",
    "CopperCase",
    "PadSpec",
    "PairIntent",
    "PairKind",
    "SegmentSpec",
    "ViaSpec",
    "ZoneSpec",
    "DEFAULT_RULES",
    "PAD_SHAPES",
    "generate_case",
    "generate_corpus",
]

BOUNDARY_BAND_MM = 0.001
"""Half-width of the "too close to the threshold to be meaningful" band."""

FIXED_BOARD_DATE = "2026-01-01"
"""Pinned title-block date -- ``PCB.create`` otherwise stamps ``date.today()``
and every regenerated fixture would differ byte-for-byte from the committed
one (Epic #5509 Phase 1a determinism criterion)."""


# ---------------------------------------------------------------------------
# Pad shape catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PadShape:
    """One committed probe footprint under ``Conformance.pretty/``.

    Attributes:
        name: Footprint (and ``.kicad_mod``) name.
        shape: KiCad pad shape keyword.
        size: ``(width, height)`` in mm, in the pad's own local frame.
        roundrect_rratio: Corner-radius ratio for ``roundrect`` pads.
        half_extent_x: Support of the pad outline along its local ``+X``
            axis, i.e. how far the copper reaches in that direction from the
            pad centre.  For every shape in this catalogue that is ``w / 2``:
            rect and roundrect reach ``w/2`` at the mid-edge, a circle reaches
            ``r`` in every direction, and a stadium reaches ``w/2`` along its
            long axis.  Placing the partner object along local ``+X`` therefore
            yields an exactly-known gap **independent of the pad rotation**.
    """

    name: str
    shape: str
    size: tuple[float, float]
    roundrect_rratio: float | None = None

    @property
    def half_extent_x(self) -> float:
        return self.size[0] / 2.0


PAD_SHAPES: tuple[PadShape, ...] = (
    PadShape(name="Pad_Rect", shape="rect", size=(1.2, 0.8)),
    PadShape(name="Pad_Circle", shape="circle", size=(1.0, 1.0)),
    PadShape(name="Pad_Oval", shape="oval", size=(1.6, 0.8)),
    PadShape(name="Pad_RoundRect", shape="roundrect", size=(1.0, 1.0), roundrect_rratio=0.25),
)

_PAD_SHAPES_BY_NAME = {s.name: s for s in PAD_SHAPES}


def pad_shape(name: str) -> PadShape:
    """Look up a probe pad shape by footprint name."""
    return _PAD_SHAPES_BY_NAME[name]


# ---------------------------------------------------------------------------
# Rule values carried by a case
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseRules:
    """Rule values under test for one case.

    Two *different* rule objects are derived from this by consumers:

    * ``project_clearance`` (plus the hole/edge values) becomes the
      ``.kicad_pro`` ``Default`` netclass and ``design_settings.rules`` block,
      which is what ``kicad-cli pcb drc`` actually applies.  This is the value
      the #5398 route-clearance resolver never consults.
    * ``trace_clearance`` / ``via_clearance`` are the *router*-side values.
      They are recorded here (rather than derived) precisely because the epic's
      thesis is that they can disagree with the project netclass.

    Keeping both on the case is what lets an adapter drive a consumer with the
    rule values that consumer really uses today, without the harness quietly
    "fixing" the disagreement it is supposed to measure.
    """

    project_clearance: float = 0.20
    trace_clearance: float = 0.15
    via_clearance: float = 0.20
    min_hole_to_hole: float = 0.25
    min_drill_clearance: float = 0.25
    min_copper_to_edge: float = 0.30
    min_trace_width: float = 0.15
    min_via_diameter: float = 0.40
    min_via_drill: float = 0.20
    grid_resolution: float = 0.127


DEFAULT_RULES = CaseRules()


# ---------------------------------------------------------------------------
# Copper objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentSpec:
    """A routed track segment.  Coordinates are board-relative mm."""

    net: str
    start: tuple[float, float]
    end: tuple[float, float]
    width: float
    layer: str = "F.Cu"

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)


@dataclass(frozen=True)
class ViaSpec:
    """A through via.  ``diameter`` is the copper pad, ``drill`` the hole."""

    net: str
    x: float
    y: float
    diameter: float
    drill: float
    layers: tuple[str, str] = ("F.Cu", "B.Cu")


@dataclass(frozen=True)
class PadSpec:
    """A single-pad probe footprint placed at ``(x, y)`` and rotated."""

    net: str
    reference: str
    footprint: str
    x: float
    y: float
    rotation: float
    layer: str = "F.Cu"

    @property
    def shape(self) -> PadShape:
        return pad_shape(self.footprint)

    def local_x_axis(self) -> tuple[float, float]:
        """World-frame unit vector of the pad's local ``+X`` axis.

        Uses :func:`kicad_tools.core.geometry.rotate_pad_offset`, the repo's
        canonical (pcbnew-verified, issue #3739) local->world pad transform, so
        the analytic placement below cannot drift from what KiCad renders.
        """
        return rotate_pad_offset(1.0, 0.0, self.rotation)


@dataclass(frozen=True)
class ZoneSpec:
    """One copper pour.  Written unfilled; only meaningful after a refill."""

    net: str
    layer: str
    boundary: tuple[tuple[float, float], ...]
    clearance: float
    min_thickness: float = 0.25


# ---------------------------------------------------------------------------
# Pair intents
# ---------------------------------------------------------------------------


class PairKind:
    """The close-pair shapes the generator knows how to place analytically.

    :attr:`ROUTING` is the subset whose second object is something a router
    could *propose* -- a segment or a via -- **and** whose counterpart is
    ordinary routed or placement copper.  ``PAD_PAD`` is not: two pads are
    placement output, and the only in-tree consumer that answers a pad-vs-pad
    question is the incremental placement DRC (Epic #5509 group 19,
    ``drc/cpp_backend.py check_pair_clearance_cpp``), which takes two whole
    footprints and nothing else.  Keeping the split explicit is what lets
    every router-side adapter default to :data:`ROUTING` (via
    ``_support.ALL_PAIR_KINDS``) and be scored only on pairs it is really
    consulted for.

    :attr:`ZONE` and :attr:`EDGE` are the #5644 additions, and both are
    deliberately *outside* :attr:`ROUTING` for the same reason ``PAD_PAD`` is:
    a consumer that models neither zone fill nor the board outline must not be
    scored on them by default.  Only the groups that really consult those
    branches claim them -- group 18's ``SegmentZoneClearanceRule`` /
    ``ViaZoneClearanceRule`` / ``physical_gap.py`` / ``EdgeClearanceRule``, and
    group 10's ``ObstacleModel.is_clear`` ``pours`` / ``outline`` branches.

    The counterpart of a zone pair is the case's pour (a real net); the
    counterpart of an edge pair is the reserved
    :data:`~tests.conformance.adapters.BOARD_EDGE` pseudo-net, which is what
    the oracle already pairs a one-sided ``copper_edge_clearance`` finding
    with, so a generated intent and a kicad-cli row share one key.
    """

    SEG_SEG = "seg-seg"
    SEG_VIA = "seg-via"
    VIA_VIA = "via-via"
    PAD_SEG = "pad-seg"
    PAD_VIA = "pad-via"
    PAD_PAD = "pad-pad"
    SEG_ZONE = "seg-zone"
    VIA_ZONE = "via-zone"
    COPPER_EDGE = "copper-edge"

    ROUTING = (SEG_SEG, SEG_VIA, VIA_VIA, PAD_SEG, PAD_VIA)
    ZONE = (SEG_ZONE, VIA_ZONE)
    EDGE = (COPPER_EDGE,)
    ALL = (*ROUTING, PAD_PAD, *ZONE, *EDGE)


@dataclass(frozen=True)
class PairIntent:
    """What the generator *meant* to build, recorded for the report.

    The oracle never reads this -- kicad-cli is truth.  It exists so the
    report can say "of N pairs placed at a known gap, M were flagged", and so
    a harness bug (placement that does not realise the intended gap) is
    detectable by comparing ``target_gap_mm`` against kicad-cli's reported
    ``actual``.
    """

    kind: str
    net_a: str
    net_b: str
    target_gap_mm: float
    required_mm: float
    expect_violation: bool
    boundary: bool = False

    @property
    def nets(self) -> frozenset[str]:
        return frozenset({self.net_a, self.net_b})


@dataclass(frozen=True)
class CopperCase:
    """A complete, self-contained copper configuration.

    Attributes:
        seed: The RNG seed the case was generated from (``-1`` for the
            hand-authored named fixtures).
        name: Stable slug; also the fixture filename stem.
        width / height: Board outline size in mm.
        layers: Copper layer count (2 or 4).
        rules: Rule values under test.
        segments / vias / pads: Copper objects, each on its own net.
        zone: Optional single pour.
        pairs: The close pairs the generator placed, with their known gaps.
        board_date: Pinned title-block date (determinism).
        notes: Free-text provenance shown in the report and the PR.
    """

    seed: int
    name: str
    width: float
    height: float
    layers: int
    rules: CaseRules
    segments: tuple[SegmentSpec, ...] = ()
    vias: tuple[ViaSpec, ...] = ()
    pads: tuple[PadSpec, ...] = ()
    zone: ZoneSpec | None = None
    pairs: tuple[PairIntent, ...] = ()
    board_date: str = FIXED_BOARD_DATE
    notes: str = ""

    @property
    def copper_objects(self) -> tuple[SegmentSpec | ViaSpec | PadSpec, ...]:
        """Segments, vias and pads, in declaration order."""
        return (*self.segments, *self.vias, *self.pads)

    @property
    def nets(self) -> tuple[str, ...]:
        """Every declared net, in declaration order."""
        names: list[str] = []
        for obj in self.copper_objects:
            if obj.net not in names:
                names.append(obj.net)
        if self.zone is not None and self.zone.net not in names:
            names.append(self.zone.net)
        return tuple(names)

    @property
    def boundary_pairs(self) -> tuple[PairIntent, ...]:
        return tuple(p for p in self.pairs if p.boundary)

    def with_name(self, name: str) -> CopperCase:
        return replace(self, name=name)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

# Slot lattice.  The pitch is ~40x the largest clearance under test, so two
# objects in different slots can never interact; the only near pair on the
# board is the intended one.
_SLOT_PITCH_MM = 10.0
_BOARD_MARGIN_MM = 5.0
_BOARD_WIDTH_MM = 60.0
_BOARD_HEIGHT_MM = 50.0
_SLOT_COLS = 5
_SLOT_ROWS = 4

# Vias use a generous annular ring on purpose: with diameter 0.8 / drill 0.3
# the drill-to-drill gap of a via-via pair is (copper gap + 0.5) mm, which
# keeps it comfortably clear of ``min_hole_to_hole`` (0.25) for every copper
# gap the generator draws.  A 0.6/0.3 via would put the hole-to-hole distance
# right on its own threshold and the case would measure two things at once.
_VIA_DIAMETER_MM = 0.8
_VIA_DRILL_MM = 0.3

_SEG_HALF_LEN_MM = 3.0
_PAD_PROBE_HALF_LEN_MM = 3.0

# The pour's inset from the board outline.  Zone probes are placed in the strip
# *outside* the pour (between the outline and the pour boundary), which is what
# makes their gap to the fill analytic: the filler only knocks the pour back
# where foreign copper is closer than the applied clearance, and a probe drawn
# by ``_draw_clear_gap`` never is.
_ZONE_INSET_MM = _BOARD_MARGIN_MM * 0.5

# How far above the requirement a zone pair's gap is drawn.  The floor is not
# cosmetic: kicad-cli 10 backs a fresh fill off by ``clearance + 0.0005`` mm
# (measured), so a gap inside that margin would be realised by the *knockback*
# rather than by the declared placement and the corpus would be measuring the
# filler.  20 um of headroom is 40x that margin and still well inside the
# "near the threshold" band the corpus exists to probe.
_ZONE_GAP_MIN_EXCESS_MM = 0.02
_ZONE_GAP_MAX_EXCESS_MM = 0.06

# Edge probes run parallel to the board's top edge (``y = 0``), far from every
# slot row (the nearest slot centre is 10 mm away) and from the pour boundary.
_EDGE_PROBE_HALF_LEN_MM = 3.0


def _slot_centre(index: int) -> tuple[float, float]:
    col = index % _SLOT_COLS
    row = index // _SLOT_COLS
    if row >= _SLOT_ROWS:
        raise ValueError(f"slot index {index} exceeds the {_SLOT_COLS}x{_SLOT_ROWS} lattice")
    return (
        _BOARD_MARGIN_MM + _SLOT_PITCH_MM * (col + 0.5),
        _BOARD_MARGIN_MM + _SLOT_PITCH_MM * (row + 0.5),
    )


def _draw_gap(rng: random.Random, required: float) -> float:
    """Draw a target gap near ``required``, avoiding the boundary band."""
    for _ in range(64):
        gap = required + rng.uniform(-0.05, 0.05)
        if gap > 0.01 and abs(gap - required) > BOUNDARY_BAND_MM:
            return round(gap, 6)
    # Unreachable in practice; deterministic fallback keeps the corpus valid.
    return round(required + 0.03, 6)


def _draw_clear_gap(rng: random.Random, required: float) -> float:
    """Draw a gap strictly *above* ``required``, for a zone pair.

    The one-sided draw is forced by KiCad, not chosen: a refilled pour is
    backed off from foreign copper by the applied clearance, so fill copper can
    never end up closer than the requirement and a sub-threshold zone pair does
    not exist on a refilled board (see this module's docstring).  Drawing from
    ``required + [20 um, 60 um]`` keeps the pair near the threshold -- where the
    corpus is meant to probe -- while staying clear of the filler's own
    ~0.5 um knockback margin, so the realised gap is the declared one.
    """
    return round(
        required + rng.uniform(_ZONE_GAP_MIN_EXCESS_MM, _ZONE_GAP_MAX_EXCESS_MM),
        6,
    )


def _round_pt(pt: tuple[float, float]) -> tuple[float, float]:
    """Quantise to 1 nm.

    KiCad stores coordinates as integer nanometres and re-serialises what it
    parses, so feeding it values with float noise below 1 nm makes a
    round-tripped board differ from the one we wrote.
    """
    return (round(pt[0], 6), round(pt[1], 6))


class _CaseBuilder:
    """Accumulates objects/pairs for one case, minting nets as it goes."""

    def __init__(
        self,
        rng: random.Random,
        rules: CaseRules,
        zone: ZoneSpec | None = None,
    ) -> None:
        self.rng = rng
        self.rules = rules
        self.zone = zone
        self.segments: list[SegmentSpec] = []
        self.vias: list[ViaSpec] = []
        self.pads: list[PadSpec] = []
        self.pairs: list[PairIntent] = []
        self._net_counter = 0
        self._pad_counter = 0

    def next_net(self) -> str:
        self._net_counter += 1
        return f"N{self._net_counter}"

    def next_pad_ref(self) -> str:
        self._pad_counter += 1
        return f"P{self._pad_counter}"

    # -- placement primitives ------------------------------------------------

    def _record(
        self,
        kind: str,
        net_a: str,
        net_b: str,
        gap: float,
        required: float,
    ) -> None:
        self.pairs.append(
            PairIntent(
                kind=kind,
                net_a=net_a,
                net_b=net_b,
                target_gap_mm=gap,
                required_mm=required,
                expect_violation=gap < required,
                boundary=abs(gap - required) <= BOUNDARY_BAND_MM,
            )
        )

    def add_seg_seg(self, centre: tuple[float, float], layer: str) -> None:
        cx, cy = centre
        required = self.rules.project_clearance
        gap = _draw_gap(self.rng, required)
        w_a = self.rules.min_trace_width
        w_b = self.rules.min_trace_width
        # Two parallel horizontal tracks: centre-to-centre spacing is the
        # target edge gap plus both half-widths, so the edge-to-edge gap is
        # exactly ``gap``.
        offset = (gap + w_a / 2.0 + w_b / 2.0) / 2.0
        net_a, net_b = self.next_net(), self.next_net()
        self.segments.append(
            SegmentSpec(
                net=net_a,
                start=_round_pt((cx - _SEG_HALF_LEN_MM, cy - offset)),
                end=_round_pt((cx + _SEG_HALF_LEN_MM, cy - offset)),
                width=w_a,
                layer=layer,
            )
        )
        self.segments.append(
            SegmentSpec(
                net=net_b,
                start=_round_pt((cx - _SEG_HALF_LEN_MM, cy + offset)),
                end=_round_pt((cx + _SEG_HALF_LEN_MM, cy + offset)),
                width=w_b,
                layer=layer,
            )
        )
        self._record(PairKind.SEG_SEG, net_a, net_b, gap, required)

    def add_seg_via(self, centre: tuple[float, float], layer: str) -> None:
        cx, cy = centre
        required = self.rules.project_clearance
        gap = _draw_gap(self.rng, required)
        w = self.rules.min_trace_width
        net_seg, net_via = self.next_net(), self.next_net()
        self.segments.append(
            SegmentSpec(
                net=net_seg,
                start=_round_pt((cx - _SEG_HALF_LEN_MM, cy)),
                end=_round_pt((cx + _SEG_HALF_LEN_MM, cy)),
                width=w,
                layer=layer,
            )
        )
        # A through via reaches every copper layer, so this pair is live
        # whichever layer the segment is on.
        vy = cy + w / 2.0 + gap + _VIA_DIAMETER_MM / 2.0
        self.vias.append(
            ViaSpec(
                net=net_via,
                x=round(cx, 6),
                y=round(vy, 6),
                diameter=_VIA_DIAMETER_MM,
                drill=_VIA_DRILL_MM,
            )
        )
        self._record(PairKind.SEG_VIA, net_seg, net_via, gap, required)

    def add_via_via(self, centre: tuple[float, float], layer: str) -> None:
        del layer  # through vias span all layers
        cx, cy = centre
        required = self.rules.project_clearance
        gap = _draw_gap(self.rng, required)
        separation = _VIA_DIAMETER_MM + gap
        net_a, net_b = self.next_net(), self.next_net()
        self.vias.append(
            ViaSpec(
                net=net_a,
                x=round(cx - separation / 2.0, 6),
                y=round(cy, 6),
                diameter=_VIA_DIAMETER_MM,
                drill=_VIA_DRILL_MM,
            )
        )
        self.vias.append(
            ViaSpec(
                net=net_b,
                x=round(cx + separation / 2.0, 6),
                y=round(cy, 6),
                diameter=_VIA_DIAMETER_MM,
                drill=_VIA_DRILL_MM,
            )
        )
        self._record(PairKind.VIA_VIA, net_a, net_b, gap, required)

    def _place_pad(self, centre: tuple[float, float]) -> PadSpec:
        shape = self.rng.choice(PAD_SHAPES)
        rotation = round(self.rng.uniform(0.0, 360.0), 3)
        pad = PadSpec(
            net=self.next_net(),
            reference=self.next_pad_ref(),
            footprint=shape.name,
            x=round(centre[0], 6),
            y=round(centre[1], 6),
            rotation=rotation,
        )
        self.pads.append(pad)
        return pad

    def add_pad_seg(self, centre: tuple[float, float], layer: str) -> None:
        del layer  # pads are on F.Cu; the probe must share that layer
        required = self.rules.project_clearance
        gap = _draw_gap(self.rng, required)
        pad = self._place_pad(centre)
        ux, uy = pad.local_x_axis()
        w = self.rules.min_trace_width
        # Offset along the pad's own +X axis by (support + gap + half width)
        # and run the probe segment perpendicular to it.  For every convex
        # shape in the catalogue the support along +X is w/2, so the resulting
        # copper gap is exactly ``gap`` for ANY pad rotation.
        d = pad.shape.half_extent_x + gap + w / 2.0
        mx, my = pad.x + ux * d, pad.y + uy * d
        px, py = -uy, ux  # perpendicular
        net_seg = self.next_net()
        self.segments.append(
            SegmentSpec(
                net=net_seg,
                start=_round_pt(
                    (mx - px * _PAD_PROBE_HALF_LEN_MM, my - py * _PAD_PROBE_HALF_LEN_MM)
                ),
                end=_round_pt((mx + px * _PAD_PROBE_HALF_LEN_MM, my + py * _PAD_PROBE_HALF_LEN_MM)),
                width=w,
                layer="F.Cu",
            )
        )
        self._record(PairKind.PAD_SEG, pad.net, net_seg, gap, required)

    def add_pad_via(self, centre: tuple[float, float], layer: str) -> None:
        del layer
        required = self.rules.project_clearance
        gap = _draw_gap(self.rng, required)
        pad = self._place_pad(centre)
        ux, uy = pad.local_x_axis()
        d = pad.shape.half_extent_x + gap + _VIA_DIAMETER_MM / 2.0
        net_via = self.next_net()
        self.vias.append(
            ViaSpec(
                net=net_via,
                x=round(pad.x + ux * d, 6),
                y=round(pad.y + uy * d, 6),
                diameter=_VIA_DIAMETER_MM,
                drill=_VIA_DRILL_MM,
            )
        )
        self._record(PairKind.PAD_VIA, pad.net, net_via, gap, required)

    def add_pad_pad(self, centre: tuple[float, float], layer: str) -> None:
        """Two pads facing each other at an exactly-known copper gap.

        The only pair kind whose *both* objects are static placement copper.
        It exists for Epic #5509 group 19 (``drc/cpp_backend.py``
        ``check_pair_clearance_cpp``), whose entry point takes two whole
        footprints and therefore cannot be measured on any other kind.

        **Both pads are rotated, but anti-parallel.**  The gap is realised
        along the first pad's local ``+X`` axis, where every shape in
        :data:`PAD_SHAPES` has support ``w / 2`` (see
        :attr:`PadShape.half_extent_x`).  Giving the second pad
        ``rotation + 180`` points *its* local ``+X`` back along the same axis,
        so its support towards the first pad is ``w / 2`` as well and the
        edge-to-edge gap is exactly ``gap`` -- for any drawn rotation.  Both
        facing extremities are centred on that axis, so no corner of either
        pad comes closer than its own mid-edge / apex does.
        """
        del layer  # pads are on F.Cu
        required = self.rules.project_clearance
        gap = _draw_gap(self.rng, required)
        pad_a = self._place_pad(centre)
        ux, uy = pad_a.local_x_axis()
        shape_b = self.rng.choice(PAD_SHAPES)
        d = pad_a.shape.half_extent_x + gap + shape_b.half_extent_x
        pad_b = PadSpec(
            net=self.next_net(),
            reference=self.next_pad_ref(),
            footprint=shape_b.name,
            x=round(pad_a.x + ux * d, 6),
            y=round(pad_a.y + uy * d, 6),
            rotation=round((pad_a.rotation + 180.0) % 360.0, 3),
        )
        self.pads.append(pad_b)
        self._record(PairKind.PAD_PAD, pad_a.net, pad_b.net, gap, required)

    # -- zone and edge placement (#5644) -------------------------------------

    def _zone_probe_y(self, gap: float, half_extent: float) -> float:
        """Centre ``y`` for a probe sitting ``gap`` mm above the pour boundary.

        The pour is an axis-aligned rectangle inset from the outline, so its
        nearest copper to anything in the strip above it is its top edge.  A
        probe whose copper reaches ``half_extent`` towards that edge therefore
        realises a gap of exactly ``gap`` -- a closed-form statement, asserted
        by ``test_generator.test_zone_pairs_realise_their_intended_gap_exactly``.
        """
        assert self.zone is not None
        top = min(y for _, y in self.zone.boundary)
        return round(top - gap - half_extent, 6)

    def add_seg_zone(self, centre: tuple[float, float], layer: str) -> None:
        """A track parallel to the pour boundary, at a known gap above it.

        ``layer`` is ignored and the pour's own layer used instead: KiCad's
        clearance is per-layer, so a probe on any other layer would record an
        intended gap against copper it can never interact with.
        """
        del layer  # the pour's layer is the only one where this pair is live
        assert self.zone is not None
        cx, _ = centre
        required = self.rules.project_clearance
        gap = _draw_clear_gap(self.rng, required)
        w = self.rules.min_trace_width
        y = self._zone_probe_y(gap, w / 2.0)
        net = self.next_net()
        self.segments.append(
            SegmentSpec(
                net=net,
                start=_round_pt((cx - _SEG_HALF_LEN_MM, y)),
                end=_round_pt((cx + _SEG_HALF_LEN_MM, y)),
                width=w,
                layer=self.zone.layer,
            )
        )
        self._record(PairKind.SEG_ZONE, net, self.zone.net, gap, required)

    def add_via_zone(self, centre: tuple[float, float], layer: str) -> None:
        """A through via at a known gap above the pour boundary.

        A through via reaches every copper layer, so unlike ``seg-zone`` this
        pair is live whatever layer the pour is on.
        """
        del layer  # through vias span all layers
        assert self.zone is not None
        cx, _ = centre
        required = self.rules.project_clearance
        gap = _draw_clear_gap(self.rng, required)
        y = self._zone_probe_y(gap, _VIA_DIAMETER_MM / 2.0)
        net = self.next_net()
        self.vias.append(
            ViaSpec(
                net=net,
                x=round(cx, 6),
                y=y,
                diameter=_VIA_DIAMETER_MM,
                drill=_VIA_DRILL_MM,
            )
        )
        self._record(PairKind.VIA_ZONE, net, self.zone.net, gap, required)

    def add_copper_edge(self, centre: tuple[float, float], layer: str) -> None:
        """A track parallel to the board's top edge, at a known gap from it.

        The partner is the :data:`~tests.conformance.adapters.BOARD_EDGE`
        pseudo-net rather than a second copper object, matching how the oracle
        keys a one-sided ``copper_edge_clearance`` row.  The requirement is
        ``min_copper_to_edge`` (the ``.kicad_pro`` board rule kicad-cli
        applies), not the netclass clearance every other kind is drawn against.

        A **segment**, deliberately, not a via: the candidate is then something
        every segment-candidate consumer can be asked about (group 10's
        ``is_clear`` takes two points and no width), and a via probe would
        additionally engage KiCad's hole-to-edge term and measure two rules at
        once.
        """
        cx, _ = centre
        required = self.rules.min_copper_to_edge
        gap = _draw_gap(self.rng, required)
        w = self.rules.min_trace_width
        # The board's top edge is ``y = 0`` (boards are written ``center=False``),
        # so copper reaching ``y = gap`` realises exactly that edge gap.
        y = round(gap + w / 2.0, 6)
        net = self.next_net()
        self.segments.append(
            SegmentSpec(
                net=net,
                start=_round_pt((cx - _EDGE_PROBE_HALF_LEN_MM, y)),
                end=_round_pt((cx + _EDGE_PROBE_HALF_LEN_MM, y)),
                width=w,
                layer=layer,
            )
        )
        self._record(PairKind.COPPER_EDGE, net, BOARD_EDGE, gap, required)


# ``kind -> (builder method, segments, vias, pads)``.  The three deltas are the
# object-count budget the kind consumes, which is what keeps every generated
# case inside the phase spec's envelope.
_PAIR_PLACERS = {
    PairKind.SEG_SEG: ("add_seg_seg", 2, 0, 0),
    PairKind.SEG_VIA: ("add_seg_via", 1, 1, 0),
    PairKind.VIA_VIA: ("add_via_via", 0, 2, 0),
    PairKind.PAD_SEG: ("add_pad_seg", 1, 0, 1),
    PairKind.PAD_VIA: ("add_pad_via", 0, 1, 1),
    PairKind.PAD_PAD: ("add_pad_pad", 0, 0, 2),
    PairKind.SEG_ZONE: ("add_seg_zone", 1, 0, 0),
    PairKind.VIA_ZONE: ("add_via_zone", 0, 1, 0),
    PairKind.COPPER_EDGE: ("add_copper_edge", 1, 0, 0),
}

# Object-count envelope from the phase spec: 2-6 segments, 1-3 vias, 1-2 pads.
_MAX_SEGMENTS = 6
_MAX_VIAS = 3
_MAX_PADS = 2


def _choose_pair_kinds(rng: random.Random, *, has_zone: bool) -> list[str]:
    """Pick pair kinds that respect the phase spec's object-count envelope.

    The first two picks are constrained so every case has at least two
    segments and at least one via (the spec's floor); the rest are free
    subject to the remaining budget.

    ``has_zone`` gates the two zone kinds, and is decided *before* this call
    rather than after it: a ``seg-zone`` pair with no pour to measure against
    would be an intent the board cannot realise.

    The free-pick count is ``1..3`` rather than ``0..2``.  With nine kinds to
    draw from -- and the two zone kinds only drawable on the ~half of cases
    that carry a pour -- the old range left the #5644 kinds with single-digit
    denominators over the published 200-seed corpus, which is a percentage
    nobody should read.  One extra expected pick per case is the cheapest fix
    that does not privilege the new kinds over the old ones: it costs no extra
    kicad-cli invocation (the case count is unchanged) and stays inside both
    the object-count envelope and the slot lattice's first row.
    """
    kinds: list[str] = [PairKind.SEG_VIA]  # guarantees >= 1 segment, >= 1 via
    kinds.append(rng.choice([PairKind.SEG_SEG, PairKind.PAD_SEG]))  # >= 2 segments
    segs = sum(_PAIR_PLACERS[k][1] for k in kinds)
    vias = sum(_PAIR_PLACERS[k][2] for k in kinds)
    pads = sum(_PAIR_PLACERS[k][3] for k in kinds)

    for _ in range(rng.randint(1, 3)):
        feasible = [
            k
            for k, (_, ds, dv, dp) in _PAIR_PLACERS.items()
            if segs + ds <= _MAX_SEGMENTS
            and vias + dv <= _MAX_VIAS
            and pads + dp <= _MAX_PADS
            and (has_zone or k not in PairKind.ZONE)
        ]
        if not feasible:
            break
        choice = rng.choice(sorted(feasible))
        kinds.append(choice)
        _, ds, dv, dp = _PAIR_PLACERS[choice]
        segs, vias, pads = segs + ds, vias + dv, pads + dp
    return kinds


def _make_zone(net: str, rules: CaseRules) -> ZoneSpec:
    """The case's optional pour: one board-spanning rectangle on ``B.Cu``.

    Inset by :data:`_ZONE_INSET_MM`, which leaves a strip between the outline
    and the pour wide enough to hold a zone probe at an analytic gap without
    that probe coming anywhere near the board edge (the strip is 2.5 mm and the
    edge requirement is 0.30 mm).
    """
    inset = _ZONE_INSET_MM
    return ZoneSpec(
        net=net,
        layer="B.Cu",
        boundary=(
            (inset, inset),
            (_BOARD_WIDTH_MM - inset, inset),
            (_BOARD_WIDTH_MM - inset, _BOARD_HEIGHT_MM - inset),
            (inset, _BOARD_HEIGHT_MM - inset),
        ),
        clearance=rules.project_clearance,
    )


def generate_case(seed: int, rules: CaseRules | None = None) -> CopperCase:
    """Generate one deterministic copper case.

    Args:
        seed: RNG seed; identical seeds always produce identical cases.
        rules: Rule values under test (defaults to :data:`DEFAULT_RULES`).

    Returns:
        A :class:`CopperCase` whose every close pair sits at a known,
        analytically placed copper gap.
    """
    rules = rules or DEFAULT_RULES
    rng = random.Random(seed)
    builder = _CaseBuilder(rng, rules)

    layers = 4 if rng.random() < 0.5 else 2
    signal_layers = ["F.Cu", "B.Cu"] if layers == 2 else ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    # The pour is decided, and built, *before* the pairs: a zone pair needs a
    # pour boundary to place itself against, and ``_choose_pair_kinds`` needs
    # to know whether the zone kinds are available at all.  It still exercises
    # the refill path on every case that carries one, whether or not a zone
    # pair was drawn -- KiCad's filler knocks the pour back around every other
    # foreign object it meets, so those objects gain no uncontrolled near pair.
    zone: ZoneSpec | None = _make_zone(builder.next_net(), rules) if rng.random() < 0.5 else None
    builder.zone = zone

    kinds = _choose_pair_kinds(rng, has_zone=zone is not None)
    # Every pair takes its own slot *column*, which is what keeps the zone and
    # edge probes -- which use the slot's x but their own y -- from ever
    # sharing a column with each other.
    assert len(kinds) <= _SLOT_COLS, f"{len(kinds)} pairs exceeds the lattice's first row"
    for slot, kind in enumerate(kinds):
        method, _, _, _ = _PAIR_PLACERS[kind]
        layer = rng.choice(signal_layers)
        getattr(builder, method)(_slot_centre(slot), layer)

    return CopperCase(
        seed=seed,
        name=f"corpus-seed{seed}",
        width=_BOARD_WIDTH_MM,
        height=_BOARD_HEIGHT_MM,
        layers=layers,
        rules=rules,
        segments=tuple(builder.segments),
        vias=tuple(builder.vias),
        pads=tuple(builder.pads),
        zone=zone,
        pairs=tuple(builder.pairs),
        notes=f"seeded corpus case ({len(kinds)} analytic pairs)",
    )


def generate_corpus(seeds: range | list[int], rules: CaseRules | None = None) -> list[CopperCase]:
    """Generate one case per seed, in seed order."""
    return [generate_case(seed, rules) for seed in seeds]


def parse_seed_range(spec: str) -> range:
    """Parse ``"0-199"`` / ``"7"`` into a ``range`` (upper bound inclusive)."""
    spec = spec.strip()
    if "-" in spec:
        lo_s, hi_s = spec.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
    else:
        lo = hi = int(spec)
    if hi < lo:
        raise ValueError(f"empty seed range {spec!r}")
    return range(lo, hi + 1)
