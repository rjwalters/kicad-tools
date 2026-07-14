"""Loader and validator for the ``.courtyard_waivers.json`` sidecar (Issue #4137).

The waiver file is a versioned, human-editable JSON sidecar committed next to
the ``.kicad_pcb`` (mirroring the ``.constraints.json`` / ``net_class_map.json``
precedents).  Each entry waives one *pair* of footprint courtyards from the
``courtyards_overlap`` gate, so an intentional, EE-mandated exception (e.g. a
decoupling cap placed tight against its IC) is reported as ``WAIVED`` rather
than failing the board -- while any *new*, undocumented overlap still fails.

Schema (``version == 1``)::

    {
      "version": 1,
      "waivers": [
        {
          "rule": "courtyards_overlap",
          "refs": ["C52", "U10"],
          "reason": "EE-mandated tight decoupling, cap <=2mm from VCC pin",
          "issue": "chorus#13"
        }
      ]
    }

The ``refs`` pair is order-insensitive: ``["C52", "U10"]`` matches an overlap
reported as ``(U10, C52)`` and vice versa.

Discovery / loading contract mirrors ``net_class_map`` exactly:

* An explicit ``--courtyard-waivers <path>`` always wins and a malformed
  explicit file is a hard error.
* An auto-discovered sidecar that fails to parse degrades gracefully (the
  caller warns and continues with zero waivers).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The only schema version understood by this loader.  Reject other values with
# a clear error rather than silently misinterpreting a future schema.
SUPPORTED_VERSION = 1

# The rule id a waiver entry must target.  Kept as a constant so a future rule
# reusing this waiver format only has to widen this set.
_VALID_RULES = frozenset({"courtyards_overlap"})


@dataclass(frozen=True)
class CourtyardWaiver:
    """A single pair-level courtyard-overlap waiver entry.

    Attributes:
        rule: The rule id this waiver applies to (``"courtyards_overlap"``).
        refs: The unordered pair of component references, stored sorted so
            matching is order-insensitive.
        reason: Human-readable justification (non-empty).
        issue: Tracking reference, e.g. ``"chorus#13"`` (non-empty).
    """

    rule: str
    refs: tuple[str, str]
    reason: str
    issue: str


@dataclass
class CourtyardWaivers:
    """A loaded, validated collection of courtyard waiver entries."""

    entries: list[CourtyardWaiver] = field(default_factory=list)

    def match(self, ref_a: str, ref_b: str) -> CourtyardWaiver | None:
        """Return the waiver matching the unordered pair, or ``None``.

        Matching is order-insensitive: ``match("A", "B")`` and
        ``match("B", "A")`` return the same entry.
        """
        key = tuple(sorted((ref_a, ref_b)))
        for entry in self.entries:
            if entry.refs == key:
                return entry
        return None

    def __len__(self) -> int:
        return len(self.entries)


def courtyard_waivers_from_dict(data: Any) -> CourtyardWaivers:
    """Build :class:`CourtyardWaivers` from parsed JSON data.

    Raises:
        ValueError: if the top-level structure, ``version``, or any waiver
            entry is malformed.  The message identifies the offending entry's
            index / content so a human can fix the file quickly.
    """
    if not isinstance(data, dict):
        raise ValueError(f"courtyard-waivers file must be a JSON object, got {type(data).__name__}")

    if "version" not in data:
        raise ValueError("courtyard-waivers file is missing the required 'version' key")
    version = data["version"]
    if version != SUPPORTED_VERSION:
        raise ValueError(
            f"unsupported courtyard-waivers version {version!r} "
            f"(this build understands version {SUPPORTED_VERSION})"
        )

    raw_waivers = data.get("waivers", [])
    if not isinstance(raw_waivers, list):
        raise ValueError("courtyard-waivers 'waivers' must be a list")

    entries: list[CourtyardWaiver] = []
    for idx, raw in enumerate(raw_waivers):
        entries.append(_parse_entry(idx, raw))

    return CourtyardWaivers(entries=entries)


def _parse_entry(idx: int, raw: Any) -> CourtyardWaiver:
    """Validate and build one waiver entry, raising on any defect."""
    where = f"courtyard waiver #{idx}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object, got {type(raw).__name__}")

    rule = raw.get("rule")
    if not isinstance(rule, str) or not rule:
        raise ValueError(f"{where} is missing a non-empty string 'rule'")
    if rule not in _VALID_RULES:
        raise ValueError(
            f"{where} has unsupported rule {rule!r} (expected one of {sorted(_VALID_RULES)})"
        )

    refs = raw.get("refs")
    if not isinstance(refs, list) or len(refs) != 2:
        raise ValueError(f"{where} 'refs' must be a list of exactly 2 references")
    if not all(isinstance(r, str) and r for r in refs):
        raise ValueError(f"{where} 'refs' entries must be non-empty strings")
    if refs[0] == refs[1]:
        raise ValueError(f"{where} 'refs' must name two distinct components")

    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{where} is missing a non-empty 'reason'")

    issue = raw.get("issue")
    if not isinstance(issue, str) or not issue.strip():
        raise ValueError(f"{where} is missing a non-empty 'issue'")

    ordered = sorted(refs)
    return CourtyardWaiver(
        rule=rule,
        refs=(ordered[0], ordered[1]),
        reason=reason,
        issue=issue,
    )


def load_courtyard_waivers(path: Path) -> CourtyardWaivers:
    """Load and validate a ``.courtyard_waivers.json`` file.

    Raises:
        ValueError: if the file is not valid JSON or fails schema validation.
    """
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"parsing courtyard-waivers JSON: {e}") from e
    return courtyard_waivers_from_dict(data)


def discover_courtyard_waivers_sidecar(pcb_path: Path) -> Path | None:
    """Probe conventional locations for a ``.courtyard_waivers.json`` sidecar.

    Mirrors ``check_cmd._discover_net_class_map_sidecar``: probe the PCB
    directory, then a sibling ``output/`` subdir, then ``../output/``.

    Returns:
        The first existing candidate path, or ``None`` when no sidecar found.
    """
    pcb_dir = pcb_path.parent
    filename = ".courtyard_waivers.json"
    candidates = [
        pcb_dir / filename,
        pcb_dir / "output" / filename,
        pcb_dir.parent / "output" / filename,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
