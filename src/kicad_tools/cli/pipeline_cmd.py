"""
Pipeline command for end-to-end repair workflow on existing PCBs.

Orchestrates the full repair pipeline:
0. ERC check (schematic validation)
1. Fix ERC violations (auto-remediation when errors detected)
2. Sync schematic <-> PCB (reconcile component sets before routing)
3. Fix silkscreen (manufacturer line-width compliance)
4. Fix vias (manufacturer compliance)
5. [Optional] Route (if board is unrouted)
6. Stitch (add stitching vias for plane connections on multi-layer boards)
7. Optimize traces
8. Zone fill (requires kicad-cli)
9. Fix DRC violations
10. Zone refill (recompute zones after trace nudges)
11. Audit / check
12. Report generation (manufacturing report)
13. Export manufacturing package (gerbers, BOM, CPL, project ZIP)

The stitch step must run AFTER route (so traces exist) and BEFORE zones
(so zone fill respects via clearances).  On 2-layer boards or boards
without internal plane nets, the stitch step is skipped automatically.

Usage:
    kct pipeline board.kicad_pcb --mfr jlcpcb
    kct pipeline board.kicad_pcb --dry-run
    kct pipeline board.kicad_pcb --step fix-vias
    kct pipeline board.kicad_pcb --step fix-silkscreen
    kct pipeline board.kicad_pcb --step stitch
    kct pipeline board.kicad_pcb --step erc
    kct pipeline board.kicad_pcb --step fix-erc
    kct pipeline project.kicad_pro --mfr jlcpcb --layers 4
    kct pipeline board.kicad_pcb --layers 4-sig
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

logger = logging.getLogger(__name__)

__all__ = ["main"]


class PipelineStep(str, Enum):
    """Pipeline step identifiers."""

    ERC = "erc"
    FIX_ERC = "fix-erc"
    SYNC = "sync"
    FIX_SILKSCREEN = "fix-silkscreen"
    ROUTE = "route"
    STITCH = "stitch"
    FIX_VIAS = "fix-vias"
    FIX_DRC = "fix-drc"
    OPTIMIZE = "optimize"
    ZONES = "zones"
    ZONES_REFILL = "zones-refill"
    AUDIT = "audit"
    REPORT = "report"
    EXPORT = "export"


# Ordered list of all pipeline steps.
#
# Stitch runs AFTER route so stitching vias connect pads to planes on a
# fully-routed board.  It runs BEFORE optimize/zones so that zone fill
# sees the stitching vias and respects their clearance.
#
# Zone fill runs BEFORE fix-drc so that zone copper is computed against
# current trace positions.  After fix-drc nudges traces, a zone refill
# pass recomputes fill polygons to respect the new trace positions,
# eliminating zone-to-trace clearance violations.
ALL_STEPS = [
    PipelineStep.ERC,
    PipelineStep.FIX_ERC,
    PipelineStep.SYNC,
    PipelineStep.FIX_SILKSCREEN,
    PipelineStep.FIX_VIAS,
    PipelineStep.ROUTE,
    PipelineStep.STITCH,
    PipelineStep.OPTIMIZE,
    PipelineStep.ZONES,
    PipelineStep.FIX_DRC,
    PipelineStep.ZONES_REFILL,
    PipelineStep.AUDIT,
    PipelineStep.REPORT,
    PipelineStep.EXPORT,
]


@dataclass
class PipelineResult:
    """Result of a single pipeline step."""

    step: str
    success: bool
    message: str
    skipped: bool = False
    warning: bool = False


@dataclass
class PipelineContext:
    """Context for pipeline execution."""

    pcb_file: Path
    project_file: Path | None = None
    schematic_file: Path | None = None
    mfr: str = "jlcpcb"
    layers: str | None = None
    dry_run: bool = False
    verbose: bool = False
    quiet: bool = False
    force: bool = False
    is_project: bool = False
    commit: bool = False
    best_effort: bool = False
    no_cache: bool = False
    clear_cache: bool = False
    max_displacement: float = 2.0
    apply_sync: bool = False
    route_skip_threshold: float = 95.0
    erc_error_count: int = 0
    _check_data: dict | None = None  # cached kct check --format json result

    @property
    def layer_count(self) -> int:
        """Extract numeric layer count from the layers string.

        Qualified layer strings like '4-sig', '4-all' return 4.
        'auto' or None falls back to 2.
        """
        if self.layers is None or self.layers == "auto":
            return 2
        # Strip qualifier suffix (e.g. '4-sig' -> '4', '4-all' -> '4')
        base = self.layers.split("-")[0]
        try:
            return int(base)
        except ValueError:
            return 2


def _has_routing_segments(pcb_file: Path) -> tuple[bool, int]:
    """Detect whether a PCB has any top-level routed traces.

    Reads the PCB file and counts ``(segment ...)`` and ``(arc ...)`` nodes that
    are direct children of the root ``(kicad_pcb ...)`` node (depth 1).  Segments
    and arcs nested inside ``(zone ...)`` or ``(filled_polygon ...)`` blocks are
    excluded so that zone fill polygons are not mistaken for routed traces.

    This is a fast probe used as a precondition for routing-completeness
    assessment -- it is NOT a connectivity oracle.  Use
    :func:`_assess_routing_completeness` for skip/route decisions.

    Args:
        pcb_file: Path to .kicad_pcb file

    Returns:
        Tuple of (is_routed, segment_count) where is_routed is True if the
        board has any top-level routing segments or arcs.
    """
    try:
        content = pcb_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Could not read PCB file %s: %s", pcb_file, e)
        return False, 0

    # Count only top-level (segment ...) and (arc ...) entries.
    # In KiCad s-expression format, routed traces live at depth 1 (direct
    # children of the root (kicad_pcb ...) node).  Zone fills nest segments
    # inside (zone ... (filled_polygon ...)) at depth >= 2, so we track
    # parenthesis depth and only count matches at depth 1.
    segment_count = 0
    arc_count = 0
    depth = 0
    i = 0
    length = len(content)
    while i < length:
        ch = content[i]
        if ch == "(":
            # Check for tokens at depth 1 (inside root kicad_pcb node)
            if depth == 1:
                rest = content[i : i + 12]  # longest prefix we need
                if rest.startswith("(segment ") or rest.startswith("(segment\n"):
                    segment_count += 1
                elif rest.startswith("(arc ") or rest.startswith("(arc\n"):
                    arc_count += 1
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1

    total_traces = segment_count + arc_count

    return total_traces > 0, total_traces


@dataclass
class RoutingAssessment:
    """Per-net connectivity assessment with a skip recommendation.

    Produced by :func:`_assess_routing_completeness`, which delegates to
    :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` so the
    pipeline always agrees with ``kct net-status`` on the same board.

    Routing-required subset rules (mirroring the issue #2731 acceptance
    criteria):

    * **Single-pad nets** are not counted as signal nets -- they have no
      possible trace topology and cannot block a skip.  See
      :attr:`NetStatus.status <kicad_tools.analysis.net_status.NetStatus.status>`
      lines 98-114 for the underlying rule.
    * **Zone-fillable plane nets** (``is_plane_net`` with the zone closing
      the remaining unconnected pads) are likewise excluded from the
      signal-net total -- the stitch + zone-fill steps close those.

    Attributes:
        total_signal_nets: Number of nets that need traces (excludes
            single-pad nets and plane nets).
        complete_signal_nets: Subset of ``total_signal_nets`` whose
            connectivity status is "complete".
        incomplete_signal_nets: Signal nets that are not "complete".
        zone_fillable_nets: Names of plane nets handled by zones.
        single_pad_nets: Names of single-pad nets (always complete).
        signal_completion_percent: ``complete_signal_nets / total_signal_nets``
            as a percent (or 100.0 when ``total_signal_nets == 0``).
        recommend_skip: True iff the pipeline should skip the route step.
        summary: Human-readable summary suitable for the skip message.
        trace_count: Top-level segment+arc count (for diagnostics).
    """

    total_signal_nets: int = 0
    complete_signal_nets: int = 0
    incomplete_signal_nets: int = 0
    zone_fillable_nets: list[str] = None  # type: ignore[assignment]
    single_pad_nets: list[str] = None  # type: ignore[assignment]
    signal_completion_percent: float = 0.0
    recommend_skip: bool = False
    summary: str = ""
    trace_count: int = 0

    def __post_init__(self) -> None:
        if self.zone_fillable_nets is None:
            self.zone_fillable_nets = []
        if self.single_pad_nets is None:
            self.single_pad_nets = []


def _assess_routing_completeness(
    pcb_file: Path,
    threshold_percent: float = 95.0,
) -> RoutingAssessment:
    """Return per-net connectivity status with a skip recommendation.

    Uses :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` -- the
    same engine ``kct net-status`` uses -- so the pipeline never disagrees
    with the net-status command on the same board.

    The recommendation logic (issue #2731):

    1. Compute the **routing-required subset**: nets that need traces.
       Excluded from this subset:

       * Single-pad nets (``net.total_pads <= 1``) -- they are always
         "complete" by definition (``NetStatus.status`` at
         ``net_status.py:108-109``).
       * Pure plane nets where the zone closes connectivity
         (``net.is_plane_net`` and ``net.status == "complete"``).  The
         zone-fill step handles those.

    2. Skip iff:

       * The board has at least one top-level routing segment, AND
       * The routing-required subset is at least ``threshold_percent``
         complete, AND
       * Any remaining incomplete nets are zone-fillable plane nets
         only (the stitch + zone-fill steps will close those).

    Args:
        pcb_file: Path to .kicad_pcb file.
        threshold_percent: Minimum percentage of signal nets that must
            be complete for the skip to be recommended.  Defaults to
            95.0 to match the issue's recommended threshold.

    Returns:
        RoutingAssessment carrying counts, names, and the boolean
        ``recommend_skip`` decision plus a human-readable ``summary``.
    """
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    is_routed, trace_count = _has_routing_segments(pcb_file)

    try:
        result = NetStatusAnalyzer(pcb_file).analyze()
    except Exception as exc:
        # If we cannot analyze, fall back to "do not skip" so the router runs.
        logger.warning("Could not analyze net connectivity for %s: %s", pcb_file, exc)
        summary = f"net-status unavailable ({exc.__class__.__name__}); routing required"
        return RoutingAssessment(
            recommend_skip=False,
            summary=summary,
            trace_count=trace_count,
        )

    # Bucket nets according to the issue rules.
    single_pad_nets: list[str] = []
    zone_fillable_nets: list[str] = []
    incomplete_zone_fillable: list[str] = []
    signal_nets_total = 0
    signal_nets_complete = 0

    for net in result.nets:
        # Single-pad: never a routing candidate.
        if net.total_pads <= 1:
            single_pad_nets.append(net.net_name)
            continue
        # Plane nets: handled by zone fill / stitching.  Track them so we
        # can mention them in the skip summary, but they do NOT count
        # toward the signal-net totals (they cannot block a skip).
        if net.is_plane_net:
            zone_fillable_nets.append(net.net_name)
            if net.status != "complete":
                incomplete_zone_fillable.append(net.net_name)
            continue
        # Everything else is a signal net.
        signal_nets_total += 1
        if net.status == "complete":
            signal_nets_complete += 1

    incomplete_signal = signal_nets_total - signal_nets_complete
    if signal_nets_total > 0:
        percent = 100.0 * signal_nets_complete / signal_nets_total
    else:
        # Empty signal subset (board has only plane/single-pad nets):
        # nothing to route, treat as 100%.
        percent = 100.0

    threshold_ok = percent >= threshold_percent

    # Recommend skip only when:
    # - top-level segments exist (so a router has actually run before), AND
    # - signal-net completion meets the threshold.
    # Zone-fillable plane nets that are still incomplete do NOT block the
    # skip -- the stitch + zone-fill steps will close those connections.
    recommend_skip = is_routed and threshold_ok

    # Build a human-readable summary matching `kct net-status` numbers.
    parts: list[str] = []
    parts.append(f"{signal_nets_complete}/{signal_nets_total} signal nets complete")
    if zone_fillable_nets:
        parts.append(f"{len(zone_fillable_nets)} plane nets zone-fillable")
    if single_pad_nets:
        parts.append(f"{len(single_pad_nets)} single-pad nets")

    detail = ", ".join(parts)
    if recommend_skip:
        summary = f"route: skipped ({detail})"
    elif not is_routed:
        summary = f"route: no top-level segments -- routing required ({detail})"
    else:
        summary = (
            f"route: signal completion {percent:.1f}% below threshold "
            f"{threshold_percent:.1f}% -- routing required ({detail})"
        )

    return RoutingAssessment(
        total_signal_nets=signal_nets_total,
        complete_signal_nets=signal_nets_complete,
        incomplete_signal_nets=incomplete_signal,
        zone_fillable_nets=zone_fillable_nets,
        single_pad_nets=single_pad_nets,
        signal_completion_percent=percent,
        recommend_skip=recommend_skip,
        summary=summary,
        trace_count=trace_count,
    )


def _resolve_pcb_from_project(project_file: Path) -> Path | None:
    """Resolve .kicad_pcb path from a .kicad_pro file.

    KiCad project files use the same stem as their PCB files.

    Args:
        project_file: Path to .kicad_pro file

    Returns:
        Path to the corresponding .kicad_pcb if it exists, None otherwise.
    """
    pcb_path = project_file.with_suffix(".kicad_pcb")
    if pcb_path.exists():
        return pcb_path
    return None


def _resolve_schematic(pcb_file: Path, project_file: Path | None = None) -> Path | None:
    """Resolve .kicad_sch path from a project or PCB file.

    Delegates to :func:`kicad_tools.report.utils.find_schematic` which
    implements the full discovery chain: direct stem match, suffix
    stripping (``_routed``, ``_fixed``, etc.), project file lookup, and
    single-glob fallback.

    Args:
        pcb_file: Path to .kicad_pcb file
        project_file: Optional path to .kicad_pro file (unused; retained
            for API compatibility -- ``find_schematic`` discovers project
            files automatically)

    Returns:
        Path to the corresponding .kicad_sch if it exists, None otherwise.
    """
    from ..report.utils import find_schematic

    return find_schematic(pcb_file)


def _run_step_erc(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run ERC (Electrical Rules Check) step on the schematic.

    Checks the schematic for electrical rule violations before routing.
    Skips gracefully when no schematic is found or kicad-cli is missing.
    Halts the pipeline on errors unless --force is used.
    """
    from .runner import find_kicad_cli, run_erc

    # Skip if no schematic file available
    if ctx.schematic_file is None:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message="erc: no .kicad_sch found alongside PCB — skipped (use --sch to specify)",
            skipped=True,
        )

    # Check for kicad-cli availability
    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message="erc: kicad-cli not found — install KiCad 8 to enable ERC",
            skipped=True,
        )

    # Dry-run mode
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message=f"[dry-run] Would run: kct erc {ctx.schematic_file.name} --errors-only",
        )

    if not ctx.quiet:
        console.print(f"  Running ERC on {ctx.schematic_file.name}...")

    # Run ERC via kicad-cli
    result = run_erc(ctx.schematic_file, kicad_cli=kicad_cli)

    if not result.success:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=False,
            message=f"erc: failed to run — {result.stderr}",
        )

    # Parse the ERC report
    try:
        from ..erc import ERCReport

        report = ERCReport.load(result.output_path)
    except Exception as e:
        logger.warning("Could not parse ERC report: %s", e)
        return PipelineResult(
            step=PipelineStep.ERC,
            success=False,
            message=f"erc: failed to parse report — {e}",
        )
    finally:
        # Clean up temporary report file
        if result.output_path:
            result.output_path.unlink(missing_ok=True)

    from ..erc import ERC_BLOCKING_TYPES, ERC_NON_BLOCKING_TYPES

    # Partition error-level violations into blocking / non-blocking / unknown.
    # Unknown types default to blocking (conservative, consistent with auditor).
    blocking = [v for v in report.errors if v.type in ERC_BLOCKING_TYPES]
    non_blocking = [v for v in report.errors if v.type in ERC_NON_BLOCKING_TYPES]
    unknown_errors = [
        v
        for v in report.errors
        if v.type not in ERC_BLOCKING_TYPES and v.type not in ERC_NON_BLOCKING_TYPES
    ]
    blocking_error_count = len(blocking) + len(unknown_errors)
    non_blocking_count = len(non_blocking)
    warning_count = report.warning_count

    # Store *blocking* error count so FIX_ERC only runs when there are real errors.
    ctx.erc_error_count = blocking_error_count

    # Print per-violation details (unless --quiet)
    if not ctx.quiet and (blocking_error_count > 0 or non_blocking_count > 0 or warning_count > 0):
        from ..feedback.suggestions import generate_erc_suggestions

        for violation in report.violations:
            if violation.excluded:
                continue
            severity_tag = "ERR" if violation.is_error else "WARN"
            console.print(f"    [{severity_tag}] {violation.type_str}: {violation.description}")
            suggestions = generate_erc_suggestions(violation)
            if suggestions:
                console.print(f"          Suggestion: {suggestions[0]}")

    # No errors and no warnings -> clean pass
    if blocking_error_count == 0 and non_blocking_count == 0 and warning_count == 0:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message="erc: no violations found",
        )

    # No blocking errors but non-blocking errors present -> WARN, success=True
    if blocking_error_count == 0 and non_blocking_count > 0:
        msg_parts = [f"erc: {non_blocking_count} non-blocking error(s) as warning(s)"]
        if warning_count > 0:
            msg_parts.append(f"{warning_count} warning(s)")
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message=", ".join(msg_parts),
        )

    # Warnings only (no errors of any kind) -> pass
    if blocking_error_count == 0 and non_blocking_count == 0:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message=f"erc: {warning_count} warning(s), no errors",
        )

    # Blocking errors found
    if ctx.force:
        if not ctx.quiet:
            console.print(
                f"  [yellow]erc: {blocking_error_count} blocking error(s) found"
                " — continuing (--force)[/yellow]"
            )
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message=f"erc: {blocking_error_count} blocking error(s) found — continuing (--force)",
        )

    # Halt pipeline
    return PipelineResult(
        step=PipelineStep.ERC,
        success=False,
        message=f"erc: {blocking_error_count} blocking error(s) found (use --force to continue)",
    )


