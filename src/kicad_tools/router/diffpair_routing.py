"""Differential pair routing integration for the autorouter.

This module provides differential pair-aware routing functionality
that coordinates differential pair routing with the main autorouter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Autorouter
    from .primitives import Route

from .diffpair import (
    DifferentialPair,
    DifferentialPairConfig,
    LengthMismatchWarning,
    analyze_differential_pairs,
    detect_differential_pairs,
)
from .path import calculate_route_length


class DiffPairRouter:
    """Differential pair routing coordinator for the autorouter."""

    def __init__(self, autorouter: Autorouter):
        """Initialize differential pair router.

        Args:
            autorouter: Parent autorouter instance
        """
        self.autorouter = autorouter

    def detect_differential_pairs(self) -> list[DifferentialPair]:
        """Detect differential pairs from net names."""
        return detect_differential_pairs(self.autorouter.net_names)

    def analyze_differential_pairs(self) -> dict[str, any]:
        """Analyze net names for differential pairs."""
        return analyze_differential_pairs(self.autorouter.net_names)

    def route_differential_pair(
        self,
        pair: DifferentialPair,
        spacing: float | None = None,
    ) -> tuple[list[Route], LengthMismatchWarning | None]:
        """Route a differential pair together."""
        if pair.rules is None:
            return [], None

        if spacing is None:
            spacing = pair.rules.spacing

        routes: list[Route] = []
        print(f"\n  Routing differential pair {pair}")
        print(f"    Type: {pair.pair_type.value}")
        print(f"    Spacing: {spacing}mm, Max delta: {pair.rules.max_length_delta}mm")

        p_net_id = pair.positive.net_id
        n_net_id = pair.negative.net_id

        print(f"    Routing {pair.positive.net_name} (P)...")
        p_routes = self.autorouter.route_net(p_net_id)
        routes.extend(p_routes)

        p_length = calculate_route_length(p_routes)
        pair.routed_length_p = p_length
        print(f"      Length: {p_length:.3f}mm")

        print(f"    Routing {pair.negative.net_name} (N)...")
        n_routes = self.autorouter.route_net(n_net_id)
        routes.extend(n_routes)

        n_length = calculate_route_length(n_routes)
        pair.routed_length_n = n_length
        print(f"      Length: {n_length:.3f}mm")

        delta = pair.length_delta
        warning = None
        if delta > pair.rules.max_length_delta:
            warning = LengthMismatchWarning(
                pair=pair,
                delta=delta,
                max_allowed=pair.rules.max_length_delta,
            )
            print(f"    WARNING: {warning}")
        else:
            print(f"    Length matched: delta={delta:.3f}mm (within tolerance)")

        return routes, warning

    def route_all_with_diffpairs(
        self,
        diffpair_config: DifferentialPairConfig | None = None,
        net_order: list[int] | None = None,
    ) -> tuple[list[Route], list[LengthMismatchWarning]]:
        """Route all nets with differential pair-aware routing."""
        if diffpair_config is None or not diffpair_config.enabled:
            return self.autorouter.route_all(net_order), []

        print("\n=== Differential Pair Routing ===")

        diff_pairs = self.detect_differential_pairs()
        diff_net_ids: set[int] = set()

        if diff_pairs:
            print(f"  Detected {len(diff_pairs)} differential pairs:")
            for pair in diff_pairs:
                print(f"    - {pair}: {pair.pair_type.value}")
                p_id, n_id = pair.get_net_ids()
                diff_net_ids.add(p_id)
                diff_net_ids.add(n_id)
        else:
            print("  No differential pairs detected")
            return self.autorouter.route_all(net_order), []

        for pair in diff_pairs:
            if pair.rules is not None:
                pair.rules = diffpair_config.get_rules(pair.pair_type)

        print("\n--- Routing differential pairs ---")
        all_routes: list[Route] = []
        warnings: list[LengthMismatchWarning] = []

        for pair in diff_pairs:
            pair_routes, warning = self.route_differential_pair(pair, diffpair_config.spacing)
            all_routes.extend(pair_routes)
            if warning:
                warnings.append(warning)

        non_diff_nets = [n for n in self.autorouter.nets if n not in diff_net_ids and n != 0]
        if non_diff_nets:
            print(f"\n--- Routing {len(non_diff_nets)} non-differential nets ---")
            if net_order:
                non_diff_order = [n for n in net_order if n in non_diff_nets]
            else:
                non_diff_order = sorted(
                    non_diff_nets, key=lambda n: self.autorouter._get_net_priority(n)
                )

            for net in non_diff_order:
                routes = self.autorouter.route_net(net)
                all_routes.extend(routes)
                if routes:
                    print(
                        f"  Net {net}: {len(routes)} routes, "
                        f"{sum(len(r.segments) for r in routes)} segments"
                    )

        print("\n=== Differential Pair Routing Complete ===")
        print(f"  Total routes: {len(all_routes)}")
        print(f"  Differential pair nets: {len(diff_net_ids)}")
        print(f"  Other nets: {len(non_diff_nets)}")
        if warnings:
            print(f"  Length mismatch warnings: {len(warnings)}")
            for w in warnings:
                print(f"    - {w}")

        return all_routes, warnings
