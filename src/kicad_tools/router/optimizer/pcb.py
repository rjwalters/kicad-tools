"""PCB file parsing and optimization."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..layers import Layer
from ..primitives import Segment
from .config import OptimizationConfig, OptimizationStats
from .geometry import count_corners, total_length

if TYPE_CHECKING:
    pass


def parse_net_names(pcb_text: str) -> dict[int, str]:
    """Parse net ID to name mapping from PCB file."""
    net_names: dict[int, str] = {}

    # Match net declarations: (net N "name")
    pattern = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
    for match in pattern.finditer(pcb_text):
        net_id = int(match.group(1))
        net_name = match.group(2)
        if net_name:  # Skip empty net names
            net_names[net_id] = net_name

    return net_names


def parse_segments(pcb_text: str) -> dict[str, list[Segment]]:
    """Parse segments from PCB file text, grouped by net name."""
    segments_by_net: dict[str, list[Segment]] = {}

    # First, build net ID to name mapping
    net_names = parse_net_names(pcb_text)

    # Match segment S-expressions (multiline format)
    # (segment
    #     (start X Y)
    #     (end X Y)
    #     (width W)
    #     (layer "L")
    #     (net N)
    #     ...
    # )
    pattern = re.compile(
        r"\(segment\s+"
        r"\(start\s+([\d.-]+)\s+([\d.-]+)\)\s*"
        r"\(end\s+([\d.-]+)\s+([\d.-]+)\)\s*"
        r"\(width\s+([\d.]+)\)\s*"
        r'\(layer\s+"([^"]+)"\)\s*'
        r"\(net\s+(\d+)\)",
        re.DOTALL,
    )

    for match in pattern.finditer(pcb_text):
        x1 = float(match.group(1))
        y1 = float(match.group(2))
        x2 = float(match.group(3))
        y2 = float(match.group(4))
        width = float(match.group(5))
        layer_name = match.group(6)
        net = int(match.group(7))
        net_name = net_names.get(net, f"Net{net}")

        # Convert layer name to Layer enum
        try:
            layer = Layer.from_kicad_name(layer_name)
        except ValueError:
            layer = Layer.F_CU  # Default for unknown layers

        seg = Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=width,
            layer=layer,
            net=net,
            net_name=net_name,
        )

        if net_name not in segments_by_net:
            segments_by_net[net_name] = []
        segments_by_net[net_name].append(seg)

    return segments_by_net


def replace_segments(
    pcb_text: str,
    original: dict[str, list[Segment]],
    optimized: dict[str, list[Segment]],
) -> str:
    """Replace original segments with optimized ones in PCB text."""
    result = pcb_text

    # Get net IDs for each net name
    net_ids_to_remove: set[int] = set()
    for net_name, segs in original.items():
        if net_name in optimized and segs:
            net_ids_to_remove.add(segs[0].net)

    # Remove existing segment blocks for nets we optimized
    # Match the multiline segment format:
    # (segment
    #     (start X Y)
    #     ...
    #     (net N)
    #     ...
    # )
    for net_id in net_ids_to_remove:
        pattern = re.compile(
            r"\(segment\s+[^)]*\(net\s+" + str(net_id) + r"\)[^)]*\)\s*", re.DOTALL
        )
        result = pattern.sub("", result)

    # Add optimized segments before the closing parenthesis
    new_segments_sexp = []
    for net_name, segs in optimized.items():
        for seg in segs:
            new_segments_sexp.append(seg.to_sexp())

    if new_segments_sexp:
        # Find the last ) and insert before it
        insert_pos = result.rfind(")")
        if insert_pos > 0:
            indent = "  "
            new_content = "\n" + indent + f"\n{indent}".join(new_segments_sexp) + "\n"
            result = result[:insert_pos] + new_content + result[insert_pos:]

    return result


def _run_drc_error_count(
    pcb_text: str,
    manufacturer: str,
    layers: int,
    copper_oz: float,
) -> int:
    """Run DRC on PCB text and return the error count.

    Writes text to a temporary file, loads it as a PCB object,
    runs clearance and dimension checks, and returns the error count.

    Args:
        pcb_text: Full PCB file text content.
        manufacturer: Manufacturer ID for design rules.
        layers: Number of copper layers.
        copper_oz: Copper weight in oz.

    Returns:
        Number of DRC errors found.
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    with tempfile.NamedTemporaryFile(mode="w", suffix=".kicad_pcb", delete=False) as tmp:
        tmp.write(pcb_text)
        tmp_path = tmp.name

    try:
        pcb = PCB.load(tmp_path)
        checker = DRCChecker(
            pcb,
            manufacturer=manufacturer,
            layers=layers,
            copper_oz=copper_oz,
        )
        # Check clearances and dimensions (the DRC categories relevant to
        # trace optimization -- silkscreen and edge clearance are unaffected
        # by segment reshaping)
        results = checker.check_clearances()
        results.merge(checker.check_dimensions())
        return results.error_count
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def optimize_pcb(
    pcb_path: str,
    output_path: str | None,
    optimize_fn: Callable[[list[Segment]], list[Segment]],
    config: OptimizationConfig,
    net_filter: str | None = None,
    dry_run: bool = False,
) -> OptimizationStats:
    """Optimize traces in a PCB file.

    When ``config.drc_aware`` is True, the optimizer runs DRC before and
    after optimization. Any net whose optimized segments increase the
    total DRC error count is rolled back to its original segments.

    Args:
        pcb_path: Path to input .kicad_pcb file.
        output_path: Path for output file. If None, modifies in place.
        optimize_fn: Function to optimize a list of segments.
        config: Optimization configuration.
        net_filter: Only optimize nets matching this pattern.
        dry_run: If True, calculate stats but don't write output.

    Returns:
        Statistics about the optimization.
    """
    pcb_text = Path(pcb_path).read_text()
    stats = OptimizationStats()

    # Parse existing segments
    segments_by_net = parse_segments(pcb_text)

    # Filter nets if requested
    if net_filter:
        segments_by_net = {
            net: segs for net, segs in segments_by_net.items() if net_filter.lower() in net.lower()
        }

    # Calculate before stats
    for net, segs in segments_by_net.items():
        stats.segments_before += len(segs)
        stats.corners_before += count_corners(segs, config.tolerance)
        stats.length_before += total_length(segs)

    # DRC baseline (when drc_aware is enabled)
    baseline_errors = 0
    if config.drc_aware and config.drc_manufacturer:
        baseline_errors = _run_drc_error_count(
            pcb_text,
            manufacturer=config.drc_manufacturer,
            layers=config.drc_layers,
            copper_oz=config.drc_copper_oz,
        )
        stats.drc_errors_before = baseline_errors

    # Optimize each net
    optimized_segments: dict[str, list[Segment]] = {}
    for net, segs in segments_by_net.items():
        optimized = optimize_fn(segs)
        optimized_segments[net] = optimized
        stats.nets_optimized += 1

    # DRC-aware per-net rollback
    if config.drc_aware and config.drc_manufacturer:
        # Build fully-optimized text and check DRC
        full_optimized_text = replace_segments(pcb_text, segments_by_net, optimized_segments)
        full_errors = _run_drc_error_count(
            full_optimized_text,
            manufacturer=config.drc_manufacturer,
            layers=config.drc_layers,
            copper_oz=config.drc_copper_oz,
        )

        if full_errors > baseline_errors:
            # Some nets made things worse -- try rolling back one net at a time.
            # For each net, test keeping its original segments while all
            # other nets remain optimized.  If reverting a net reduces errors
            # back to (or below) baseline, mark it as rolled back.
            final_segments: dict[str, list[Segment]] = dict(optimized_segments)

            for net in list(optimized_segments.keys()):
                # Skip nets whose optimization did not change the segments
                if optimized_segments[net] == segments_by_net[net]:
                    continue

                # Try reverting this single net
                trial = dict(final_segments)
                trial[net] = segments_by_net[net]
                trial_text = replace_segments(pcb_text, segments_by_net, trial)
                trial_errors = _run_drc_error_count(
                    trial_text,
                    manufacturer=config.drc_manufacturer,
                    layers=config.drc_layers,
                    copper_oz=config.drc_copper_oz,
                )

                if trial_errors < full_errors:
                    # Reverting this net helped -- keep original segments
                    final_segments[net] = segments_by_net[net]
                    stats.nets_rolled_back += 1
                    full_errors = trial_errors

                    # If we are back at or below baseline, stop rolling back
                    if full_errors <= baseline_errors:
                        break

            optimized_segments = final_segments

        stats.drc_errors_after = full_errors

    # Recalculate after stats (may have changed due to rollbacks)
    stats.segments_after = 0
    stats.corners_after = 0
    stats.length_after = 0.0
    for net, segs in optimized_segments.items():
        stats.segments_after += len(segs)
        stats.corners_after += count_corners(segs, config.tolerance)
        stats.length_after += total_length(segs)

    # Generate output (only if not dry run)
    if not dry_run:
        output_text = replace_segments(pcb_text, segments_by_net, optimized_segments)
        out_path = output_path or pcb_path
        Path(out_path).write_text(output_text)

    return stats
