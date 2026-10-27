"""Shared, versioned ``Finding`` model for machine-readable output (Issue #8015).

``kct check``, ``kct drc`` and ``kct detect-mistakes`` each emit findings in
their own JSON shape.  Consumers that select / highlight / zoom the items a
finding concerns (e.g. openkonnect's ``show_findings``) need one shape with
explicit locators.  This module is the thin adapter layer: each emitter keeps
its existing keys and additionally carries ``schema_version`` plus a
``findings`` array of :meth:`Finding.to_dict` objects.

Coordinate frame: ``point`` and ``bbox`` are millimetres in the KiCad *file*
frame (the frame KiCad shows; y grows downward).  Emitters that measure in
another frame convert before building a :class:`Finding`.

Compatibility policy: see ``docs/reference/machine-output.md``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Version of the ``Finding`` JSON shape.  Additive fields keep the version;
#: a renamed / removed / retyped field or a changed meaning bumps it.
SCHEMA_VERSION = 1

_REF = r"[A-Za-z]+[0-9]+[A-Za-z]?"
_REF_ONLY = re.compile(rf"^{_REF}$")
# "U1.3", "U1-3" (optionally followed by free text such as " hole")
_REF_PAD = re.compile(rf"^({_REF})[.\-]([A-Za-z0-9]+)(?:\s.*)?$")
# kicad-cli: "Pad 6 [<no net>] of U3 on F.Cu"
_KICAD_PAD = re.compile(rf"^Pad\s+(\S+)\s+(?:\[[^\]]*\]\s+)?of\s+({_REF})\b")
# kicad-cli: "Footprint C52"
_KICAD_FOOTPRINT = re.compile(rf"^Footprint\s+({_REF})\b")


def split_items(items: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split free-form finding items into ``(refs, pads)``.

    ``pads`` holds ``REF.PAD`` strings; ``refs`` holds reference designators
    (including the owner of every pad).  Items that are neither (a net name,
    ``net_settings``, a track description) are dropped here -- nets travel in
    their own field.  Order of first appearance is kept, duplicates removed.
    """
    refs: list[str] = []
    pads: list[str] = []

    def _add(seq: list[str], value: str) -> None:
        if value not in seq:
            seq.append(value)

    for raw in items:
        item = str(raw).strip()
        if m := _KICAD_PAD.match(item):
            _add(pads, f"{m.group(2).upper()}.{m.group(1)}")
            _add(refs, m.group(2).upper())
        elif m := _KICAD_FOOTPRINT.match(item):
            _add(refs, m.group(1).upper())
        elif m := _REF_PAD.match(item):
            _add(pads, f"{m.group(1).upper()}.{m.group(2)}")
            _add(refs, m.group(1).upper())
        elif _REF_ONLY.match(item):
            _add(refs, item.upper())
    return refs, pads


def _point(value: Sequence[Any] | None) -> list[float] | None:
    if value is None or len(value) < 2:
        return None
    return [float(value[0]), float(value[1])]


@dataclass
class Finding:
    """One finding with explicit locators (mm, KiCad file frame)."""

    rule_id: str
    severity: str
    message: str
    refs: list[str] = field(default_factory=list)
    pads: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    point: list[float] | None = None
    bbox: list[float] | None = None
    layer: str | None = None
    waived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "refs": list(self.refs),
            "pads": list(self.pads),
            "nets": list(self.nets),
            "point": list(self.point) if self.point is not None else None,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "layer": self.layer,
            "waived": self.waived,
        }


def _bbox_of(points: Iterable[Sequence[float] | None]) -> list[float] | None:
    pts = [p for p in points if p is not None]
    if len(pts) < 2:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def from_violation_dict(data: dict[str, Any]) -> Finding:
    """Adapt a ``Violation.to_dict()`` / mistake finding dict (check, mistakes).

    Both carry ``rule_id, severity, message, items, nets, layer, location``
    with ``location`` an ``[x, y]`` list in the file frame (or null).
    ``closest_locations`` (when present) yields the ``bbox``.
    """
    refs, pads = split_items(data.get("items") or [])
    point = _point(data.get("location"))
    closest = [_point(p) for p in data.get("closest_locations") or []]
    bbox = _bbox_of([point, *closest])
    return Finding(
        rule_id=str(data.get("rule_id", "")),
        severity=str(data.get("severity", "warning")),
        waived=bool(data.get("waived", False)),
        message=str(data.get("message", "")),
        refs=refs,
        pads=pads,
        nets=[str(n) for n in data.get("nets") or []],
        point=point,
        bbox=bbox,
        layer=data.get("layer"),
    )


def findings_payload(findings: Iterable[Finding]) -> dict[str, Any]:
    """The additive ``schema_version`` + ``findings`` keys for an emitter."""
    return {"schema_version": SCHEMA_VERSION, "findings": [f.to_dict() for f in findings]}


def from_drc_dict(data: dict[str, Any]) -> Finding:
    """Adapt a ``DRCViolation.to_dict()`` (``kct drc``).

    DRC locations are ``{"x_mm", "y_mm", "layer"}`` records in the file frame;
    the first is the ``point`` and two or more give a ``bbox``.
    """
    locs = data.get("locations") or []
    pts = [[float(loc["x_mm"]), float(loc["y_mm"])] for loc in locs]
    refs, pads = split_items(data.get("items") or [])
    layer = next((loc.get("layer") for loc in locs if loc.get("layer")), None)
    return Finding(
        rule_id=str(data.get("type_str") or data.get("type", "")),
        severity=str(data.get("severity", "warning")),
        message=str(data.get("message", "")),
        refs=refs,
        pads=pads,
        nets=[str(n) for n in data.get("nets") or []],
        point=pts[0] if pts else None,
        bbox=_bbox_of(pts),
        layer=layer,
        waived=bool(data.get("waived", False)),
    )