def _run_step_fix_erc(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run fix-erc step to auto-remediate ERC violations.

    Invokes ``kct fix-erc <schematic>`` as a subprocess when the preceding
    ERC step detected errors.  Skips gracefully when:
    - No schematic file is available.
    - The ERC step found zero errors (and ``--force`` is not set).
    """
    # Skip if no schematic file available
    if ctx.schematic_file is None:
        return PipelineResult(
            step=PipelineStep.FIX_ERC,
            success=True,
            message="fix-erc: no .kicad_sch found — skipped",
            skipped=True,
        )

    # Skip if ERC found no errors (unless --force)
    if ctx.erc_error_count == 0 and not ctx.force:
        return PipelineResult(
            step=PipelineStep.FIX_ERC,
            success=True,
            message="fix-erc: no ERC errors to fix — skipped",
            skipped=True,
        )

    # Dry-run mode
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.FIX_ERC,
            success=True,
            message=f"[dry-run] Would run: kct fix-erc {ctx.schematic_file.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running fix-erc on {ctx.schematic_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "fix-erc",
        str(ctx.schematic_file),
    ]

    success, message = _run_subprocess_step(cmd, ctx.schematic_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.FIX_ERC,
        success=success,
        message=f"fix-erc: {message}",
    )


def _run_step_fix_silkscreen(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run silkscreen line-width repair step.

    Fixes silkscreen line widths to meet manufacturer minimum specifications.
    This is safe to run unconditionally and is idempotent.
    """
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.FIX_SILKSCREEN,
            success=True,
            message=f"[dry-run] Would run: kct fix-silkscreen {ctx.pcb_file.name} --mfr {ctx.mfr}",
        )

    if not ctx.quiet:
        console.print(f"  Fixing silkscreen widths for {ctx.mfr}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "fix-silkscreen",
        str(ctx.pcb_file),
        "--mfr",
        ctx.mfr,
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)
    return PipelineResult(
        step=PipelineStep.FIX_SILKSCREEN,
        success=success,
        message=f"fix-silkscreen: {message}",
    )


