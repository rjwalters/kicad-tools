"""Detect a rule-file rewrite that *loosens* authored design rules (Issue #7385).

kct has several writers that rewrite a board's rule files on an existing
project -- ``kct route`` / ``kct check --emit-drc-constraints`` (through
:func:`~kicad_tools.manufacturers.project_generator.merge_project_rules`),
``kct mfr apply-rules``, ``kicad-fab-rules apply`` and ``kct init``.  The
policy every one of them must follow is the #6191 one: **a rewrite may only
tighten authored rules**, unless the project explicitly opts into the legacy
overwrite (``KCT_PRESERVE_BOARD_RULES=0``) or the change is one of the few
documented exceptions.

:func:`rule_loosenings` is the one shared check.  It compares a rule file
*before* and *after* a rewrite and lists every way the rewrite made the rules
more permissive.  Writers call it before saving and refuse an unallowed
loosening; the guard test ``tests/test_rule_loosening_7385.py`` calls it on
every committed board.

The comparison currently understands the KiCad ``.kicad_pro`` shape.  The
signature deliberately takes plain mappings, so a later ``net_class_map.json``
variant (dropped ``swap_group``, lowered ``clearance``) can slot in behind the
same entry point without changing callers.

This module is pure (no kct imports): the *policy* that decides which
loosenings are allowed for a given project lives with the merge itself, in
:func:`kicad_tools.manufacturers.project_generator.merge_policy_allowances`.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard

__all__ = [
    "LOOSENING_CHANGED",
    "LOOSENING_EXCLUSION_ADDED",
    "LOOSENING_LOWERED",
    "LOOSENING_REMOVED",
    "LOOSENING_SEVERITY_DOWNGRADED",
    "NETCLASS_MINIMUM_KEYS",
    "NETCLASS_TARGET_KEYS",
    "Loosening",
    "RuleLooseningError",
    "assert_no_loosenings",
    "rule_loosenings",
]

#: A numeric minimum decreased.
LOOSENING_LOWERED = "lowered"
#: A ``rule_severities`` entry dropped rank (``error > warning > ignore``).
LOOSENING_SEVERITY_DOWNGRADED = "severity_downgraded"
#: A new ``drc_exclusions`` entry appeared.
LOOSENING_EXCLUSION_ADDED = "exclusion_added"
#: An authored rule, netclass, netclass key, pattern or assignment vanished.
LOOSENING_REMOVED = "removed"
#: An authored target value (diff-pair geometry, net assignment) was rewritten.
LOOSENING_CHANGED = "changed"

#: Netclass keys that are *floors*: a smaller value is more permissive.
NETCLASS_MINIMUM_KEYS: tuple[str, ...] = (
    "clearance",
    "track_width",
    "via_diameter",
    "via_drill",
    "microvia_diameter",
    "microvia_drill",
)

#: Netclass keys that are *targets*, not floors.  A diff-pair gap is the
#: coupling geometry the designer chose for an impedance; rewriting it in
#: either direction discards an authored decision (the #7385 reproduction
#: reset an authored 0.12 mm gap to the 0.25 mm template default).
NETCLASS_TARGET_KEYS: tuple[str, ...] = (
    "diff_pair_gap",
    "diff_pair_width",
    "diff_pair_via_gap",
)

_SEVERITY_RANK: dict[str, int] = {"ignore": 0, "warning": 1, "error": 2}

_TOLERANCE = 1e-9


@dataclass(frozen=True)
class Loosening:
    """One way a rewrite made the rules more permissive.

    Attributes:
        path: Stable dotted locator, e.g. ``rules.min_clearance``,
            ``defaults.clearance_min``, ``rule_severities.isolated_copper``,
            ``netclasses.Default.clearance``, ``netclasses.Power``,
            ``netclass_patterns.Power:VCC*``, ``netclass_assignments.GND``,
            ``drc_exclusions.<entry>``.  ``allow=`` patterns match against it.
        kind: One of the ``LOOSENING_*`` constants.
        before: The authored value (``None`` when it did not exist).
        after: The rewritten value (``None`` when it was removed).
    """

    path: str
    kind: str
    before: Any
    after: Any

    def describe(self) -> str:
        """Return a one-line human-readable description."""
        if self.kind == LOOSENING_LOWERED:
            return f"{self.path}: lowered {self.before} -> {self.after}"
        if self.kind == LOOSENING_SEVERITY_DOWNGRADED:
            return f"{self.path}: severity downgraded {self.before} -> {self.after}"
        if self.kind == LOOSENING_EXCLUSION_ADDED:
            return f"{self.path}: new DRC exclusion"
        if self.kind == LOOSENING_REMOVED:
            return f"{self.path}: removed (was {self.before})"
        return f"{self.path}: changed {self.before} -> {self.after}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for ``--format json`` payloads."""
        return {"path": self.path, "kind": self.kind, "before": self.before, "after": self.after}


