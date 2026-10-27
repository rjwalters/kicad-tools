"""Spatial / identity scope for ``kct check`` findings (Issue #8016).

The checks always run over the whole board; a :class:`CheckScope` filters the
*findings* afterwards.  A finding is in scope when its ``location`` lies inside
the bbox, OR its ``items`` intersect ``refs``, OR its ``nets`` intersect
``nets`` (a union).  Findings carrying none of location / items / nets are
board-level and cannot be scoped: they are kept and counted, never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .violations import DRCViolation


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    """Parse ``X1,Y1,X2,Y2`` (mm) into a normalized ``(xmin, ymin, xmax, ymax)``.

    Raises:
        ValueError: when the text is not four finite numbers.
    """
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 4:
        raise ValueError(f"--bbox expects X1,Y1,X2,Y2 (4 numbers in mm), got {value!r}")
    try:
        x1, y1, x2, y2 = (float(p) for p in parts)
    except ValueError:
        raise ValueError(f"--bbox expects X1,Y1,X2,Y2 (4 numbers in mm), got {value!r}") from None
    if not all(abs(c) != float("inf") and c == c for c in (x1, y1, x2, y2)):
        raise ValueError(f"--bbox values must be finite, got {value!r}")
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


@dataclass(frozen=True)
class CheckScope:
    """A union scope: bbox (mm, board coordinates) and/or refs and/or nets."""

    bbox: tuple[float, float, float, float] | None = None
    refs: frozenset[str] = field(default_factory=frozenset)
    nets: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_args(
        cls, bbox: str | None = None, refs: str | None = None, nets: str | None = None
    ) -> CheckScope | None:
        """Build a scope from raw CLI strings; ``None`` when no flag was given.

        Raises:
            ValueError: on a malformed ``bbox``.
        """
        parsed_bbox = parse_bbox(bbox) if bbox is not None else None
        ref_set = frozenset(_split_csv(refs))
        net_set = frozenset(_split_csv(nets))
        if parsed_bbox is None and not ref_set and not net_set:
            return None
        return cls(bbox=parsed_bbox, refs=ref_set, nets=net_set)

    def is_unscopable(self, v: DRCViolation) -> bool:
        """A board-level finding: no location, no items and no nets."""
        return v.location is None and not v.items and not v.nets

    def in_scope(self, v: DRCViolation) -> bool:
        """True when ``v`` falls inside the scope (board-level findings are kept)."""
        if self.is_unscopable(v):
            return True
        if self.bbox is not None and v.location is not None:
            xmin, ymin, xmax, ymax = self.bbox
            x, y = v.location
            if xmin <= x <= xmax and ymin <= y <= ymax:
                return True
        if self.refs and any(i in self.refs for i in v.items):
            return True
        return bool(self.nets and any(n in self.nets for n in v.nets))

    def apply(self, violations: Iterable[DRCViolation]) -> tuple[list[DRCViolation], dict]:
        """Filter ``violations``; return ``(kept, scope_record)``."""
        all_findings = list(violations)
        kept = [v for v in all_findings if self.in_scope(v)]
        unscopable = sum(1 for v in kept if self.is_unscopable(v))
        return kept, self.to_dict(
            findings_total=len(all_findings),
            findings_in_scope=len(kept),
            unscopable_kept=unscopable,
        )

    def to_dict(
        self, findings_total: int = 0, findings_in_scope: int = 0, unscopable_kept: int = 0
    ) -> dict:
        return {
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "refs": sorted(self.refs),
            "nets": sorted(self.nets),
            "findings_total": findings_total,
            "findings_in_scope": findings_in_scope,
            "unscopable_kept": unscopable_kept,
            "checks_run_in_full": True,
            "signoff_eligible": False,
        }

    def describe(self) -> str:
        parts = []
        if self.bbox is not None:
            parts.append("bbox=" + ",".join(f"{c:g}" for c in self.bbox) + " mm")
        if self.refs:
            parts.append("refs=" + ",".join(sorted(self.refs)))
        if self.nets:
            parts.append("nets=" + ",".join(sorted(self.nets)))
        return " ".join(parts)

    def unknown_names(self, known_refs: Iterable[str], known_nets: Iterable[str]) -> list[str]:
        """Refs/nets named in the scope that do not exist on the board."""
        kr, kn = set(known_refs), set(known_nets)
        return sorted(self.refs - kr) + sorted(self.nets - kn)
