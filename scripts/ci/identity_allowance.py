"""Identity-keyed allowances for the synthetic-board CI gates (Issue #7359).

The #7270 Q3 operator ruling: a known finding is excepted by *identity* (the
net it sits on) plus a tracking issue number, never by a per-rule count.  A
count lets a different new finding hide behind a known one, and leaves spare
headroom when a listed finding is fixed.  The model was #7108's board 03
via-count exception (retired in #7713).

An allowance is ``{(rule_id, identity): tracking_issue}``.  ``identity`` is the
sorted, ``/``-joined ``items`` (falling back to ``nets``) of one error
violation record, so two segments of the same net share one slot.  Removing an
entry is a plain deletion (#7349 does that once the router defect is fixed).

Issue #8957 / #8938: for :data:`NET_IDENTITY_RULES` the identity is the
``nets`` signature (falling back to ``items``) instead.  Those rules' ``items``
are geometry refs (track/via positions, footprint UUIDs), and the synthetic
gates regenerate their boards from scratch every CI run, so geometry-keyed
items differ run to run while the net signature is stable: measured on CI run
43652354983, two independent board-07 regenerations agreed on all nine
``physical_copper_gap`` net signatures while sharing zero geometry-keyed items.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

Allowance = dict[tuple[str, str], int]

#: Rules whose ``items`` are geometry refs (track/via positions, footprint
#: UUIDs) rather than net names, so their allowance identity is the ``nets``
#: signature (Issue #8957: those refs are re-minted by every synthetic-board
#: regeneration, but the net signature is stable run to run).
NET_IDENTITY_RULES = frozenset({"physical_copper_gap"})


def violation_identity(violation: dict[str, Any]) -> str:
    """Identity of one ``kct check --format json`` violation: its net(s).

    :data:`NET_IDENTITY_RULES` prefer ``nets`` over ``items``: on the
    regenerated synthetic boards their geometry-keyed items are ephemeral
    while the net signature is stable, so the allowance stays keyed to
    something the next run reproduces.
    """
    if violation.get("rule_id") in NET_IDENTITY_RULES:
        names = violation.get("nets") or violation.get("items") or []
    else:
        names = violation.get("items") or violation.get("nets") or []
    return "/".join(sorted(str(n) for n in names))


def error_identities(violations: Iterable[Any]) -> dict[str, set[str]]:
    """``{rule_id: {identity, ...}}`` over the error-severity violation records."""
    found: dict[str, set[str]] = {}
    for v in violations:
        if not isinstance(v, dict) or v.get("severity") != "error":
            continue
        rule = v.get("rule_id")
        if isinstance(rule, str):
            found.setdefault(rule, set()).add(violation_identity(v))
    return found


def unlisted_findings(allowance: Allowance, rule: str, identities: Iterable[str]) -> list[str]:
    """Identities of ``rule`` findings that no allowance entry covers."""
    return sorted(i for i in identities if (rule, i) not in allowance)


def stale_entries(allowance: Allowance, found: dict[str, set[str]]) -> list[tuple[str, str, int]]:
    """Allowance entries whose finding no longer occurs: ``(rule, identity, issue)``."""
    return sorted(
        (rule, ident, issue)
        for (rule, ident), issue in allowance.items()
        if ident not in found.get(rule, set())
    )


def unallowed_findings(
    allowance: Allowance,
    violations: Iterable[Any],
    *,
    advisory: Callable[[str], bool] = lambda _rule: False,
) -> list[tuple[str, str]]:
    """Blocking error findings no allowance entry covers: ``(rule, identity)``.

    Issue #7367: this is the number the routed-DRC total is compared to, so a
    finding that is allowed by identity never uses up budget, and every other
    blocking finding counts, whatever its rule.  One entry per violation
    record; ``advisory`` rules (``connectivity``) are not blocking and are
    left out.
    """
    out: list[tuple[str, str]] = []
    for v in violations:
        if not isinstance(v, dict) or v.get("severity") != "error":
            continue
        rule = v.get("rule_id")
        if not isinstance(rule, str) or advisory(rule):
            continue
        ident = violation_identity(v)
        if (rule, ident) not in allowance:
            out.append((rule, ident))
    return sorted(out)


def format_stale(stale: list[tuple[str, str, int]]) -> str:
    """Human-readable failure text telling the author which entries to delete."""
    return "; ".join(
        f"{rule} on {ident!r} no longer occurs -- delete its allowance entry (tracked in #{issue})"
        for rule, ident, issue in stale
    )
