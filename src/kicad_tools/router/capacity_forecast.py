"""Pre-route pad-field escape-capacity forecast (issue #4799).

A **genuinely pre-route** capacity/solvability predictor: it reads placement
and design rules only, runs before the first A\\* expansion, and answers one
counting question per dense pad field --

    can every pin that has to leave this footprint physically get out,
    given the channels the pad geometry leaves and the layers the stack
    offers?

Why this and not the crossing-tail census
-----------------------------------------

The earlier slices of #4799 (#4852 report, #4862 replay, #4865 gate) made the
diff-pair *crossing-tail* census a leading indicator, but that measurement is
order-dependent (it consults drills placed by earlier crossovers and an
escape-channel registry keyed on which nets are still unrouted), so it can only
be replayed from a **previous run** -- and it only exists on boards that route
coupled differential pairs through the shadow constructor.  This model has
neither limitation: ring depth, inter-pad gaps and pin counts are properties of
the placement, so the forecast is exact on the first run of a brand-new board
and applies to any board with a multi-ring pad field.

Why the pad field and not the whole board (fleet run, 2026-08-15)
-----------------------------------------------------------------

A **board-scale** estimate is not the binding constraint on real designs here.
Feeding the in-repo fleet through the existing
:class:`~kicad_tools.router.congestion_estimator.CongestionEstimator` (RUDY) and
dividing per-tile demand by a per-tile track supply gives 1.2-3.5% global
utilisation and a peak tile of 0.24x on **every** board, including the two with
open router epics (#4409, #3438).  A predictor built on that would call every
board healthy, which is worth exactly nothing.  Escape geometry is local, so
this model is local.

The same fleet run through *this* model finds two multi-ring pad fields -- the
7x7 / 1.27 mm arrays on boards 06 and 07 -- and reports both as ``ample``
(0.08x and 0.05x): a 0.6 mm via fits their diagonal interstice, so the interior
reaches the inner layers.  That is the honest reading, and it is the one the
model must give: those two boards fail for reasons this counting model does not
claim to see.  Its positive controls are the counterfactuals it is built for --
the same array on a stack with no legal via drop is 1.04x **over capacity**
(25 interior pins, 24 single-track gaps), and a 0.5 mm-pitch array is
``infeasible`` outright (no track fits between pads and no via fits between
them either, so nothing gets out on any layer).

The model (ring-cut counting)
-----------------------------

For each footprint with at least :data:`MIN_PADS_FOR_FIELD` pads:

1. **Ring depth** by onion peel.  A pad is on the boundary (depth 0) when it is
   missing a near neighbour in one of the four cardinal directions; peel the
   boundary away and repeat for depth 1, 2, ...  This is shape-agnostic, which
   matters: a two-row connector (USB-C) is *all* boundary and must not be
   modelled as an array, while a 7x7 grid peels into four rings.
2. **Channel width.**  ``gap`` is the smallest clear spacing between
   neighbouring pads; a channel of clear width *g* carries
   ``floor((g - clearance) / (track + clearance))`` tracks
   (:func:`channels_in_gap`).
3. **Via drop.**  An interior pin reaches another layer only if a barrel can be
   placed: in the pad (a fab-tier capability) or at an *interstitial* site --
   for a grid array the diagonal gap between four pads, which is ``sqrt(2)``
   further from every pad centre than the orthogonal one
   (:func:`via_site_clearance`).  Modelling this on the orthogonal gap is the
   difference between "this BGA cannot reach an inner layer" and the dogbone
   every BGA in the world uses.
4. **Ring cuts.**  Every pin at depth >= *d* must cross the ring at depth
   *d - 1* to leave the part.  Demand is those pins; supply is the number of
   gaps in that ring times the channels each gap carries, on the pad's own
   layer plus -- only when a via drop exists -- the other signal layers, whose
   channels are narrowed by the via barrels rather than the pads.

The worst cut's ``demand / supply`` is the field's utilisation, and the worst
field is the board's.  Anything at or above 1.0 is a pin count that does not
fit through the geometry, i.e. a *solvability* statement rather than a
congestion guess.

**Advisory.**  Nothing here changes routing or an exit code.  ``kct route
--capacity-forecast`` prints the block before any router or component loading
and returns 0 whatever it finds; a crash inside the predictor degrades to a
one-line diagnostic (the ``_offboard_preflight`` precedent).  Wiring it into a
go/no-go the way #4865 did for the census is deliberate follow-up work.

Known biases (both directions, deliberately not tuned away)
-----------------------------------------------------------

* Pad extent is approximated by ``min(width, height)``, so the gap between
  oblong pads is *under*-stated in one axis -- pessimistic.
* Supply assumes every gap in a ring is usable, ignoring the neck-down rules,
  keepouts and teardrops that consume some of them -- optimistic.
* Escaping pins are counted as pads, not nets: two pads of one net inside a
  field could in principle share a single exit -- pessimistic, rarely by much.
* Pins on a poured net are deferred (they drop straight to their plane) only
  where a via drop exists; the barrels they consume at interstitial sites are
  not charged against the drop-layer channels -- optimistic.
* Nothing outside the footprint is modelled.  A field that escapes cleanly can
  still fail in the corridor beyond it (board-05's sense band), so ``ample`` is
  *not* a routability guarantee.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "MAX_PADS_FOR_VIA_SITE_SEARCH",
    "MIN_PADS_FOR_FIELD",
    "NEIGHBOUR_REACH_FACTOR",
    "OVER_CAPACITY_RATIO",
    "REPORT_KIND",
    "SCHEMA_VERSION",
    "TIGHT_RATIO",
    "VERDICT_AMPLE",
    "VERDICT_INFEASIBLE",
    "VERDICT_NOT_APPLICABLE",
    "VERDICT_OVER_CAPACITY",
    "VERDICT_TIGHT",
    "WORST_FIELDS_SHOWN",
    "CapacityForecast",
    "CapacityForecastError",
    "FieldPad",
    "PadFieldForecast",
    "RingCut",
    "build_forecast",
    "channels_in_gap",
    "emit_forecast",
    "forecast_from_board",
    "load_field_pads",
    "pour_nets_for_board",
    "ring_depths",
    "via_site_clearance",
    "write_report",
    "zone_net_names",
]

#: Bump only on a breaking change to the document (additions are free).
SCHEMA_VERSION = 1

#: ``report`` discriminator in the JSON envelope.
REPORT_KIND = "capacity-forecast"

#: Footprints with fewer pads than this are never modelled as pad fields.
#: Discretes, two-pin connectors and small SOT packages have no interior by
#: construction, and running the peel on them only adds noise.
MIN_PADS_FOR_FIELD = 5

#: Two pads are neighbours when their centres are within this multiple of the
#: field's nearest-neighbour pitch.  1.6 admits the orthogonal neighbours of a
#: square grid (1.0 pitch) and its diagonals (1.41) while excluding the next
#: row out (2.0).
NEIGHBOUR_REACH_FACTOR = 1.6

#: Utilisation at or above which a ring cut is over capacity: the pins that
#: must cross it outnumber the channels crossing it.
OVER_CAPACITY_RATIO = 1.0

#: Utilisation at or above which a field is called ``tight``.  Advisory and
#: **uncalibrated** -- 0.8 leaves one channel in five for the detours, guard
#: traces and teardrops the counting model does not see.
TIGHT_RATIO = 0.8

#: How many fields the human block names before eliding.
WORST_FIELDS_SHOWN = 5

#: Above this pad count the interstitial via-site search
#: (:func:`via_site_clearance`, O(pads * neighbour pairs)) is skipped and the
#: orthogonal gap is used instead -- pessimistic, but bounded.  Nothing in the
#: in-repo fleet comes close; a 1000-ball BGA would.
MAX_PADS_FOR_VIA_SITE_SEARCH = 400

#: Slack on "does this fit" comparisons.  Board dimensions are authored in mm
#: at four decimal places, so 1e-9 only absorbs float representation error
#: (0.6 - 0.2 == 0.39999999999999997), never a real 0.0001 mm difference --
#: the same reasoning as ``route_cmd._TRACE_WIDTH_EPS``.
_FIT_EPS = 1e-9

VERDICT_NOT_APPLICABLE = "not-applicable"
VERDICT_AMPLE = "ample"
VERDICT_TIGHT = "tight"
VERDICT_OVER_CAPACITY = "over-capacity"
VERDICT_INFEASIBLE = "infeasible"

#: Worst-first ordering of the verdicts, used to roll fields up to a board.
_VERDICT_RANK = {
    VERDICT_NOT_APPLICABLE: 0,
    VERDICT_AMPLE: 1,
    VERDICT_TIGHT: 2,
    VERDICT_OVER_CAPACITY: 3,
    VERDICT_INFEASIBLE: 4,
}


class CapacityForecastError(ValueError):
    """The forecast could not be built from the inputs given."""


@dataclass(frozen=True)
class FieldPad:
    """One pad, in board coordinates -- the model's whole view of a footprint.

    Deliberately minimal and loader-agnostic: :func:`build_forecast` accepts
    anything with these attributes, so a caller that already has pads (a board
    script, a test fabricating a synthetic array) never has to round-trip
    through a file.
    """

    x: float
    y: float
    width: float
    height: float
    ref: str
    net_name: str = ""
    footprint_name: str = ""


def _pad_net_name(pad: Any) -> str:
    """Net name of *pad*, tolerating the two shapes in this codebase.

    ``FieldPad`` and ``router.io``'s ``Pad`` both carry ``net_name``; the
    latter also carries a numeric ``net``, which is used as the key when the
    name is empty so an unnamed-but-numbered net still groups correctly.
    """
    name = str(getattr(pad, "net_name", "") or "")
    if name:
        return name
    net = getattr(pad, "net", None)
    if isinstance(net, str):
        return net
    if isinstance(net, int) and net:
        return f"#{net}"
    return ""


def channels_in_gap(gap_mm: float, track_width_mm: float, clearance_mm: float) -> int:
    """How many tracks fit through a clear channel *gap_mm* wide.

    ``n`` tracks need ``n * track + (n + 1) * clearance`` of clear width (a
    clearance either side and between each pair), so
    ``n = floor((gap - clearance) / (track + clearance))``, floored at 0.

    Dimensions are authored in mm at a few decimal places, so an exactly-fitting
    channel (0.60 mm for one 0.2/0.2 track) must not be lost to binary
    representation error -- hence :data:`_FIT_EPS`.
    """
    pitch = track_width_mm + clearance_mm
    if pitch <= 0 or gap_mm <= clearance_mm:
        return 0
    return max(0, int((gap_mm - clearance_mm) / pitch + _FIT_EPS))


def ring_depths(points: Sequence[tuple[float, float]], pitch_mm: float) -> list[int]:
    """Onion-peel ring depth for each point in *points*.

    Depth 0 is the boundary of the field: a point missing a near neighbour in
    any of the four cardinal directions.  Peeling the boundary away and
    repeating gives depth 1, 2, ...  The classification is shape-agnostic on
    purpose -- a two-row connector peels to a single ring (every pad has open
    sky on one side) while a square grid peels to ``ceil(n / 2)`` rings -- and
    a bbox-inset rule gets both of those wrong.

    Returns a list parallel to *points*.  An empty input returns ``[]``; a
    non-positive pitch puts everything on the boundary.
    """
    n = len(points)
    depths = [0] * n
    if n == 0:
        return depths
    if pitch_mm <= 0:
        return depths
    reach = NEIGHBOUR_REACH_FACTOR * pitch_mm
    lateral = 0.5 * pitch_mm
    alive = set(range(n))
    depth = 0
    while alive:
        boundary: set[int] = set()
        for i in alive:
            xi, yi = points[i]
            east = west = south = north = False
            for j in alive:
                if i == j:
                    continue
                dx = points[j][0] - xi
                dy = points[j][1] - yi
                if math.hypot(dx, dy) > reach:
                    continue
                if dx > lateral:
                    east = True
                elif dx < -lateral:
                    west = True
                if dy > lateral:
                    south = True
                elif dy < -lateral:
                    north = True
            if not (east and west and south and north):
                boundary.add(i)
        if not boundary:
            # Fully enclosed remainder (a ring with no open direction, e.g. a
            # 2x2 core): stop rather than loop forever, and call it one ring.
            boundary = set(alive)
        for i in boundary:
            depths[i] = depth
        alive -= boundary
        depth += 1
    return depths


@dataclass(frozen=True)
class RingCut:
    """The boundary a pin at depth >= :attr:`depth` has to cross to get out."""

    depth: int
    #: Pads on the ring immediately outside the cut -- one channel per gap
    #: between consecutive pads, so the pad count is the gap count.
    gaps: int
    #: Channels each gap carries, summed over the layers reachable here.
    channels_per_gap: int
    #: Pads at depth >= :attr:`depth` whose net leaves the footprint.
    demand: int

    @property
    def supply(self) -> int:
        return self.gaps * self.channels_per_gap

    @property
    def ratio(self) -> float:
        """``demand / supply``; ``inf`` when demand exists with no supply."""
        if self.supply > 0:
            return self.demand / self.supply
        return math.inf if self.demand > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        ratio = self.ratio
        return {
            "depth": self.depth,
            "gaps": self.gaps,
            "channels_per_gap": self.channels_per_gap,
            "supply": self.supply,
            "demand": self.demand,
            "ratio": None if math.isinf(ratio) else round(ratio, 3),
        }


@dataclass(frozen=True)
class PadFieldForecast:
    """One footprint's escape-capacity forecast."""

    ref: str
    footprint: str
    pads: int
    escaping_pads: int
    #: Pads left out of demand because they sit on a poured net **and** a via
    #: drop is available, so they reach their plane without crossing a ring.
    pour_pads_deferred: int
    pitch_mm: float
    gap_mm: float
    rings: int
    surface_channels_per_gap: int
    drop_channels_per_gap: int
    via_drop_available: bool
    cuts: tuple[RingCut, ...]

    @property
    def worst_cut(self) -> RingCut | None:
        """The cut with the highest utilisation, or ``None`` for a flat field."""
        if not self.cuts:
            return None
        return max(self.cuts, key=lambda c: (c.ratio, -c.depth))

    @property
    def worst_ratio(self) -> float:
        cut = self.worst_cut
        return cut.ratio if cut is not None else 0.0

    @property
    def verdict(self) -> str:
        cut = self.worst_cut
        if cut is None:
            return VERDICT_NOT_APPLICABLE
        if math.isinf(cut.ratio):
            return VERDICT_INFEASIBLE
        if cut.ratio >= OVER_CAPACITY_RATIO:
            return VERDICT_OVER_CAPACITY
        if cut.ratio >= TIGHT_RATIO:
            return VERDICT_TIGHT
        return VERDICT_AMPLE

    def to_dict(self) -> dict[str, Any]:
        ratio = self.worst_ratio
        cut = self.worst_cut
        return {
            "ref": self.ref,
            "footprint": self.footprint,
            "pads": self.pads,
            "escaping_pads": self.escaping_pads,
            "pour_pads_deferred": self.pour_pads_deferred,
            "pitch_mm": round(self.pitch_mm, 4),
            "gap_mm": round(self.gap_mm, 4),
            "rings": self.rings,
            "surface_channels_per_gap": self.surface_channels_per_gap,
            "drop_channels_per_gap": self.drop_channels_per_gap,
            "via_drop_available": self.via_drop_available,
            "worst_depth": cut.depth if cut is not None else None,
            "worst_ratio": None if math.isinf(ratio) else round(ratio, 3),
            "verdict": self.verdict,
            "cuts": [c.to_dict() for c in self.cuts],
        }

    def format_human(self, tag: str) -> str:
        cut = self.worst_cut
        if cut is None:  # pragma: no cover - fields with no cut are not listed
            return f"{tag}   {self.ref}: single ring, no interior pins"
        ratio = "no channel" if math.isinf(cut.ratio) else f"{cut.ratio:.2f}x"
        deferred = (
            f", {self.pour_pads_deferred} poured pin(s) dropped straight down"
            if self.pour_pads_deferred
            else ""
        )
        return (
            f"{tag}   {self.ref} ({self.footprint or 'unknown footprint'}): "
            f"{self.pads} pads / {self.rings} rings, pitch {self.pitch_mm:.3f} mm, "
            f"gap {self.gap_mm:.3f} mm -> {ratio} at ring cut {cut.depth} "
            f"({cut.demand} pin(s) must cross {cut.supply} channel(s){deferred}) "
            f"[{self.verdict}]"
        )


