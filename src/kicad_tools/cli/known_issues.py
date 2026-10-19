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
plus an optional ``"warnings": [{"rule", "ref", "pad"}, ...]`` naming the exact
``kct check`` warning findings the issue accounts for (Issue #7389).  The
identity is the one :func:`kicad_tools.validate.known_warnings.warning_key`
yields, so a *different* warning of the same rule cannot hide behind a known
one, and an identity that no longer fires is stale.  Board scripts that
tolerate known warnings should read that allowance
(:func:`known_warning_allowance`) and match with :func:`match_warning_allowance`
instead of keeping their own list.

An ``exception`` entry may likewise carry ``"errors": [{"rule", "at": [x_mm, y_mm]}, ...]``
naming the individual ``kct check`` errors the approved deviation accounts for
(:func:`known_error_allowance`, Issue #7355).  This is the same structured
identity shape as ``warnings`` (``rule`` plus where the finding is), with ``at``
the violation's board location in mm rounded to 0.01 (:func:`error_identity`)
for errors that have no ref/pad, such as vias.  An identity may be listed by only
one issue, whether it is a warning or an error.  The readiness ``kct_check`` gate
passes only when the report's errors match the listed identities as a set.

The list is hash-bound evidence: the producer also writes it to a
``known-issues.json`` file in the evidence directory, hashed into the report's
``inputs``, and :func:`kicad_tools.cli.board_readiness.read_readiness` rejects a
report whose list differs from that file.  Validation is format-only and
offline; nothing here contacts the issue tracker.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

__all__ = [
    "KNOWN_ISSUES_FILENAME",
    "KNOWN_ISSUE_KINDS",
    "KnownIssueError",
    "defects",
    "error_identity",
    "identity_key",
    "known_error_allowance",
    "known_warning_allowance",
    "match_warning_allowance",
    "merge_known_issues",
    "parse_known_issue",
    "validate_known_issues",
]

KNOWN_ISSUE_KINDS = ("defect", "exception", "advisory")
#: Evidence file that binds the list into the report's hashed ``inputs``.
KNOWN_ISSUES_FILENAME = "known-issues.json"

_RULE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_IDENT_RE = r"[A-Za-z0-9_.-]+@[^\s,:@]+"
_COORD = r"-?\d+(?:\.\d+)?"
_ERROR_ITEM = rf"![A-Za-z0-9_.-]+@{_COORD}/{_COORD}"
_ERROR_ITEM_RE = re.compile(rf"^!([A-Za-z0-9_.-]+)@({_COORD})/({_COORD})$")
_ITEM = rf"(?:{_IDENT_RE}|{_ERROR_ITEM})"
#: ``--known-issue`` allowance: ``rule@pad`` (warning) or ``!rule@x/y`` (error) items.
_ALLOWANCE_RE = re.compile(rf"^{_ITEM}(,{_ITEM})*$")
#: Identity shapes: ``pad`` is a pad-scoped ``{rule, ref, pad}``, ``at`` a located ``{rule, at}``.
_SHAPE_KEYS = {"pad": ("rule", "ref", "pad"), "at": ("rule", "at")}
_SORT_ORDER = {"pad": "(rule, ref, pad)", "at": "(rule, x, y)"}
#: The shapes each field accepts.  The matchers handle exactly these.
_WARNING_SHAPES = ("pad",)
_ERROR_SHAPES = ("at",)
_ENTRY_KEYS = {"issue", "kind", "summary", "recorded", "warnings", "errors"}


class KnownIssueError(ValueError):
    """A ``--known-issue`` value or a recorded ``known_issues`` list is malformed."""


def _coord(value: float) -> float:
    return round(value, 2) + 0.0  # + 0.0 turns -0.0 into 0.0


def error_identity(rule: str, x: float, y: float) -> dict[str, Any]:
    """A located identity, ``{rule, at: [x_mm, y_mm]}``, rounded to 0.01 mm."""
    return {"rule": rule, "at": [_coord(x), _coord(y)]}


def identity_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    """The one canonical key of an identity of either shape.

    Sorting, duplicate detection and issue ownership all go through it, so a
    ``{rule, ref, pad}`` and a ``{rule, at}`` identity can never be ordered or
    compared by different rules.  Within a shape it sorts as ``(rule, ref, pad)``
    or ``(rule, x, y)`` respectively.
    """
    at = item.get("at")
    x, y = (at[0], at[1]) if at is not None else (0.0, 0.0)
    return (item["rule"], item.get("ref", ""), item.get("pad", ""), at is not None, x, y)


def _describe_identity(item: Mapping[str, Any]) -> str:
    if "at" in item:
        return f"{item['rule']} at {item['at'][0]}/{item['at'][1]}"
    return f"{item['rule']} {item['pad']}"


def _check_identities(value: Any, field: str, shapes: Iterable[str]) -> list[dict[str, Any]]:
    """Validate a ``warnings`` / ``errors`` list: sorted, unique, canonical identities.

    ``shapes`` names the shapes this field accepts (``"pad"`` for
    ``{rule, ref, pad}``, ``"at"`` for ``{rule, at: [x_mm, y_mm]}``).
    """
    allowed = {shape: _SHAPE_KEYS[shape] for shape in shapes}
    expected = " or ".join(
        "{" + ", ".join(k if k != "at" else "at: [x_mm, y_mm]" for k in keys) + "}"
        for keys in allowed.values()
    )
    if not isinstance(value, list) or not value:
        raise KnownIssueError(f"{field} must be a non-empty list of {expected} identities")
    out: list[dict[str, Any]] = []
    for item in value:
        keys = set(item) if isinstance(item, Mapping) else set()
        shape = next((name for name, want in allowed.items() if keys == set(want)), None)
        if shape is None:
            raise KnownIssueError(f"a {field} identity must be exactly {expected}")
        rule = item["rule"]
        if not isinstance(rule, str) or not _RULE_RE.match(rule):
            raise KnownIssueError(f"invalid {field} rule id: {rule!r}")
        if shape == "at":
            at = item["at"]
            if (
                not isinstance(at, list)
                or len(at) != 2
                or not all(type(c) in (int, float) and math.isfinite(c) for c in at)
            ):
                raise KnownIssueError(f"{field} identity at must be [x_mm, y_mm]")
            canonical = error_identity(rule, at[0], at[1])
            if canonical["at"] != at:
                raise KnownIssueError(f"{field} identity at must be rounded to 0.01 mm")
            out.append(canonical)
            continue
        for key in _SHAPE_KEYS[shape]:
            text = item[key]
            if not isinstance(text, str) or not text or text != text.strip():
                raise KnownIssueError(f"{field} identity {key} must be non-empty, trimmed text")
        out.append({key: item[key] for key in _SHAPE_KEYS[shape]})
    keys_ = [identity_key(item) for item in out]
    if len(set(keys_)) != len(keys_):
        raise KnownIssueError(f"{field} lists an identity more than once")
    if keys_ != sorted(keys_):
        raise KnownIssueError(f"{field} must be sorted by {_SORT_ORDER[next(iter(allowed))]}")
    return out


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
        out["warnings"] = _check_identities(entry["warnings"], "warnings", _WARNING_SHAPES)
    if "errors" in entry:
        if kind != "exception":
            raise KnownIssueError(
                f"known issue #{issue}: only an exception may carry an errors allowance"
            )
        out["errors"] = _check_identities(entry["errors"], "errors", _ERROR_SHAPES)
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
    owners: dict[tuple[Any, ...], int] = {}
    for entry in entries:
        for field in ("warnings", "errors"):
            for item in entry.get(field) or ():
                key = identity_key(item)
                if key in owners:
                    raise KnownIssueError(
                        f"{field[:-1]} {_describe_identity(item)} is listed by both "
                        f"#{owners[key]} and #{entry['issue']}"
                    )
                owners[key] = entry["issue"]
    return entries


def parse_known_issue(spec: str, recorded: str) -> dict[str, Any]:
    """Parse ``N:summary:kind[:allowance,...]`` (the ``--known-issue`` form).

    The number is split from the left and the kind (plus the optional
    allowance) from the right, so the summary may itself contain ``:``.  In the
    allowance, ``rule@pad`` names a warning and ``!rule@x/y`` one error
    (exceptions only; x/y in mm).
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
    warnings: list[dict[str, str]] | None = None
    errors: list[dict[str, Any]] | None = None
    if _ALLOWANCE_RE.match(kind.strip()) and ":" in summary:
        allowance = kind.strip()
        summary, _, kind = summary.rpartition(":")
        for item in allowance.split(","):
            error = _ERROR_ITEM_RE.match(item)
            if error:
                rule, x, y = error.groups()
                errors = [] if errors is None else errors
                errors.append(error_identity(rule, float(x), float(y)))
                continue
            warnings = [] if warnings is None else warnings
            rule, _, pad = item.partition("@")
            ref, sep, number_part = pad.rpartition("-")
            if not sep or not ref or not number_part:
                raise KnownIssueError(
                    f"--known-issue {spec!r}: pad {pad!r} must look like <ref>-<number>"
                )
            warnings.append({"rule": rule, "ref": ref, "pad": pad})
        if warnings is not None:
            warnings.sort(key=identity_key)
        if errors is not None:
            errors.sort(key=identity_key)
    entry: dict[str, Any] = {
        "issue": int(number),
        "kind": kind.strip(),
        "summary": summary.strip(),
        "recorded": recorded,
    }
    if warnings is not None:
        entry["warnings"] = warnings
    if errors is not None:
        entry["errors"] = errors
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


def known_warning_allowance(
    entries: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str], int]:
    """``{(rule, ref, pad): issue}`` the board's recorded issues account for.

    A board script gating on ``kct check`` warnings should tolerate exactly
    this allowance (via :func:`match_warning_allowance`), so the tolerated
    warnings and their disclosure come from the same record.
    """
    allowance: dict[tuple[str, str, str], int] = {}
    for entry in entries:
        for item in entry.get("warnings") or ():
            allowance[(item["rule"], item["ref"], item["pad"])] = int(entry["issue"])
    return dict(sorted(allowance.items()))


def match_warning_allowance(
    violations: Iterable[Any],
    allowance: Mapping[tuple[str, str, str], int],
) -> tuple[list[str], list[str]]:
    """Match active warnings against *allowance* as a set: ``(unaccepted, stale)``.

    * ``unaccepted`` -- active warnings no allowance names: an identity that is
      not listed (a swapped identity at an unchanged count included), a rule
      with no keyable layout, or a listed identity reported more than once.
    * ``stale`` -- listed identities that no longer fire; the record must drop
      them, otherwise they are silent headroom for a new finding.

    ``violations`` are ``kct check`` JSON ``violations`` dicts or
    ``DRCViolation`` objects.  Errors and waived findings are not examined.
    """
    from kicad_tools.validate.known_warnings import (
        _describe,
        _is_active_warning,
        warning_key,
    )

    seen: set[tuple[str, str, str]] = set()
    unaccepted: list[str] = []
    for violation in violations:
        if not _is_active_warning(violation):
            continue
        key = warning_key(violation)
        if key is None or key not in allowance or key in seen:
            unaccepted.append(_describe(violation))
        else:
            seen.add(key)
    stale = [
        f"{rule} {pad} (#{allowance[(rule, ref, pad)]})"
        for rule, ref, pad in allowance
        if (rule, ref, pad) not in seen
    ]
    return unaccepted, stale


def known_error_allowance(
    entries: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, float, float], int]:
    """``{(rule, x_mm, y_mm): issue}`` the recorded exceptions account for.

    The readiness ``kct_check`` gate tolerates errors only when the report's
    error identities equal this set exactly.
    """
    allowance: dict[tuple[str, float, float], int] = {}
    for entry in entries:
        for item in entry.get("errors") or ():
            allowance[(item["rule"], item["at"][0], item["at"][1])] = int(entry["issue"])
    return dict(sorted(allowance.items()))
