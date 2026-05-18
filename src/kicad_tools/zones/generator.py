"""
Zone generator for creating copper pour zones on PCBs.

This module provides a high-level API for generating copper pour zones,
with automatic board outline detection and sensible defaults for power nets.

Outline allocator (#2771, #3043)
--------------------------------

In addition to the layer/priority allocator (:func:`_assign_layers_for_pour_nets`),
this module owns the *outline* allocator (:func:`_compute_pour_outlines`).
The outline allocator runs after layer assignment and decides whether each
zone should use the full board outline (when it is the only zone on its
layer) or a per-net bounding region derived from that net's pads (when it
shares a layer with another zone).

This is the fix for the "all power nets share F.Cu with identical board
outlines, so the highest-priority zone wins everything" failure documented
in #2771.  Earlier issues #2410 / #2041 / #2593 made the priorities
distinct, which silenced the warning, but distinct priorities alone do not
produce real copper for the losing zones -- the outlines have to be
geometrically disjoint for KiCad's fill resolver to award copper to more
than one zone on a shared layer.

The original PR #2771 only computed each net's bbox independently and
relied on pad-cluster separation to keep the bboxes disjoint.  On real
boards where power-net pads are *spatially interleaved* (e.g. board 06's
``+3V3``/``+1V8``/``+1V2`` cluster on a BGA), the bboxes overlap and the
silent-override returns.  Issue #3043 extends the allocator with a
second pass that *subtracts higher-priority bboxes from lower-priority
outlines* (using Shapely's polygon difference), so the resulting
outlines are disjoint regardless of pad layout.
"""

from __future__ import annotations

import math
import sys
import uuid as uuid_module
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.core.sexp_file import save_pcb, verify_pcb_write
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import SExp, parse_file
from kicad_tools.sexp.builders import zone_node

if TYPE_CHECKING:
    from kicad_tools.router.net_class import NetClass


@dataclass
class ZoneOverlapWarning:
    """Warning about overlapping zones on the same layer.

    Attributes:
        new_net: Net name of the zone being added
        existing_net: Net name of the existing zone that overlaps
        layer: The shared copper layer
        message: Human-readable warning message
    """

    new_net: str
    existing_net: str
    layer: str
    message: str


@dataclass
class ZoneConfig:
    """Configuration for a copper pour zone.

    Attributes:
        net: Net name (e.g., "GND", "+3.3V")
        layer: Copper layer (e.g., "B.Cu", "F.Cu")
        priority: Zone fill priority (higher = fills later, on top)
        clearance: Clearance to other nets in mm
        min_thickness: Minimum copper thickness in mm
        thermal_gap: Thermal relief gap in mm
        thermal_bridge_width: Thermal relief spoke width in mm
        boundary: Custom boundary polygon, or None for board outline
    """

    net: str
    layer: str
    priority: int = 0
    clearance: float = 0.3
    min_thickness: float = 0.25
    thermal_gap: float = 0.3
    thermal_bridge_width: float = 0.4
    boundary: list[tuple[float, float]] | None = None


