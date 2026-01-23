"""Routing output and diagnostics display utilities.

This module provides shared output functions for displaying routing results,
including successful routes, failed routes, and actionable suggestions.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Autorouter, RoutingFailure
    from .fine_pitch import FinePitchReport
    from .primitives import Route


def show_routing_summary(
    router: Autorouter,
    net_map: dict[str, int],
    nets_to_route: int,
    quiet: bool = False,
    verbose: bool = False,
) -> None:
    """Show comprehensive routing summary with successes, failures, and suggestions.

    Displays:
    - Successfully routed nets with endpoints and trace lengths
    - Failed routes with specific reasons and blocking components
    - Actionable suggestions for improving routing success

    Args:
        router: The Autorouter instance with completed routing
        net_map: Mapping of net names to net IDs
        nets_to_route: Total number of nets that should be routed
        quiet: If True, skip output
        verbose: If True, show detailed path analysis for failures
    """
    if quiet:
        return

    # Build reverse mapping for net names
    reverse_net = {v: k for k, v in net_map.items() if v > 0}

    # Calculate route lengths by net
    route_lengths_by_net: dict[int, float] = {}
    route_vias_by_net: dict[int, int] = {}
    for route in router.routes:
        net_id = route.net
        # Calculate route length
        length_mm = _calculate_route_length(route)
        route_lengths_by_net[net_id] = route_lengths_by_net.get(net_id, 0) + length_mm
        # Track vias
        route_vias_by_net[net_id] = route_vias_by_net.get(net_id, 0) + len(route.vias)

    # Identify routed and unrouted nets
    routed_net_ids = {route.net for route in router.routes}
    all_net_ids = {v for k, v in net_map.items() if v > 0}
    unrouted_ids = all_net_ids - routed_net_ids

    # Group recorded failures by net
    failures_by_net: dict[int, list[RoutingFailure]] = {}
    for failure in getattr(router, "routing_failures", []):
        if failure.net not in failures_by_net:
            failures_by_net[failure.net] = []
        failures_by_net[failure.net].append(failure)

    print(f"\n{'=' * 60}")
    print("Routing Diagnostics")
    print(f"{'=' * 60}")

    # Show successful routes
    for net_id in sorted(routed_net_ids):
        net_name = reverse_net.get(net_id, f"Net_{net_id}")
        length = route_lengths_by_net.get(net_id, 0)
        vias = route_vias_by_net.get(net_id, 0)
        via_info = f", {vias} via{'s' if vias != 1 else ''}" if vias > 0 else ""
        print(f"\n[✓] {net_name}: Routed successfully ({length:.1f}mm{via_info})")

    # Group failures by cause for summary
    failures_by_cause: dict[str, list[RoutingFailure]] = defaultdict(list)
    all_failures: list[RoutingFailure] = []
    for failure in getattr(router, "routing_failures", []):
        if failure.net in unrouted_ids:
            cause_name = (
                failure.failure_cause.value if hasattr(failure, "failure_cause") else "unknown"
            )
            failures_by_cause[cause_name].append(failure)
            all_failures.append(failure)

    # Show failed routes with detailed diagnostics
    shown_nets: set[int] = set()
    for net_id in sorted(unrouted_ids):
        if net_id in shown_nets:
            continue
        shown_nets.add(net_id)

        net_name = reverse_net.get(net_id, f"Net_{net_id}")
        net_failures = failures_by_net.get(net_id, [])

        if net_failures:
            failure = net_failures[0]
            cause_name = (
                failure.failure_cause.value.upper()
                if hasattr(failure, "failure_cause")
                else "UNKNOWN"
            )

            print(f"\n[✗] {net_name}: FAILED")
            print(f"    Reason: {cause_name}")
            print(f"    Details: {failure.reason}")

            # Show coordinates if available
            if hasattr(failure, "source_coords") and failure.source_coords:
                src = failure.source_coords
                tgt = failure.target_coords
                print(f"    Path: ({src[0]:.1f}, {src[1]:.1f}) → ({tgt[0]:.1f}, {tgt[1]:.1f})")

            # Show suggestions from analysis
            if hasattr(failure, "analysis") and failure.analysis and failure.analysis.suggestions:
                print(f"    Suggestion: {failure.analysis.suggestions[0]}")

            # Verbose mode: show additional details
            if verbose and hasattr(failure, "analysis") and failure.analysis:
                analysis = failure.analysis
                print("\n    --- Detailed Analysis ---")
                print(f"    Confidence: {analysis.confidence:.0%}")
                print(f"    Congestion score: {analysis.congestion_score:.0%}")
                if analysis.clearance_margin != float("inf"):
                    print(f"    Clearance margin: {analysis.clearance_margin:.2f}mm")
                if analysis.blocking_elements:
                    print(f"    Blocking elements: {len(analysis.blocking_elements)}")
                    for elem in analysis.blocking_elements[:3]:
                        elem_desc = f"{elem.type}"
                        if elem.ref:
                            elem_desc += f" ({elem.ref})"
                        if elem.net:
                            elem_desc += f" net={elem.net}"
                        print(f"      - {elem_desc}")
                    if len(analysis.blocking_elements) > 3:
                        print(f"      ... and {len(analysis.blocking_elements) - 3} more")
                if analysis.suggestions:
                    print("    All suggestions:")
                    for suggestion in analysis.suggestions:
                        print(f"      - {suggestion}")
        else:
            print(f"\n[✗] {net_name}: FAILED")
            print("    Reason: No path found")

    # Show summary grouped by failure reason
    if failures_by_cause:
        print(f"\n{'=' * 60}")
        print("Failure Summary by Cause")
        print(f"{'=' * 60}")

        total_failed = len(unrouted_ids)
        for cause, failures in sorted(failures_by_cause.items(), key=lambda x: -len(x[1])):
            count = len({f.net for f in failures})
            pct = count / total_failed * 100 if total_failed > 0 else 0
            print(f"  {cause.upper()}: {count} net{'s' if count != 1 else ''} ({pct:.0f}%)")

    # Generate pattern-based suggestions
    if unrouted_ids:
        print(f"\n{'=' * 60}")
        print("Routing Suggestions")
        print(f"{'=' * 60}")
        print("\nBased on failure analysis:\n")

        suggestions_shown = 0

        # Check for PAD_INACCESSIBLE issues
        pad_failures = failures_by_cause.get("pin_access", [])
        if pad_failures:
            count = len({f.net for f in pad_failures})
            grid_res = getattr(router.grid, "resolution", 0.25)
            suggestions_shown += 1
            print(
                f"{suggestions_shown}. GRID ALIGNMENT ({count} net{'s' if count != 1 else ''} affected)"
            )
            print(f"   Some pads don't align with the {grid_res}mm routing grid.")
            print(f"   Try: --grid {grid_res / 2} or adjust pad positions\n")

        # Check for CONGESTION issues
        congestion_failures = failures_by_cause.get("congestion", [])
        if congestion_failures:
            count = len({f.net for f in congestion_failures})
            num_layers = getattr(router.grid, "num_layers", 2)
            suggestions_shown += 1
            print(
                f"{suggestions_shown}. CONGESTION ({count} net{'s' if count != 1 else ''} affected)"
            )
            print(
                f"   Routing channels are saturated on {num_layers} layer{'s' if num_layers != 1 else ''}."
            )
            if num_layers < 4:
                print("   Try: --layers 4 or --layers 6 for more routing resources\n")
            else:
                print("   Try: Increase board area or reduce component density\n")

        # Check for BLOCKED_PATH issues
        blocked_failures = failures_by_cause.get("blocked_path", [])
        if blocked_failures:
            count = len({f.net for f in blocked_failures})
            all_blocking = set()
            for f in blocked_failures:
                all_blocking.update(f.blocking_components)
            suggestions_shown += 1
            print(
                f"{suggestions_shown}. COMPONENT BLOCKING ({count} net{'s' if count != 1 else ''} affected)"
            )
            print("   Direct paths are blocked by component keepouts.")
            if all_blocking:
                comp_list = ", ".join(sorted(all_blocking)[:5])
                if len(all_blocking) > 5:
                    comp_list += f" and {len(all_blocking) - 5} more"
                print(f"   Blocking components: {comp_list}")
            print("   Try: Reposition components or use vias to route around\n")

        # Check for CLEARANCE issues
        clearance_failures = failures_by_cause.get("clearance", [])
        if clearance_failures:
            count = len({f.net for f in clearance_failures})
            suggestions_shown += 1
            print(
                f"{suggestions_shown}. CLEARANCE VIOLATIONS ({count} net{'s' if count != 1 else ''} affected)"
            )
            print("   Cannot meet trace clearance requirements.")
            print("   Try: --clearance <smaller_value> (check manufacturer limits)\n")

        # General suggestions if nothing specific
        if suggestions_shown == 0:
            num_layers = getattr(router.grid, "num_layers", 2)
            print("1. Try negotiated routing: kct route --strategy negotiated")
            print("2. Try Monte Carlo routing: kct route --strategy monte-carlo --mc-trials 20")
            if num_layers <= 2:
                print("3. Consider 4-layer routing: kct route --layers 4")

    print(f"\n{'=' * 60}")


def _calculate_route_length(route: Route) -> float:
    """Calculate total length of a route in millimeters.

    Args:
        route: Route object with segments

    Returns:
        Total length in millimeters
    """
    length = 0.0
    for segment in route.segments:
        dx = segment.end.x - segment.start.x
        dy = segment.end.y - segment.start.y
        length += (dx**2 + dy**2) ** 0.5
    return length


def get_routing_diagnostics_json(
    router: Autorouter,
    net_map: dict[str, int],
    nets_to_route: int,
) -> dict:
    """Get routing diagnostics as a JSON-serializable dictionary.

    Returns structured diagnostic information suitable for tooling and automation.

    Args:
        router: The Autorouter instance with completed routing
        net_map: Mapping of net names to net IDs
        nets_to_route: Total number of nets that should be routed

    Returns:
        Dictionary with routing diagnostics in JSON-serializable format
    """
    # Build reverse mapping for net names
    reverse_net = {v: k for k, v in net_map.items() if v > 0}

    # Calculate route metrics
    route_lengths_by_net: dict[int, float] = {}
    route_vias_by_net: dict[int, int] = {}
    for route in router.routes:
        net_id = route.net
        length_mm = _calculate_route_length(route)
        route_lengths_by_net[net_id] = route_lengths_by_net.get(net_id, 0) + length_mm
        route_vias_by_net[net_id] = route_vias_by_net.get(net_id, 0) + len(route.vias)

    # Identify routed and unrouted nets
    routed_net_ids = {route.net for route in router.routes}
    all_net_ids = {v for k, v in net_map.items() if v > 0}
    unrouted_ids = all_net_ids - routed_net_ids

    # Build successful routes list
    successful_routes = []
    for net_id in sorted(routed_net_ids):
        net_name = reverse_net.get(net_id, f"Net_{net_id}")
        successful_routes.append(
            {
                "net_id": net_id,
                "net_name": net_name,
                "status": "routed",
                "length_mm": round(route_lengths_by_net.get(net_id, 0), 2),
                "vias": route_vias_by_net.get(net_id, 0),
            }
        )

    # Build failed routes list with diagnostics
    failed_routes = []
    failures_by_cause: dict[str, int] = defaultdict(int)

    for failure in getattr(router, "routing_failures", []):
        if failure.net not in unrouted_ids:
            continue

        cause_name = failure.failure_cause.value if hasattr(failure, "failure_cause") else "unknown"
        failures_by_cause[cause_name] += 1

        failure_dict = {
            "net_id": failure.net,
            "net_name": failure.net_name,
            "status": "failed",
            "failure_cause": cause_name,
            "reason": failure.reason,
            "source_pad": {"ref": failure.source_pad[0], "pin": failure.source_pad[1]},
            "target_pad": {"ref": failure.target_pad[0], "pin": failure.target_pad[1]},
            "blocking_components": failure.blocking_components,
        }

        if hasattr(failure, "source_coords") and failure.source_coords:
            failure_dict["source_coords"] = list(failure.source_coords)
            failure_dict["target_coords"] = list(failure.target_coords)

        if hasattr(failure, "analysis") and failure.analysis:
            failure_dict["analysis"] = failure.analysis.to_dict()

        failed_routes.append(failure_dict)

    # Build suggestions based on failure patterns
    suggestions = []
    num_layers = getattr(router.grid, "num_layers", 2)
    grid_res = getattr(router.grid, "resolution", 0.25)

    if failures_by_cause.get("pin_access", 0) > 0:
        suggestions.append(
            {
                "category": "GRID_ALIGNMENT",
                "affected_nets": failures_by_cause["pin_access"],
                "description": f"Some pads don't align with the {grid_res}mm routing grid",
                "fix": f"--grid {grid_res / 2} or adjust pad positions",
            }
        )

    if failures_by_cause.get("congestion", 0) > 0:
        suggestions.append(
            {
                "category": "LAYER_COUNT",
                "affected_nets": failures_by_cause["congestion"],
                "description": f"Routing channels saturated on {num_layers} layers",
                "fix": "--layers 4" if num_layers < 4 else "Increase board area",
            }
        )

    if failures_by_cause.get("blocked_path", 0) > 0:
        suggestions.append(
            {
                "category": "COMPONENT_BLOCKING",
                "affected_nets": failures_by_cause["blocked_path"],
                "description": "Direct paths blocked by component keepouts",
                "fix": "Reposition components or use vias",
            }
        )

    if failures_by_cause.get("clearance", 0) > 0:
        suggestions.append(
            {
                "category": "CLEARANCE",
                "affected_nets": failures_by_cause["clearance"],
                "description": "Cannot meet trace clearance requirements",
                "fix": "--clearance <smaller_value> (check manufacturer limits)",
            }
        )

    return {
        "summary": {
            "nets_requested": nets_to_route,
            "nets_routed": len(routed_net_ids),
            "nets_failed": len(unrouted_ids),
            "success_rate": round(len(routed_net_ids) / nets_to_route * 100, 1)
            if nets_to_route > 0
            else 0,
        },
        "failure_breakdown": dict(failures_by_cause),
        "successful_routes": successful_routes,
        "failed_routes": failed_routes,
        "suggestions": suggestions,
    }


def print_routing_diagnostics_json(
    router: Autorouter,
    net_map: dict[str, int],
    nets_to_route: int,
) -> None:
    """Print routing diagnostics as JSON to stdout.

    Args:
        router: The Autorouter instance with completed routing
        net_map: Mapping of net names to net IDs
        nets_to_route: Total number of nets that should be routed
    """
    diagnostics = get_routing_diagnostics_json(router, net_map, nets_to_route)
    print(json.dumps(diagnostics, indent=2))


def format_failed_nets_summary(
    routing_failures: list[RoutingFailure],
    max_display: int = 10,
) -> str:
    """Format a compact summary of failed routing attempts.

    Produces output like:
        Failed nets:
          - Net 33 "Net-(R20-Pad1)": congestion (area too crowded)
          - Net 35 "Net-(U3-OUTH)": blocked_path (blocked by U1, R4)
          - Net 41 "PRECHG_NEG": pin_access (pad not on grid)

    Args:
        routing_failures: List of RoutingFailure objects from router
        max_display: Maximum number of failures to show (default 10)

    Returns:
        Formatted string ready for printing, or empty string if no failures.
    """
    if not routing_failures:
        return ""

    # Group failures by net to avoid duplicates (one net may have multiple failed paths)
    seen_nets: set[int] = set()
    unique_failures: list[RoutingFailure] = []
    for failure in routing_failures:
        if failure.net not in seen_nets:
            seen_nets.add(failure.net)
            unique_failures.append(failure)

    if not unique_failures:
        return ""

    lines = ["Failed nets:"]

    for i, failure in enumerate(unique_failures[:max_display]):
        # Format: Net ID "name": cause (details)
        cause = failure.failure_cause.value if hasattr(failure, "failure_cause") else "unknown"

        # Build detail string based on failure type
        details = ""
        if failure.blocking_components:
            blockers = ", ".join(failure.blocking_components[:3])
            if len(failure.blocking_components) > 3:
                blockers += f" +{len(failure.blocking_components) - 3} more"
            details = f"blocked by {blockers}"
        elif failure.blocking_nets:
            net_count = len(failure.blocking_nets)
            details = f"{net_count} blocking net{'s' if net_count != 1 else ''}"
        elif failure.reason and failure.reason != "No path found":
            # Use the reason if it's more specific
            details = failure.reason
        else:
            # Provide cause-specific details
            cause_details = {
                "congestion": "area too crowded",
                "blocked_path": "no clear path",
                "clearance": "clearance violation",
                "layer_conflict": "wrong layer",
                "pin_access": "pad not on grid",
                "length_constraint": "length limit exceeded",
                "differential_pair": "pair constraint failed",
                "keepout": "crosses keepout zone",
            }
            details = cause_details.get(cause, "no path found")

        lines.append(f'  - Net {failure.net} "{failure.net_name}": {cause} ({details})')

        # Add detailed pad access blocker information if available
        if (
            hasattr(failure, "analysis")
            and failure.analysis
            and hasattr(failure.analysis, "pad_access_blockers")
            and failure.analysis.pad_access_blockers
        ):
            for blocker in failure.analysis.pad_access_blockers[:2]:  # Show top 2 blockers
                lines.append(
                    f'    Pad {blocker.pad_ref}: blocked by clearance from "{blocker.blocking_net_name}" '
                    f"({blocker.blocking_type} at {blocker.distance:.2f}mm distance)"
                )
            # Add suggestion if available
            if failure.analysis.suggestions:
                for suggestion in failure.analysis.suggestions[:1]:  # Show top suggestion
                    if "clearance" in suggestion.lower():
                        lines.append(f"    Suggestion: {suggestion}")

    # Show count of remaining failures if truncated
    remaining = len(unique_failures) - max_display
    if remaining > 0:
        lines.append(f"  ... and {remaining} more failed net{'s' if remaining != 1 else ''}")

    return "\n".join(lines)


def show_fine_pitch_warnings(
    report: FinePitchReport,
    quiet: bool = False,
    verbose: bool = False,
) -> None:
    """Display fine-pitch component warnings before routing.

    This function displays warnings about fine-pitch ICs that may cause
    routing difficulties due to grid/clearance constraints. It's designed
    to be called before routing begins so users can adjust settings.

    Args:
        report: FinePitchReport from analyze_fine_pitch_components()
        quiet: If True, skip output (useful for scripting)
        verbose: If True, show detailed per-pad information
    """
    if quiet:
        return

    if not report.has_warnings:
        return

    print(report.format_warnings(verbose=verbose))


__all__ = [
    "format_failed_nets_summary",
    "get_routing_diagnostics_json",
    "print_routing_diagnostics_json",
    "show_fine_pitch_warnings",
    "show_routing_summary",
]
