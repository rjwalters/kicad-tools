"""Integrated place-route-DRC optimization loop.

This module provides the PlaceRouteOptimizer class that iterates between
placement optimization, routing, and DRC checking to achieve DRC-clean
routing automatically.

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.optim import PlaceRouteOptimizer
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

import os
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.optim.workflow import OptimizationResult

if TYPE_CHECKING:
    from kicad_tools.placement.analyzer import PlacementAnalyzer
    from kicad_tools.placement.conflict import Conflict
    from kicad_tools.placement.fixer import PlacementFixer
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.primitives import Route
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.checker import DRCChecker
    from kicad_tools.validate.violations import DRCResults

__all__ = ["PlaceRouteOptimizer"]


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
        >>> from kicad_tools.optim import PlaceRouteOptimizer
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
        escape_routing: bool | None = None,
        max_fix_attempts: int = 3,
        fixed_refs: set[str] | list[str] | None = None,
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
            escape_routing: Enable escape routing for dense packages.
                True = always use escape routing before global routing.
                False = never use escape routing.
                None = auto-detect dense packages (default).
            max_fix_attempts: Maximum DRC fix attempts per optimization
                iteration before giving up and reporting failure.
            fixed_refs: Optional set/list of component references that must
                not move during optimization. The optimizer wires this into
                the ``PlacementFixer.anchored`` set so that placement-conflict
                fixes won't move these components, and filters anchored refs
                out of the blocker-nudge phase.
        """
        self.pcb_path = Path(pcb_path)
        self.analyzer = analyzer
        self.fixer = fixer
        self.router_factory = router_factory
        self.drc_checker_factory = drc_checker_factory
        self.verbose = verbose
        self.escape_routing = escape_routing
        self.max_fix_attempts = max_fix_attempts
        # Normalize to a frozen set; expose as public attribute for inspection.
        self.fixed_refs: set[str] = set(fixed_refs or [])

        # Make sure the fixer honors the same anchored set. The fixer already
        # supports ``anchored`` in ``_choose_component_to_move``; merge any
        # refs the caller pre-configured on the fixer with our own list so
        # the union is anchored.
        if self.fixed_refs:
            existing = getattr(self.fixer, "anchored", None) or set()
            self.fixer.anchored = set(existing) | self.fixed_refs

        # Track state across iterations
        self._current_routes: list[Route] = []
        self._failed_nets: list[int] = []
        self._fix_attempts: int = 0

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
        fixed_refs: set[str] | list[str] | None = None,
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
            fixed_refs: Optional set/list of component references that must
                not move during optimization. Forwarded to the constructor
                and the underlying ``PlacementFixer.anchored`` set.

        Returns:
            Configured PlaceRouteOptimizer ready to run

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> optimizer = PlaceRouteOptimizer.from_pcb(
            ...     pcb,
            ...     manufacturer="jlcpcb",
            ...     layers=4,
            ...     fixed_refs={"J1"},
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

        # Normalize fixed_refs into a set early so we can pass it to the fixer.
        anchored: set[str] = set(fixed_refs or [])

        # Create components. PlacementFixer already honors ``anchored`` in
        # ``_choose_component_to_move`` (see placement/fixer.py).
        analyzer = PlacementAnalyzer(verbose=verbose)
        fixer = PlacementFixer(verbose=verbose, anchored=anchored)

        # Router factory - creates fresh router for each attempt
        # Issue #2708: forward manufacturer so capability-gated routing
        # features (e.g., via_in_pad_supported) opt in correctly.
        def router_factory() -> Autorouter:
            router = Autorouter(
                width=board_width,
                height=board_height,
                rules=RouterDesignRules(manufacturer=manufacturer),
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
            fixed_refs=anchored,
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

            # Transform pad positions.  KiCad rotation is positive
            # counter-clockwise; the standard 2D rotation matrix applies
            # directly (no negation).  Matches PCB.get_pad_position.
            rot_rad = math.radians(rotation)
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
        cancel_flag: Callable[[], bool] | None = None,
        checkpoint_interval: int = 0,
    ) -> OptimizationResult:
        """Run the optimization loop.

        Iterates between placement, routing, and DRC until clean or max iterations.

        Args:
            max_iterations: Maximum iterations before giving up
            allow_placement_changes: Whether to modify component placements
            skip_drc: Skip DRC checking (useful for faster iteration)
            cancel_flag: Optional callable returning True when cancellation
                is requested. Checked at the start of each iteration.
            checkpoint_interval: Save a checkpoint copy of the PCB every N
                iterations. 0 (default) disables periodic checkpoints.
                Checkpoint files are written atomically (temp + rename) to
                prevent corruption on hard kill.

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
            # Cooperative cancellation check
            if cancel_flag is not None and cancel_flag():
                return OptimizationResult(
                    success=False,
                    pcb_path=self.pcb_path,
                    routes=self._current_routes,
                    iterations=iteration,
                    message="Cancelled via cancel_flag",
                )
            if self.verbose:
                print(f"\n--- Iteration {iteration + 1}/{max_iterations} ---")

            # Reset per-iteration DRC fix attempt counter
            self._fix_attempts = 0

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

            # Periodic checkpoint save (atomic write)
            if checkpoint_interval > 0 and (iteration + 1) % checkpoint_interval == 0:
                self._save_checkpoint()

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
                print(f"\nOptimization converged in {iteration + 1} iterations!")
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

    def _save_checkpoint(self) -> None:
        """Save a checkpoint copy of the PCB using atomic write.

        Writes to a temp file in the same directory and renames, so a
        hard kill during the write cannot corrupt the checkpoint.
        """
        checkpoint_path = self.pcb_path.with_suffix(".checkpoint.kicad_pcb")
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.pcb_path.parent),
                prefix=".checkpoint_",
                suffix=".tmp",
            )
            os.close(fd)
            shutil.copy2(str(self.pcb_path), tmp_path)
            Path(tmp_path).replace(checkpoint_path)
            if self.verbose:
                print(f"  Checkpoint saved: {checkpoint_path}")
        except Exception as e:
            if self.verbose:
                print(f"  Warning: checkpoint save failed: {e}")
            # Clean up temp file if it exists
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass

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

        Uses escape routing when dense packages are detected (or when
        escape_routing is explicitly enabled). Escape routing runs as a
        pre-phase that generates escape routes for dense package pins
        before global routing begins.

        Returns:
            Tuple of (routes, failed_net_ids)
        """
        if self.verbose:
            print("  Phase 2: Routing...")

        # Create fresh router
        router = self.router_factory()

        # Get total nets to route
        total_nets = len([n for n in router.nets if n != 0])

        # Determine whether to use escape routing
        use_escape = self._should_use_escape_routing(router)

        if use_escape:
            routes = router.route_with_escape()
        else:
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

    def _should_use_escape_routing(self, router: Autorouter) -> bool:
        """Determine whether escape routing should be used.

        Checks the escape_routing flag: True forces it on, False forces
        it off, and None (default) auto-detects by scanning for dense
        packages.

        Args:
            router: The Autorouter instance to check for dense packages.

        Returns:
            True if escape routing should be used.
        """
        if self.escape_routing is True:
            if self.verbose:
                print("    Escape routing: enabled (explicit)")
            return True
        if self.escape_routing is False:
            return False

        # Auto-detect: check for dense packages
        if not hasattr(router, "detect_dense_packages"):
            return False
        dense_packages = router.detect_dense_packages()
        if isinstance(dense_packages, list) and len(dense_packages) > 0:
            if self.verbose:
                refs = [p.ref for p in dense_packages]
                print(f"    Escape routing: auto-enabled (dense packages: {refs})")
            return True
        return False

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
        to create routing channels. Components listed in ``self.fixed_refs``
        are never nudged.

        Args:
            blockers: List of component references to nudge
        """
        from kicad_tools.placement.conflict import Point
        from kicad_tools.placement.fixer import PlacementFix

        # Filter out anchored / fixed components before slicing.
        if self.fixed_refs:
            filtered_blockers = [b for b in blockers if b not in self.fixed_refs]
            if self.verbose and len(filtered_blockers) != len(blockers):
                skipped = [b for b in blockers if b in self.fixed_refs]
                print(f"    Skipping fixed blockers: {skipped}")
        else:
            filtered_blockers = blockers

        # Create synthetic fixes to move blockers
        fixes: list[PlacementFix] = []

        for ref in filtered_blockers[:3]:  # Limit to avoid too many changes
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
        """Attempt to fix DRC violations using ClearanceRepairer.

        Converts the validate-module ``DRCResults`` into a ``DRCReport``
        understood by the repair tools, then runs ``ClearanceRepairer``
        to nudge traces/vias for clearance violations.  A per-iteration
        attempt counter (``_fix_attempts``) prevents infinite fix-retry
        cycles when the repairer makes changes that re-introduce the
        same violations.

        Args:
            drc_results: DRC results containing violations

        Returns:
            True if fixes were applied (should retry), False otherwise
        """
        # Guard: respect per-iteration attempt limit
        if self._fix_attempts >= self.max_fix_attempts:
            if self.verbose:
                print(f"    DRC fix: max attempts ({self.max_fix_attempts}) reached, giving up")
            return False

        self._fix_attempts += 1

        # No violations to fix
        if not drc_results.violations:
            return False

        try:
            from kicad_tools.drc.compat import drc_results_to_report
            from kicad_tools.drc.repair_clearance import ClearanceRepairer

            # Convert validate DRCResults -> drc DRCReport
            report = drc_results_to_report(drc_results, self.pcb_path)

            if not report.violations:
                return False

            # Run ClearanceRepairer (non-destructive nudge/reroute)
            repairer = ClearanceRepairer(str(self.pcb_path))
            result = repairer.repair_from_report(
                report,
                max_displacement=0.5,
                local_reroute=True,
            )

            if self.verbose:
                print(
                    f"    DRC fix attempt {self._fix_attempts}/"
                    f"{self.max_fix_attempts}: "
                    f"{result.repaired}/{result.total_violations} repaired"
                )

            if result.repaired > 0:
                repairer.save()
                return True

        except Exception as exc:
            if self.verbose:
                print(f"    DRC fix: error during repair: {exc}")

        return False
