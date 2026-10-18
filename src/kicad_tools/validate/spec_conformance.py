"""``project.kct`` requirements vs the routed board (Issue #7292, PR-B of #7250).

``kct check`` used to read ``project.kct`` only for ``target_fab``, so a board
whose spec claims 20x20 mm, 6 layers and ``min_trace`` 0.5 mm could still pass
sign-off as a routed 50x55 mm, 2-layer, 0.2 mm board.  This module compares
the spec's machine-checkable ``requirements`` against the board and reports:

* **error** -- Edge.Cuts bbox differs from ``mechanical.dimensions`` by more
  than 0.1 mm (a rotated board is not a mismatch); copper-layer count differs
  from ``manufacturing.layers.preferred`` or exceeds ``layers.max``.
* **warning** -- a routed minimum (trace width, via diameter, via drill) is
  below the spec's ``min_trace`` / ``min_via`` / ``min_drill``; or the spec's
  ``target_fab`` differs from the tier ``kct check`` resolved.

Coverage (:func:`coverage_for`): ``skipped:no_project_kct`` without a spec,
``unknown:unread_spec_keys:<paths>`` when the spec declares requirement keys
nothing reads, otherwise ``checked``.  The category is deliberately not in
``BLOCKING_CATEGORIES``: an ``unknown`` never blocks readiness, its errors do.

``min_space`` is **not** compared: measuring the routed minimum spacing needs
a full pairwise clearance pass that the ``clearance`` category already owns,
so the key is reported as unread rather than silently ignored.

Dispatched as a ``check_cmd``-local category (like ``netclass_floor``) because
it needs the PCB *path* to locate ``project.kct``; it is NOT a
:class:`DRCChecker` method.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .violations import DRCResults, DRCViolation

if TYPE_CHECKING:
    from ..schema.pcb import PCB
    from ..spec.schema import ProjectSpec
    from .coverage import Coverage

DIMENSION_TOLERANCE_MM = 0.1
# A routed minimum within float noise of the spec floor is not "below" it.
MIN_TOLERANCE_MM = 1e-6

SPEC_CONFORMANCE_RULE_IDS: tuple[str, ...] = (
    "spec_conformance_dimensions",
    "spec_conformance_layers",
    "spec_conformance_min_trace",
    "spec_conformance_min_via",
    "spec_conformance_min_drill",
    "spec_conformance_target_fab",
)

# Known schema keys that the spec declares but no check reads.
_UNREAD_KNOWN_KEYS: tuple[tuple[str, ...], ...] = (
    ("electrical", "signals"),
    ("manufacturing", "min_space"),
)


def find_project_kct(pcb_path: Path) -> Path | None:
    """``project.kct`` next to the PCB, else one directory up (as ``target_fab``)."""
    for candidate in (
        pcb_path.parent / "project.kct",
        pcb_path.parent.parent / "project.kct",
    ):
        if candidate.is_file():
            return candidate
    return None


def _load(pcb_path: Path) -> ProjectSpec | None:
    kct = find_project_kct(pcb_path)
    if kct is None:
        return None
    from ..spec import load_spec

    try:
        return load_spec(kct)
    except Exception:
        return None


def unread_spec_keys(spec: ProjectSpec) -> list[str]:
    """Dotted ``requirements.*`` paths this check does not read, sorted."""
    from ..spec.parser import collect_unknown_keys

    found = {k for k in collect_unknown_keys(spec) if k.startswith("requirements.")}
    reqs = spec.requirements
    if reqs is not None:
        for path in _UNREAD_KNOWN_KEYS:
            node: Any = reqs
            for part in path:
                node = getattr(node, part, None)
                if node is None:
                    break
            if node is not None:
                found.add("requirements." + ".".join(path))
    return sorted(found)


def coverage_for(category: str, pcb_path: Path | None) -> Coverage:
    """Coverage state of ``spec_conformance`` for the board at ``pcb_path``."""
    from . import coverage as cov

    if pcb_path is None:
        return cov.unknown(category, "needs_input:pcb_path")
    kct = find_project_kct(pcb_path)
    if kct is None:
        return cov.skipped(category, "no_project_kct")
    spec = _load(pcb_path)
    if spec is None:
        return cov.unknown(category, "unreadable_project_kct")
    unread = unread_spec_keys(spec)
    if unread:
        return cov.unknown(category, f"unread_spec_keys:{','.join(unread)}")
    return cov.checked(category)


def _mm(value: Any) -> float | None:
    """Parse a spec length string like ``"50mm"``; ``None`` when unparseable."""
    from ..spec.units import parse_unit_value

    if value is None:
        return None
    try:
        return float(parse_unit_value(str(value)).to_base())
    except (ValueError, TypeError):
        return None


def _board_size(pcb: PCB) -> tuple[float, float] | None:
    from ..core.board_outline import board_outline_bounds

    bounds = board_outline_bounds(pcb._sexp)
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    return (x1 - x0, y1 - y0)


def check_spec_conformance(
    pcb: PCB,
    pcb_path: Path | None,
    resolved_mfr: str | None = None,
) -> DRCResults:
    """Compare ``project.kct`` requirements to the routed board."""
    results = DRCResults()
    for rule_id in SPEC_CONFORMANCE_RULE_IDS:
        results.rules_checked_by_rule[rule_id] = 1
    results.rules_checked = len(SPEC_CONFORMANCE_RULE_IDS)
    if pcb_path is None:
        return results
    spec = _load(pcb_path)
    reqs = spec.requirements if spec is not None else None
    if reqs is None:
        return results

    def add(rule_id: str, severity: str, message: str, actual: Any, required: Any) -> None:
        results.add(
            DRCViolation(
                rule_id=rule_id,
                severity=severity,
                message=message,
                actual_value=float(actual) if isinstance(actual, (int, float)) else None,
                required_value=float(required) if isinstance(required, (int, float)) else None,
            )
        )

    mech = reqs.mechanical
    dims = mech.dimensions if mech is not None else None
    if dims:
        want_w, want_h = _mm(dims.get("width")), _mm(dims.get("height"))
        have = _board_size(pcb)
        if want_w is not None and want_h is not None and have is not None:
            tol = DIMENSION_TOLERANCE_MM
            direct = abs(have[0] - want_w) <= tol and abs(have[1] - want_h) <= tol
            rotated = abs(have[0] - want_h) <= tol and abs(have[1] - want_w) <= tol
            if not (direct or rotated):
                add(
                    "spec_conformance_dimensions",
                    "error",
                    f"Board outline is {have[0]:.2f} x {have[1]:.2f} mm but project.kct "
                    f"mechanical.dimensions declares {want_w:g} x {want_h:g} mm "
                    f"(tolerance {tol} mm)",
                    have[0] * have[1],
                    want_w * want_h,
                )

    mfg = reqs.manufacturing
    if mfg is not None:
        layers = mfg.layers or {}
        count = len(pcb.copper_layers)
        preferred, maximum = layers.get("preferred"), layers.get("max")
        if preferred is not None and count != preferred:
            add(
                "spec_conformance_layers",
                "error",
                f"Board has {count} copper layer(s) but project.kct "
                f"manufacturing.layers.preferred is {preferred}",
                count,
                preferred,
            )
        if maximum is not None and count > maximum:
            add(
                "spec_conformance_layers",
                "error",
                f"Board has {count} copper layer(s), above project.kct "
                f"manufacturing.layers.max of {maximum}",
                count,
                maximum,
            )

        segments = list(getattr(pcb, "segments", []) or [])
        vias = list(getattr(pcb, "vias", []) or [])
        measured = (
            ("min_trace", "spec_conformance_min_trace", "trace width", [s.width for s in segments]),
            ("min_via", "spec_conformance_min_via", "via diameter", [v.size for v in vias]),
            ("min_drill", "spec_conformance_min_drill", "via drill", [v.drill for v in vias]),
        )
        for key, rule_id, label, values in measured:
            floor = _mm(getattr(mfg, key, None))
            values = [v for v in values if v]
            if floor is None or not values:
                continue
            routed = min(values)
            if routed < floor - MIN_TOLERANCE_MM:
                add(
                    rule_id,
                    "warning",
                    f"Smallest routed {label} is {routed:g} mm, below project.kct "
                    f"manufacturing.{key} of {floor:g} mm",
                    routed,
                    floor,
                )

        if mfg.target_fab and resolved_mfr and mfg.target_fab != resolved_mfr:
            add(
                "spec_conformance_target_fab",
                "warning",
                f"project.kct target_fab is {mfg.target_fab!r} but kct check resolved "
                f"{resolved_mfr!r}",
                None,
                None,
            )

    return results
