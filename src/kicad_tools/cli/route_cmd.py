"""
PCB autorouting CLI command.

Provides command-line access to the autorouter:

    kicad-tools route board.kicad_pcb
    kicad-tools route board.kicad_pcb -o board_routed.kicad_pcb
    kicad-tools route board.kicad_pcb --skip-nets GND,VCC --strategy negotiated

Performance Profiling:

    Use --profile to measure routing performance and identify bottlenecks:

    # Profile routing and save results
    kicad-tools route board.kicad_pcb --profile

    # Specify custom output file
    kicad-tools route board.kicad_pcb --profile --profile-output my_profile.prof

    # Analyze results with pstats
    python -m pstats route_profile.prof

    # Visualize with snakeviz (pip install snakeviz)
    snakeviz route_profile.prof

Layer Stack Configuration:

    By default, the autorouter uses a 2-layer configuration (F.Cu, B.Cu).
    For multi-layer boards, use the --layers option:

    # 4-layer board with GND/PWR planes (typical for Pi HAT, Arduino shields)
    kicad-tools route board.kicad_pcb --layers 4

    # 4-layer with 2 signal layers (for high-density routing)
    kicad-tools route board.kicad_pcb --layers 4-sig

    # 4-layer with all 4 signal layers (no planes, maximum routing resources)
    kicad-tools route board.kicad_pcb --layers 4-all

    # 6-layer with 4 signal layers
    kicad-tools route board.kicad_pcb --layers 6

    Layer stack configurations:
    - '2': F.Cu (signal), B.Cu (signal)
    - '4': F.Cu (signal), In1.Cu (GND plane), In2.Cu (PWR plane), B.Cu (signal)
    - '4-sig': F.Cu (signal), In1.Cu (signal), In2.Cu (GND plane), B.Cu (mixed)
    - '4-all': F.Cu (signal), In1.Cu (signal), In2.Cu (signal), B.Cu (signal)
    - '6': F.Cu, In1.Cu (GND), In2.Cu (signal), In3.Cu (signal), In4.Cu (PWR), B.Cu

    For 4-layer boards with inner planes (--layers 4), signals are routed on
    the outer layers (F.Cu and B.Cu) with vias providing layer transitions
    through the planes. This is the most common configuration for hobby/small
    production boards.
"""

import argparse
import logging
import math
import signal
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router import Autorouter, LayerStack

logger = logging.getLogger(__name__)


def _insert_sexp_before_closing(pcb_content: str, sexp_fragments: str) -> str:
    """Insert S-expression fragments before the final closing parenthesis of a PCB file.

    This correctly removes only the last closing parenthesis from the PCB content
    and re-adds it after the inserted fragments. Unlike ``rstrip(")")``, which
    strips ALL trailing ``)``, this function preserves the S-expression structure.

    Args:
        pcb_content: Original PCB file content.
        sexp_fragments: S-expression string(s) to insert (segments, vias, zones).

    Returns:
        Modified PCB content with fragments inserted before the final ``)``.
    """
    content = pcb_content.rstrip()
    if content.endswith(")"):
        content = content[:-1].rstrip()

    result = content + "\n\n"
    result += f"  {sexp_fragments}\n"
    result += ")\n"
    return result


def _validate_sexp_parentheses(content: str) -> bool:
    """Validate that S-expression parentheses are balanced.

    Scans the content respecting quoted strings (parentheses inside quotes
    are not counted). Returns True if parentheses are balanced.

    Args:
        content: S-expression content to validate.

    Returns:
        True if parentheses are balanced, False otherwise.
    """
    depth = 0
    in_string = False
    prev_char = ""
    for char in content:
        if char == '"' and prev_char != "\\":
            in_string = not in_string
        elif not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return False
        prev_char = char
    return depth == 0


def _finalize_routes(
    router: "Autorouter",
    multi_pad_net_ids: set[int],
    nets_to_route: int,
    quiet: bool = False,
) -> tuple[str, dict, dict]:
    """Run cleanup, compute statistics, and generate S-expressions.

    This is the single canonical sequence that must be followed whenever
    route output is produced.  The ordering is:

    1. ``cleanup_artifacts()`` -- mutates ``router.routes`` in place,
       removing net-0 orphans and out-of-bounds segments while preserving
       connectivity.
    2. ``to_sexp(skip_cleanup=True)`` -- serialize the (now clean) routes.
    3. ``get_statistics()`` -- compute metrics from the cleaned routes so
       they match what was written to disk.

    All four output paths in route_cmd.py (main CLI, layer escalation,
    rule relaxation, multi-strategy matrix) must use this helper to
    prevent the stats-before-cleanup bug from recurring.

    Args:
        router: The Autorouter instance with completed routes.
        multi_pad_net_ids: Set of net IDs with >= 2 pads (for accurate
            nets_routed counting per Issue #1643).
        nets_to_route: Total number of nets targeted for routing.
        quiet: Suppress console output.

    Returns:
        Tuple of (route_sexp, stats, cleanup_stats) where:
        - route_sexp: S-expression string for the cleaned routes.
        - stats: Post-cleanup statistics dict from ``get_statistics()``.
        - cleanup_stats: Dict returned by ``cleanup_artifacts()`` with
          keys like ``net0_routes_removed``, ``oob_segments_removed``,
          ``segments_restored``, etc.
    """
    from kicad_tools.cli.progress import flush_print

    # Step 1: Run connectivity-aware cleanup before computing statistics
    # so that metrics reflect the segments actually written to the output
    # file.  The cleanup is safe (it restores segments whose removal would
    # fragment a net).  See io.py for the canonical ordering.
    pre_cleanup_segments = sum(len(r.segments) for r in router.routes)
    pre_cleanup_vias = sum(len(r.vias) for r in router.routes)
    cleanup_stats = router.cleanup_artifacts()
    post_cleanup_segments = sum(len(r.segments) for r in router.routes)
    post_cleanup_vias = sum(len(r.vias) for r in router.routes)

    segments_removed = pre_cleanup_segments - post_cleanup_segments
    vias_removed = pre_cleanup_vias - post_cleanup_vias

    if not quiet and (segments_removed > 0 or vias_removed > 0):
        flush_print("\n--- Cleanup ---")
        flush_print(
            f"  Segments: {pre_cleanup_segments} -> {post_cleanup_segments} "
            f"({segments_removed} removed)"
        )
        if vias_removed > 0:
            flush_print(
                f"  Vias:     {pre_cleanup_vias} -> {post_cleanup_vias} "
                f"({vias_removed} removed)"
            )
        if cleanup_stats.get("segments_restored", 0) > 0:
            flush_print(
                f"  Restored: {cleanup_stats['segments_restored']} segments, "
                f"{cleanup_stats.get('vias_restored', 0)} vias (connectivity preservation)"
            )

    # Step 2: Generate S-expressions from the cleaned routes
    route_sexp = router.to_sexp(skip_cleanup=True)

    # Step 3: Compute statistics from the cleaned routes
    stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)

    if not quiet:
        flush_print("\n--- Results ---")
        flush_print(f"  Routes created:  {stats['routes']}")
        flush_print(f"  Segments:        {stats['segments']}")
        flush_print(f"  Vias:            {stats['vias']}")
        flush_print(f"  Total length:    {stats['total_length_mm']:.2f}mm")
        flush_print(f"  Nets routed:     {stats['nets_routed']}/{nets_to_route}")

    return route_sexp, stats, cleanup_stats


# Global state for Ctrl+C handling
_interrupt_state = {
    "interrupted": False,
    "router": None,
    "output_path": None,
    "pcb_path": None,
    "quiet": False,
    "best_completed_attempt": False,
}


def _handle_interrupt(signum, frame):
    """Handle Ctrl+C by setting the interrupted flag and saving partial results."""
    _interrupt_state["interrupted"] = True
    if not _interrupt_state["quiet"]:
        print("\n\n⚠ Interrupt received! Saving partial results...")
    # Save partial results immediately
    saved = _save_partial_results()
    # Exit with code 5 to indicate SIGINT interruption with saved partial results.
    # This is distinct from code 2 (partial routing below threshold) so scripts can
    # distinguish user-interrupted from router-decided-partial.
    sys.exit(5 if saved else 130)  # 130 = 128 + SIGINT (2)


def _save_partial_results() -> bool:
    """Save partial routing results if interrupted.

    Returns:
        True if partial results were saved, False otherwise.
    """
    router = _interrupt_state["router"]
    output_path = _interrupt_state["output_path"]
    pcb_path = _interrupt_state["pcb_path"]
    quiet = _interrupt_state["quiet"]

    if router is None or output_path is None or pcb_path is None:
        return False

    if not router.routes:
        if not quiet:
            print("  No routes to save.")
        return False

    try:
        # Read original PCB content
        original_content = pcb_path.read_text()

        # Get partial route S-expressions
        route_sexp = router.to_sexp()

        if route_sexp:
            # When the interrupt state holds a best *completed* attempt from
            # adaptive-rules routing, write to the main output path (not
            # _partial) because the result is a full routing pass.
            if _interrupt_state.get("best_completed_attempt"):
                save_path = output_path
            else:
                save_path = output_path.with_stem(output_path.stem + "_partial")

            # Insert routes before final closing parenthesis
            output_content = _insert_sexp_before_closing(original_content, route_sexp)

            save_path.write_text(output_content)

            if not quiet:
                stats = router.get_statistics()
                print(f"\n  Partial results saved to: {save_path}")
                print(f"    Nets routed: {stats['nets_routed']}")
                print(f"    Segments: {stats['segments']}")
                print(f"    Vias: {stats['vias']}")
            return True
    except Exception as e:
        if not quiet:
            print(f"  Error saving partial results: {e}")

    return False


def _export_failed_nets(
    router: "Autorouter",
    net_map: dict[str, int],
    export_path: str,
    quiet: bool = False,
    nets_to_route_ids: set[int] | None = None,
) -> bool:
    """Export the list of failed (unrouted) net names to a file.

    Writes one net name per line to the specified path.

    Args:
        router: The Autorouter instance with completed routing.
        net_map: Mapping of net names to net IDs.
        export_path: File path to write the failed net names.
        quiet: If True, suppress output messages.
        nets_to_route_ids: Optional set of net IDs targeted for routing
            (multi-pad signal nets).  When provided, only nets in this set
            are considered candidates so single-pad and power nets are
            excluded from the export.

    Returns:
        True if the file was written successfully, False otherwise.
    """
    reverse_net = {v: k for k, v in net_map.items() if v > 0}
    routed_net_ids = {route.net for route in router.routes}
    if nets_to_route_ids is not None:
        unrouted_ids = nets_to_route_ids - routed_net_ids
    else:
        all_net_ids = {v for k, v in net_map.items() if v > 0}
        unrouted_ids = all_net_ids - routed_net_ids

    if not unrouted_ids:
        if not quiet:
            print("  No failed nets to export.")
        return False

    try:
        failed_names = sorted(reverse_net.get(nid, f"Net_{nid}") for nid in unrouted_ids)
        export_file = Path(export_path)
        export_file.write_text("\n".join(failed_names) + "\n")
        if not quiet:
            print(f"  Failed nets exported to: {export_file} ({len(failed_names)} nets)")
        return True
    except Exception as e:
        if not quiet:
            print(f"  Error exporting failed nets: {e}")
        return False


def show_preview(
    router,
    net_map: dict[str, int],
    nets_to_route: int,
    quiet: bool = False,
    nets_to_route_ids: set[int] | None = None,
) -> str:
    """Display routing preview with per-net breakdown.

    Args:
        router: The Autorouter instance with completed routes
        net_map: Mapping of net names to net IDs
        nets_to_route: Total number of nets expected to be routed
        quiet: If True, skip interactive prompt and return 'n'
        nets_to_route_ids: Optional set of net IDs targeted for routing.
            When provided, ``nets_routed`` only counts nets in this set.

    Returns:
        User response: 'y' (apply), 'n' (reject), or 'e' (edit - future)
    """
    # Build reverse mapping: net_id -> net_name
    reverse_net = {v: k for k, v in net_map.items()}

    # Collect per-net statistics
    net_stats: dict[int, dict] = {}
    for route in router.routes:
        net_id = route.net
        if net_id not in net_stats:
            net_stats[net_id] = {
                "net_name": route.net_name or reverse_net.get(net_id, f"Net {net_id}"),
                "segments": 0,
                "vias": 0,
                "length": 0.0,
                "layers": set(),
            }
        stats = net_stats[net_id]
        stats["segments"] += len(route.segments)
        stats["vias"] += len(route.vias)
        for seg in route.segments:
            dx = seg.x2 - seg.x1
            dy = seg.y2 - seg.y1
            stats["length"] += math.sqrt(dx * dx + dy * dy)
            stats["layers"].add(seg.layer.kicad_name)

    # Identify unrouted nets — filter to target population so the
    # "No path found" list only shows actual routing candidates,
    # not skipped power nets or single-pad nets (Issue #1833).
    routed_net_ids = set(net_stats.keys())
    if nets_to_route_ids is not None:
        unrouted_ids = nets_to_route_ids - routed_net_ids
    else:
        all_net_ids = {v for k, v in net_map.items() if v > 0}
        unrouted_ids = all_net_ids - routed_net_ids

    # Print header
    print("\n" + "=" * 60)
    print("ROUTING PREVIEW")
    print("=" * 60)

    # Print per-net breakdown
    for net_id in sorted(net_stats.keys()):
        stats = net_stats[net_id]
        net_name = stats["net_name"]
        layers = " -> ".join(sorted(stats["layers"]))
        via_info = f", {stats['vias']} via(s)" if stats["vias"] > 0 else ""

        print(f"\nNet: {net_name}")
        print(f"  Layers:   {layers}")
        print(f"  Length:   {stats['length']:.2f}mm")
        print(f"  Segments: {stats['segments']}{via_info}")
        print("  Status:   \u2713 Routed")

    # Show unrouted nets
    if unrouted_ids:
        print("\n" + "-" * 40)
        for net_id in sorted(unrouted_ids):
            net_name = reverse_net.get(net_id, f"Net {net_id}")
            if net_name:  # Skip empty net names
                print(f"\nNet: {net_name}")
                print("  Status:   \u2717 No path found")

    # Summary statistics — filter to target population (Issue #1643)
    overall_stats = router.get_statistics(nets_to_route_ids=nets_to_route_ids)
    nets_routed = overall_stats["nets_routed"]
    success_rate = (nets_routed / nets_to_route * 100) if nets_to_route > 0 else 0

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Nets routed:  {nets_routed}/{nets_to_route} ({success_rate:.0f}%)")
    print(f"  Total length: {overall_stats['total_length_mm']:.2f}mm")
    print(f"  Total vias:   {overall_stats['vias']}")
    print(f"  Segments:     {overall_stats['segments']}")

    # Layer usage summary
    all_layers: dict[str, int] = {}
    for route in router.routes:
        for seg in route.segments:
            layer_name = seg.layer.kicad_name
            all_layers[layer_name] = all_layers.get(layer_name, 0) + 1

    if all_layers:
        print("\n  Layer usage:")
        for layer_name, count in sorted(all_layers.items()):
            print(f"    {layer_name}: {count} segments")

    print("=" * 60)

    # Interactive prompt (unless quiet mode)
    if quiet:
        return "n"

    print("\nApply routes? [y/N/e(dit)]:", end=" ")
    try:
        response = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return "n"

    if response in ("y", "yes"):
        return "y"
    elif response in ("e", "edit"):
        print("  (Edit mode not yet implemented - treating as reject)")
        return "n"
    else:
        return "n"


