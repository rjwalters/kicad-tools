"""Routing output and diagnostics display utilities.

This module provides shared output functions for displaying routing results,
including successful routes, failed routes, and actionable suggestions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Autorouter, RoutingFailure
    from .primitives import Route


def show_routing_summary(
    router: Autorouter,
    net_map: dict[str, int],
    nets_to_route: int,
    quiet: bool = False,
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
    """
    if quiet:
        return

    # Build reverse mapping for net names
    reverse_net = {v: k for k, v in net_map.items() if v > 0}

    # Calculate route lengths by net
    route_lengths_by_net: dict[int, float] = {}
    route_endpoints_by_net: dict[int, list[tuple[str, str]]] = {}
    for route in router.routes:
        net_id = route.net
        # Calculate route length
        length_mm = _calculate_route_length(route)
        route_lengths_by_net[net_id] = route_lengths_by_net.get(net_id, 0) + length_mm
        # Track endpoints
        if net_id not in route_endpoints_by_net:
            route_endpoints_by_net[net_id] = []
        if route.segments:
            start = route.segments[0].start
            end = route.segments[-1].end
            # Format as component.pad if possible
            start_str = f"({start.x:.1f}, {start.y:.1f})"
            end_str = f"({end.x:.1f}, {end.y:.1f})"
            route_endpoints_by_net[net_id].append((start_str, end_str))

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
    print("ROUTING SUMMARY")
    print(f"{'=' * 60}")

    # Show successful routes
    if routed_net_ids:
        print(f"\nSuccessful routes ({len(routed_net_ids)}):\n")
        for net_id in sorted(routed_net_ids):
            net_name = reverse_net.get(net_id, f"Net_{net_id}")
            length = route_lengths_by_net.get(net_id, 0)
            # Get endpoint info from failures (which have pad info) or routes
            net_failures = failures_by_net.get(net_id, [])
            endpoint_str = ""
            if net_failures:
                # Use first failure's endpoint info
                f = net_failures[0]
                endpoint_str = f" ({f.source_pad[0]}.{f.source_pad[1]} -> {f.target_pad[0]}.{f.target_pad[1]})"
            print(f"  - {net_name}{endpoint_str}: {length:.2f}mm")

    # Show failed routes
    if unrouted_ids:
        # Collect all blocking components for suggestions
        all_blocking_components: set[str] = set()

        print(f"\nFailed routes ({len(unrouted_ids)}):\n")

        for net_id in sorted(unrouted_ids):
            net_name = reverse_net.get(net_id, f"Net_{net_id}")
            net_failures = failures_by_net.get(net_id, [])

            # Determine failure reason
            if net_failures:
                # Use recorded failure information
                failure = net_failures[0]  # First failure gives the reason
                reason = failure.reason
                src = f"{failure.source_pad[0]}.{failure.source_pad[1]}"
                tgt = f"{failure.target_pad[0]}.{failure.target_pad[1]}"
                for f in net_failures:
                    all_blocking_components.update(f.blocking_components)
                print(f"  - {net_name}: {src} -> {tgt}")
                print(f"      Reason: {reason}")
            else:
                reason = "No path found"
                print(f"  - {net_name}: {reason}")

            # Show additional failed connections if there are multiple
            if len(net_failures) > 1:
                for f in net_failures[1:3]:  # Show next 2 failed connections
                    src = f"{f.source_pad[0]}.{f.source_pad[1]}"
                    tgt = f"{f.target_pad[0]}.{f.target_pad[1]}"
                    print(f"      {src} -> {tgt}: {f.reason}")
                if len(net_failures) > 3:
                    print(f"      ... and {len(net_failures) - 3} more failed connections")

        # Show suggestions based on failure analysis
        print("\nSuggestions:")

        if all_blocking_components:
            comp_list = ", ".join(sorted(all_blocking_components)[:5])
            if len(all_blocking_components) > 5:
                comp_list += f" and {len(all_blocking_components) - 5} more"
            print(f"  - Reposition blocking components: {comp_list}")

        # Check if multi-layer routing might help
        num_layers = getattr(router.grid, "num_layers", 2)
        if num_layers <= 2:
            print("  - Consider 4-layer routing: kct route --layers 4")

        print("  - Try negotiated routing: kct route --algorithm negotiated")
        print("  - Try Monte Carlo routing: kct route --algorithm monte-carlo --trials 20")

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


__all__ = [
    "show_routing_summary",
]
