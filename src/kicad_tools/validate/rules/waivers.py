"""General ``.kct_waivers.json`` waiver mechanism for ``kct check`` (Issue #4417).

This generalizes the pair-level courtyard waiver infrastructure that shipped as
Issue #4137 (``.courtyard_waivers.json``, see
:mod:`kicad_tools.validate.rules.courtyard_waivers`) to a *central*,
rule-agnostic waiver step: any ``rule_id`` emitted by the checker can be waived
by matching the violation's ``items`` (and, optionally, ``nets``) *set* against
a committed, human-editable sidecar.

Unlike the courtyard loader -- which is bound to the single ``courtyards_overlap``
rule, matches a fixed unordered *ref pair*, and applies waivers *inside* the
rule while it runs -- this module:

* accepts **any** ``rule`` id (no hard-coded rule allow-list),
* matches on the violation's ``items`` set (exact set, order-insensitive) and an
  optional ``nets`` set (for net-scoped findings such as clearance shorts), and
* applies waivers as a **post-check** step (:func:`apply_waivers`) that runs once
  after ``DRCChecker.check_all()``, replacing each matched finding with a
  ``waived=True`` copy.

Schema (``version == 2``)::

    {
      "version": 2,
      "waivers": [
        {
          "rule": "courtyards_overlap",
          "items": ["C52", "U10"],
          "reason": "EE-mandated tight decoupling, <=2mm from U10 VCC",
          "issue": "chorus#18"
        },
        {
          "rule": "clearance_pad_pad",
          "nets": ["GND", "VBUS"],
          "reason": "documented star-ground tie",
          "issue": "chorus#20"
        }
      ]
    }

Matching semantics (exact-set, order-insensitive):

* A waiver matches a violation when ``violation.rule_id == waiver.rule`` **and**,
  when the waiver names ``items``, ``set(violation.items) == set(waiver.items)``
  **and**, when the waiver names ``nets``, ``set(violation.nets) == set(waiver.nets)``.
* Exact-set (not subset): a 2-item entry does **NOT** waive a 3-item finding.
* At least one of ``items`` / ``nets`` must be present so a waiver cannot match
  every finding for a rule blindly.

Manufacturing-gate safety (mirrors the #4403 ``gate_passed`` precedent): a
waived finding keeps its underlying ``severity`` (typically ``"error"``) in the
JSON output while reporting ``status: "waived"``.  ``kct check``'s own exit gate
keys off ``is_error`` (waived excluded -> intended relief), but ``kct audit``
re-parses the JSON ``severity`` field and ignores ``waived``, so a waived
finding **stays blocking in the manufacturing gate by default**.

Discovery / loading contract mirrors ``courtyard_waivers`` exactly:

* An explicit ``--waivers <path>`` always wins and a malformed explicit file is a
  hard error.
* An auto-discovered ``.kct_waivers.json`` sidecar that fails to parse degrades
  gracefully (the caller warns and continues with zero waivers).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from kicad_tools.validate.violations import DRCResults, DRCViolation

# The only schema version understood by this loader.  Reject other values with
# a clear error rather than silently misinterpreting a future schema.
SUPPORTED_VERSION = 2

# Rule id for the advisory "unused waiver" info finding emitted when a loaded
# waiver entry matches no violation on the board (generalization of the
# courtyard-specific ``courtyard_waiver_unused``).
WAIVER_UNUSED_RULE_ID = "waiver_unused"


@dataclass(frozen=True)
class Waiver:
    """A single general waiver entry.

    Attributes:
        rule: The ``rule_id`` this waiver applies to (any checker rule).
        items: Unordered set of item references (e.g. ``{"C52", "U10"}``).
            Empty when the waiver is matched purely by ``nets``.
        nets: Unordered set of net names.  Empty when the waiver is matched
            purely by ``items``.
        reason: Human-readable justification (non-empty).
        issue: Tracking reference, e.g. ``"chorus#18"`` (non-empty).
    """

    rule: str
    items: frozenset[str]
    nets: frozenset[str]
    reason: str
    issue: str

    def matches(self, violation: DRCViolation) -> bool:
        """Return True when this waiver applies to ``violation``.

        Exact-set, order-insensitive match on ``items`` and (optionally)
        ``nets``.  An empty ``items`` / ``nets`` on the waiver means "do not
        constrain on that axis"; at least one axis is always populated (the
        loader enforces it).
        """
        if violation.rule_id != self.rule:
            return False
        if self.items and frozenset(violation.items) != self.items:
            return False
        if self.nets and frozenset(violation.nets) != self.nets:
            return False
        return True


@dataclass
class Waivers:
    """A loaded, validated collection of general waiver entries."""

    entries: list[Waiver] = field(default_factory=list)

    def match(self, violation: DRCViolation) -> Waiver | None:
        """Return the first waiver matching ``violation``, or ``None``."""
        for entry in self.entries:
            if entry.matches(violation):
                return entry
        return None

    def __len__(self) -> int:
        return len(self.entries)


def waivers_from_dict(data: Any) -> Waivers:
    """Build :class:`Waivers` from parsed JSON data.

    Raises:
        ValueError: if the top-level structure, ``version``, or any waiver
            entry is malformed.  The message identifies the offending entry's
            index so a human can fix the file quickly.
    """
    if not isinstance(data, dict):
        raise ValueError(f"waivers file must be a JSON object, got {type(data).__name__}")

    if "version" not in data:
        raise ValueError("waivers file is missing the required 'version' key")
    version = data["version"]
    if version != SUPPORTED_VERSION:
        raise ValueError(
            f"unsupported waivers version {version!r} "
            f"(this build understands version {SUPPORTED_VERSION})"
        )

    raw_waivers = data.get("waivers", [])
    if not isinstance(raw_waivers, list):
        raise ValueError("waivers 'waivers' must be a list")

    entries: list[Waiver] = []
    for idx, raw in enumerate(raw_waivers):
        entries.append(_parse_entry(idx, raw))

    return Waivers(entries=entries)


def _parse_str_set(where: str, key: str, raw: Any) -> frozenset[str]:
    """Validate an optional list-of-non-empty-strings field into a frozenset."""
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise ValueError(f"{where} '{key}' must be a list of strings")
    if not all(isinstance(x, str) and x for x in raw):
        raise ValueError(f"{where} '{key}' entries must be non-empty strings")
    return frozenset(raw)


def _parse_entry(idx: int, raw: Any) -> Waiver:
    """Validate and build one waiver entry, raising on any defect."""
    where = f"waiver #{idx}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object, got {type(raw).__name__}")

    rule = raw.get("rule")
    if not isinstance(rule, str) or not rule:
        raise ValueError(f"{where} is missing a non-empty string 'rule'")

    items = _parse_str_set(where, "items", raw.get("items"))
    nets = _parse_str_set(where, "nets", raw.get("nets"))
    if not items and not nets:
        raise ValueError(f"{where} must name at least one 'items' or 'nets' entry")

    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{where} is missing a non-empty 'reason'")

    issue = raw.get("issue")
    if not isinstance(issue, str) or not issue.strip():
        raise ValueError(f"{where} is missing a non-empty 'issue'")

    return Waiver(rule=rule, items=items, nets=nets, reason=reason, issue=issue)


def load_waivers(path: Path) -> Waivers:
    """Load and validate a ``.kct_waivers.json`` file.

    Raises:
        ValueError: if the file is not valid JSON or fails schema validation.
    """
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"parsing waivers JSON: {e}") from e
    return waivers_from_dict(data)


def discover_waivers_sidecar(pcb_path: Path) -> Path | None:
    """Probe conventional locations for a ``.kct_waivers.json`` sidecar.

    Mirrors :func:`courtyard_waivers.discover_courtyard_waivers_sidecar`: probe
    the PCB directory, then a sibling ``output/`` subdir, then ``../output/``.

    Returns:
        The first existing candidate path, or ``None`` when no sidecar found.
    """
    pcb_dir = pcb_path.parent
    filename = ".kct_waivers.json"
    candidates = [
        pcb_dir / filename,
        pcb_dir / "output" / filename,
        pcb_dir.parent / "output" / filename,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def apply_waivers(results: DRCResults, waivers: Waivers) -> None:
    """Apply general waivers to ``results`` in place (post-check step).

    For each non-waived violation that matches a waiver entry, replace it with a
    ``waived=True`` copy carrying the entry's ``reason`` / ``issue``.  Because
    :class:`DRCViolation` is frozen, a new instance is built via
    :func:`dataclasses.replace`.  Findings already waived (e.g. by the
    per-rule courtyard path) are left untouched.

    Any waiver entry that matched no finding gets a :data:`WAIVER_UNUSED_RULE_ID`
    ``info`` advisory appended so stale entries stay visible without failing the
    gate.
    """
    if not waivers.entries:
        return

    used: set[int] = set()
    rebuilt: list[DRCViolation] = []
    for v in results.violations:
        if v.waived:
            rebuilt.append(v)
            continue
        matched_idx: int | None = None
        for idx, entry in enumerate(waivers.entries):
            if entry.matches(v):
                matched_idx = idx
                break
        if matched_idx is None:
            rebuilt.append(v)
            continue
        entry = waivers.entries[matched_idx]
        used.add(matched_idx)
        rebuilt.append(
            replace(
                v,
                waived=True,
                waiver_reason=entry.reason,
                waiver_issue=entry.issue,
            )
        )

    for idx, entry in enumerate(waivers.entries):
        if idx in used:
            continue
        scope_parts = []
        if entry.items:
            scope_parts.append(f"items={sorted(entry.items)}")
        if entry.nets:
            scope_parts.append(f"nets={sorted(entry.nets)}")
        scope = ", ".join(scope_parts)
        rebuilt.append(
            DRCViolation(
                rule_id=WAIVER_UNUSED_RULE_ID,
                severity="info",
                message=(
                    f"Waiver for rule {entry.rule!r} ({scope}) matched no finding "
                    f"(tracking {entry.issue}); the underlying defect may already "
                    "be resolved, or the rule/refs may have changed."
                ),
                items=tuple(sorted(entry.items)),
                nets=tuple(sorted(entry.nets)),
            )
        )

    results.violations = rebuilt
