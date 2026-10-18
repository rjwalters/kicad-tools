"""Helpers shared by the sign-off commands' exit gates (Issue #7230).

View filters (``--net``, ``--type``, ``--category``, ``--errors-only``) change
what is *shown*, never the verdict: a command snapshots its findings before
applying them, gates on the snapshot, and discloses what the filters hid.

:func:`gate_exit_code` is the one exit ladder every sign-off command calls
(Part B).  It reproduces each command family's *existing* numbering through a
named :data:`EXIT_LADDERS` entry rather than unifying it; the operator kept
both ladders (Part C, option 1).  Pipelines that need one answer across
commands read the ``verdict`` the same call returns, which the commands emit
as a top-level JSON key next to ``exit_code``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal

Verdict = Literal["pass", "fail", "incomplete"]
LadderStyle = Literal["drc", "check", "sch"]


@dataclass(frozen=True)
class ExitLadder:
    """The exit code each failing state maps to for one command family.

    A clean run (or a warnings-only run without ``strict``) always exits 0.
    """

    errors: int
    strict_warnings: int
    incomplete: int


#: Per-family numbering, unchanged from before Issue #7230 Part B:
#:
#: * ``"drc"`` -- ``kct drc`` (plain and ``--mfr``), ``kct erc``,
#:   ``kct validate --sync``: errors 1; warnings under ``--strict`` 2; a check
#:   that did not run (schematic parity NOT RUN) 2.  ``erc`` and
#:   ``validate --sync`` have no incomplete state.
#: * ``"check"`` -- ``kct check`` (meta rollup and ``--drc-only``) and
#:   ``kct audit``: every failing state is 2; 1 is reserved for "the tool could
#:   not run" (Issue #3750), which those commands return before gating.
#: * ``"sch"`` -- ``kct sch validate``: every failing state is 1.
#:
#: ``docs/reference/cli.md`` "Sign-off exit codes" documents this table, and
#: ``tests/test_gate_exit_code_7230.py`` keeps the two in step.
EXIT_LADDERS: dict[str, ExitLadder] = {
    "drc": ExitLadder(errors=1, strict_warnings=2, incomplete=2),
    "check": ExitLadder(errors=2, strict_warnings=2, incomplete=2),
    "sch": ExitLadder(errors=1, strict_warnings=1, incomplete=1),
}


def gate_exit_code(
    errors: int,
    warnings: int,
    *,
    incomplete: bool = False,
    strict: bool = False,
    allow_incomplete: bool = False,
    style: LadderStyle = "drc",
) -> tuple[int, Verdict]:
    """The sign-off exit gate: ``(exit_code, verdict)`` for one run.

    Precedence is fixed: any error is ``fail``; otherwise warnings under
    ``strict`` are ``fail``; otherwise a check that did not run is
    ``incomplete`` (exit 0 under ``allow_incomplete``, the verdict stays
    honest); otherwise ``pass`` (exit 0), warnings or not.

    Args:
        errors: Blocking findings the verdict counts (never view-filtered).
        warnings: Non-blocking findings the verdict counts.
        incomplete: A required check did not run.
        strict: Warnings are fatal.
        allow_incomplete: An ``incomplete`` verdict exits 0.
        style: Which :data:`EXIT_LADDERS` numbering to use.
    """
    ladder = EXIT_LADDERS[style]
    if errors > 0:
        return ladder.errors, "fail"
    if warnings > 0 and strict:
        return ladder.strict_warnings, "fail"
    if incomplete:
        return (0 if allow_incomplete else ladder.incomplete), "incomplete"
    return 0, "pass"


def hidden_by_view_filter(
    gate_violations: Iterable[Any], shown: Iterable[Any]
) -> dict[str, int] | None:
    """Errors/warnings in ``gate_violations`` that a view filter left out of ``shown``.

    Generic over DRC/ERC violations and sync issues: only ``.is_error`` and
    object identity are used.  Returns ``None`` when nothing was hidden, so a
    filter that matches everything (or no filter at all) produces exactly
    today's output.
    """
    shown_ids = {id(v) for v in shown}
    hidden = [v for v in gate_violations if id(v) not in shown_ids]
    if not hidden:
        return None
    errors = sum(1 for v in hidden if v.is_error)
    return {"errors": errors, "warnings": len(hidden) - errors}
