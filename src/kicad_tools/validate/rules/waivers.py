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

Evidence-bound waivers (schema ``version == 3``, Issue #5946)::

    {
      "version": 3,
      "waivers": [
        {
          "key": "courtyards_overlap|C52,U10||F.Cu",
          "evidence_hash": "ev2:3f0c9a1d2b7e4c55",
          "reason": "EE-mandated tight decoupling",
          "reviewer": "rjwalters",
          "date": "2026-10-06"
        }
      ]
    }

A keyed entry names one finding by its stable ``key`` (see
:mod:`kicad_tools.validate.evidence`) and is bound to the hash of the local
evidence that was reviewed.  When the geometry or nets under the finding
change, the hash no longer matches: the waiver is **stale**, the finding stays
active (it is never suppressed), it is annotated with the stale waiver's hash,
and a ``waiver_stale`` warning names the entry.  A waiver whose hash was
computed by an older evidence recipe (an ``ev1:`` hash after the ``ev2``
upgrade, Issue #6011) can never match; it is reported as stale with a
distinct *outdated evidence version* message, so a reviewer knows the cause
is the kct upgrade rather than a board edit.  ``reviewer`` and ``date`` are
required on keyed entries; ``issue`` is optional.  Version 3 files may also
carry legacy ``rule``/``items``/``nets`` entries, which may optionally add an
``evidence_hash`` of their own.  ``kct check --waive KEY --reason ...
--reviewer ...`` writes keyed entries with the current hash.  Version-2 files
keep loading unchanged; a build older than #5946 rejects a version-3 file
(so evidence binding can never be silently dropped).

Keyed entries are ``kct check``-only: the ``kct drc`` cross-gate
(:mod:`kicad_tools.drc.waivers`) ignores them, because kicad-cli findings have
no ``kct check`` key.

Discovery / loading contract mirrors ``courtyard_waivers`` exactly:

* An explicit ``--waivers <path>`` always wins and a malformed explicit file is a
  hard error.
* An auto-discovered ``.kct_waivers.json`` sidecar that fails to parse degrades
  gracefully (the caller warns and continues with zero waivers).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from kicad_tools.validate.evidence import (
    KEY_SEPARATOR,
    current_evidence_hash_version,
    evidence_hash_version,
    is_outdated_evidence_hash,
)
from kicad_tools.validate.violations import DRCResults, DRCViolation

# Schema versions understood by this loader.  Version 3 (Issue #5946) adds
# keyed, evidence-bound entries; version 2 files keep loading unchanged.
# Reject other values with a clear error rather than silently misinterpreting
# a future schema.
SUPPORTED_VERSIONS = (2, 3)
# The version written by :func:`write_keyed_waivers`.
CURRENT_VERSION = 3
# Backwards-compatible alias for callers that imported the old constant.
SUPPORTED_VERSION = CURRENT_VERSION

# Rule id for the advisory "unused waiver" info finding emitted when a loaded
# waiver entry matches no violation on the board (generalization of the
# courtyard-specific ``courtyard_waiver_unused``).
WAIVER_UNUSED_RULE_ID = "waiver_unused"

# Rule id for the warning emitted when an evidence-bound waiver names a finding
# whose local evidence has changed since review (Issue #5946).
WAIVER_STALE_RULE_ID = "waiver_stale"

# Rule-id namespace of ``kct detect-mistakes`` findings (Issue #6006).  The
# mistake checks share the ``.kct_waivers.json`` sidecar with ``kct check``;
# each command applies only its own entries, so a mistake waiver never reads
# as "unused" in ``kct check`` (and vice versa).
MISTAKE_RULE_PREFIX = "mistake."


def is_mistake_rule(rule: str) -> bool:
    """True when ``rule`` names a ``kct detect-mistakes`` check (Issue #6006)."""
    return rule.startswith(MISTAKE_RULE_PREFIX)


# A silk primitive item name that carries its own geometry, e.g.
# ``fp_line@-2.11/-2.11~-1.635/-2.11`` or ``J1 (fp_line@-1.62/6~-1.62/8.73)``
# (Issues #5946, #6015).  Older builds named the same finding by the bare
# primitive type (``fp_line`` / ``J1 (fp_line)``); stripping the ``@...``
# suffix recovers that coarse name so an unused legacy waiver can point at
# the per-line keys that replaced it.
_GEOMETRY_SUFFIX = re.compile(r"\b((?:fp|gr)_[a-z]+)@[^\s,|)]+")

# Copper tracks, arcs and vias named by geometry (``Trace@F.Cu:w0.25:...``,
# ``Via@25.2/14.9:F.Cu-B.Cu:d0.3/s0.6``, Issue #6088) and by the UUID prefix
# older builds used (``Trace-1a2b3c4d`` / ``Via-1a2b3c4d``; arcs were
# ``Trace-...`` too).  Both coarsen to the bare kind, so an unused UUID-named
# waiver can point at the geometry keys that replaced it.
_COPPER_GEOMETRY = re.compile(r"\b(Trace|Arc|Via|Run)@[^\s,|)]+")
_LEGACY_COPPER = re.compile(r"\b(Trace|Via)-[^\s,|)]+")

# ``width_consistency`` findings named every track of a run by its raw UUID
# (no ``Trace-`` prefix) and now name the run as ``Run@...``.  A bare UUID
# coarsens to ``Run`` too, and repeated ``Run`` items collapse to one, so a
# legacy multi-UUID waiver lines up with the one-item run finding.
_LEGACY_RUN_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Cap on the replacement keys listed in one unused-waiver hint.
_MAX_REWAIVE_HINT_KEYS = 5


def _coarsen(text: str) -> str:
    """Strip per-primitive geometry / identity from an item name."""
    text = _GEOMETRY_SUFFIX.sub(r"\1", text)
    text = _COPPER_GEOMETRY.sub(
        lambda m: m.group(1) if m.group(1) in ("Via", "Run") else "Trace", text
    )
    text = _LEGACY_RUN_UUID.sub("Run", text)
    return _LEGACY_COPPER.sub(r"\1", text)


def _coarsen_key(key: str) -> str:
    """:func:`_coarsen` every item of a finding key, re-sorting the items field."""
    parts = key.split(KEY_SEPARATOR)
    if len(parts) >= 2:
        items = sorted(_coarsen(item) for item in parts[1].split(",") if item)
        if items and all(item == "Run" for item in items):
            items = ["Run"]
        parts[1] = ",".join(items)
    return KEY_SEPARATOR.join(parts)


def _is_legacy_name(text: str) -> bool:
    """True when ``text`` (a key or item) uses a pre-geometry item name."""
    return bool(_LEGACY_COPPER.search(text)) or (
        not _COPPER_GEOMETRY.search(text) and not _GEOMETRY_SUFFIX.search(text)
    )


# ``physical_copper_gap`` (Issue #6106) named every copper source by its raw
# UUID, whatever its kind, so a UUID cannot be coarsened to a kind.  Such a
# waiver is matched to current findings of the same rule, nets and layer
# whose items include a geometry-named track, arc or via instead.
_PHYSICAL_GAP_RULE = "physical_copper_gap"


def _has_copper_geometry(items: Iterable[str]) -> bool:
    return any(_COPPER_GEOMETRY.search(i) for i in items)


def _physical_gap_candidates(entry: Waiver, findings: Iterable[DRCViolation]) -> list[str]:
    """Current ``physical_copper_gap`` keys a UUID-named ``entry`` would have matched."""
    if entry.key is not None:
        parts = entry.key.split(KEY_SEPARATOR)
        if len(parts) < 4 or parts[0] != _PHYSICAL_GAP_RULE:
            return []
        if any(_COPPER_GEOMETRY.search(p) for p in parts[1].split(",")):
            return []
    else:
        if entry.rule != _PHYSICAL_GAP_RULE or _has_copper_geometry(entry.items):
            return []
        parts = []
    keys: list[str] = []
    for v in findings:
        if v.waived or not isinstance(v, DRCViolation) or v.rule_id != _PHYSICAL_GAP_RULE:
            continue
        key = getattr(v, "key", "")
        if not _has_copper_geometry(v.items) or key == entry.key:
            continue
        if parts:
            vparts = key.split(KEY_SEPARATOR)
            hit = len(vparts) >= 4 and vparts[2:4] == parts[2:4]
        else:
            hit = not entry.nets or frozenset(v.nets) == entry.nets
        if hit and key not in keys:
            keys.append(key)
    return sorted(keys)


def _rewaive_candidates(entry: Waiver, findings: Iterable[DRCViolation]) -> list[str]:
    """Keys of active findings this unused ``entry`` named before geometry keys.

    Issue #6015: ``silkscreen_line_width`` (#5946) and ``silk_edge_clearance``
    (#6015) now name each offending silk line by its geometry, so a waiver
    written against the old coarse name (``fp_line`` / ``J1 (fp_line)``)
    matches nothing.  Issue #6088 did the same for copper: tracks and vias
    used to be named by UUID prefix (``Trace-1a2b3c4d``).  Return the finding
    keys that *would* have matched had their items been named the old way
    (for copper: same rule, nets, layer and item kinds), so the unused
    advisory can tell the user exactly what to re-waive.  Empty when nothing
    qualifies.
    """
    findings = list(findings)
    gap_keys = _physical_gap_candidates(entry, findings)
    if gap_keys:
        return gap_keys
    keys: list[str] = []
    if entry.key is not None:
        if not _is_legacy_name(entry.key):
            return keys
        entry_coarse = _coarsen_key(entry.key)
    else:
        if not all(_is_legacy_name(i) for i in entry.items):
            return keys
        entry_items = frozenset(_coarsen(i) for i in entry.items)
    for v in findings:
        if v.waived or not isinstance(v, DRCViolation):
            continue
        key = getattr(v, "key", "")
        if entry.key is not None:
            hit = key != entry.key and _coarsen_key(key) == entry_coarse
        else:
            coarse = frozenset(_coarsen(i) for i in v.items)
            hit = (
                v.rule_id == entry.rule
                and coarse != frozenset(v.items)
                and (not entry.items or coarse == entry_items)
                and (not entry.nets or frozenset(v.nets) == entry.nets)
            )
        if hit and key not in keys:
            keys.append(key)
    return sorted(keys)


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
        issue: Tracking reference, e.g. ``"chorus#18"`` (non-empty on legacy
            entries; optional -- empty string -- on keyed entries).
        key: Stable finding key (Issue #5946).  When set, the entry is a
            *keyed* entry: it matches findings whose :attr:`DRCViolation.key`
            equals it, and ``rule`` is derived from the key's first field.
        evidence_hash: Local-evidence hash the waiver was reviewed against.
            When set, a finding matched by identity but carrying a different
            hash is **not** waived -- the waiver is stale.
        reviewer: Who reviewed the finding (required on keyed entries).
        date: ISO date of the review (required on keyed entries).
    """

    rule: str
    items: frozenset[str]
    nets: frozenset[str]
    reason: str
    issue: str
    key: str | None = None
    evidence_hash: str | None = None
    reviewer: str | None = None
    date: str | None = None

    def matches_normalized(
        self,
        rule: str,
        items: frozenset[str] | set[str],
        nets: frozenset[str] | set[str],
    ) -> bool:
        """Engine-agnostic match core: rule id + normalized item / net sets.

        This is the single definition of the waiver matching semantics.  Both
        engines call it with their own already-normalized inputs so there is
        exactly one waiver vocabulary (Issue #4691):

        * ``kct check`` passes the finding's ``rule_id`` and its ``items`` /
          ``nets`` tuples verbatim (see :meth:`matches`).
        * ``kct drc`` passes KiCad's own ``type_str`` plus the refs extracted
          from the kicad-cli item *descriptions* (see
          :func:`kicad_tools.drc.waivers.apply_waivers_to_report`).

        Exact-set, order-insensitive match on ``items`` and (optionally)
        ``nets``.  An empty ``items`` / ``nets`` on the waiver means "do not
        constrain on that axis"; at least one axis is always populated (the
        loader enforces it).

        Keyed entries (Issue #5946) never match here: a kicad-cli finding has
        no ``kct check`` key, so they apply to ``kct check`` only.
        """
        if self.key is not None:
            return False
        if rule != self.rule:
            return False
        if self.items and frozenset(items) != self.items:
            return False
        if self.nets and frozenset(nets) != self.nets:
            return False
        return True

    def matches(self, violation: DRCViolation) -> bool:
        """Return True when this waiver *names* a ``kct check`` finding.

        Identity only: an evidence-bound entry whose hash no longer matches
        still "names" the finding (that is what makes it stale rather than
        unused).  See :meth:`evidence_matches`.
        """
        if self.key is not None:
            return getattr(violation, "key", None) == self.key
        return self.matches_normalized(
            violation.rule_id,
            frozenset(violation.items),
            frozenset(violation.nets),
        )

    def evidence_matches(self, violation: DRCViolation) -> bool:
        """True when this entry carries no hash, or its hash equals the finding's."""
        if self.evidence_hash is None:
            return True
        return _evidence_hash_of(violation) == self.evidence_hash

    @property
    def scope(self) -> str:
        """Human-readable description of what this entry names."""
        if self.key is not None:
            return f"key={self.key!r}"
        parts = []
        if self.items:
            parts.append(f"items={sorted(self.items)}")
        if self.nets:
            parts.append(f"nets={sorted(self.nets)}")
        return ", ".join(parts)


def _evidence_hash_of(violation: DRCViolation) -> str:
    existing: str | None = getattr(violation, "evidence_hash", None)
    if existing:
        return existing
    # Not annotated (library caller): hash the finding's own evidence only.
    from kicad_tools.validate.evidence import compute_evidence_hash

    return compute_evidence_hash(violation)


@dataclass
class Waivers:
    """A loaded, validated collection of general waiver entries."""

    entries: list[Waiver] = field(default_factory=list)

    def match(self, violation: DRCViolation) -> Waiver | None:
        """Return the first waiver that names ``violation`` with matching evidence."""
        for entry in self.entries:
            if entry.matches(violation) and entry.evidence_matches(violation):
                return entry
        return None

    def __len__(self) -> int:
        return len(self.entries)

    def for_check(self) -> Waivers:
        """The entries ``kct check`` applies: everything but mistake entries."""
        return Waivers(entries=[e for e in self.entries if not is_mistake_rule(e.rule)])

    def for_mistakes(self, rules: Iterable[str] | None = None) -> Waivers:
        """The entries ``kct detect-mistakes`` applies (Issue #6006).

        Only ``mistake.*`` entries; when ``rules`` is given, only those whose
        rule is in it (the checks that actually ran, so a ``--category``
        filter does not report every other category's waivers as unused).
        """
        wanted = None if rules is None else set(rules)
        return Waivers(
            entries=[
                e
                for e in self.entries
                if is_mistake_rule(e.rule) and (wanted is None or e.rule in wanted)
            ]
        )


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
    if version not in SUPPORTED_VERSIONS:
        understood = " and ".join(str(v) for v in SUPPORTED_VERSIONS)
        raise ValueError(
            f"unsupported waivers version {version!r} "
            f"(this build understands versions {understood})"
        )

    raw_waivers = data.get("waivers", [])
    if not isinstance(raw_waivers, list):
        raise ValueError("waivers 'waivers' must be a list")

    entries: list[Waiver] = []
    for idx, raw in enumerate(raw_waivers):
        entries.append(_parse_entry(idx, raw, version))

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


def _optional_str(where: str, raw: dict, name: str) -> str | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} '{name}' must be a non-empty string")
    return value


def _required_str(where: str, raw: dict, name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} is missing a non-empty '{name}'")
    return value


def _parse_keyed_entry(where: str, raw: dict) -> Waiver:
    """Validate a version-3 keyed, evidence-bound entry (Issue #5946)."""
    for legacy in ("rule", "items", "nets"):
        if legacy in raw:
            raise ValueError(
                f"{where} names a 'key'; it must not also carry '{legacy}' "
                "(the key already encodes the rule, items, nets and layer)"
            )
    key = _required_str(where, raw, "key")
    evidence_hash = _required_str(where, raw, "evidence_hash")
    reason = _required_str(where, raw, "reason")
    reviewer = _required_str(where, raw, "reviewer")
    date = _required_str(where, raw, "date")
    try:
        _dt.date.fromisoformat(date)
    except ValueError as e:
        raise ValueError(f"{where} 'date' must be an ISO date (YYYY-MM-DD): {e}") from e
    issue = _optional_str(where, raw, "issue") or ""
    rule = key.split("|", 1)[0]
    if not rule:
        raise ValueError(f"{where} 'key' must start with a rule id")
    return Waiver(
        rule=rule,
        items=frozenset(),
        nets=frozenset(),
        reason=reason,
        issue=issue,
        key=key,
        evidence_hash=evidence_hash,
        reviewer=reviewer,
        date=date,
    )


def _parse_entry(idx: int, raw: Any, version: int = 2) -> Waiver:
    """Validate and build one waiver entry, raising on any defect."""
    where = f"waiver #{idx}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object, got {type(raw).__name__}")

    if "key" in raw:
        if version < 3:
            raise ValueError(
                f"{where} is a keyed (evidence-bound) entry, which needs waivers version 3"
            )
        return _parse_keyed_entry(where, raw)

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

    evidence_hash = reviewer = date = None
    if version >= 3:
        evidence_hash = _optional_str(where, raw, "evidence_hash")
        reviewer = _optional_str(where, raw, "reviewer")
        date = _optional_str(where, raw, "date")
    return Waiver(
        rule=rule,
        items=items,
        nets=nets,
        reason=reason,
        issue=issue,
        evidence_hash=evidence_hash,
        reviewer=reviewer,
        date=date,
    )


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


def _waivers_sidecar_candidates(pcb_path: Path) -> list[Path]:
    pcb_dir = pcb_path.parent
    filename = ".kct_waivers.json"
    return [
        # Issue #5946: a per-board ``<board>.kct-waivers.json`` committed next
        # to the board wins over the shared directory-level sidecar.
        pcb_dir / f"{pcb_path.stem}.kct-waivers.json",
        pcb_dir / filename,
        pcb_dir / "output" / filename,
        pcb_dir.parent / "output" / filename,
    ]


def shadowed_waivers_sidecars(pcb_path: Path, chosen: Path) -> list[Path]:
    """Existing sidecars that :func:`discover_waivers_sidecar` passed over.

    Only one sidecar is loaded, so when a per-board
    ``<board>.kct-waivers.json`` exists every lower-priority
    ``.kct_waivers.json`` is ignored.  Callers warn with this list so entries
    in the shared file do not silently stop applying (Issue #5946 review).
    """
    try:
        chosen_resolved = chosen.resolve()
    except OSError:  # pragma: no cover - defensive
        chosen_resolved = chosen
    out: list[Path] = []
    for candidate in _waivers_sidecar_candidates(pcb_path):
        if candidate.is_file() and candidate.resolve() != chosen_resolved:
            if candidate.resolve() not in {p.resolve() for p in out}:
                out.append(candidate)
    return out


def discover_waivers_sidecar(pcb_path: Path) -> Path | None:
    """Probe conventional locations for a ``.kct_waivers.json`` sidecar.

    Probes ``<board>.kct-waivers.json`` next to the board first (Issue #5946),
    then mirrors :func:`courtyard_waivers.discover_courtyard_waivers_sidecar`
    for ``.kct_waivers.json``: the PCB directory, then a sibling ``output/``
    subdir, then ``../output/``.

    Only the first hit is loaded; see :func:`shadowed_waivers_sidecars` for
    the ones it passed over.

    Returns:
        The first existing candidate path, or ``None`` when no sidecar found.
    """
    for candidate in _waivers_sidecar_candidates(pcb_path):
        if candidate.is_file():
            return candidate
    return None


def apply_waivers(
    results: DRCResults, waivers: Waivers, *, waive_command: str = "kct check --waive"
) -> None:
    """Apply general waivers to ``results`` in place (post-check step).

    For each non-waived violation that a waiver entry names *and* whose
    evidence matches (entries without an ``evidence_hash`` always match),
    replace it with a ``waived=True`` copy carrying the entry's ``reason`` /
    ``issue``.  Because :class:`DRCViolation` is frozen, a new instance is
    built via :func:`dataclasses.replace`.  Findings already waived (e.g. by
    the per-rule courtyard path) are left untouched.

    Issue #5946: when the only entries naming a finding are evidence-bound
    and their hash differs from the finding's current
    :attr:`DRCViolation.evidence_hash`, the waiver is **stale**: the finding
    stays active, is annotated with the stale hash, and a
    :data:`WAIVER_STALE_RULE_ID` warning names the entry.  Annotate findings
    with :func:`kicad_tools.validate.evidence.annotate_evidence` first so the
    hash includes the board-level evidence.

    Any waiver entry that named no finding at all gets a
    :data:`WAIVER_UNUSED_RULE_ID` ``info`` advisory appended so leftover
    entries stay visible without failing the gate.

    ``waive_command`` is the command the stale / unused advisories tell the
    user to re-waive with (``kct detect-mistakes --waive`` for mistake
    findings, Issue #6006).
    """
    if not waivers.entries:
        return

    used: set[int] = set()
    stale_hits: dict[int, list[DRCViolation]] = {}
    rebuilt: list[DRCViolation] = []
    for v in results.violations:
        if v.waived:
            rebuilt.append(v)
            continue
        matched_idx: int | None = None
        stale_idx: int | None = None
        for idx, entry in enumerate(waivers.entries):
            if not entry.matches(v):
                continue
            if entry.evidence_matches(v):
                matched_idx = idx
                break
            if stale_idx is None:
                stale_idx = idx
        if matched_idx is not None:
            entry = waivers.entries[matched_idx]
            used.add(matched_idx)
            rebuilt.append(
                replace(
                    v,
                    waived=True,
                    waiver_reason=entry.reason,
                    waiver_issue=entry.issue or None,
                )
            )
            continue
        if stale_idx is not None:
            entry = waivers.entries[stale_idx]
            stale_hits.setdefault(stale_idx, []).append(v)
            rebuilt.append(
                replace(
                    v,
                    stale_waiver_hash=entry.evidence_hash,
                    stale_waiver_reason=entry.reason,
                )
            )
            continue
        rebuilt.append(v)

    findings = list(rebuilt)
    for idx, entry in enumerate(waivers.entries):
        if idx in used:
            continue
        if idx in stale_hits:
            current = sorted({_evidence_hash_of(v) for v in stale_hits[idx]})
            tracking = f" (tracking {entry.issue})" if entry.issue else ""
            reviewed = f" reviewed by {entry.reviewer}" if entry.reviewer else ""
            dated = f" on {entry.date}" if entry.date else ""
            if is_outdated_evidence_hash(entry.evidence_hash):
                # Issue #6011: the recipe changed, not (necessarily) the board.
                message = (
                    f"STALE waiver (outdated evidence version) for rule {entry.rule!r}"
                    f" ({entry.scope}){tracking}: it was recorded{reviewed}{dated} as"
                    f" {entry.evidence_hash}, an"
                    f" {evidence_hash_version(entry.evidence_hash)!s} hash, but this kct"
                    f" computes {current_evidence_hash_version(entry.evidence_hash)} hashes (now"
                    f" {', '.join(current)}). The evidence recipe changed in a kct"
                    " upgrade, so every older waiver goes stale once even if the"
                    " board is unchanged; re-review the finding and re-waive it"
                    f" ({waive_command}) to record the new hash."
                )
            else:
                message = (
                    f"STALE waiver for rule {entry.rule!r} ({entry.scope}){tracking}:"
                    f" the evidence{reviewed}{dated} was {entry.evidence_hash}, the"
                    f" board now gives {', '.join(current)}. The geometry or nets"
                    " under the finding changed, so the finding is active again;"
                    f" re-review it and re-waive ({waive_command}) if it is"
                    " still intentional."
                )
            rebuilt.append(
                DRCViolation(
                    rule_id=WAIVER_STALE_RULE_ID,
                    severity="warning",
                    message=message,
                    items=tuple(sorted(entry.items)),
                    nets=tuple(sorted(entry.nets)),
                )
            )
            continue
        tracking = f" (tracking {entry.issue})" if entry.issue else ""
        candidates = _rewaive_candidates(entry, findings)
        if candidates:
            shown = candidates[:_MAX_REWAIVE_HINT_KEYS]
            more = len(candidates) - len(shown)
            listed = ", ".join(repr(k) for k in shown) + (f" (+{more} more)" if more else "")
            hint = (
                " This rule now names silk lines, tracks and vias by their geometry,"
                " so the old item name no longer matches; review and re-waive the"
                f" matching finding(s) with {waive_command} KEY: {listed}."
            )
        else:
            hint = ""
        rebuilt.append(
            DRCViolation(
                rule_id=WAIVER_UNUSED_RULE_ID,
                severity="info",
                message=(
                    f"Waiver for rule {entry.rule!r} ({entry.scope}) matched no finding"
                    f"{tracking}; the underlying defect may already "
                    f"be resolved, or the rule/refs may have changed.{hint}"
                ),
                items=tuple(sorted(entry.items)),
                nets=tuple(sorted(entry.nets)),
            )
        )

    results.violations = rebuilt


def write_keyed_waivers(
    path: Path,
    findings: Iterable[DRCViolation],
    *,
    reason: str,
    reviewer: str,
    issue: str | None = None,
    date: str | None = None,
) -> int:
    """Record evidence-bound waivers for ``findings`` in ``path`` (Issue #5946).

    Writes one keyed entry per distinct ``(key, evidence_hash)`` among
    ``findings``, binding each to the finding's *current* evidence hash.  Any
    existing keyed entry for the same key is replaced -- re-waiving a stale
    finding is the re-review.  Other entries (legacy or for other keys) are
    preserved verbatim.  The file is (re)written as schema version 3.

    Raises:
        ValueError: when ``reason`` / ``reviewer`` is empty, a finding has no
            evidence hash, or an existing file fails validation (it is never
            clobbered).

    Returns:
        The number of entries written.
    """
    if not reason.strip():
        raise ValueError("a waiver needs a non-empty reason")
    if not reviewer.strip():
        raise ValueError("a waiver needs a non-empty reviewer")
    day = date or _dt.date.today().isoformat()

    raw_entries: list[Any] = []
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"parsing waivers JSON {path}: {e}") from e
        waivers_from_dict(existing)  # validate before touching it
        raw_entries = list(existing.get("waivers", []))

    new_entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        if not finding.evidence_hash:
            raise ValueError(f"finding {finding.key!r} has no evidence hash to bind to")
        ident = (finding.key, finding.evidence_hash)
        if ident in seen:
            continue
        seen.add(ident)
        entry = {
            "key": finding.key,
            "evidence_hash": finding.evidence_hash,
            "reason": reason,
            "reviewer": reviewer,
            "date": day,
        }
        if issue:
            entry["issue"] = issue
        new_entries.append(entry)

    keys = {key for key, _ in seen}
    kept = [e for e in raw_entries if not (isinstance(e, dict) and e.get("key") in keys)]
    data = {"version": CURRENT_VERSION, "waivers": [*kept, *new_entries]}
    waivers_from_dict(data)  # never write a file this build cannot read back
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return len(new_entries)
