"""Per-project CPL rotation corrections (issue #9322).

The shipped manufacturer table (``manufacturers/data/jlcpcb_rotations.yaml``)
keys corrections on *footprint-name globs*.  That is a weak key for connectors:
the correction really belongs to the fab's library model (the LCSC part), so a
``PinHeader_*`` glob would rotate every other header wrongly.  A project can
instead declare corrections in a tracked sidecar next to its ``.kicad_pcb``::

    # cpl_rotations.yaml -- degrees ADDED to the KiCad rotation in the CPL
    by_reference:
      J2: 90
    by_lcsc:
      C5156614: 90     # 1x05 pin header
      C4991872: 270    # SJ-3523-SMT audio jack

Resolution per placement is ``by_reference`` > ``by_lcsc`` > the shipped
footprint-glob table.  A sidecar entry *replaces* the glob correction for that
part; a part the sidecar does not name keeps its shipped-table correction.
Corrections are additive to the KiCad rotation (``(kicad + correction) % 360``).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools.exceptions import ConfigurationError

if TYPE_CHECKING:
    from .pnp import PlacementData

SIDECAR_NAME = "cpl_rotations.yaml"
"""Conventional sidecar filename, looked up next to the ``.kicad_pcb``."""

_LCSC_RE = re.compile(r"^C\d+$")
_KNOWN_KEYS = ("by_reference", "by_lcsc")

# Reference-designator prefixes of polarised / asymmetric parts whose CPL
# rotation is worth a human look when no correction matched them: connectors,
# ICs, diodes, transistors, LEDs, switches, crystals/oscillators and jumpers.
POLARISED_PREFIXES: tuple[str, ...] = ("J", "U", "D", "Q", "LED", "SW", "Y", "JP", "X")

SOURCE_REFERENCE = "by_reference"
SOURCE_LCSC = "by_lcsc"
SOURCE_GLOB = "footprint_glob"


class RotationOverridesError(ConfigurationError):
    """The rotation sidecar is missing, unparsable or invalid."""


@dataclass(frozen=True)
class RotationOverrides:
    """Parsed ``cpl_rotations.yaml``."""

    by_reference: dict[str, float] = field(default_factory=dict)
    by_lcsc: dict[str, float] = field(default_factory=dict)
    path: Path | None = None
    sha256: str = ""

    def __bool__(self) -> bool:
        return bool(self.by_reference or self.by_lcsc)


def normalize_lcsc(value: str | None) -> str:
    """Canonical LCSC part number (``" c5156614 "`` -> ``"C5156614"``)."""
    return (value or "").strip().upper()


def discover_rotation_overrides(pcb_path: str | Path) -> Path | None:
    """Return ``<pcb dir>/cpl_rotations.yaml`` when it exists, else ``None``."""
    candidate = Path(pcb_path).parent / SIDECAR_NAME
    return candidate if candidate.is_file() else None


def _degrees(section: str, key: object, value: object, path: Path) -> float:
    # bool is an int subclass; ``J1: yes`` must not silently mean 1 degree.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RotationOverridesError(
            f"{path.name}: {section}[{key!r}] must be a number of degrees, got {value!r}",
            context={"file": str(path), "section": section, "key": str(key)},
            suggestions=["Use a plain number such as 90, 180 or 270"],
        )
    if not math.isfinite(value):
        raise RotationOverridesError(
            f"{path.name}: {section}[{key!r}] must be finite, got {value!r}",
            context={"file": str(path), "section": section, "key": str(key)},
        )
    return float(value)


def parse_rotation_overrides(raw: bytes, path: Path) -> RotationOverrides:
    """Parse and validate sidecar bytes; raises :class:`RotationOverridesError`."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - PyYAML is a core dependency
        raise RotationOverridesError(
            f"PyYAML is required to read {path.name}",
            suggestions=["pip install pyyaml"],
        ) from exc

    try:
        data: Any = yaml.safe_load(raw.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise RotationOverridesError(
            f"{path.name} is not valid YAML: {exc}",
            context={"file": str(path)},
        ) from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise RotationOverridesError(
            f"{path.name}: top level must be a mapping with {' / '.join(_KNOWN_KEYS)}",
            context={"file": str(path)},
        )
    unknown = sorted(str(k) for k in data if k not in _KNOWN_KEYS)
    if unknown:
        raise RotationOverridesError(
            f"{path.name}: unknown key(s) {', '.join(unknown)}; "
            f"expected {' and/or '.join(_KNOWN_KEYS)}",
            context={"file": str(path), "unknown": unknown},
            suggestions=["Check for a typo such as 'by_ref' or 'by_lcsc_part'"],
        )

    sections: dict[str, dict[str, float]] = {}
    for section in _KNOWN_KEYS:
        body = data.get(section)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise RotationOverridesError(
                f"{path.name}: {section} must be a mapping of key -> degrees",
                context={"file": str(path), "section": section},
            )
        parsed: dict[str, float] = {}
        for key, value in body.items():
            name = str(key).strip()
            if not name:
                raise RotationOverridesError(
                    f"{path.name}: {section} has an empty key", context={"file": str(path)}
                )
            if section == SOURCE_LCSC:
                name = normalize_lcsc(name)
                if not _LCSC_RE.match(name):
                    raise RotationOverridesError(
                        f"{path.name}: by_lcsc key {key!r} is not an LCSC part number (Cnnnnn)",
                        context={"file": str(path), "key": str(key)},
                    )
            if name in parsed:
                raise RotationOverridesError(
                    f"{path.name}: {section} lists {name} twice",
                    context={"file": str(path), "key": name},
                )
            parsed[name] = _degrees(section, key, value, path)
        sections[section] = parsed

    return RotationOverrides(
        by_reference=sections[SOURCE_REFERENCE],
        by_lcsc=sections[SOURCE_LCSC],
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_rotation_overrides(path: str | Path) -> RotationOverrides:
    """Load the sidecar at *path*; a missing or invalid file is an error."""
    sidecar = Path(path)
    try:
        raw = sidecar.read_bytes()
    except OSError as exc:
        raise RotationOverridesError(
            f"Cannot read rotation overrides {sidecar}: {exc}",
            context={"file": str(sidecar)},
        ) from exc
    return parse_rotation_overrides(raw, sidecar)


def resolve_rotation_correction(
    placement: PlacementData,
    glob_corrections: dict[str, float] | None,
    overrides: RotationOverrides | None,
) -> tuple[float, str | None]:
    """Resolve one placement's correction and where it came from.

    Returns ``(degrees, source)``; ``source`` is ``"by_reference"``,
    ``"by_lcsc"``, ``"footprint_glob"`` or ``None`` when nothing matched.
    A glob match whose offset is ``0`` still reports ``footprint_glob`` -- the
    part *was* reviewed by the shipped table.
    """
    from kicad_tools.manufacturers.base import match_rotation_correction

    if overrides:
        if placement.reference in overrides.by_reference:
            return overrides.by_reference[placement.reference], SOURCE_REFERENCE
        lcsc = normalize_lcsc(placement.lcsc)
        if lcsc and lcsc in overrides.by_lcsc:
            return overrides.by_lcsc[lcsc], SOURCE_LCSC
    if glob_corrections:
        for pattern in glob_corrections:
            if _glob_matches(placement.footprint, pattern):
                return match_rotation_correction(placement.footprint, glob_corrections), (
                    SOURCE_GLOB
                )
    return 0.0, None


def _glob_matches(footprint: str, pattern: str) -> bool:
    import fnmatch

    if fnmatch.fnmatch(footprint, pattern):
        return True
    return ":" in footprint and fnmatch.fnmatch(footprint.split(":", 1)[1], pattern)


def is_polarised_reference(reference: str) -> bool:
    """True for references whose prefix marks a connector, IC, diode, transistor or LED."""
    match = re.match(r"[A-Za-z_]+", reference)
    return match is not None and match.group(0).upper() in POLARISED_PREFIXES


@dataclass
class RotationReview:
    """What the CPL's rotation pipeline did, for the readiness report."""

    sidecar: str | None = None
    """Sidecar path as given to the loader (callers relativise for reports)."""
    sidecar_sha256: str | None = None
    applied: list[dict[str, Any]] = field(default_factory=list)
    """``{"reference", "source", "key", "degrees"}`` for every non-zero correction."""
    unreviewed: list[dict[str, str]] = field(default_factory=list)
    """Polarised/asymmetric placements no correction matched (review list)."""
    unmatched_overrides: list[str] = field(default_factory=list)
    """Sidecar ``by_reference`` / ``by_lcsc`` keys no CPL placement used."""
    rotations: dict[str, float] = field(default_factory=dict, repr=False)
    """Final CPL rotation per reference (used to verify a retained CPL)."""

    def to_report(self, sidecar_rel: str | None = None) -> dict[str, Any]:
        """JSON-able block for ``readiness.json`` (no absolute paths)."""
        block: dict[str, Any] = {
            "sidecar": sidecar_rel,
            "sidecar_sha256": self.sidecar_sha256,
            "applied": self.applied,
            "unreviewed": self.unreviewed,
        }
        if self.unmatched_overrides:
            block["unmatched_overrides"] = self.unmatched_overrides
        return block


def review_rotations(
    placements: Iterable[PlacementData],
    transform: Any,
    glob_corrections: dict[str, float] | None,
    overrides: RotationOverrides | None,
) -> RotationReview:
    """Audit *placements* against the correction sources.

    *transform* is the formatter's ``apply_transforms`` so the recorded final
    rotations are exactly the CPL's.
    """
    # Guard on ``path is not None``, not truthiness: an empty-but-present
    # sidecar (``__bool__`` False) still has a path and hash worth recording.
    has_sidecar = overrides is not None and overrides.path is not None
    review = RotationReview(
        sidecar=str(overrides.path) if has_sidecar else None,
        sidecar_sha256=overrides.sha256 if has_sidecar else None,
    )
    used_refs: set[str] = set()
    used_lcsc: set[str] = set()
    for placement in sorted(placements, key=lambda p: p.reference):
        degrees, source = resolve_rotation_correction(placement, glob_corrections, overrides)
        review.rotations[placement.reference] = transform(placement).rotation
        if source == SOURCE_REFERENCE:
            used_refs.add(placement.reference)
        elif source == SOURCE_LCSC:
            used_lcsc.add(normalize_lcsc(placement.lcsc))
        if degrees != 0:
            key = {
                SOURCE_REFERENCE: placement.reference,
                SOURCE_LCSC: normalize_lcsc(placement.lcsc),
                SOURCE_GLOB: placement.footprint,
            }[source or SOURCE_GLOB]
            review.applied.append(
                {
                    "reference": placement.reference,
                    "source": source,
                    "key": key,
                    "degrees": degrees,
                }
            )
        elif source is None and is_polarised_reference(placement.reference):
            review.unreviewed.append(
                {
                    "reference": placement.reference,
                    "footprint": placement.footprint,
                    "lcsc": normalize_lcsc(placement.lcsc),
                    "rotation": f"{review.rotations[placement.reference]:.1f}",
                }
            )
    if overrides:
        review.unmatched_overrides = sorted(
            [f"by_reference:{r}" for r in overrides.by_reference if r not in used_refs]
            + [f"by_lcsc:{c}" for c in overrides.by_lcsc if c not in used_lcsc]
        )
    return review
