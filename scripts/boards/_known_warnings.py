"""Per-board known-warning allowlists, shared by the readiness refresher and CI.

Issue #7273.  ``scripts/boards/refresh_readiness.py`` and the routed-PCB DRC
gate (``scripts/ci/check_routed_drc.py``) must accept exactly the same
warnings for a board, so the list lives here once.

An entry is ``{rule_id: max count}``.  A warning of any other rule, or more
warnings of a listed rule than its count, is not accepted.  This never
tolerates errors: callers still fail on every error.  Delete an entry when
the tracked fix lands.
"""

from __future__ import annotations

KNOWN_WARNINGS: dict[str, dict[str, int]] = {
    # Left for #7220 PR2 (narrower-spoke fill repair).
    "04-stm32-devboard": {"thermal_asymmetry": 9},
    # Left for #7220 PR3 (router mask-dam fix).
    "05-bldc-motor-controller": {"via_at_pad_edge": 3},
}


def warning_counts_by_rule(data: dict) -> dict[str, int]:
    """Active (non-waived) warning findings of a ``kct check`` report, by rule."""
    counts: dict[str, int] = {}
    for violation in data.get("violations", []):
        if (
            isinstance(violation, dict)
            and violation.get("severity") == "warning"
            and not violation.get("waived")
        ):
            rule = str(violation.get("rule_id") or "unknown")
            counts[rule] = counts.get(rule, 0) + 1
    return dict(sorted(counts.items()))


def unaccepted_warnings(
    data: dict, known_warnings: dict[str, int] | None = None
) -> dict[str, tuple[int, int]]:
    """``{rule: (found, allowed)}`` for warnings the allowlist does not cover."""
    known = known_warnings or {}
    counts = warning_counts_by_rule(data)
    summary_total = data.get("summary", {}).get("warnings")
    if isinstance(summary_total, int) and summary_total != sum(counts.values()):
        # The summary and the finding list disagree: never trust the allowlist.
        counts["<summary/violations mismatch>"] = abs(summary_total - sum(counts.values()))
    return {
        rule: (count, known.get(rule, 0))
        for rule, count in counts.items()
        if count > known.get(rule, 0)
    }
