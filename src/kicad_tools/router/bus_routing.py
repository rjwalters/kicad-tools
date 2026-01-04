"""Bus routing integration for the autorouter.

This module provides bus-aware routing functionality that coordinates
bus signal routing with the main autorouter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Autorouter
    from .primitives import Route

from .bus import (
    BusGroup,
    BusRoutingConfig,
    BusRoutingMode,
    analyze_buses,
    detect_bus_signals,
    group_buses,
)


class BusRouter:
    """Bus routing coordinator for the autorouter."""

    def __init__(self, autorouter: Autorouter):
        """Initialize bus router.

        Args:
            autorouter: Parent autorouter instance
        """
        self.autorouter = autorouter

    def detect_buses(self, min_bus_width: int = 2) -> list[BusGroup]:
        """Detect bus signals from net names."""
        signals = detect_bus_signals(self.autorouter.net_names, min_bus_width)
        return group_buses(signals, min_bus_width)

    def get_bus_analysis(self) -> dict:
        """Get a summary of detected buses in the design."""
        return analyze_buses(self.autorouter.net_names)

    def route_bus_group(
        self,
        bus_group: BusGroup,
        mode: BusRoutingMode = BusRoutingMode.PARALLEL,
        spacing: float | None = None,
    ) -> list[Route]:
        """Route all signals in a bus group together."""
        if not bus_group.signals:
            return []

        rules = self.autorouter.rules
        if spacing is None:
            spacing = rules.trace_width + rules.trace_clearance

        routes: list[Route] = []
        print(f"\n  Routing bus {bus_group} ({mode.value} mode, spacing={spacing}mm)")

        net_ids = bus_group.get_net_ids()

        if mode == BusRoutingMode.PARALLEL:
            routes = self._route_bus_parallel(net_ids, bus_group.name, spacing)
        elif mode == BusRoutingMode.STACKED:
            routes = self._route_bus_stacked(net_ids, bus_group.name)
        else:
            for net_id in net_ids:
                net_routes = self.autorouter.route_net(net_id)
                routes.extend(net_routes)

        return routes

    def _route_bus_parallel(self, net_ids: list[int], bus_name: str, spacing: float) -> list[Route]:
        """Route bus signals in parallel with consistent spacing."""
        routes: list[Route] = []
        if not net_ids:
            return routes

        first_routes = self.autorouter.route_net(net_ids[0])
        routes.extend(first_routes)

        if not first_routes:
            print(f"    Warning: Could not route first bus signal {bus_name}[0]")
            for i, net_id in enumerate(net_ids[1:], 1):
                net_routes = self.autorouter.route_net(net_id)
                routes.extend(net_routes)
            return routes

        for i, net_id in enumerate(net_ids[1:], 1):
            print(f"    Signal [{i}] (net {net_id})...")
            net_routes = self.autorouter.route_net(net_id)
            routes.extend(net_routes)
            if net_routes:
                print(
                    f"      Routed: {len(net_routes)} routes, "
                    f"{sum(len(r.segments) for r in net_routes)} segments"
                )
            else:
                print(f"      Warning: Could not route {bus_name}[{i}]")

        return routes

    def _route_bus_stacked(self, net_ids: list[int], bus_name: str) -> list[Route]:
        """Route bus signals on alternating layers."""
        routes: list[Route] = []
        for i, net_id in enumerate(net_ids):
            print(f"    Signal [{i}] (net {net_id})...")
            net_routes = self.autorouter.route_net(net_id)
            routes.extend(net_routes)
        return routes

    def route_all_with_buses(
        self,
        bus_config: BusRoutingConfig | None = None,
        net_order: list[int] | None = None,
    ) -> list[Route]:
        """Route all nets with bus-aware routing."""
        if bus_config is None or not bus_config.enabled:
            return self.autorouter.route_all(net_order)

        print("\n=== Bus-Aware Routing ===")

        bus_groups = self.detect_buses(bus_config.min_bus_width)
        bus_net_ids: set[int] = set()

        if bus_groups:
            print(f"  Detected {len(bus_groups)} bus groups:")
            for group in bus_groups:
                print(f"    - {group}: {group.width} bits")
                bus_net_ids.update(group.get_net_ids())
        else:
            print("  No bus signals detected")
            return self.autorouter.route_all(net_order)

        spacing = bus_config.get_spacing(
            self.autorouter.rules.trace_width, self.autorouter.rules.trace_clearance
        )

        print("\n--- Routing bus signals ---")
        all_routes: list[Route] = []

        for group in bus_groups:
            bus_routes = self.route_bus_group(group, bus_config.mode, spacing)
            all_routes.extend(bus_routes)

        non_bus_nets = [n for n in self.autorouter.nets if n not in bus_net_ids and n != 0]
        if non_bus_nets:
            print(f"\n--- Routing {len(non_bus_nets)} non-bus nets ---")
            if net_order:
                non_bus_order = [n for n in net_order if n in non_bus_nets]
            else:
                non_bus_order = sorted(
                    non_bus_nets, key=lambda n: self.autorouter._get_net_priority(n)
                )

            for net in non_bus_order:
                routes = self.autorouter.route_net(net)
                all_routes.extend(routes)
                if routes:
                    print(
                        f"  Net {net}: {len(routes)} routes, "
                        f"{sum(len(r.segments) for r in routes)} segments"
                    )

        print("\n=== Bus-Aware Routing Complete ===")
        print(f"  Total routes: {len(all_routes)}")
        print(f"  Bus nets: {len(bus_net_ids)}")
        print(f"  Other nets: {len(non_bus_nets)}")

        return all_routes
