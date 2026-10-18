"""Known open issues recorded in a board's readiness report (Issue #7261).

A readiness report can say "ready" only about the checks it ran.  Defects and
exceptions found by a person, and filed as issues, used to have nowhere to go,
so a board with an open defect still read "ready".  ``known_issues`` is the one
per-board record of those issues:

* ``defect``    -- the board fails its own spec.  Any open defect makes the
  readiness status ``blocked``.
* ``exception`` -- an approved deviation (for example a paid fab option the
  generic tier cannot model).  Disclosed; the board stays ``ready``.
* ``advisory``  -- a known risk or ordering requirement.  Disclosed; the board
  stays ``ready``.

Each entry is ``{"issue": N, "kind": ..., "summary": ..., "recorded": "YYYY-MM-DD"}``
plus an optional ``"warnings": {rule_id: max_count}`` naming the ``kct check``
warnings the issue accounts for.  Board scripts that tolerate known warnings
should read that allowance (:func:`known_warning_allowance`) instead of
keeping their own list.

The list is hash-bound evidence: the producer also writes it to a
``known-issues.json`` file in the evidence directory, hashed into the report's
``inputs``, and :func:`kicad_tools.cli.board_readiness.read_readiness` rejects a
report whose list differs from that file.  Validation is format-only and
offline; nothing here contacts the issue tracker.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

__all__ = [
    "KNOWN_ISSUES_FILENAME",
    "KNOWN_ISSUE_KINDS",
    "KnownIssueError",
    "defects",
    "known_warning_allowance",
    "merge_known_issues",
    "parse_known_issue",
    "validate_known_issues",
]

KNOWN_ISSUE_KINDS = ("defect", "exception", "advisory")
#: Evidence file that binds the list into the report's hashed ``inputs``.
KNOWN_ISSUES_FILENAME = "known-issues.json"

_RULE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_WARNINGS_RE = re.compile(r"^[A-Za-z0-9_.-]+=\d+(,[A-Za-z0-9_.-]+=\d+)*$")
_ENTRY_KEYS = {"issue", "kind", "summary", "recorded", "warnings"}


class KnownIssueError(ValueError):
    """A ``--known-issue`` value or a recorded ``known_issues`` list is malformed."""


def _check_warnings(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value:
        raise KnownIssueError("warnings must be a non-empty {rule: count} object")
    out: dict[str, int] = {}
    for rule, count in value.items():
        if not isinstance(rule, str) or not _RULE_RE.match(rule):
            raise KnownIssueError(f"invalid warning rule id: {rule!r}")
        if type(count) is not int or count < 1:
            raise KnownIssueError(f"warning count for {rule} must be a positive integer")
        out[rule] = count
    return dict(sorted(out.items()))


def _check_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise KnownIssueError("known issue must be an object")
    extra = set(entry) - _ENTRY_KEYS
    if extra:
        raise KnownIssueError(f"unknown known-issue field(s): {', '.join(sorted(extra))}")
    issue = entry.get("issue")
    if type(issue) is not int or issue < 1:
        raise KnownIssueError("known issue number must be a positive integer")
    kind = entry.get("kind")
    if kind not in KNOWN_ISSUE_KINDS:
        raise KnownIssueError(
            f"known issue #{issue}: kind must be one of {', '.join(KNOWN_ISSUE_KINDS)}"
        )
    summary = entry.get("summary")
    if not isinstance(summary, str) or not summary.strip() or summary != summary.strip():
        raise KnownIssueError(f"known issue #{issue}: summary must be non-empty, trimmed text")
    recorded = entry.get("recorded")
    try:
        if not isinstance(recorded, str) or date.fromisoformat(recorded).isoformat() != recorded:
            raise ValueError
    except ValueError:
        raise KnownIssueError(
            f"known issue #{issue}: recorded must be an ISO date (YYYY-MM-DD)"
        ) from None
    out: dict[str, Any] = {"issue": issue, "kind": kind, "summary": summary, "recorded": recorded}
    if "warnings" in entry:
        out["warnings"] = _check_warnings(entry["warnings"])
    return out


def validate_known_issues(value: Any) -> list[dict[str, Any]]:
    """Return the normalised list, or raise :class:`KnownIssueError`.

    ``None`` (field absent) is the empty list.  Entries must be unique by issue
    number and sorted by it, so the recorded form is canonical.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise KnownIssueError("known_issues must be a list")
    entries = [_check_entry(entry) for entry in value]
    numbers = [entry["issue"] for entry in entries]
    if len(set(numbers)) != len(numbers):
        raise KnownIssueError("known_issues lists an issue more than once")
    if numbers != sorted(numbers):
        raise KnownIssueError("known_issues must be sorted by issue number")
    return entries


