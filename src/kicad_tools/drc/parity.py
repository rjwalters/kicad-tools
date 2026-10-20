"""Schematic-parity status for kicad-cli DRC reports (Issue #7077).

``kct drc`` asks kicad-cli for ``--schematic-parity``.  The result has three
honest answers, and they must never collapse into one:

* **PASSED**  -- parity ran and found nothing (unwaived);
* **FAILED**  -- parity ran and found board/schematic mismatches;
* **NOT RUN** -- kicad-cli could not run parity (no schematic resolved, several
  candidates, or one kicad-cli cannot netlist), or a pre-existing report does not record whether it
  ran.  kicad-cli writes ``"schematic_parity": []`` in that case too, so an
  empty array alone never means "clean".

The vocabulary matches ``kct check``'s ``SubCheckStatus``.  This module is the
single place the status, its one-line text rendering and its JSON shape are
built, shared by ``kct drc`` (table, summary, JSON, ``--mfr``) and
``kct drc-summary``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .violation import DRCViolation

PARITY_PASSED = "PASSED"
PARITY_FAILED = "FAILED"
PARITY_NOT_RUN = "NOT RUN"

#: Generic reason shown when kicad-cli said it could not fetch the schematic
#: netlist and the caller did not say why (Issue #7077).  ``run_drc`` now
#: reports one of the cause-specific reasons below instead (Issue #7147).
PARITY_REASON_KICAD_CLI_SKIPPED = "kicad-cli could not fetch the schematic netlist"


def parity_reason_no_schematic(pcb_name: str) -> str:
    """NOT RUN reason: no schematic could be resolved for the board (Issue #7147)."""
    stem = pcb_name.rsplit(".", 1)[0]
    return (
        f"no schematic found for {pcb_name} (kicad-cli only reads {stem}.kicad_sch; "
        "looked for project.kct artifacts.schematic, <stripped-stem>.kicad_sch, "
        "a .kicad_pro-paired or sole .kicad_sch); pass --schematic PATH"
    )


def parity_reason_ambiguous(pcb_name: str) -> str:
    """NOT RUN reason: several candidate root schematics (Issue #7147)."""
    return f"several candidate schematics next to {pcb_name}; pass --schematic PATH"


def parity_reason_netlist_failed(schematic: str) -> str:
    """NOT RUN reason: a schematic was found but kicad-cli could not netlist it."""
    return f"kicad-cli could not build a netlist from {schematic} (is it fully annotated?)"


#: Reason shown for report input (``kct drc report.json``): kicad-cli writes
#: ``"schematic_parity": []`` both when parity was clean and when it never
#: ran, so the report alone cannot tell the two apart.
PARITY_REASON_REPORT_UNKNOWN = "report does not record whether parity ran"


@dataclass(frozen=True)
class SchematicParityStatus:
    """The PASSED / FAILED / NOT RUN answer for one DRC run or report."""

    status: str
    reason: str | None = None
    #: Unwaived parity findings, with their kicad-cli severities.
    issues: tuple[DRCViolation, ...] = field(default_factory=tuple)
    #: Parity findings matched by a ``.kct_waivers.json`` entry.
    waived_count: int = 0
    #: The schematic parity was run against (or attempted with), when known
    #: (Issue #7147).
    schematic: str | None = None

    @property
    def checked(self) -> bool:
        """Whether parity actually ran (PASSED or FAILED)."""
        return self.status != PARITY_NOT_RUN

    @property
    def error_count(self) -> int:
        """Unwaived error-severity parity findings (gate: exit 1)."""
        return sum(1 for v in self.issues if v.is_error)

    @property
    def warning_count(self) -> int:
        """Unwaived non-error parity findings (gate: exit 2 under --strict)."""
        return len(self.issues) - self.error_count

    def status_line(self) -> str:
        """The one ``Schematic parity: ...`` line every text output prints."""
        if self.status == PARITY_FAILED:
            text = f"Schematic parity: FAILED ({len(self.issues)} issue(s))"
        elif self.status == PARITY_NOT_RUN:
            text = f"Schematic parity: NOT RUN ({self.reason or 'unknown reason'})"
        else:
            text = "Schematic parity: PASSED"
        if self.waived_count:
            text += f" [{self.waived_count} waived]"
        return text

    def issue_lines(self) -> list[str]:
        """A dedicated findings section for FAILED (empty otherwise)."""
        if not self.issues:
            return []
        lines = [f"SCHEMATIC PARITY ({len(self.issues)} issue(s)):"]
        for v in self.issues:
            lines.append(f"  [{v.severity.value.upper()}] {v.type_str}: {v.message}")
            for item in v.items:
                lines.append(f"      - {item}")
        return lines

    def to_dict(self, *, include_issues: bool = True) -> dict[str, Any]:
        """JSON shape: ``{status, checked, reason, error_count, warning_count, schematic, issues}``."""
        data: dict[str, Any] = {
            "status": self.status,
            "checked": self.checked,
            "reason": self.reason,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "schematic": self.schematic,
        }
        if self.waived_count:
            data["waived_count"] = self.waived_count
        if include_issues:
            data["issues"] = [v.to_dict() for v in self.issues]
        return data


def schematic_parity_status(
    findings: list[DRCViolation],
    ran: bool | None,
    reason: str | None = None,
    schematic: str | None = None,
) -> SchematicParityStatus:
    """Derive the parity status from parsed findings and the run signal.

    Args:
        findings: The report's parsed ``schematic_parity`` entries.
        ran: ``True`` when this run invoked kicad-cli and it did run parity,
            ``False`` when kicad-cli said it could not, ``None`` when unknown
            (a pre-existing report).
        reason: Override for the NOT RUN reason text.
        schematic: The schematic parity used or attempted (Issue #7147).

    Any finding proves parity ran, whatever ``ran`` says.  An empty finding
    list is PASSED only when the run signal affirmatively says parity ran.
    """
    unwaived = tuple(v for v in findings if not v.is_waived)
    waived = len(findings) - len(unwaived)
    if unwaived:
        return SchematicParityStatus(PARITY_FAILED, None, unwaived, waived, schematic)
    if findings or ran is True:
        return SchematicParityStatus(PARITY_PASSED, None, (), waived, schematic)
    if ran is False:
        return SchematicParityStatus(
            PARITY_NOT_RUN, reason or PARITY_REASON_KICAD_CLI_SKIPPED, schematic=schematic
        )
    return SchematicParityStatus(
        PARITY_NOT_RUN, reason or PARITY_REASON_REPORT_UNKNOWN, schematic=schematic
    )
