"""Shared schematic/PCB drift reporting for the route/check entry points.

This module wires the existing :class:`kicad_tools.sync.reconciler.Reconciler`
into the ``kct route`` and ``kct check`` commands so that a PCB whose footprint
set has drifted from the schematic netlist is surfaced -- as an advisory banner
on ``route``/plain ``check`` and as a blocking gate behind
``kct check --netlist-sync``.

Set-comparison logic is NOT re-implemented here; it lives in the ``Reconciler``.
This module only renders the analysis and decides the exit-code policy, which
mirrors the auditor's "schematic-only drift == unbuildable BOM == NOT_READY"
rule (see ``Auditor`` verdict logic).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.sync.discover import resolve_schematic_for_pcb

if TYPE_CHECKING:
    from kicad_tools.sync.reconciler import SyncAnalysis

logger = logging.getLogger(__name__)


def analyze_drift(pcb_path: str | Path, schematic_path: str | Path | None = None):
    """Run the Reconciler for a PCB, discovering the schematic if needed.

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file.
        schematic_path: Optional explicit schematic override.  When ``None``
            the schematic is auto-discovered via
            :func:`resolve_schematic_for_pcb`.

    Returns:
        Tuple ``(analysis, resolved_schematic)``.  Both are ``None`` when no
        schematic could be resolved or the analysis failed -- callers should
        treat that as "skip silently".
    """
    from kicad_tools.sync.reconciler import Reconciler

    resolved = Path(schematic_path) if schematic_path else resolve_schematic_for_pcb(pcb_path)
    if resolved is None or not resolved.exists():
        return None, None

    try:
        reconciler = Reconciler(schematic=resolved, pcb=pcb_path)
        analysis = reconciler.analyze()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Drift analysis failed for %s: %s", pcb_path, e)
        return None, None

    return analysis, resolved


def _drift_summary_parts(analysis: SyncAnalysis) -> list[str]:
    """Build the human-readable count-delta fragments shared by all renderers."""
    parts: list[str] = []
    if analysis.schematic_orphans:
        parts.append(f"{len(analysis.schematic_orphans)} schematic-only")
    if analysis.pcb_orphans:
        parts.append(f"{len(analysis.pcb_orphans)} PCB-only")
    if analysis.value_mismatches:
        parts.append(f"{len(analysis.value_mismatches)} value mismatch(es)")
    if analysis.footprint_mismatches:
        parts.append(f"{len(analysis.footprint_mismatches)} footprint mismatch(es)")
    return parts


def has_drift(analysis: SyncAnalysis) -> bool:
    """True when any of the four drift axes is non-empty."""
    return bool(
        analysis.schematic_orphans
        or analysis.pcb_orphans
        or analysis.value_mismatches
        or analysis.footprint_mismatches
    )


def format_drift_banner(analysis: SyncAnalysis, pcb_path: str | Path) -> str | None:
    """Render the one-line advisory drift banner, or ``None`` when in sync.

    The banner names the count delta and points to the analysis/remediation
    commands, matching the message style used elsewhere in ``route``/``check``.
    """
    parts = _drift_summary_parts(analysis)
    if not parts:
        return None

    pcb_name = Path(pcb_path).name
    return (
        f"  WARNING: PCB out of sync with schematic -- {', '.join(parts)}. "
        f"Run 'kct sync --analyze {pcb_name}' to inspect "
        f"(apply with 'kct pcb sync-netlist')."
    )


def render_drift_report(analysis: SyncAnalysis, pcb_path: str | Path, schematic_path: Path) -> str:
    """Render the full add/drop/orphan report for ``--netlist-sync``."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("NETLIST SYNC CHECK")
    lines.append("=" * 60)
    lines.append(f"PCB:       {Path(pcb_path).name}")
    lines.append(f"Schematic: {Path(schematic_path).name}")

    if not has_drift(analysis):
        lines.append("")
        lines.append("IN SYNC - schematic and PCB component sets match.")
        return "\n".join(lines)

    parts = _drift_summary_parts(analysis)
    lines.append("")
    lines.append(f"OUT OF SYNC - {', '.join(parts)}.")

    if analysis.schematic_orphans:
        lines.append("")
        lines.append(
            f"Schematic-only (in schematic, missing from PCB) [{len(analysis.schematic_orphans)}]:"
        )
        for ref in analysis.schematic_orphans:
            lines.append(f"  - {ref}")

    if analysis.pcb_orphans:
        lines.append("")
        lines.append(f"PCB-only (on PCB, missing from schematic) [{len(analysis.pcb_orphans)}]:")
        for ref in analysis.pcb_orphans:
            lines.append(f"  - {ref}")

    if analysis.value_mismatches:
        lines.append("")
        lines.append(f"Value mismatches [{len(analysis.value_mismatches)}]:")
        for m in analysis.value_mismatches:
            lines.append(
                f"  - {m['reference']}: schematic={m['schematic_value']!r} pcb={m['pcb_value']!r}"
            )

    if analysis.footprint_mismatches:
        lines.append("")
        lines.append(f"Footprint mismatches [{len(analysis.footprint_mismatches)}]:")
        for m in analysis.footprint_mismatches:
            lines.append(
                f"  - {m['reference']}: schematic={m['schematic_footprint']!r} "
                f"pcb={m['pcb_footprint']!r}"
            )

    lines.append("")
    lines.append("Remediation:")
    lines.append(f"  kct sync --analyze {Path(pcb_path).name}      # inspect proposed changes")
    lines.append(f"  kct pcb sync-netlist {Path(pcb_path).name}    # apply changes to the PCB")
    return "\n".join(lines)
