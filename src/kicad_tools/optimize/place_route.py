"""Integrated place-route-DRC optimization loop.

This module provides the PlaceRouteOptimizer class that iterates between
placement optimization, routing, and DRC checking to achieve DRC-clean
routing automatically.

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.optimize import PlaceRouteOptimizer
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> optimizer = PlaceRouteOptimizer.from_pcb(pcb, manufacturer="jlcpcb")
    >>> result = optimizer.optimize(max_iterations=10)
    >>>
    >>> if result.success:
    ...     print(f"Converged in {result.iterations} iterations")
    ...     result.save("board-optimized.kicad_pcb")
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.placement.analyzer import PlacementAnalyzer
    from kicad_tools.placement.conflict import Conflict
    from kicad_tools.placement.fixer import PlacementFixer
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.primitives import Route
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.checker import DRCChecker
    from kicad_tools.validate.violations import DRCResults


@dataclass
class OptimizationResult:
    """Result of a place-route-DRC optimization run.

    Attributes:
        success: Whether optimization achieved DRC-clean routing
        pcb_path: Path to the PCB file (modified if optimization applied)
        routes: List of routes from the autorouter (if routing succeeded)
        placement_conflicts: Remaining placement conflicts (if any)
        drc_results: DRC check results (if DRC was run)
        iterations: Number of iterations performed
        message: Human-readable status message

    Example:
        >>> result = optimizer.optimize()
        >>> if result.success:
        ...     print(f"Done in {result.iterations} iterations")
        ... else:
        ...     print(f"Failed: {result.message}")
        ...     for conflict in result.placement_conflicts or []:
        ...         print(f"  - {conflict}")
    """

    success: bool
    pcb_path: Path | None = None
    routes: list[Route] | None = None
    placement_conflicts: list[Conflict] | None = None
    drc_results: DRCResults | None = None
    iterations: int = 0
    message: str = ""

    @property
    def has_placement_conflicts(self) -> bool:
        """Check if there are unresolved placement conflicts."""
        return bool(self.placement_conflicts)

    @property
    def has_drc_violations(self) -> bool:
        """Check if there are DRC violations."""
        if self.drc_results is None:
            return False
        return not self.drc_results.passed

    @property
    def routing_complete(self) -> bool:
        """Check if routing produced any routes."""
        return self.routes is not None and len(self.routes) > 0

    def __str__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        parts = [f"OptimizationResult({status}"]
        if self.iterations > 0:
            parts.append(f", iterations={self.iterations}")
        if self.routes:
            parts.append(f", routes={len(self.routes)}")
        if self.message:
            parts.append(f", message={self.message!r}")
        parts.append(")")
        return "".join(parts)


class PlaceRouteOptimizer:
    """Integrated placement and routing optimizer with DRC verification.

    Iterates between placement fixing, routing, and DRC checking until
    either a clean solution is found or max iterations are exceeded.

    The optimization loop:
    1. **Placement phase**: Fix any placement conflicts (courtyard overlaps, etc.)
    2. **Routing phase**: Route all nets using the autorouter
    3. **DRC phase**: Check for design rule violations
    4. **Iterate**: If routing failed or DRC violations exist, adjust and retry

    Example:
        >>> from kicad_tools.schema.pcb import PCB
        >>> from kicad_tools.optimize import PlaceRouteOptimizer
        >>>
        >>> # Simple usage with factory method
        >>> optimizer = PlaceRouteOptimizer.from_pcb(
        ...     PCB.load("board.kicad_pcb"),
        ...     manufacturer="jlcpcb",
        ... )
        >>> result = optimizer.optimize(max_iterations=10)
        >>>
        >>> # Advanced usage with custom components
        >>> from kicad_tools.placement.analyzer import PlacementAnalyzer
        >>> from kicad_tools.placement.fixer import PlacementFixer
        >>> from kicad_tools.router.core import Autorouter
        >>> from kicad_tools.validate import DRCChecker
        >>>
        >>> optimizer = PlaceRouteOptimizer(
        ...     pcb_path="board.kicad_pcb",
        ...     analyzer=PlacementAnalyzer(),
        ...     fixer=PlacementFixer(),
        ...     router_factory=lambda: Autorouter(65, 56),
        ...     drc_checker_factory=lambda pcb: DRCChecker(pcb, "jlcpcb"),
        ... )

    Attributes:
        pcb_path: Path to the PCB file being optimized
        analyzer: PlacementAnalyzer for detecting conflicts
        fixer: PlacementFixer for resolving conflicts
        router_factory: Callable that creates a fresh Autorouter
        drc_checker_factory: Optional callable to create DRCChecker for a PCB
        verbose: Whether to print progress messages
    """

    def __init__(
        self,
        pcb_path: str | Path,
        analyzer: PlacementAnalyzer,
        fixer: PlacementFixer,
        router_factory: Callable[[], Autorouter],
        drc_checker_factory: Callable[[PCB], DRCChecker] | None = None,
        verbose: bool = True,
    ) -> None:
        """Initialize the optimizer.

        Args:
            pcb_path: Path to the .kicad_pcb file to optimize
            analyzer: PlacementAnalyzer for detecting conflicts
            fixer: PlacementFixer for resolving conflicts
            router_factory: Factory function that returns a fresh Autorouter.
                Called before each routing attempt to get a clean router.
            drc_checker_factory: Optional factory for creating DRCChecker.
                Receives the PCB object and returns a DRCChecker.
                If None, DRC checking is skipped.
            verbose: Print progress messages during optimization
        """
        self.pcb_path = Path(pcb_path)
        self.analyzer = analyzer
        self.fixer = fixer
        self.router_factory = router_factory
        self.drc_checker_factory = drc_checker_factory
        self.verbose = verbose

        # Track state across iterations
        self._current_routes: list[Route] = []
        self._failed_nets: list[int] = []

    @classmethod
    def from_pcb(
        cls,
        pcb: PCB,
        pcb_path: str | Path | None = None,
        manufacturer: str = "jlcpcb",
        layers: int = 4,
        board_width: float | None = None,
        board_height: float | None = None,
        verbose: bool = True,
    ) -> PlaceRouteOptimizer:
        """Create optimizer from a PCB object.

        Factory method that creates all required components from a PCB.

        Args:
            pcb: Loaded PCB object
            pcb_path: Path to the PCB file (inferred from pcb if available)
            manufacturer: Manufacturer profile for DRC rules (e.g., "jlcpcb")
            layers: Number of PCB layers for design rules
            board_width: Board width in mm (auto-detected if None)
            board_height: Board height in mm (auto-detected if None)
            verbose: Print progress messages

        Returns:
            Configured PlaceRouteOptimizer ready to run

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> optimizer = PlaceRouteOptimizer.from_pcb(
            ...     pcb,
            ...     manufacturer="jlcpcb",
            ...     layers=4,
            ... )
            >>> result = optimizer.optimize()
        """
        from kicad_tools.placement.analyzer import PlacementAnalyzer
        from kicad_tools.placement.fixer import PlacementFixer
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.rules import DesignRules as RouterDesignRules
        from kicad_tools.validate.checker import DRCChecker

        # Determine PCB path
        if pcb_path is None:
            # Try to get path from PCB object if it has one
            pcb_path = getattr(pcb, "_path", None) or "board.kicad_pcb"

        pcb_path = Path(pcb_path)

        # Auto-detect board dimensions from PCB if not provided
        if board_width is None or board_height is None:
            width, height = cls._detect_board_dimensions(pcb)
            board_width = board_width or width
            board_height = board_height or height

        # Create components
        analyzer = PlacementAnalyzer(verbose=verbose)
        fixer = PlacementFixer(verbose=verbose)

        # Router factory - creates fresh router for each attempt
        def router_factory() -> Autorouter:
            router = Autorouter(
                width=board_width,
                height=board_height,
                rules=RouterDesignRules(),
            )
            # Load components from PCB
            cls._load_components_into_router(router, pcb)
            return router

        # DRC checker factory
        def drc_checker_factory(check_pcb: PCB) -> DRCChecker:
            return DRCChecker(check_pcb, manufacturer=manufacturer, layers=layers)

        return cls(
            pcb_path=pcb_path,
            analyzer=analyzer,
            fixer=fixer,
            router_factory=router_factory,
            drc_checker_factory=drc_checker_factory,
            verbose=verbose,
        )

    @staticmethod
    def _detect_board_dimensions(pcb: PCB) -> tuple[float, float]:
        """Detect board dimensions from edge cuts or component bounds.

        Args:
            pcb: PCB object to analyze

        Returns:
            Tuple of (width, height) in mm
        """
        # Try to find board outline from Edge.Cuts segments
        edge_segments = list(pcb.segments_on_layer("Edge.Cuts"))

        if edge_segments:
            all_x = []
            all_y = []
            for seg in edge_segments:
                all_x.extend([seg.start[0], seg.end[0]])
                all_y.extend([seg.start[1], seg.end[1]])

            if all_x and all_y:
                width = max(all_x) - min(all_x)
                height = max(all_y) - min(all_y)
                return (width, height)

        # Fall back to footprint bounds
        if pcb.footprints:
            all_x = [fp.position[0] for fp in pcb.footprints]
            all_y = [fp.position[1] for fp in pcb.footprints]
            if all_x and all_y:
                # Add margin around components
                margin = 10.0  # mm
                width = max(all_x) - min(all_x) + 2 * margin
                height = max(all_y) - min(all_y) + 2 * margin
                return (width, height)

        # Default fallback
        return (100.0, 100.0)

    @staticmethod
    def _load_components_into_router(router: Autorouter, pcb: PCB) -> None:
        """Load PCB components into an Autorouter instance.

        Args:
            router: Autorouter to populate
            pcb: PCB containing footprints to load
        """
        import math

        from kicad_tools.router.layers import Layer

        for fp in pcb.footprints:
            ref = fp.reference
            cx, cy = fp.position
            rotation = fp.rotation

            # Transform pad positions
            rot_rad = math.radians(-rotation)  # KiCad uses clockwise
            cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

            pads = []
            for pad in fp.pads:
                # Rotate pad position around component center
                px, py = pad.position
                rx = px * cos_r - py * sin_r
                ry = px * sin_r + py * cos_r

                # Determine if through-hole
                is_pth = pad.type == "thru_hole"

                pads.append(
                    {
                        "number": pad.number,
                        "x": cx + rx,
                        "y": cy + ry,
                        "width": pad.size[0],
                        "height": pad.size[1],
                        "net": pad.net_number,
                        "net_name": pad.net_name,
                        "layer": Layer.F_CU,
                        "through_hole": is_pth,
                        "drill": pad.drill if is_pth else 0.0,
                    }
                )

            if pads:
                router.add_component(ref, pads)

    def optimize(
        self,
        max_iterations: int = 10,
        allow_placement_changes: bool = True,
        skip_drc: bool = False,
    ) -> OptimizationResult:
        """Run the optimization loop.

        Iterates between placement, routing, and DRC until clean or max iterations.

        Args:
            max_iterations: Maximum iterations before giving up
            allow_placement_changes: Whether to modify component placements
            skip_drc: Skip DRC checking (useful for faster iteration)

        Returns:
            OptimizationResult with final state and metrics

        Example:
            >>> result = optimizer.optimize(max_iterations=20)
            >>> if result.success:
            ...     print("DRC-clean routing achieved!")
            ... else:
            ...     print(f"Failed after {result.iterations} iterations")
            ...     print(f"Reason: {result.message}")
        """
        if self.verbose:
            print(f"\n{'=' * 60}")
            print("PLACE-ROUTE-DRC OPTIMIZATION")
            print(f"{'=' * 60}")
            print(f"  PCB: {self.pcb_path}")
            print(f"  Max iterations: {max_iterations}")
            print(f"  Allow placement changes: {allow_placement_changes}")
            print(f"  DRC enabled: {not skip_drc and self.drc_checker_factory is not None}")

        for iteration in range(max_iterations):
            if self.verbose:
                print(f"\n--- Iteration {iteration + 1}/{max_iterations} ---")

            # Phase 1: Fix placement conflicts
            if allow_placement_changes:
                conflicts = self._run_placement_phase()
                if conflicts:
                    if self.verbose:
                        print(f"  Placement conflicts remaining: {len(conflicts)}")
                    # Apply fixes
                    fixes = self.fixer.suggest_fixes(conflicts, self.analyzer)
                    if fixes:
                        result = self.fixer.apply_fixes(self.pcb_path, fixes)
                        if self.verbose:
                            print(f"  Applied {result.fixes_applied} fixes")
            else:
                conflicts = []

            # Phase 2: Route
            routes, failed_nets = self._run_routing_phase()

            if failed_nets:
                if self.verbose:
                    print(f"  Routing incomplete: {len(failed_nets)} nets failed")

                if allow_placement_changes:
                    # Identify components blocking failed nets
                    blockers = self._identify_blockers(failed_nets)
                    if blockers:
                        if self.verbose:
                            print(f"  Nudging {len(blockers)} blocking components")
                        self._nudge_blockers(blockers)
                        continue  # Retry after nudging
                    else:
                        return OptimizationResult(
                            success=False,
                            pcb_path=self.pcb_path,
                            routes=routes,
                            placement_conflicts=conflicts,
                            iterations=iteration + 1,
                            message=f"Could not route {len(failed_nets)} nets, no blockers found",
                        )
                else:
                    return OptimizationResult(
                        success=False,
                        pcb_path=self.pcb_path,
                        routes=routes,
                        iterations=iteration + 1,
                        message=f"Could not route {len(failed_nets)} nets (placement locked)",
                    )

            # Phase 3: DRC check
            if not skip_drc and self.drc_checker_factory is not None:
                drc_results = self._run_drc_phase()

                if not drc_results.passed:
                    if self.verbose:
                        error_count = len(drc_results.errors)
                        warning_count = len(drc_results.warnings)
                        print(f"  DRC: {error_count} errors, {warning_count} warnings")

                    # Try to fix DRC violations by adjusting routing
                    if self._fix_drc_violations(drc_results):
                        continue  # Retry after fixing

                    return OptimizationResult(
                        success=False,
                        pcb_path=self.pcb_path,
                        routes=routes,
                        placement_conflicts=conflicts,
                        drc_results=drc_results,
                        iterations=iteration + 1,
                        message=f"DRC violations: {len(drc_results.errors)} errors",
                    )

            # Success!
            if self.verbose:
                print(f"\n✓ Optimization converged in {iteration + 1} iterations!")
                print(f"  Routes: {len(routes)}")

            return OptimizationResult(
                success=True,
                pcb_path=self.pcb_path,
                routes=routes,
                iterations=iteration + 1,
                message="DRC-clean routing achieved",
            )

        # Max iterations exceeded
        return OptimizationResult(
            success=False,
            pcb_path=self.pcb_path,
            routes=self._current_routes,
            iterations=max_iterations,
            message=f"Max iterations ({max_iterations}) exceeded",
        )

    def _run_placement_phase(self) -> list[Conflict]:
        """Run placement conflict detection.

        Returns:
            List of detected placement conflicts
        """
        if self.verbose:
            print("  Phase 1: Checking placement...")

        conflicts = self.analyzer.find_conflicts(self.pcb_path)

        if self.verbose:
            if conflicts:
                print(f"    Found {len(conflicts)} conflicts")
            else:
                print("    No placement conflicts")

        return conflicts

    def _run_routing_phase(self) -> tuple[list[Route], list[int]]:
        """Run autorouting phase.

        Returns:
            Tuple of (routes, failed_net_ids)
        """
        if self.verbose:
            print("  Phase 2: Routing...")

        # Create fresh router
        router = self.router_factory()

        # Get total nets to route
        total_nets = len([n for n in router.nets if n != 0])

        # Route all nets
        routes = router.route_all()

        # Determine which nets failed
        routed_nets = {r.net for r in routes if r.net != 0}
        all_nets = {n for n in router.nets if n != 0}
        failed_nets = list(all_nets - routed_nets)

        self._current_routes = routes
        self._failed_nets = failed_nets

        if self.verbose:
            print(f"    Routed {len(routed_nets)}/{total_nets} nets")
            if failed_nets:
                print(f"    Failed nets: {failed_nets[:5]}{'...' if len(failed_nets) > 5 else ''}")

        return routes, failed_nets

    def _run_drc_phase(self) -> DRCResults:
        """Run DRC checking phase.

        Returns:
            DRCResults from the checker
        """
        from kicad_tools.schema.pcb import PCB

        if self.verbose:
            print("  Phase 3: DRC check...")

        # Load current PCB state
        pcb = PCB.load(str(self.pcb_path))

        # Create checker and run
        checker = self.drc_checker_factory(pcb)
        results = checker.check_all()

        if self.verbose:
            if results.passed:
                print("    DRC passed!")
            else:
                print(f"    DRC: {len(results.errors)} errors, {len(results.warnings)} warnings")

        return results

    def _identify_blockers(self, failed_nets: list[int]) -> list[str]:
        """Identify components that may be blocking routing of failed nets.

        Uses heuristics to find which components are likely preventing
        routing of the failed nets.

        Args:
            failed_nets: List of net IDs that failed to route

        Returns:
            List of component references that may be blocking routes
        """
        # Create router to get net information
        router = self.router_factory()

        blockers: set[str] = set()

        for net_id in failed_nets:
            pads = router.nets.get(net_id, [])
            if len(pads) < 2:
                continue

            # Get pad positions for this net
            pad_positions = []
            for ref, pin in pads:
                if (ref, str(pin)) in router.pads:
                    pad = router.pads[(ref, str(pin))]
                    pad_positions.append((ref, pad.x, pad.y))

            if len(pad_positions) < 2:
                continue

            # Find components that lie between the net's pads
            # (Simple heuristic: components whose center is within
            # the bounding box of the net's pads)
            xs = [p[1] for p in pad_positions]
            ys = [p[2] for p in pad_positions]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)

            # Add margin
            margin = 2.0  # mm
            min_x -= margin
            max_x += margin
            min_y -= margin
            max_y += margin

            # Check which other components fall in this region
            net_refs = {p[0] for p in pad_positions}

            for comp in self.analyzer.get_components():
                if comp.reference in net_refs:
                    continue  # Skip components that are part of this net

                cx, cy = comp.position.x, comp.position.y
                if min_x <= cx <= max_x and min_y <= cy <= max_y:
                    blockers.add(comp.reference)

        return list(blockers)

    def _nudge_blockers(self, blockers: list[str]) -> None:
        """Move blocking components slightly to allow routing.

        Applies small displacements to potentially blocking components
        to create routing channels.

        Args:
            blockers: List of component references to nudge
        """
        from kicad_tools.placement.conflict import Point
        from kicad_tools.placement.fixer import PlacementFix

        # Create synthetic fixes to move blockers
        fixes: list[PlacementFix] = []

        for ref in blockers[:3]:  # Limit to avoid too many changes
            # Try small moves in each direction
            # Pick direction based on component position relative to board center
            components = self.analyzer.get_components()
            comp = next((c for c in components if c.reference == ref), None)

            if comp is None:
                continue

            # Default nudge: move away from center
            board_edge = self.analyzer.get_board_edge()
            if board_edge:
                center_x = (board_edge.min_x + board_edge.max_x) / 2
                center_y = (board_edge.min_y + board_edge.max_y) / 2
            else:
                center_x, center_y = 50.0, 50.0  # Default

            # Direction away from center
            dx = 0.5 if comp.position.x > center_x else -0.5
            dy = 0.5 if comp.position.y > center_y else -0.5

            # Create a minimal fix structure
            fix = PlacementFix(
                conflict=None,  # type: ignore[arg-type]
                component=ref,
                move_vector=Point(dx, dy),
                confidence=0.5,
            )
            fixes.append(fix)

        if fixes and self.verbose:
            print(f"    Nudging: {[f.component for f in fixes]}")

        # Apply the nudges
        if fixes:
            self.fixer.apply_fixes(self.pcb_path, fixes)

    def _fix_drc_violations(self, drc_results: DRCResults) -> bool:
        """Attempt to fix DRC violations.

        Currently a placeholder - returns False to indicate no fix applied.
        Future implementations could:
        - Reroute specific nets causing violations
        - Widen traces that violate minimum width
        - Increase clearances

        Args:
            drc_results: DRC results containing violations

        Returns:
            True if fixes were applied (should retry), False otherwise
        """
        # For now, we don't have automated DRC fixing
        # This is a hook for future implementation
        return False
