"""Length tuning application logic.

Applies serpentine (meander) patterns to routes that don't meet length
constraints. This post-routing pass handles both match group tuning
and individual minimum length violations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .length import LengthTracker
    from .primitives import Route


def apply_length_tuning(
    routes: list[Route],
    length_tracker: LengthTracker,
    grid: RoutingGrid,
    net_names: dict[int, str] | None = None,
    verbose: bool = True,
) -> dict[int, tuple[Route, Any]]:
    """Apply serpentine tuning to routes that don't meet length constraints.

    This post-routing pass adds serpentine (meander) patterns to routes
    that are too short or need to match other routes in their match group.

    Args:
        routes: List of Route objects (modified in-place when routes are replaced)
        length_tracker: LengthTracker with constraints and recorded lengths
        grid: The routing grid
        net_names: Optional mapping of net ID to human-readable name
        verbose: Whether to print progress information

    Returns:
        Dictionary mapping net ID to (tuned_route, result)
    """
    from .optimizer.serpentine import SerpentineGenerator, tune_match_group

    if net_names is None:
        net_names = {}

    results: dict[int, tuple[Route, Any]] = {}

    # Get violations to determine which nets need tuning
    violations = length_tracker.get_violations()

    if not violations and verbose:
        print("No length violations - no tuning needed")
        return results

    if verbose:
        print(f"\n=== Length Tuning ({len(violations)} violations) ===")

    # Build routes by net ID
    routes_by_net: dict[int, Route] = {}
    for route in routes:
        routes_by_net[route.net] = route

    # Process match groups
    processed_groups: set[str] = set()
    for group_name, net_ids in length_tracker.match_groups.items():
        if group_name in processed_groups:
            continue
        processed_groups.add(group_name)

        # Get tolerance from first constraint
        tolerance = 0.5
        if net_ids and net_ids[0] in length_tracker._constraint_map:
            tolerance = length_tracker._constraint_map[net_ids[0]].match_tolerance

        if verbose:
            print(f"  Tuning match group '{group_name}' ({len(net_ids)} nets)")

        group_results = tune_match_group(
            routes=routes_by_net,
            group_net_ids=net_ids,
            tolerance=tolerance,
            grid=grid,
        )

        # Update routes and collect results
        for net_id, (new_route, result) in group_results.items():
            if result.success and result.length_added > 0:
                # Replace route in routes list
                for i, r in enumerate(routes):
                    if r.net == net_id:
                        routes[i] = new_route
                        break
                routes_by_net[net_id] = new_route

                if verbose:
                    print(f"    Net {net_id}: {result.message}")

            results[net_id] = (new_route, result)

    # Process individual min length violations (not in match groups)
    generator = SerpentineGenerator()
    for violation in violations:
        if violation.violation_type.value == "too_short":
            net_id = violation.net_id
            if isinstance(net_id, str):
                continue  # Match group, already processed

            constraint = length_tracker.get_constraint(net_id)
            if constraint and constraint.match_group:
                continue  # Part of a match group, already processed

            route = routes_by_net.get(net_id)
            if not route:
                continue

            target = violation.target_length or 0
            new_route, result = generator.add_serpentine(route, target, grid)

            if result.success and result.length_added > 0:
                for i, r in enumerate(routes):
                    if r.net == net_id:
                        routes[i] = new_route
                        break

                if verbose:
                    net_name = net_names.get(net_id, f"Net {net_id}")
                    print(f"  {net_name}: {result.message}")

            results[net_id] = (new_route, result)

    if verbose:
        print("=== Length Tuning Complete ===\n")

    return results
