"""Explicit process policy and source-qualified solder-mask exposure assessment.

Outside ``--mfr`` this check is opt-in. Neither a manufacturer's generic mask
expansion nor a mask-web width supplies a mask-to-copper process clearance
requirement; ``default_request_for_manufacturer`` supplies an explicit, cited
interim policy for manufacturers whose process limit has been observed
(issue #8842).

Finding message format (issue #9038)
====================================

Each ``mask_to_copper`` finding reads::

    Mask-to-copper <relation>: <disposition>; [provisional: coverage incomplete;]
    exposed area <A> mm²; assessment measurement <N>; opening <owner>;
    exposed copper <kind> (net <net>); <net relation>

``<owner>`` is ``pad:<ref>/<num>`` when the opening's owner pad could be
identified from the source footprint reference and pad number, otherwise the
opening's inventory kind with an eight-character UUID prefix (``pad:rect[4f2c1a9b]``,
``merged_region[...]``). ``<kind>`` is the exposed conductor's inventory kind
(``segment``, ``arc``, ``via``, ``zone``, ``pad:...``); a ``zone`` (a pad's own
same-net pour) is also declarable through a ``MaskEscapeIntent``.
``<net relation>`` is one of

- ``same-net own-pad entry`` -- the conductor shares the opening owner's net
  and terminates inside the opening: for ``segment``/``arc`` conductors one of
  the conductor's endpoints (the farthest-apart pair of boundary points of its
  copper, i.e. the rounded track caps) lies within the opening; for a ``via``
  its center stands in for an endpoint. This is a track entering or leaving its
  own pad, the shape a fab reports when the exposure is the pad's own escape.
- ``same-net pass-through`` -- same net, but no endpoint inside: the track
  passes or bends across the opening without terminating there.
- ``different-net`` -- the conductor's identified net differs from the
  opening owner's.
- ``same-net`` -- same net, but the entry/pass-through distinction does not
  apply, for one of two reasons: the conductor is an area (``zone``) or another
  pad, so it has no terminating endpoint to test; or the opening has no single
  owner pad (a ``merged_region``), so there is no pad to test an endpoint
  against -- a ``segment``/``arc``/``via`` over such an opening has endpoints,
  but they are not judged.
- (omitted) -- the conductor's net is unidentified, or none of the opening's
  contributors has an identified net.

While ``assessment.coverage != "complete"`` the findings are provisional:
severity is capped at ``warning`` (never ``error``) and each message carries
``provisional: coverage incomplete``, so an incomplete run never inflates the
error count. A closing ``mask_to_copper_openings`` info finding summarizes the
measurements per opening, so one pad with many findings reads as one opening.

Copper on the pad's own land (issue #8858)
==========================================

A same-net track that enters its pad lies partly *under* the pad, so the
opening covers that part -- but it is the pad's own exposed land, and the one
track width it puts under mask where it leaves the pad is unavoidable. Such a
measurement's disposition is ``within_own_pad`` and its finding is ``info``.
The class is granted for what the copper *does*, never merely for touching
the pad (``_within_own_pad`` states the conditions): a plain pad shape with a
non-negative margin; the same identified net; a proven direct overlap; nothing
exposed beyond the pad (the construction budget ``u`` tolerated once); and no
copper under mask within the danger clearance beside the opening beyond what
one straight entry at 45 degrees or steeper puts there. Only a straight
segment with at least one authored end strictly inside the pad gets that
allowance (issue #9261). So a track that straddles, runs along or passes
through the pad, a shallower entry, an arc, a via or pour touching the pad
(ring copper beside the opening), a positive expansion ring wider than
``u``, solder-mask-defined, custom and padstack pads, pads with no net, and
any different-net copper all keep their measured verdict.
``exposed_beyond_owner_mm2`` and ``beside_owner_excess_mm2`` record the two
measured quantities. "Same net" is the net pcbnew loads, which assigns a
floating stub touching one pad to that pad's net. Evidence:
``docs/research/mask-to-copper-own-pad-8858.md``.

Copper a reinforce wire run exposes on purpose (issue #9315)
============================================================

``kct pcb reinforce`` (#8743) strips the solder mask along a wire run and
leaves its anchor vias untented; ``reinforcement_incomplete`` (#8746)
requires those openings to be continuous -- and ``mask_to_copper`` then
failed the same board for having them (board-level ``gr_line``/``gr_circle``
openings carry no native net, so the run's own copper measured as an
ordinary violation). When ``<board>.reinforcement.json`` records the runs,
each run's openings are attributed to their sidecar record -- a strip
``gr_line`` piece belongs to the run through mask continuity (#9315): it
lies within the strip tolerance of a recorded anchor point or of a piece
already in the run's chain, seeded at anchors a same-net hole really
stands on -- anchor flashes and end-hole rings sit within the strip
tolerance of a recorded anchor point -- and the measurement's owner net
becomes the run's net instead of ``null``. The run's own track (wholly
under its strip, or entering one of its anchors), its anchor vias, and a
same-net pad standing on the run (the strip passes over its copper, or
the pad is itself an anchor) measure as disposition
``within_reinforce_run`` -- info, like ``within_own_pad``. Copper of a
different net within a strip's clearance band stays an error, and
same-net copper that is not the run's (a neighbouring track the strip
clips, or a trace wholly under an unrelated opening elsewhere on the
board) stays at its current severity: that is the hazard a strip can
actually create. With no sidecar, or no matching record, nothing changes.

A pad's own pour joined by thermal spokes (issue #9284)
=======================================================

A same-net ``zone`` whose only copper under mask beside a plain pad's opening is
that pad's thermal spokes is ``own_pour_thermal`` at info: pcbnew declares
thermal relief between the two, the fill overlaps the pad, nothing is exposed
beyond the pad, and within the examined band (the warning clearance, else the
danger clearance) the fill is 1 to 4 spoke crossings of at most the declared
spoke width and nothing else, so the pour body lies beyond the band
(``_own_pour_thermal``). JLCDFM listed no item for that shape on board 01
(relief gaps 0.2 and 0.3 mm, 0 mm expansion). A solid connection, an
unconnected same-net pour, a pour body inside the band, pour exposed by a
positive expansion and different-net pour keep their measured verdict.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .violations import DRCResults


@dataclass(frozen=True)
class MaskCopperPolicy:
    clearance_mm: float
    source: str
    process: str
    revision: str
    # Optional advisory tier (issue #8842): measurements at or above
    # ``clearance_mm`` but within this distance are warnings, never failures.
    warning_clearance_mm: float | None = None

    def __post_init__(self):
        if (
            isinstance(self.clearance_mm, bool)
            or not math.isfinite(self.clearance_mm)
            or self.clearance_mm < 0
        ):
            raise ValueError("Mask-to-copper clearance must be finite and nonnegative")
        if self.warning_clearance_mm is not None and (
            isinstance(self.warning_clearance_mm, bool)
            or not math.isfinite(self.warning_clearance_mm)
            or self.warning_clearance_mm < self.clearance_mm
        ):
            raise ValueError("Mask-to-copper warning clearance must be finite and >= clearance")
        if any(
            not isinstance(v, str) or not v.strip()
            for v in (self.source, self.process, self.revision)
        ):
            raise ValueError("Mask-to-copper policy requires source, process and revision")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class MaskSourceBinding:
    source_sha256: str
    project_sha256: str | None
    rules_sha256: str | None
    export_profile_sha256: str

    def __post_init__(self):
        import re

        for value in (
            self.source_sha256,
            self.project_sha256,
            self.rules_sha256,
            self.export_profile_sha256,
        ):
            if value is not None and (
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            ):
                raise ValueError("Malformed source binding SHA256")
        if self.source_sha256 is None or self.export_profile_sha256 is None:
            raise ValueError("Source and export binding are required")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class MaskEscapeIntent:
    binding: MaskSourceBinding
    owner_uuid: str
    conductor_uuid: str
    mask_side: str
    rationale: str
    scope: str = "connected_escape"

    def __post_init__(self):
        if (
            self.mask_side not in ("F.Mask", "B.Mask")
            or self.scope != "connected_escape"
            or not isinstance(self.rationale, str)
            or not self.rationale.strip()
        ):
            raise ValueError("Intent needs a mask side, connected_escape scope and rationale")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class MaskCopperMeasurement:
    opening_uuids: tuple[str, ...]
    conductor_uuid: str
    mask_side: str
    copper_layer: str
    location_mm: tuple[float, float]
    clearance_mm: float
    exposed_area_mm2: float
    relation: str
    disposition: str
    uncertainty_mm: float
    opening_kind: str
    mask_defined: bool
    margin_provenance: str
    rationale: str | None = None
    negative_expansion: bool = False
    # Triage metadata (issue #9038). All optional so a v1 payload without
    # them loads unchanged; None means "not determined at measurement time".
    owner_net: str | None = None
    conductor_net: str | None = None
    conductor_kind: str | None = None
    same_net: bool | None = None
    own_pad_entry: bool | None = None
    owner_ref: str | None = None
    # Issue #8858: copper exposed beyond the owner pad's own copper, mm²; None
    # when there is no single pad owner or no overlap. Optional, like the
    # #9038 fields, so a v1 payload without it loads unchanged.
    exposed_beyond_owner_mm2: float | None = None
    # Issue #8858: copper under mask in the danger band beside the opening, in
    # excess of one clean entry crossing (mm²); None when not evaluated.
    beside_owner_excess_mm2: float | None = None
    # Issue #9315: the reinforce wire run that owns this opening, when the
    # opening was attributed to a ``<board>.reinforcement.json`` record.
    # Optional, like the other triage fields, so a v1 payload loads unchanged.
    owner_run: str | None = None
    # Issue #9284: for an ``own_pour_thermal`` measurement, the number of
    # thermal spokes found crossing the examined band, and the pad's declared
    # effective relief gap and spoke width (mm). None otherwise.
    pour_spokes: int | None = None
    pour_relief_gap_mm: float | None = None
    pour_spoke_width_mm: float | None = None

    def __post_init__(self):
        if self.disposition not in (
            "violation",
            "uncertain",
            "intentional",
            "warning",
            "within_own_pad",
            "within_reinforce_run",
            "own_pour_thermal",
        ) or self.relation not in ("overlap", "touching", "disjoint"):
            raise ValueError("Invalid mask measurement disposition/relation")
        if self.mask_side not in (
            "F.Mask",
            "B.Mask",
        ) or self.copper_layer != self.mask_side.replace(".Mask", ".Cu"):
            raise ValueError("Invalid mask/copper side pairing")
        values = (*self.location_mm, self.clearance_mm, self.exposed_area_mm2, self.uncertainty_mm)
        if (
            any(isinstance(v, bool) or not math.isfinite(v) for v in values)
            or min(self.clearance_mm, self.exposed_area_mm2, self.uncertainty_mm) < 0
        ):
            raise ValueError("Invalid mask measurement units/value")
        for value in (
            self.owner_net,
            self.conductor_net,
            self.conductor_kind,
            self.owner_ref,
            self.owner_run,
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError("Invalid mask measurement triage metadata")
        for flag in (self.same_net, self.own_pad_entry):
            if flag is not None and not isinstance(flag, bool):
                raise ValueError("Invalid mask measurement triage metadata")
        for extra in (self.exposed_beyond_owner_mm2, self.beside_owner_excess_mm2):
            if extra is not None and (
                isinstance(extra, bool)
                or not isinstance(extra, (int, float))
                or not math.isfinite(extra)
                or extra < 0
            ):
                raise ValueError("Invalid mask measurement units/value")
        if self.pour_spokes is not None and (
            isinstance(self.pour_spokes, bool)
            or not isinstance(self.pour_spokes, int)
            or self.pour_spokes < 0
        ):
            raise ValueError("Invalid mask measurement units/value")
        for extra in (self.pour_relief_gap_mm, self.pour_spoke_width_mm):
            if extra is not None and (
                isinstance(extra, bool)
                or not isinstance(extra, (int, float))
                or not math.isfinite(extra)
                or extra < 0
            ):
                raise ValueError("Invalid mask measurement units/value")

    def to_dict(self):
        return asdict(self)


@dataclass
class MaskCopperAssessment:
    coverage: str = "not_run"
    reasons: list[str] = field(default_factory=list)
    policy: MaskCopperPolicy | None = None
    binding: MaskSourceBinding | None = None
    measurements: list[MaskCopperMeasurement] = field(default_factory=list)
    intent_audit: list[dict[str, Any]] = field(default_factory=list)
    geometry_provenance: dict[str, Any] = field(default_factory=dict)
    evaluated_pairs: int = 0

    def __post_init__(self):
        if self.coverage not in ("complete", "incomplete", "not_run"):
            raise ValueError("Unknown mask-to-copper coverage status")

    @property
    def remediation(self) -> str | None:
        """What to configure so this assessment can run, when it could not (issue #8949).

        ``None`` for a ``complete`` assessment.  Sourced here so the hint
        travels with the assessment instead of living in CLI formatting --
        and deliberately a property, not a serialized field: ``to_dict()``
        ships the JSON report and its ``schema`` is round-trip validated,
        so a new key there would force a ``kct.mask-copper-assessment.v1``
        -> ``v2`` bump.  Machine consumers already read ``coverage``.
        """
        if self.coverage == "complete":
            return None
        return (
            "run the mask assessment on a qualified KiCad build "
            "(native interpreter): set KCT_KICAD_DOCKER=1 to use the pinned "
            "image, or install a qualified kicad-cli, then re-run"
        )

    @property
    def passed(self):
        return (
            not self.reasons
            and self.policy is not None
            and self.binding is not None
            and self.coverage == "complete"
            and not any(m.disposition in ("violation", "uncertain") for m in self.measurements)
        )

    @classmethod
    def from_dict(cls, data):
        if data.get("schema") != "kct.mask-copper-assessment.v1":
            raise ValueError("Unknown mask assessment schema")
        return cls(
            coverage=data["coverage"],
            reasons=list(data["reasons"]),
            policy=MaskCopperPolicy(**data["policy"]) if data["policy"] else None,
            binding=MaskSourceBinding(**data["binding"]) if data["binding"] else None,
            measurements=[
                MaskCopperMeasurement(
                    **{
                        **m,
                        "opening_uuids": tuple(m["opening_uuids"]),
                        "location_mm": tuple(m["location_mm"]),
                    }
                )
                for m in data["measurements"]
            ],
            intent_audit=list(data["intent_audit"]),
            geometry_provenance=dict(data["geometry_provenance"]),
            evaluated_pairs=data["evaluated_pairs"],
        )

    def to_dict(self):
        return {
            "schema": "kct.mask-copper-assessment.v1",
            "coverage": self.coverage,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "policy": self.policy.to_dict() if self.policy else None,
            "binding": self.binding.to_dict() if self.binding else None,
            "measurements": [m.to_dict() for m in self.measurements],
            "intent_audit": self.intent_audit,
            "geometry_provenance": self.geometry_provenance,
            "evaluated_pairs": self.evaluated_pairs,
        }


def source_binding(geometry):
    """Stable identity of immutable inputs and geometry-affecting native policy."""
    import hashlib
    import json

    exported = geometry.exported
    profile = {k: v for k, v in exported.export_identity.items() if k not in ("stdout", "stderr")}
    profile["native_version"] = exported.native_version
    profile["object_reader"] = {
        k: v for k, v in geometry.provenance.items() if k not in ("objects_sha256",)
    }
    return MaskSourceBinding(
        exported.source_sha256,
        exported.project_sha256,
        exported.rules_sha256,
        hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest(),
    )


def _conductor_endpoints(copper):
    """The two farthest-apart boundary points of a track conductor's copper.

    A stroked track plots as a rounded capsule, so this pair is the track's
    endpoint caps: the source segment's endpoints themselves are not carried
    by the object geometry. O(n²) over the few boundary vertices; callers
    cache per conductor. ``None`` for a degenerate (single-point) shape.
    """
    from shapely.geometry import MultiPoint  # type: ignore[import-untyped]

    polygons = list(copper.geoms) if copper.geom_type == "MultiPolygon" else [copper]
    points = [tuple(p) for polygon in polygons for p in polygon.exterior.coords]
    if len(points) < 2:
        return None
    best_pair = (points[0], points[1])
    best_square = -1.0
    for position, first in enumerate(points):
        for second in points[position + 1 :]:
            square = (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2
            if square > best_square:
                best_square = square
                best_pair = (first, second)
    return MultiPoint(best_pair)


#: Owner pad shapes whose plotted opening the within-own-pad class has been
#: checked against (issue #8858). Custom, padstack and chamfered pads, and any
#: shape not listed, keep their measured verdict.
_WITHIN_OWN_PAD_OWNER_KINDS = ("pad:rect", "pad:roundrect", "pad:circle", "pad:oval")

#: One straight track crossing the pad edge at 45 degrees or steeper puts at
#: most sqrt(2) x width x clearance of copper under mask in the band beside the
#: opening (exactly width x clearance when perpendicular). 1.45 = sqrt(2) plus
#: 2.5 % for the polygonal approximation of the capsule and the pad outline.
_ENTRY_CROSSING_FACTOR = 1.45

#: Areas at or below this are numerical zero (mm²), not a tolerance.
_AREA_EPSILON_MM2 = 1e-9


def _within_own_pad(opening, copper, owner_copper, owner, item, margin, clearance, uncertainty):
    """``(qualifies, beside_excess_mm2)`` for the within-own-pad class (issue #8858).

    A same-net conductor qualifies only when **what it does** is a clean entry
    into its own pad. Every condition is required:

    1. The owner is a plain pad shape (``_WITHIN_OWN_PAD_OWNER_KINDS``) with a
       non-negative mask margin. Solder-mask-defined openings fail closed.
    2. Direct connection: the conductor's copper and the pad's copper overlap
       beyond the construction budget ``u`` (both shrunk by ``u``).
    3. Nothing exposed beyond the pad: ``opening ∩ copper − pad.buffer(u)`` is
       empty. ``u`` is tolerated once and only once, so any copper crossing an
       expansion ring wider than ``u`` keeps its verdict.
    4. Nothing beside the pad beyond a clean entry. The band copper is
       ``B = (copper − opening.buffer(u)) ∩ opening.buffer(clearance + u)``,
       the conductor's copper under mask within the danger clearance of the
       opening. Its allowance ``A`` is zero, except for a straight segment
       with at least one authored endpoint strictly inside the pad: one entry
       crossing, ``A = 1.45 × width × clearance`` (the other end may exit, sit
       on the edge or be inside too).
       The finding qualifies only if ``B − A`` (returned as the excess) is
       numerically zero. So a track that straddles, runs along or passes
       through the pad, an entry shallower than about 45 degrees, a via or
       pour with ring copper beside the pad, and arcs all keep their verdict.
    5. For a via, its authored drill lies wholly outside the opening.
    """
    if (
        owner["kind"] not in _WITHIN_OWN_PAD_OWNER_KINDS
        or margin < 0
        or owner_copper.buffer(-uncertainty).intersection(copper.buffer(-uncertainty)).area <= 0
    ):
        return False, None
    if (
        opening.intersection(copper).difference(owner_copper.buffer(uncertainty)).area
        > _AREA_EPSILON_MM2
    ):
        return False, None
    band = (
        copper.difference(opening.buffer(uncertainty))
        .intersection(opening.buffer(clearance + uncertainty))
        .area
    )
    allowance = 0.0
    if item["kind"] == "segment":
        from shapely.geometry import Point

        width = item.get("width_mm")
        ends = item.get("endpoints_mm")
        if not width or not ends:
            return False, None
        core = owner_copper.buffer(-uncertainty)
        if not any(core.contains(Point(p)) for p in ends):
            # Pass-through, straddle or along-edge copper: not an entry.
            return False, max(0.0, band)
        # At least one authored end is strictly inside the pad. The budget is
        # one entry crossing whether the other end exits, lies on the edge or
        # is also inside, so an end cap poking a hair past the edge never
        # scores worse than a full exit (issue #9261). B is still measured
        # from the real copper, so the excess check keeps gating.
        allowance = _ENTRY_CROSSING_FACTOR * width * clearance
    excess = max(0.0, band - allowance)
    if excess > _AREA_EPSILON_MM2:
        return False, excess
    if item["kind"] == "via":
        drill = item.get("drill_mm")
        if drill is None or drill <= 0:
            return False, excess
        if opening.distance(copper.centroid) < drill / 2 + uncertainty:
            return False, excess
    return True, excess


@dataclass
class _ReinforceRun:
    """One ``<board>.reinforcement.json`` record resolved for attribution (#9315).

    Sidecar coordinates are sheet-absolute and the native object inventory
    reads the same file frame, so the record's anchors and net attribute
    openings directly. ``confirmed_anchors`` are the recorded anchors a
    same-net hole really stands on (copper within the strip tolerance of
    the point): a stale record, or one naming another net's anchor, cannot
    claim the openings there. ``anchors`` itself stays complete for the
    conductor tests. A run claims openings only through mask continuity
    with those confirmed anchors -- never through the net alone, so an
    unrelated ``gr_line`` opening over the same net elsewhere on the board
    attributes to nothing (#9315).
    """

    label: str
    net: str
    layer: str
    mask_side: str
    anchors: tuple[tuple[float, float], ...]
    confirmed_anchors: tuple[tuple[float, float], ...]


def _reinforce_run_label(record: dict[str, Any]) -> str:
    """Human name for a wire record: its current path, else its leg endpoints."""
    path = record.get("path")
    if isinstance(path, str) and path.strip():
        return path
    paths = record.get("paths")
    if isinstance(paths, list):
        names = [p for p in paths if isinstance(p, str) and p.strip()]
        if names:
            return names[0]
    net = record.get("net")
    if not isinstance(net, str):
        net = "?"
    start, end = record.get("leg_start"), record.get("leg_end")
    if isinstance(start, dict) and isinstance(end, dict):
        try:
            return f"{net} leg ({start['x']:.2f},{start['y']:.2f})-({end['x']:.2f},{end['y']:.2f})"
        except (TypeError, KeyError, ValueError):
            pass
    return net


def _reinforce_runs(objects: dict[str, Any], wires: Any) -> list[_ReinforceRun]:
    """Resolve wire records against the native inventory; empty when unusable.

    A record needs a net, at least one anchor and an outer copper layer (an
    inner layer has no strip to attribute). Malformed records are skipped,
    never fail the assessment (#8886 resolves the same records for
    ``reinforcement_incomplete``; this is the mask side of that split).
    """
    if not wires:
        return []
    from shapely.geometry import Point

    from kicad_tools.pcb.reinforce import MASK_LAYER_FOR_COPPER

    from .rules.reinforcement_incomplete import MASK_TOLERANCE_MM

    runs: list[_ReinforceRun] = []
    for record in wires:
        if not isinstance(record, dict):
            continue
        net = record.get("net")
        layer = record.get("layer")
        anchors = []
        for anchor in record.get("anchors") or []:
            try:
                anchors.append((float(anchor["x"]), float(anchor["y"])))
            except (TypeError, KeyError, ValueError):
                continue
        if not isinstance(net, str) or not net or not isinstance(layer, str):
            continue
        mask_side = MASK_LAYER_FOR_COPPER.get(layer)
        if mask_side is None or not anchors:
            continue
        net_copper: list[Any] = []
        for item in objects.values():
            if item.get("net") != net:
                continue
            copper = item.get("layers_geometry", {}).get(layer)
            if copper is None or copper.is_empty:
                continue
            net_copper.append(copper)
        confirmed: tuple[tuple[float, float], ...] = ()
        if net_copper:
            from shapely.ops import unary_union  # type: ignore[import-untyped]

            union = unary_union(net_copper)
            confirmed = tuple(
                anchor for anchor in anchors if union.distance(Point(anchor)) <= MASK_TOLERANCE_MM
            )
        runs.append(
            _ReinforceRun(
                label=_reinforce_run_label(record),
                net=net,
                layer=layer,
                mask_side=mask_side,
                anchors=tuple(anchors),
                confirmed_anchors=confirmed,
            )
        )
    return runs


def _attribute_reinforce_openings(runs, openings, objects, uncertainty):
    """Attribute mask openings of one side to runs; ``(uuid -> run, id -> strip union)``.

    An opening belongs to a run when its convex hull lies within the strip
    tolerance of one of the run's *confirmed* anchor points -- the hull, not
    the material, so a ring-shaped end-hole opening that surrounds its
    anchor still matches; the anchor is corroborated by same-net copper
    standing on it, so a record naming another net's anchor (or a stale one)
    claims nothing. A ``gr_line`` strip piece that runs far between the
    anchors belongs to the run through *mask continuity*: the strip (#8743)
    is one continuous cutout from anchor to anchor, so a piece joins the
    run's chain when its hull lies within the strip tolerance of a piece
    already in the chain, seeded by the pieces standing on the confirmed
    anchors. An opening that is merely over the same net's copper elsewhere
    on the board is not continuous with any anchor flash and attributes to
    nothing (#9315). An anchor-flash ``via`` must also carry the run's net;
    board graphics have none to check. Ties between records of one net go
    to the run with the nearest anchor, first recorded winning a tie, so
    attribution is deterministic. ``merged_region`` openings attribute
    through their contributors, which are attributed individually before
    the merge.
    """
    from shapely.geometry import Point
    from shapely.ops import unary_union

    from .rules.reinforcement_incomplete import MASK_TOLERANCE_MM

    tolerance = MASK_TOLERANCE_MM + uncertainty
    candidates: dict[str, list[tuple[_ReinforceRun, float]]] = {}
    strip_materials: dict[str, Any] = {}
    for owners, material, kind, *_ in openings:
        if kind == "merged_region" or material is None or material.is_empty:
            continue
        uuid = owners[0]
        if kind == "gr_line":
            strip_materials[uuid] = material
        for run in runs:
            if not run.confirmed_anchors:
                continue
            if kind == "via":
                owner = objects.get(uuid) if len(owners) == 1 else None
                if (owner.get("net") if owner else "") != run.net:
                    continue
            distance = min(material.convex_hull.distance(Point(a)) for a in run.confirmed_anchors)
            if distance <= tolerance:
                candidates.setdefault(uuid, []).append((run, distance))

    # Grow each run's strip chain to a fixpoint: a piece whose hull lies
    # within the strip tolerance of a confirmed anchor -- or of an already
    # admitted piece -- joins the chain and reaches further pieces.
    for run in runs:
        if not run.confirmed_anchors:
            continue
        anchor_points = [Point(a) for a in run.confirmed_anchors]
        admitted_hulls: list[Any] = []
        pending = list(strip_materials.items())
        while pending:
            remaining = []
            progressed = False
            for uuid, material in pending:
                hull = material.convex_hull
                near = any(hull.distance(point) <= tolerance for point in anchor_points) or any(
                    hull.distance(other) <= tolerance for other in admitted_hulls
                )
                if not near:
                    remaining.append((uuid, material))
                    continue
                if not any(r is run for r, _ in candidates.get(uuid, ())):
                    candidates.setdefault(uuid, []).append((run, float("inf")))
                admitted_hulls.append(hull)
                progressed = True
            pending = remaining
            if not progressed:
                break

    run_by_uuid: dict[str, _ReinforceRun] = {}
    pieces: dict[int, list[Any]] = {}
    for uuid, pairs in candidates.items():
        best = None
        best_distance = None
        for run, distance in pairs:
            if best_distance is None or distance < best_distance:
                best, best_distance = run, distance
        if best is None:
            continue
        run_by_uuid[uuid] = best
        material = strip_materials.get(uuid)
        if material is not None:
            pieces.setdefault(id(best), []).append(material)
    strips = {identity: unary_union(found) for identity, found in pieces.items()}
    return run_by_uuid, strips


def _conductor_on_reinforce_run(run, item, copper, opening, strip_union, uncertainty) -> bool:
    """``True`` when the conductor is copper the run exposes on purpose (#9315).

    The run's own track -- the strip lies along it (the opening is covered by
    this segment's copper), or the segment is wholly under the strip union,
    or the segment reaches one of the run's anchors --, its anchor vias, and
    a same-net pad standing on the run (the strip passes over its copper, or
    the pad is itself an anchor) qualify. Everything else keeps its measured
    verdict: different-net copper, a zone, an arc, and same-net copper off
    the run (a neighbour the strip clips pokes past it, so neither
    containment holds).
    """
    from shapely.geometry import Point

    from .rules.reinforcement_incomplete import ANCHOR_ON_LEG_SLACK_MM, MASK_TOLERANCE_MM

    if (item.get("net") or "") != run.net:
        return False
    kind = item["kind"]
    anchor_points = [Point(a) for a in run.anchors]
    if kind == "via":
        return any(
            copper.centroid.distance(point) <= MASK_TOLERANCE_MM + uncertainty
            for point in anchor_points
        )
    if kind == "segment":
        if (
            opening is not None
            and not opening.is_empty
            and opening.difference(copper.buffer(uncertainty)).area <= _AREA_EPSILON_MM2
        ):
            return True
        if strip_union is not None and (
            copper.difference(strip_union.buffer(MASK_TOLERANCE_MM + uncertainty)).area
            <= _AREA_EPSILON_MM2
        ):
            return True
    elif kind.startswith("pad"):
        if strip_union is not None and copper.intersection(strip_union).area > 0:
            return True
    else:
        return False
    # The track reaching an anchor, or a through-hole pad standing at one:
    # both are the run's own copper at its anchor even past the strip's end.
    return any(
        copper.buffer(ANCHOR_ON_LEG_SLACK_MM + uncertainty).contains(point)
        for point in anchor_points
    )


#: pcbnew ZONE_CONNECTION codes (black-box read on the pinned image, #9284).
_ZONE_CONNECTION_INHERITED = -1
_ZONE_CONNECTION_THERMAL = 1
_ZONE_CONNECTION_THT_THERMAL = 3

#: pcbnew draws at most four thermal spokes per pad.
_MAX_THERMAL_SPOKES = 4


def _declared_thermal(owner, zone):
    """``(relief_gap_mm, spoke_width_mm)`` when the pad declares thermal relief to *zone*.

    Precedence as pcbnew stores it: the pad's own zone-connection override,
    else its footprint's, else the zone's. Thermal relief only (or
    "thermal for through-hole pads" on a plated through-hole pad); a solid,
    none, or unknown connection returns None. Gap and spoke width are the
    pad's overrides when set, else the zone's. Custom ``zone_connection``
    DRC rules never get here: they make the native run incomplete.
    """
    pad = owner.get("zone_connection")
    pour = zone.get("zone_connection")
    if not isinstance(pad, dict) or not isinstance(pour, dict):
        return None
    if "pad" not in pad or "pad_connection" not in pour:
        return None
    effective = pad["pad"]
    if effective == _ZONE_CONNECTION_INHERITED:
        effective = pad["footprint"]
    if effective == _ZONE_CONNECTION_INHERITED:
        effective = pour["pad_connection"]
    thermal = effective == _ZONE_CONNECTION_THERMAL or (
        effective == _ZONE_CONNECTION_THT_THERMAL and pad["plated_through"]
    )
    if not thermal:
        return None
    gap = pad["thermal_gap_mm"] if pad["thermal_gap_mm"] is not None else pour["thermal_gap_mm"]
    spoke = (
        pad["thermal_spoke_width_mm"]
        if pad["thermal_spoke_width_mm"] is not None
        else pour["thermal_spoke_width_mm"]
    )
    if not gap or not spoke or gap <= 0 or spoke <= 0:
        return None
    return gap, spoke


def _own_pour_thermal(opening, copper, owner_copper, owner, item, margin, band, uncertainty):
    """``(qualifies, spokes, excess_mm2, gap_mm, spoke_mm)`` for issue #9284.

    The fab evidence (#8858, JLCDFM on board 01) covers a pad joined to its
    own same-net pour by **thermal spokes** at 0 mm mask expansion, with the
    pour body a relief gap (0.2 and 0.3 mm there) away. It excuses exactly
    that, and every condition is required:

    1. The owner is a plain pad shape with a non-negative mask margin.
    2. The pad declares thermal relief to this zone (``_declared_thermal``,
       pcbnew's own connection data).
    3. Direct connection: the fill overlaps the pad beyond ``u``.
    4. Nothing exposed beyond the pad: ``opening ∩ fill − pad.buffer(u)`` is
       empty, so a positive expansion with spoke copper in its ring fails.
    5. Within ``band`` of the opening (the rule's whole examined reach: the
       warning clearance, or the danger clearance without one) the fill is
       spokes and nothing else. Each connected piece of
       ``(fill − opening.buffer(u)) ∩ opening.buffer(band + u)`` must touch
       the pad, run out to the band's outer edge, meet each of the band's two
       edges along no more than ``1.45 × spoke_width`` (so pour body lying
       along the band's outer edge never rides in on a spoke), not wrap
       around the pad, and hold at most ``1.45 × spoke_width × band`` (one
       perpendicular crossing, 1.45 as for track entries); there are 1 to 4
       of them. Any
       other copper in the band -- the pour body nearer than ``band``, an
       island, a wider spoke -- is the returned excess and fails the class.

    So the excused copper is the spokes, each crossing the pad edge, and
    nothing else; the pour body must lie beyond ``band`` of the opening.
    """
    declared = _declared_thermal(owner, item)
    if (
        owner["kind"] not in _WITHIN_OWN_PAD_OWNER_KINDS
        or margin < 0
        or declared is None
        or owner_copper.buffer(-uncertainty).intersection(copper.buffer(-uncertainty)).area <= 0
        or opening.intersection(copper).difference(owner_copper.buffer(uncertainty)).area
        > _AREA_EPSILON_MM2
    ):
        return False, None, None, None, None
    gap, spoke = declared
    inner = opening.buffer(uncertainty)
    outer = opening.buffer(band + uncertainty)
    ring = copper.difference(inner).intersection(outer)
    pieces = [
        piece
        for piece in (getattr(ring, "geoms", None) or [ring])
        if piece.geom_type in ("Polygon", "MultiPolygon") and piece.area > _AREA_EPSILON_MM2
    ]
    pieces = [p for piece in pieces for p in (getattr(piece, "geoms", None) or [piece])]
    tolerance = max(uncertainty, 1e-6)
    cap = _ENTRY_CROSSING_FACTOR * spoke * band
    # A spoke meets each edge of the band across its own width only. Pour
    # body inside the band lies *along* the outer edge, so a piece whose
    # contact with either edge is longer than one crossing is not a spoke,
    # however little area the body adds to it.
    contact_cap = _ENTRY_CROSSING_FACTOR * spoke + 4 * tolerance
    inner_edge = inner.boundary.buffer(tolerance)
    outer_edge = outer.boundary.buffer(tolerance)
    centre = opening.representative_point()
    spokes = 0
    excess = 0.0
    for piece in pieces:
        if (
            piece.area <= cap + _AREA_EPSILON_MM2
            and piece.distance(inner) <= tolerance
            and piece.distance(outer.boundary) <= tolerance
            and piece.boundary.intersection(inner_edge).length <= contact_cap
            and piece.boundary.intersection(outer_edge).length <= contact_cap
            and not piece.convex_hull.contains(centre)
        ):
            spokes += 1
        else:
            excess += piece.area
    if spokes > _MAX_THERMAL_SPOKES:
        excess = sum(piece.area for piece in pieces)
    qualifies = spokes >= 1 and excess <= _AREA_EPSILON_MM2
    return qualifies, spokes, excess, gap, spoke


def assess_attributed_geometry(geometry, policy, intents=(), reinforce_wires=()):
    """Measure actual native mask against independently retained source copper.

    Distances are millimetres; exposure is area in square millimetres, never
    mislabeled as penetration depth. Exact-limit measurements remain uncertain
    within the native/Gerber construction budget, and cannot certify a pass.

    ``reinforce_wires`` are the ``<board>.reinforcement.json`` records of the
    board's reinforce runs (issue #9315); their openings attribute to the run
    and the run's own copper measures as info (see the module docstring).
    """
    # The shapely.geometry stubs error is consumed at the first import site
    # (_conductor_endpoints above); a second ignore here would read as unused.
    from shapely import STRtree  # type: ignore[import-untyped]
    from shapely.geometry import GeometryCollection, box
    from shapely.ops import nearest_points, unary_union

    binding = source_binding(geometry)
    result = MaskCopperAssessment(
        coverage="complete" if not geometry.errors else "incomplete",
        reasons=list(geometry.errors),
        policy=policy,
        binding=binding,
        geometry_provenance={"export": geometry.exported.to_dict(), "objects": geometry.provenance},
    )
    result.geometry_provenance["source_objects"] = {
        identity: {
            "kind": item["kind"],
            "net": item["net"],
            "margin_mm": item["margin_mm"],
            "margin_source": item.get("margin_source", "native effective expansion"),
            "layers_wkt": {layer: shape.wkt for layer, shape in item["layers_geometry"].items()},
        }
        for identity, item in geometry.objects.items()
    }
    # The native plot construction error comes from the captured board's
    # m_MaxError (typically 5um for custom/chamfer polygons). Both the mask
    # contribution and the adjacent copper may be independently constructed,
    # so add both bounds, then 4nm for Gerber reconstruction/rounding.
    # This is uncertainty, never a process clearance allowance.
    uncertainty = 2 * geometry.provenance.get("max_error_mm_per_construction", 0) + 0.000004
    approved = {}
    for intent in intents:
        owner = geometry.objects.get(intent.owner_uuid)
        conductor = geometry.objects.get(intent.conductor_uuid)
        layer = intent.mask_side.replace(".Mask", ".Cu")
        reason = None
        if intent.binding != binding:
            reason = "stale source/export binding"
        elif not owner or not conductor or intent.owner_uuid == intent.conductor_uuid:
            reason = "missing or invalid source identity"
        elif not owner["kind"].startswith("pad:") or conductor["kind"] not in (
            "segment",
            "arc",
            "via",
            "zone",
        ):
            reason = "intent is limited to a pad and a directly connected conductor"
        elif (
            intent.mask_side not in owner["layers_geometry"]
            or layer not in conductor["layers_geometry"]
        ):
            reason = "wrong side or conductor layer"
        elif not owner["net"] or owner["net"] != conductor["net"]:
            reason = "connection must share an identified net"
        else:
            a = owner["layers_geometry"].get(layer, GeometryCollection())
            b = conductor["layers_geometry"][layer]
            if a.buffer(-uncertainty).intersection(b.buffer(-uncertainty)).area <= 0:
                reason = "direct physical owner-to-conductor connection unproven"
        result.intent_audit.append(
            {
                "declaration": intent.to_dict(),
                "accepted": reason is None,
                "reason": reason or "source-bound direct physical connection verified",
            }
        )
        if reason is None:
            approved[(intent.owner_uuid, intent.conductor_uuid, intent.mask_side)] = intent
    runs = _reinforce_runs(geometry.objects, reinforce_wires)
    for side in ("F.Mask", "B.Mask"):
        layer = side.replace(".Mask", ".Cu")
        native = geometry.exported.layers.get(side)
        if native is None:
            continue
        openings = []
        for identity, item in geometry.objects.items():
            shape = item["layers_geometry"].get(side)
            if shape is None or shape.is_empty:
                continue
            if not shape.difference(native.buffer(uncertainty)).is_empty:
                result.reasons.append(
                    f"{identity}/{side}: object mask exceeds native plotted material"
                )
                result.coverage = "incomplete"
            # Intersect authoritative material with the error-bounded native
            # object footprint. This captures native plotting approximation
            # without inventing mask outside the exported layer.
            material = native.intersection(shape.buffer(uncertainty))
            openings.append(
                (
                    (identity,),
                    material,
                    item["kind"],
                    item["margin_mm"].get(side, 0),
                    item.get("margin_source", "native effective expansion"),
                )
            )
        covered = unary_union([entry[1] for entry in openings])
        residual = native.difference(covered)
        parts = list(residual.geoms) if hasattr(residual, "geoms") else [residual]
        components = list(native.geoms) if hasattr(native, "geoms") else [native]
        for part in parts:
            if part.is_empty:
                continue
            component = next((c for c in components if c.intersects(part)), None)
            contributors = tuple(
                sorted(
                    identity
                    for identity, item in geometry.objects.items()
                    if side in item["layers_geometry"]
                    and component is not None
                    and component.intersection(item["layers_geometry"][side]).area > 0
                )
            )
            if len(contributors) < 2:
                result.coverage = "incomplete"
                result.reasons.append(f"{side}: unattributed native mask region")
            # These UUIDs are contributors to the containing native component,
            # not a claim that one source owns the newly merged bridge.
            openings.append(
                (
                    contributors,
                    part,
                    "merged_region",
                    0,
                    "native merged region; containing-component contributors",
                )
            )
        # Issue #9315: attribute this side's openings to the reinforce runs
        # before measuring, so the run's own copper can be classified below.
        side_runs = [run for run in runs if run.mask_side == side]
        run_by_uuid, run_strips = (
            _attribute_reinforce_openings(side_runs, openings, geometry.objects, uncertainty)
            if side_runs
            else ({}, {})
        )
        # Every opening is measured against every object's copper on this layer.
        # That unindexed all-pairs scan is what dominates the check on a real
        # board (issue #8901): board 03 alone reaches ~149k shapely ``distance``
        # calls, and over 99% of them only prove the pair lies beyond
        # ``outer_limit``. A bounding-box index settles that proof without the
        # exact distance -- a copper whose envelope is further than the reach
        # from the opening's envelope cannot be nearer than the reach in truth,
        # which is precisely the ``continue`` below. Pairs the index rules out
        # are still counted in ``evaluated_pairs``, so the reported provenance
        # describes the same comparison set as before.
        outer_limit = (
            policy.warning_clearance_mm
            if policy.warning_clearance_mm is not None
            else policy.clearance_mm
        )
        reach = outer_limit + uncertainty
        conductors = []
        for identity, item in geometry.objects.items():
            copper = item["layers_geometry"].get(layer)
            if copper is None or copper.is_empty:
                continue
            conductors.append((identity, item, copper))
        conductor_ids = {entry[0] for entry in conductors}
        index = STRtree([entry[2] for entry in conductors]) if conductors else None
        endpoint_cache: dict[str, Any] = {}
        for owners, opening, kind, margin, provenance in openings:
            if index is None:
                continue
            # Only a single-owner, non-merged opening excludes its own UUID.
            excluded = owners[0] if kind != "merged_region" and len(owners) == 1 else None
            result.evaluated_pairs += len(conductors) - (
                1 if excluded is not None and excluded in conductor_ids else 0
            )
            candidates: list[int]
            if opening.is_empty:
                # ``bounds`` is undefined for an empty geometry; fall back to the
                # exact comparison rather than guessing at an envelope.
                candidates = list(range(len(conductors)))
            else:
                minx, miny, maxx, maxy = opening.bounds
                candidates = sorted(
                    index.query(box(minx - reach, miny - reach, maxx + reach, maxy + reach))
                )
            for position in candidates:
                identity, item, copper = conductors[position]
                if identity == excluded:
                    continue
                distance = opening.distance(copper)
                if distance > reach:
                    continue
                intersection = opening.intersection(copper)
                area = intersection.area
                certain_exposure = (
                    opening.buffer(-uncertainty).intersection(copper.buffer(-uncertainty)).area > 0
                )
                relation = "overlap" if area > 0 else "touching" if distance == 0 else "disjoint"
                if certain_exposure or distance < policy.clearance_mm - uncertainty:
                    disposition = "violation"
                elif distance > policy.clearance_mm + uncertainty:
                    disposition = "warning"
                else:
                    disposition = "uncertain"
                # Triage metadata (issue #9038): who owns the opening, what
                # copper is exposed, and whether the exposure is the pad's
                # own escape or an unrelated crossing. Never gates the
                # measurement itself -- the comparison set stays all objects.
                owner_items = [geometry.objects[o] for o in owners]
                owner_nets = sorted({o["net"] for o in owner_items if o["net"]})
                conductor_net = item["net"] or None
                same_net = (
                    True
                    if conductor_net and conductor_net in owner_nets
                    else False
                    if conductor_net and owner_nets
                    else None
                )
                single_pad_owner = (
                    kind != "merged_region"
                    and len(owner_items) == 1
                    and owner_items[0]["kind"].startswith("pad:")
                )
                # Issue #8858: copper the opening exposes *beyond the owner
                # pad's own copper*. A same-net conductor joined to the pad
                # lies partly under the pad; that part is the pad's land, not
                # exposed trace. Recorded for every single-pad-owner overlap.
                beyond_owner = None
                owner_copper = (
                    owner_items[0]["layers_geometry"].get(layer) if single_pad_owner else None
                )
                if area > 0 and owner_copper is not None and not owner_copper.is_empty:
                    beyond_owner = intersection.difference(owner_copper).area
                beside_excess = None
                if (
                    disposition in ("violation", "uncertain")
                    and same_net
                    and owner_copper is not None
                    and not owner_copper.is_empty
                ):
                    qualifies, beside_excess = _within_own_pad(
                        opening,
                        copper,
                        owner_copper,
                        owner_items[0],
                        item,
                        margin,
                        policy.clearance_mm,
                        uncertainty,
                    )
                    if qualifies:
                        disposition = "within_own_pad"
                pour_spokes = pour_gap = pour_spoke = None
                if (
                    disposition in ("violation", "uncertain")
                    and same_net
                    and item["kind"] == "zone"
                    and owner_copper is not None
                    and not owner_copper.is_empty
                ):
                    # Issue #9284: a pad tied to its own pour by thermal
                    # spokes, the pour body beyond the examined band.
                    thermal, spokes, pour_excess, gap, spoke = _own_pour_thermal(
                        opening,
                        copper,
                        owner_copper,
                        owner_items[0],
                        item,
                        margin,
                        outer_limit,
                        uncertainty,
                    )
                    if thermal:
                        disposition = "own_pour_thermal"
                        beside_excess = pour_excess
                        pour_spokes, pour_gap, pour_spoke = spokes, gap, spoke
                intent = (
                    approved.get((owners[0], identity, side))
                    if len(owners) == 1 and kind != "merged_region"
                    else None
                )
                if intent:
                    disposition = "intentional"
                owner_ref = owner_items[0].get("pad_ref") if single_pad_owner else None
                own_pad_entry = None
                if same_net and single_pad_owner and item["kind"] in ("segment", "arc", "via"):
                    # Endpoint within the opening = the conductor terminates
                    # at its own pad (own-pad entry); otherwise it merely
                    # passes or bends through (pass-through). A via has no
                    # endpoint; its center stands in for one (docstring).
                    if item["kind"] == "via":
                        endpoint = copper.centroid
                    else:
                        endpoint = endpoint_cache.get(identity)
                        if endpoint is None:
                            endpoint = _conductor_endpoints(copper)
                            endpoint_cache[identity] = endpoint
                    own_pad_entry = (
                        bool(opening.intersects(endpoint)) if endpoint is not None else False
                    )
                # Issue #9315: copper that a reinforce wire run exposes on
                # purpose is not an error. The opening is attributed to its
                # sidecar record; the run's own track, anchor vias and
                # same-net pads on the run become info. Different-net copper,
                # and same-net copper off the run, keep their measured
                # verdict -- and the attributed opening names the owning net
                # and run either way, where the bare graphic carried none.
                attributed_run = next((run_by_uuid[u] for u in owners if u in run_by_uuid), None)
                owner_run = None
                if attributed_run is not None:
                    owner_run = attributed_run.label
                    owner_nets = [attributed_run.net]
                    same_net = (
                        True
                        if conductor_net == attributed_run.net
                        else False
                        if conductor_net
                        else None
                    )
                    if disposition in (
                        "violation",
                        "uncertain",
                        "warning",
                    ) and _conductor_on_reinforce_run(
                        attributed_run,
                        item,
                        copper,
                        opening,
                        run_strips.get(id(attributed_run)),
                        uncertainty,
                    ):
                        disposition = "within_reinforce_run"
                point = (
                    intersection.representative_point()
                    if area > 0
                    else nearest_points(opening, copper)[0]
                )
                result.measurements.append(
                    MaskCopperMeasurement(
                        owners,
                        identity,
                        side,
                        layer,
                        (point.x, point.y),
                        distance,
                        area,
                        relation,
                        disposition,
                        uncertainty,
                        kind,
                        margin < 0
                        and len(owners) == 1
                        and opening.area
                        < geometry.objects[owners[0]]["layers_geometry"]
                        .get(layer, GeometryCollection())
                        .area
                        - uncertainty * opening.length,
                        provenance,
                        intent.rationale if intent else None,
                        margin < 0,
                        owner_net=owner_nets[0] if len(owner_nets) == 1 else None,
                        conductor_net=conductor_net,
                        conductor_kind=item["kind"],
                        same_net=same_net,
                        own_pad_entry=own_pad_entry,
                        owner_ref=owner_ref,
                        exposed_beyond_owner_mm2=beyond_owner,
                        beside_owner_excess_mm2=beside_excess,
                        owner_run=owner_run,
                        pour_spokes=pour_spokes,
                        pour_relief_gap_mm=pour_gap,
                        pour_spoke_width_mm=pour_spoke,
                    )
                )
    return result


def check_mask_to_copper(path, policy=None, intents=(), reinforce_wires=None, **native_options):
    """Public immutable-source checker. Missing policy/native coverage never passes.

    ``reinforce_wires`` are the ``<board>.reinforcement.json`` records naming
    the board's reinforce runs (issue #9315); ``None`` loads the sidecar from
    ``<path>.reinforcement.json`` when it exists, so no caller plumbing is
    needed.
    """
    if policy is None:
        return MaskCopperAssessment(
            reasons=["An explicit process-specific mask-to-copper policy is required"]
        )
    if reinforce_wires is None:
        from kicad_tools.pcb.reinforcement_sidecar import load_wires_if_present

        try:
            reinforce_wires = load_wires_if_present(Path(path))
        except Exception:  # pragma: no cover - contractually silent on bad files
            reinforce_wires = []
    from .mask_copper_geometry import inspect_attributed_mask_geometry

    try:
        geometry = inspect_attributed_mask_geometry(path, **native_options)
        return assess_attributed_geometry(geometry, policy, intents, reinforce_wires)
    except Exception as exc:
        return MaskCopperAssessment(coverage="incomplete", policy=policy, reasons=[str(exc)])


@dataclass
class MaskCopperRequest:
    policy: MaskCopperPolicy | None = None
    intents: tuple[MaskEscapeIntent, ...] = ()
    native_options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path):
        import json
        from pathlib import Path

        from .mask_export_geometry import MaskExportOptions

        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict) or data.get("schema") != "kct.mask-copper-request.v1":
            raise ValueError("Expected kct.mask-copper-request.v1 schema")
        policy = MaskCopperPolicy(**data["policy"]) if data.get("policy") is not None else None
        intents = []
        for declaration in data.get("intents", []):
            declaration = dict(declaration)
            declaration["binding"] = MaskSourceBinding(**declaration["binding"])
            intents.append(MaskEscapeIntent(**declaration))
        native = dict(data.get("native", {}))
        allowed = {
            "native_command",
            "native_python_command",
            "options",
            "artifact_dir",
            "scratch_dir",
        }
        if set(native) - allowed:
            raise ValueError("Unknown native mask request options")
        for key in ("native_command", "native_python_command"):
            if key in native and (
                not isinstance(native[key], list)
                or not native[key]
                or any(not isinstance(p, str) or not p for p in native[key])
            ):
                raise ValueError(f"{key} must be a nonempty argument list")
        if "options" in native:
            native["options"] = MaskExportOptions(**native["options"])
        return cls(policy, tuple(intents), native)


def _opening_owner_label(measurement) -> str:
    """Human name for a measurement's mask opening (issue #9038).

    ``pad:<ref>/<num>`` when the owner pad's source identity is known,
    otherwise the opening kind with an eight-character UUID prefix, because
    that is the identity the ``items`` list carries.
    """
    if measurement.owner_ref:
        return f"pad:{measurement.owner_ref}"
    kind = measurement.opening_kind or "opening"
    if measurement.opening_uuids:
        return f"{kind}[{measurement.opening_uuids[0][:8]}]"
    return kind


def assessment_results(assessment: MaskCopperAssessment) -> DRCResults:
    from .violations import DRCResults, DRCViolation

    result = DRCResults(
        mask_copper_assessments=[assessment],
        rules_checked=1,
        rules_checked_by_rule={"mask_to_copper": 1},
    )
    # Issue #8914: an assessment that could not run produces no measurements and
    # therefore no violations, which is indistinguishable from a board with no
    # exposure -- the shape of the #8914 report, where a rule-by-rule comparison
    # against JLCDFM read "mask_to_copper: 0 findings" for a check that may never
    # have executed.  ``coverage`` and ``reasons`` already reach the report and
    # the verdict (a default-applied run that cannot execute is ``incomplete``);
    # what was missing was a *finding* saying so.  Info severity, like
    # ``physical_copper_gap_incomplete``: missing coverage is not a board defect,
    # so it is always listed and never blocks.
    if assessment.coverage != "complete":
        result.add(
            DRCViolation(
                rule_id="mask_to_copper_incomplete",
                severity="info",
                message=(
                    f"Mask-to-copper coverage {assessment.coverage}: "
                    f"{'; '.join(assessment.reasons) or 'no reason recorded'} "
                    "(zero mask_to_copper findings below mean the check did not "
                    "run, not that the board is clean)"
                ),
            )
        )
    # Issue #9038: an incomplete assessment still measures, but its findings
    # are provisional -- the run's coverage may be incomplete for exactly the
    # regions that produced them. They are capped at warning (never error) and
    # the message says so, so an unfinished run cannot inflate the error count
    # the way the 1059-finding consumer report did.
    provisional = assessment.coverage != "complete"
    for index, measurement in enumerate(assessment.measurements):
        # ``within_own_pad`` (issue #8858): the opening exposes no copper
        # beyond the owner pad's own land, so there is no exposed trace to
        # report -- recorded at info, like a reviewed ``intentional`` escape.
        # ``within_reinforce_run`` (issue #9315): the opening exposes no
        # copper beyond what its reinforce run puts there on purpose.
        # ``own_pour_thermal`` (issue #9284): the only copper under mask
        # beside the opening is the pad's own thermal spokes.
        if measurement.disposition in (
            "intentional",
            "within_own_pad",
            "within_reinforce_run",
            "own_pour_thermal",
        ):
            severity = "info"
        # Warning-disposition measurements and provisional (incomplete-run)
        # findings share the warning ceiling: neither may read as an error.
        elif measurement.disposition == "warning" or provisional:
            severity = "warning"
        else:
            severity = "error"
        parts = [f"Mask-to-copper {measurement.relation}: {measurement.disposition}"]
        if provisional:
            parts.append("provisional: coverage incomplete")
        parts.append(f"exposed area {measurement.exposed_area_mm2:.9g} mm²")
        if measurement.exposed_beyond_owner_mm2 is not None:
            parts.append(f"beyond owner pad copper {measurement.exposed_beyond_owner_mm2:.9g} mm²")
        # Issue #9269: print the deciding beside-pad excess, so a reader can
        # see why a non-zero "beyond owner pad copper" is excused.
        if measurement.beside_owner_excess_mm2 is not None:
            parts.append(f"beside owner excess {measurement.beside_owner_excess_mm2:.9g} mm²")
        parts.append(f"assessment measurement {index}")
        parts.append(f"opening {_opening_owner_label(measurement)}")
        copper_desc = measurement.conductor_kind or "unknown copper"
        if measurement.conductor_net:
            parts.append(f"exposed copper {copper_desc} (net {measurement.conductor_net})")
        else:
            parts.append(f"exposed copper {copper_desc} (unnamed net)")
        if measurement.same_net is True:
            if measurement.own_pad_entry is True:
                parts.append("same-net own-pad entry")
            elif measurement.own_pad_entry is False:
                parts.append("same-net pass-through")
            else:
                parts.append("same-net")
        elif measurement.same_net is False:
            parts.append("different-net")
        # Issue #9315: a reinforce opening names the run that created it,
        # whether excused (within_reinforce_run) or still gated (copper the
        # run does not own inside its clearance band).
        if measurement.owner_run:
            parts.append(f"reinforce run {measurement.owner_run}")
        if measurement.pour_spokes is not None:
            parts.append(
                f"{measurement.pour_spokes} thermal spokes "
                f"(declared spoke {measurement.pour_spoke_width_mm:.9g} mm, "
                f"relief gap {measurement.pour_relief_gap_mm:.9g} mm)"
            )
        result.add(
            DRCViolation(
                rule_id="mask_to_copper",
                severity=severity,
                message="; ".join(parts),
                location=measurement.location_mm,
                layer=measurement.copper_layer,
                actual_value=measurement.clearance_mm,
                required_value=assessment.policy.clearance_mm if assessment.policy else None,
                items=(*measurement.opening_uuids, measurement.conductor_uuid),
                # Issue #8858: name the nets so the synthetic-fixture CI gates
                # can baseline findings by the net signature (#8957 model).
                # ``items`` are opening/conductor UUIDs, which every
                # board regeneration re-mints, so they cannot key an
                # identity allowance; owner + conductor net names are
                # stable across regenerations of the same recipe.
                nets=tuple(
                    sorted({n for n in (measurement.owner_net, measurement.conductor_net) if n})
                ),
            )
        )
    if assessment.measurements:
        # Per-opening rollup (issue #9038): 1059 findings on one board read
        # triageably only once they collapse to the handful of openings that
        # produced them. Info, like the incomplete finding: never blocks.
        counts: dict[tuple[str, ...], int] = {}
        labels: dict[tuple[str, ...], str] = {}
        for measurement in assessment.measurements:
            key = tuple(measurement.opening_uuids)
            counts[key] = counts.get(key, 0) + 1
            labels.setdefault(key, _opening_owner_label(measurement))
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        shown = [f"{labels[key]} x{count}" for key, count in ordered[:5]]
        remaining = len(ordered) - len(shown)
        suffix = f" (+{remaining} more openings)" if remaining > 0 else ""
        result.add(
            DRCViolation(
                rule_id="mask_to_copper_openings",
                severity="info",
                message=(
                    f"Mask-to-copper: {len(assessment.measurements)} findings across "
                    f"{len(counts)} mask openings; per-opening: {', '.join(shown)}{suffix}"
                ),
            )
        )
    return result


# Interim constants (issue #8842) until the manufacturer threshold table of
# #8839 lands: JLCDFM's observed mask-opening-to-trace limits for JLCPCB.
_JLCPCB_MASK_COPPER_POLICY = MaskCopperPolicy(
    clearance_mm=0.09,
    source="JLCDFM mask-opening-exposing-trace observation (epic #8838)",
    process="jlcpcb",
    revision="interim-2026-11-12",
    warning_clearance_mm=0.15,
)


def default_request_for_manufacturer(manufacturer_id: str | None) -> MaskCopperRequest | None:
    """Default-on request for ``--mfr``; None when no cited policy exists.

    Mask expansion is not supplied here: the native geometry already carries
    the board's effective expansion.
    """
    if manufacturer_id and manufacturer_id.lower().startswith("jlcpcb"):
        return MaskCopperRequest(policy=_JLCPCB_MASK_COPPER_POLICY)
    return None