@dataclass(frozen=True)
class CapacityForecast:
    """Board-level roll-up of every modelled pad field."""

    source: str
    signal_layers: int
    track_width_mm: float
    clearance_mm: float
    via_diameter_mm: float
    via_in_pad: bool
    footprints_seen: int
    fields_modelled: int
    fields: tuple[PadFieldForecast, ...]
    warnings: tuple[str, ...] = ()

    @property
    def applicable(self) -> bool:
        """Did any footprint have an interior to model?

        ``False`` is the honest answer for a board of discretes and
        perimeter-pin packages -- distinct from ``ample``, which would claim
        the model looked at a pad field and liked it.
        """
        return bool(self.fields)

    @property
    def ranked_fields(self) -> tuple[PadFieldForecast, ...]:
        """Fields worst-first (utilisation, then pin count, then ref)."""
        return tuple(
            sorted(
                self.fields,
                key=lambda f: (
                    -(f.worst_ratio if not math.isinf(f.worst_ratio) else 1e9),
                    -f.escaping_pads,
                    f.ref,
                ),
            )
        )

    @property
    def worst_field(self) -> PadFieldForecast | None:
        ranked = self.ranked_fields
        return ranked[0] if ranked else None

    @property
    def worst_ratio(self) -> float:
        field = self.worst_field
        return field.worst_ratio if field is not None else 0.0

    @property
    def verdict(self) -> str:
        field = self.worst_field
        if field is None:
            return VERDICT_NOT_APPLICABLE
        return field.verdict

    def to_dict(self) -> dict[str, Any]:
        worst = self.worst_field
        ratio = self.worst_ratio
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "report": REPORT_KIND,
            "source": self.source,
            "rules": {
                "signal_layers": self.signal_layers,
                "track_width_mm": self.track_width_mm,
                "clearance_mm": self.clearance_mm,
                "via_diameter_mm": self.via_diameter_mm,
                "via_in_pad": self.via_in_pad,
            },
            "summary": {
                "applicable": self.applicable,
                "footprints_seen": self.footprints_seen,
                "fields_modelled": self.fields_modelled,
                "worst_ref": worst.ref if worst is not None else None,
                "worst_ratio": None if math.isinf(ratio) else round(ratio, 3),
                "verdict": self.verdict,
                "over_capacity_ratio": OVER_CAPACITY_RATIO,
                "tight_ratio": TIGHT_RATIO,
            },
            "fields": [f.to_dict() for f in self.ranked_fields],
            "warnings": list(self.warnings),
        }

    def format_human(self) -> str:
        """Multi-line, greppable text form, every line tagged for interleaving."""
        tag = "[capacity-forecast]"
        head = f"{tag} pre-route escape-capacity forecast for {self.source} (placement-only, #4799)"
        rules = (
            f"{tag}   rules: {self.signal_layers} signal layer(s), track "
            f"{self.track_width_mm:.3f} mm, clearance {self.clearance_mm:.3f} mm, "
            f"via {self.via_diameter_mm:.3f} mm, via-in-pad "
            f"{'yes' if self.via_in_pad else 'no'}"
        )
        if not self.applicable:
            return "\n".join(
                [
                    head,
                    rules,
                    f"{tag}   {self.footprints_seen} footprint(s) examined, 0 with an "
                    f"interior ring -- NOT APPLICABLE",
                    f"{tag}   every pad field on this board is a single ring (perimeter "
                    f"pins escape outward directly); this is not an 'ample' result",
                    *[f"{tag}   WARNING: {w}" for w in self.warnings],
                    f"{tag}   ADVISORY ONLY -- this route is unchanged by the above (#4799)",
                ]
            )
        lines = [
            head,
            rules,
            f"{tag}   {self.fields_modelled} multi-ring pad field(s) of "
            f"{self.footprints_seen} footprint(s), verdict={self.verdict}",
        ]
        ranked = self.ranked_fields
        for field in ranked[:WORST_FIELDS_SHOWN]:
            lines.append(field.format_human(tag))
        if len(ranked) > WORST_FIELDS_SHOWN:
            lines.append(f"{tag}   (+{len(ranked) - WORST_FIELDS_SHOWN} more field(s))")
        worst = self.worst_field
        if worst is not None and self.verdict in (
            VERDICT_OVER_CAPACITY,
            VERDICT_INFEASIBLE,
            VERDICT_TIGHT,
        ):
            lines.append(f"{tag}   reading: {self._reading(worst)}")
            lines.append(f"{tag}   FIX LAYER: {self._fix_layer(worst)}")
        lines.extend(f"{tag}   WARNING: {w}" for w in self.warnings)
        lines.append(f"{tag}   ADVISORY ONLY -- this route is unchanged by the above (#4799)")
        return "\n".join(lines)

    def _reading(self, worst: PadFieldForecast) -> str:
        if self.verdict == VERDICT_INFEASIBLE:
            return (
                f"{worst.ref}'s interior pins have NO channel out: the "
                f"{worst.gap_mm:.3f} mm gap between pads carries 0 tracks at this "
                f"track/clearance and no via can be dropped, so those pins cannot "
                f"escape on any layer -- no router setting can fix this"
            )
        if self.verdict == VERDICT_OVER_CAPACITY:
            return (
                f"{worst.ref} has more interior pins than channels to carry them; the "
                f"router will spend its budget and leave some of them unrouted "
                f"whatever the ordering, because the shortfall is geometric"
            )
        return (
            f"{worst.ref} clears its ring cut with little margin -- the counting model "
            f"ignores neck-down, keepouts and teardrops, so expect the real escape to "
            f"be harder than this ratio"
        )

    def _fix_layer(self, worst: PadFieldForecast) -> str:
        parts = []
        if not worst.via_drop_available and self.signal_layers > 1:
            parts.append(
                "make a via drop possible (a fab tier with via-in-pad, or a via small "
                "enough for the interstice between pads) so the inner layers become "
                "reachable"
            )
        if self.signal_layers < 4:
            parts.append("add signal layers")
        parts.append("widen the pad field's pitch or shrink track/clearance")
        parts.append("move pins so fewer interior nets have to leave the part")
        return "; ".join(parts) + " -- placement / stackup, not the router"


