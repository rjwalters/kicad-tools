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

    @property
    def passed(self):
        return self.coverage == "complete" and not any(
            m.disposition in ("violation", "uncertain") for m in self.measurements
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
