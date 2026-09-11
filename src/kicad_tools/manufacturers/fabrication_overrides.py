"""Validated per-board fabrication-floor overrides (Issue #5006).

``kct check --emit-drc-constraints`` (and the manufacturing export path,
``ManufacturingPackageGenerator._write_drc_constraints``) emit native
``.kicad_pro`` / ``.kicad_dru`` sidecars straight from a manufacturer
profile's :class:`~kicad_tools.manufacturers.base.DesignRules`.  Those
profile values are deliberately *conservative* defaults -- e.g. jlcpcb /
jlcpcb-tier1 ship ``min_hole_to_hole_mm=0.5`` even though JLC's own
published capability table
(https://jlcpcb.com/capabilities/pcb-capabilities/) states a 0.45mm
pad-hole-spacing floor.  A board whose actual, reviewed geometry meets
the *real* fab floor (0.45mm) but not the profile's conservative default
(0.5mm) had no way to say so: emission always overwrote any hand-tuned
project setting with the generic 0.5mm value, and the only workaround was
a board-local hack that patched the ``.kicad_pro`` back to 0.45mm after
the fact -- a script-specific fix with no validation and no connection to
the Python DRC checker that ran against the *same* board.

This module is the shared, validated contract both surfaces should use
instead:

- :func:`load_fabrication_overrides` / :func:`discover_fabrication_overrides_sidecar`
  read a small JSON sidecar declaring per-field overrides, each carrying
  the manufacturer it applies to, a source citation, a reason, and a
  tracking-issue reference recording the review that approved it.
- :func:`validate_fabrication_override` rejects an override that is not
  backed by an independently verified capability floor for that exact
  (manufacturer, field) pair -- so a board cannot invent an arbitrary
  looser number, it can only assert a value the manufacturer publicly
  guarantees, and it cannot even go looser than *that*.
- :func:`apply_fabrication_overrides` returns a new
  :class:`~kicad_tools.manufacturers.base.DesignRules` with the validated
  overrides applied, for both the Python ``DRCChecker`` (mutate
  ``checker.design_rules`` after construction) and native emission
  (``write_drc_constraints`` and friends) to consume identically.

This is deliberately narrower than "accept any override": the point is to
retain a *specific, cited, verified* board floor, not to let a board
silently loosen its own DRC gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path

from .base import DesignRules

__all__ = [
    "FabricationOverride",
    "UnsafeFabricationOverrideError",
    "apply_fabrication_overrides",
    "discover_fabrication_overrides_sidecar",
    "fabrication_overrides_sidecar_candidates",
    "load_fabrication_overrides",
    "resolve_pcb_fabrication_overrides",
    "validate_fabrication_override",
]

# The bare sidecar name probed next to a routed PCB, mirroring the
# ``fab_profile.json`` convention (Issue #3920): board directory, its
# ``output/`` subdirectory, and the parent's ``output/`` subdirectory
# (nearest wins). See ``kicad_tools.cli.check_cmd._discover_fab_profile_sidecar``.
FABRICATION_OVERRIDES_SIDECAR_BASENAME = "fabrication_overrides.json"

# Fields of DesignRules that a board is permitted to override at all.
# Deliberately an allow-list, not "any dataclass field": capability flags
# (``via_in_pad_supported``) and board-physical facts (``board_thickness_mm``,
# ``max_board_width_mm``) are not "fabrication floors" a board can assert a
# tighter/looser number for -- they either match the board or they don't.
_OVERRIDABLE_FIELDS: frozenset[str] = frozenset(
    {
        "min_trace_width_mm",
        "min_clearance_mm",
        "min_via_drill_mm",
        "min_via_diameter_mm",
        "min_annular_ring_mm",
        "min_pth_annular_ring_mm",
        "min_hole_diameter_mm",
        "min_copper_to_edge_mm",
        "min_hole_to_edge_mm",
        "min_hole_to_hole_mm",
    }
)

# Independently verified ABSOLUTE capability floors, keyed by manufacturer
# profile id then DesignRules field name.  This is distinct from -- and may
# be LOWER than -- the (often more conservative) operating default a
# profile's YAML ships: it exists so a per-board override request can be
# validated against real, cited manufacturer capability data rather than
# trusted blindly.
#
# Extend this table only with independently verified manufacturer capability
# data (cite the source in a comment), never to make one specific board's
# geometry pass.  A (manufacturer, field) pair with no entry here has no
# override permitted -- the override is rejected as unvalidated, not
# silently accepted (see ``validate_fabrication_override``).
_VERIFIED_ABSOLUTE_FLOORS_MM: dict[str, dict[str, float]] = {
    # https://jlcpcb.com/capabilities/pcb-capabilities/ -- "Min Space between
    # any two holes must be >= 0.45mm (0.0177Inch)" — verified 2026-09-10.
    "jlcpcb": {"min_hole_to_hole_mm": 0.45},
    "jlcpcb-tier1": {"min_hole_to_hole_mm": 0.45},
}


class UnsafeFabricationOverrideError(ValueError):
    """A requested per-board fabrication override failed validation.

    Raised for anything that would let a board silently weaken its DRC
    floor: an unrecognized field, a missing/empty citation, a manufacturer
    mismatch, an unregistered (manufacturer, field) pair, or a value below
    the verified capability floor.
    """


@dataclass(frozen=True)
class FabricationOverride:
    """A single, cited per-board override of one ``DesignRules`` field.

    Attributes:
        field: The ``DesignRules`` attribute name to override, e.g.
            ``"min_hole_to_hole_mm"``.
        value: The overriding value, in the field's native unit (mm).
        manufacturer_id: The manufacturer profile id (or alias) this
            override is scoped to -- it does not silently apply under a
            different profile.
        source: A citation (URL or document reference) for the verified
            capability this override asserts.
        reason: A short human-readable justification tying the override to
            the board's actual geometry.
        tracking_issue: A reference (URL or issue number) to the issue that
            reviewed and approved this override -- mandatory provenance so a
            narrower-than-profile floor is never asserted without a human
            decision trail to point back to.
    """

    field: str
    value: float
    manufacturer_id: str
    source: str
    reason: str
    tracking_issue: str


def fabrication_overrides_sidecar_candidates(pcb_path: str | Path) -> list[Path]:
    """Enumerate the fabrication-overrides sidecar paths to probe.

    Mirrors :func:`kicad_tools.cli.check_cmd._discover_fab_profile_sidecar`'s
    three-directory probe order (board dir, its ``output/``, and the
    parent's ``output/``), nearest wins.

    Args:
        pcb_path: Path to the ``*.kicad_pcb`` being checked/exported.

    Returns:
        Candidate paths in probe order.
    """
    pcb_dir = Path(pcb_path).parent
    return [
        pcb_dir / FABRICATION_OVERRIDES_SIDECAR_BASENAME,
        pcb_dir / "output" / FABRICATION_OVERRIDES_SIDECAR_BASENAME,
        pcb_dir.parent / "output" / FABRICATION_OVERRIDES_SIDECAR_BASENAME,
    ]


def discover_fabrication_overrides_sidecar(pcb_path: str | Path) -> Path | None:
    """Return the first existing fabrication-overrides sidecar, or ``None``."""
    for candidate in fabrication_overrides_sidecar_candidates(pcb_path):
        if candidate.is_file():
            return candidate
    return None


def load_fabrication_overrides(path: str | Path) -> list[FabricationOverride]:
    """Parse a fabrication-overrides sidecar JSON file.

    Expected shape::

        {
          "fabrication_overrides": {
            "min_hole_to_hole_mm": {
              "value": 0.45,
              "manufacturer": "jlcpcb-tier1",
              "source": "https://jlcpcb.com/capabilities/pcb-capabilities/",
              "reason": "Published pad-hole minimum; matches reviewed geometry",
              "tracking_issue": "https://github.com/rjwalters/kicad-tools/issues/5006"
            }
          }
        }

    Args:
        path: Path to the sidecar JSON file.

    Returns:
        One :class:`FabricationOverride` per declared field, in file order.

    Raises:
        ValueError: The file is not valid JSON, is not a JSON object, or an
            entry is missing a required key (``value``, ``manufacturer``,
            ``source``, ``reason``, ``tracking_issue``). This is a format
            error, distinct from :class:`UnsafeFabricationOverrideError` (a
            well-formed but unsafe request) -- callers should treat both as
            fatal.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: not valid JSON: {e}") from e

    entries = data.get("fabrication_overrides") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: missing top-level 'fabrication_overrides' object")

    overrides: list[FabricationOverride] = []
    required = ("value", "manufacturer", "source", "reason", "tracking_issue")
    for field_name, entry in entries.items():
        if not isinstance(entry, dict) or not all(k in entry for k in required):
            raise ValueError(
                f"{path}: override {field_name!r} must be an object with keys {required}"
            )
        try:
            value = float(entry["value"])
        except (TypeError, ValueError) as e:
            raise ValueError(f"{path}: override {field_name!r} has non-numeric value: {e}") from e
        overrides.append(
            FabricationOverride(
                field=field_name,
                value=value,
                manufacturer_id=str(entry["manufacturer"]),
                source=str(entry["source"]),
                reason=str(entry["reason"]),
                tracking_issue=str(entry["tracking_issue"]),
            )
        )
    return overrides


def validate_fabrication_override(
    override: FabricationOverride,
    *,
    manufacturer_id: str,
) -> None:
    """Validate a single override against the verified-floor registry.

    Args:
        override: The override to validate.
        manufacturer_id: The manufacturer profile id actually in effect
            (e.g. the resolved ``--mfr``).

    Raises:
        UnsafeFabricationOverrideError: The override is unrecognized,
            missing required provenance (``source``, ``reason``, or
            ``tracking_issue``), scoped to a different manufacturer, has no
            registered verified floor, or requests a value looser than that
            floor.
    """
    if override.field not in _OVERRIDABLE_FIELDS:
        raise UnsafeFabricationOverrideError(
            f"{override.field!r} is not an overridable fabrication field "
            f"(allowed: {sorted(_OVERRIDABLE_FIELDS)})"
        )
    if not {f.name for f in fields(DesignRules)} >= {override.field}:
        # Defensive: _OVERRIDABLE_FIELDS should always be a subset of
        # DesignRules' own fields, but never trust that silently.
        raise UnsafeFabricationOverrideError(
            f"{override.field!r} is not a recognized DesignRules field"
        )
    if not override.source.strip():
        raise UnsafeFabricationOverrideError(f"{override.field}: override must cite a 'source'")
    if not override.reason.strip():
        raise UnsafeFabricationOverrideError(f"{override.field}: override must state a 'reason'")
    if not override.tracking_issue.strip():
        raise UnsafeFabricationOverrideError(
            f"{override.field}: override must cite a 'tracking_issue' recording the "
            "review that approved it"
        )
    if override.manufacturer_id != manufacturer_id:
        raise UnsafeFabricationOverrideError(
            f"{override.field}: override is scoped to manufacturer "
            f"{override.manufacturer_id!r}, but the active profile is "
            f"{manufacturer_id!r} -- an override never silently carries "
            "across manufacturers"
        )
    floor = _VERIFIED_ABSOLUTE_FLOORS_MM.get(manufacturer_id, {}).get(override.field)
    if floor is None:
        raise UnsafeFabricationOverrideError(
            f"{override.field}: no independently verified capability floor "
            f"is on record for manufacturer {manufacturer_id!r} -- cannot "
            "validate this override, refusing to apply it unverified"
        )
    if override.value < floor:
        raise UnsafeFabricationOverrideError(
            f"{override.field}: requested override {override.value}mm is "
            f"below the verified {manufacturer_id!r} capability floor of "
            f"{floor}mm ({override.source}) -- refusing to apply an unsafe "
            "override"
        )


def apply_fabrication_overrides(
    rules: DesignRules,
    overrides: list[FabricationOverride],
    *,
    manufacturer_id: str,
) -> DesignRules:
    """Validate and apply per-board overrides onto a resolved ``DesignRules``.

    Every override is validated with :func:`validate_fabrication_override`
    before any is applied -- a single unsafe override in the list aborts the
    whole call with none applied, rather than partially applying a batch.

    Args:
        rules: The manufacturer-profile-resolved rules to override.
        overrides: Overrides to apply (e.g. from
            :func:`load_fabrication_overrides`). An empty list is a no-op
            that returns ``rules`` unchanged.
        manufacturer_id: The manufacturer profile id actually in effect.

    Returns:
        A new ``DesignRules`` with the validated overrides applied (or
        ``rules`` itself, unchanged, when ``overrides`` is empty).

    Raises:
        UnsafeFabricationOverrideError: Any override fails validation.
    """
    if not overrides:
        return rules
    for override in overrides:
        validate_fabrication_override(override, manufacturer_id=manufacturer_id)
    changes = {override.field: override.value for override in overrides}
    # mypy's `dataclasses.replace` overload cannot verify a **dict[str, float]
    # splat against DesignRules' mixed field types (it also carries bool
    # fields); every field name was already checked against `_OVERRIDABLE_FIELDS`
    # (itself checked against DesignRules' own fields) in the validation loop
    # above, so this is safe at runtime.
    return replace(rules, **changes)  # type: ignore[arg-type]


def resolve_pcb_fabrication_overrides(
    pcb_path: str | Path,
    rules: DesignRules,
    *,
    manufacturer_id: str,
) -> tuple[DesignRules, str | None]:
    """Discover, validate, and apply a board's fabrication-overrides sidecar.

    This is the single shared entry point every native-constraint-emission
    call site (``kct check --emit-drc-constraints``, the manufacturing
    export path, ``kct mfr apply-rules``, ``kct route``'s sidecar emission)
    and the Python ``DRCChecker`` should call once they have resolved a
    manufacturer profile's ``DesignRules`` for a specific PCB -- so a
    board-authored override is retained (or rejected) identically no matter
    which surface resolved the rules first (Issue #5006).

    A missing sidecar is a silent no-op: ``rules`` is returned unchanged and
    ``message`` is ``None``. A malformed or unsafe sidecar degrades
    gracefully -- never raises -- falling back to the conservative profile
    default already in ``rules`` and returning a one-line message the caller
    should surface as a warning (mirroring how ``kct check`` treats every
    other auto-discovered sidecar: an explicit, cited override is trusted;
    an invalid one never silently weakens the floor, it is simply ignored).

    Args:
        pcb_path: Path to the ``*.kicad_pcb`` being checked/exported.
        rules: The manufacturer-profile-resolved rules to (possibly)
            override.
        manufacturer_id: The manufacturer profile id actually in effect.

    Returns:
        ``(resolved_rules, message)``. ``message`` is ``None`` when there is
        nothing to report (no sidecar found). Otherwise a single-line
        string starting with ``"applied "`` (success) or ``"ignoring "``
        (failure) for the caller to log/print -- the prefix is a stable
        contract callers may use to route the message to info vs. warning.
    """
    # Deferred import: ``kicad_tools.manufacturers.__init__`` imports this
    # module at package-load time, so a module-level ``from . import
    # get_profile`` here would be a circular import at the time
    # ``__init__.py`` executes it. Safe as a deferred import because by the
    # time this function actually runs, package import has completed.
    from . import get_profile

    sidecar = discover_fabrication_overrides_sidecar(pcb_path)
    if sidecar is None:
        return rules, None
    # Callers pass whatever manufacturer id they resolved a profile from --
    # sometimes the canonical profile id (``profile.id``), sometimes a raw
    # ``--mfr`` alias (e.g. ``"jlc"`` for ``"jlcpcb"``). A sidecar always
    # declares the CANONICAL id (matching what ``get_profile`` returns), so
    # normalize here once rather than requiring every call site to resolve
    # aliases itself -- otherwise the identical sidecar would be accepted
    # via one call site (canonical id) and rejected via another (alias) for
    # the exact same board (Issue #5006 follow-up: this is the single
    # shared entry point, so alias-tolerance belongs here, not duplicated
    # at each of the four native-emission call sites).
    try:
        canonical_manufacturer_id = get_profile(manufacturer_id).id
    except ValueError:
        # An unrecognized manufacturer id would already have failed profile
        # resolution upstream (e.g. DRCChecker construction); fall back to
        # the raw value so validation below still reports a clear error
        # rather than masking it with an unrelated ValueError here.
        canonical_manufacturer_id = manufacturer_id
    try:
        overrides = load_fabrication_overrides(sidecar)
        resolved = apply_fabrication_overrides(
            rules, overrides, manufacturer_id=canonical_manufacturer_id
        )
    except (ValueError, UnsafeFabricationOverrideError) as e:
        return rules, (
            f"ignoring fabrication-overrides sidecar {sidecar}: {e}. Falling back to "
            "the manufacturer profile's default floor."
        )
    overridden_fields = ", ".join(sorted({o.field for o in overrides}))
    return resolved, f"applied fabrication-overrides sidecar {sidecar} ({overridden_fields})"