class RuleLooseningError(ValueError):
    """A writer would have loosened authored rules without an explicit opt-in."""

    def __init__(self, loosenings: list[Loosening], writer: str = "") -> None:
        self.loosenings = loosenings
        self.writer = writer
        who = f"{writer}: " if writer else ""
        lines = "\n".join(f"  - {item.describe()}" for item in loosenings)
        super().__init__(
            f"{who}refusing to write: the rewrite would loosen "
            f"{len(loosenings)} authored rule(s):\n{lines}\n"
            "Rule rewrites may only tighten authored rules (Issue #6191/#7385). "
            "To deliberately replace the board's rules with the profile, set the "
            "project text variable KCT_PRESERVE_BOARD_RULES=0."
        )


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _get(mapping: object, *keys: str) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _is_rule_minimum_key(key: str) -> bool:
    return key.startswith("min_")


def _is_default_minimum_key(key: str) -> bool:
    # ``design_settings.defaults`` mixes floors (``track_min_width``,
    # ``clearance_min``, ``via_min_diameter``, ``via_min_drill``) with
    # cosmetic new-object defaults (silk text size, line widths).  Only the
    # floors are rules.
    return "_min" in key or key.startswith("min_")


def _compare_minima(
    out: list[Loosening],
    prefix: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    keys: Iterable[str],
) -> None:
    for key in keys:
        old = before.get(key)
        if not _is_number(old):
            continue
        if key not in after:
            out.append(Loosening(f"{prefix}.{key}", LOOSENING_REMOVED, old, None))
            continue
        new = after.get(key)
        if not _is_number(new):
            out.append(Loosening(f"{prefix}.{key}", LOOSENING_CHANGED, old, new))
        elif new < old - _TOLERANCE:
            out.append(Loosening(f"{prefix}.{key}", LOOSENING_LOWERED, old, new))


def _netclasses(data: object) -> dict[str, Mapping[str, Any]]:
    classes = _get(data, "net_settings", "classes")
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(classes, list):
        for cls in classes:
            if isinstance(cls, Mapping) and isinstance(cls.get("name"), str):
                result.setdefault(cls["name"], cls)
    return result


def _patterns(data: object) -> list[tuple[str, str]]:
    patterns = _get(data, "net_settings", "netclass_patterns")
    result: list[tuple[str, str]] = []
    if isinstance(patterns, list):
        for entry in patterns:
            if isinstance(entry, Mapping):
                result.append((str(entry.get("netclass", "")), str(entry.get("pattern", ""))))
    return result


def _assignments(data: object) -> dict[str, str]:
    raw = _get(data, "net_settings", "netclass_assignments")
    if isinstance(raw, Mapping):
        return {str(k): json.dumps(v, sort_keys=True) for k, v in raw.items()}
    return {}


def _exclusion_key(entry: object) -> str:
    return entry if isinstance(entry, str) else json.dumps(entry, sort_keys=True)