def _pad_extent(pad: Any) -> float:
    """Conservative pad extent: the smaller of the two footprint dimensions."""
    width = float(getattr(pad, "width", 0.0) or 0.0)
    height = float(getattr(pad, "height", 0.0) or 0.0)
    if width <= 0 and height <= 0:
        return 0.0
    if width <= 0:
        return height
    if height <= 0:
        return width
    return min(width, height)


def _pad_radius(pad: Any) -> float:
    """Circumscribed pad radius -- half the diagonal of its bounding box."""
    width = float(getattr(pad, "width", 0.0) or 0.0)
    height = float(getattr(pad, "height", 0.0) or 0.0)
    return 0.5 * math.hypot(width, height)


def _field_geometry(pads: Sequence[Any]) -> tuple[float, float]:
    """Nearest-neighbour pitch and the smallest clear gap in a pad field."""
    pitch = math.inf
    gap = math.inf
    for i, a in enumerate(pads):
        for b in pads[i + 1 :]:
            dist = math.hypot(a.x - b.x, a.y - b.y)
            if dist <= 0:
                continue
            if dist < pitch:
                pitch = dist
            clear = dist - 0.5 * (_pad_extent(a) + _pad_extent(b))
            if clear < gap:
                gap = clear
    if not math.isfinite(pitch):
        return (0.0, 0.0)
    return (pitch, max(0.0, gap if math.isfinite(gap) else 0.0))


