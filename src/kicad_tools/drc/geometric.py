"""Shared native (kicad-cli) geometric DRC reconciliation.

kct has two internal DRC entry points -- ``kct audit``
(:class:`kicad_tools.audit.auditor.DesignAuditor`) and the ``kct route``
post-route gate (:func:`kicad_tools.cli.route_cmd.run_post_route_drc`).
Both must reconcile their internal verdict against KiCad's own
``kicad-cli pcb drc`` so that a clean *internal* verdict can never
overstate cleanliness when KiCad finds geometric defects the internal
engine is structurally blind to (shorts, ``copper_edge_clearance``,
``solder_mask_bridge``, ``silk_*`` overlaps, ...).

Previously only ``kct audit`` did this (issue #3721); the route gate did
not, so ``kct route`` could print ``DRC PASSED`` while ``kicad-cli pcb
drc`` reported 400+ violations including real shorts (issue #3803).

This module factors that reconciliation into a single shared helper,
:func:`run_geometric_drc`, so the two entry points cannot drift again.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.drc.violation import DRCViolation

logger = logging.getLogger(__name__)

__all__ = [
    "GeometricDRCResult",
    "SavedFillCheck",
    "check_saved_fill",
    "run_geometric_drc",
    "saved_fill_regressions",
]


# Machine-readable skip reasons (Issue #4178).  ``--strict-drc`` uses these
# to emit an actionable message distinguishing WHY the native DRC did not run.
REASON_OK = "ok"  # kicad-cli ran and produced a report
REASON_ABSENT = "kicad_cli_absent"  # kicad-cli not found on PATH
REASON_TIMEOUT = "kicad_cli_timeout"  # kicad-cli exceeded the timeout budget
REASON_NO_REPORT = "kicad_cli_no_report"  # kicad-cli ran but wrote no report
REASON_CRASH = "kicad_cli_crash"  # kicad-cli raised/crashed


@dataclass
class GeometricDRCResult:
    """Outcome of a native ``kicad-cli pcb drc`` reconciliation run.

    Attributes:
        ran: ``True`` when kicad-cli actually executed and produced a
            report.  ``False`` for every skip path (kicad-cli absent,
            timeout, no report, crash) -- callers MUST treat a
            ``ran=False`` result as "geometric DRC not performed" and
            never as a clean PASS.
        error_count: Number of error-severity geometric violations found
            by kicad-cli.  ``0`` when ``ran`` is ``False``.
        by_type: ``{kicad-cli type_str: count}`` for the error-severity
            violations (unnamespaced; callers that need a namespace add
            their own prefix).
        all_by_type: ``{kicad-cli type_str: count}`` across all severities.
            This includes warning-only checks such as ``copper_sliver`` while
            preserving ``by_type``'s error-only contract for existing gates.
        note: A human-readable note describing the outcome -- always set
            for skip paths (e.g. the "kicad-cli not found" fallback) and
            ``None`` on a clean successful run.
        reason: A machine-readable outcome code (one of ``REASON_*``)
            distinguishing "kicad-cli not found" from "kicad-cli timed
            out" from "kicad-cli crashed" so ``kct route --strict-drc``
            can emit an actionable failure message (Issue #4178).
        unconnected_items: kicad-cli's ``unconnected_items`` array (the
            ratsnest links KiCad's own connectivity, after the zone refill,
            still reports missing), parsed into
            :class:`~kicad_tools.drc.violation.DRCViolation` records.  Kept
            out of ``error_count`` / ``by_type`` on purpose (issue #4498:
            those stay a geometric-only verdict).  Issue #5785 reads it as
            the completion oracle for pour nets.  Empty when ``ran`` is
            ``False``.
        error_violations: The error-severity violations behind
            ``error_count``, so a caller can locate a regression (Issue
            #5785 attributes one to the copper that caused it).
    """

    ran: bool = False
    error_count: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    all_by_type: dict[str, int] = field(default_factory=dict)
    note: str | None = None
    reason: str = REASON_OK
    unconnected_items: list[DRCViolation] = field(default_factory=list)
    error_violations: list[DRCViolation] = field(default_factory=list)

    @property
    def unconnected_count(self) -> int:
        """Number of kicad-cli ``unconnected_items`` (0 when ``ran`` is False)."""
        return len(self.unconnected_items)

    @property
    def has_errors(self) -> bool:
        """``True`` when kicad-cli ran and found >=1 error-severity violation."""
        return self.ran and self.error_count > 0

    def top_types(self, n: int = 3) -> list[tuple[str, int]]:
        """Return the ``n`` most frequent violation types (desc by count)."""
        return sorted(self.by_type.items(), key=lambda x: -x[1])[:n]


# Reuse the exact fallback wording from auditor.py so audit + route stay
# consistent for KiCad-less environments.
KICAD_CLI_ABSENT_NOTE = "kicad-cli not found; geometric DRC skipped"


def run_geometric_drc(
    pcb_path: Path | str,
    *,
    timeout: int = 120,
    kicad_cli: Path | None = None,
    refill_zones: bool = True,
) -> GeometricDRCResult:
    """Run ``kicad-cli pcb drc`` and summarize findings by severity.

    kicad-cli loads the sibling ``<board>.kicad_pro`` emitted by ``kct
    export`` (issue #3720), so error-severity findings are checked against
    the manufacturer's fab-accurate rules with ``lib_footprint_mismatch``
    / ``isolated_copper`` already downgraded below error severity.

    Issue #3969: the invocation passes ``--refill-zones`` so kicad-cli
    re-fills the copper pours from scratch *before* evaluating clearance,
    exactly as a fab-house DRC (and ``kicad-cli pcb drc --refill-zones``,
    the acceptance command boards are gated against) does.  Without it,
    kicad-cli judges the *stale* zone-fill polygons persisted in the
    ``.kicad_pcb``; those can differ from a fresh fill by sub-micron
    rounding and manufacture a phantom ``clearance`` violation (board 03
    reported ``zone clearance 0.3000 mm; actual 0.2996 mm`` -- a 0.4 µm
    gap that vanishes on refill).  This phantom was latent in the
    committed board too and only surfaced once PR #3950 wired this
    cross-check into the post-route gate; refilling makes both engines and
    the shipped manufacturing bundle judge the same geometry.

    This never raises: every failure mode (kicad-cli absent, timeout, no
    report, parse error) returns a :class:`GeometricDRCResult` with
    ``ran=False`` and an explanatory ``note`` so callers can degrade
    gracefully without ever silently overstating cleanliness.

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file to check.
        timeout: Seconds before the kicad-cli invocation is abandoned.
        kicad_cli: Explicit kicad-cli path (auto-detected when ``None``).
        refill_zones: Recompute zones before checking by default. False checks
            the saved copper exactly, for deliberately preserved fixed fills.
            This does not attest to what a later full refill would produce.

    Returns:
        A :class:`GeometricDRCResult` summarizing the run.
    """
    from kicad_tools.cli.runner import find_kicad_cli

    pcb_path = Path(pcb_path)

    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        from kicad_tools.cli.runner import last_kicad_cli_lookup

        lookup = last_kicad_cli_lookup()
        if lookup is not None and lookup.probe_failed:
            # Installed but unresponsive (#5932): say so instead of "not found".
            note = f"{lookup.reason}; geometric DRC skipped"
            return GeometricDRCResult(ran=False, note=note, reason=REASON_ABSENT)
        return GeometricDRCResult(ran=False, note=KICAD_CLI_ABSENT_NOTE, reason=REASON_ABSENT)

    report_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            report_path = Path(f.name)

        cmd = [
            str(kicad_cli),
            "pcb",
            "drc",
            *(["--refill-zones"] if refill_zones else []),
            "--format",
            "json",
            # Request all severities so warning-only native checks such as
            # copper_sliver can be reconciled.  ``by_type`` below remains
            # error-only for backward compatibility with existing gates.
            "--severity-all",
            "--units",
            "mm",
            "--output",
            str(report_path),
            str(pcb_path),
        ]
        subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)

        if report_path is None or not report_path.exists():
            return GeometricDRCResult(
                ran=False,
                note="kicad-cli produced no DRC report (geometric DRC skipped)",
                reason=REASON_NO_REPORT,
            )

        from kicad_tools.drc import DRCReport

        report = DRCReport.load(report_path)

        cli_errors = [v for v in report.violations if v.is_error]

        by_type: dict[str, int] = {}
        for v in cli_errors:
            by_type[v.type_str] = by_type.get(v.type_str, 0) + 1

        all_by_type: dict[str, int] = {}
        for v in report.violations:
            all_by_type[v.type_str] = all_by_type.get(v.type_str, 0) + 1

        return GeometricDRCResult(
            ran=True,
            error_count=len(cli_errors),
            by_type=by_type,
            all_by_type=all_by_type,
            note=None,
            reason=REASON_OK,
            unconnected_items=list(report.unconnected_items),
            error_violations=cli_errors,
        )
    except subprocess.TimeoutExpired:
        return GeometricDRCResult(
            ran=False,
            note="kicad-cli DRC timed out (geometric DRC skipped)",
            reason=REASON_TIMEOUT,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Geometric DRC (kicad-cli) failed: %s", e)
        return GeometricDRCResult(ran=False, note=f"geometric DRC failed: {e}", reason=REASON_CRASH)
    finally:
        if report_path is not None:
            report_path.unlink(missing_ok=True)


# kicad-cli finding types that show a saved zone fill whose copper is split
# apart (Issue #6078).  A fresh fill from KiCad never produces more of these
# than the board's routing already implies.  So when the SAVED fill has more
# of them than a ``--refill-zones`` run, the extra findings come from the
# saved copper itself.
SAVED_FILL_CONNECTIVITY_TYPES: tuple[str, ...] = ("isolated_copper", "copper_sliver")


def saved_fill_regressions(saved: GeometricDRCResult, refilled: GeometricDRCResult) -> list[str]:
    """Explain where the SAVED fill is worse-connected than a fresh fill.

    ``saved`` is ``kicad-cli pcb drc`` *without* ``--refill-zones``: it reads
    the copper stored in the board, which ``kicad-cli pcb export gerbers``
    also plots without refilling.  ``refilled`` is the same board with
    ``--refill-zones``, the view every refill-based gate checks.  Issue
    #6078: a post-fill edit to the saved pours (the #3711 clearance carve)
    split board 03's F.Cu GND pour into 16 pieces.  The refill-based gates
    reported 1 GND link, but the copper sent to the fab had 17.

    Returns one human-readable line per regressed measure (unconnected items,
    ``isolated_copper``, ``copper_sliver``).  Returns ``[]`` when either run
    did not happen, because a check that was not performed has no verdict.
    """
    if not (saved.ran and refilled.ran):
        return []
    out: list[str] = []
    if saved.unconnected_count > refilled.unconnected_count:
        out.append(
            f"unconnected_items: {saved.unconnected_count} on the saved fill vs "
            f"{refilled.unconnected_count} after a refill"
        )
    for type_str in SAVED_FILL_CONNECTIVITY_TYPES:
        n_saved = saved.all_by_type.get(type_str, 0)
        n_refilled = refilled.all_by_type.get(type_str, 0)
        if n_saved > n_refilled:
            out.append(f"{type_str}: {n_saved} on the saved fill vs {n_refilled} after a refill")
    return out


@dataclass
class SavedFillCheck:
    """Verdict of :func:`check_saved_fill` (Issue #6078).

    Attributes:
        ran: ``True`` when every DRC run the verdict needs actually ran.
        regressions: :func:`saved_fill_regressions` lines; empty when clean.
        note: Why the check did not run (``ran`` is ``False``).
    """

    ran: bool
    regressions: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def ok(self) -> bool:
        """``True`` only when the check ran and found no regression."""
        return self.ran and not self.regressions


def check_saved_fill(
    pcb_path: Path | str,
    *,
    refilled: GeometricDRCResult | None = None,
    timeout: int = 120,
    kicad_cli: Path | None = None,
) -> SavedFillCheck:
    """Check that the zone copper SAVED in ``pcb_path`` is as connected as a refill.

    Gerber export plots the saved fill (it does not pass ``--check-zones``),
    while ``kicad-cli pcb drc --refill-zones`` checks a fresh one.  This
    check makes the difference visible (Issue #6078).  It runs kicad-cli DRC
    without refill and compares the result with the refilled run.  Pass
    ``refilled`` if you already have that run, so it is not repeated.

    Fast path: if the saved run shows no unconnected items and none of the
    :data:`SAVED_FILL_CONNECTIVITY_TYPES`, nothing can have regressed, and
    the refill run is skipped.  The board file is never modified: neither
    run passes ``--save-board``.
    """
    saved = run_geometric_drc(pcb_path, timeout=timeout, kicad_cli=kicad_cli, refill_zones=False)
    if not saved.ran:
        return SavedFillCheck(ran=False, note=saved.note)
    if refilled is None:
        if saved.unconnected_count == 0 and not any(
            saved.all_by_type.get(t, 0) for t in SAVED_FILL_CONNECTIVITY_TYPES
        ):
            return SavedFillCheck(ran=True)
        refilled = run_geometric_drc(pcb_path, timeout=timeout, kicad_cli=kicad_cli)
    if not refilled.ran:
        return SavedFillCheck(ran=False, note=refilled.note)
    return SavedFillCheck(ran=True, regressions=saved_fill_regressions(saved, refilled))