def _project_loosenings(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Loosening]:
    out: list[Loosening] = []

    settings_before = _as_mapping(_get(before, "board", "design_settings"))
    settings_after = _as_mapping(_get(after, "board", "design_settings"))

    # 1. Numeric floors in design_settings.rules / design_settings.defaults.
    rules_before = _as_mapping(settings_before.get("rules"))
    rules_after = _as_mapping(settings_after.get("rules"))
    _compare_minima(
        out,
        "rules",
        rules_before,
        rules_after,
        sorted(k for k in rules_before if _is_rule_minimum_key(k)),
    )
    defaults_before = _as_mapping(settings_before.get("defaults"))
    defaults_after = _as_mapping(settings_after.get("defaults"))
    _compare_minima(
        out,
        "defaults",
        defaults_before,
        defaults_after,
        sorted(k for k in defaults_before if _is_default_minimum_key(k)),
    )

    # 2. Severities: error > warning > ignore.  A removed entry falls back to
    #    KiCad's built-in severity, which we cannot know here -- report it.
    sev_before = _as_mapping(settings_before.get("rule_severities"))
    sev_after = _as_mapping(settings_after.get("rule_severities"))
    for key in sorted(sev_before):
        old = sev_before[key]
        if key not in sev_after:
            out.append(Loosening(f"rule_severities.{key}", LOOSENING_REMOVED, old, None))
            continue
        new = sev_after[key]
        old_rank = _SEVERITY_RANK.get(str(old))
        new_rank = _SEVERITY_RANK.get(str(new))
        if old_rank is not None and new_rank is not None and new_rank < old_rank:
            out.append(Loosening(f"rule_severities.{key}", LOOSENING_SEVERITY_DOWNGRADED, old, new))

    # 3. New DRC exclusions.
    excl_before = settings_before.get("drc_exclusions")
    excl_after = settings_after.get("drc_exclusions")
    known = {_exclusion_key(e) for e in excl_before} if isinstance(excl_before, list) else set()
    if isinstance(excl_after, list):
        for entry in excl_after:
            key = _exclusion_key(entry)
            if key not in known:
                out.append(
                    Loosening(f"drc_exclusions.{key}", LOOSENING_EXCLUSION_ADDED, None, entry)
                )

    # 4. Netclasses: removed classes, dropped keys, lowered floors, rewritten
    #    targets.
    classes_before = _netclasses(before)
    classes_after = _netclasses(after)
    for name in sorted(classes_before):
        old_cls = classes_before[name]
        prefix = f"netclasses.{name}"
        new_cls = classes_after.get(name)
        if new_cls is None:
            out.append(Loosening(prefix, LOOSENING_REMOVED, name, None))
            continue
        for key in sorted(old_cls):
            if key not in new_cls:
                out.append(Loosening(f"{prefix}.{key}", LOOSENING_REMOVED, old_cls[key], None))
        _compare_minima(
            out,
            prefix,
            old_cls,
            new_cls,
            [k for k in NETCLASS_MINIMUM_KEYS if k in new_cls],
        )
        for key in NETCLASS_TARGET_KEYS:
            if key in old_cls and key in new_cls:
                old, new = old_cls[key], new_cls[key]
                if _is_number(old) and _is_number(new):
                    if abs(old - new) > _TOLERANCE:
                        out.append(Loosening(f"{prefix}.{key}", LOOSENING_CHANGED, old, new))
                elif old != new:
                    out.append(Loosening(f"{prefix}.{key}", LOOSENING_CHANGED, old, new))

    # 5. Netclass patterns and explicit assignments.
    patterns_after = set(_patterns(after))
    for netclass, pattern in _patterns(before):
        if (netclass, pattern) not in patterns_after:
            out.append(
                Loosening(
                    f"netclass_patterns.{netclass}:{pattern}",
                    LOOSENING_REMOVED,
                    {"netclass": netclass, "pattern": pattern},
                    None,
                )
            )
    assign_before = _assignments(before)
    assign_after = _assignments(after)
    for net in sorted(assign_before):
        old = json.loads(assign_before[net])
        if net not in assign_after:
            out.append(Loosening(f"netclass_assignments.{net}", LOOSENING_REMOVED, old, None))
        elif assign_after[net] != assign_before[net]:
            out.append(
                Loosening(
                    f"netclass_assignments.{net}",
                    LOOSENING_CHANGED,
                    old,
                    json.loads(assign_after[net]),
                )
            )

    return out


def _allowed(path: str, allow: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in allow)


def rule_loosenings(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    allow: Iterable[str] = (),
) -> list[Loosening]:
    """Return every way ``after`` is more permissive than ``before``.

    Reports, for a ``.kicad_pro`` dict:

    * a ``board.design_settings.rules.min_*`` or ``design_settings.defaults``
      floor (``track_min_width``, ``clearance_min``, ...) that decreased or
      disappeared;
    * a ``rule_severities`` entry that dropped rank (``error > warning >
      ignore``) or disappeared;
    * a new ``drc_exclusions`` entry;
    * an authored netclass that was removed, a netclass key that was dropped,
      a netclass floor (:data:`NETCLASS_MINIMUM_KEYS`) that decreased, or a
      diff-pair target (:data:`NETCLASS_TARGET_KEYS`) that was rewritten;
    * a ``netclass_patterns`` entry that was removed, and a
      ``netclass_assignments`` entry that was removed or reassigned.

    Values ``before`` does not declare are never reported: adding a rule is
    not a loosening.

    Args:
        before: The rule file as authored (parsed JSON).
        after: The rule file a writer is about to save.
        allow: :mod:`fnmatch` patterns over :attr:`Loosening.path` for
            documented, permitted loosenings -- e.g. the #6191 non-blocking
            severities, or ``"*"`` under ``KCT_PRESERVE_BOARD_RULES=0``.  Build
            it with
            :func:`~kicad_tools.manufacturers.project_generator.merge_policy_allowances`.

    Returns:
        Unallowed loosenings, in a stable order; empty when the rewrite only
        tightened or kept every authored rule.
    """
    allow = tuple(allow)
    return [item for item in _project_loosenings(before, after) if not _allowed(item.path, allow)]


def assert_no_loosenings(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    allow: Iterable[str] = (),
    writer: str = "",
) -> None:
    """Raise :class:`RuleLooseningError` when ``after`` loosens ``before``.

    Writers call this immediately before saving, so a regression in the merge
    (or a new writer that bypasses it) refuses with a list of what it would
    have lowered instead of silently loosening the board's rules.
    """
    found = rule_loosenings(before, after, allow=allow)
    if found:
        raise RuleLooseningError(found, writer)