def via_site_clearance(pads: Sequence[Any], pitch_mm: float) -> float:
    """Largest clear radius available to a dogbone via inside a pad field.

    A dogbone drop does **not** go through the orthogonal gap between two
    adjacent pads -- it goes into the interstice between four of them, which on
    a square grid is ``sqrt(2)`` further from every pad centre.  Modelling the
    drop on the orthogonal gap is the difference between "this BGA cannot reach
    an inner layer" and "of course it can", so the site is searched for
    explicitly: every midpoint between two neighbouring pads is a candidate,
    scored by its distance to the nearest pad copper (pads circumscribed by
    :func:`_pad_radius`, i.e. conservatively round).

    Returns the best candidate's clear radius in mm, ``0.0`` when there is no
    room anywhere.  Fields larger than :data:`MAX_PADS_FOR_VIA_SITE_SEARCH`
    skip the search and fall back to the orthogonal gap (see
    :func:`build_forecast`), keeping the forecast's cost bounded.
    """
    if pitch_mm <= 0 or len(pads) < 2:
        return 0.0
    reach = NEIGHBOUR_REACH_FACTOR * pitch_mm
    scan = 2.0 * pitch_mm
    best = 0.0
    for i, a in enumerate(pads):
        for b in pads[i + 1 :]:
            if math.hypot(a.x - b.x, a.y - b.y) > reach:
                continue
            mx = 0.5 * (a.x + b.x)
            my = 0.5 * (a.y + b.y)
            clear = math.inf
            for pad in pads:
                dist = math.hypot(pad.x - mx, pad.y - my)
                if dist > scan:
                    continue
                clear = min(clear, dist - _pad_radius(pad))
                if clear <= best:
                    break
            if math.isfinite(clear) and clear > best:
                best = clear
    return max(0.0, best)


