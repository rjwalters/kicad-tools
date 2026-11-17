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
  apply: the conductor is an area (``zone``) or another pad, so it has no
  terminating endpoint to test.
- (omitted) -- the conductor's net is unidentified, or none of the opening's
  contributors has an identified net.

While ``assessment.coverage != "complete"`` the findings are provisional:
severity is capped at ``warning`` (never ``error``) and each message carries
``provisional: coverage incomplete``, so an incomplete run never inflates the
error count. A closing ``mask_to_copper_openings`` info finding summarizes the
measurements per opening, so one pad with many findings reads as one opening.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
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

    def __post_init__(self):
        if self.disposition not in (
            "violation",
            "uncertain",
            "intentional",
            "warning",
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
        for value in (self.owner_net, self.conductor_net, self.conductor_kind, self.owner_ref):
            if value is not None and not isinstance(value, str):
                raise ValueError("Invalid mask measurement triage metadata")
        for flag in (self.same_net, self.own_pad_entry):
            if flag is not None and not isinstance(flag, bool):
                raise ValueError("Invalid mask measurement triage metadata")

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


def assess_attributed_geometry(geometry, policy, intents=()):
    """Measure actual native mask against independently retained source copper.

    Distances are millimetres; exposure is area in square millimetres, never
    mislabeled as penetration depth. Exact-limit measurements remain uncertain
    within the native/Gerber construction budget, and cannot certify a pass.
    """
    # The shapely.geometry stubs error is consumed at the first import site
    # (_conductor_endpoints above); a second ignore here would read as unused.
    from shapely import STRtree  # type: ignore[import-untyped]
    from shapely.geometry import GeometryCollection, box
    from shapely.ops import nearest_points, unary_union  # type: ignore[import-untyped]

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
                intent = (
                    approved.get((owners[0], identity, side))
                    if len(owners) == 1 and kind != "merged_region"
                    else None
                )
                if intent:
                    disposition = "intentional"
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
                    )
                )
    return result


def check_mask_to_copper(path, policy=None, intents=(), **native_options):
    """Public immutable-source checker. Missing policy/native coverage never passes."""
    if policy is None:
        return MaskCopperAssessment(
            reasons=["An explicit process-specific mask-to-copper policy is required"]
        )
    from .mask_copper_geometry import inspect_attributed_mask_geometry

    try:
        geometry = inspect_attributed_mask_geometry(path, **native_options)
        return assess_attributed_geometry(geometry, policy, intents)
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
        if measurement.disposition == "intentional":
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
