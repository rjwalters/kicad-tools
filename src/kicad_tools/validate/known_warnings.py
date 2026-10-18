"""Exact, issue-keyed allowances for known warning findings on a board gate.

Some reviewed board gates (board 04's ``check_manufacturing.py``, board 05's
``redesign/build.py``) fail on *any* warning.  When a new warning rule lands
before its source fix, a board can carry real, understood findings that the
fix will remove.  This module lets such a gate accept exactly those findings
(Issue #7298), keyed by ``(rule_id, ref, pad)`` and citing the issue that
removes them -- never by rule name or count alone:

* a warning of any other rule, ref or pad is unaccepted;
* the same known ``(rule, ref, pad)`` reported twice is unaccepted (a higher
  count);
* errors are never examined here -- callers keep their error gate unchanged;
* fewer findings than listed (a partial source fix) is accepted.

Remove a board's entries when the cited fix lands.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: ``(rule_id, ref, pad)``.
KnownWarningKey = tuple[str, str, str]


def _field(violation: Any, name: str) -> Any:
    if isinstance(violation, Mapping):
        return violation.get(name)
    return getattr(violation, name, None)


def _is_active_warning(violation: Any) -> bool:
    return _field(violation, "severity") == "warning" and not _field(violation, "waived")


def warning_key(violation: Any) -> KnownWarningKey | None:
    """The ``(rule_id, ref, pad)`` a warning finding is about, or ``None``.

    Only rules whose ``items`` layout is known are keyed; any other rule
    returns ``None`` and so can never match an allowance.

    * ``thermal_asymmetry``: ``items = (ref, solid_pad[, other_pad])``.
    * ``via_at_pad_edge``: ``items = (via_ident, pad)``; ref is the pad's
      ``<ref>-<number>`` prefix.
    """
    rule = str(_field(violation, "rule_id") or "")
    items = tuple(str(i) for i in (_field(violation, "items") or ()))
    if rule == "thermal_asymmetry" and len(items) >= 2:
        ref, pad = items[0], items[1]
        if pad.startswith(f"{ref}-"):
            return (rule, ref, pad)
    elif rule == "via_at_pad_edge" and len(items) == 2:
        pad = items[1]
        ref, sep, number = pad.rpartition("-")
        if sep and ref and number:
            return (rule, ref, pad)
    return None


def _describe(violation: Any) -> str:
    rule = _field(violation, "rule_id")
    items = list(_field(violation, "items") or ())
    return f"{rule} {items}"


def unaccepted_warnings(
    violations: Iterable[Any], known: Mapping[KnownWarningKey, str]
) -> list[str]:
    """Active warnings ``known`` does not cover, as human-readable strings.

    ``violations`` may be :class:`~kicad_tools.validate.violations.DRCViolation`
    objects or their ``to_dict()`` form (a ``kct check`` JSON report's
    ``violations`` list).  ``known`` maps each allowed key to the issue that
    removes it.  An empty result means every active warning is known.
    """
    seen: set[KnownWarningKey] = set()
    extra: list[str] = []
    for violation in violations:
        if not _is_active_warning(violation):
            continue
        key = warning_key(violation)
        if key is None or key not in known:
            extra.append(_describe(violation))
        elif key in seen:
            extra.append(f"{_describe(violation)} (repeated)")
        else:
            seen.add(key)
    return extra


def unaccepted_report_warnings(
    report: Mapping[str, Any], known: Mapping[KnownWarningKey, str]
) -> list[str]:
    """:func:`unaccepted_warnings` for a ``kct check`` JSON report.

    Also refuses when ``summary.warnings`` disagrees with the active warning
    findings listed, so the allowance can never hide an unlisted warning.
    """
    violations = list(report.get("violations") or [])
    extra = unaccepted_warnings(violations, known)
    listed = sum(1 for v in violations if _is_active_warning(v))
    summary = (report.get("summary") or {}).get("warnings")
    if summary != listed:
        extra.append(f"summary reports {summary} warning(s) but {listed} are listed")
    return extra


def known_warning_count(violations: Iterable[Any], known: Mapping[KnownWarningKey, str]) -> int:
    """How many active warnings match an allowance (first occurrence only)."""
    seen: set[KnownWarningKey] = set()
    for violation in violations:
        if _is_active_warning(violation):
            key = warning_key(violation)
            if key is not None and key in known:
                seen.add(key)
    return len(seen)