def build_forecast(
    pads: Iterable[Any],
    *,
    source: str = "",
    signal_layers: int = 2,
    track_width_mm: float = 0.2,
    clearance_mm: float = 0.2,
    via_diameter_mm: float = 0.6,
    via_in_pad: bool = False,
    pour_nets: Iterable[str] = (),
    warnings: Sequence[str] = (),
) -> CapacityForecast:
    """Model every multi-ring pad field among *pads*.

    *pads* is any iterable of objects with ``x``, ``y``, ``width``, ``height``,
    ``ref`` and a net name (``net_name``, or ``net`` when that is a string) --
    both :class:`FieldPad` and the objects
    :func:`kicad_tools.router.io.load_pads_for_analysis` returns satisfy it.
    Pads are grouped by ``ref``; a net counts as *escaping* a field when it
    also has a pad on some other reference, so an internal-only net (a
    decoupling pair inside one footprint) never inflates demand.

    Pads on a net in *pour_nets* are deferred from demand **only where a via
    drop exists**: a poured pad reaches its plane straight down, but if no via
    can be placed (not in pad, and no interstitial site big enough) that pad
    has to leave through the rings exactly like a signal, and pretending
    otherwise is how a counting model talks itself out of a real blockage.
    """
    pad_list = list(pads)
    pours = {str(n) for n in pour_nets if str(n)}
    by_ref: dict[str, list[Any]] = {}
    net_refs: dict[str, set[str]] = {}
    for pad in pad_list:
        ref = str(getattr(pad, "ref", "") or "")
        by_ref.setdefault(ref, []).append(pad)
        net = _pad_net_name(pad)
        if net:
            net_refs.setdefault(net, set()).add(ref)

    drop_layers = max(0, int(signal_layers) - 1)
    fields: list[PadFieldForecast] = []
    footprints_seen = 0
    for ref, field_pads in sorted(by_ref.items()):
        if not ref:
            continue
        footprints_seen += 1
        if len(field_pads) < MIN_PADS_FOR_FIELD:
            continue
        pitch, gap = _field_geometry(field_pads)
        if pitch <= 0:
            continue
        depths = ring_depths([(p.x, p.y) for p in field_pads], pitch)
        rings = max(depths) + 1
        if rings < 2:
            # Every pad is on the boundary: perimeter pins escape outward with
            # no ring to cross, so there is nothing for this model to say.
            continue

        surface_channels = channels_in_gap(gap, track_width_mm, clearance_mm)
        # A via drop needs somewhere to put the barrel: either in the pad (a
        # fab-tier capability) or at an interstitial site between pads with
        # clearance all round -- for a grid array that is the diagonal
        # interstice, not the orthogonal gap (see :func:`via_site_clearance`).
        # Without one, the inner layers are simply unreachable from an interior
        # pad, however many of them the stack has.
        if len(field_pads) <= MAX_PADS_FOR_VIA_SITE_SEARCH:
            site_clear = via_site_clearance(field_pads, pitch)
        else:  # pragma: no cover - very large fields fall back to the gap
            site_clear = 0.5 * gap
        via_fits_between = site_clear + _FIT_EPS >= 0.5 * via_diameter_mm + clearance_mm
        via_drop = bool(via_in_pad or via_fits_between)
        # On the dropped-to layers the obstacles are the via barrels, not the
        # pads, so the channel is measured against the via diameter.
        drop_channels = (
            channels_in_gap(pitch - via_diameter_mm, track_width_mm, clearance_mm)
            if via_drop
            else 0
        )
        channels_per_gap = surface_channels + drop_layers * drop_channels

        escaping: list[int] = []
        deferred = 0
        for i, pad in enumerate(field_pads):
            net = _pad_net_name(pad)
            if not net or len(net_refs.get(net, set())) <= 1:
                continue
            if net in pours:
                if via_drop:
                    # Reaches its plane straight down; never crosses a ring.
                    deferred += 1
                    continue
                # No via can be placed here, so the poured pad still has to
                # leave through the surface rings like any other pin.
            escaping.append(i)
        cuts: list[RingCut] = []
        for depth in range(1, rings):
            gaps = sum(1 for d in depths if d == depth - 1)
            demand = sum(1 for i in escaping if depths[i] >= depth)
            if demand == 0:
                continue
            cuts.append(
                RingCut(
                    depth=depth,
                    gaps=gaps,
                    channels_per_gap=channels_per_gap,
                    demand=demand,
                )
            )
        if not cuts:
            # Multi-ring, but nothing inside needs to leave (a thermal-pad
            # array, or an interior tied entirely to a poured net).
            continue
        fields.append(
            PadFieldForecast(
                ref=ref,
                footprint=str(getattr(field_pads[0], "footprint_name", "") or ""),
                pads=len(field_pads),
                escaping_pads=len(escaping),
                pour_pads_deferred=deferred,
                pitch_mm=pitch,
                gap_mm=gap,
                rings=rings,
                surface_channels_per_gap=surface_channels,
                drop_channels_per_gap=drop_channels,
                via_drop_available=via_drop,
                cuts=tuple(cuts),
            )
        )

    return CapacityForecast(
        source=source,
        signal_layers=int(signal_layers),
        track_width_mm=float(track_width_mm),
        clearance_mm=float(clearance_mm),
        via_diameter_mm=float(via_diameter_mm),
        via_in_pad=bool(via_in_pad),
        footprints_seen=footprints_seen,
        fields_modelled=len(fields),
        fields=tuple(fields),
        warnings=tuple(warnings),
    )


