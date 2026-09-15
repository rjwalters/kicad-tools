"""Classifier -> concrete placement-delta translator (issue #4466).

Phase 1 of the board-07 router<->placement feedback epic (#3438).

The stuck-net classifier (:mod:`kicad_tools.router.stuck_classifier`) already
PROVES the fix for a placement-bound net: :func:`_build_recommendation` ranks a
fix ladder (``DE_REVERSE_BUNDLE`` / ``REORDER_PINS`` / ``MOVE_PART`` / ...) and
:func:`_resolve_bundle_orientation` measures which facing part is reversed.  But
the ladder terminates at an *English sentence* -- nothing turns it into an
applyable ``(ref, dx, dy, rotation)``.  This module is the missing translator.

It is the "propose" half only: a **pure, read-only** function that maps one
:class:`~kicad_tools.router.stuck_classifier.StuckNetDiagnosis` onto a single
:class:`PlacementDelta` (JSON-serializable data).  It performs NO ``PCB``
mutation and attempts NO re-route -- applying the delta and re-routing is
Phase 2 (#4467).

Mapping rules (driven off the *top-ranked* action, honoring the ladder's
deliberate omissions -- e.g. a reversed bus never gets ``WIDEN_CHANNEL``):

* ``DE_REVERSE_BUNDLE`` -> ``kind="mirror"`` on the reversed facing part
  (:attr:`BundleOrientation.secondary_ref`): a KiCad-semantics layer flip
  about the part's own anchor.  This REPLACES the earlier ``rotate_180``
  proposal (issue #4560): rotation preserves the pin column's chirality, so
  it structurally cannot un-reverse a mirrored pin order -- it only relocates
  the whole facing column to the far side of the package (measured on
  board-07 CI: routed 25 -> 14, reverted).  A mirror un-reverses the column
  in place; KiCad expresses it as a flip to the other board side.  The
  ``rotate_180`` *kind* remains fully supported by the Phase-2 applicator for
  committed delta artifacts -- only the proposal changed.
* ``MOVE_PART`` -> ``kind="translate"``: ``target_ref`` is the crowded foreign
  component nearest the stranded pad; ``(dx, dy)`` is a minimal bounded step
  toward the widest open escape arc.
* ``REORDER_PINS`` -> ``kind="reorder_pins"`` with rationale only (no geometry
  in P1; an applicator needs a pad-remap that does not yet exist).
* ``WIDEN_CHANNEL`` / ``ACCEPT_PLATEAU`` / no recommendation -> ``None`` (there
  is no placement move to emit -- never synthesize one the ladder dropped).

Issue #4968 adds a SECOND, additive family of proposals on top of that
one-delta-per-diagnosis table: bounded +/-90-degree **endpoint-orientation**
candidates (``kind="rotate_align"``) for a stuck net whose two endpoints
present a pad-ROW against a pad-COLUMN.  ``MOVE_PART`` can only express "shove
the crowding part a couple of millimetres"; it structurally cannot express
"turn the source connector a quarter turn so its horizontal pad row faces the
receiver's vertical column", which is the move an A/B probe on board-07
measured as connecting 6/6 MIPI nets where the unchanged-angle arm connected
4/6.  :func:`endpoint_align_deltas` emits those candidates and
:func:`deltas_from_result` appends them AFTER the primary delta for the same
diagnosis, so the existing selection order is unchanged and the new candidates
are simply further rungs for a caller that probes past the first one.

**A ``rotate_align`` candidate is a connectivity hypothesis, never a
manufacturability verdict.**  This module measures pad geometry only: it does
not evaluate pair/group skew, intra-pair coupling, or any other match-group
constraint, and nothing it emits may be read as "the resulting board is
Ready".  The rationale string says so explicitly so the claim travels with the
artifact.

Generic: works for any board's ``PLACEMENT_BOUND`` / ``CONGESTION_SATURATED``
diagnosis, not just board-07.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from kicad_tools.router.stuck_classifier import (
    DEFAULT_CONGESTION_RADIUS_MM,
    ESCAPE_SECTORS,
    RecommendedAction,
    StuckNetDiagnosis,
    _foreign_obstructions,
    _resolve_match_groups,
)

if TYPE_CHECKING:
    from kicad_tools.router.stuck_classifier import StuckClassifierResult
    from kicad_tools.schema.pcb import PCB


__all__ = [
    "PlacementDelta",
    "MAX_TRANSLATE_MM",
    "ENDPOINT_ALIGN_ROTATIONS",
    "ENDPOINT_ALIGN_SOURCE",
    "delta_from_diagnosis",
    "deltas_from_result",
    "endpoint_align_deltas",
]


# Bound on a single ``translate`` proposal.  The move is deliberately minimal --
# a small nudge to open a lane, not a wholesale relocation -- so a Phase-2
# applicator can probe cheaply and a human can eyeball it.  "A few mm" per the
# #4466 design; the step magnitude equals this bound (minimal-first).
MAX_TRANSLATE_MM = 2.0

# Radius scanned around the stranded pad for the foreign obstructions that seed
# the translate direction.  Reuses the classifier's congestion ring so the
# delta agrees with the geometry that produced the MOVE_PART verdict.
_TRANSLATE_RADIUS_MM = DEFAULT_CONGESTION_RADIUS_MM

# --- endpoint-orientation alignment bounds (issue #4968) --------------------
#
# The search is deliberately TINY: two quarter turns, considered for at most
# two endpoints of one net, with the better-scoring turn kept per endpoint.  A
# quarter turn is the only rotation that converts a pad row into a pad column;
# anything finer is a placement optimizer's job, not a stuck-net proposer's.

#: The complete bounded candidate set for an endpoint-orientation proposal.
ENDPOINT_ALIGN_ROTATIONS: tuple[float, ...] = (90.0, -90.0)

#: ``source_action`` stamped on a ``rotate_align`` delta.  Unlike the other
#: kinds this is NOT a :class:`RecommendedAction` value: the candidate comes
#: from a geometric measurement layered on top of the ladder, not from a rung
#: the classifier ranked.  The triggering rung is named in the rationale.
ENDPOINT_ALIGN_SOURCE = "endpoint_align"

#: Minimum pad count for a footprint's pad array to be read as a row/column.
#: Two pads are a line segment through any two points -- meaningless as an
#: orientation claim.
MIN_ALIGN_PADS = 3

#: Minimum elongation (major/minor spread ratio) of a footprint's pad cloud
#: before it counts as a "row" or "column".  A square QFN pad ring has no
#: dominant axis, so rotating it aligns nothing.
MIN_ALIGN_ASPECT = 2.0

#: How far from exactly perpendicular the two endpoints' pad axes may sit and
#: still count as a row/column MISMATCH worth a quarter turn.
ALIGN_MISMATCH_TOLERANCE_DEG = 30.0

# A candidate is graded ``medium`` only when both endpoints are strongly
# elongated AND close to exactly perpendicular; everything else is ``low``.
# ``high`` is never emitted -- see the module docstring: this proposer measures
# connectivity geometry, not manufacturability.
_STRONG_ALIGN_ASPECT = 3.0
_STRONG_ALIGN_TOLERANCE_DEG = 15.0

#: A footprint whose pad bounding box already sits within this distance of the
#: board outline is read as EDGE-MOUNTED -- a connector whose mating face has
#: to stay at the edge.  Turning it is a mechanical change this proposer has no
#: standing to make, so such parts are never proposed for rotation (the
#: edge-facing rotation objective is #4525, deliberately separate).
EDGE_ACCESS_MARGIN_MM = 1.0


@dataclass
class PlacementDelta:
    """A concrete, applyable placement change proposed for one stuck net.

    Data only -- emitting a ``PlacementDelta`` mutates nothing.  ``kind`` is one
    of ``"translate"`` | ``"rotate_180"`` | ``"rotate_align"`` | ``"mirror"`` |
    ``"reorder_pins"``; the geometric fields carry the move for the kinds that
    have one (``translate`` uses ``dx``/``dy``; ``rotate_180`` and
    ``rotate_align`` use ``rotation_delta``; ``mirror`` is parameterless -- a
    left/right layer flip about the target's own anchor, #4560;
    ``reorder_pins`` carries rationale only).

    ``rotate_align`` (issue #4968) is a bounded quarter turn about the target's
    own anchor -- ``rotation_delta`` is always ``+90`` or ``-90``.  Because the
    target's ``fp.position`` never moves and only its orientation changes, both
    the board side (F.Cu/B.Cu) and the logical pad->net mapping are preserved
    by construction; ``rationale`` carries the measured pad-axis evidence that
    produced the candidate.
    """

    net_name: str
    target_ref: str
    kind: str
    dx: float = 0.0
    dy: float = 0.0
    rotation_delta: float = 0.0
    source_action: str = ""
    rationale: str = ""
    confidence: str = ""
    component_id: str = ""

    @property
    def target_key(self) -> str:
        """Physical target identity; target_ref remains authored metadata."""
        return self.component_id or self.target_ref

    def to_dict(self) -> dict:
        return {
            "net_name": self.net_name,
            "target_ref": self.target_ref,
            "kind": self.kind,
            "dx": round(self.dx, 4),
            "dy": round(self.dy, 4),
            "rotation_delta": round(self.rotation_delta, 4),
            "source_action": self.source_action,
            "rationale": self.rationale,
            "confidence": self.confidence,
            **({"component_id": self.component_id} if self.component_id else {}),
        }

    @classmethod
    def from_dict(cls, data: dict) -> PlacementDelta:
        """Reconstruct a :class:`PlacementDelta` from :meth:`to_dict` output.

        The round-trip counterpart required by Phase 2 (#4467): the
        ``<output>_placement_delta.json`` artifact stores each delta via
        :meth:`to_dict`, and a propose-only recipe reloads them via this
        classmethod to apply them deterministically without re-running the
        classifier.  Missing optional keys fall back to the dataclass
        defaults so a hand-written or older artifact still loads.
        """
        return cls(
            net_name=data["net_name"],
            target_ref=data["target_ref"],
            kind=data["kind"],
            dx=float(data.get("dx", 0.0)),
            dy=float(data.get("dy", 0.0)),
            rotation_delta=float(data.get("rotation_delta", 0.0)),
            source_action=data.get("source_action", ""),
            rationale=data.get("rationale", ""),
            confidence=data.get("confidence", ""),
            component_id=data.get("component_id", ""),
        )


def delta_from_diagnosis(pcb: PCB, diag: StuckNetDiagnosis) -> PlacementDelta | None:
    """Translate one classifier diagnosis into a concrete placement delta.

    Returns ``None`` when the top-ranked action is not an applyable placement
    move (``WIDEN_CHANNEL`` / ``ACCEPT_PLATEAU`` / empty ladder), or when the
    geometry needed to realize the move is unavailable (e.g. a reversed-bundle
    verdict whose facing part could not be resolved).  Pure/read-only: ``pcb``
    is never mutated.
    """
    if not diag.recommendation:
        return None

    top = diag.recommendation[0]
    action = top.action
    confidence = top.confidence.value
    rationale = top.rationale

    if action is RecommendedAction.DE_REVERSE_BUNDLE:
        return _mirror_delta(diag, confidence, rationale)
    if action is RecommendedAction.MOVE_PART:
        return _translate_delta(pcb, diag, confidence, rationale)
    if action is RecommendedAction.REORDER_PINS:
        return _reorder_pins_delta(diag, confidence, rationale)

    # WIDEN_CHANNEL / ACCEPT_PLATEAU (or anything else): no placement move.
    return None


def deltas_from_result(
    pcb: PCB,
    result: StuckClassifierResult,
    *,
    fixed_refs: set[str] | frozenset[str] | list[str] | None = None,
    include_endpoint_alignment: bool = True,
) -> list[PlacementDelta]:
    """Emit the ``PlacementDelta`` candidates for every diagnosis.

    Convenience wrapper over :func:`delta_from_diagnosis`; diagnoses that map to
    ``None`` (non-placement top action) contribute no primary delta.

    Issue #4968: when ``include_endpoint_alignment`` is True (the default) the
    bounded +/-90-degree endpoint-orientation candidates from
    :func:`endpoint_align_deltas` are APPENDED after each diagnosis's primary
    delta.  Appending (rather than replacing or preceding) is deliberate: a
    consumer that takes the first applyable delta keeps exactly its pre-#4968
    behaviour, and only reaches an alignment candidate once the ladder's own
    proposal has been probed.  ``fixed_refs`` names parts the caller has
    anchored; they are never proposed for rotation.
    """
    out: list[PlacementDelta] = []
    for diag in result.diagnoses:
        delta = delta_from_diagnosis(pcb, diag)
        if delta is not None:
            out.append(delta)
        if include_endpoint_alignment:
            out.extend(endpoint_align_deltas(pcb, diag, fixed_refs=fixed_refs))
    return out


# --- per-kind builders ------------------------------------------------------


def _mirror_delta(
    diag: StuckNetDiagnosis, confidence: str, rationale: str
) -> PlacementDelta | None:
    """DE_REVERSE_BUNDLE -> mirror (layer-flip) the reversed facing part.

    The reversed part is the bundle's ``secondary_ref`` (the classifier resolves
    the two facing rows and reports ``primary_ref`` / ``secondary_ref`` with the
    ``secondary`` being the one whose pad column runs opposite the primary).

    Ladder note (#4560): the geometric realization of "un-reverse the facing
    pad column" is a MIRROR, not a rotation.  A 180-degree rotation preserves
    the column's chirality -- it de-reverses the pad order only by relocating
    the whole column to the far side of the package, which collapsed reach on
    board-07 (CI: routed 25 -> 14, reverted).  The mirror is therefore the
    single delta emitted for this verdict (``delta_from_diagnosis`` keeps its
    one-delta-per-diagnosis contract); it is deterministic and parameterless
    (a left/right flip about the target's own anchor).  Returns ``None`` when
    the facing part could not be resolved (no applyable target).
    """
    orientation = diag.bundle_orientation
    if orientation is None or not orientation.secondary_ref:
        return None
    return PlacementDelta(
        net_name=diag.net_name,
        target_ref=orientation.secondary_ref,
        kind="mirror",
        source_action=RecommendedAction.DE_REVERSE_BUNDLE.value,
        rationale=rationale,
        confidence=confidence,
    )


def _reorder_pins_delta(
    diag: StuckNetDiagnosis, confidence: str, rationale: str
) -> PlacementDelta | None:
    """REORDER_PINS -> a rationale-only delta (no geometry in Phase 1).

    A pad-level re-map applicator does not exist yet, so this carries no
    ``(dx, dy, rotation)`` -- it names the part whose pin order should change
    (the reversed facing part when known) and defers the mechanics to a future
    phase.
    """
    orientation = diag.bundle_orientation
    target_ref = orientation.secondary_ref if orientation else ""
    return PlacementDelta(
        net_name=diag.net_name,
        target_ref=target_ref,
        kind="reorder_pins",
        source_action=RecommendedAction.REORDER_PINS.value,
        rationale=rationale,
        confidence=confidence,
    )


def _translate_delta(
    pcb: PCB, diag: StuckNetDiagnosis, confidence: str, rationale: str
) -> PlacementDelta | None:
    """MOVE_PART -> a minimal bounded translate of the crowding foreign part.

    ``target_ref`` is the foreign component whose pad sits nearest the stranded
    pad (the part physically walling the escape); ``(dx, dy)`` is a step of at
    most :data:`MAX_TRANSLATE_MM` from the foreign-congestion centroid toward
    the widest open escape arc.  Returns ``None`` when there is no foreign
    obstruction to move away from, or no resolvable direction / target.
    """
    positions = _stranded_pad_positions(pcb, diag.net_name)
    if not positions:
        return None

    # The densest stranded pad drives the move (it is the most walled-in).
    best_point: tuple[float, float] | None = None
    best_obstr: list[tuple[float, float, float, int]] = []
    best_count = -1
    for pt in positions:
        obstr = _foreign_obstructions(pcb, diag.net_number, pt, _TRANSLATE_RADIUS_MM)
        if len(obstr) > best_count:
            best_count = len(obstr)
            best_point = pt
            best_obstr = obstr
    if best_point is None or not best_obstr:
        return None

    direction = _widest_open_arc_direction(best_obstr, best_point, ESCAPE_SECTORS)
    if direction is None:
        return None

    target_ref = _nearest_foreign_component(pcb, diag.net_number, best_point)
    if target_ref is None:
        return None

    target = _find_footprint(pcb, target_ref)
    if target is None:
        return None

    dx = MAX_TRANSLATE_MM * math.cos(direction)
    dy = MAX_TRANSLATE_MM * math.sin(direction)
    return PlacementDelta(
        net_name=diag.net_name,
        target_ref=target.reference,
        component_id=target_ref if target_ref != target.reference else "",
        kind="translate",
        dx=dx,
        dy=dy,
        source_action=RecommendedAction.MOVE_PART.value,
        rationale=rationale,
        confidence=confidence,
    )


# --- endpoint-orientation alignment (issue #4968) ---------------------------


@dataclass(frozen=True)
class _PadAxis:
    """Measured dominant axis of one footprint's pad cloud (board frame)."""

    ref: str
    angle_deg: float  # principal-axis bearing folded into [0, 180)
    aspect: float  # major/minor spread ratio (>= 1.0; inf for a perfect line)
    pad_count: int
    scope: str = "pad array"  # which pads were measured (named in the rationale)


def endpoint_align_deltas(
    pcb: PCB,
    diag: StuckNetDiagnosis,
    *,
    fixed_refs: set[str] | frozenset[str] | list[str] | None = None,
) -> list[PlacementDelta]:
    """Bounded +/-90-degree endpoint-orientation candidates for one diagnosis.

    The capability the classifier-to-delta table could not express (#4968): a
    stuck net whose two endpoints present a pad ROW against a pad COLUMN is not
    helped by shoving a neighbour 2 mm sideways -- it needs one endpoint turned
    a quarter turn so the two pad arrays run parallel.  This measures that
    mismatch and proposes the turn.

    Gating, in order:

    1. Only diagnoses whose TOP ranked action is ``MOVE_PART`` are considered.
       The ladder's omissions stay honoured: a reversed bundle is a pad-ORDER
       defect a rotation cannot fix (#4560), and ``WIDEN_CHANNEL`` /
       ``ACCEPT_PLATEAU`` say no placement move applies at all.
    2. The net must reach at least two footprints; the two whose pads of this
       net sit farthest apart are taken as its endpoints (source/sink).
    3. Both endpoints' pad clouds must read as a row/column at all --
       ``>= MIN_ALIGN_PADS`` pads and elongation ``>= MIN_ALIGN_ASPECT``.  The
       cloud is the net's match-group bundle on that footprint when there is
       one (a QFN's lane pads down one edge), falling back to the complete pad
       array otherwise (a connector, or an ungrouped point-to-point net) --
       see :func:`_endpoint_axis`.
    4. Their axes must be perpendicular within
       ``ALIGN_MISMATCH_TOLERANCE_DEG`` -- that IS the mismatch.
    5. Each endpoint is then checked for standing to rotate it at all (see
       :func:`_rotation_blocked_reason`): anchored by the caller, ``locked`` on
       the board, or edge-mounted (a connector whose mating face must stay at
       the board edge) all disqualify it.
    6. For each surviving endpoint both quarter turns are scored and the better
       one is emitted -- at most one candidate per endpoint, at most two per
       diagnosis.

    Returns ``[]`` (never a malformed delta) whenever any gate fails -- notably
    when both endpoints are fixed/locked, or when no quarter turn keeps the
    rotated pads inside the board outline.  Pure/read-only: ``pcb`` is never
    mutated.
    """
    if pcb is None or not diag.recommendation:
        return []
    if diag.recommendation[0].action is not RecommendedAction.MOVE_PART:
        return []

    group_ids = _bundle_net_ids(pcb, diag)
    all_pads: dict[str, list[tuple[float, float]]] = {}
    bundle_pads: dict[str, list[tuple[float, float]]] = {}
    net_pads: dict[str, list[tuple[float, float]]] = {}
    for ref, net_number, point, _size in _iter_physical_board_pads(pcb):
        all_pads.setdefault(ref, []).append(point)
        if net_number in group_ids:
            bundle_pads.setdefault(ref, []).append(point)
        if net_number == diag.net_number:
            net_pads.setdefault(ref, []).append(point)

    endpoints = _endpoint_pair(net_pads)
    if endpoints is None:
        return []

    axes: list[_PadAxis] = []
    for ref in endpoints:
        axis = _endpoint_axis(ref, bundle_pads.get(ref, []), all_pads.get(ref, []))
        if axis is None:
            return []
        axes.append(axis)

    separation = _axis_separation_deg(axes[0].angle_deg, axes[1].angle_deg)
    if abs(separation - 90.0) > ALIGN_MISMATCH_TOLERANCE_DEG:
        return []

    confidence = (
        "medium"
        if (
            min(axes[0].aspect, axes[1].aspect) >= _STRONG_ALIGN_ASPECT
            and abs(separation - 90.0) <= _STRONG_ALIGN_TOLERANCE_DEG
        )
        else "low"
    )
    bounds = _board_bounds(pcb)
    anchored = frozenset(fixed_refs or ())

    # Smaller pad array first: turning the connector perturbs less copper than
    # turning the receiver.  Ties break on reference ascending (determinism).
    ordered = sorted(range(2), key=lambda i: (axes[i].pad_count, axes[i].ref))

    out: list[PlacementDelta] = []
    for i in ordered:
        target, partner = axes[i], axes[1 - i]
        if _rotation_blocked_reason(pcb, target.ref, all_pads[target.ref], anchored, bounds):
            continue
        choice = _best_quarter_turn(
            pcb,
            target.ref,
            all_pads[target.ref],
            net_pads.get(target.ref, []),
            net_pads.get(partner.ref, []),
            bounds,
        )
        if choice is None:
            continue
        rotation, reach = choice
        footprint = _find_footprint(pcb, target.ref)
        if footprint is None:
            continue
        partner_footprint = _find_footprint(pcb, partner.ref)
        out.append(
            PlacementDelta(
                net_name=diag.net_name,
                target_ref=footprint.reference,
                component_id=target.ref if target.ref != footprint.reference else "",
                kind="rotate_align",
                rotation_delta=rotation,
                source_action=ENDPOINT_ALIGN_SOURCE,
                rationale=_align_rationale(
                    replace(target, ref=footprint.reference),
                    replace(partner, ref=partner_footprint.reference)
                    if partner_footprint is not None
                    else partner,
                    separation,
                    rotation,
                    reach,
                ),
                confidence=confidence,
            )
        )
    return out


def _align_rationale(
    target: _PadAxis,
    partner: _PadAxis,
    separation: float,
    rotation: float,
    reach: float,
) -> str:
    """Auditable pad-alignment evidence for one ``rotate_align`` candidate."""
    return (
        f"pad-row/column mismatch (ladder rung: {RecommendedAction.MOVE_PART.value}): "
        f"{target.ref} pad axis {target.angle_deg:.1f} deg (aspect {target.aspect:.1f}, "
        f"{target.pad_count} {target.scope}) vs {partner.ref} pad axis "
        f"{partner.angle_deg:.1f} deg (aspect {partner.aspect:.1f}, {partner.pad_count} "
        f"{partner.scope}) -- {separation:.1f} deg "
        f"apart. Rotating {target.ref} by {rotation:+.0f} deg about its own anchor makes "
        f"the two pad arrays parallel (nearest-endpoint pad span {reach:.2f} mm); the "
        f"anchor, board side and pad->net mapping are unchanged. CONNECTIVITY candidate "
        f"only -- pair/group skew, coupling and other match-group constraints are NOT "
        f"evaluated here and must still be checked before any manufacturability claim."
    )


def _bundle_net_ids(pcb: PCB, diag: StuckNetDiagnosis) -> set[int]:
    """Net numbers whose pads make up the stuck net's bundle at each endpoint.

    The stuck net's own number plus its length-match-group siblings when the
    classifier inferred a group.  A lone net falls back to just itself.
    """
    ids = {diag.net_number}
    if not diag.match_group:
        return ids
    try:
        _net_to_group, group_members = _resolve_match_groups(pcb)
    except Exception:  # pragma: no cover - detector is best-effort here
        return ids
    return set(group_members.get(diag.match_group, set())) | ids


def _endpoint_axis(
    ref: str,
    bundle_points: list[tuple[float, float]],
    all_points: list[tuple[float, float]],
) -> _PadAxis | None:
    """Axis of ``ref``'s endpoint pad line, measured bundle-first.

    The row/column an escape has to face is the BUNDLE's pad line at that
    endpoint, not necessarily the whole package: board-07's MIPI receiver is a
    48-pad QFN whose pad ring has no dominant axis at all, while its MIPI lane
    pads form an unambiguous column down one edge.  Measuring the full ring
    there would report "no row" and silently drop exactly the candidate this
    issue exists to express.

    Falls back to the complete pad array when the bundle contributes too few
    pads on this footprint to carry an orientation claim -- which is the normal
    case for a two-pin connector or an ungrouped point-to-point net.
    """
    if len(bundle_points) >= MIN_ALIGN_PADS:
        axis = _pad_axis(ref, bundle_points)
        if axis is not None:
            return _replace_scope(axis, "match-group bundle pads")
    axis = _pad_axis(ref, all_points)
    return None if axis is None else _replace_scope(axis, "full pad array")


def _replace_scope(axis: _PadAxis, scope: str) -> _PadAxis:
    return _PadAxis(
        ref=axis.ref,
        angle_deg=axis.angle_deg,
        aspect=axis.aspect,
        pad_count=axis.pad_count,
        scope=scope,
    )


def _endpoint_pair(net_pads: dict[str, list[tuple[float, float]]]) -> tuple[str, str] | None:
    """The two refs of ``net_pads`` whose pad centroids sit farthest apart.

    With the usual two-endpoint net this is simply "both of them"; for a net
    that fans out to three or more parts it picks the source/sink extremes,
    which is what an orientation mismatch is about.  Deterministic: refs are
    scanned in sorted order and ties keep the first pair found.  ``None`` when
    the net reaches fewer than two footprints.
    """
    refs = sorted(net_pads)
    if len(refs) < 2:
        return None
    centroids = {ref: _centroid(net_pads[ref]) for ref in refs}
    best: tuple[str, str] | None = None
    best_d = -1.0
    for i, a in enumerate(refs):
        for b in refs[i + 1 :]:
            d = math.dist(centroids[a], centroids[b])
            if d > best_d:
                best_d = d
                best = (a, b)
    return best


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    n = float(len(points))
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)


def _pad_axis(ref: str, points: list[tuple[float, float]]) -> _PadAxis | None:
    """Principal axis of a footprint's pad cloud, or ``None`` if it is not a row.

    Closed-form 2x2 PCA over the board-frame pad centres.  ``angle_deg`` is the
    major axis folded into ``[0, 180)`` (an axis has no direction, so 10 deg and
    190 deg are the same row) and ``aspect`` is the major/minor spread ratio.
    Returns ``None`` when the cloud is too small or too round to carry an
    orientation claim -- the caller then proposes nothing rather than inventing
    an axis for a square pad ring.
    """
    if len(points) < MIN_ALIGN_PADS:
        return None
    cx, cy = _centroid(points)
    sxx = sum((x - cx) ** 2 for x, _ in points) / len(points)
    syy = sum((y - cy) ** 2 for _, y in points) / len(points)
    sxy = sum((x - cx) * (y - cy) for x, y in points) / len(points)

    mid = (sxx + syy) / 2.0
    spread = math.hypot((sxx - syy) / 2.0, sxy)
    major, minor = mid + spread, mid - spread
    if major <= 1e-12:
        return None
    aspect = math.inf if minor <= 1e-12 else math.sqrt(major / minor)
    if aspect < MIN_ALIGN_ASPECT:
        return None
    angle = math.degrees(0.5 * math.atan2(2.0 * sxy, sxx - syy)) % 180.0
    return _PadAxis(ref=ref, angle_deg=angle, aspect=aspect, pad_count=len(points))


def _axis_separation_deg(a: float, b: float) -> float:
    """Unsigned separation of two undirected axes, folded into ``[0, 90]``."""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _rotate_about(
    points: list[tuple[float, float]], cx: float, cy: float, rotation_delta: float
) -> list[tuple[float, float]]:
    """Board-frame image of ``points`` after the anchor rotates by ``rotation_delta``.

    Mirrors :func:`~kicad_tools.router.stuck_classifier._iter_board_pads`
    exactly: KiCad negates the footprint orientation relative to standard CCW
    math (#3739), so bumping ``fp.rotation`` by ``d`` maps each pad offset
    through ``R(-d)``.  Keeping the two in lockstep is what makes the proposer's
    predicted geometry equal the geometry the classifier will read back after
    the applicator runs.
    """
    ang = math.radians(-rotation_delta)
    cos_a, sin_a = math.cos(ang), math.sin(ang)
    out = []
    for x, y in points:
        dx, dy = x - cx, y - cy
        out.append((cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a))
    return out


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _board_bounds(pcb: PCB) -> tuple[float, float, float, float] | None:
    """Board outline bounding box, or ``None`` when it cannot be read.

    A board with no (or unreadable) ``Edge.Cuts`` geometry simply disables the
    mechanical gates below rather than failing the proposal -- the synthetic
    fixtures the classifier is unit-tested on have no outline.
    """
    try:
        outline = pcb.get_board_outline()
    except Exception:  # pragma: no cover - malformed Edge.Cuts is fail-loud upstream
        return None
    if not outline:
        return None
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    return (min(xs), min(ys), max(xs), max(ys))


def _find_footprint(pcb: PCB, ref: str):
    from kicad_tools.schema.physical_identity import footprint_keys

    footprints = list(getattr(pcb, "footprints", []))
    for key, fp in zip(footprint_keys(footprints), footprints, strict=True):
        if key == ref:
            return fp
    if sum(fp.reference == ref for fp in footprints) > 1:
        raise ValueError(f"Ambiguous footprint reference {ref!r}; use a physical component ID")
    return None


def _iter_physical_board_pads(pcb: PCB):
    """Complete physical-footprint grouping, including anonymous pads."""
    from kicad_tools.schema.physical_identity import footprint_keys

    footprints = list(pcb.footprints)
    for key, fp in zip(footprint_keys(footprints), footprints, strict=True):
        if fp.reference.startswith("#"):
            continue
        angle = math.radians(-fp.rotation)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        for pad in fp.pads:
            px, py = pad.position
            point = (
                fp.position[0] + px * cos_a - py * sin_a,
                fp.position[1] + px * sin_a + py * cos_a,
            )
            yield key, pad.net_number, point, pad.size


def _rotation_blocked_reason(
    pcb: PCB,
    ref: str,
    pad_points: list[tuple[float, float]],
    anchored: frozenset[str],
    bounds: tuple[float, float, float, float] | None,
) -> str:
    """Why ``ref`` may NOT be rotated, or ``""`` when it may.

    Three standing checks, in increasing cost:

    * the caller anchored it (``fixed_refs``) -- an explicit fixed placement;
    * the board marks it ``(locked yes)`` -- an explicit fixed placement the
      board itself asserts, which no proposer may quietly override;
    * its pads already sit flush against the board outline -- an edge-mounted
      connector whose mating face has to stay at the edge.  Turning it is a
      mechanical change, not a routing change (the separate edge-facing
      rotation objective is #4525).
    """
    if ref in anchored:
        return f"{ref} is anchored by the caller"
    fp = _find_footprint(pcb, ref)
    if fp is None:
        return f"{ref} has no footprint on the board"
    if fp.reference in anchored:
        return f"{fp.reference} is anchored by the caller"
    if getattr(fp, "locked", False):
        return f"{ref} is locked on the board"
    if bounds is not None and pad_points:
        min_x, min_y, max_x, max_y = bounds
        px0, py0, px1, py1 = _bbox(pad_points)
        if (
            px0 - min_x <= EDGE_ACCESS_MARGIN_MM
            or py0 - min_y <= EDGE_ACCESS_MARGIN_MM
            or max_x - px1 <= EDGE_ACCESS_MARGIN_MM
            or max_y - py1 <= EDGE_ACCESS_MARGIN_MM
        ):
            return f"{ref} is edge-mounted (connector access must be preserved)"
    return ""


def _best_quarter_turn(
    pcb: PCB,
    ref: str,
    pad_points: list[tuple[float, float]],
    target_net_pads: list[tuple[float, float]],
    partner_net_pads: list[tuple[float, float]],
    bounds: tuple[float, float, float, float] | None,
) -> tuple[float, float] | None:
    """Score both quarter turns for ``ref`` and return ``(rotation, reach)``.

    Both signs realign the axis identically -- an axis is undirected -- so the
    sign is chosen on a SECONDARY objective: the total nearest-partner distance
    of this net's own pads on ``ref`` after the turn.  Lower is better (the
    fan-out gets shorter, which is what the stuck net needs); an exact tie keeps
    ``+90`` so the proposal is reproducible.

    A turn whose rotated pads would leave the board outline is dropped.
    ``None`` when no turn survives, or when the footprint anchor is unavailable.
    """
    fp = _find_footprint(pcb, ref)
    if fp is None:
        return None
    cx, cy = fp.position[0], fp.position[1]

    best: tuple[float, float] | None = None
    for rotation in ENDPOINT_ALIGN_ROTATIONS:
        moved_all = _rotate_about(pad_points, cx, cy, rotation)
        if bounds is not None and moved_all:
            min_x, min_y, max_x, max_y = bounds
            bx0, by0, bx1, by1 = _bbox(moved_all)
            if bx0 < min_x or by0 < min_y or bx1 > max_x or by1 > max_y:
                continue
        reach = _nearest_partner_span(
            _rotate_about(target_net_pads, cx, cy, rotation), partner_net_pads
        )
        if best is None or reach < best[1]:
            best = (rotation, reach)
    return best


def _nearest_partner_span(
    points: list[tuple[float, float]], partner: list[tuple[float, float]]
) -> float:
    """Sum over ``points`` of the distance to the nearest ``partner`` point.

    ``0.0`` when either side is empty (no evidence either way -- the caller then
    falls back to the deterministic ``+90`` tie-break).
    """
    if not points or not partner:
        return 0.0
    return sum(min(math.dist(p, q) for q in partner) for p in points)


# --- geometry helpers -------------------------------------------------------


def _stranded_pad_positions(pcb: PCB, net_name: str) -> list[tuple[float, float]]:
    """Board-frame positions of ``net_name``'s unconnected pads (read-only)."""
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    analysis = NetStatusAnalyzer(pcb).analyze()
    status = analysis.get_net(net_name)
    if status is None:
        return []
    return [p.position for p in status.unconnected_pads]


def _nearest_foreign_component(pcb: PCB, target_net: int, point: tuple[float, float]) -> str | None:
    """Reference of the foreign component whose pad is nearest ``point``.

    "Foreign" == any net other than ``target_net``.  Ties break on the smaller
    distance; the scan is deterministic in footprint order. Returns None when
    the board has no foreign pad at all.
    """
    px, py = point
    best_ref = None
    best_dist = math.inf
    for ref, net_number, (bx, by), _size in _iter_physical_board_pads(pcb):
        if net_number == target_net:
            continue
        d = math.hypot(bx - px, by - py)
        if d < best_dist:
            best_dist = d
            best_ref = ref
    return best_ref


def _widest_open_arc_direction(
    obstructions: list[tuple[float, float, float, int]],
    point: tuple[float, float],
    sectors: int,
) -> float | None:
    """Center angle (radians) of the widest open escape arc around ``point``.

    Mirrors :func:`~kicad_tools.router.stuck_classifier._widest_open_arc` but
    returns the *direction* of the widest open run rather than its width.  Every
    obstruction blocks its angular sector; the widest contiguous run of open
    sectors (wrapping around the circle) yields the escape direction.  Returns
    ``0.0`` when nothing blocks (pick +x) and ``None`` when every sector is
    blocked (no lane -> caller declines to emit a move).
    """
    px, py = point
    blocked = [False] * sectors
    for ox, oy, _d, _net in obstructions:
        ang = math.atan2(oy - py, ox - px)
        idx = int((ang + math.pi) / (2 * math.pi) * sectors) % sectors
        blocked[idx] = True

    if not any(blocked):
        return 0.0
    if all(blocked):
        return None

    # Widest run of open (False) sectors over the doubled circular array.
    best_len = 0
    best_start = 0
    run = 0
    run_start = 0
    for i in range(2 * sectors):
        if blocked[i % sectors]:
            run = 0
            run_start = i + 1
        else:
            if run == 0:
                run_start = i
            run += 1
            if run > best_len:
                best_len = run
                best_start = run_start

    center_idx = best_start + best_len / 2.0
    # Invert the sector bucketing: idx = (ang + pi) / (2pi) * sectors.
    return (center_idx / sectors) * 2 * math.pi - math.pi