def run_post_route_drc(
    output_path: Path,
    manufacturer: str,
    layers: int,
    quiet: bool = False,
) -> tuple[int, int]:
    """Run DRC validation on the routed PCB.

    Args:
        output_path: Path to the routed PCB file
        manufacturer: Manufacturer profile for DRC rules (e.g., "jlcpcb")
        layers: Number of PCB layers
        quiet: If True, suppress output

    Returns:
        Tuple of (error_count, warning_count)
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    try:
        # Load the routed PCB
        pcb = PCB.load(str(output_path))

        # Run DRC
        checker = DRCChecker(pcb, manufacturer=manufacturer, layers=layers)
        results = checker.check_all()

        error_count = results.error_count
        warning_count = results.warning_count

        if not quiet:
            print("\n--- DRC Validation ---")
            if error_count == 0 and warning_count == 0:
                print(f"  DRC PASSED ({manufacturer} profile, {layers} layers)")
            else:
                if error_count > 0:
                    print(f"  Errors:   {error_count}")
                if warning_count > 0:
                    print(f"  Warnings: {warning_count}")

                # Show first few violations
                shown = 0
                for v in results.errors[:5]:
                    location = (
                        f" at ({v.location[0]:.2f}, {v.location[1]:.2f})" if v.location else ""
                    )
                    print(f"    - {v.rule_id}: {v.message}{location}")
                    shown += 1
                if error_count > 5:
                    print(f"    ... and {error_count - 5} more errors")

                if warning_count > 0 and shown < 5:
                    for v in results.warnings[: 5 - shown]:
                        location = (
                            f" at ({v.location[0]:.2f}, {v.location[1]:.2f})" if v.location else ""
                        )
                        print(f"    - {v.rule_id}: {v.message}{location}")
                    if warning_count > (5 - shown):
                        print(f"    ... and {warning_count - (5 - shown)} more warnings")

                print(f"\n  Run 'kct check {output_path} --mfr {manufacturer}' for full details")
                if error_count > 0:
                    print(f"  Run 'kct fix-drc {output_path}' to auto-repair clearance violations")

        return error_count, warning_count

    except Exception as e:
        if not quiet:
            print("\n--- DRC Validation ---")
            print(f"  Warning: DRC check failed: {e}")
        return -1, -1  # Indicate failure to run DRC


def _run_auto_fix(
    output_path: Path,
    max_passes: int = 1,
    quiet: bool = False,
) -> int:
    """Run fix-drc on the routed PCB to auto-repair DRC violations.

    Args:
        output_path: Path to the routed PCB file to repair.
        max_passes: Number of iterative repair passes.
        quiet: If True, suppress output.

    Returns:
        Exit code from fix_drc_cmd.main() (0 = all violations fixed).
    """
    from kicad_tools.cli.fix_drc_cmd import main as fix_drc_main

    if not quiet:
        print("\n--- Auto-Fix DRC Violations ---")

    fix_argv = [
        str(output_path),
        "--max-passes",
        str(max_passes),
        "--max-displacement",
        "2.0",
        "--local-reroute",
    ]
    if quiet:
        fix_argv.append("--quiet")

    result = fix_drc_main(fix_argv)

    if not quiet:
        if result == 0:
            print("  Auto-fix: all targeted violations repaired!")
        else:
            print("  Auto-fix: some violations remain (manual repair may be needed)")

    return result


def _should_auto_fix(args) -> bool:
    """Determine whether auto-fix should run based on CLI flags.

    Auto-fix runs when --auto-fix is set (which is also implied by
    --auto-fix-passes), but is suppressed by --dry-run and --skip-drc.
    """
    auto_fix = getattr(args, "auto_fix", False)
    dry_run = getattr(args, "dry_run", False)
    skip_drc = getattr(args, "skip_drc", False)

    if not auto_fix:
        return False
    if dry_run or skip_drc:
        return False
    return True


@dataclass
class LayerEscalationResult:
    """Result of a layer escalation routing attempt."""

    layer_count: int
    layer_stack: "LayerStack"
    router: "Autorouter"
    net_map: dict
    nets_routed: int
    nets_to_route: int
    completion: float
    success: bool
    stats: dict | None = None
    overflow: int = 0


@dataclass
class RuleRelaxationResult:
    """Result of a rule relaxation routing attempt."""

    tier: int
    trace_width: float
    clearance: float
    via_drill: float
    via_diameter: float
    tier_description: str
    router: "Autorouter"
    net_map: dict
    nets_routed: int
    nets_to_route: int
    completion: float
    success: bool
    layer_count: int = 2  # May be set by layer escalation integration
    stats: dict | None = None


def _is_better_result(
    candidate: "LayerEscalationResult | RuleRelaxationResult",
    best: "LayerEscalationResult | RuleRelaxationResult",
) -> bool:
    """Compare routing results with tiebreaking on connectivity metrics.

    Issue #2396: The primary comparison uses **absolute nets_routed** rather
    than completion ratio.  When ``nets_to_route`` differs across escalation
    attempts (e.g. power nets auto-skipped on 4L but not 2L), comparing
    ratios produces misleading results: 6/10 (0.60) vs 3/8 (0.375) looks
    like a clear win for 2L, but the raw ratio comparison used to use
    ``completion`` which could disagree when denominators differ.  Using
    absolute counts ensures we always keep the attempt that routed the most
    nets, breaking ties by completion ratio, then segments, vias, and layer
    count.
    """
    # Primary: absolute nets routed (cross-denominator safe)
    if candidate.nets_routed != best.nets_routed:
        return candidate.nets_routed > best.nets_routed

    # Tied on absolute count: use completion ratio as tiebreaker
    if candidate.completion != best.completion:
        return candidate.completion > best.completion

    # Tie on completion -- use stats-based tiebreakers (Issue #2397)
    c_stats = candidate.stats or {}
    b_stats = best.stats or {}

    c_segments = c_stats.get("segments", 0)
    b_segments = b_stats.get("segments", 0)
    if c_segments != b_segments:
        return c_segments > b_segments

    c_vias = c_stats.get("vias", 0)
    b_vias = b_stats.get("vias", 0)
    if c_vias != b_vias:
        return c_vias > b_vias

    # Still tied: prefer fewer layers (simpler board)
    return candidate.layer_count < best.layer_count


def update_pcb_layer_stackup(pcb_content: str, target_layers: int) -> str:
    """Update PCB content to have the specified number of copper layers.

    Args:
        pcb_content: Original PCB file content
        target_layers: Target number of copper layers (2, 4, or 6)

    Returns:
        Updated PCB content with correct layer definitions
    """
    import re

    # Layer definitions for different stackups
    layer_defs = {
        2: [
            '(0 "F.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
        4: [
            '(0 "F.Cu" signal)',
            '(1 "In1.Cu" signal)',
            '(2 "In2.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
        6: [
            '(0 "F.Cu" signal)',
            '(1 "In1.Cu" signal)',
            '(2 "In2.Cu" signal)',
            '(3 "In3.Cu" signal)',
            '(4 "In4.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
    }

    if target_layers not in layer_defs:
        return pcb_content

    # Check if we need to update — count ALL copper layers regardless of type
    # (signal, power, mixed, etc.) not just those marked "signal"
    current_layers = len(re.findall(r'\(\d+\s+"[^"]*\.Cu"\s+\w+', pcb_content))
    if current_layers >= target_layers:
        return pcb_content

    # Match the entire (layers ...) block including all inner entries.
    # Each inner entry is e.g. (0 "F.Cu" signal) or (44 "Edge.Cuts" user "Edge.Cuts").
    # The pattern matches from "(layers" through each "(...)" entry to the
    # block-closing ")".
    layers_pattern = re.compile(
        r'\(layers\s*\n(\s+\(\d+\s+"[^"]+"\s+\w+[^)]*\)\s*\n)+\s*\)',
        re.MULTILINE,
    )

    # Non-copper layer entry pattern (e.g. B.SilkS, Edge.Cuts, F.Fab)
    non_copper_re = re.compile(
        r'(\s*\(\d+\s+"(?!.*\.Cu")[^"]+"\s+\w+[^)]*\))',
    )

    def replace_layers(match):
        matched_text = match.group(0)
        # Extract non-copper layer entries from the original block
        non_copper_entries = non_copper_re.findall(matched_text)
        # Build new layers content with copper layers
        new_layers = "\n    ".join(layer_defs[target_layers])
        # Append non-copper layers after copper layers
        if non_copper_entries:
            non_copper_lines = "\n".join(
                entry.strip() for entry in non_copper_entries
            )
            return f"(layers\n    {new_layers}\n    {non_copper_lines}\n  )"
        return f"(layers\n    {new_layers}\n  )"

    new_content = layers_pattern.sub(replace_layers, pcb_content)

    # Validate output has balanced parentheses to catch regressions early
    if not _validate_sexp_parentheses(new_content):
        import warnings

        warnings.warn(
            "update_pcb_layer_stackup produced unbalanced parentheses; "
            "returning original content unchanged",
            stacklevel=2,
        )
        return pcb_content

    return new_content


def _print_power_stall_suggestions(
    stalled_nets: list[str],
    layer_count: int,
    pcb_arg: str,
) -> None:
    """Print actionable suggestions for a power-net stall (Issue #2388).

    Surfaces concrete remediation flags naming the stalled nets so users
    can pick the appropriate workaround instead of being told the router
    timed out.

    Args:
        stalled_nets: Names of the power/pour nets that stalled.
        layer_count: Layer count used for the failing attempt.
        pcb_arg: The original PCB argument the user passed (for echoing
            in suggested commands).
    """
    if not stalled_nets:
        return
    nets_csv = ", ".join(stalled_nets)
    # Default zone-layer assignment: GND on B.Cu, others on F.Cu.
    pour_assignments = []
    for n in stalled_nets:
        if n.upper() in {"GND", "VSS", "AGND", "DGND", "PGND", "GROUND"}:
            pour_assignments.append(f"{n}:B.Cu")
        else:
            pour_assignments.append(f"{n}:F.Cu")
    pour_arg = ",".join(pour_assignments)

    print()
    print(
        f"Routing did not complete: {nets_csv} could not be routed on "
        f"{layer_count} layer(s)."
    )
    print("Suggestions:")
    print(f"  1. Add copper zones for power nets:  --power-nets \"{pour_arg}\"")
    print("  2. Increase layer count:              --layers 4 (or --max-layers 6)")
    print(
        f"  3. Manual routing in KiCad for the {len(stalled_nets)} remaining net(s)"
    )
    print()


def _auto_skip_pour_nets(
    pcb_path: Path,
    skip_nets: list[str],
    quiet: bool = False,
) -> tuple[list[str], list[str]]:
    """Detect pour nets in the PCB and add them to the skip list.

    Reads net definitions from the PCB file and classifies them.  Nets
    identified as pour nets (GND, power rails, etc.) are appended to
    *skip_nets* so the router excludes them -- they will be connected
    via zone fill instead of traces.

    Args:
        pcb_path: Path to the .kicad_pcb file.
        skip_nets: Mutable list of net names already marked for skipping
            (e.g. from ``--skip-nets`` CLI flag).  Modified in place.
        quiet: Suppress informational output.

    Returns:
        Tuple of (auto_skipped, no_zone_nets):
        - auto_skipped: Net names that were auto-skipped (have zones).
        - no_zone_nets: Pour net names that lack zones and must be
          routed as signals.  Pass these to the autorouter's
          ``_pour_nets_without_zones`` attribute so that
          ``_filter_pour_nets()`` does not re-skip them (Issue #1841).
    """
    try:
        import re as _re

        from kicad_tools.router.net_class import classify_and_apply_rules

        pcb_text = pcb_path.read_text()
        net_names: dict[int, str] = {}
        for m in _re.finditer(r'\(net\s+(\d+)\s+"([^"]+)"\)', pcb_text):
            net_num, name = int(m.group(1)), m.group(2)
            if net_num > 0:
                net_names[net_num] = name

        if net_names:
            net_class_map = classify_and_apply_rules(net_names)
            # Only auto-skip pour nets that actually have zones in the PCB.
            # Nets classified as pour by name (e.g. +5V) but without a zone
            # must still be routed as signals.
            nets_with_zones: set[str] = set()
            # Match traditional KiCad 7/8 format: (zone ... (net_name "GND") ...)
            for zm in _re.finditer(
                r'\(zone\s+.*?\(net_name\s+"([^"]+)"\)',
                pcb_text,
                _re.DOTALL,
            ):
                nets_with_zones.add(zm.group(1))
            # Match KiCad 9 name-only format: (zone ... (net "GND") ...)
            for zm in _re.finditer(r'\(zone\s[^)]*\(net\s+"([^"]+)"\)', pcb_text):
                nets_with_zones.add(zm.group(1))
            del pcb_text  # free memory

            auto_skip = [
                name
                for name, routing in net_class_map.items()
                if routing.is_pour_net and name not in skip_nets and name in nets_with_zones
            ]
            if auto_skip:
                skip_nets.extend(auto_skip)
                if not quiet:
                    print(
                        f"Auto-skip: {', '.join(sorted(auto_skip))} (pour nets \u2014 use zone fill)"
                    )
            # Warn about pour nets without zones
            no_zone = [
                name
                for name, routing in net_class_map.items()
                if routing.is_pour_net and name not in skip_nets and name not in nets_with_zones
            ]
            if no_zone and not quiet:
                print(f"Routing: {', '.join(sorted(no_zone))} (power nets without zones)")
            return auto_skip, no_zone
    except Exception:
        pass  # Fall back to user-supplied skip_nets only
    return [], []


def route_with_layer_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with automatic layer escalation.

    Tries routing at 2, 4, and 6 layers until success or max is reached.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import flush_print, spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        is_cpp_available,
        load_pcb_for_routing,
        show_routing_summary,
    )

    # Handle backend selection
    force_python = False
    if args.backend == "cpp":
        if not is_cpp_available():
            print(
                "Error: C++ backend requested but not available.\n"
                "Build the C++ extension or use --backend auto/python.\n"
                "See README for build instructions.",
                file=sys.stderr,
            )
            return 1
    elif args.backend == "python":
        force_python = True

    # Warn prominently when auto-backend falls back to Python
    if args.backend == "auto" and not is_cpp_available() and not quiet:
        flush_print("WARNING: C++ router backend not installed -- using Python (10-100x slower).")
        flush_print("  Build it now:  kct build-native")
        flush_print("  Check status:  kct build-native --check")
        flush_print()

    # Configure design rules
    fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
    rules = DesignRules(
        grid_resolution=args.grid,
        trace_width=args.trace_width,
        trace_clearance=args.clearance,
        via_drill=args.via_drill,
        via_diameter=args.via_diameter,
        fine_pitch_clearance=fine_pitch_cl,
    )

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Auto-create copper pours for power nets (before skip detection)
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        auto_pour_if_missing(pcb_path, quiet=quiet)

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=quiet)

    # Layer stacks to try (in escalation order)
    layer_configs = [
        (2, LayerStack.two_layer()),
        (4, LayerStack.four_layer_sig_gnd_pwr_sig()),
        (4, LayerStack.four_layer_all_signal()),
        (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
    ]

    # Filter by max_layers
    layer_configs = [(n, s) for n, s in layer_configs if n <= args.max_layers]

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Layer Escalation Mode")
        flush_print("=" * 60)
        flush_print(f"Input:          {pcb_path}")
        flush_print(f"Output:         {output_path}")
        flush_print(f"Strategy:       {args.strategy}")
        flush_print(f"Max layers:     {args.max_layers}")
        flush_print(f"Min completion: {args.min_completion * 100:.0f}%")
        if skip_nets:
            flush_print(f"Skip:           {', '.join(skip_nets)}")
        flush_print()

    best_result: LayerEscalationResult | None = None
    successful_result: LayerEscalationResult | None = None

    # Issue #2412: Track previous attempt metrics for early termination
    prev_nets_routed: int | None = None
    prev_overflow: int | None = None

    # Issue #2388: Track power-net stall across escalation attempts.  When
    # a 2-layer attempt aborts due to power-net stall, the next attempt
    # is biased toward a stack with dedicated planes for those nets, and
    # we auto-extend skip_nets with the plane nets so the router relies
    # on the plane connections instead of routing power as signals.
    last_power_stall_nets: list[str] = []

    for attempt_num, (layer_count, layer_stack) in enumerate(layer_configs, 1):
        # Issue #2388: When the previous attempt stalled on power nets and
        # this stack provides dedicated planes for them, auto-skip those
        # plane nets so the router doesn't try to route them as signals.
        attempt_skip_nets = list(skip_nets)
        plane_nets_in_stack = {
            lyr.plane_net for lyr in layer_stack.plane_layers if lyr.plane_net
        }
        if last_power_stall_nets and plane_nets_in_stack:
            auto_plane_skip = [
                n for n in last_power_stall_nets
                if n in plane_nets_in_stack and n not in attempt_skip_nets
            ]
            if auto_plane_skip:
                attempt_skip_nets.extend(auto_plane_skip)
                if not quiet:
                    flush_print(
                        f"  Auto-skipping {', '.join(auto_plane_skip)} "
                        "(connected via dedicated plane(s) in this stack)"
                    )

        if not quiet:
            flush_print("=" * 60)
            flush_print(f"Attempt {attempt_num}: {layer_count} layers ({layer_stack.name})")
            flush_print("=" * 60)

        # Load PCB with this layer stack
        try:
            with spinner(f"Loading PCB ({layer_count} layers)...", quiet=quiet):
                router, net_map = load_pcb_for_routing(
                    str(pcb_path),
                    skip_nets=attempt_skip_nets,
                    rules=rules,
                    edge_clearance=args.edge_clearance,
                    layer_stack=layer_stack,
                    force_python=force_python,
                    validate_drc=not args.force,
                    strict_drc=False,
                )
        except Exception as e:
            if not quiet:
                print(f"  Error loading PCB: {e}")
            continue

        # Issue #2396: Ensure pristine per-attempt state.  Today this is a
        # no-op (load_pcb_for_routing creates a fresh Autorouter) but it
        # documents the contract and prevents silent regression if future
        # refactors reuse an Autorouter across attempts.
        router.reset_attempt_state()

        # Issue #1841: Tell the autorouter which pour nets lack zones
        router._pour_nets_without_zones = set(_no_zone)

        # Count nets to route
        multi_pad_nets = [
            net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) >= 2
        ]
        nets_to_route = len(multi_pad_nets)

        if not quiet:
            flush_print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
            flush_print(f"  Nets to route: {nets_to_route}")

        # Route
        if not quiet:
            flush_print(f"\n  Routing ({args.strategy})...")

        escape_flag = _resolve_escape_routing_flag(args)

        try:
            if _should_use_escape_routing(router, escape_flag, quiet):
                router.route_with_escape(
                    use_negotiated=(args.strategy == "negotiated"),
                    timeout=args.timeout,
                )
            elif getattr(args, "multi_resolution", False):
                router.route_all_multi_resolution(
                    use_negotiated=(args.strategy == "negotiated"),
                    max_iterations=args.iterations,
                    timeout=args.timeout,
                )
            elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                router.route_all_two_phase(
                    use_negotiated=True,
                    corridor_width_factor=2.0,
                    timeout=args.timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                )
            elif args.strategy == "negotiated":
                router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=args.timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    batch_routing=getattr(args, "batch_routing", False)
                    or getattr(args, "high_performance", False),
                    hierarchical=getattr(args, "hierarchical", False),
                    perturbation=getattr(args, "perturbation", True),
                )
            elif args.strategy == "basic":
                router.route_all()
            elif args.strategy == "monte-carlo":
                router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
        except Exception as e:
            if not quiet:
                print(f"  Routing error: {e}")
            continue

        # Calculate completion — filter to multi-pad nets only (Issue #1643)
        multi_pad_net_ids = set(multi_pad_nets)
        stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)
        nets_routed = stats["nets_routed"]
        completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0

        # Issue #2412: Capture overflow for early termination detection
        overflow = int(router.grid.get_total_overflow())

        # Create result
        result = LayerEscalationResult(
            layer_count=layer_count,
            layer_stack=layer_stack,
            router=router,
            net_map=net_map,
            nets_routed=nets_routed,
            nets_to_route=nets_to_route,
            completion=completion,
            success=completion >= args.min_completion,
            stats=stats,
            overflow=overflow,
        )

        # Track best result (Issue #2396: absolute nets_routed comparison)
        if best_result is None or _is_better_result(result, best_result):
            best_result = result
            if not quiet:
                flush_print(
                    f"  Best result so far: {best_result.layer_count}L with "
                    f"{best_result.nets_routed}/{best_result.nets_to_route} "
                    f"({best_result.completion:.0%})"
                )

        # Issue #2412: Early termination — zero overflow means failures
        # are placement/topology issues, not congestion.  Adding layers
        # cannot help when there is no congestion to relieve.
        if overflow == 0 and nets_routed < nets_to_route:
            if not quiet:
                flush_print(
                    "  Escalation stopped: failures are not congestion-related (overflow=0)"
                )
            # Report attempt result before breaking
            if not quiet:
                flush_print(
                    f"\n  Routed: {nets_routed}/{nets_to_route} nets "
                    f"({completion * 100:.0f}%)"
                )
                flush_print("  Status: INSUFFICIENT - early stop (zero overflow)")
            break

        # Issue #2412: Early termination — stagnation detection.  If adding
        # layers did not improve nets_routed or reduce overflow, further
        # escalation is unlikely to help.
        if (
            prev_nets_routed is not None
            and nets_routed <= prev_nets_routed
            and overflow >= prev_overflow
        ):
            if not quiet:
                flush_print("  Escalation stopped: no improvement after adding layers")
            # Report attempt result before breaking
            if not quiet:
                flush_print(
                    f"\n  Routed: {nets_routed}/{nets_to_route} nets "
                    f"({completion * 100:.0f}%)"
                )
                flush_print("  Status: INSUFFICIENT - early stop (stagnation)")
            break

        prev_nets_routed = nets_routed
        prev_overflow = overflow

        # Issue #2388: Record any power-net stall for the next attempt's
        # bias logic.  ``power_stall_nets`` is populated by
        # ``route_all_negotiated`` when the early-abort heuristic fires.
        if getattr(router, "power_stall_abort", False):
            last_power_stall_nets = list(getattr(router, "power_stall_nets", []))
            if not quiet and last_power_stall_nets:
                flush_print(
                    f"  Power-net stall on this attempt: "
                    f"{', '.join(last_power_stall_nets)}"
                )
        else:
            last_power_stall_nets = []

        # Report attempt result
        status = "SUCCESS" if result.success else "INSUFFICIENT - escalating"
        if not quiet:
            flush_print(f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)")
            flush_print(f"  Status: {status}")

        # Check for success
        if result.success:
            successful_result = result
            break

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("LAYER ESCALATION SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Design routed successfully on {final_result.layer_count} layers "
                f"({final_result.completion * 100:.0f}% completion)"
            )
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result on {final_result.layer_count} layers "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"on any layer count (max: {args.max_layers})"
            )
            # Issue #2388: Surface actionable suggestions when escalation
            # exhausted because of a power-net stall.
            if last_power_stall_nets:
                _print_power_stall_suggestions(
                    last_power_stall_nets,
                    final_result.layer_count,
                    args.pcb,
                )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
            if last_power_stall_nets:
                _print_power_stall_suggestions(
                    last_power_stall_nets,
                    args.max_layers,
                    args.pcb,
                )
        return 1

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.  This prevents the
        # optimizer from fragmenting routes through overused cells.
        has_overflow = final_result.router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(
            final_result.router.grid, ignore_overflow=has_overflow
        )
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        with spinner("Optimizing traces...", quiet=quiet):
            optimized_routes = []
            for route in final_result.router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            final_result.router.routes = optimized_routes

    # Post-optimization DRC nudge pass
    if final_result.router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(final_result.router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    _final_multi_pad_ids = {
        n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
    }
    route_sexp, final_stats, _cleanup_stats = _finalize_routes(
        final_result.router,
        _final_multi_pad_ids,
        final_result.nets_to_route,
        quiet=quiet,
    )
    # Update result with post-cleanup stats
    final_result.nets_routed = final_stats["nets_routed"]
    final_result.completion = (
        final_result.nets_routed / final_result.nets_to_route
        if final_result.nets_to_route > 0
        else 1.0
    )
    final_result.success = final_result.completion >= args.min_completion

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        # Read original PCB content
        original_content = pcb_path.read_text()

        # Update layer stackup if we escalated
        if final_result.layer_count > 2:
            original_content = update_pcb_layer_stackup(original_content, final_result.layer_count)

        # Insert routes before final closing parenthesis
        if route_sexp:
            output_content = _insert_sexp_before_closing(original_content, route_sexp)
        else:
            output_content = original_content
            if not quiet:
                print("  Warning: No routes generated!")

        # Validate S-expression structure before writing
        if not _validate_sexp_parentheses(output_content):
            logger.error("Generated PCB file has unbalanced parentheses")
            raise ValueError(
                "Generated PCB file has invalid S-expression syntax "
                "(unbalanced parentheses). This is a bug in kicad-tools. "
                "Please report it."
            )

        # Update output filename to include layer count
        if final_result.layer_count > 2:
            output_path = output_path.with_stem(
                output_path.stem + f"_{final_result.layer_count}layer"
            )

        output_path.write_text(output_content)

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Layer count: {final_result.layer_count}")

    # Run DRC validation unless skipped
    if not args.skip_drc and final_result.nets_routed > 0:
        drc_errors, _ = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
            )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print(f"SUCCESS: Design requires minimum {final_result.layer_count} layers")
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"on {final_result.layer_count} layers"
            )
            _multi_pad_ids = {
                n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
            }
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
                current_strategy=args.strategy,
                pcb_file=args.pcb,
                nets_to_route_ids=_multi_pad_ids,
                single_pad_count=getattr(final_result, "single_pad_count", 0),
            )

    if final_result.success:
        return 0
    # Partial routing: some nets were routed but not all — pipeline should continue
    if final_result.nets_routed > 0:
        return 2
    # Nothing was routed — treat as fatal failure
    return 1


def route_with_rule_relaxation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with automatic design rule relaxation.

    Tries routing with progressively relaxed design rules (trace width,
    clearance) until success or manufacturer minimum limits are reached.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import flush_print, spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        get_relaxation_tiers,
        is_cpp_available,
        load_pcb_for_routing,
        show_routing_summary,
    )
    from kicad_tools.router.io import detect_layer_stack

    # Handle backend selection
    force_python = False
    if args.backend == "cpp":
        if not is_cpp_available():
            print(
                "Error: C++ backend requested but not available.\n"
                "Build the C++ extension or use --backend auto/python.\n"
                "See README for build instructions.",
                file=sys.stderr,
            )
            return 1
    elif args.backend == "python":
        force_python = True

    # Warn prominently when auto-backend falls back to Python
    if args.backend == "auto" and not is_cpp_available() and not quiet:
        flush_print("WARNING: C++ router backend not installed -- using Python (10-100x slower).")
        flush_print("  Build it now:  kct build-native")
        flush_print("  Check status:  kct build-native --check")
        flush_print()

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Auto-create copper pours for power nets (before skip detection)
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        auto_pour_if_missing(pcb_path, quiet=quiet)

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=quiet)

    # Get relaxation tiers
    tiers = get_relaxation_tiers(
        initial_trace_width=args.trace_width,
        initial_clearance=args.clearance,
        initial_via_drill=args.via_drill,
        initial_via_diameter=args.via_diameter,
        manufacturer=args.manufacturer,
        min_trace_floor=args.min_trace,
        min_clearance_floor=args.min_clearance_floor,
    )

    # Determine layer stack
    if args.layers == "auto":
        pcb_text = pcb_path.read_text()
        layer_stack = detect_layer_stack(pcb_text)
    else:
        layer_stack_map = {
            "2": LayerStack.two_layer(),
            "4": LayerStack.four_layer_sig_gnd_pwr_sig(),
            "4-sig": LayerStack.four_layer_sig_sig_gnd_pwr(),
            "4-all": LayerStack.four_layer_all_signal(),
            "6": LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig(),
        }
        layer_stack = layer_stack_map[args.layers]

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Adaptive Rules Mode")
        flush_print("=" * 60)
        flush_print(f"Input:          {pcb_path}")
        flush_print(f"Output:         {output_path}")
        flush_print(f"Strategy:       {args.strategy}")
        flush_print(f"Manufacturer:   {args.manufacturer}")
        flush_print(f"Min completion: {args.min_completion * 100:.0f}%")
        flush_print(f"Relaxation tiers: {len(tiers)}")
        if skip_nets:
            flush_print(f"Skip:           {', '.join(skip_nets)}")
        flush_print()

    best_result: RuleRelaxationResult | None = None
    successful_result: RuleRelaxationResult | None = None

    # Register signal handlers so SIGTERM/SIGINT save the best attempt so far
    _interrupt_state["output_path"] = output_path
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["router"] = None
    _interrupt_state["interrupted"] = False
    _interrupt_state["best_completed_attempt"] = False
    prev_sigint = signal.signal(signal.SIGINT, _handle_interrupt)
    prev_sigterm = signal.signal(signal.SIGTERM, _handle_interrupt)

    for tier in tiers:
        if not quiet:
            flush_print("=" * 60)
            flush_print(f"Attempt {tier.tier + 1}: {tier.description}")
            flush_print(f"  trace={tier.trace_width:.3f}mm, clearance={tier.clearance:.3f}mm")
            flush_print("=" * 60)

        # Configure design rules for this tier
        fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
        rules = DesignRules(
            grid_resolution=args.grid,
            trace_width=tier.trace_width,
            trace_clearance=tier.clearance,
            via_drill=tier.via_drill,
            via_diameter=tier.via_diameter,
            fine_pitch_clearance=fine_pitch_cl,
        )

        # Load PCB
        try:
            with spinner(f"Loading PCB (tier {tier.tier})...", quiet=quiet):
                router, net_map = load_pcb_for_routing(
                    str(pcb_path),
                    skip_nets=skip_nets,
                    rules=rules,
                    edge_clearance=args.edge_clearance,
                    layer_stack=layer_stack,
                    force_python=force_python,
                    validate_drc=not args.force,
                    strict_drc=False,
                )
        except Exception as e:
            if not quiet:
                print(f"  Error loading PCB: {e}")
            continue

        # Issue #1841: Tell the autorouter which pour nets lack zones
        router._pour_nets_without_zones = set(_no_zone)

        # Count nets to route
        multi_pad_nets = [
            net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) >= 2
        ]
        nets_to_route = len(multi_pad_nets)

        if not quiet:
            flush_print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
            flush_print(f"  Nets to route: {nets_to_route}")

        # Route
        if not quiet:
            flush_print(f"\n  Routing ({args.strategy})...")

        escape_flag = _resolve_escape_routing_flag(args)

        try:
            if _should_use_escape_routing(router, escape_flag, quiet):
                router.route_with_escape(
                    use_negotiated=(args.strategy == "negotiated"),
                    timeout=args.timeout,
                )
            elif getattr(args, "multi_resolution", False):
                router.route_all_multi_resolution(
                    use_negotiated=(args.strategy == "negotiated"),
                    max_iterations=args.iterations,
                    timeout=args.timeout,
                )
            elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                router.route_all_two_phase(
                    use_negotiated=True,
                    corridor_width_factor=2.0,
                    timeout=args.timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                )
            elif args.strategy == "negotiated":
                router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=args.timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    batch_routing=getattr(args, "batch_routing", False)
                    or getattr(args, "high_performance", False),
                    hierarchical=getattr(args, "hierarchical", False),
                    perturbation=getattr(args, "perturbation", True),
                )
            elif args.strategy == "basic":
                router.route_all()
            elif args.strategy == "monte-carlo":
                router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
        except Exception as e:
            if not quiet:
                print(f"  Routing error: {e}")
            continue

        # Calculate completion — filter to multi-pad nets only (Issue #1643)
        multi_pad_net_ids = set(multi_pad_nets)
        stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)
        nets_routed = stats["nets_routed"]
        completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0

        # Create result
        result = RuleRelaxationResult(
            tier=tier.tier,
            trace_width=tier.trace_width,
            clearance=tier.clearance,
            via_drill=tier.via_drill,
            via_diameter=tier.via_diameter,
            tier_description=tier.description,
            router=router,
            net_map=net_map,
            nets_routed=nets_routed,
            nets_to_route=nets_to_route,
            completion=completion,
            success=completion >= args.min_completion,
            layer_count=layer_stack.num_layers,
            stats=stats,
        )

        # Track best result (Issue #2396: absolute nets_routed comparison)
        if best_result is None or _is_better_result(result, best_result):
            best_result = result
            # Update interrupt state so signal handler saves the best attempt
            _interrupt_state["router"] = result.router
            _interrupt_state["best_completed_attempt"] = True

        # Report attempt result
        status = "SUCCESS" if result.success else "INSUFFICIENT - relaxing rules"
        if not quiet:
            flush_print(f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)")
            flush_print(f"  Status: {status}")

        # Check for success
        if result.success:
            successful_result = result
            break

        # Early termination: skip remaining tiers when completion regresses
        if not getattr(args, "no_early_stop", False) and best_result is not None:
            if completion < best_result.completion:
                if not quiet:
                    flush_print(
                        f"\n  Early stop: tier {tier.tier + 1} completion "
                        f"({completion * 100:.0f}%) is worse than best "
                        f"({best_result.completion * 100:.0f}%) — "
                        f"skipping remaining tiers"
                    )
                break

    # Restore original signal handlers
    signal.signal(signal.SIGINT, prev_sigint)
    signal.signal(signal.SIGTERM, prev_sigterm)
    _interrupt_state["best_completed_attempt"] = False

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("ADAPTIVE RULES SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Design routed successfully with relaxed rules "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print("\nFinal design rules:")
            print(f"  Trace width: {final_result.trace_width:.3f}mm (was {args.trace_width}mm)")
            print(f"  Clearance:   {final_result.clearance:.3f}mm (was {args.clearance}mm)")
            if final_result.tier > 0:
                print(f"\n  Note: Rules were relaxed ({final_result.tier_description})")
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result at tier {final_result.tier} "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"even at manufacturer minimum tolerances"
            )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
        return 1

    # Check if at manufacturer minimum
    from kicad_tools.router import get_mfr_limits

    mfr = get_mfr_limits(args.manufacturer)
    at_minimum = (
        final_result.trace_width <= mfr.min_trace + 0.001
        and final_result.clearance <= mfr.min_clearance + 0.001
    )
    if at_minimum and not quiet:
        print(f"\nWARNING: Design uses {args.manufacturer.upper()} minimum tolerances.")
        print("Consider adding layers for more manufacturing margin.")

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.
        has_overflow = final_result.router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(
            final_result.router.grid, ignore_overflow=has_overflow
        )
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        with spinner("Optimizing traces...", quiet=quiet):
            optimized_routes = []
            for route in final_result.router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            final_result.router.routes = optimized_routes

    # Post-optimization DRC nudge pass
    if final_result.router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(final_result.router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    _final_multi_pad_ids = {
        n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
    }
    route_sexp, final_stats, _cleanup_stats = _finalize_routes(
        final_result.router,
        _final_multi_pad_ids,
        final_result.nets_to_route,
        quiet=quiet,
    )
    # Update result with post-cleanup stats
    final_result.nets_routed = final_stats["nets_routed"]
    final_result.completion = (
        final_result.nets_routed / final_result.nets_to_route
        if final_result.nets_to_route > 0
        else 1.0
    )
    final_result.success = final_result.completion >= args.min_completion

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        # Read original PCB content
        original_content = pcb_path.read_text()

        # Insert routes before final closing parenthesis
        if route_sexp:
            output_content = _insert_sexp_before_closing(original_content, route_sexp)
        else:
            output_content = original_content
            if not quiet:
                print("  Warning: No routes generated!")

        # Validate S-expression structure before writing
        if not _validate_sexp_parentheses(output_content):
            logger.error("Generated PCB file has unbalanced parentheses")
            raise ValueError(
                "Generated PCB file has invalid S-expression syntax "
                "(unbalanced parentheses). This is a bug in kicad-tools. "
                "Please report it."
            )

        output_path.write_text(output_content)

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Final trace width: {final_result.trace_width:.3f}mm")
        print(f"  Final clearance: {final_result.clearance:.3f}mm")

    # Run DRC validation unless skipped
    if not args.skip_drc and final_result.nets_routed > 0:
        drc_errors, _ = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
            )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print("SUCCESS: Routing complete with adaptive rules")
            if final_result.tier > 0:
                print(
                    f"  Note: Relaxed from tier 0 to tier {final_result.tier} "
                    f"({final_result.tier_description})"
                )
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"at tier {final_result.tier}"
            )
            _multi_pad_ids = {
                n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
            }
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
                current_strategy=args.strategy,
                pcb_file=args.pcb,
                nets_to_route_ids=_multi_pad_ids,
                single_pad_count=getattr(final_result, "single_pad_count", 0),
            )

    if final_result.success:
        return 0
    # Partial routing: some nets were routed but not all — pipeline should continue
    if final_result.nets_routed > 0:
        return 2
    # Nothing was routed — treat as fatal failure
    return 1


def route_with_combined_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with combined layer and rule escalation (2D search).

    Implements a 2D search across both layer counts and design rule tiers
    to find the minimum viable configuration.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import flush_print, spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        get_relaxation_tiers,
        is_cpp_available,
        load_pcb_for_routing,
        show_routing_summary,
    )

    # Handle backend selection
    force_python = False
    if args.backend == "cpp":
        if not is_cpp_available():
            print(
                "Error: C++ backend requested but not available.\n"
                "Build the C++ extension or use --backend auto/python.\n"
                "See README for build instructions.",
                file=sys.stderr,
            )
            return 1
    elif args.backend == "python":
        force_python = True

    # Warn prominently when auto-backend falls back to Python
    if args.backend == "auto" and not is_cpp_available() and not quiet:
        flush_print("WARNING: C++ router backend not installed -- using Python (10-100x slower).")
        flush_print("  Build it now:  kct build-native")
        flush_print("  Check status:  kct build-native --check")
        flush_print()

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Auto-create copper pours for power nets (before skip detection)
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        auto_pour_if_missing(pcb_path, quiet=quiet)

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=quiet)

    # Get relaxation tiers
    tiers = get_relaxation_tiers(
        initial_trace_width=args.trace_width,
        initial_clearance=args.clearance,
        initial_via_drill=args.via_drill,
        initial_via_diameter=args.via_diameter,
        manufacturer=args.manufacturer,
        min_trace_floor=args.min_trace,
        min_clearance_floor=args.min_clearance_floor,
    )

    # Layer stacks to try (in escalation order)
    layer_configs = [
        (2, LayerStack.two_layer()),
        (4, LayerStack.four_layer_sig_gnd_pwr_sig()),
        (4, LayerStack.four_layer_all_signal()),
        (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
    ]

    # Filter by max_layers
    layer_configs = [(n, s) for n, s in layer_configs if n <= args.max_layers]

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Combined Escalation Mode")
        flush_print("=" * 60)
        flush_print(f"Input:          {pcb_path}")
        flush_print(f"Output:         {output_path}")
        flush_print(f"Strategy:       {args.strategy}")
        flush_print(f"Manufacturer:   {args.manufacturer}")
        flush_print(f"Max layers:     {args.max_layers}")
        flush_print(f"Min completion: {args.min_completion * 100:.0f}%")
        flush_print(f"Rule tiers:     {len(tiers)}")
        flush_print(f"Layer configs:  {[n for n, _ in layer_configs]}")
        if skip_nets:
            flush_print(f"Skip:           {', '.join(skip_nets)}")
        flush_print()
        flush_print("Search matrix:")
        flush_print("         ", end="")
        for n, _ in layer_configs:
            flush_print(f" {n}L    ", end="")
        flush_print()

    best_result: RuleRelaxationResult | None = None
    successful_result: RuleRelaxationResult | None = None
    results_matrix: dict[tuple[int, int], float] = {}  # (tier, layers) -> completion

    # Register signal handlers so SIGTERM/SIGINT save the best attempt so far
    _interrupt_state["output_path"] = output_path
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["router"] = None
    _interrupt_state["interrupted"] = False
    _interrupt_state["best_completed_attempt"] = False
    prev_sigint = signal.signal(signal.SIGINT, _handle_interrupt)
    prev_sigterm = signal.signal(signal.SIGTERM, _handle_interrupt)

    # 2D search: prioritize fewer layers first, then stricter rules
    for layer_count, layer_stack in layer_configs:
        best_completion_for_layer: float | None = None
        for tier in tiers:
            if not quiet:
                flush_print(
                    f"\nTrying: {layer_count} layers, tier {tier.tier} "
                    f"(trace={tier.trace_width:.2f}mm, clearance={tier.clearance:.2f}mm)"
                )

            # Configure design rules for this tier
            fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
            rules = DesignRules(
                grid_resolution=args.grid,
                trace_width=tier.trace_width,
                trace_clearance=tier.clearance,
                via_drill=tier.via_drill,
                via_diameter=tier.via_diameter,
                fine_pitch_clearance=fine_pitch_cl,
            )

            # Load PCB
            try:
                with spinner(f"Loading PCB ({layer_count}L, tier {tier.tier})...", quiet=quiet):
                    router, net_map = load_pcb_for_routing(
                        str(pcb_path),
                        skip_nets=skip_nets,
                        rules=rules,
                        edge_clearance=args.edge_clearance,
                        layer_stack=layer_stack,
                        force_python=force_python,
                        validate_drc=not args.force,
                        strict_drc=False,
                    )
            except Exception as e:
                if not quiet:
                    print(f"  Error loading PCB: {e}")
                results_matrix[(tier.tier, layer_count)] = 0.0
                continue

            # Issue #1841: Tell the autorouter which pour nets lack zones
            router._pour_nets_without_zones = set(_no_zone)

            # Count nets to route
            multi_pad_nets = [
                net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) >= 2
            ]
            nets_to_route = len(multi_pad_nets)

            # Route
            escape_flag = _resolve_escape_routing_flag(args)

            try:
                if _should_use_escape_routing(router, escape_flag, quiet):
                    router.route_with_escape(
                        use_negotiated=(args.strategy == "negotiated"),
                        timeout=args.timeout,
                    )
                elif getattr(args, "multi_resolution", False):
                    router.route_all_multi_resolution(
                        use_negotiated=(args.strategy == "negotiated"),
                        max_iterations=args.iterations,
                        timeout=args.timeout,
                    )
                elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                    router.route_all_two_phase(
                        use_negotiated=True,
                        corridor_width_factor=2.0,
                        timeout=args.timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                        max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                    )
                elif args.strategy == "negotiated":
                    router.route_all_negotiated(
                        max_iterations=args.iterations,
                        timeout=args.timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                        batch_routing=getattr(args, "batch_routing", False)
                        or getattr(args, "high_performance", False),
                        hierarchical=getattr(args, "hierarchical", False),
                        perturbation=getattr(args, "perturbation", True),
                    )
                elif args.strategy == "basic":
                    router.route_all()
                elif args.strategy == "monte-carlo":
                    router.route_all_monte_carlo(
                        num_trials=args.mc_trials,
                        verbose=args.verbose and not quiet,
                    )
            except Exception as e:
                if not quiet:
                    print(f"  Routing error: {e}")
                results_matrix[(tier.tier, layer_count)] = 0.0
                continue

            # Calculate completion — filter to multi-pad nets only (Issue #1643)
            multi_pad_net_ids = set(multi_pad_nets)
            stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)
            nets_routed = stats["nets_routed"]
            completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0
            results_matrix[(tier.tier, layer_count)] = completion

            if not quiet:
                flush_print(f"  Routed: {nets_routed}/{nets_to_route} ({completion * 100:.0f}%)")

            # Create result
            result = RuleRelaxationResult(
                tier=tier.tier,
                trace_width=tier.trace_width,
                clearance=tier.clearance,
                via_drill=tier.via_drill,
                via_diameter=tier.via_diameter,
                tier_description=tier.description,
                router=router,
                net_map=net_map,
                nets_routed=nets_routed,
                nets_to_route=nets_to_route,
                completion=completion,
                success=completion >= args.min_completion,
                layer_count=layer_count,
                stats=stats,
            )

            # Track best result (Issue #2396: absolute nets_routed comparison)
            if best_result is None or _is_better_result(result, best_result):
                best_result = result
                # Update interrupt state so signal handler saves the best attempt
                _interrupt_state["router"] = result.router
                _interrupt_state["best_completed_attempt"] = True

            # Track best completion for this layer config
            if best_completion_for_layer is None or completion > best_completion_for_layer:
                best_completion_for_layer = completion

            # Check for success (first success wins - minimum config)
            if result.success:
                successful_result = result
                break

            # Early termination: skip remaining tiers when completion regresses
            # within this layer config
            if not getattr(args, "no_early_stop", False):
                if best_completion_for_layer is not None and completion < best_completion_for_layer:
                    if not quiet:
                        flush_print(
                            f"\n  Early stop: {layer_count}L tier {tier.tier} "
                            f"completion ({completion * 100:.0f}%) is worse than "
                            f"best for {layer_count}L "
                            f"({best_completion_for_layer * 100:.0f}%) — "
                            f"skipping remaining tiers for {layer_count}L"
                        )
                    break

        # If we found a successful config at this layer count, stop
        if successful_result:
            break

    # Restore original signal handlers
    signal.signal(signal.SIGINT, prev_sigint)
    signal.signal(signal.SIGTERM, prev_sigterm)
    _interrupt_state["best_completed_attempt"] = False

    # Print results matrix
    if not quiet:
        print("\n" + "=" * 60)
        print("SEARCH MATRIX RESULTS")
        print("=" * 60)
        print("         ", end="")
        for n, _ in layer_configs:
            print(f" {n}L     ", end="")
        print()
        for tier in tiers:
            print(f"Tier {tier.tier}:  ", end="")
            for n, _ in layer_configs:
                comp = results_matrix.get((tier.tier, n), 0.0)
                if comp >= args.min_completion:
                    print(f" {comp * 100:3.0f}%✓  ", end="")
                else:
                    print(f" {comp * 100:3.0f}%   ", end="")
            print()

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("COMBINED ESCALATION SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Minimum viable configuration found\n"
                f"  Layers: {final_result.layer_count}\n"
                f"  Tier: {final_result.tier} ({final_result.tier_description})\n"
                f"  Completion: {final_result.completion * 100:.0f}%"
            )
            print("\nFinal design rules:")
            print(f"  Trace width: {final_result.trace_width:.3f}mm")
            print(f"  Clearance:   {final_result.clearance:.3f}mm")
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result at {final_result.layer_count} layers, "
                f"tier {final_result.tier} ({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"in any configuration"
            )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
        return 1

    # Check if at manufacturer minimum
    from kicad_tools.router import get_mfr_limits

    mfr = get_mfr_limits(args.manufacturer)
    at_minimum = (
        final_result.trace_width <= mfr.min_trace + 0.001
        and final_result.clearance <= mfr.min_clearance + 0.001
    )
    if at_minimum and not quiet:
        print(f"\nWARNING: Design uses {args.manufacturer.upper()} minimum tolerances.")
        print("Consider redesigning placement for more margin.")

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.
        has_overflow = final_result.router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(
            final_result.router.grid, ignore_overflow=has_overflow
        )
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        with spinner("Optimizing traces...", quiet=quiet):
            optimized_routes = []
            for route in final_result.router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            final_result.router.routes = optimized_routes

    # Post-optimization DRC nudge pass
    if final_result.router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(final_result.router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    _final_multi_pad_ids = {
        n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
    }
    route_sexp, final_stats, _cleanup_stats = _finalize_routes(
        final_result.router,
        _final_multi_pad_ids,
        final_result.nets_to_route,
        quiet=quiet,
    )
    # Update result with post-cleanup stats
    final_result.nets_routed = final_stats["nets_routed"]
    final_result.completion = (
        final_result.nets_routed / final_result.nets_to_route
        if final_result.nets_to_route > 0
        else 1.0
    )
    final_result.success = final_result.completion >= args.min_completion

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        # Read original PCB content
        original_content = pcb_path.read_text()

        # Update layer stackup if we escalated
        if final_result.layer_count > 2:
            original_content = update_pcb_layer_stackup(original_content, final_result.layer_count)

        # Insert routes before final closing parenthesis
        if route_sexp:
            output_content = _insert_sexp_before_closing(original_content, route_sexp)
        else:
            output_content = original_content
            if not quiet:
                print("  Warning: No routes generated!")

        # Validate S-expression structure before writing
        if not _validate_sexp_parentheses(output_content):
            logger.error("Generated PCB file has unbalanced parentheses")
            raise ValueError(
                "Generated PCB file has invalid S-expression syntax "
                "(unbalanced parentheses). This is a bug in kicad-tools. "
                "Please report it."
            )

        # Update output filename to include layer count and tier
        if final_result.layer_count > 2 or final_result.tier > 0:
            suffix = ""
            if final_result.layer_count > 2:
                suffix += f"_{final_result.layer_count}layer"
            output_path = output_path.with_stem(output_path.stem + suffix)

        output_path.write_text(output_content)

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Layer count: {final_result.layer_count}")
        print(f"  Final trace width: {final_result.trace_width:.3f}mm")
        print(f"  Final clearance: {final_result.clearance:.3f}mm")

    # Run DRC validation unless skipped
    if not args.skip_drc and final_result.nets_routed > 0:
        drc_errors, _ = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
            )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print(
                f"SUCCESS: Minimum viable config = {final_result.layer_count} layers + "
                f"tier {final_result.tier} rules"
            )
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"at {final_result.layer_count} layers, tier {final_result.tier}"
            )
            _multi_pad_ids = {
                n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
            }
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
                current_strategy=args.strategy,
                pcb_file=args.pcb,
                nets_to_route_ids=_multi_pad_ids,
                single_pad_count=getattr(final_result, "single_pad_count", 0),
            )

    if final_result.success:
        return 0
    # Partial routing: some nets were routed but not all — pipeline should continue
    if final_result.nets_routed > 0:
        return 2
    # Nothing was routed — treat as fatal failure
    return 1


def _resolve_escape_routing_flag(args) -> bool | None:
    """Resolve --escape-routing / --no-escape-routing into a tri-state value.

    Returns:
        True if escape routing is explicitly enabled,
        False if explicitly disabled,
        None for auto-detect (default).
    """
    no_escape = getattr(args, "no_escape_routing", False)
    escape = getattr(args, "escape_routing", None)

    if no_escape:
        return False
    if escape:
        return True
    return None


def _should_use_escape_routing(router, escape_flag: bool | None, quiet: bool) -> bool:
    """Determine whether to use escape routing for the current board.

    Args:
        router: The Autorouter instance.
        escape_flag: True=force on, False=force off, None=auto-detect.
        quiet: Suppress progress output.

    Returns:
        True if escape routing should be used.
    """
    if escape_flag is True:
        if not quiet:
            print("  Escape routing: enabled (--escape-routing)")
        return True
    if escape_flag is False:
        return False

    # Auto-detect dense packages
    dense_packages = router.detect_dense_packages()
    if dense_packages:
        if not quiet:
            refs = [p.ref for p in dense_packages]
            print(f"  Escape routing: auto-enabled (dense packages: {refs})")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    """Main entry point for route command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools route",
        description="Autoroute a KiCad PCB file",
        epilog=textwrap.dedent("""\
            exit codes:
              0  all nets routed (or meets --min-completion), DRC clean
              1  fatal failure -- no nets routed
              2  partial routing -- below --min-completion threshold
              3  routing meets threshold but DRC violations remain
              4  partial routing AND segment-segment clearance violations
              5  interrupted by SIGINT with partial results saved
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: <input>_routed.kicad_pcb)",
    )
    parser.add_argument(
        "--strategy",
        choices=["basic", "negotiated", "monte-carlo"],
        default="negotiated",
        help="Routing strategy (default: negotiated)",
    )
    parser.add_argument(
        "--skip-nets",
        help="Comma-separated nets to skip (e.g., GND,VCC,VBUS)",
    )
    parser.add_argument(
        "--grid",
        type=str,
        default="auto",
        help=(
            "Grid resolution in mm or 'auto' for automatic selection "
            "(default: auto, analyzes pad positions and clearance; "
            "use explicit value like 0.1 for dense QFP)"
        ),
    )
    parser.add_argument(
        "--grid-strategy",
        choices=["adaptive", "uniform"],
        default="adaptive",
        help=(
            "Grid strategy when --grid auto is used. "
            "'adaptive' (default) uses multi-resolution grids with fine zones "
            "around fine-pitch components. 'uniform' forces single-resolution grid."
        ),
    )
    parser.add_argument(
        "--trace-width",
        type=float,
        default=0.2,
        help="Trace width in mm (default: 0.2)",
    )
    parser.add_argument(
        "--clearance",
        type=float,
        default=0.15,
        help="Trace clearance in mm (default: 0.15)",
    )
    parser.add_argument(
        "--fine-pitch-clearance",
        type=float,
        default=None,
        help=(
            "Clearance for fine-pitch components (pitch < 0.8mm) in mm. "
            "When set, SSOP/QFP/QFN packages automatically use this reduced "
            "clearance to allow traces between pins. Example: --fine-pitch-clearance 0.08"
        ),
    )
    parser.add_argument(
        "--via-drill",
        type=float,
        default=0.3,
        help="Via drill size in mm (default: 0.3)",
    )
    parser.add_argument(
        "--via-diameter",
        type=float,
        default=0.6,
        help="Via pad diameter in mm (default: 0.6)",
    )
    parser.add_argument(
        "--mc-trials",
        type=int,
        default=10,
        help="Number of Monte Carlo trials (default: 10)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=15,
        help=(
            "Max iterations for negotiated routing (default: 15). "
            "Also applies to two-phase routing when --two-phase-iterations "
            "is not explicitly set."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Timeout in seconds for routing (default: no timeout). Returns best partial result if reached.",
    )
    parser.add_argument(
        "--per-net-timeout",
        type=float,
        default=30.0,
        help="Wall-clock timeout in seconds for each per-net A* search (default: 30). "
        "Prevents individual nets from monopolizing the router. Use 0 to disable.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show routing preview with per-net details before saving (interactive)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing output",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Analyze routability before routing and show diagnostic report",
    )
    parser.add_argument(
        "--bus-routing",
        action="store_true",
        help="Enable bus-aware routing (routes bus signals together)",
    )
    parser.add_argument(
        "--bus-mode",
        choices=["parallel", "stacked", "bundled"],
        default="parallel",
        help="Bus routing mode (default: parallel)",
    )
    parser.add_argument(
        "--bus-spacing",
        type=float,
        help="Spacing between bus signals in mm (default: trace_width + clearance)",
    )
    parser.add_argument(
        "--bus-min-width",
        type=int,
        default=2,
        help="Minimum signals to form a bus group (default: 2)",
    )
    parser.add_argument(
        "--differential-pairs",
        action="store_true",
        help="Enable differential pair routing (routes paired signals together)",
    )
    parser.add_argument(
        "--diffpair-spacing",
        type=float,
        help="Spacing between differential pair traces in mm (default: auto based on type)",
    )
    parser.add_argument(
        "--diffpair-max-delta",
        type=float,
        help="Maximum length mismatch for differential pairs in mm (default: auto based on type)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )
    parser.add_argument(
        "--perturbation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enable stochastic cost perturbation to escape local minima "
            "(default: enabled). Use --no-perturbation to disable."
        ),
    )
    parser.add_argument(
        "--power-nets",
        help=(
            "Generate copper zones for power nets: 'NET1:LAYER1,NET2:LAYER2,...' "
            "(e.g., 'GND:B.Cu,+3.3V:F.Cu')"
        ),
    )
    parser.add_argument(
        "--edge-clearance",
        type=float,
        help=(
            "Copper-to-edge clearance in mm. Blocks routing within this distance "
            "of the board edge. Common values: 0.25-0.5mm (default: no clearance)"
        ),
    )
    parser.add_argument(
        "--layers",
        choices=["auto", "2", "4", "4-sig", "4-all", "6"],
        default="auto",
        help=(
            "Layer stack configuration for routing: "
            "'auto' = auto-detect from PCB file (default); "
            "'2' = 2-layer (F.Cu, B.Cu); "
            "'4' = 4-layer with GND/PWR planes (F.Cu, In1=GND, In2=PWR, B.Cu); "
            "'4-sig' = 4-layer with 2 signal layers (F.Cu, In1=signal, In2=GND, B.Cu); "
            "'4-all' = 4-layer with all 4 signal layers (no planes); "
            "'6' = 6-layer with 4 signal layers. "
            "Auto-detection parses the PCB's layer definitions and zones to "
            "determine the appropriate layer stack."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Force routing even when grid resolution exceeds clearance. "
            "Without this flag, routing will fail if grid > clearance to "
            "prevent DRC violations. Use with caution."
        ),
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help=(
            "Enable profiling to measure performance. Outputs a cProfile "
            "stats file that can be analyzed with pstats or visualization tools."
        ),
    )
    parser.add_argument(
        "--profile-output",
        metavar="FILE",
        help=(
            "Output file for profile data (default: route_profile.prof). "
            "Analyze with: python -m pstats route_profile.prof, or "
            "visualize with: snakeviz route_profile.prof"
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "cpp", "python"],
        default="auto",
        help=(
            "Router backend to use: "
            "'auto' = use C++ if available, fall back to Python (default); "
            "'cpp' = require C++ backend (fails if not available); "
            "'python' = force Python backend (for testing/debugging). "
            "C++ backend provides 10-100x speedup for fine-grid routing."
        ),
    )
    parser.add_argument(
        "--skip-drc",
        action="store_true",
        help=(
            "Skip post-routing DRC validation. By default, the router runs "
            "a DRC check after routing and warns about violations. Use this "
            "flag for performance-critical use or when running separate validation."
        ),
    )
    parser.add_argument(
        "--manufacturer",
        "--mfr",
        default="jlcpcb",
        help=(
            "Manufacturer profile for DRC validation (default: jlcpcb). "
            "Determines minimum clearances, trace widths, and other design rules."
        ),
    )
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        help=(
            "Automatically run 'kct fix-drc' after routing if DRC violations are "
            "detected. Suppressed by --dry-run and --skip-drc. Uses iterative "
            "repair to fix clearance and drill violations."
        ),
    )
    parser.add_argument(
        "--auto-fix-passes",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Number of repair passes for --auto-fix (default: 3). "
            "Implies --auto-fix. Multiple passes can fix cascading violations "
            "where fixing one violation exposes or resolves others."
        ),
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help=(
            "Skip trace optimization after routing. By default, traces are "
            "optimized to merge collinear segments, eliminate zigzags, and "
            "convert corners to 45 degrees. Use this flag to keep raw "
            "grid-step segments for debugging."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        dest="no_optimize",
        help="Alias for --no-optimize (keep raw grid-step segments for debugging)",
    )
    parser.add_argument(
        "--auto-pour",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Automatically create copper pour zones for power-classified "
            "nets (GND, VCC, etc.) when the input PCB has none "
            "(default: enabled). Use --no-auto-pour to disable."
        ),
    )
    parser.add_argument(
        "--auto-layers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Automatically escalate layer count on routing failure "
            "(default: enabled). Tries 2 → 4 → 6 layers until routing "
            "succeeds or --max-layers is reached. Reports minimum viable "
            "layer count for the design. The first attempt is still "
            "2-layer, so 2-layer-solvable boards pay no extra cost. "
            "Use --no-auto-layers to disable and route at a fixed layer "
            "count (whatever --layers specifies, or auto-detected)."
        ),
    )
    parser.add_argument(
        "--max-layers",
        type=int,
        default=6,
        choices=[2, 4, 6],
        help=(
            "Maximum layer count for auto-escalation (default: 6). Only used with --auto-layers."
        ),
    )
    parser.add_argument(
        "--min-completion",
        type=float,
        default=0.95,
        help=(
            "Minimum routing completion rate for success (default: 0.95 = 95%%). "
            "Controls the exit code threshold: routing above this rate returns "
            "exit code 0 (success), below returns exit code 2 (partial). "
            "Also used with --auto-layers to control layer escalation. "
            "If no layer count achieves this, the best result is saved."
        ),
    )
    parser.add_argument(
        "--adaptive-rules",
        action="store_true",
        help=(
            "Automatically relax design rules on routing failure. "
            "Tries progressively relaxed trace widths and clearances "
            "until routing succeeds or manufacturer limits are reached. "
            "Reports which rules were relaxed and warns if minimum tolerances used."
        ),
    )
    parser.add_argument(
        "--no-early-stop",
        action="store_true",
        help=(
            "Disable early termination of adaptive-rules tier search. "
            "By default, if a tier routes fewer nets than the best prior "
            "attempt, remaining tiers are skipped. Use this flag to force "
            "all tiers to run (useful for debugging or benchmarking)."
        ),
    )
    parser.add_argument(
        "--min-trace",
        type=float,
        help=(
            "Minimum trace width floor for adaptive rules (mm). "
            "Prevents relaxation below this value. "
            "Default: manufacturer minimum (e.g., 0.127mm for JLCPCB)."
        ),
    )
    parser.add_argument(
        "--min-clearance-floor",
        type=float,
        help=(
            "Minimum clearance floor for adaptive rules (mm). "
            "Prevents relaxation below this value. "
            "Default: manufacturer minimum (e.g., 0.127mm for JLCPCB)."
        ),
    )
    parser.add_argument(
        "--progressive-clearance",
        action="store_true",
        help=(
            "Enable progressive clearance relaxation for failed nets. "
            "Routes all nets with standard clearance first, then retries "
            "failed nets with progressively relaxed clearance (up to --min-clearance). "
            "Unlike --adaptive-rules which globally relaxes all rules, this only "
            "relaxes clearance for specific failed nets. Reports which nets needed "
            "relaxation and the clearance used."
        ),
    )
    parser.add_argument(
        "--min-clearance",
        type=float,
        help=(
            "Minimum clearance for progressive relaxation (mm). "
            "Used with --progressive-clearance to set the floor for relaxation. "
            "Default: 50%% of --clearance value."
        ),
    )
    parser.add_argument(
        "--relaxation-levels",
        type=int,
        default=3,
        help=(
            "Number of progressive relaxation levels (default: 3). "
            "More levels = finer-grained relaxation steps."
        ),
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help=(
            "Show detailed routing failure diagnostics. For each failed net, "
            "reports the specific failure reason, blocking obstacles, coordinates, "
            "and actionable suggestions. Failures are grouped by cause for analysis."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help=(
            "Output format for routing diagnostics: "
            "'text' = human-readable output (default); "
            "'json' = JSON output for tooling and automation."
        ),
    )
    parser.add_argument(
        "--high-performance",
        action="store_true",
        help=(
            "Use high-performance mode with aggressive parallelization and more trials. "
            "Uses calibrated settings if available (run 'kicad-tools calibrate' first)."
        ),
    )
    parser.add_argument(
        "--hierarchical",
        action="store_true",
        help=(
            "Enable hierarchical coarse-to-fine routing mode. "
            "First performs global routing on a coarse grid (4x resolution) "
            "to establish corridors, then refines with the fine grid only "
            "near pads and congestion points. Can significantly speed up "
            "fine-grid routing (0.05mm-0.1mm) on large boards."
        ),
    )
    parser.add_argument(
        "--multi-resolution",
        action="store_true",
        help=(
            "Enable multi-resolution routing with fine-grid fallback. "
            "First routes all nets on the coarse grid, then retries failed "
            "nets on a finer grid (2x resolution) scoped to their bounding "
            "boxes. Useful for boards where some nets fail due to grid "
            "resolution limitations."
        ),
    )
    parser.add_argument(
        "--escape-routing",
        action="store_true",
        default=None,
        help=(
            "Enable escape routing phase before global routing. "
            "Generates escape routes for dense QFP/QFN/BGA packages "
            "where pin pitch is too small for traces to pass between "
            "adjacent pins. Without this flag, escape routing is "
            "auto-detected based on package density."
        ),
    )
    parser.add_argument(
        "--no-escape-routing",
        action="store_true",
        help=(
            "Disable automatic escape routing detection. By default, "
            "the router auto-detects dense packages and enables escape "
            "routing when needed. Use this flag to skip escape routing "
            "even when dense packages are present."
        ),
    )
    parser.add_argument(
        "--two-phase",
        action="store_true",
        help=(
            "Use two-phase global+detailed routing. Phase 1 allocates "
            "coarse corridors on a tile graph; Phase 2 routes within those "
            "corridors using negotiated congestion. Produces dramatically "
            "better results on complex multi-layer boards by preventing "
            "overflow divergence. When combined with escape routing, "
            "replaces the negotiated rip-up phase after escape generation."
        ),
    )
    parser.add_argument(
        "--two-phase-iterations",
        type=int,
        default=None,
        help=(
            "Max rip-up-and-reroute iterations for the Phase 2 detailed "
            "negotiated routing loop in two-phase mode. Overrides --iterations "
            "for the two-phase path when both are given. If omitted, falls back "
            "to --iterations (default: 20 when neither flag is set). "
            "Only effective with --two-phase."
        ),
    )
    parser.add_argument(
        "--batch-routing",
        action="store_true",
        help=(
            "Enable GPU-accelerated batch routing for parallel net processing. "
            "Routes multiple independent nets simultaneously using GPU compute. "
            "Best results with 4+ independent nets and Metal/CUDA GPU. "
            "Enabled automatically in high-performance mode."
        ),
    )

    # Power plane stitching
    parser.add_argument(
        "--stitch-power-planes",
        action="store_true",
        help=(
            "Automatically add stitching vias for power planes after routing. "
            "Connects surface-mount component pads to their power plane layers. "
            "Equivalent to running 'kicad-pcb-stitch' after routing."
        ),
    )

    # Export failed nets
    parser.add_argument(
        "--export-failed-nets",
        metavar="PATH",
        help=(
            "Export failed (unrouted) net names to a file, one per line. "
            "Useful for scripted workflows or manual completion in KiCad."
        ),
    )

    # Cache arguments
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable routing cache (force fresh routing)",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Only use cached results (fail if cache miss)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear routing cache before routing",
    )
    parser.add_argument(
        "--cache-stats",
        action="store_true",
        help="Show routing cache statistics and exit",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Fail with exit code 6 if output connectivity verification detects "
            "any disconnected net. Without this flag, disconnected nets in the "
            "written output are reported as warnings but do not affect the exit code."
        ),
    )
    parser.add_argument(
        "--show-congestion",
        action="store_true",
        help=(
            "Show pre-route RUDY congestion estimation before routing begins. "
            "Displays an ASCII heatmap of predicted congestion per tile, useful "
            "for diagnosing routing failures caused by congestion hotspots."
        ),
    )

    args = parser.parse_args(argv)

    # Resolve two-phase iteration count.
    # Priority: --two-phase-iterations (explicit) > --iterations (explicit) > 20 (default)
    _TWO_PHASE_DEFAULT = 20
    _two_phase_iters_explicit = getattr(args, "two_phase_iterations", None) is not None
    _iterations_explicitly_set = args.iterations != parser.get_default("iterations")
    if not _two_phase_iters_explicit:
        if _iterations_explicitly_set:
            args.two_phase_iterations = args.iterations
        else:
            args.two_phase_iterations = _TWO_PHASE_DEFAULT

    # Validate input
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Warning: Expected .kicad_pcb file, got {pcb_path.suffix}")

    # Normalize --auto-fix-passes: explicit value implies --auto-fix
    if args.auto_fix_passes is not None:
        if args.auto_fix_passes < 1:
            print("Error: --auto-fix-passes must be at least 1", file=sys.stderr)
            return 1
        args.auto_fix = True
    else:
        # Default to 3 passes when --auto-fix is used without explicit --auto-fix-passes
        args.auto_fix_passes = 3

    # Issue #2388: --auto-layers is now enabled by default.  When --layers
    # is explicitly set, silently disable auto-escalation so existing
    # users of --layers see no behavior change.  Only raise an error if
    # the user *explicitly* typed both --auto-layers and --layers
    # (a true conflict in intent).
    _argv_for_detect = argv if argv is not None else sys.argv
    explicit_auto_layers = "--auto-layers" in _argv_for_detect
    if args.auto_layers and args.layers != "auto":
        if explicit_auto_layers:
            print(
                f"Error: --auto-layers cannot be used with --layers {args.layers}.\n"
                "Use --auto-layers alone, or use --layers to specify a "
                "fixed layer count (and pass --no-auto-layers to silence "
                "this error).",
                file=sys.stderr,
            )
            return 1
        # --layers was explicit but --auto-layers was the default; honor --layers.
        args.auto_layers = False

    # Validate --adaptive-rules is not used with explicit --layers (unless also using --auto-layers)
    if args.adaptive_rules and args.layers != "auto" and not args.auto_layers:
        print(
            f"Error: --adaptive-rules cannot be used with --layers {args.layers}.\n"
            "Use --adaptive-rules alone, with --auto-layers, or use --layers for fixed config.",
            file=sys.stderr,
        )
        return 1

    # Validate min-completion is between 0 and 1
    if args.min_completion < 0 or args.min_completion > 1:
        print(
            f"Error: --min-completion must be between 0 and 1 (got {args.min_completion}).",
            file=sys.stderr,
        )
        return 1

    # Apply high-performance settings if requested
    if getattr(args, "high_performance", False):
        from kicad_tools.performance import get_performance_config

        perf_config = get_performance_config(high_performance=True)

        # Override defaults with high-performance settings
        if not args.quiet:
            print("\n--- High-Performance Mode ---")
            print(f"  CPU cores:         {perf_config.cpu_cores}")
            print(f"  Monte Carlo trials: {perf_config.monte_carlo_trials}")
            print(f"  Parallel workers:   {perf_config.parallel_workers}")
            print(f"  Max iterations:     {perf_config.negotiated_iterations}")
            if perf_config.calibrated:
                print(f"  (Using calibrated settings from {perf_config.calibration_date})")
            print()

        # Apply to routing parameters
        args.mc_trials = perf_config.monte_carlo_trials
        args.iterations = perf_config.negotiated_iterations
        # Also apply calibrated iterations to two-phase if not explicitly set
        if not _two_phase_iters_explicit:
            args.two_phase_iterations = perf_config.negotiated_iterations

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_routed")

    # Resolve grid value: "auto" or numeric
    # We need to resolve this early, before sub-functions are called
    grid_auto_result = None
    multi_res_plan = None
    if args.grid.lower() == "auto":
        from kicad_tools.router.io import (
            auto_select_grid_resolution,
            compute_multi_resolution_plan,
            extract_board_dimensions,
            extract_pad_positions,
        )

        if not args.quiet:
            print("\n--- Auto-selecting grid resolution ---")
        pad_positions = extract_pad_positions(pcb_path)
        board_dims = extract_board_dimensions(pcb_path)
        board_width = board_dims[0] if board_dims else None
        board_height = board_dims[1] if board_dims else None
        grid_auto_result = auto_select_grid_resolution(
            pads=pad_positions,
            clearance=args.clearance,
            board_width=board_width,
            board_height=board_height,
        )

        # When grid_strategy is adaptive (default), attempt multi-resolution
        grid_strategy = getattr(args, "grid_strategy", "adaptive")
        if grid_strategy == "adaptive":
            # compute_multi_resolution_plan needs full Pad objects (with ref)
            # Try loading them; fall back to uniform if not available
            try:
                from kicad_tools.router.io import load_pads_for_analysis

                full_pads = load_pads_for_analysis(pcb_path)
                multi_res_plan = compute_multi_resolution_plan(
                    pads=full_pads,
                    clearance=args.clearance,
                    board_width=board_width,
                    board_height=board_height,
                )
            except Exception:
                # Fall back: try with pad positions (won't have ref info)
                multi_res_plan = compute_multi_resolution_plan(
                    pads=pad_positions,
                    clearance=args.clearance,
                    board_width=board_width,
                    board_height=board_height,
                )

        if multi_res_plan is not None and multi_res_plan.is_multi_resolution:
            # Use coarse resolution for the global grid
            args.grid = multi_res_plan.coarse_resolution
            if not args.quiet:
                print(multi_res_plan.summary())
                print()
        else:
            # No fine-pitch components or uniform strategy requested
            multi_res_plan = None
            args.grid = grid_auto_result.resolution
            if not args.quiet:
                print(grid_auto_result.summary())
                print()

        # Store grid origin offset from auto-selection for DesignRules
        args._grid_origin_offset = grid_auto_result.origin_offset
    else:
        try:
            args.grid = float(args.grid)
        except ValueError:
            print(
                f"Error: Invalid grid value '{args.grid}'. Use a number (e.g., 0.25) or 'auto'.",
                file=sys.stderr,
            )
            return 1

    # Handle cache-related commands early
    if args.cache_stats:
        from kicad_tools.router import RoutingCache

        cache = RoutingCache()
        stats = cache.stats()
        print("\n--- Routing Cache Statistics ---")
        print(f"  Cache directory:     {stats['cache_dir']}")
        print(f"  Routing results:     {stats['routing_results_count']}")
        print(f"  Partial net routes:  {stats['partial_routes_count']}")
        print(f"  Total size:          {stats['total_size_mb']:.2f} MB")
        print(f"  Valid results:       {stats['valid_results']}")
        print(f"  Expired results:     {stats['expired_results']}")
        print(f"  TTL:                 {stats['ttl_days']} days")
        print(f"  Max size:            {stats['max_size_mb']:.0f} MB")
        if stats["oldest"]:
            print(f"  Oldest entry:        {stats['oldest']}")
        if stats["newest"]:
            print(f"  Newest entry:        {stats['newest']}")
        return 0

    if args.clear_cache:
        from kicad_tools.router import RoutingCache

        cache = RoutingCache()
        count = cache.clear()
        if not args.quiet:
            print(f"Cleared {count} entries from routing cache")

    # Auto-apply edge clearance from manufacturer when not explicitly set.
    # This ensures --manufacturer jlcpcb automatically enforces the 0.3mm
    # copper-to-edge clearance without requiring a separate --edge-clearance flag.
    if args.edge_clearance is None:
        from kicad_tools.router.mfr_limits import get_mfr_limits

        try:
            _mfr = get_mfr_limits(args.manufacturer)
            if _mfr.min_edge_clearance > 0:
                args.edge_clearance = _mfr.min_edge_clearance
                if not args.quiet:
                    print(
                        f"Edge clearance: {args.edge_clearance}mm "
                        f"(from {args.manufacturer} manufacturer limits)"
                    )
        except ValueError:
            pass  # Unknown manufacturer -- edge_clearance stays None

    # Handle auto-layers mode (separate code path)
    if args.auto_layers and args.adaptive_rules:
        # Combined 2D search: layers + rules
        return route_with_combined_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )
    elif args.auto_layers:
        return route_with_layer_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )
    elif args.adaptive_rules:
        # Adaptive rules only (fixed layer count)
        return route_with_rule_relaxation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Auto-create copper pours for power nets (before skip detection)
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        auto_pour_if_missing(pcb_path, quiet=args.quiet)

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=args.quiet)

    # Import router modules
    from kicad_tools.analysis import ComplexityAnalyzer, ComplexityRating
    from kicad_tools.router import (
        BusRoutingConfig,
        BusRoutingMode,
        DesignRules,
        DifferentialPairConfig,
        LayerStack,
        RoutabilityAnalyzer,
        is_cpp_available,
        load_pcb_for_routing,
        print_routing_diagnostics_json,
        show_routing_summary,
    )
    from kicad_tools.router.io import detect_layer_stack
    from kicad_tools.schema.pcb import PCB

    # Handle backend selection
    force_python = False
    if args.backend == "cpp":
        if not is_cpp_available():
            print(
                "Error: C++ backend requested but not available.\n"
                "Build the C++ extension or use --backend auto/python.\n"
                "See README for build instructions.",
                file=sys.stderr,
            )
            return 1
    elif args.backend == "python":
        force_python = True

    # Grid resolution already resolved early in main()
    # (args.grid is now a float, grid_auto_result set if "auto" was used)

    # Validate grid resolution vs clearance (prevents DRC violations)
    # Skip validation for auto mode since auto_select_grid_resolution ensures DRC compliance
    if grid_auto_result is None and args.grid > args.clearance:
        recommended_grid = args.clearance / 2
        if not args.force:
            print(
                f"Error: Grid resolution {args.grid}mm exceeds clearance {args.clearance}mm.\n"
                f"This WILL cause DRC violations.\n\n"
                f"Options:\n"
                f"  1. Use a finer grid: --grid {recommended_grid}\n"
                f"  2. Use --grid auto for automatic selection\n"
                f"  3. Use --force to override (not recommended)\n",
                file=sys.stderr,
            )
            return 1
        else:
            # User forced, continue with warning
            print(
                f"Warning: Grid resolution {args.grid}mm exceeds clearance {args.clearance}mm.\n"
                f"Proceeding anyway due to --force flag. Expect DRC violations.",
                file=sys.stderr,
            )

    # Create layer stack from --layers argument (or auto-detect)
    if args.layers == "auto":
        # Auto-detect layer stack from PCB file
        pcb_text = pcb_path.read_text()
        layer_stack = detect_layer_stack(pcb_text)
    else:
        layer_stack_map = {
            "2": LayerStack.two_layer(),
            "4": LayerStack.four_layer_sig_gnd_pwr_sig(),
            "4-sig": LayerStack.four_layer_sig_sig_gnd_pwr(),
            "4-all": LayerStack.four_layer_all_signal(),
            "6": LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig(),
        }
        layer_stack = layer_stack_map[args.layers]

    # Configure design rules
    grid_origin_offset = getattr(args, "_grid_origin_offset", (0.0, 0.0))
    fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
    rules = DesignRules(
        grid_resolution=args.grid,
        grid_origin_offset=grid_origin_offset,
        trace_width=args.trace_width,
        trace_clearance=args.clearance,
        via_drill=args.via_drill,
        via_diameter=args.via_diameter,
        fine_pitch_clearance=fine_pitch_cl,
    )

    # Import progress helpers
    from kicad_tools.cli.progress import flush_print, spinner

    quiet = args.quiet

    # Print header (unless quiet)
    if not quiet:
        print("=" * 60)
        print("KiCad PCB Autorouter")
        print("=" * 60)
        print(f"Input:    {pcb_path}")
        print(f"Output:   {output_path}")
        print(f"Strategy: {args.strategy}")
        print(f"Layers:   {layer_stack.name} ({layer_stack.num_layers} layers)")
        if skip_nets:
            print(f"Skip:     {', '.join(skip_nets)}")
        if args.bus_routing:
            print(f"Bus:      enabled ({args.bus_mode} mode)")
        if args.differential_pairs:
            print("DiffPair: enabled")

        if args.edge_clearance:
            print(f"Edge:     {args.edge_clearance}mm clearance")
        if args.verbose:
            print("\nDesign Rules:")
            grid_mode = " (auto)" if grid_auto_result else ""
            print(f"  Grid resolution: {rules.grid_resolution}mm{grid_mode}")
            print(f"  Trace width:     {rules.trace_width}mm")
            print(f"  Clearance:       {rules.trace_clearance}mm")
            print(f"  Via drill:       {rules.via_drill}mm")
            print(f"  Via diameter:    {rules.via_diameter}mm")
            if args.edge_clearance:
                print(f"  Edge clearance:  {args.edge_clearance}mm")

            print(f"\nLayer Stack ({layer_stack.name}):")
            signal_layers = [lyr.name for lyr in layer_stack.signal_layers]
            plane_layers = [f"{lyr.name} ({lyr.plane_net})" for lyr in layer_stack.plane_layers]
            print(f"  Signal layers:  {', '.join(signal_layers)}")
            if plane_layers:
                print(f"  Plane layers:   {', '.join(plane_layers)}")

    # Load PCB
    if not quiet:
        flush_print("\n--- Loading PCB ---")
    try:
        with spinner("Loading PCB...", quiet=quiet):
            router, net_map = load_pcb_for_routing(
                str(pcb_path),
                skip_nets=skip_nets,
                rules=rules,
                edge_clearance=args.edge_clearance,
                layer_stack=layer_stack,
                force_python=force_python,
                validate_drc=not args.force,
                strict_drc=False,  # Only fail on hard constraint (grid > clearance)
            )
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Pass fine zones from multi-resolution plan to the router (Issue #1828).
    # This enables SubGridRouter to use fine-grid resolution for escape
    # routing of pads within dense IC packages (e.g. SSOP at 0.05mm)
    # instead of the coarse global grid (e.g. 0.17mm).
    if multi_res_plan is not None and multi_res_plan.is_multi_resolution:
        router.fine_zones = list(multi_res_plan.fine_zones)
        if not quiet:
            flush_print(
                f"  Fine zones: {len(router.fine_zones)} "
                f"(sub-grid escape routing enabled)"
            )

    # Issue #1841: Tell the autorouter which pour nets lack zones
    router._pour_nets_without_zones = set(_no_zone)

    # Set up Ctrl+C handling to save partial results
    _interrupt_state["router"] = router
    _interrupt_state["output_path"] = output_path
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["interrupted"] = False
    signal.signal(signal.SIGINT, _handle_interrupt)

    # Count nets by category for accurate status reporting (Issue #812)
    # - Multi-pad nets: 2+ pads, need actual routing
    # - Single-pad nets: 1 pad, trivially complete (no routing needed)
    # - Power nets: skipped via skip_nets, handled by copper pours
    multi_pad_nets = []
    single_pad_nets = []
    for net_num, pads in router.nets.items():
        if net_num > 0:  # Skip net 0 (unconnected)
            if len(pads) >= 2:
                multi_pad_nets.append(net_num)
            elif len(pads) == 1:
                single_pad_nets.append(net_num)
    nets_to_route = len(multi_pad_nets)  # Only multi-pad nets need routing
    power_nets_skipped = len(skip_nets)

    if not quiet:
        flush_print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
        backend_info = router.backend_info
        grid_cells = router.grid.cols * router.grid.rows * router.grid.num_layers
        from kicad_tools.router.cpp_backend import format_backend_status

        backend_status = format_backend_status(backend_info, grid_cells)
        flush_print(f"  Backend:    {backend_status}")
        flush_print(
            f"  Grid:       {router.grid.cols}x{router.grid.rows}x{router.grid.num_layers} = {grid_cells:,} cells"
        )
        flush_print(f"  Total nets: {len(net_map)}")
        flush_print(f"  Nets to route: {nets_to_route} (multi-pad signal nets)")

        if args.verbose:
            print("\n  Net breakdown:")
            for net_name, net_num in sorted(net_map.items(), key=lambda x: x[1]):
                if net_name and net_name not in skip_nets:
                    pad_count = len(router.nets.get(net_num, []))
                    print(f"    {net_name}: {pad_count} pads")

    # Analyze fine-pitch components for grid compatibility warnings
    # This runs automatically to warn users about potential routing issues
    if not quiet:
        from kicad_tools.router.fine_pitch import analyze_fine_pitch_components
        from kicad_tools.router.output import show_fine_pitch_warnings

        fine_pitch_report = analyze_fine_pitch_components(
            pads=router.pads,
            grid_resolution=args.grid,
            trace_width=args.trace_width,
            clearance=args.clearance,
        )
        if fine_pitch_report.has_warnings:
            if router.use_waypoint_injection:
                # Waypoint injection handles off-grid pads by injecting their
                # exact positions into the A* search graph, so the grid-alignment
                # warnings are misleading.  Show a brief summary instead.
                if fine_pitch_report.total_off_grid > 0:
                    flush_print(
                        f"\n  {fine_pitch_report.total_off_grid} pads off-grid; "
                        "waypoint injection will handle pad connections"
                    )
                # Still show full per-component detail at verbose (-v)
                if args.verbose:
                    flush_print("\n--- Fine-Pitch Component Analysis (verbose) ---")
                    show_fine_pitch_warnings(
                        fine_pitch_report, quiet=quiet, verbose=True
                    )
            else:
                flush_print("\n--- Fine-Pitch Component Analysis ---")
                show_fine_pitch_warnings(
                    fine_pitch_report, quiet=quiet, verbose=args.verbose
                )

    # Show pre-route RUDY congestion estimation (Issue #2278)
    if getattr(args, "show_congestion", False):
        try:
            estimator = router._ensure_congestion_estimator()
            if not quiet:
                flush_print("\n--- Pre-Route Congestion Estimation (RUDY) ---")
            if args.format == "json":
                import json as _json

                print(_json.dumps(estimator.format_json(), indent=2))
            else:
                print(estimator.format_ascii_heatmap())
                # Summary stats
                max_demand = max(
                    (estimator.demand[r][c]
                     for r in range(estimator.grid.rows)
                     for c in range(estimator.grid.cols)),
                    default=0.0,
                )
                scored_nets = [
                    (nid, s) for nid, s in estimator.net_scores.items() if s > 0
                ]
                scored_nets.sort(key=lambda x: -x[1])
                print(f"\n  Peak tile demand: {max_demand:.2f}")
                print(f"  Nets with congestion score: {len(scored_nets)}")
                if scored_nets and not quiet:
                    print("  Top congested nets:")
                    for nid, score in scored_nets[:5]:
                        name = router.net_names.get(nid, f"Net {nid}")
                        print(f"    {name}: {score:.3f}")
        except Exception as e:
            if not quiet:
                print(f"  Warning: Congestion estimation failed: {e}", file=sys.stderr)

    # Analyze routability if requested
    if args.analyze:
        # Run pre-routing complexity analysis first
        if not quiet:
            print("\n--- Pre-Routing Complexity Analysis ---")
        try:
            pcb_for_analysis = PCB.load(str(pcb_path))
            complexity_analyzer = ComplexityAnalyzer()
            complexity = complexity_analyzer.analyze(pcb_for_analysis)

            # Show complexity summary
            print(f"\n{'=' * 60}")
            print("COMPLEXITY ANALYSIS")
            print(f"{'=' * 60}")
            print(f"Board: {complexity.board_width_mm:.1f}mm x {complexity.board_height_mm:.1f}mm")
            print(f"Pads: {complexity.total_pads}, Nets: {complexity.total_nets}")

            # Show complexity rating with color
            rating_symbols = {
                ComplexityRating.TRIVIAL: "[TRIVIAL]",
                ComplexityRating.SIMPLE: "[SIMPLE]",
                ComplexityRating.MODERATE: "[MODERATE]",
                ComplexityRating.COMPLEX: "[COMPLEX]",
                ComplexityRating.EXTREME: "[EXTREME]",
            }
            print(
                f"Complexity: {complexity.overall_score:.0f}/100 - "
                f"{rating_symbols[complexity.complexity_rating]}"
            )

            # Show layer predictions
            print("\nLayer Predictions:")
            for pred in complexity.layer_predictions:
                rec_str = " (recommended)" if pred.recommended else ""
                print(
                    f"  {pred.layer_count} layers: {pred.success_probability * 100:.0f}% success{rec_str}"
                )

            # Show bottlenecks
            if complexity.bottlenecks:
                print(f"\nBottlenecks ({len(complexity.bottlenecks)}):")
                for bottleneck in complexity.bottlenecks[:3]:
                    print(f"  - {bottleneck.component_ref}: {bottleneck.description}")

            print(f"{'=' * 60}")
        except Exception as e:
            print(f"Warning: Complexity analysis failed: {e}", file=sys.stderr)

        if not quiet:
            print("\n--- Routability Analysis ---")
        try:
            analyzer = RoutabilityAnalyzer(router)
            report = analyzer.analyze()

            # Print analysis report
            print(f"\n{'=' * 60}")
            print("ROUTABILITY ANALYSIS")
            print(f"{'=' * 60}")
            print(
                f"Estimated completion: {report.estimated_success_rate * 100:.0f}% "
                f"({report.expected_routable}/{report.total_nets} nets)"
            )

            # Show layer utilization
            if report.layer_utilization:
                print("\nLayer Utilization:")
                for layer_name, util in report.layer_utilization.items():
                    bar = "#" * int(util * 20)
                    print(f"  {layer_name:10s}: [{bar:20s}] {util * 100:.0f}%")

            # Show problem nets
            if report.problem_nets:
                print(f"\nProblem Nets ({len(report.problem_nets)}):")
                for net_report in report.problem_nets[:10]:  # Show first 10
                    print(f"\n  {net_report.net_name} ({net_report.pad_count} pads):")
                    print(f"    Severity: {net_report.severity.name}")
                    print(f"    Difficulty: {net_report.difficulty_score:.0f}/100")
                    if net_report.blocking_obstacles:
                        print("    Blocked by:")
                        for obs in net_report.blocking_obstacles[:5]:
                            print(f"      - {obs}")
                    if net_report.alternatives:
                        print("    Alternatives:")
                        for alt in net_report.alternatives[:3]:
                            print(f"      {alt}")
                    if net_report.suggestions:
                        print("    Suggestions:")
                        for sug in net_report.suggestions:
                            print(f"      - {sug}")

            # Show recommendations
            if report.recommendations:
                print("\nRecommendations:")
                for i, rec in enumerate(report.recommendations, 1):
                    print(f"  {i}. {rec}")

            print(f"{'=' * 60}")

            # If just analyzing, exit here
            if args.dry_run:
                return 0

        except Exception as e:
            print(f"Warning: Analysis failed: {e}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    # Configure bus routing if enabled
    bus_config = None
    if args.bus_routing:
        bus_mode_map = {
            "parallel": BusRoutingMode.PARALLEL,
            "stacked": BusRoutingMode.STACKED,
            "bundled": BusRoutingMode.BUNDLED,
        }
        bus_config = BusRoutingConfig(
            enabled=True,
            mode=bus_mode_map[args.bus_mode],
            spacing=args.bus_spacing,
            min_bus_width=args.bus_min_width,
        )

        # Show detected buses
        if args.verbose and not quiet:
            analysis = router.get_bus_analysis()
            if analysis["total_groups"] > 0:
                print(f"\n  Detected {analysis['total_groups']} bus groups:")
                for group in analysis["groups"]:
                    status = "complete" if group["complete"] else "partial"
                    print(f"    - {group['name']}: {group['width']} bits ({status})")
            else:
                print("\n  No bus signals detected")

    # Configure differential pair routing if enabled
    diffpair_config = None
    diffpair_warnings = []
    if args.differential_pairs:
        diffpair_config = DifferentialPairConfig(
            enabled=True,
            spacing=args.diffpair_spacing,
            max_length_delta=args.diffpair_max_delta,
        )

        # Show detected differential pairs
        if args.verbose and not quiet:
            analysis = router.analyze_differential_pairs()
            if analysis["total_pairs"] > 0:
                print(f"\n  Detected {analysis['total_pairs']} differential pairs:")
                for pair in analysis["pairs"]:
                    print(
                        f"    - {pair['name']}: {pair['type']} "
                        f"(spacing={pair['spacing']}mm, max_delta={pair['max_delta']}mm)"
                    )
                if analysis["unpaired"]:
                    print(f"\n  Unpaired differential signals: {analysis['unpaired_signals']}")
                    for sig in analysis["unpaired"]:
                        print(f"    - {sig['net_name']} ({sig['polarity']})")
            else:
                print("\n  No differential pairs detected")

    # Check cache for existing routing result (unless --no-cache)
    cache_key = None
    cached_result = None
    use_cache = not args.no_cache

    if use_cache:
        from kicad_tools.router import CacheKey, RoutingCache

        try:
            # Compute cache key from PCB content and rules
            pcb_content = pcb_path.read_bytes()
            cache_key = CacheKey.compute(pcb_content, rules, args.grid)

            cache = RoutingCache()

            if not quiet:
                flush_print("\n--- Checking routing cache ---")

            cached_result = cache.get(cache_key)
            if cached_result is not None:
                if not quiet:
                    print(f"  Cache HIT: {cached_result.success_count} nets routed")
                    print(
                        f"  Segments: {cached_result.total_segments}, Vias: {cached_result.total_vias}"
                    )
                    print(f"  Original compute time: {cached_result.compute_time_ms}ms")

                # Deserialize and apply cached routes
                cached_routes = cache.deserialize_routes(cached_result.routes_data)

                # Apply cached routes to router
                router.routes = cached_routes

                if not quiet:
                    print("  Using cached routing result")
            else:
                if not quiet:
                    print(f"  Cache MISS (key: {cache_key.full_key[:32]}...)")
                if args.cache_only:
                    print(
                        "Error: --cache-only specified but no cached result found", file=sys.stderr
                    )
                    return 1
        except Exception as e:
            if not quiet:
                print(f"  Cache error: {e}")
            cached_result = None
            if args.cache_only:
                print("Error: --cache-only specified but cache lookup failed", file=sys.stderr)
                return 1

    # Track nets that needed clearance relaxation (for --progressive-clearance)
    relaxed_nets_report: dict[int, float] = {}
    routing_start_time = None

    # Route (skip if using cached result)
    if cached_result is not None:
        # Skip routing - using cached result
        if not quiet:
            flush_print(f"\n--- Using cached result (skipping routing) ---")
    else:
        # Route
        if not quiet:
            flush_print(f"\n--- Routing ({args.strategy}) ---")
            if args.timeout:
                flush_print(f"  Timeout: {args.timeout}s")
            per_net_timeout_val = getattr(args, "per_net_timeout", None)
            if per_net_timeout_val:
                flush_print(f"  Per-net timeout: {per_net_timeout_val}s")
            if args.profile:
                profile_output = args.profile_output or "route_profile.prof"
                flush_print(f"  Profiling enabled: {profile_output}")

        import time

        routing_start_time = time.time()

        # Resolve escape routing flag: True=force on, False=force off, None=auto-detect
        escape_routing_flag = _resolve_escape_routing_flag(args)

        # Define routing function for profiling
        def do_routing():
            nonlocal diffpair_warnings, relaxed_nets_report

            # Adaptive multi-resolution routing (when --grid auto selects it)
            if multi_res_plan is not None and multi_res_plan.is_multi_resolution:
                from kicad_tools.router.adaptive_grid import AdaptiveGridRouter

                if not quiet:
                    flush_print("  Using adaptive multi-resolution grid strategy")
                adaptive_router = AdaptiveGridRouter(
                    grid=router.grid,
                    rules=rules,
                    router=router,
                )

                # Build nets dict (filter to routable multi-pad nets)
                adaptive_nets = {
                    net_id: pad_keys
                    for net_id, pad_keys in router.nets.items()
                    if net_id > 0 and len(pad_keys) >= 2
                }
                adaptive_pads = router.pads

                # Define the Phase 2 routing function
                def phase2_route_fn():
                    if getattr(args, "two_phase", False) and args.strategy == "negotiated":
                        return router.route_all_two_phase(
                            use_negotiated=True,
                            corridor_width_factor=2.0,
                            timeout=args.timeout,
                            per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                            max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                        )
                    elif args.strategy == "negotiated":
                        return router.route_all_negotiated(
                            max_iterations=args.iterations,
                            timeout=args.timeout,
                            per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                            batch_routing=getattr(args, "batch_routing", False)
                            or getattr(args, "high_performance", False),
                            hierarchical=getattr(args, "hierarchical", False),
                            perturbation=getattr(args, "perturbation", True),
                        )
                    else:
                        return router.route_all()

                adaptive_result = adaptive_router.route_adaptive(
                    nets=adaptive_nets,
                    pads=adaptive_pads,
                    route_fn=phase2_route_fn,
                )

                if not quiet:
                    flush_print(f"\n{adaptive_result.format_summary()}")

                return adaptive_result.all_routes

            # Check if escape routing should run as a pre-phase
            if _should_use_escape_routing(router, escape_routing_flag, quiet):
                return router.route_with_escape(
                    use_negotiated=(args.strategy == "negotiated"),
                    timeout=args.timeout,
                )

            # Progressive clearance relaxation mode
            if getattr(args, "progressive_clearance", False):
                routes, relaxed_nets_report = router.route_with_progressive_clearance(
                    min_clearance=getattr(args, "min_clearance", None),
                    num_relaxation_levels=getattr(args, "relaxation_levels", 3),
                    max_iterations=args.iterations,
                    timeout=args.timeout,
                )
                return routes
            elif getattr(args, "multi_resolution", False):
                return router.route_all_multi_resolution(
                    use_negotiated=(args.strategy == "negotiated"),
                    max_iterations=args.iterations,
                    timeout=args.timeout,
                )
            elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                return router.route_all_two_phase(
                    use_negotiated=True,
                    corridor_width_factor=2.0,
                    timeout=args.timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                )
            elif args.strategy == "negotiated":
                return router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=args.timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    batch_routing=getattr(args, "batch_routing", False)
                    or getattr(args, "high_performance", False),
                    hierarchical=getattr(args, "hierarchical", False),
                    perturbation=getattr(args, "perturbation", True),
                )
            elif args.differential_pairs and args.strategy == "basic":
                result, diffpair_warnings = router.route_all_with_diffpairs(diffpair_config)
                return result
            elif args.bus_routing and args.strategy == "basic":
                return router.route_all_with_buses(bus_config)
            elif args.strategy == "basic":
                return router.route_all()
            elif args.strategy == "monte-carlo":
                return router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
            return None

        try:
            if args.profile:
                # Profile the routing operation
                import cProfile
                import pstats

                profile_output = args.profile_output or "route_profile.prof"
                profiler = cProfile.Profile()
                profiler.enable()
                try:
                    _ = do_routing()
                finally:
                    profiler.disable()
                    # Save profile data
                    profiler.dump_stats(profile_output)
                    if not quiet:
                        print(f"\n  Profile saved to: {profile_output}")
                        # Print top 20 functions by cumulative time
                        print("\n--- Profile Summary (top 20 by cumulative time) ---")
                        stats = pstats.Stats(profiler)
                        stats.strip_dirs().sort_stats("cumulative").print_stats(20)
            else:
                # Normal routing without profiling
                if args.strategy == "negotiated":
                    # Negotiated routing has its own progress output - don't use spinner
                    _ = do_routing()
                else:
                    with spinner(f"Routing {nets_to_route} nets...", quiet=quiet):
                        _ = do_routing()
        except KeyboardInterrupt:
            # Handle any KeyboardInterrupt that wasn't caught by signal handler
            _interrupt_state["interrupted"] = True
            if not quiet:
                print("\n\n⚠ Routing interrupted!")
        except Exception as e:
            # Provide actionable guidance when escape routing detected a
            # doomed coarse grid (issue #2387).
            try:
                from kicad_tools.router.adaptive_grid import (
                    FinePitchEscapeFailure,
                )
            except Exception:
                FinePitchEscapeFailure = None  # type: ignore[assignment]
            if (
                FinePitchEscapeFailure is not None
                and isinstance(e, FinePitchEscapeFailure)
            ):
                print(
                    f"Error during routing: {e}",
                    file=sys.stderr,
                )
                print(
                    f"  Suggested fix: rerun with --grid {e.suggested_grid:.4f}",
                    file=sys.stderr,
                )
            else:
                print(f"Error during routing: {e}", file=sys.stderr)
            # Still try to save partial results on error
            if router.routes:
                _save_partial_results()
            return 1

        # Check if interrupted and save partial results
        if _interrupt_state["interrupted"]:
            _save_partial_results()
            return 5  # Exit code 5 indicates interruption with partial results saved

        # Cache the routing result (if caching enabled and routing succeeded)
        if use_cache and cache_key is not None and router.routes:
            import time

            try:
                routing_time_ms = (
                    int((time.time() - routing_start_time) * 1000) if routing_start_time else 0
                )
                stats = router.get_statistics()
                cache.put(cache_key, router.routes, stats, routing_time_ms)
                if not quiet:
                    print(f"  Cached routing result ({routing_time_ms}ms compute time)")
            except Exception as e:
                if not quiet:
                    print(f"  Warning: Failed to cache result: {e}")

    # Get pre-optimization statistics (also used in the no-optimize path
    # below so the segment/via summary print does not raise
    # UnboundLocalError when --no-optimize is set).
    pre_segments = sum(len(r.segments) for r in router.routes)
    pre_vias = sum(len(r.vias) for r in router.routes)

    # Optimize traces (unless --no-optimize/--raw flag is set)
    if not args.no_optimize and router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        # Configure and run optimizer
        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.
        has_overflow = router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(
            router.grid, ignore_overflow=has_overflow
        )
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        with spinner("Optimizing traces...", quiet=quiet):
            optimized_routes = []
            for route in router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            router.routes = optimized_routes

    # Post-optimization DRC nudge pass
    if router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

        # Get post-optimization statistics
        post_segments = sum(len(r.segments) for r in router.routes)
        post_vias = sum(len(r.vias) for r in router.routes)

        if not quiet:
            segment_reduction = (
                ((pre_segments - post_segments) / pre_segments * 100) if pre_segments > 0 else 0
            )
            via_reduction = ((pre_vias - post_vias) / pre_vias * 100) if pre_vias > 0 else 0
            print(f"  Segments: {pre_segments} -> {post_segments} ({-segment_reduction:+.1f}%)")
            if pre_vias > 0:
                print(f"  Vias:     {pre_vias} -> {post_vias} ({-via_reduction:+.1f}%)")

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    multi_pad_net_ids = set(multi_pad_nets)
    route_sexp, stats, cleanup_stats = _finalize_routes(
        router,
        multi_pad_net_ids,
        nets_to_route,
        quiet=quiet,
    )

    # Report differential pair length mismatch warnings
    if diffpair_warnings and not quiet:
        print(f"\n--- Differential Pair Warnings ({len(diffpair_warnings)}) ---")
        for warning in diffpair_warnings:
            print(f"  {warning}")

    # Report nets that needed clearance relaxation (--progressive-clearance mode)
    if relaxed_nets_report and not quiet:
        original_clearance = rules.trace_clearance
        print(f"\n--- Clearance Relaxation Report ({len(relaxed_nets_report)} nets) ---")
        print(f"  Original clearance: {original_clearance:.3f}mm")
        for net_id, clearance in sorted(relaxed_nets_report.items(), key=lambda x: x[1]):
            net_name = router.net_names.get(net_id, f"Net {net_id}")
            reduction = (1 - clearance / original_clearance) * 100
            print(f"  {net_name}: {clearance:.3f}mm ({reduction:.0f}% relaxation)")

    # Show preview if requested
    if args.preview:
        response = show_preview(
            router,
            net_map,
            nets_to_route,
            quiet=quiet,
            nets_to_route_ids=multi_pad_net_ids,
        )
        if response != "y":
            if not quiet:
                print("\nRouting cancelled. No changes saved.")
            return 0

    # Generate power zones if requested
    zone_sexp = ""
    if args.power_nets:
        from kicad_tools.zones import ZoneGenerator, parse_power_nets

        try:
            power_nets = parse_power_nets(args.power_nets)
        except ValueError as e:
            print(f"Error parsing power-nets: {e}", file=sys.stderr)
            return 1

        if power_nets and not quiet:
            print("\n--- Generating copper zones ---")
            print(f"  Power nets: {', '.join(f'{n}:{l}' for n, l in power_nets)}")

        if power_nets:
            try:
                gen = ZoneGenerator.from_pcb(str(pcb_path))
                for net_name, layer in power_nets:
                    # GND gets higher priority (fills last, on top)
                    priority = 1 if net_name.upper() in ("GND", "GNDA", "GNDD") else 0
                    try:
                        gen.add_zone(
                            net=net_name,
                            layer=layer,
                            priority=priority,
                        )
                        if not quiet:
                            print(f"    Added zone: {net_name} on {layer} (priority {priority})")
                    except ValueError as e:
                        print(f"  Warning: Could not add zone for {net_name}: {e}")

                zone_sexp = gen.generate_sexp()
            except Exception as e:
                print(f"  Warning: Zone generation failed: {e}")

    # Pre-save clearance validation
    # Issue #1666: Segment-to-segment violations now cause a non-zero exit
    # code so that CI pipelines and DRC workflows can detect the failure.
    seg_seg_violation_count = 0
    if stats["nets_routed"] > 0 and not args.dry_run:
        from kicad_tools.router.io import format_clearance_violations, validate_routes

        clearance_violations = validate_routes(router)
        if clearance_violations:
            seg_seg_violation_count = sum(
                1
                for v in clearance_violations
                if v.obstacle_type == "segment" and not v.component_inherent
            )
            if not quiet:
                print("\n--- Pre-save Clearance Validation ---")
                if seg_seg_violation_count > 0:
                    print(
                        f"  ERROR: {seg_seg_violation_count} segment-to-segment "
                        f"clearance violation(s) remain after routing"
                    )
                print(f"  {format_clearance_violations(clearance_violations)}")

    # Save output
    output_content = ""  # Tracks written content for output connectivity verification
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
    else:
        if not quiet:
            print("\n--- Saving routed PCB ---")

        with spinner("Saving routed PCB...", quiet=quiet):
            # Read original PCB content
            original_content = pcb_path.read_text()

            # route_sexp was already generated by _finalize_routes() above

            # Insert routes and zones before final closing parenthesis
            # Note: KiCad's S-expression format doesn't support ; comments
            if route_sexp or zone_sexp:
                # Combine zone and route fragments
                fragments = []
                if zone_sexp:
                    fragments.append(zone_sexp)
                if route_sexp:
                    fragments.append(route_sexp)
                combined_sexp = "\n  ".join(fragments)
                output_content = _insert_sexp_before_closing(original_content, combined_sexp)
            else:
                output_content = original_content
                if not quiet:
                    print("  Warning: No routes generated!")

            # Validate S-expression structure before writing
            if not _validate_sexp_parentheses(output_content):
                logger.error("Generated PCB file has unbalanced parentheses")
                raise ValueError(
                    "Generated PCB file has invalid S-expression syntax "
                    "(unbalanced parentheses). This is a bug in kicad-tools. "
                    "Please report it."
                )

            output_path.write_text(output_content)

        if not quiet:
            print(f"  Saved to: {output_path}")

    # Output connectivity verification (Issue #2264)
    # Re-parse written S-expressions and verify pad-to-pad connectivity
    output_has_disconnected = False
    if not args.dry_run and router.pads and router.nets and output_content:
        from kicad_tools.router.io import verify_output_connectivity

        # Build net_pads mapping (same as get_statistics)
        verify_net_pads: dict[int, list] = {}
        for net_id, pad_keys in router.nets.items():
            if net_id not in multi_pad_net_ids:
                continue
            pad_list = [router.pads[k] for k in pad_keys if k in router.pads]
            if len(pad_list) >= 2:
                verify_net_pads[net_id] = pad_list

        # Build net name lookup
        reverse_net_map = {v: k for k, v in net_map.items()}

        output_connectivity = verify_output_connectivity(
            pcb_content=output_content,
            net_pads=verify_net_pads,
            net_names=reverse_net_map,
        )

        disconnected_nets = {
            nid: info
            for nid, info in output_connectivity.items()
            if not info["connected"] and info["total_pads"] >= 2
        }

        if disconnected_nets:
            output_has_disconnected = True
            if not quiet:
                print(f"\n--- Output Connectivity Verification ---")
                print(
                    f"  WARNING: {len(disconnected_nets)} net(s) have disconnected "
                    f"pads in written output"
                )
                for nid, info in sorted(
                    disconnected_nets.items(), key=lambda x: x[1]["net_name"]
                ):
                    disc_str = ", ".join(info["disconnected_pads"][:5])
                    if len(info["disconnected_pads"]) > 5:
                        disc_str += f" (+{len(info['disconnected_pads']) - 5} more)"
                    print(
                        f"  {info['net_name']}: "
                        f"{info['connected_pads']}/{info['total_pads']} pads connected"
                        f" -- disconnected: {disc_str}"
                    )
        else:
            if not quiet:
                print("\n--- Output Connectivity Verification ---")
                print("  All nets verified connected in written output")

    # Run power plane stitching if requested
    stitch_result = None
    if getattr(args, "stitch_power_planes", False) and not args.dry_run:
        from kicad_tools.cli.stitch_cmd import find_all_plane_nets, run_stitch

        if not quiet:
            print("\n--- Stitching Power Planes ---")

        # Load the saved PCB to find plane nets
        from kicad_tools.core.sexp_file import load_pcb as load_stitch_pcb

        stitch_sexp = load_stitch_pcb(output_path)
        plane_nets = find_all_plane_nets(stitch_sexp)

        if plane_nets:
            net_names = list(plane_nets.keys())
            if not quiet:
                print(f"  Found {len(net_names)} power plane nets: {', '.join(sorted(net_names))}")

            stitch_result = run_stitch(
                pcb_path=output_path,
                net_names=net_names,
                via_size=args.via_diameter,  # Use same via size as routing
                drill=args.via_drill,
                clearance=args.clearance,
                dry_run=False,
            )

            if not quiet:
                if stitch_result.vias_added:
                    print(f"  Added {len(stitch_result.vias_added)} stitching vias")
                else:
                    print("  No stitching vias needed (all pads already connected)")
        else:
            if not quiet:
                print("  No power plane nets found (no zones with assigned nets)")

    # Run DRC validation unless skipped or dry-run
    drc_errors = 0
    drc_warnings = 0
    drc_ran = False

    if not args.dry_run and not args.skip_drc and stats["nets_routed"] > 0:
        drc_ran = True
        drc_errors, drc_warnings = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=layer_stack.num_layers,
            quiet=quiet,
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            fix_result = _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
            )
            if fix_result == 0:
                drc_errors = 0

    # Summary
    all_nets_routed = stats["nets_routed"] == nets_to_route
    drc_passed = drc_errors <= 0  # -1 means DRC failed to run, treat as passed
    completion_ratio = stats["nets_routed"] / nets_to_route if nets_to_route > 0 else 1.0
    meets_threshold = completion_ratio >= args.min_completion

    # Build summary suffix for net breakdown (Issue #812)
    summary_parts = []
    if len(single_pad_nets) > 0:
        summary_parts.append(f"{len(single_pad_nets)} single-pad")
    if power_nets_skipped > 0:
        summary_parts.append(f"{power_nets_skipped} power skipped")
    summary_suffix = f" ({', '.join(summary_parts)})" if summary_parts else ""

    if not quiet:
        print("\n" + "=" * 60)
        if all_nets_routed and drc_passed:
            if drc_ran and drc_errors == 0:
                print(f"SUCCESS: All signal nets routed, DRC passed!{summary_suffix}")
            else:
                print(f"SUCCESS: All signal nets routed!{summary_suffix}")
                if not drc_ran and not args.skip_drc and not args.dry_run:
                    print("  Note: Run 'kct check' to validate before manufacturing")
        elif meets_threshold and not all_nets_routed and drc_passed:
            pct = completion_ratio * 100
            print(
                f"SUCCESS: Routed {stats['nets_routed']}/{nets_to_route} signal nets "
                f"({pct:.0f}%, meets {args.min_completion * 100:.0f}% threshold){summary_suffix}"
            )
            if not drc_ran and not args.skip_drc and not args.dry_run:
                print("  Note: Run 'kct check' to validate before manufacturing")
        elif all_nets_routed and not drc_passed:
            print("ROUTING FAILED: DRC violations detected")
            print("=" * 60)
            print()
            print("Net Statistics:")
            print(f"  Multi-pad nets:  {nets_to_route}")
            print(f"  Nets connected:  {stats['nets_routed']} (topologically complete)")
            print("  Nets DRC-clean:  0 (manufacturing blocked)")
            if len(single_pad_nets) > 0 or power_nets_skipped > 0:
                print(f"  Also:{summary_suffix}")
            print()
            print("DRC Summary:")
            print(f"  Violations: {drc_errors}")
            print()
            print("The autorouter connected all nets but violated design rules.")
            print("This board cannot be manufactured without fixing DRC errors.")
            print()
            print("Suggestions:")
            print(f"  - Auto-repair DRC violations: kct fix-drc {output_path} --max-passes 20")
            print(
                f"  - Try Monte Carlo routing: kct route {args.pcb} --strategy monte-carlo --mc-trials 10"
            )
            print("  - Increase board area")
            print("  - Reduce component density")
            print("  - Try 4-layer routing: kct route --layers 4")
            print(f"  - Or re-route with auto-fix: kct route {args.pcb} --auto-fix")
            print()
            print(f"  Run 'kct check {output_path} --mfr {args.manufacturer}' for full details")
        else:
            print(
                f"PARTIAL: Routed {stats['nets_routed']}/{nets_to_route} signal nets{summary_suffix}"
            )
            if drc_ran and drc_errors > 0:
                print(f"  Additionally, {drc_errors} DRC violation(s) detected.")

            # Issue #2388: When the negotiated loop bailed out due to a
            # power-net stall, surface actionable suggestions naming the
            # specific stalled nets and recommended remediation flags.
            if getattr(router, "power_stall_abort", False):
                _print_power_stall_suggestions(
                    list(getattr(router, "power_stall_nets", [])),
                    layer_stack.num_layers,
                    args.pcb,
                )

            # Show comprehensive routing summary with successes, failures, and suggestions
            # Use JSON format if requested
            if args.format == "json":
                print_routing_diagnostics_json(
                    router,
                    net_map,
                    nets_to_route,
                    current_strategy=args.strategy,
                    nets_to_route_ids=multi_pad_net_ids,
                    single_pad_count=len(single_pad_nets),
                )
            else:
                # Verbose mode shows detailed path analysis for each failure
                verbose = args.verbose or args.diagnostics
                show_routing_summary(
                    router,
                    net_map,
                    nets_to_route,
                    quiet=quiet,
                    verbose=verbose,
                    current_strategy=args.strategy,
                    pcb_file=args.pcb,
                    nets_to_route_ids=multi_pad_net_ids,
                    single_pad_count=len(single_pad_nets),
                )

    # Save partial results on clean partial exit (not just SIGINT)
    if not all_nets_routed and not args.dry_run and router.routes:
        partial_saved = _save_partial_results()
        if partial_saved and not quiet:
            print("  Open in KiCad to complete remaining nets manually")

    # Export failed nets to file if requested
    if getattr(args, "export_failed_nets", None) and not all_nets_routed:
        _export_failed_nets(
            router,
            net_map,
            args.export_failed_nets,
            quiet=quiet,
            nets_to_route_ids=multi_pad_net_ids,
        )

    # Exit codes:
    # 0 = Routing meets --min-completion threshold AND (DRC passed OR DRC not run)
    # 1 = Fatal failure — no nets routed, no useful output
    # 2 = Partial routing — some nets routed but below --min-completion threshold
    # 3 = Meets threshold but DRC violations detected (includes seg-seg violations)
    # 4 = Seg-seg clearance violations remain AND routing is below threshold (Issue #1666)
    # 5 = Interrupted by SIGINT with partial results saved (handled in _handle_interrupt)
    # 6 = Output connectivity verification failed (--strict mode only)
    #
    # The --min-completion flag (default 0.95) controls the success threshold.
    # With --min-completion 0.80, routing 85% of nets returns exit code 0.
    completion_ratio = stats["nets_routed"] / nets_to_route if nets_to_route > 0 else 1.0
    meets_threshold = completion_ratio >= args.min_completion

    # --strict: output connectivity verification failure is fatal
    if getattr(args, "strict", False) and output_has_disconnected:
        return 6

    if stats["nets_routed"] == 0 and nets_to_route > 0:
        # Nothing was routed — treat as fatal failure
        return 1
    elif meets_threshold and drc_passed and seg_seg_violation_count == 0:
        return 0
    elif meets_threshold and (not drc_passed or seg_seg_violation_count > 0):
        # Meets completion threshold but has DRC or clearance violations
        return 3
    elif not meets_threshold and seg_seg_violation_count > 0:
        # Below threshold AND has seg-seg clearance violations
        return 4
    else:
        # Partial routing: some nets routed but below threshold
        return 2


if __name__ == "__main__":
    sys.exit(main())