@dataclass
class GeneratedZone:
    """A generated zone ready for insertion.

    Attributes:
        config: The zone configuration used
        net_number: Resolved net number
        boundary: The actual boundary polygon used
        uuid: Generated UUID for the zone
    """

    config: ZoneConfig
    net_number: int
    boundary: list[tuple[float, float]]
    uuid: str = field(default_factory=lambda: str(uuid_module.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build S-expression node for this zone."""
        return zone_node(
            self.net_number,
            self.config.net,
            self.config.layer,
            self.boundary,
            self.uuid,
            self.config.priority,
            self.config.min_thickness,
            self.config.clearance,
            self.config.thermal_gap,
            self.config.thermal_bridge_width,
        )


class ZoneGenerator:
    """High-level zone generator for PCB copper pours.

    Provides an easy-to-use API for adding zones to PCB files with:
    - Automatic board outline detection for zone boundaries
    - Net name to net number resolution
    - Sensible defaults for power net zones (thermal relief, etc.)

    Example::

        gen = ZoneGenerator.from_pcb("board.kicad_pcb")

        # Add ground plane using board outline as boundary
        gen.add_zone(net="GND", layer="B.Cu", priority=1)

        # Add power plane with lower priority
        gen.add_zone(net="+3.3V", layer="F.Cu", priority=0)

        # Generate zones and save
        gen.save("board_with_zones.kicad_pcb")
    """

    def __init__(
        self,
        pcb: PCB,
        doc: SExp | None = None,
        edge_clearance: float | None = None,
    ):
        """Initialize zone generator.

        Args:
            pcb: Parsed PCB object
            doc: Raw S-expression document (for modification)
            edge_clearance: If set, inset the auto-derived board outline
                by this many mm so zone copper does not extend to the
                board edge.  Only affects the automatic board-outline
                boundary; explicit ``boundary`` arguments to
                :meth:`add_zone` are never modified.
        """
        self._pcb = pcb
        self._doc = doc
        self._edge_clearance = edge_clearance
        self._zones: list[GeneratedZone] = []
        self._warnings: list[ZoneOverlapWarning] = []
        self._board_outline: list[tuple[float, float]] | None = None
        self._applied = False

    @classmethod
    def from_pcb(
        cls,
        path: str | Path,
        edge_clearance: float | None = None,
    ) -> ZoneGenerator:
        """Load PCB and create zone generator.

        Args:
            path: Path to .kicad_pcb file
            edge_clearance: Optional edge clearance in mm (see
                :meth:`__init__` for details).

        Returns:
            ZoneGenerator instance
        """
        path = Path(path)
        pcb = PCB.load(str(path))
        doc = parse_file(path)
        return cls(pcb, doc, edge_clearance=edge_clearance)

    @property
    def pcb(self) -> PCB:
        """The loaded PCB object."""
        return self._pcb

    @property
    def board_outline(self) -> list[tuple[float, float]]:
        """Get board outline polygon in sheet-absolute coordinates.

        Uses cached outline if available, otherwise extracts from PCB.
        Falls back to a default rectangle if no Edge.Cuts layer found.

        When *edge_clearance* was set at construction time, the outline
        is inset by that distance using Shapely's ``buffer(-clearance)``
        so that zone copper does not extend to the board edge.

        Zone boundaries written to the PCB file must be in sheet-absolute
        coordinates. ``get_board_outline()`` returns board-relative coords,
        so we add the board origin back.

        Note: this property is the source of the sheet-absolute ``boundary``
        argument passed to ``_check_overlap()``. Since PR #2753 normalized
        ``Zone.polygon`` to board-relative coordinates on load, the overlap
        comparison must reconcile the two frames; ``_check_overlap()`` does
        so by shifting ``existing.polygon`` by the board origin before
        computing the AABB intersection (see #2759).
        """
        if self._board_outline is None:
            outline = self._pcb.get_board_outline()
            if outline:
                # Convert board-relative back to sheet-absolute for PCB output
                ox, oy = self._pcb.board_origin
                if ox != 0.0 or oy != 0.0:
                    outline = [(x + ox, y + oy) for x, y in outline]
                self._board_outline = outline
            else:
                # Fallback: create outline from board bounds
                self._board_outline = self._estimate_board_bounds()

            # Apply edge clearance inset if configured
            if self._edge_clearance and self._edge_clearance > 0:
                try:
                    self._board_outline = self._inset_polygon(
                        self._board_outline, self._edge_clearance
                    )
                except ImportError:
                    # Shapely unavailable -- use pure-Python rect fallback
                    # for axis-aligned rectangular outlines (covers the
                    # majority of hobby/demo boards).
                    if self._is_axis_aligned_rect(self._board_outline):
                        self._board_outline = self._inset_rect(
                            self._board_outline, self._edge_clearance
                        )
                    else:
                        import warnings

                        warnings.warn(
                            "shapely is required for edge_clearance inset "
                            "on non-rectangular board outlines "
                            "(install with: pip install kicad-tools[geometry]). "
                            "Zone boundary will use exact board outline.",
                            stacklevel=2,
                        )
        return self._board_outline

    @staticmethod
    def _is_axis_aligned_rect(
        coords: list[tuple[float, float]],
    ) -> bool:
        """Check whether *coords* form an axis-aligned rectangle.

        Returns ``True`` when the polygon has exactly 4 unique vertices
        whose bounding box matches the vertex coordinates (i.e. all
        corners sit at the extremes of the bounding box).  Handles
        closed polygons where the first point is repeated at the end.
        """
        # Strip closing duplicate if present
        if len(coords) == 5 and coords[0] == coords[-1]:
            coords = coords[:4]
        if len(coords) != 4:
            return False
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        # Width and height must be positive
        if x_max - x_min < 1e-6 or y_max - y_min < 1e-6:
            return False
        # Every vertex must be at a corner of the bounding box
        corners = {(x_min, y_min), (x_min, y_max), (x_max, y_min), (x_max, y_max)}
        return all((round(x, 6), round(y, 6)) in corners for x, y in coords)

    @staticmethod
    def _inset_rect(
        coords: list[tuple[float, float]],
        distance: float,
    ) -> list[tuple[float, float]]:
        """Shrink an axis-aligned rectangle inward by *distance* mm.

        Pure-Python fallback that does not require Shapely.  Returns the
        original coordinates unchanged if the inset would collapse the
        rectangle (width or height < 2 * distance).  Handles closed
        polygons where the first point is repeated at the end.
        """
        # Strip closing duplicate if present
        if len(coords) == 5 and coords[0] == coords[-1]:
            coords = coords[:4]
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)

        new_x_min = x_min + distance
        new_x_max = x_max - distance
        new_y_min = y_min + distance
        new_y_max = y_max - distance

        # Collapsed -- fall back to original
        if new_x_min >= new_x_max or new_y_min >= new_y_max:
            return coords

        return [
            (round(new_x_min, 6), round(new_y_min, 6)),
            (round(new_x_max, 6), round(new_y_min, 6)),
            (round(new_x_max, 6), round(new_y_max, 6)),
            (round(new_x_min, 6), round(new_y_max, 6)),
        ]

    @staticmethod
    def _inset_polygon(
        coords: list[tuple[float, float]],
        distance: float,
    ) -> list[tuple[float, float]]:
        """Shrink a polygon inward by *distance* mm using Shapely.

        If the inset collapses the polygon (e.g. very thin peninsulas),
        returns the original coordinates unchanged.
        """
        from shapely.geometry import Polygon

        poly = Polygon(coords)
        inset = poly.buffer(-distance)

        if inset.is_empty:
            # Collapsed polygon -- fall back to original
            return coords

        # buffer() may return a MultiPolygon if thin regions collapse;
        # keep only the largest polygon.
        if inset.geom_type == "MultiPolygon":
            inset = max(inset.geoms, key=lambda g: g.area)

        # Extract exterior coordinates (Shapely repeats the first point
        # at the end; drop the duplicate).
        exterior_coords = list(inset.exterior.coords)
        if len(exterior_coords) > 1 and exterior_coords[0] == exterior_coords[-1]:
            exterior_coords = exterior_coords[:-1]

        return [(round(x, 6), round(y, 6)) for x, y in exterior_coords]

    def _estimate_board_bounds(self) -> list[tuple[float, float]]:
        """Estimate board bounds from component positions.

        Used as fallback when no Edge.Cuts outline is found.
        """
        min_x, min_y = float("inf"), float("inf")
        max_x, max_y = float("-inf"), float("-inf")

        for fp in self._pcb.footprints:
            x, y = fp.position
            # Add some padding around components
            min_x = min(min_x, x - 5)
            min_y = min(min_y, y - 5)
            max_x = max(max_x, x + 5)
            max_y = max(max_y, y + 5)

        # Ensure we have valid bounds
        if min_x == float("inf"):
            # No footprints, use a default size
            return [(0, 0), (100, 0), (100, 100), (0, 100)]

        return [
            (min_x, min_y),
            (max_x, min_y),
            (max_x, max_y),
            (min_x, max_y),
        ]

    def get_net_number(self, net_name: str) -> int:
        """Get net number by name.

        Args:
            net_name: Net name (e.g., "GND", "+3.3V")

        Returns:
            Net number, or 0 if not found

        Raises:
            ValueError: If net name not found
        """
        net = self._pcb.get_net_by_name(net_name)
        if net is None:
            raise ValueError(f"Net '{net_name}' not found in PCB")
        return net.number

    @property
    def warnings(self) -> list[ZoneOverlapWarning]:
        """Overlap warnings generated during zone addition."""
        return self._warnings

    @staticmethod
    def _boundaries_overlap(
        boundary_a: list[tuple[float, float]],
        boundary_b: list[tuple[float, float]],
    ) -> bool:
        """Check whether two boundary polygons overlap.

        Uses Shapely's exact polygon intersection when available so the
        outline allocator's carved-out (concave) polygons are correctly
        recognised as disjoint when their AABBs still overlap (#3043).
        Falls back to a conservative AABB-overlap test when Shapely is
        not installed.

        Returns:
            True if the boundaries overlap with positive area.
        """
        if not boundary_a or not boundary_b:
            return False

        # Prefer exact intersection via Shapely so the disjoint-but-AABB-
        # overlapping case (carved-out outlines from ``_compute_pour_outlines``)
        # does not trigger a spurious warning.
        try:
            from shapely.geometry import Polygon

            poly_a = Polygon(boundary_a)
            poly_b = Polygon(boundary_b)
            if not poly_a.is_valid or not poly_b.is_valid:
                # Fall through to AABB on degenerate input.
                raise ValueError("invalid polygon")
            return poly_a.intersection(poly_b).area > 1e-9
        except (ImportError, ValueError):
            pass

        a_xs = [p[0] for p in boundary_a]
        a_ys = [p[1] for p in boundary_a]
        b_xs = [p[0] for p in boundary_b]
        b_ys = [p[1] for p in boundary_b]

        # Axis-aligned bounding-box overlap test (conservative fallback).
        return not (
            max(a_xs) <= min(b_xs)
            or max(b_xs) <= min(a_xs)
            or max(a_ys) <= min(b_ys)
            or max(b_ys) <= min(a_ys)
        )

    def _check_overlap(
        self,
        net: str,
        layer: str,
        priority: int,
        boundary: list[tuple[float, float]],
    ) -> list[ZoneOverlapWarning]:
        """Check for overlapping zones on the same layer.

        Checks both existing PCB zones and zones already queued in
        this generator.

        Coordinate frames:
            * ``boundary`` is sheet-absolute (sourced from
              :attr:`board_outline`, which converts the board-relative
              outline back to sheet-absolute for PCB output -- see PR #2753).
            * ``existing.polygon`` is board-relative on PCBs loaded via
              :class:`PCB` (see ``_detect_board_origin`` in
              ``schema/pcb.py``).  We add the board origin so both polygons
              live in the same frame before the AABB intersection test.
            * Queued zones (``self._zones``) already carry sheet-absolute
              boundaries because :meth:`add_zone` derives them from
              :attr:`board_outline` (or accepts a caller-supplied boundary
              in the same frame), so no conversion is required there.

        Returns:
            List of overlap warnings (empty if no overlaps detected).
        """
        warnings: list[ZoneOverlapWarning] = []
        ox, oy = self._pcb.board_origin

        # Check against existing zones in the PCB
        for existing in self._pcb.zones:
            if existing.layer != layer:
                continue
            if existing.net_name == net:
                continue  # Same net on same layer is fine (e.g. re-run)

            # Bring the board-relative existing polygon (post-PR-2753
            # invariant) into the sheet-absolute frame used by ``boundary``
            # so the AABB intersection test works for non-zero-origin boards.
            if ox != 0.0 or oy != 0.0:
                existing_boundary = [(x + ox, y + oy) for x, y in existing.polygon]
            else:
                existing_boundary = existing.polygon
            if self._boundaries_overlap(boundary, existing_boundary):
                if priority <= existing.priority:
                    msg = (
                        f"Zone '{net}' on {layer} (priority {priority}) overlaps "
                        f"existing zone '{existing.net_name}' (priority {existing.priority}). "
                        f"The new zone will get zero copper because the existing zone "
                        f"has equal or higher priority."
                    )
                else:
                    msg = (
                        f"Zone '{net}' on {layer} (priority {priority}) overlaps "
                        f"existing zone '{existing.net_name}' (priority {existing.priority}). "
                        f"The existing zone will get zero copper."
                    )
                warnings.append(
                    ZoneOverlapWarning(
                        new_net=net,
                        existing_net=existing.net_name,
                        layer=layer,
                        message=msg,
                    )
                )

        # Check against queued zones in this generator
        for queued in self._zones:
            if queued.config.layer != layer:
                continue
            if queued.config.net == net:
                continue

            if self._boundaries_overlap(boundary, queued.boundary):
                if priority <= queued.config.priority:
                    msg = (
                        f"Zone '{net}' on {layer} (priority {priority}) overlaps "
                        f"queued zone '{queued.config.net}' (priority {queued.config.priority}). "
                        f"The new zone will get zero copper because the other zone "
                        f"has equal or higher priority."
                    )
                else:
                    msg = (
                        f"Zone '{net}' on {layer} (priority {priority}) overlaps "
                        f"queued zone '{queued.config.net}' (priority {queued.config.priority}). "
                        f"The other zone will get zero copper."
                    )
                warnings.append(
                    ZoneOverlapWarning(
                        new_net=net,
                        existing_net=queued.config.net,
                        layer=layer,
                        message=msg,
                    )
                )

        return warnings

    def add_zone(
        self,
        net: str,
        layer: str,
        priority: int = 0,
        clearance: float = 0.3,
        min_thickness: float = 0.25,
        thermal_gap: float = 0.3,
        thermal_bridge_width: float = 0.4,
        boundary: list[tuple[float, float]] | None = None,
    ) -> GeneratedZone:
        """Add a copper pour zone.

        Checks for overlapping zones on the same layer and emits warnings
        to stderr if conflicts are detected.

        Args:
            net: Net name (e.g., "GND", "+3.3V")
            layer: Copper layer (e.g., "B.Cu", "F.Cu", "In1.Cu")
            priority: Zone fill priority (higher = fills later)
            clearance: Clearance to other nets in mm
            min_thickness: Minimum copper thickness in mm
            thermal_gap: Thermal relief gap in mm
            thermal_bridge_width: Thermal relief spoke width in mm
            boundary: Custom boundary polygon, or None for board outline

        Returns:
            GeneratedZone object

        Raises:
            ValueError: If net not found in PCB
        """
        config = ZoneConfig(
            net=net,
            layer=layer,
            priority=priority,
            clearance=clearance,
            min_thickness=min_thickness,
            thermal_gap=thermal_gap,
            thermal_bridge_width=thermal_bridge_width,
            boundary=boundary,
        )

        # Resolve net number
        net_number = self.get_net_number(net)

        # Use board outline if no boundary specified
        actual_boundary = boundary if boundary is not None else self.board_outline

        # Check for overlapping zones on the same layer
        overlap_warnings = self._check_overlap(net, layer, priority, actual_boundary)
        for warning in overlap_warnings:
            self._warnings.append(warning)
            print(f"WARNING: {warning.message}", file=sys.stderr)

        zone = GeneratedZone(
            config=config,
            net_number=net_number,
            boundary=actual_boundary,
        )

        self._zones.append(zone)
        return zone

    def add_ground_plane(
        self,
        layer: str = "B.Cu",
        priority: int = 1,
        **kwargs,
    ) -> GeneratedZone:
        """Add a ground plane zone.

        Convenience method for adding GND zones with appropriate defaults.

        Args:
            layer: Copper layer (default: "B.Cu" for bottom layer ground)
            priority: Zone priority (default: 1, higher than power)
            **kwargs: Additional arguments passed to add_zone()

        Returns:
            GeneratedZone object
        """
        return self.add_zone(net="GND", layer=layer, priority=priority, **kwargs)

    def add_power_plane(
        self,
        net: str,
        layer: str = "F.Cu",
        priority: int = 0,
        **kwargs,
    ) -> GeneratedZone:
        """Add a power plane zone.

        Convenience method for adding power net zones.

        Args:
            net: Power net name (e.g., "+3.3V", "+5V", "VCC")
            layer: Copper layer (default: "F.Cu" for top layer)
            priority: Zone priority (default: 0, lower than ground)
            **kwargs: Additional arguments passed to add_zone()

        Returns:
            GeneratedZone object
        """
        return self.add_zone(net=net, layer=layer, priority=priority, **kwargs)

    @property
    def zones(self) -> list[GeneratedZone]:
        """List of zones to be generated."""
        return self._zones

    def generate_sexp(self) -> str:
        """Generate S-expression string for all zones.

        Returns:
            S-expression string for inserting into PCB file
        """
        if not self._zones:
            return ""

        parts = []
        for zone in self._zones:
            parts.append(zone.to_sexp_node().to_string(indent=1))

        return "\n".join(parts)

    def apply(self) -> None:
        """Apply zones to the loaded document.

        Modifies the internal document by appending zone definitions.
        Call save() after this to write changes to disk.

        Safe to call multiple times -- zones are only appended once.
        """
        if not self._doc:
            raise ValueError("No document loaded - use from_pcb() to load a PCB")

        if self._applied:
            return

        for zone in self._zones:
            self._doc.append(zone.to_sexp_node())

        self._applied = True

    def save(self, output_path: str | Path | None = None) -> Path:
        """Save PCB with generated zones and verify persistence.

        Args:
            output_path: Output file path

        Returns:
            Path to the output file

        Raises:
            WriteVerificationError: If zones are missing from the written file.
        """
        if not self._doc:
            raise ValueError("No document loaded - use from_pcb() to load a PCB")

        # Apply zones if not already applied
        self.apply()

        if output_path is None:
            raise ValueError("Output path required")

        output_path = Path(output_path)
        save_pcb(self._doc, output_path)

        # Post-write verification: re-read and confirm zones are present
        if self._zones:
            verify_pcb_write(output_path, expected_zones=len(self._zones))

        return output_path

    def get_statistics(self) -> dict:
        """Get statistics about generated zones.

        Returns:
            Dictionary with zone generation statistics
        """
        return {
            "zone_count": len(self._zones),
            "zones": [
                {
                    "net": z.config.net,
                    "layer": z.config.layer,
                    "priority": z.config.priority,
                    "boundary_points": len(z.boundary),
                }
                for z in self._zones
            ],
        }


def parse_power_nets(spec: str) -> list[tuple[str, str]]:
    """Parse power nets specification string.

    Parses format: "NET1:LAYER1,NET2:LAYER2,..."
    e.g., "GND:B.Cu,+3.3V:F.Cu"

    Args:
        spec: Power nets specification string

    Returns:
        List of (net_name, layer) tuples

    Raises:
        ValueError: If format is invalid
    """
    if not spec or not spec.strip():
        return []

    result = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue

        if ":" not in item:
            raise ValueError(
                f"Invalid power net format: '{item}'. Expected 'NET:LAYER' (e.g., 'GND:B.Cu')"
            )

        parts = item.split(":", 1)
        net_name = parts[0].strip()
        layer = parts[1].strip()

        if not net_name:
            raise ValueError(f"Empty net name in: '{item}'")
        if not layer:
            raise ValueError(f"Empty layer in: '{item}'")

        result.append((net_name, layer))

    return result


def _order_ground_nets_canonically(
    ground_nets: Sequence[tuple[str, NetClass]],
) -> list[tuple[str, NetClass]]:
    """Order ground nets so the canonical "system ground" is first.

    The canonical ground (assigned to the most-preferred layer/priority,
    e.g. ``In1.Cu`` on a 4-layer board) is selected by:

    1. Exact name match ``GND`` if present.
    2. Else, alphabetical first.

    Note: the issue's curator notes mention pin count as a tie-breaker
    proxy for "system ground", but pin counts are not available at this
    layer of the API (the function receives only ``(name, NetClass)``
    pairs).  Falling back to alphabetical ordering keeps the assignment
    deterministic and reproducible across runs without an API change.
    The remaining grounds are emitted in alphabetical order so the same
    set of split-ground nets always produces the same layer assignment.
    """
    if not ground_nets:
        return []

    # Stable alphabetical order first
    sorted_nets = sorted(ground_nets, key=lambda nc: nc[0])

    # Promote exact "GND" to head if present
    for i, (name, _) in enumerate(sorted_nets):
        if name == "GND":
            return [sorted_nets[i]] + sorted_nets[:i] + sorted_nets[i + 1 :]
    return sorted_nets


def _assign_layers_for_pour_nets(
    copper_layer_count: int,
    pour_nets: list[tuple[str, NetClass]],
) -> list[tuple[str, str, int]]:
    """Assign layers and priorities for pour nets based on board stackup.

    Single-ground designs (the common case):

    - 2-layer: GROUND -> ``B.Cu`` priority 1; first POWER -> ``F.Cu`` with
      a descending priority scheme so multiple power nets coexist.
    - 4-layer: GROUND -> ``In1.Cu`` priority 1; first POWER -> ``In2.Cu``
      priority 0; additional POWER -> ``F.Cu`` with non-zero priorities.

    Split-ground designs (mixed-signal boards with multiple distinct
    ``NetClass.GROUND`` nets, e.g. ``GNDA`` + ``GNDD``):

    - 2-layer: each ground gets ``B.Cu`` with a *distinct* descending
      priority (canonical first).  True plane separation is impossible
      on a 2-layer stack; the priority distinction is the best the
      generator can do.  The router still emits its overlap warning so
      the user knows to migrate to a 4-layer stack.
    - 4-layer: the first two grounds split across the two inner layers
      (``In1.Cu`` and ``In2.Cu``, both priority 1), giving each ground
      domain a dedicated plane.  Power must move to ``F.Cu`` because
      both inner layers are now reserved for ground planes.  Additional
      grounds (>2) fall back to ``B.Cu`` with distinct descending
      priorities and a stderr warning is emitted -- >2 ground domains
      are unusual and require manual stackup planning.

    Canonical-ground selection (which ground gets the "best" slot):

    1. Exact net name ``GND`` if present.
    2. Else, alphabetical first.

    See ``_order_ground_nets_canonically`` for the full rule.

    Args:
        copper_layer_count: Number of copper layers (2, 4, 6, etc.)
        pour_nets: List of (net_name, NetClass) tuples

    Returns:
        List of (net_name, layer, priority) tuples.  Every ground-class
        net has a *distinct* (layer, priority) pair so no ground zone
        gets silently overridden to zero copper.
    """
    from kicad_tools.router.net_class import NetClass

    ground_nets_raw = [(n, c) for n, c in pour_nets if c == NetClass.GROUND]
    power_nets = [(n, c) for n, c in pour_nets if c != NetClass.GROUND]

    # Canonicalize ground ordering so split-ground assignment is stable.
    ground_nets = _order_ground_nets_canonically(ground_nets_raw)
    split_ground = len(ground_nets) > 1

    if split_ground:
        ground_names = ", ".join(n for n, _ in ground_nets)
        print(
            f"Auto-pour: split-ground detected ({ground_names}) "
            f"-- assigning distinct layers/priorities so each ground "
            f"domain receives copper",
            file=sys.stderr,
        )

    assignments: list[tuple[str, str, int]] = []

    if copper_layer_count >= 4:
        # 4+ layer board: use inner layers for power/ground planes
        if not split_ground:
            # Common case: single ground on In1.Cu, power tree on In2.Cu/F.Cu
            for net_name, _ in ground_nets:
                assignments.append((net_name, "In1.Cu", 1))

            if len(power_nets) == 1:
                # Single power net gets its own inner layer
                assignments.append((power_nets[0][0], "In2.Cu", 0))
            else:
                # Multiple power nets: first gets In2.Cu, rest go on F.Cu
                # with decreasing priorities so they don't fully override each other.
                # NOTE: Full-board overlapping zones on the same layer still produce
                # zero-copper for lower-priority zones.  The overlap warning will
                # fire, prompting the user to use smaller boundaries or `zones split`.
                for i, (net_name, _) in enumerate(power_nets):
                    if i == 0:
                        assignments.append((net_name, "In2.Cu", 0))
                    else:
                        assignments.append((net_name, "F.Cu", i))
        else:
            # Split-ground: dedicate both inner layers to ground domains.
            # First two grounds get In1.Cu and In2.Cu (both priority 1 --
            # they are on distinct layers so there is no overlap conflict).
            assignments.append((ground_nets[0][0], "In1.Cu", 1))
            assignments.append((ground_nets[1][0], "In2.Cu", 1))

            # >2 grounds: spill to B.Cu with distinct descending priorities
            # and warn the user.
            extra_grounds = ground_nets[2:]
            if extra_grounds:
                extra_names = ", ".join(n for n, _ in extra_grounds)
                print(
                    f"WARNING: more than 2 ground domains ({extra_names}) "
                    f"-- placing on B.Cu with distinct priorities; "
                    f"manual stackup planning recommended for >2 ground domains",
                    file=sys.stderr,
                )
                for i, (net_name, _) in enumerate(extra_grounds):
                    # Descending priorities starting from len(extra) so they
                    # are distinct on the same layer.
                    assignments.append((net_name, "B.Cu", len(extra_grounds) - i))

            # Power moves to F.Cu because both inner layers are reserved.
            # 1 power net -> F.Cu priority 0 (matches single-power semantics
            # of "highest plane priority" being zero).  N power nets get
            # distinct descending priorities so multiple power planes
            # coexist on F.Cu.
            if len(power_nets) == 1:
                assignments.append((power_nets[0][0], "F.Cu", 0))
            else:
                for i, (net_name, _) in enumerate(power_nets):
                    assignments.append((net_name, "F.Cu", len(power_nets) - i))
    else:
        # 2-layer board
        if not split_ground:
            for net_name, _ in ground_nets:
                assignments.append((net_name, "B.Cu", 1))
        else:
            # Split-ground on 2-layer: distinct descending priorities on B.Cu.
            # Plane separation is impossible on this stackup; the priority
            # distinction at least prevents the silent zero-copper bug.
            for i, (net_name, _) in enumerate(ground_nets):
                assignments.append((net_name, "B.Cu", len(ground_nets) - i))

        for i, (net_name, _) in enumerate(power_nets):
            assignments.append((net_name, "F.Cu", len(power_nets) - i))

    return assignments


# ---------------------------------------------------------------------------
# Outline allocator (#2771)
# ---------------------------------------------------------------------------

# Default per-net margin around the pad bounding box, in mm.  1.5 mm gives
# enough copper around each pad cluster for thermal relief and trace exits
# without aggressively encroaching on neighbouring nets.  Empirically chosen
# during the curator analysis of board 05.
DEFAULT_POUR_BBOX_MARGIN_MM = 1.5

# Default side length of the fallback square emitted when a net has only a
# single pad (or all its pads coincide).  Slightly larger than the margin
# alone so single-pad nets still receive a usable copper patch.
SINGLE_PAD_FALLBACK_SIDE_MM = 4.0


def _net_pad_positions_absolute(
    pcb: PCB,
    net_name: str,
) -> list[tuple[float, float]]:
    """Return sheet-absolute (x, y) positions of every pad on ``net_name``.

    ``Footprint.position`` is board-relative after :meth:`PCB._detect_board_origin`,
    so we add the board origin back to produce coordinates in the same frame
    as :pyattr:`ZoneGenerator.board_outline` (which is the frame expected by
    ``add_zone(boundary=)``).

    Pads with ``net_number == 0`` (the implicit "no net" pad) are skipped
    even if they happen to share a name with the requested net.  Footprints
    whose reference designator is empty or begins with ``#`` (KiCad's
    convention for power-symbol stand-ins like ``#PWR01``) are also skipped
    because they have no physical pads on the board.
    """
    ox, oy = pcb.board_origin
    positions: list[tuple[float, float]] = []

    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue

        fp_x, fp_y = fp.position
        # NOTE: positive sign matches PCB.get_pad_position (the canonical
        # local->world transform used throughout the codebase).  See #2778
        # for the previous drift bug where a negative sign was used here.
        rot_rad = math.radians(fp.rotation)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        for pad in fp.pads:
            # ``Pad.net_name`` is authoritative (matches the netlist exactly);
            # ``net_number`` may be zero on freshly-imported PCBs that have
            # not been routed yet, so name comparison is the safe predicate.
            if pad.net_name != net_name:
                continue
            # Skip the implicit no-net pad (net_number 0 with empty name).
            if not pad.net_name:
                continue

            px, py = pad.position
            rx = px * cos_r - py * sin_r
            ry = px * sin_r + py * cos_r
            # Footprint position is board-relative -> add board origin to get
            # sheet-absolute coordinates.
            positions.append((fp_x + rx + ox, fp_y + ry + oy))

    return positions


def _bbox_polygon(
    positions: list[tuple[float, float]],
    margin_mm: float,
) -> list[tuple[float, float]] | None:
    """Build an axis-aligned bounding box polygon around ``positions``.

    Returns ``None`` if ``positions`` is empty.  When all positions
    coincide (e.g. a single-pad net), returns a small square of side
    :data:`SINGLE_PAD_FALLBACK_SIDE_MM` centered on the point so the
    resulting zone still has non-zero area.

    The returned polygon is in the same coordinate frame as ``positions``.
    """
    if not positions:
        return None

    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    # Single-pad (or zero-extent) fallback: emit a small square centered on
    # the pad cluster so the zone is non-degenerate.
    if x_max - x_min < 1e-6 and y_max - y_min < 1e-6:
        half = SINGLE_PAD_FALLBACK_SIDE_MM / 2.0
        cx, cy = x_min, y_min
        return [
            (round(cx - half, 6), round(cy - half, 6)),
            (round(cx + half, 6), round(cy - half, 6)),
            (round(cx + half, 6), round(cy + half, 6)),
            (round(cx - half, 6), round(cy + half, 6)),
        ]

    x_min -= margin_mm
    x_max += margin_mm
    y_min -= margin_mm
    y_max += margin_mm

    return [
        (round(x_min, 6), round(y_min, 6)),
        (round(x_max, 6), round(y_min, 6)),
        (round(x_max, 6), round(y_max, 6)),
        (round(x_min, 6), round(y_max, 6)),
    ]


def _clip_polygon_to_outline(
    polygon: list[tuple[float, float]],
    outline: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Clip ``polygon`` to the interior of ``outline``.

    Both polygons are in the same coordinate frame (sheet-absolute).  Uses
    Shapely if available for an exact intersection; falls back to a pure
    AABB clip when Shapely is missing or the intersection collapses.  When
    everything fails, returns the original polygon unchanged -- KiCad
    happily ignores zone copper that lands outside the board edge, so an
    unclipped polygon still produces correct fills (just with a larger
    bounding region on disk than necessary).
    """
    if not polygon or not outline:
        return polygon

    # Try Shapely first for accurate intersection with arbitrary outlines.
    try:
        from shapely.geometry import Polygon

        clipped = Polygon(polygon).intersection(Polygon(outline))
        if clipped.is_empty:
            return polygon
        if clipped.geom_type == "MultiPolygon":
            clipped = max(clipped.geoms, key=lambda g: g.area)
        if clipped.geom_type != "Polygon":
            return polygon
        coords = list(clipped.exterior.coords)
        if len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) < 3:
            return polygon
        return [(round(x, 6), round(y, 6)) for x, y in coords]
    except ImportError:
        pass

    # Pure-Python fallback: AABB clip (correct for rectangular outlines,
    # conservative for everything else).
    out_xs = [p[0] for p in outline]
    out_ys = [p[1] for p in outline]
    ox_min, ox_max = min(out_xs), max(out_xs)
    oy_min, oy_max = min(out_ys), max(out_ys)

    poly_xs = [p[0] for p in polygon]
    poly_ys = [p[1] for p in polygon]
    px_min = max(min(poly_xs), ox_min)
    px_max = min(max(poly_xs), ox_max)
    py_min = max(min(poly_ys), oy_min)
    py_max = min(max(poly_ys), oy_max)

    if px_min >= px_max or py_min >= py_max:
        return polygon

    return [
        (round(px_min, 6), round(py_min, 6)),
        (round(px_max, 6), round(py_min, 6)),
        (round(px_max, 6), round(py_max, 6)),
        (round(px_min, 6), round(py_max, 6)),
    ]


def _compute_pour_outlines(
    pcb: PCB,
    assignments: list[tuple[str, str, int]],
    board_outline: list[tuple[float, float]],
    margin_mm: float = DEFAULT_POUR_BBOX_MARGIN_MM,
) -> dict[str, list[tuple[float, float]] | None]:
    """Compute per-net pour outlines for the given layer assignments.

    For each ``(net_name, layer, priority)`` in ``assignments``:

    * If this is the only zone on its layer, return ``None`` so the caller
      uses the default ``board_outline``.  This preserves the GND
      return-path plane on 4-layer stackups (where GND sits alone on
      ``In1.Cu``) and on 2-layer stackups (where GND sits alone on
      ``B.Cu``).
    * If two or more zones share a layer, compute a bounding-box outline
      around the net's pads, inflated by ``margin_mm``, then clip to
      ``board_outline`` so the zone never extends past the board edge.
      A second pass (#3043) then subtracts higher-priority bboxes from
      lower-priority outlines so the resulting polygons are
      **geometrically disjoint** even when pad clusters are spatially
      interleaved (as on board 06, where ``+3V3``/``+1V8``/``+1V2`` all
      feed the same BGA region).

    Without the disjoint-carve pass, real boards routinely produce raw
    bboxes that overlap each other.  KiCad's fill resolver would then
    silently award the entire overlap to the highest-priority zone and
    the lower-priority siblings would receive zero copper despite being
    declared in the file.  The carve makes per-zone copper deterministic
    regardless of priority ordering -- each zone receives the region
    delineated by its own pad bbox minus any higher-priority sibling.

    The returned polygons are in the same coordinate frame as
    ``board_outline`` -- sheet-absolute, which is the frame expected by
    :meth:`ZoneGenerator.add_zone`'s ``boundary=`` argument (post-PR-#2753).

    Args:
        pcb: Loaded PCB object (used to look up pad positions).
        assignments: Output of :func:`_assign_layers_for_pour_nets`.
        board_outline: Sheet-absolute board outline polygon.  Used as the
            clipping mask and returned as ``None`` for sole-zone layers
            (the caller falls back to ``board_outline`` via
            :pyattr:`ZoneGenerator.board_outline`).
        margin_mm: Margin around the pad bounding box, in mm.  Defaults to
            :data:`DEFAULT_POUR_BBOX_MARGIN_MM` (1.5 mm).

    Returns:
        Dict mapping ``net_name`` -> polygon (or ``None`` for default).
    """
    # Count zones per layer so we know which assignments share a layer.
    layer_counts: dict[str, int] = {}
    for _, layer, _ in assignments:
        layer_counts[layer] = layer_counts.get(layer, 0) + 1

    outlines: dict[str, list[tuple[float, float]] | None] = {}

    # First pass: compute the raw bbox for every shared-layer zone.
    # ``raw_bboxes[net]`` is the per-net AABB polygon (or ``None`` if the net
    # has no pads -- the fallback case at the bottom of the loop).
    raw_bboxes: dict[str, list[tuple[float, float]] | None] = {}
    for net_name, layer, _ in assignments:
        if layer_counts[layer] < 2:
            # Sole zone on its layer -- keep the full board outline so
            # ground/return planes stay continuous.
            outlines[net_name] = None
            continue

        positions = _net_pad_positions_absolute(pcb, net_name)
        bbox = _bbox_polygon(positions, margin_mm)
        if bbox is None:
            # No pads found for this net.  Fall back to the full board
            # outline -- it will still overlap siblings, but at least the
            # zone is created.  This path is unusual (net classified as
            # power/ground but has no physical pads) and only happens for
            # ERC-marker leakage that the upstream filter missed.
            outlines[net_name] = None
            continue

        raw_bboxes[net_name] = _clip_polygon_to_outline(bbox, board_outline)

    # Second pass (#3043): for each layer that hosts multiple zones, subtract
    # higher-priority zone bboxes from lower-priority zones so the final
    # outlines are *geometrically disjoint*.  Without this, the bboxes can
    # spatially overlap (typical of real boards where power-net pads are
    # interleaved rather than clustered), and KiCad's fill resolver would
    # silently award the entire overlap to the highest-priority zone.
    #
    # KiCad zone-priority convention: HIGHER priority value WINS the overlap
    # region.  So we sort highest-first and subtract each zone's bbox from
    # the lower-priority zones that follow.
    by_layer: dict[str, list[tuple[str, int]]] = {}
    for net_name, layer, priority in assignments:
        if layer_counts[layer] < 2:
            continue
        if net_name not in raw_bboxes:
            continue
        by_layer.setdefault(layer, []).append((net_name, priority))

    for layer, nets_on_layer in by_layer.items():
        # Sort highest priority first -- those zones "win" the overlap.
        nets_on_layer.sort(key=lambda np: np[1], reverse=True)

        # Accumulate the union of higher-priority bboxes; each lower-priority
        # zone has this union subtracted from its own bbox to keep outlines
        # disjoint.
        winners_union = None
        for net_name, _ in nets_on_layer:
            current = raw_bboxes[net_name]
            if winners_union is None:
                # Highest-priority zone keeps its bbox unchanged.
                outlines[net_name] = current
            else:
                outlines[net_name] = _subtract_polygon(
                    current,
                    winners_union,
                    fallback=current,
                )
            # Add this zone's bbox to the winners-union for the next iteration.
            winners_union = _union_polygons(winners_union, current)

    return outlines


def _union_polygons(
    a: list[tuple[float, float]] | None,
    b: list[tuple[float, float]] | None,
):
    """Return the Shapely union of two polygons (or None inputs).

    Returns a Shapely geometry (Polygon or MultiPolygon) so subsequent
    operations can use it directly.  When Shapely is unavailable, returns
    ``None`` -- callers should fall back to leaving outlines unchanged.
    """
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except ImportError:
        return None

    geoms = []
    for poly in (a, b):
        if poly is None:
            continue
        if hasattr(poly, "geom_type"):
            geoms.append(poly)
        else:
            geoms.append(Polygon(poly))
    if not geoms:
        return None
    return unary_union(geoms)


def _subtract_polygon(
    minuend: list[tuple[float, float]],
    subtrahend,
    fallback: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Return ``minuend - subtrahend`` as a polygon.

    ``subtrahend`` may be a list of ``(x, y)`` tuples or a Shapely geometry
    (the latter is what ``_union_polygons`` returns).  KiCad zones do not
    natively support polygon holes in the *outline*; when the subtraction
    produces a polygon with holes, we approximate by returning the largest
    exterior ring (which is still strictly inside ``minuend``).

    When the result is empty or Shapely is missing, returns ``fallback``.
    """
    try:
        from shapely.geometry import Polygon
    except ImportError:
        return fallback

    minuend_poly = Polygon(minuend)
    if hasattr(subtrahend, "geom_type"):
        sub_geom = subtrahend
    else:
        sub_geom = Polygon(subtrahend)

    diff = minuend_poly.difference(sub_geom)

    if diff.is_empty:
        # The minuend was completely covered by higher-priority zones.
        # Return ``fallback`` so the zone still has *some* outline (KiCad's
        # fill resolver will still award zero copper, but the zone exists
        # in the file for tooling that inspects declared geometry).
        return fallback

    if diff.geom_type == "MultiPolygon":
        # Keep only the largest piece -- KiCad zone outlines are single
        # polygons.  Smaller fragments are lost, but the dominant region
        # is preserved.
        diff = max(diff.geoms, key=lambda g: g.area)

    if diff.geom_type != "Polygon":
        return fallback

    coords = list(diff.exterior.coords)
    if len(coords) > 1 and coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) < 3:
        return fallback

    return [(round(x, 6), round(y, 6)) for x, y in coords]


def auto_create_zones_for_pour_nets(
    pcb_path: str | Path,
    pour_nets: list[tuple[str, NetClass]],
    edge_clearance: float | None = None,
) -> int:
    """Create zones for power and ground nets on a PCB.

    Loads the PCB, creates zone definitions for each pour net, and saves
    the modified PCB in place.  Layer assignment is stackup-aware:

    For 2-layer boards:
    - GROUND nets get a zone on B.Cu with priority 1
    - POWER nets get zones on F.Cu with descending priorities
      (first power net gets highest priority) so overlapping zones
      on the same layer coexist without undefined fill order

    For 4-layer boards:
    - GROUND nets get a zone on In1.Cu with priority 1
    - First POWER net gets a zone on In2.Cu with priority 0
    - Additional POWER nets get zones on F.Cu with distinct
      non-zero priorities

    Split-ground designs (multiple distinct GROUND-class nets, e.g.
    ``GNDA`` + ``GNDD``) get special handling so each ground domain
    receives its own copper plane:

    - 4-layer: the two grounds split across ``In1.Cu`` and ``In2.Cu``
      (both priority 1), and POWER moves to ``F.Cu`` since both inner
      layers are now reserved.  Canonical ground (``GND`` if present,
      else alphabetical first) is placed on ``In1.Cu``.
    - 2-layer: each ground gets ``B.Cu`` with a distinct descending
      priority.  True plane separation is impossible on this stackup.
    - >2 grounds: extras spill to ``B.Cu`` with distinct priorities;
      a stderr warning is emitted recommending manual stackup planning.

    See ``_assign_layers_for_pour_nets`` for full assignment rules.

    Geometric outline partition (#2771)
    -----------------------------------

    After the layer/priority allocator runs, the **outline allocator**
    (:func:`_compute_pour_outlines`) decides whether each zone uses the
    full board outline or a per-net bounding region:

    * Zones that are the *only* zone on their layer (e.g. ``GND`` on
      ``B.Cu`` for a single-ground 2-layer board, or ``GND`` on
      ``In1.Cu`` for a 4-layer board) keep the full board outline.  This
      preserves the return-path plane that signals need to cross without
      gaps.
    * Zones that *share* a layer with one or more other zones (e.g. the
      ``+5V`` / ``+3.3V`` / ``PWR_LED`` cluster on F.Cu of a 2-layer
      board) get a bounding box around their own pads (with a 1.5 mm
      default margin), clipped to the board outline.

    Without this geometric partition, distinct priorities (added in
    #2410) are insufficient -- KiCad's fill resolver awards the entire
    shared region to the highest-priority zone, so siblings receive zero
    copper despite the file containing a zone definition for each
    (see #2771 for the board 05 reproduction).

    Args:
        pcb_path: Path to .kicad_pcb file (modified in place)
        pour_nets: List of (net_name, NetClass) tuples identifying
            which nets need zones
        edge_clearance: Optional edge clearance in mm.  When set, the
            auto-derived board outline is inset by this distance so that
            zone copper does not extend to the board edge.  The same
            inset is applied to the per-net bounding outlines so they
            also stay inside the inset board.

    Returns:
        Number of zones created
    """
    pcb_path = Path(pcb_path)
    gen = ZoneGenerator.from_pcb(pcb_path, edge_clearance=edge_clearance)

    copper_layer_count = len(gen.pcb.copper_layers)
    assignments = _assign_layers_for_pour_nets(copper_layer_count, pour_nets)

    # Outline allocator (#2771): compute per-net bounding outlines for any
    # layer that hosts more than one zone.  Sole-layer zones (typically
    # ground) get ``None`` so :meth:`ZoneGenerator.add_zone` falls back to
    # the full ``board_outline`` and keeps the return-path plane continuous.
    pour_outlines = _compute_pour_outlines(gen.pcb, assignments, gen.board_outline)

    count = 0
    for net_name, layer, priority in assignments:
        gen.add_zone(
            net=net_name,
            layer=layer,
            priority=priority,
            boundary=pour_outlines.get(net_name),
        )
        count += 1

    if count > 0:
        gen.save(pcb_path)

    return count