def zone_net_names(pcb) -> set[str]:
    """Net names that already own a copper zone on a loaded board."""
    names: set[str] = set()
    for zone in getattr(pcb, "zones", ()) or ():
        name = str(getattr(zone, "net_name", "") or "")
        if name:
            names.add(name)
    return names


def pour_nets_for_board(pcb, pad_net_names: Iterable[str]) -> set[str]:
    """Net names this board's route will carry as copper pour, not as tracks.

    Two sources, unioned:

    * zones already drawn in the file (:func:`zone_net_names`);
    * the POWER/GROUND nets ``kct route``'s auto-pour would create zones for,
      via the shared
      :func:`kicad_tools.router.auto_pour.classify_pour_candidates` helper --
      an unrouted board usually has no zones yet, and forecasting its power
      pins as track demand would describe a run nobody performs.

    The helper's all-power-board guard is honoured: when *every* net is a pour
    candidate no zones are created (#2740), so nothing is deferred.
    """
    pours = zone_net_names(pcb)
    names = sorted({str(n) for n in pad_net_names if str(n)})
    if not names:
        return pours
    try:
        from kicad_tools.router.auto_pour import classify_pour_candidates

        pour_candidates, _signal_count, is_all_power = classify_pour_candidates(
            dict(enumerate(names, start=1))
        )
        if not is_all_power:
            pours |= {name for name, _cls in pour_candidates}
    except Exception:  # pragma: no cover - the classifier is best-effort here
        pass
    return pours


