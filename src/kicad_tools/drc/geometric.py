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

logger = logging.getLogger(__name__)

__all__ = ["GeometricDRCResult", "run_geometric_drc"]


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
        note: A human-readable note describing the outcome -- always set
            for skip paths (e.g. the "kicad-cli not found" fallback) and
            ``None`` on a clean successful run.
    """

    ran: bool = False
    error_count: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    note: str | None = None

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
) -> GeometricDRCResult:
    """Run ``kicad-cli pcb drc`` and summarize its error-severity findings.

    kicad-cli loads the sibling ``<board>.kicad_pro`` emitted by ``kct
    export`` (issue #3720), so a ``--severity-error`` run checks against
    the manufacturer's fab-accurate rules with ``lib_footprint_mismatch``
    / ``isolated_copper`` already downgraded below error severity.

    This never raises: every failure mode (kicad-cli absent, timeout, no
    report, parse error) returns a :class:`GeometricDRCResult` with
    ``ran=False`` and an explanatory ``note`` so callers can degrade
    gracefully without ever silently overstating cleanliness.

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file to check.
        timeout: Seconds before the kicad-cli invocation is abandoned.
        kicad_cli: Explicit kicad-cli path (auto-detected when ``None``).

    Returns:
        A :class:`GeometricDRCResult` summarizing the run.
    """
    from kicad_tools.cli.runner import find_kicad_cli

    pcb_path = Path(pcb_path)

    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        return GeometricDRCResult(ran=False, note=KICAD_CLI_ABSENT_NOTE)

    report_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            report_path = Path(f.name)

        cmd = [
            str(kicad_cli),
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-error",
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
            )

        from kicad_tools.drc import DRCReport

        report = DRCReport.load(report_path)

        # --severity-error already filters to errors, but guard defensively
        # in case a future kicad-cli emits mixed severities.
        cli_errors = [v for v in report.violations if v.is_error]

        by_type: dict[str, int] = {}
        for v in cli_errors:
            by_type[v.type_str] = by_type.get(v.type_str, 0) + 1

        return GeometricDRCResult(
            ran=True,
            error_count=len(cli_errors),
            by_type=by_type,
            note=None,
        )
    except subprocess.TimeoutExpired:
        return GeometricDRCResult(
            ran=False,
            note="kicad-cli DRC timed out (geometric DRC skipped)",
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Geometric DRC (kicad-cli) failed: %s", e)
        return GeometricDRCResult(ran=False, note=f"geometric DRC failed: {e}")
    finally:
        if report_path is not None:
            report_path.unlink(missing_ok=True)
