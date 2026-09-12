"""Explicit process policy and source-qualified solder-mask exposure assessment.

This check is opt-in. Neither a manufacturer's generic mask expansion nor a
mask-web width supplies a mask-to-copper process clearance requirement.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class MaskCopperPolicy:
    clearance_mm: float
    source: str
    process: str
    revision: str

    def __post_init__(self):
        if (
            isinstance(self.clearance_mm, bool)
            or not math.isfinite(self.clearance_mm)
            or self.clearance_mm < 0
        ):
            raise ValueError("Mask-to-copper clearance must be finite and nonnegative")
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

    def __post_init__(self):
        if self.disposition not in (
            "violation",
            "uncertain",
            "intentional",
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


def assess_attributed_geometry(geometry, policy, intents=()):
    """Measure actual native mask against independently retained source copper.

    Distances are millimetres; exposure is area in square millimetres, never
    mislabeled as penetration depth. Exact-limit measurements remain uncertain
    within the native/Gerber construction budget, and cannot certify a pass.
    """
    from shapely.geometry import GeometryCollection
    from shapely.ops import nearest_points, unary_union

    binding = source_binding(geometry)
    result = MaskCopperAssessment(
        coverage="complete" if not geometry.errors else "incomplete",
        reasons=list(geometry.errors),
        policy=policy,
        binding=binding,
        geometry_provenance={"export": geometry.exported.to_dict(), "objects": geometry.provenance},
    )
    # The native plot construction error comes from the captured board's
    # m_MaxError (typically 5um for custom/chamfer polygons). Add 4nm for
    # Gerber reconstruction and coordinate rounding. This is uncertainty,
    # never a process clearance allowance.
    uncertainty = geometry.provenance.get("max_error_mm_per_construction", 0) + 0.000004
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
        ):
            reason = "intent is limited to a pad and its particular escape conductor"
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
        for owners, opening, kind, margin, provenance in openings:
            for identity, item in geometry.objects.items():
                copper = item["layers_geometry"].get(layer)
                if copper is None or copper.is_empty:
                    continue
                if kind != "merged_region" and owners == (identity,):
                    continue  # Exclude only this UUID's own contribution.
                result.evaluated_pairs += 1
                distance = opening.distance(copper)
                if distance > policy.clearance_mm + uncertainty:
                    continue
                intersection = opening.intersection(copper)
                area = intersection.area
                certain_exposure = (
                    opening.buffer(-uncertainty).intersection(copper.buffer(-uncertainty)).area > 0
                )
                relation = "overlap" if area > 0 else "touching" if distance == 0 else "disjoint"
                if certain_exposure or distance < policy.clearance_mm - uncertainty:
                    disposition = "violation"
                else:
                    disposition = "uncertain"
                intent = (
                    approved.get((owners[0], identity, side))
                    if len(owners) == 1 and kind != "merged_region"
                    else None
                )
                if intent:
                    disposition = "intentional"
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
                        margin < 0,
                        provenance,
                        intent.rationale if intent else None,
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


def assessment_results(assessment):
    from .violations import DRCResults, DRCViolation

    result = DRCResults(
        mask_copper_assessments=[assessment],
        rules_checked=1,
        rules_checked_by_rule={"mask_to_copper": 1},
    )
    for index, measurement in enumerate(assessment.measurements):
        if measurement.disposition == "intentional":
            severity = "info"
        else:
            severity = "error"
        result.add(
            DRCViolation(
                rule_id="mask_to_copper",
                severity=severity,
                message=f"Mask-to-copper {measurement.relation}: {measurement.disposition}; "
                f"exposed area {measurement.exposed_area_mm2:.9g} mm²; assessment measurement {index}",
                location=measurement.location_mm,
                layer=measurement.copper_layer,
                actual_value=measurement.clearance_mm,
                required_value=assessment.policy.clearance_mm if assessment.policy else None,
                items=(*measurement.opening_uuids, measurement.conductor_uuid),
            )
        )
    return result