def load_field_pads(pcb) -> list[FieldPad]:
    """Absolute-positioned pads of every footprint on a loaded board.

    Rotation is applied through
    :func:`kicad_tools.core.geometry.rotate_pad_offset`, the single source of
    truth for KiCad's negated-angle convention (#3739) -- this model measures
    inter-pad distances, so a mis-rotated pad field would silently change every
    number it reports.
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    pads: list[FieldPad] = []
    for footprint in getattr(pcb, "footprints", ()) or ():
        ref = str(getattr(footprint, "reference", "") or "")
        if not ref:
            continue
        origin = getattr(footprint, "position", (0.0, 0.0))
        rotation = float(getattr(footprint, "rotation", 0.0) or 0.0)
        name = str(getattr(footprint, "library_link", "") or getattr(footprint, "name", "") or "")
        for pad in getattr(footprint, "pads", ()) or ():
            local_x, local_y = pad.position
            offset_x, offset_y = rotate_pad_offset(local_x, local_y, rotation)
            size = getattr(pad, "size", (0.0, 0.0)) or (0.0, 0.0)
            pads.append(
                FieldPad(
                    x=float(origin[0]) + offset_x,
                    y=float(origin[1]) + offset_y,
                    width=float(size[0]),
                    height=float(size[1]),
                    ref=ref,
                    net_name=str(getattr(pad, "net_name", "") or ""),
                    footprint_name=name,
                )
            )
    return pads


def forecast_from_board(
    pcb_path: Path | str,
    *,
    signal_layers: int | None = None,
    track_width_mm: float | None = None,
    clearance_mm: float | None = None,
    via_diameter_mm: float | None = None,
    via_in_pad: bool = False,
) -> CapacityForecast:
    """Build the forecast for a board file.

    Any rule left as ``None`` is taken from the board's own ``(setup ...)``
    section (and the layer count from its ``(layers ...)`` section), so the
    forecast describes the board as drawn; ``kct route`` passes the values the
    run will actually use instead.

    Raises :class:`CapacityForecastError` when the board cannot be read.
    """
    target = Path(pcb_path)
    try:
        pcb_text = target.read_text()
    except OSError as exc:
        raise CapacityForecastError(f"cannot read {target}: {exc}") from exc

    from kicad_tools.router.io import detect_layer_stack, parse_pcb_design_rules
    from kicad_tools.schema import PCB

    warnings: list[str] = []
    rules = parse_pcb_design_rules(pcb_text)
    if signal_layers is None:
        try:
            signal_layers = len(detect_layer_stack(pcb_text).signal_layers)
        except Exception as exc:  # pragma: no cover - malformed layer section
            warnings.append(f"layer stack unreadable ({exc}); assuming 2 signal layers")
            signal_layers = 2
    try:
        pcb = PCB.load(str(target))
    except Exception as exc:
        raise CapacityForecastError(f"cannot parse {target}: {exc}") from exc
    pads = load_field_pads(pcb)
    if not pads:
        warnings.append("no footprint pads parsed from the board")
    return build_forecast(
        pads,
        source=str(target),
        signal_layers=signal_layers,
        track_width_mm=rules.min_track_width if track_width_mm is None else track_width_mm,
        clearance_mm=rules.min_clearance if clearance_mm is None else clearance_mm,
        via_diameter_mm=(rules.min_via_diameter if via_diameter_mm is None else via_diameter_mm),
        via_in_pad=via_in_pad,
        pour_nets=pour_nets_for_board(pcb, (p.net_name for p in pads)),
        warnings=warnings,
    )


def write_report(path: Path | str, forecast: CapacityForecast) -> Path:
    """Write *forecast* as the JSON document at *path* and return the path.

    Keys are sorted and the payload is a single object, matching ``kct``'s
    machine-output contract (``docs/reference/machine-output.md``).
    """
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(forecast.to_dict(), indent=2, sort_keys=True, default=str) + "\n")
    return target


def emit_forecast(
    pcb_path: Path | str,
    *,
    stream: Any = None,
    report_path: Path | str | None = None,
    signal_layers: int | None = None,
    track_width_mm: float | None = None,
    clearance_mm: float | None = None,
    via_diameter_mm: float | None = None,
    via_in_pad: bool = False,
) -> CapacityForecast | None:
    """Print the pre-route forecast for *pcb_path*.  Never raises.

    Returns the forecast, or ``None`` when it could not be built (a one-line
    diagnostic goes to *stream*).  A predictor that cannot run is a missing
    diagnostic, not a reason to refuse to route -- the ``_offboard_preflight``
    precedent, restated for every advisory surface on #4799.
    """
    import sys

    out = stream if stream is not None else sys.stderr
    try:
        forecast = forecast_from_board(
            pcb_path,
            signal_layers=signal_layers,
            track_width_mm=track_width_mm,
            clearance_mm=clearance_mm,
            via_diameter_mm=via_diameter_mm,
            via_in_pad=via_in_pad,
        )
    except Exception as exc:
        print(f"[capacity-forecast] no forecast for {pcb_path}: {exc}", file=out)
        return None
    print(forecast.format_human(), file=out)
    if report_path is not None:
        try:
            written = write_report(report_path, forecast)
        except Exception as exc:
            print(f"[capacity-forecast] report NOT written to {report_path}: {exc}", file=out)
        else:
            print(
                f"[capacity-forecast] report written to {written} "
                f"({forecast.fields_modelled} field(s), verdict={forecast.verdict})",
                file=out,
            )
    return forecast
