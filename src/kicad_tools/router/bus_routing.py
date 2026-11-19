"""Bus routing integration for the autorouter.

This module provides bus-aware routing functionality that coordinates
bus signal routing with the main autorouter.
"""

from __future__ import annotations

import math
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
    group_by_length_window,
    order_group_by_escape,
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

    def detect_length_window_groups(
        self,
        bus_config: BusRoutingConfig,
        exclude: set[int],
    ) -> list[BusGroup]:
        """Cluster non-bus nets of similar MST length into synthetic groups (Issue #9621).

        Candidates are multi-pad (>= 2 pads) nets that are not in
        ``exclude`` (name-detected bus members) and are not power/ground or
        pour nets.  MST lengths reuse ``RoutabilityAnalyzer._calculate_mst_length``
        -- no new geometry.  Each cluster becomes a synthetic ``BusGroup``
        whose member order is the escape-feasibility order (fewest open pad
        directions first), so the tightest escapes route first.
        """
        if bus_config.length_window is None:
            return []

        autorouter = self.autorouter
        # Reuses the analyzer's Prim MST (reference-only __init__); no new
        # geometry code for the length metric (#9621).
        from .analysis import RoutabilityAnalyzer

        mst_length = RoutabilityAnalyzer(autorouter)._calculate_mst_length
        nets_with_lengths: dict[int, tuple[str, float]] = {}
        for net_id, pad_keys in autorouter.nets.items():
            if net_id == 0 or net_id in exclude or len(pad_keys) < 2:
                continue
            if autorouter._is_power_net_by_class(net_id) or autorouter._is_pour_net(net_id):
                continue
            pads = [autorouter.pads[key] for key in pad_keys if key in autorouter.pads]
            if len(pads) < 2:
                continue
            nets_with_lengths[net_id] = (
                autorouter.net_names.get(net_id, f"Net_{net_id}"),
                mst_length(pads),
            )

        groups = group_by_length_window(
            nets_with_lengths,
            bus_config.length_window,
            min_width=bus_config.min_bus_width,
        )

        openness: dict[int, int] = {}
        for group in groups:
            for signal in group.signals:
                if signal.net_id not in openness:
                    openness[signal.net_id] = self._net_escape_openness(signal.net_id)
        return [order_group_by_escape(group, openness) for group in groups]

    def _net_escape_openness(self, net_id: int) -> int:
        """Count open escape directions across a net's pads (Issue #9621).

        For each pad, each of the four cardinal directions counts as open
        when the run of cells from just past the pad's own clearance halo
        (one clearance ring, ``trace_clearance + trace_width / 2``) is
        routable for this net on the pad's layer.  The probe starts beyond
        the own halo because halo cells carry ``net == 0`` and block every
        net in ``RoutingGrid.is_blocked``; what decides the direction is
        whether FOREIGN metal (other pads' halos, components, keepouts)
        seals it.  Free cells and same-net copper both count as open.  The
        count over all pads is the net's escape openness: fewer open
        directions means a tighter escape, so ascending order routes the
        most constrained nets first.
        """
        autorouter = self.autorouter
        grid = autorouter.grid
        rules = autorouter.rules
        halo = max(1, math.ceil((rules.trace_clearance + rules.trace_width / 2) / grid.resolution))
        openness = 0
        for key in autorouter.nets.get(net_id, []):
            pad = autorouter.pads.get(key)
            if pad is None:
                continue
            gx, gy = grid.world_to_grid(pad.x, pad.y)
            half_w = max(1, math.ceil(pad.width / 2 / grid.resolution))
            half_h = max(1, math.ceil(pad.height / 2 / grid.resolution))
            for dx, dy, extent in (
                (1, 0, half_w),
                (-1, 0, half_w),
                (0, 1, half_h),
                (0, -1, half_h),
            ):
                start = extent + halo + 1  # first cell past the pad's own halo
                if all(
                    not grid.is_blocked(
                        gx + dx * (start + step), gy + dy * (start + step), pad.layer, net=net_id
                    )
                    for step in range(halo)
                ):
                    openness += 1
        return openness

    def route_bus_group(
        self,
        bus_group: BusGroup,
        mode: BusRoutingMode = BusRoutingMode.PARALLEL,
        spacing: float | None = None,
    ) -> list[Route]:
        """Route all signals in a bus group together."""
        # Issue #6008: this driver can route without the ``route_all*``
        # preamble that installs board keepout rule areas, so install them
        # here (idempotent; one ``getattr`` once installed).
        _install_rule_area_keepouts_on(self.autorouter)
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
        # Issue #6008: this driver can route without the ``route_all*``
        # preamble that installs board keepout rule areas, so install them
        # here (idempotent; one ``getattr`` once installed).
        _install_rule_area_keepouts_on(self.autorouter)
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

        # Issue #9621: length-window groups for similar-length nets with no
        # shared naming convention.  Routed by the same per-group path as
        # named buses, with escape-feasibility ordering inside each group.
        if bus_config.length_window is not None:
            length_groups = self.detect_length_window_groups(bus_config, exclude=bus_net_ids)
            if length_groups:
                print(f"  Length-window groups ({bus_config.length_window}% window):")
                for group in length_groups:
                    print(f"    - {group} ({group.width} nets): ", end="")
                    print(", ".join(s.net_name for s in group.signals))
                bus_groups.extend(length_groups)
                bus_net_ids.update(net for g in length_groups for net in g.get_net_ids())
            else:
                print("  No length-window groups detected")

        if not bus_groups:
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


def _install_rule_area_keepouts_on(autorouter: object) -> None:
    """Install board keepout rule areas on ``autorouter``'s grid (#6008)."""
    install = getattr(autorouter, "_install_grid_rule_area_keepouts", None)
    if install is not None:
        install()