def parse_known_issue(spec: str, recorded: str) -> dict[str, Any]:
    """Parse ``N:summary:kind[:rule=count,...]`` (the ``--known-issue`` form).

    The number is split from the left and the kind (plus the optional warning
    allowance) from the right, so the summary may itself contain ``:``.
    """
    head, sep, rest = spec.partition(":")
    if not sep:
        raise KnownIssueError(f"--known-issue {spec!r}: expected N:summary:kind")
    number = head.strip().lstrip("#")
    if not number.isdigit() or int(number) < 1:
        raise KnownIssueError(f"--known-issue {spec!r}: issue number must be a positive integer")
    summary, sep, kind = rest.rpartition(":")
    if not sep:
        raise KnownIssueError(f"--known-issue {spec!r}: expected N:summary:kind")
    warnings: dict[str, int] | None = None
    if _WARNINGS_RE.match(kind.strip()) and ":" in summary:
        allowance = kind.strip()
        summary, _, kind = summary.rpartition(":")
        warnings = {}
        for item in allowance.split(","):
            rule, _, count = item.partition("=")
            if rule in warnings:
                raise KnownIssueError(f"--known-issue {spec!r}: rule {rule} listed twice")
            warnings[rule] = int(count)
    entry: dict[str, Any] = {
        "issue": int(number),
        "kind": kind.strip(),
        "summary": summary.strip(),
        "recorded": recorded,
    }
    if warnings is not None:
        entry["warnings"] = warnings
    try:
        return _check_entry(entry)
    except KnownIssueError as exc:
        raise KnownIssueError(f"--known-issue {spec!r}: {exc}") from None


def merge_known_issues(
    previous: Iterable[Mapping[str, Any]],
    added: Iterable[Mapping[str, Any]],
    resolved: Iterable[int] = (),
) -> list[dict[str, Any]]:
    """Carry *previous* forward, apply *added* (replacing by number), drop *resolved*.

    A recorded issue stays on the board until someone names it as resolved:
    regenerating evidence must never silently drop a disclosure.  An entry
    re-stated unchanged keeps its original ``recorded`` date.
    """
    current = {int(entry["issue"]): dict(entry) for entry in previous}
    resolved = set(resolved)
    unknown = sorted(resolved - set(current))
    if unknown:
        raise KnownIssueError(
            "--resolve-known-issue names issue(s) not recorded on this board: "
            + ", ".join(f"#{n}" for n in unknown)
        )
    seen: set[int] = set()
    for entry in added:
        number = int(entry["issue"])
        if number in seen:
            raise KnownIssueError(f"--known-issue lists #{number} more than once")
        if number in resolved:
            raise KnownIssueError(f"#{number} is both added and resolved")
        seen.add(number)
        old = current.get(number)
        new = dict(entry)
        if old is not None and {k: v for k, v in old.items() if k != "recorded"} == {
            k: v for k, v in new.items() if k != "recorded"
        }:
            new["recorded"] = old["recorded"]
        current[number] = new
    for number in resolved:
        current.pop(number)
    return validate_known_issues([current[n] for n in sorted(current)])


def defects(entries: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The entries of kind ``defect`` -- each one keeps a board from ``ready``."""
    return [entry for entry in entries if entry.get("kind") == "defect"]


def known_warning_allowance(entries: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """``{rule_id: max count}`` the board's recorded issues account for.

    Counts for a rule named by several issues add up.  A board script gating
    on ``kct check`` warnings should tolerate exactly this allowance, so the
    tolerated warnings and their disclosure come from the same record.
    """
    allowance: dict[str, int] = {}
    for entry in entries:
        for rule, count in (entry.get("warnings") or {}).items():
            allowance[rule] = allowance.get(rule, 0) + int(count)
    return dict(sorted(allowance.items()))