def _run_step_fix_erc(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run ERC auto-fix step on the schematic.

    Inserts PWR_FLAG and no-connect markers to resolve common ERC violations.
    Skips gracefully when no schematic is found.
    """
    # Skip if no schematic file available
    if ctx.schematic_file is None:
        return PipelineResult(
            step=PipelineStep.FIX_ERC,
            success=True,
            message="fix-erc: no .kicad_sch found alongside PCB -- skipped",
            skipped=True,
        )

    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.FIX_ERC,
            success=True,
            message=f"[dry-run] Would run: kct fix-erc {ctx.schematic_file.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running ERC auto-fix on {ctx.schematic_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "fix-erc",
        str(ctx.schematic_file),
    ]

    success, message = _run_subprocess_step(cmd, ctx.schematic_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.FIX_ERC,
        success=success,
        message=f"fix-erc: {message}",
    )


def _print_sync_analysis_detail(analysis, console: Console) -> None:
    """Print per-category sync analysis detail (mirrors sync_cmd._output_table)."""
    if analysis.value_mismatches:
        console.print(f"    Value mismatches ({len(analysis.value_mismatches)}):")
        for mm in analysis.value_mismatches:
            console.print(
                f"      {mm['reference']}: sch={mm['schematic_value']} pcb={mm['pcb_value']}"
            )

    if analysis.footprint_mismatches:
        console.print(f"    Footprint mismatches ({len(analysis.footprint_mismatches)}):")
        for mm in analysis.footprint_mismatches:
            console.print(
                f"      {mm['reference']}: sch={mm['schematic_footprint']}"
                f" pcb={mm['pcb_footprint']}"
            )

    if analysis.add_footprint_actions:
        console.print(f"    Add footprint ({len(analysis.add_footprint_actions)}):")
        for action in analysis.add_footprint_actions:
            ref = action["reference"]
            fp = action.get("footprint", "")
            val = action.get("value", "")
            console.print(f"      {ref}: {fp} ({val})")

    if analysis.schematic_orphans:
        console.print(f"    Schematic-only ({len(analysis.schematic_orphans)}):")
        for ref in analysis.schematic_orphans:
            console.print(f"      {ref} - missing from PCB")

    if analysis.pcb_orphans:
        console.print(f"    PCB-only ({len(analysis.pcb_orphans)}):")
        for ref in analysis.pcb_orphans:
            console.print(f"      {ref} - not in schematic")


def _run_step_sync(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Reconcile schematic <-> PCB component sets in-process.

    Uses :class:`kicad_tools.sync.reconciler.Reconciler` to analyse drift
    between the schematic and PCB before routing.  Behaviour matches the
    ERC step's blocking semantics:

    - In sync: success with a one-line "sync: in sync" message.
    - Schematic orphans (refs missing from PCB): blocking.  Halts the
      pipeline unless ``--force`` (continue past drift) or ``--apply-sync``
      (auto-add missing footprints and apply high-confidence corrections)
      is set.
    - Value/footprint mismatches or PCB-only refs without schematic
      orphans: warning, pipeline continues.

    Skips gracefully when no schematic is available.
    """
    # Skip if no schematic file available
    if ctx.schematic_file is None:
        return PipelineResult(
            step=PipelineStep.SYNC,
            success=True,
            message="sync: no .kicad_sch found alongside PCB — skipped (use --sch to specify)",
            skipped=True,
        )

    # Dry-run mode: preview without instantiating Reconciler
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.SYNC,
            success=True,
            message=(
                f"[dry-run] Would run: kct sync --analyze {ctx.schematic_file.name}"
                f" --pcb {ctx.pcb_file.name}"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Reconciling {ctx.schematic_file.name} <-> {ctx.pcb_file.name}...")

    # Instantiate Reconciler in-process (no subprocess)
    try:
        from ..sync.reconciler import Reconciler

        reconciler = Reconciler(
            schematic=ctx.schematic_file,
            pcb=ctx.pcb_file,
        )
        analysis = reconciler.analyze()
    except Exception as e:
        logger.warning("sync: failed to analyze: %s", e)
        return PipelineResult(
            step=PipelineStep.SYNC,
            success=False,
            message=f"sync: failed to analyze — {e}",
        )

    # Clean pass
    if analysis.is_in_sync:
        return PipelineResult(
            step=PipelineStep.SYNC,
            success=True,
            message="sync: in sync",
        )

    # Print the summary plus per-category detail
    if not ctx.quiet:
        for line in analysis.summary().splitlines():
            console.print(f"    {line}")
        _print_sync_analysis_detail(analysis, console)

    schematic_orphans = list(analysis.schematic_orphans)
    value_mismatch_count = len(analysis.value_mismatches)
    footprint_mismatch_count = len(analysis.footprint_mismatches)
    pcb_orphan_count = len(analysis.pcb_orphans)

    # --apply-sync: invoke Reconciler.apply() to auto-add missing footprints
    # and apply high-confidence value/footprint corrections.
    if ctx.apply_sync:
        try:
            changes = reconciler.apply(
                analysis,
                dry_run=False,
                min_confidence="high",
                remove_orphans=False,
            )
        except Exception as e:
            logger.warning("sync: apply failed: %s", e)
            return PipelineResult(
                step=PipelineStep.SYNC,
                success=False,
                message=f"sync: apply failed — {e}",
            )

        applied = [c for c in changes if c.applied]
        if not ctx.quiet:
            console.print(f"    Applied {len(applied)} change(s) (of {len(changes)} proposed)")

        # Re-run analyze() so the user sees the residual drift
        try:
            post_analysis = reconciler.analyze()
        except Exception as e:
            logger.warning("sync: post-apply analyze failed: %s", e)
            post_analysis = None

        if post_analysis is not None and not ctx.quiet:
            console.print("    Post-apply summary:")
            for line in post_analysis.summary().splitlines():
                console.print(f"      {line}")

        return PipelineResult(
            step=PipelineStep.SYNC,
            success=True,
            message=(f"sync: applied {len(applied)} change(s) (min_confidence=high)"),
            warning=bool(post_analysis is not None and not post_analysis.is_in_sync),
        )

    # Schematic orphans are blocking (parallels ERC's blocking semantics)
    if schematic_orphans:
        if ctx.force:
            if not ctx.quiet:
                console.print(
                    f"  [yellow]sync: {len(schematic_orphans)} schematic-only ref(s)"
                    " missing from PCB — continuing (--force)[/yellow]"
                )
            return PipelineResult(
                step=PipelineStep.SYNC,
                success=True,
                message=(
                    f"sync: {len(schematic_orphans)} schematic-only ref(s)"
                    " missing from PCB — continuing (--force)"
                ),
                warning=True,
            )

        return PipelineResult(
            step=PipelineStep.SYNC,
            success=False,
            message=(
                f"sync: {len(schematic_orphans)} schematic-only ref(s) missing"
                " from PCB (use --apply-sync to add, or --force to continue)"
            ),
        )

    # No blocking schematic orphans, but other drift exists -> WARN, success=True
    parts = []
    if value_mismatch_count:
        parts.append(f"{value_mismatch_count} value mismatch(es)")
    if footprint_mismatch_count:
        parts.append(f"{footprint_mismatch_count} footprint mismatch(es)")
    if pcb_orphan_count:
        parts.append(f"{pcb_orphan_count} PCB-only ref(s)")

    return PipelineResult(
        step=PipelineStep.SYNC,
        success=True,
        message=(
            "sync: drift detected — " + (", ".join(parts) if parts else "non-blocking issues")
        ),
        warning=True,
    )


def _run_subprocess_step(
    cmd: list[str],
    cwd: Path,
    verbose: bool = False,
) -> tuple[bool, str]:
    """Run a subprocess command and return (success, message).

    Args:
        cmd: Command and arguments to execute
        cwd: Working directory
        verbose: Whether to show output

    Returns:
        Tuple of (success, output/error message)
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=not verbose,
            text=True,
        )

        if result.returncode == 0:
            return True, "completed successfully"
        elif result.returncode in (2, 3, 4, 5):
            # Exit codes 2-5 indicate usable output exists:
            #   2 = partial routing below threshold
            #   3 = routing meets threshold but DRC violations remain
            #   4 = partial routing with segment-segment clearance violations
            #   5 = interrupted by SIGINT with partial results saved
            return True, "completed with warnings"
        else:
            error_msg = result.stderr.strip() if result.stderr else f"exit code {result.returncode}"
            return False, f"failed: {error_msg}"

    except FileNotFoundError:
        return False, f"command not found: {cmd[0]}"
    except Exception as e:
        return False, f"failed: {e}"


def _run_step_route(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run routing step if the board is not yet sufficiently routed.

    Skip semantics (issue #2731): the route step is skipped only when
    :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` reports the
    routing-required signal-net subset as at least
    ``ctx.route_skip_threshold`` percent complete.  Single-pad nets and
    zone-fillable plane nets do not block the skip -- those are handled by
    other pipeline steps (or are inherently complete).  ``--force`` always
    re-runs the router.
    """
    assessment = _assess_routing_completeness(
        ctx.pcb_file, ctx.route_skip_threshold
    )

    if assessment.recommend_skip and not ctx.force:
        return PipelineResult(
            step=PipelineStep.ROUTE,
            success=True,
            message=assessment.summary,
            skipped=True,
        )

    route_layers = ctx.layers or "auto"

    if ctx.dry_run:
        cache_flags = ""
        if ctx.no_cache:
            cache_flags += " --no-cache"
        if ctx.clear_cache:
            cache_flags += " --clear-cache"
        if assessment.recommend_skip:
            return PipelineResult(
                step=PipelineStep.ROUTE,
                success=True,
                message=(
                    f"[dry-run] Would re-route (--force): {ctx.pcb_file.name} "
                    f"--grid auto --manufacturer {ctx.mfr} --layers {route_layers} --auto-fix"
                    f"{cache_flags}"
                ),
            )
        return PipelineResult(
            step=PipelineStep.ROUTE,
            success=True,
            message=(
                f"[dry-run] Would run: kct route {ctx.pcb_file.name} "
                f"--grid auto --manufacturer {ctx.mfr} --layers {route_layers} --auto-fix"
                f"{cache_flags}"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Running autorouter on {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(ctx.pcb_file),
        "-o",
        str(ctx.pcb_file),  # Route in place for pipeline
        "--grid",
        "auto",
        "--manufacturer",
        ctx.mfr,
        "--layers",
        route_layers,
        "--auto-fix",
    ]

    if ctx.quiet:
        cmd.append("--quiet")
    if ctx.no_cache:
        cmd.append("--no-cache")
    if ctx.clear_cache:
        cmd.append("--clear-cache")

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    # Detect partial routing (exit code 2 -> "completed with warnings")
    is_partial = success and "warnings" in message

    return PipelineResult(
        step=PipelineStep.ROUTE,
        success=success,
        message=f"route: {message}",
        warning=is_partial,
    )


def _run_step_stitch(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run stitching via placement step for multi-layer boards.

    Adds stitching vias to connect surface-mount component pads to
    internal power/ground planes.  Skips gracefully when:
    - The board has only 2 layers (no internal planes).
    - No plane nets are detected in the PCB zones.
    """
    # Skip on 2-layer boards (no internal planes to stitch to)
    if ctx.layer_count <= 2:
        return PipelineResult(
            step=PipelineStep.STITCH,
            success=True,
            message="stitch: 2-layer board — skipped (no internal planes)",
            skipped=True,
        )

    # Probe the PCB for plane nets before invoking the subprocess.
    # This avoids spawning a child process just to discover there is
    # nothing to stitch.
    try:
        from ..cli.stitch_cmd import find_all_plane_nets
        from ..core.sexp_file import load_pcb as _load_pcb

        sexp = _load_pcb(ctx.pcb_file)
        plane_nets = find_all_plane_nets(sexp)
    except Exception as exc:
        logger.debug("Could not probe plane nets: %s", exc)
        plane_nets = {}

    if not plane_nets:
        return PipelineResult(
            step=PipelineStep.STITCH,
            success=True,
            message="stitch: no plane nets detected — skipped",
            skipped=True,
        )

    # Look up manufacturer via specs so stitching vias meet DRC requirements.
    try:
        from ..manufacturers import get_profile

        profile = get_profile(ctx.mfr)
        rules = profile.get_design_rules(layers=ctx.layer_count)
        via_size = rules.min_via_diameter_mm
        via_drill = rules.min_via_drill_mm
    except Exception:
        # Fall back to conservative defaults if lookup fails
        via_size = 0.6
        via_drill = 0.3

    if ctx.dry_run:
        nets_str = ", ".join(sorted(plane_nets.keys()))
        return PipelineResult(
            step=PipelineStep.STITCH,
            success=True,
            message=(
                f"[dry-run] Would run: kct stitch {ctx.pcb_file.name} "
                f"--via-size {via_size} --drill {via_drill} "
                f"(auto-detected nets: {nets_str})"
            ),
        )

    if not ctx.quiet:
        console.print(
            f"  Stitching vias on {ctx.pcb_file.name} "
            f"({len(plane_nets)} plane net(s), "
            f"via {via_size}mm/{via_drill}mm drill for {ctx.mfr})..."
        )

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "stitch",
        str(ctx.pcb_file),
        "--via-size",
        str(via_size),
        "--drill",
        str(via_drill),
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.STITCH,
        success=success,
        message=f"stitch: {message}",
    )


def _run_step_fix_vias(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run via repair step."""
    numeric_layers = ctx.layer_count
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.FIX_VIAS,
            success=True,
            message=f"[dry-run] Would run: kct fix-vias {ctx.pcb_file.name} --mfr {ctx.mfr} --layers {numeric_layers}",
        )

    if not ctx.quiet:
        console.print(f"  Fixing vias for {ctx.mfr} ({numeric_layers} layers)...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "fix-vias",
        str(ctx.pcb_file),
        "--mfr",
        ctx.mfr,
        "--layers",
        str(numeric_layers),
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.FIX_VIAS,
        success=success,
        message=f"fix-vias: {message}",
    )


def _run_step_fix_drc(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run DRC repair step."""
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.FIX_DRC,
            success=True,
            message=(
                f"[dry-run] Would run: kct fix-drc {ctx.pcb_file.name} "
                f"--max-passes 20 --local-reroute --max-displacement {ctx.max_displacement}"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Running DRC repair on {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "fix-drc",
        str(ctx.pcb_file),
        "--max-passes",
        "20",
        "--local-reroute",
        "--max-displacement",
        str(ctx.max_displacement),
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.FIX_DRC,
        success=success,
        message=f"fix-drc: {message}",
    )


def _run_step_optimize(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run trace optimization step."""
    numeric_layers = ctx.layer_count
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.OPTIMIZE,
            success=True,
            message=(
                f"[dry-run] Would run: kct optimize-traces {ctx.pcb_file.name} "
                f"--drc-aware --mfr {ctx.mfr} --layers {numeric_layers}"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Optimizing traces in {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "optimize-traces",
        str(ctx.pcb_file),
        "--drc-aware",
        "--mfr",
        ctx.mfr,
        "--layers",
        str(numeric_layers),
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.OPTIMIZE,
        success=success,
        message=f"optimize-traces: {message}",
    )


def _run_step_zones(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run zone fill step (requires kicad-cli)."""
    from .runner import find_kicad_cli

    kicad_cli = find_kicad_cli()

    if kicad_cli is None:
        return PipelineResult(
            step=PipelineStep.ZONES,
            success=True,
            message="zones fill: skipped (kicad-cli not installed)",
            skipped=True,
        )

    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.ZONES,
            success=True,
            message=f"[dry-run] Would run: kct zones fill {ctx.pcb_file.name}",
        )

    if not ctx.quiet:
        console.print(f"  Filling zones in {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "zones",
        "fill",
        str(ctx.pcb_file),
    ]

    if ctx.quiet:
        cmd.append("--quiet")

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    # Validate net format after zone fill — kicad-cli may corrupt nets.
    if success:
        from .runner import validate_net_format

        report = validate_net_format(ctx.pcb_file)
        if not report.valid:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Net format corruption detected after zone fill: "
                "%d element(s) have non-canonical net format "
                "(name_only_segments=%d, name_only_vias=%d, name_only_pads=%d, "
                "empty_net_segments=%d, empty_net_vias=%d, empty_net_pads=%d)",
                report.total_corrupt,
                report.name_only_segments,
                report.name_only_vias,
                report.name_only_pads,
                report.empty_net_segments,
                report.empty_net_vias,
                report.empty_net_pads,
            )

    return PipelineResult(
        step=PipelineStep.ZONES,
        success=success,
        message=f"zones fill: {message}",
    )


def _run_step_zones_refill(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Re-fill zones after fix-drc has nudged traces.

    This ensures zone copper is recomputed against post-nudge trace
    positions, eliminating zone-to-trace clearance violations that would
    otherwise appear in the audit step.
    """
    from .runner import find_kicad_cli

    kicad_cli = find_kicad_cli()

    if kicad_cli is None:
        return PipelineResult(
            step=PipelineStep.ZONES_REFILL,
            success=True,
            message="zones refill: skipped (kicad-cli not installed)",
            skipped=True,
        )

    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.ZONES_REFILL,
            success=True,
            message=f"[dry-run] Would run: kct zones fill {ctx.pcb_file.name} (refill)",
        )

    if not ctx.quiet:
        console.print(f"  Re-filling zones in {ctx.pcb_file.name} (post fix-drc)...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "zones",
        "fill",
        str(ctx.pcb_file),
    ]

    if ctx.quiet:
        cmd.append("--quiet")

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    # Validate net format after zone refill — kicad-cli may corrupt nets.
    if success:
        from .runner import validate_net_format

        report = validate_net_format(ctx.pcb_file)
        if not report.valid:
            logger = logging.getLogger(__name__)
            logger.warning(
                "Net format corruption detected after zone refill: "
                "%d element(s) have non-canonical net format "
                "(name_only_segments=%d, name_only_vias=%d, name_only_pads=%d, "
                "empty_net_segments=%d, empty_net_vias=%d, empty_net_pads=%d)",
                report.total_corrupt,
                report.name_only_segments,
                report.name_only_vias,
                report.name_only_pads,
                report.empty_net_segments,
                report.empty_net_vias,
                report.empty_net_pads,
            )

    return PipelineResult(
        step=PipelineStep.ZONES_REFILL,
        success=success,
        message=f"zones refill: {message}",
    )


def _run_step_audit(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run final audit/check step."""
    # Use project-level audit if we have a .kicad_pro, else PCB-level check
    if ctx.is_project and ctx.project_file:
        target = str(ctx.project_file)
        cmd_name = "audit"
    else:
        target = str(ctx.pcb_file)
        cmd_name = "check"

    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.AUDIT,
            success=True,
            message=f"[dry-run] Would run: kct {cmd_name} {Path(target).name} --mfr {ctx.mfr}",
        )

    if not ctx.quiet:
        console.print(f"  Running {cmd_name} on {Path(target).name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        cmd_name,
        target,
        "--mfr",
        ctx.mfr,
    ]

    if cmd_name == "check":
        cmd.extend(["--layers", str(ctx.layer_count)])

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.AUDIT,
        success=success,
        message=f"{cmd_name}: {message}",
    )


def _run_step_report(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run report generation step (final step after AUDIT).

    Writes output into ``manufacturing/`` (the same directory used by
    the EXPORT step) so the full pipeline produces a single output
    directory instead of splitting between ``reports/`` and
    ``manufacturing/``.
    """
    mfr_dir = ctx.pcb_file.parent / "manufacturing"

    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.REPORT,
            success=True,
            message=(
                f"[dry-run] Would run: kct report generate {ctx.pcb_file.name} "
                f"--mfr {ctx.mfr} --no-figures -o manufacturing/"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Generating report for {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "report",
        "generate",
        str(ctx.pcb_file),
        "--mfr",
        ctx.mfr,
        "--no-figures",
        "-o",
        str(mfr_dir),
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.REPORT,
        success=success,
        message=f"report: {message}",
    )


def _run_step_export(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run manufacturing export step (final step after REPORT).

    Invokes ``kct export`` to generate gerbers, BOM, CPL, project ZIP
    and manifest in a ``manufacturing/`` directory alongside the PCB.
    """
    mfr_dir = ctx.pcb_file.parent / "manufacturing"

    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.EXPORT,
            success=True,
            message=(
                f"[dry-run] Would run: kct export {ctx.pcb_file.name} "
                f"--mfr {ctx.mfr} -o manufacturing/"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Exporting manufacturing package for {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "export",
        str(ctx.pcb_file),
        "--mfr",
        ctx.mfr,
        "-o",
        str(mfr_dir),
    ]

    # Pass --sch so the BOM/PCB match preflight check can run.  Without
    # the schematic, ``_check_bom_footprint_match`` falls back to the
    # PCB-derived BOM (which trivially matches itself), so schematic-only
    # refs are not detected.  Auto-detect via the pipeline context or
    # ``find_schematic`` when no explicit path is set.
    sch_path: Path | None = ctx.schematic_file
    if sch_path is None:
        sch_path = _resolve_schematic(ctx.pcb_file, ctx.project_file)
    if sch_path is not None and sch_path.exists():
        cmd.extend(["--sch", str(sch_path)])

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.EXPORT,
        success=success,
        message=f"export: {message}",
    )


def _is_git_repo(directory: Path) -> bool:
    """Check whether *directory* is inside a git repository.

    Args:
        directory: Directory to check.

    Returns:
        True if the directory is inside a git working tree.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _fetch_audit_results(ctx: PipelineContext) -> dict | None:
    """Run ``kct audit --format json`` and return parsed result dict.

    Used by :func:`_print_final_summary` so the pipeline can derive its
    verdict from the audit's sync drift state (schematic-only refs make
    the BOM unbuildable -> NOT_READY).

    Args:
        ctx: Pipeline context (project or PCB path, manufacturer).

    Returns:
        Parsed JSON dict on success, ``None`` on any failure.
    """
    import json as _json

    # Audit needs a project file when available so it can pick up the
    # schematic for sync drift detection.  Fall back to the PCB.
    if ctx.is_project and ctx.project_file:
        target = str(ctx.project_file)
    else:
        target = str(ctx.pcb_file)

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "audit",
        target,
        "--mfr",
        ctx.mfr,
        "--format",
        "json",
    ]

    try:
        audit_result = subprocess.run(
            cmd,
            cwd=str(ctx.pcb_file.parent),
            capture_output=True,
            text=True,
        )
        # audit exits 2 on NOT_READY but still emits valid JSON to stdout
        data = _json.loads(audit_result.stdout)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _fetch_check_results(ctx: PipelineContext) -> dict | None:
    """Run ``kct check --format json`` and return parsed result dict.

    This helper is shared between :func:`_build_commit_message` and
    :func:`_print_final_summary` so that the check subprocess is only
    invoked once per pipeline run.

    Args:
        ctx: Pipeline context (PCB path, manufacturer, layers).

    Returns:
        Parsed JSON dict on success, ``None`` on any failure.
    """
    import json as _json

    try:
        check_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "check",
                str(ctx.pcb_file),
                "--mfr",
                ctx.mfr,
                "--layers",
                str(ctx.layer_count),
                "--format",
                "json",
            ],
            cwd=str(ctx.pcb_file.parent),
            capture_output=True,
            text=True,
        )
        data = _json.loads(check_result.stdout)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _print_final_summary(
    ctx: PipelineContext,
    results: list[PipelineResult],
    console: Console,
    check_data: dict | None = None,
    audit_data: dict | None = None,
) -> None:
    """Print a DRC/ERC/verdict summary block after pipeline completion.

    Called from :func:`run_pipeline` when the run is a full pipeline
    (not single-step) and neither ``--quiet`` nor ``--dry-run`` is set.

    Args:
        ctx: Pipeline context.
        results: List of step results from the pipeline run.
        console: Rich console for output.
        check_data: Pre-fetched ``kct check --format json`` result, or
            ``None`` to skip the subprocess (used for dry-run placeholder).
        audit_data: Pre-fetched ``kct audit --format json`` result with
            the new ``sync`` section.  When supplied with schematic-only
            refs, the verdict is forced to NOT READY regardless of
            DRC/ERC pass state (an unbuildable BOM blocks manufacturing).
    """
    # --- DRC ---
    if check_data is not None:
        summary = check_data.get("summary", {})
        error_count = summary.get("errors", 0)
        violations = check_data.get("violations", [])

        # Per-rule-type breakdown of errors
        by_type: dict[str, int] = {}
        for v in violations:
            if v.get("severity") == "error":
                vtype = v.get("type", v.get("type_str", "unknown"))
                by_type[vtype] = by_type.get(vtype, 0) + 1

        if by_type:
            breakdown = ", ".join(
                f"{count} {rule}" for rule, count in sorted(by_type.items(), key=lambda x: -x[1])
            )
            drc_line = f"{error_count} errors ({breakdown})"
        elif error_count > 0:
            drc_line = f"{error_count} errors"
        else:
            drc_line = "0 errors"

        # Silkscreen warnings: violations whose type contains "silk"
        silk_count = sum(1 for v in violations if "silk" in v.get("type", "").lower())
    else:
        error_count = -1  # sentinel: unknown
        drc_line = "unknown (check failed)"
        silk_count = 0

    # --- ERC ---
    erc_result = next(
        (r for r in results if r.step == PipelineStep.ERC or r.step == PipelineStep.ERC.value),
        None,
    )
    if erc_result is None or erc_result.skipped:
        erc_line = "skipped"
    elif "no violations" in erc_result.message.lower():
        erc_line = "PASS"
    elif "non-blocking" in erc_result.message.lower():
        erc_line = f"PASS with warnings -- {erc_result.message}"
    elif not erc_result.success or "blocking" in erc_result.message.lower():
        erc_line = f"FAIL -- {erc_result.message}"
    else:
        erc_line = erc_result.message

    # --- Audit ---
    audit_result = next(
        (r for r in results if r.step == PipelineStep.AUDIT or r.step == PipelineStep.AUDIT.value),
        None,
    )

    # --- Sync drift (schematic <-> PCB) ---
    # Read from audit JSON when available.  Schematic-only refs make the
    # BOM unbuildable, which trumps every other check: the package cannot
    # be manufactured regardless of DRC/ERC pass state.
    sync_summary = audit_data.get("sync", {}) if audit_data else {}
    sync_schematic_only = sync_summary.get("schematic_only_count", 0)
    sync_pcb_only = sync_summary.get("pcb_only_count", 0)
    sync_value_mm = sync_summary.get("value_mismatch_count", 0)
    sync_fp_mm = sync_summary.get("footprint_mismatch_count", 0)
    sync_drift_parts: list[str] = []
    if sync_schematic_only:
        sync_drift_parts.append(f"{sync_schematic_only} schematic-only")
    if sync_pcb_only:
        sync_drift_parts.append(f"{sync_pcb_only} PCB-only")
    if sync_value_mm:
        sync_drift_parts.append(f"{sync_value_mm} value mm")
    if sync_fp_mm:
        sync_drift_parts.append(f"{sync_fp_mm} fp mm")
    if not audit_data:
        sync_line = "skipped (audit unavailable)"
    elif not sync_drift_parts:
        sync_line = "in sync"
    else:
        sync_line = ", ".join(sync_drift_parts)

    # --- Verdict ---
    # Schematic-only refs == unbuildable BOM == hard fail.  Check this
    # before audit_result.success because the pipeline's audit subprocess
    # may have run before the sync axis was wired in, but the JSON we
    # just fetched is authoritative.
    if sync_schematic_only > 0:
        verdict = (
            f"[red]NOT READY[/red] -- {sync_schematic_only} schematic-only refs (unbuildable BOM)"
        )
    # When the AUDIT step ran, its success/failure reflects the full audit
    # verdict (ERC, DRC, connectivity, manufacturer compatibility).  Use it
    # as the authoritative source so the pipeline summary matches the audit.
    elif audit_result is not None and not audit_result.skipped:
        if audit_result.success:
            # Audit passed -- distinguish READY from WARNING.
            # Check for DRC/ERC warnings to surface a WARNING verdict.
            has_drc_warnings = (
                check_data is not None and check_data.get("summary", {}).get("warnings", 0) > 0
            )
            erc_has_warnings = (
                erc_result is not None
                and not erc_result.skipped
                and (
                    "non-blocking" in erc_result.message.lower()
                    or (erc_result.warning and erc_result.success)
                )
            )
            has_sync_warnings = sync_pcb_only > 0 or sync_value_mm > 0 or sync_fp_mm > 0
            if has_drc_warnings or erc_has_warnings or has_sync_warnings:
                bits: list[str] = []
                if has_drc_warnings:
                    bits.append("DRC warnings")
                if erc_has_warnings:
                    bits.append("ERC warnings")
                if has_sync_warnings:
                    bits.append("sync drift")
                verdict = (
                    f"[yellow]WARNING[/yellow] -- audit passed with warnings ({', '.join(bits)})"
                )
            else:
                verdict = "[green]READY[/green] -- audit passed"
        else:
            # Audit failed -- collect failure reasons from individual steps.
            reasons: list[str] = []
            if error_count > 0:
                reasons.append(f"{error_count} DRC error(s)")
            if erc_result is not None and not erc_result.skipped and not erc_result.success:
                reasons.append("ERC errors")
            if not reasons:
                reasons.append("see audit output above")
            verdict = f"[red]NOT READY[/red] -- {', '.join(reasons)}"
    else:
        # Audit step did not run -- fall back to DRC + ERC heuristics.
        erc_failed = erc_result is not None and not erc_result.skipped and not erc_result.success
        if error_count == 0 and not erc_failed:
            verdict = "[green]READY[/green] -- board passes DRC"
        elif error_count > 0 and erc_failed:
            verdict = (
                f"[red]NOT READY[/red] -- {error_count} DRC error(s) and ERC errors to resolve"
            )
        elif error_count > 0:
            verdict = f"[red]NOT READY[/red] -- {error_count} DRC error(s) to resolve"
        elif erc_failed:
            verdict = "[red]NOT READY[/red] -- ERC errors to resolve"
        else:
            verdict = "unknown -- could not determine DRC status"

    console.print()
    console.print("[bold]Summary:[/bold]")
    console.print(f"  DRC:        {drc_line}")
    console.print(f"  ERC:        {erc_line}")
    console.print(f"  Silkscreen: {silk_count} warnings")
    console.print(f"  Sync:       {sync_line}")
    console.print(f"  Verdict:    {verdict}")


def _build_commit_message(
    ctx: PipelineContext,
    results: list[PipelineResult],
    check_data: dict | None = None,
) -> str:
    """Build a structured commit message from pipeline results.

    Attempts to extract DRC error count and routing net counts.  Falls back
    to a simpler message when metrics cannot be determined.

    Args:
        ctx: Pipeline context (for manufacturer name).
        results: List of results from the pipeline run.
        check_data: Pre-fetched ``kct check --format json`` result. When
            ``None``, the helper calls :func:`_fetch_check_results` itself.

    Returns:
        A single-line commit message string.
    """
    drc_errors: int | None = None
    routed_nets: int | None = None
    total_nets: int | None = None

    # Pull routing status from NetStatusAnalyzer so the commit message
    # matches the numbers ``kct net-status`` would print (issue #2731).
    try:
        from kicad_tools.analysis.net_status import NetStatusAnalyzer

        net_result = NetStatusAnalyzer(ctx.pcb_file).analyze()
        if net_result.total_nets > 0:
            routed_nets = net_result.complete_count
            total_nets = net_result.total_nets
    except Exception:
        pass

    # Reuse pre-fetched check data or fetch now
    data = check_data if check_data is not None else _fetch_check_results(ctx)
    if data is not None:
        # Try common keys for violation count
        drc_errors = data.get("total_violations", data.get("violations_count"))
        if drc_errors is None and "violations" in data:
            violations = data["violations"]
            if isinstance(violations, list):
                drc_errors = len(violations)

    # Build message
    parts: list[str] = []
    if drc_errors is not None:
        parts.append(f"{drc_errors} DRC errors")
    if routed_nets is not None and total_nets is not None:
        parts.append(f"{routed_nets}/{total_nets} signal nets routed")

    if parts:
        detail = ", ".join(parts)
        return f"fix: run kct pipeline ({detail})"
    else:
        return f"fix: run kct pipeline ({ctx.mfr})"


def _git_commit_result(
    ctx: PipelineContext,
    results: list[PipelineResult],
    console: Console,
) -> int:
    """Stage the PCB file and create a git commit after a successful pipeline run.

    Args:
        ctx: Pipeline context.
        results: Pipeline step results (used for commit message).
        console: Rich console for output.

    Returns:
        0 on success, 1 on failure.
    """
    pcb_dir = ctx.pcb_file.parent

    # Verify we are inside a git repository
    if not _is_git_repo(pcb_dir):
        print(
            f"Error: --commit requires a git repository, "
            f"but {pcb_dir} is not inside a git working tree.",
            file=sys.stderr,
        )
        return 1

    # Stage the PCB file and manufacturing/ directory (if present)
    manufacturing_dir = ctx.pcb_file.parent / "manufacturing"
    files_to_stage = [str(ctx.pcb_file)]
    if manufacturing_dir.exists():
        files_to_stage.append(str(manufacturing_dir))
    add_result = subprocess.run(
        ["git", "-C", str(pcb_dir), "add"] + files_to_stage,
        capture_output=True,
        text=True,
    )
    if add_result.returncode != 0:
        print(
            f"Error: git add failed: {add_result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    # Check whether there are actually staged changes
    diff_result = subprocess.run(
        ["git", "-C", str(pcb_dir), "diff", "--cached", "--quiet"],
        capture_output=True,
        text=True,
    )
    if diff_result.returncode == 0:
        # Exit code 0 means no staged changes
        print(
            "Error: --commit specified but the pipeline produced no file changes to commit.",
            file=sys.stderr,
        )
        return 1

    commit_msg = _build_commit_message(ctx, results, check_data=ctx._check_data)

    commit_result = subprocess.run(
        ["git", "-C", str(pcb_dir), "commit", "-m", commit_msg],
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        print(
            f"Error: git commit failed: {commit_result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    if not ctx.quiet:
        console.print(f"[green]Committed:[/green] {commit_msg}")

    return 0


# Map of step name to runner function
STEP_RUNNERS = {
    PipelineStep.ERC: _run_step_erc,
    PipelineStep.FIX_ERC: _run_step_fix_erc,
    PipelineStep.SYNC: _run_step_sync,
    PipelineStep.FIX_SILKSCREEN: _run_step_fix_silkscreen,
    PipelineStep.ROUTE: _run_step_route,
    PipelineStep.STITCH: _run_step_stitch,
    PipelineStep.FIX_VIAS: _run_step_fix_vias,
    PipelineStep.FIX_DRC: _run_step_fix_drc,
    PipelineStep.OPTIMIZE: _run_step_optimize,
    PipelineStep.ZONES: _run_step_zones,
    PipelineStep.ZONES_REFILL: _run_step_zones_refill,
    PipelineStep.AUDIT: _run_step_audit,
    PipelineStep.REPORT: _run_step_report,
    PipelineStep.EXPORT: _run_step_export,
}


def run_pipeline(
    ctx: PipelineContext, steps: list[PipelineStep] | None = None
) -> list[PipelineResult]:
    """Run the pipeline with the given context.

    Args:
        ctx: Pipeline execution context
        steps: Steps to run, or None for all steps

    Returns:
        List of PipelineResult for each step executed.
    """
    if steps is None:
        steps = list(ALL_STEPS)

    console = Console(quiet=ctx.quiet)
    results: list[PipelineResult] = []

    # Print pipeline header
    if not ctx.quiet:
        mode = "[dry-run] " if ctx.dry_run else ""
        console.print(
            Panel.fit(
                f"[bold]{mode}Pipeline:[/bold] {ctx.pcb_file.name}\n"
                f"[dim]Directory:[/dim] {ctx.pcb_file.parent}\n"
                f"[dim]Manufacturer:[/dim] {ctx.mfr} ({ctx.layers} layers)",
                title="kct pipeline",
            )
        )

    # Track whether the route step has failed (used by --best-effort logic)
    route_failed = False

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        disable=ctx.quiet,
    ) as progress:
        for i, step in enumerate(steps):
            runner = STEP_RUNNERS[step]
            task = progress.add_task(f"[cyan]{step.value}[/cyan]...", total=None)

            result = runner(ctx, console)
            results.append(result)

            progress.remove_task(task)

            # Print step result
            if not ctx.quiet:
                if result.skipped:
                    status = "[yellow]SKIP[/yellow]"
                elif result.warning:
                    status = "[yellow]WARN[/yellow]"
                elif result.success:
                    status = "[green]OK[/green]"
                else:
                    status = "[red]FAIL[/red]"
                console.print(f"  [{status}] {result.message}")

            # Stop on failure unless:
            # - it's the audit, report, or export step (always run informational steps), or
            # - ERC just failed and FIX_ERC is the next step (auto-remediation path), or
            # - --best-effort is set and route has failed (continue past routing
            #   failure and any downstream consequences to zone fill, DRC, audit,
            #   report, and export)
            if not result.success and step not in (
                PipelineStep.AUDIT,
                PipelineStep.REPORT,
                PipelineStep.EXPORT,
            ):
                next_step = steps[i + 1] if i + 1 < len(steps) else None
                if step == PipelineStep.ERC and next_step == PipelineStep.FIX_ERC:
                    pass  # allow ERC -> FIX_ERC auto-remediation
                elif step == PipelineStep.ROUTE and ctx.best_effort:
                    # Route failed in best-effort mode -- mark as warning
                    # and let downstream steps continue.
                    route_failed = True
                    result.warning = True
                    if not ctx.quiet:
                        console.print(
                            "  [yellow]--best-effort: continuing past routing failure[/yellow]"
                        )
                elif ctx.best_effort and route_failed:
                    # In best-effort mode after a route failure, downstream
                    # step failures are expected (incomplete routing causes
                    # DRC violations, zone fill issues, etc.).  Mark them as
                    # warnings and continue.
                    result.warning = True
                else:
                    break

    # Post-loop reclassification: when ERC failed but FIX_ERC resolved the errors,
    # reclassify ERC as a warning so the banner and exit code reflect success.
    erc_result = next((r for r in results if r.step == PipelineStep.ERC), None)
    fix_erc_result = next((r for r in results if r.step == PipelineStep.FIX_ERC), None)
    if (
        erc_result
        and not erc_result.success
        and fix_erc_result
        and fix_erc_result.success
        and not fix_erc_result.skipped
    ):
        erc_result.success = True
        erc_result.warning = True
        erc_result.message = erc_result.message.replace(
            "blocking error(s) found",
            "blocking error(s) found -> fixed by fix-erc",
        )

    # Print step-completion banner
    if not ctx.quiet:
        console.print()
        success_count = sum(1 for r in results if r.success)
        total_count = len(results)
        skipped_count = sum(1 for r in results if r.skipped)
        warning_count = sum(1 for r in results if r.warning)

        if success_count == total_count:
            skip_note = f", {skipped_count} skipped" if skipped_count else ""
            if warning_count > 0:
                console.print(
                    f"[yellow]Pipeline completed with warnings[/yellow] "
                    f"({success_count}/{total_count} steps{skip_note})"
                )
            else:
                console.print(
                    f"[green]Pipeline completed successfully[/green] "
                    f"({success_count}/{total_count} steps{skip_note})"
                )
        else:
            console.print(
                f"[red]Pipeline failed[/red] ({success_count}/{total_count} steps succeeded)"
            )

    # Print final DRC/ERC/verdict summary (full pipeline only)
    is_single_step = len(steps) == 1
    if not ctx.quiet and not is_single_step:
        if ctx.dry_run:
            # Dry-run: show placeholder summary without running kct check
            console.print()
            console.print("[bold]Summary:[/bold]")
            console.print("  DRC:        N/A (dry run)")
            console.print("  ERC:        N/A (dry run)")
            console.print("  Silkscreen: N/A (dry run)")
            console.print("  Sync:       N/A (dry run)")
            console.print("  Verdict:    N/A (dry run)")
        else:
            check_data = _fetch_check_results(ctx)
            audit_data = _fetch_audit_results(ctx)
            ctx._check_data = check_data  # cache for _build_commit_message
            _print_final_summary(
                ctx,
                results,
                console,
                check_data=check_data,
                audit_data=audit_data,
            )

    return results


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct pipeline command."""
    parser = argparse.ArgumentParser(
        prog="kct pipeline",
        description="End-to-end repair pipeline for existing PCBs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    kct pipeline board.kicad_pcb                      # Full pipeline with defaults
    kct pipeline board.kicad_pcb --mfr jlcpcb         # Target JLCPCB rules
    kct pipeline board.kicad_pcb --dry-run             # Preview steps
    kct pipeline board.kicad_pcb --step fix-vias       # Run single step
    kct pipeline project.kicad_pro --layers 4          # 4-layer project audit
    kct pipeline board.kicad_pcb --force               # Force re-route
    kct pipeline board.kicad_pcb --commit              # Commit changes after success
    kct pipeline board.kicad_pcb --mfr jlcpcb --commit # Pipeline + auto-commit
    kct pipeline board.kicad_pcb --best-effort         # Continue past routing failures
    kct pipeline board.kicad_pcb --no-cache            # Bypass routing cache
    kct pipeline board.kicad_pcb --clear-cache         # Clear cache before routing
        """,
    )

    parser.add_argument(
        "input",
        help="Path to .kicad_pcb or .kicad_pro file",
    )
    parser.add_argument(
        "--step",
        "-s",
        choices=[s.value for s in PipelineStep],
        default=None,
        help="Run only this step (default: run all steps in order)",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        default="jlcpcb",
        help="Target manufacturer (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        "-l",
        choices=["auto", "2", "4", "4-sig", "4-all", "6"],
        default=None,
        help=(
            "Layer stack configuration: "
            "'auto' = auto-detect from PCB (default when omitted); "
            "'2' = 2-layer; '4' = 4-layer with GND/PWR planes; "
            "'4-sig' = 4-layer with 2 signal + 1 ground plane; "
            "'4-all' = 4-layer all-signal; '6' = 6-layer"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview pipeline steps without modifying files",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed output from each step",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force all steps (e.g., re-route even if already routed)",
    )
    parser.add_argument(
        "--route-skip-threshold",
        type=float,
        default=95.0,
        metavar="PERCENT",
        help=(
            "Minimum signal-net completion percentage required to skip the "
            "route step (default: 95.0). Single-pad nets and zone-fillable "
            "plane nets are excluded from the signal-net subset and never "
            "block the skip."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="Create a git commit with the modified PCB file after a successful pipeline run",
    )
    parser.add_argument(
        "--best-effort",
        action="store_true",
        default=False,
        help=(
            "Continue past routing failures to zone fill, DRC, audit, report, and export. "
            "Exit code 2 indicates partial success (routing incomplete but downstream steps ran)"
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Bypass routing cache (force fresh routing in the route step)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        default=False,
        help="Clear routing cache before the route step runs",
    )
    parser.add_argument(
        "--max-displacement",
        type=float,
        default=2.0,
        help=(
            "Maximum nudge/slide distance in mm for fix-drc step (default: 2.0). "
            "Increase when enlarged vias cause segment-to-via violations that "
            "exceed the displacement budget."
        ),
    )
    parser.add_argument(
        "--sch",
        "--schematic",
        dest="sch",
        default=None,
        help="Path to root .kicad_sch file (overrides auto-discovery)",
    )
    parser.add_argument(
        "--apply-sync",
        action="store_true",
        default=False,
        help=(
            "In the sync step, auto-add missing footprints and apply"
            " high-confidence value/footprint corrections in place."
            " Without this flag, sync drift is blocking (use --force to"
            " continue past drift without modification)."
        ),
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input).resolve()

    if not input_path.exists():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1

    # Determine input type and resolve PCB file
    is_project = input_path.suffix == ".kicad_pro"
    project_file: Path | None = None

    if is_project:
        project_file = input_path
        pcb_file = _resolve_pcb_from_project(input_path)
        if pcb_file is None:
            print(
                f"Error: No .kicad_pcb file found for project {input_path.name}",
                file=sys.stderr,
            )
            return 1
    elif input_path.suffix == ".kicad_pcb":
        pcb_file = input_path
        # Check if a .kicad_pro exists alongside
        pro_file = input_path.with_suffix(".kicad_pro")
        if pro_file.exists():
            project_file = pro_file
            is_project = True
    else:
        print(
            f"Error: Unsupported file type: {input_path.suffix} "
            f"(expected .kicad_pcb or .kicad_pro)",
            file=sys.stderr,
        )
        return 1

    # Resolve layer configuration from PCB when not explicitly specified.
    # When --layers is given (e.g. "4", "4-sig"), use the string as-is so it
    # passes through to the route subprocess.  When omitted, auto-detect the
    # numeric layer count from the PCB file and store it as a string.
    if args.layers is not None:
        resolved_layers: str = args.layers
    else:
        try:
            from kicad_tools.schema.pcb import PCB

            pcb = PCB.load(pcb_file)
            detected = len(pcb.copper_layers)
            resolved_layers = str(detected) if detected > 0 else "2"
        except Exception:
            logger.warning(
                "Could not auto-detect layer count from %s; defaulting to 2",
                pcb_file.name,
            )
            resolved_layers = "2"

    # Resolve schematic file for ERC step
    if args.sch is not None:
        sch_path = Path(args.sch).resolve()
        if not sch_path.exists():
            print(f"Error: Schematic file not found: {sch_path}", file=sys.stderr)
            return 1
        schematic_file = sch_path
    else:
        schematic_file = _resolve_schematic(pcb_file, project_file)

    # Build context
    ctx = PipelineContext(
        pcb_file=pcb_file,
        project_file=project_file,
        schematic_file=schematic_file,
        mfr=args.mfr,
        layers=resolved_layers,
        dry_run=args.dry_run,
        verbose=args.verbose,
        quiet=args.quiet,
        force=args.force,
        is_project=is_project,
        commit=args.commit,
        best_effort=args.best_effort,
        no_cache=args.no_cache,
        clear_cache=args.clear_cache,
        max_displacement=args.max_displacement,
        apply_sync=args.apply_sync,
        route_skip_threshold=args.route_skip_threshold,
    )

    # Determine steps to run
    if args.step:
        steps = [PipelineStep(args.step)]
    else:
        steps = list(ALL_STEPS)

    results = run_pipeline(ctx, steps)

    # Determine exit code:
    #   0 = all steps succeeded
    #   2 = partial success (--best-effort continued past a routing failure)
    #   1 = hard failure
    all_succeeded = all(r.success for r in results)
    has_route_warning = any(
        r.step == PipelineStep.ROUTE and not r.success and r.warning for r in results
    )

    if not all_succeeded and not has_route_warning:
        return 1

    # Handle --commit flag (silently ignored with --dry-run)
    if ctx.commit and not ctx.dry_run:
        console = Console(quiet=ctx.quiet)
        commit_rc = _git_commit_result(ctx, results, console)
        if commit_rc != 0:
            return commit_rc

    # Exit code 2 for partial success (routing incomplete in best-effort mode)
    if has_route_warning:
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
