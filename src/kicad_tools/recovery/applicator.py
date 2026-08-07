"""
Strategy application for placement-routing feedback.

This module provides the StrategyApplicator class which executes resolution
strategies on a PCB, enabling automatic adjustment of placement based on
routing failures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .types import (
    Rectangle,
    ResolutionStrategy,
    StrategyType,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


def _flip_layer_side(name: str) -> str:
    """Swap a KiCad layer name to the other board side (``F.* <-> B.*``).

    Side-neutral names (``*.Cu``, ``*.Mask``, ``In1.Cu``, ``Edge.Cuts``, ...)
    are returned unchanged -- matching what KiCad's own footprint flip does to
    through-hole pad layer lists.
    """
    if name.startswith("F."):
        return "B." + name[2:]
    if name.startswith("B."):
        return "F." + name[2:]
    return name


@dataclass
class ApplicationResult:
    """Result of applying a strategy.

    Attributes:
        success: Whether the strategy was applied successfully.
        components_moved: List of component references that were moved.
        message: Human-readable description of what happened.
        conflicts_created: Number of new conflicts created (if any).
    """

    success: bool
    components_moved: list[str]
    message: str
    conflicts_created: int = 0


class StrategyApplicator:
    """Applies resolution strategies to a PCB.

    This class transforms ResolutionStrategy objects into concrete changes
    on a PCB, moving components and updating placement to resolve routing
    failures.

    Example::

        from kicad_tools.recovery import StrategyGenerator, StrategyApplicator

        # Generate strategies from failure analysis
        generator = StrategyGenerator()
        strategies = generator.generate_strategies(pcb, failure_analysis)

        # Apply the best strategy
        applicator = StrategyApplicator()
        if strategies and applicator.is_safe_to_apply(strategies[0], pcb):
            result = applicator.apply_strategy(pcb, strategies[0])
            if result.success:
                print(f"Moved {len(result.components_moved)} components")
    """

    # Board margin to prevent components from being placed too close to edge
    BOARD_EDGE_MARGIN = 1.0  # mm

    # Maximum move distance to prevent drastic placement changes
    MAX_MOVE_DISTANCE = 10.0  # mm

    def apply_strategy(self, pcb: PCB, strategy: ResolutionStrategy) -> ApplicationResult:
        """Apply a resolution strategy to a PCB.

        Args:
            pcb: The PCB to modify.
            strategy: The strategy to apply.

        Returns:
            ApplicationResult with success status and details.
        """
        if strategy.type == StrategyType.MOVE_COMPONENT:
            return self._apply_move_component(pcb, strategy)
        elif strategy.type == StrategyType.MOVE_MULTIPLE:
            return self._apply_move_multiple(pcb, strategy)
        elif strategy.type == StrategyType.ROTATE_COMPONENT:
            return self._apply_rotate_component(pcb, strategy)
        elif strategy.type == StrategyType.MIRROR_COMPONENT:
            return self._apply_mirror_component(pcb, strategy)
        else:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message=f"Strategy type {strategy.type.value} cannot be applied to placement",
            )

    def _apply_move_component(self, pcb: PCB, strategy: ResolutionStrategy) -> ApplicationResult:
        """Apply a single component move strategy."""
        if not strategy.actions:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message="No actions in strategy",
            )

        action = strategy.actions[0]
        if action.type != "move":
            return ApplicationResult(
                success=False,
                components_moved=[],
                message=f"Expected move action, got {action.type}",
            )

        ref = action.target
        new_x = action.params.get("x")
        new_y = action.params.get("y")

        if new_x is None or new_y is None:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message="Missing x or y in move action params",
            )

        # Find and move the footprint
        fp = self._find_footprint(pcb, ref)
        if fp is None:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message=f"Component {ref} not found",
            )

        # Store old position for reporting
        old_x, old_y = fp.position[0], fp.position[1]

        # Apply the move
        fp.position = (new_x, new_y)

        return ApplicationResult(
            success=True,
            components_moved=[ref],
            message=f"Moved {ref} from ({old_x:.2f}, {old_y:.2f}) to ({new_x:.2f}, {new_y:.2f})",
        )

    def _apply_move_multiple(self, pcb: PCB, strategy: ResolutionStrategy) -> ApplicationResult:
        """Apply a multi-component move strategy."""
        moved: list[str] = []
        messages: list[str] = []

        for action in strategy.actions:
            if action.type != "move":
                continue

            ref = action.target
            new_x = action.params.get("x")
            new_y = action.params.get("y")

            if new_x is None or new_y is None:
                continue

            fp = self._find_footprint(pcb, ref)
            if fp is None:
                messages.append(f"{ref}: not found")
                continue

            old_x, old_y = fp.position[0], fp.position[1]
            fp.position = (new_x, new_y)
            moved.append(ref)
            messages.append(f"{ref}: ({old_x:.2f}, {old_y:.2f}) -> ({new_x:.2f}, {new_y:.2f})")

        if not moved:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message="No components moved",
            )

        return ApplicationResult(
            success=True,
            components_moved=moved,
            message=f"Moved {len(moved)} components: " + "; ".join(messages),
        )

    def _apply_rotate_component(self, pcb: PCB, strategy: ResolutionStrategy) -> ApplicationResult:
        """Apply a single-component in-place rotation strategy (issue #4467).

        Rotates the footprint about its own origin (``fp.position`` is
        unchanged) by ``rotation_delta`` degrees.  This is the geometric
        realization of the classifier's ``DE_REVERSE_BUNDLE`` verdict --
        flipping a reversed facing pad column (``rotation_delta == 180``) so a
        self-crossing bundle stops crossing.

        Both the footprint-level orientation AND every pad's absolute angle are
        updated here.  The board-frame pad *positions* the classifier reads are
        derived from ``fp.position`` + ``fp.rotation`` applied to each pad's
        local offset (see ``stuck_classifier._iter_board_pads``), so bumping
        ``fp.rotation`` is sufficient for that view.  But each pad's ``(at x y
        ANGLE)`` third token is the pad's ABSOLUTE board-frame orientation (see
        ``schema/pcb.py`` ``Pad.rotation`` and the #3902 fix), so it must be
        bumped by the same ``rotation_delta`` or serializing the mutated PCB
        leaves every pad angle stale -- reintroducing the #3902 defect class for
        non-180-symmetric pad shapes (chamfered roundrect, trapezoid, custom).
        The Phase-2 driver separately re-syncs the router's flat pad coordinates
        (which carry no rotation of their own) before re-routing.
        """
        if not strategy.actions:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message="No actions in strategy",
            )

        action = strategy.actions[0]
        if action.type != "rotate":
            return ApplicationResult(
                success=False,
                components_moved=[],
                message=f"Expected rotate action, got {action.type}",
            )

        ref = action.target
        rotation_delta = action.params.get("rotation_delta")
        if rotation_delta is None:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message="Missing rotation_delta in rotate action params",
            )

        fp = self._find_footprint(pcb, ref)
        if fp is None:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message=f"Component {ref} not found",
            )

        old_rotation = float(getattr(fp, "rotation", 0.0))
        new_rotation = (old_rotation + float(rotation_delta)) % 360.0
        fp.rotation = new_rotation

        # A pad's ``(at x y ANGLE)`` third token is the pad's ABSOLUTE board-frame
        # orientation and already includes the parent footprint's rotation, so it
        # must be advanced by the same delta.  ``Footprint.__setattr__`` for
        # ``rotation`` only rewrites the footprint's own ``(at ...)`` token and is
        # unaware of pads (schema/pcb.py), so without this loop the pad angles go
        # stale on serialization (the #3902 defect class).  Guard with getattr/
        # hasattr since test doubles (MockFootprint) may carry pads without a
        # ``.rotation`` attribute.
        for pad in getattr(fp, "pads", []):
            if hasattr(pad, "rotation"):
                pad.rotation = (float(pad.rotation) + float(rotation_delta)) % 360.0

        return ApplicationResult(
            success=True,
            components_moved=[ref],
            message=(
                f"Rotated {ref} by {float(rotation_delta):.1f} deg "
                f"({old_rotation:.1f} -> {new_rotation:.1f})"
            ),
        )

    def _apply_mirror_component(self, pcb: PCB, strategy: ResolutionStrategy) -> ApplicationResult:
        """Apply a single-component mirror (layer flip) strategy (issue #4560).

        KiCad-semantics **left/right** flip of one footprint about its own
        anchor (``fp.position`` is unchanged), i.e. exactly what
        ``FOOTPRINT::Flip(anchor, FLIP_DIRECTION_LEFT_RIGHT)`` writes to the
        board file.  This is the geometric realization of the classifier's
        ``DE_REVERSE_BUNDLE`` verdict that a 180-degree rotation structurally
        cannot deliver: rotation preserves the pin column's chirality, so it
        de-reverses the pad ORDER only by relocating the whole column to the
        far side of the package (measured on board-07 CI: routed 25 -> 14).  A
        mirror un-reverses the column in place -- at the cost of moving the
        part to the other board side.

        The file-level transform below is pinned EMPIRICALLY against KiCad
        10.0.5 (``tests/fixtures/mirror_flip/``, generated by KiCad's own
        ``FOOTPRINT.Flip``; asserted by ``tests/test_mirror_golden.py``):

        * footprint ``layer``: ``F.* <-> B.*`` side swap;
        * footprint ``rotation``: ``theta -> (180 - theta)`` (left/right flip
          reflects orientation about the vertical axis; the top/bottom flip's
          ``-theta`` is NOT this transform);
        * pad ``position`` (footprint-LOCAL file coords): ``(x, y) -> (x, -y)``
          -- constant in ``theta`` because the stored local frame co-rotates;
        * pad ``rotation`` (ABSOLUTE, includes footprint rotation -- #3902):
          ``a -> (180 - a)``;
        * pad ``layers``: per-name side swap (through-hole ``*.Cu``/``*.Mask``
          unchanged);
        * footprint texts/graphics: local ``y`` negation + side swap +
          ``(justify mirror)`` toggle (correct side/geometry; silk prettiness
          beyond that is out of scope, #4560).

        The pad mutations go through ``Pad.__setattr__`` write-through
        (``position``/``layers`` sync added by #4560), so the flip reaches the
        S-expression tree ``PCB.save`` serialises -- no half-flipped in-memory
        state.  Attribute access is guarded like ``_apply_rotate_component`` so
        test doubles without the full schema surface still work.
        """
        if not strategy.actions:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message="No actions in strategy",
            )

        action = strategy.actions[0]
        if action.type != "mirror":
            return ApplicationResult(
                success=False,
                components_moved=[],
                message=f"Expected mirror action, got {action.type}",
            )

        ref = action.target
        fp = self._find_footprint(pcb, ref)
        if fp is None:
            return ApplicationResult(
                success=False,
                components_moved=[],
                message=f"Component {ref} not found",
            )

        old_layer = str(getattr(fp, "layer", "F.Cu"))
        if hasattr(fp, "layer"):
            fp.layer = _flip_layer_side(old_layer)

        old_rotation = float(getattr(fp, "rotation", 0.0))
        if hasattr(fp, "rotation"):
            fp.rotation = (180.0 - old_rotation) % 360.0

        for pad in getattr(fp, "pads", []):
            if hasattr(pad, "position"):
                px, py = pad.position
                pad.position = (px, -py)
            if hasattr(pad, "rotation"):
                pad.rotation = (180.0 - float(pad.rotation)) % 360.0
            if hasattr(pad, "layers"):
                pad.layers = [_flip_layer_side(layer) for layer in pad.layers]

        self._mirror_footprint_cosmetics(fp)

        new_layer = str(getattr(fp, "layer", old_layer))
        return ApplicationResult(
            success=True,
            components_moved=[ref],
            message=f"Mirrored {ref} (layer {old_layer} -> {new_layer}, left/right flip)",
        )

    # Footprint-level cosmetic children a left/right flip must move to the
    # other side and y-mirror ("pad" is deliberately absent -- pads flip via
    # the attribute write-through above, never twice).
    _COSMETIC_SEXP_TAGS = frozenset(
        {
            "property",
            "fp_text",
            "fp_text_box",
            "fp_line",
            "fp_rect",
            "fp_circle",
            "fp_arc",
            "fp_poly",
            "fp_curve",
        }
    )

    def _mirror_footprint_cosmetics(self, fp) -> None:
        """Flip footprint texts/graphics to the other side (issue #4560).

        Two parallel representations must stay consistent:

        * the parsed Python objects (``fp.texts`` / ``fp.graphics``), which the
          silkscreen/courtyard validators read, and
        * the raw S-expression children of ``fp._sexp_node`` (texts/graphics
          carry no per-object node back-reference), which ``PCB.save`` writes.

        Both derive from the same pre-flip state and receive the identical
        transform: local ``y`` negation, absolute angle ``a -> 180 - a``,
        ``F.* <-> B.*`` side swap, and a ``(justify mirror)`` toggle on text
        effects (what KiCad itself emits).  No-ops gracefully on test doubles
        without ``texts``/``graphics``/``_sexp_node``.
        """
        for text in getattr(fp, "texts", []):
            if hasattr(text, "position"):
                tx, ty = text.position
                text.position = (tx, -ty)
            if hasattr(text, "layer"):
                text.layer = _flip_layer_side(text.layer)
        for graphic in getattr(fp, "graphics", []):
            if hasattr(graphic, "start"):
                sx, sy = graphic.start
                graphic.start = (sx, -sy)
            if hasattr(graphic, "end"):
                ex, ey = graphic.end
                graphic.end = (ex, -ey)
            if getattr(graphic, "center", None) is not None:
                cx, cy = graphic.center
                graphic.center = (cx, -cy)
            if getattr(graphic, "points", None):
                graphic.points = [(px, -py) for (px, py) in graphic.points]
            if hasattr(graphic, "layer"):
                graphic.layer = _flip_layer_side(graphic.layer)

        sexp_node = getattr(fp, "_sexp_node", None)
        if sexp_node is None:
            return
        for child in sexp_node.iter_children():
            if child.tag not in self._COSMETIC_SEXP_TAGS:
                continue
            self._mirror_cosmetic_node(child)

    @staticmethod
    def _mirror_cosmetic_node(node) -> None:
        """Y-mirror + side-swap one footprint text/graphic S-expression node."""
        from kicad_tools.sexp import SExp

        at_node = node.find_child("at")
        if at_node is not None:
            y = at_node.get_float(1)
            if y is not None:
                at_node.set_value(1, -y)
            angle = at_node.get_float(2)
            if angle is not None:
                at_node.set_value(2, (180.0 - angle) % 360.0)
            elif node.tag in ("property", "fp_text", "fp_text_box"):
                # Absent angle token == 0 deg; the flipped text is at 180.
                at_node.add(180.0)

        for tag in ("start", "end", "center", "mid"):
            geo = node.find_child(tag)
            if geo is not None:
                y = geo.get_float(1)
                if y is not None:
                    geo.set_value(1, -y)

        pts = node.find_child("pts")
        if pts is not None:
            for xy in pts.find_children("xy"):
                y = xy.get_float(1)
                if y is not None:
                    xy.set_value(1, -y)

        layer_node = node.find_child("layer")
        if layer_node is not None:
            name = layer_node.get_string(0)
            if name:
                layer_node.children[0] = SExp.quoted_atom(_flip_layer_side(name))

        # Text mirror flag: KiCad toggles ``(justify mirror)`` on flipped text.
        if node.tag in ("property", "fp_text", "fp_text_box"):
            effects = node.find_child("effects")
            if effects is not None:
                justify = effects.find_child("justify")
                if justify is None:
                    effects.append(SExp.list("justify", "mirror"))
                elif "mirror" in justify.values:
                    justify.children = [
                        c for c in justify.children if not (c.is_atom and c.value == "mirror")
                    ]
                else:
                    justify.add("mirror")

    def is_safe_to_apply(self, strategy: ResolutionStrategy, pcb: PCB) -> bool:
        """Check if applying a strategy is safe.

        Verifies that:
        1. All target components exist
        2. New positions are within board bounds
        3. Move distances are reasonable
        4. Functional groups won't be broken (basic check)

        Args:
            strategy: The strategy to check.
            pcb: The PCB to check against.

        Returns:
            True if the strategy is safe to apply.
        """
        # ROTATE_COMPONENT and MIRROR_COMPONENT keep the footprint centre fixed
        # (rotation/flip about the anchor), so board-bounds / move-distance
        # checks do not apply -- only the target's existence matters (issues
        # #4467, #4560).
        if strategy.type in (StrategyType.ROTATE_COMPONENT, StrategyType.MIRROR_COMPONENT):
            expected_action = (
                "rotate" if strategy.type == StrategyType.ROTATE_COMPONENT else "mirror"
            )
            for action in strategy.actions:
                if action.type != expected_action:
                    continue
                if self._find_footprint(pcb, action.target) is None:
                    return False
            return True

        if strategy.type not in [StrategyType.MOVE_COMPONENT, StrategyType.MOVE_MULTIPLE]:
            return False

        board_bounds = self._get_board_bounds(pcb)
        if board_bounds is None:
            # If we can't determine board bounds, be conservative
            return False

        for action in strategy.actions:
            if action.type != "move":
                continue

            ref = action.target
            new_x = action.params.get("x")
            new_y = action.params.get("y")

            if new_x is None or new_y is None:
                return False

            # Check component exists
            fp = self._find_footprint(pcb, ref)
            if fp is None:
                return False

            # Check new position is within board bounds (with margin)
            if not self._position_within_bounds(new_x, new_y, board_bounds):
                return False

            # Check move distance is reasonable
            old_x, old_y = fp.position[0], fp.position[1]
            distance = math.sqrt((new_x - old_x) ** 2 + (new_y - old_y) ** 2)
            if distance > self.MAX_MOVE_DISTANCE:
                return False

        return True

    def calculate_move_vector(
        self,
        pcb: PCB,
        ref: str,
        failure_area: Rectangle,
        direction: tuple[float, float] | None = None,
    ) -> tuple[float, float] | None:
        """Calculate the move vector for a single component.

        Determines how far to move a component to clear a failure area.

        Args:
            pcb: The PCB containing the component.
            ref: Component reference designator.
            failure_area: The area to clear.
            direction: Optional preferred move direction (dx, dy). If not
                provided, direction is calculated from failure area center.

        Returns:
            Move vector (dx, dy) in mm, or None if cannot calculate.
        """
        fp = self._find_footprint(pcb, ref)
        if fp is None:
            return None

        comp_x, comp_y = fp.position[0], fp.position[1]
        center_x, center_y = failure_area.center

        # Calculate direction if not provided
        if direction is None:
            dx = comp_x - center_x
            dy = comp_y - center_y
            dist = math.sqrt(dx * dx + dy * dy)

            if dist < 0.1:
                # Component at center, move in arbitrary direction
                dx, dy = 1.0, 0.0
            else:
                dx /= dist
                dy /= dist
        else:
            dx, dy = direction
            dist = math.sqrt(dx * dx + dy * dy)
            if dist > 0:
                dx /= dist
                dy /= dist

        # Calculate move distance to clear failure area
        # Use the larger dimension of failure area plus margin
        clear_dist = max(failure_area.width, failure_area.height) / 2 + 0.5

        return (dx * clear_dist, dy * clear_dist)

    def calculate_spread_vector(
        self,
        pcb: PCB,
        ref: str,
        center: tuple[float, float],
        spread_distance: float = 2.0,
    ) -> tuple[float, float] | None:
        """Calculate the spread vector for multi-component spreading.

        Calculates a vector to move a component away from a center point,
        used for relieving congestion.

        Args:
            pcb: The PCB containing the component.
            ref: Component reference designator.
            center: Center point to spread away from (x, y).
            spread_distance: How far to spread in mm.

        Returns:
            Spread vector (dx, dy) in mm, or None if cannot calculate.
        """
        fp = self._find_footprint(pcb, ref)
        if fp is None:
            return None

        comp_x, comp_y = fp.position[0], fp.position[1]
        center_x, center_y = center

        dx = comp_x - center_x
        dy = comp_y - center_y
        dist = math.sqrt(dx * dx + dy * dy)

        if dist < 0.1:
            # Component at center, spread in arbitrary direction based on ref
            # Use hash of ref for consistent but varied direction
            angle = (hash(ref) % 360) * math.pi / 180
            dx = math.cos(angle)
            dy = math.sin(angle)
        else:
            dx /= dist
            dy /= dist

        return (dx * spread_distance, dy * spread_distance)

    def simulate_placement_change(
        self,
        pcb: PCB,
        strategy: ResolutionStrategy,
    ) -> dict[str, tuple[float, float]]:
        """Simulate a placement change without applying it.

        Returns what the new positions would be if the strategy were applied.

        Args:
            pcb: The PCB (not modified).
            strategy: The strategy to simulate.

        Returns:
            Dictionary mapping component ref to new (x, y) position.
        """
        positions: dict[str, tuple[float, float]] = {}

        for action in strategy.actions:
            if action.type != "move":
                continue

            ref = action.target
            new_x = action.params.get("x")
            new_y = action.params.get("y")

            if new_x is not None and new_y is not None:
                positions[ref] = (new_x, new_y)

        return positions

    def _find_footprint(self, pcb: PCB, ref: str):
        """Find a footprint by reference designator."""
        for fp in pcb.footprints:
            if fp.reference == ref:
                return fp
        return None

    def _get_board_bounds(self, pcb: PCB) -> Rectangle | None:
        """Get the board outline bounds, in the SAME frame as ``fp.position``.

        Frame correctness is load-bearing (issue #4468, the #3861 defect class):
        :class:`~kicad_tools.schema.pcb.PCB` resolves footprint positions into
        **board-relative** coordinates (relative to ``pcb.board_origin``) but
        leaves board graphics in **sheet-absolute** coordinates.  Comparing a
        board-relative ``fp.position`` against sheet-absolute Edge.Cuts bounds
        makes every footprint on any board with a non-zero origin read as
        "outside the board", so :meth:`is_safe_to_apply` rejected EVERY
        translate strategy with "unsafe (board bounds)".  Measured on board-07
        (origin ``(93.5, 40.0)``): outline bounds ``x:[93.5, 203.5]`` vs
        footprints at ``x:[15, 85]`` -- 8/8 footprints falsely out of bounds,
        which silently disabled the whole MOVE_COMPONENT recovery path there.

        The Edge.Cuts coordinates are therefore shifted by ``-board_origin``.
        The no-outline fallback below already derives its bounds from
        ``fp.position`` and so is already in the right frame.
        """
        # Try to find board outline from edge cuts
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")

        origin_x, origin_y = getattr(pcb, "board_origin", (0.0, 0.0))

        found = False
        for item in pcb.graphic_items:
            # Check if this is an edge cut
            layer = getattr(item, "layer", None)
            if layer is not None and "Edge" in str(layer):
                found = True
                # Get coordinates from the item
                if hasattr(item, "start") and hasattr(item, "end"):
                    min_x = min(min_x, item.start[0] - origin_x, item.end[0] - origin_x)
                    min_y = min(min_y, item.start[1] - origin_y, item.end[1] - origin_y)
                    max_x = max(max_x, item.start[0] - origin_x, item.end[0] - origin_x)
                    max_y = max(max_y, item.start[1] - origin_y, item.end[1] - origin_y)
                elif hasattr(item, "center") and hasattr(item, "radius"):
                    min_x = min(min_x, item.center[0] - origin_x - item.radius)
                    min_y = min(min_y, item.center[1] - origin_y - item.radius)
                    max_x = max(max_x, item.center[0] - origin_x + item.radius)
                    max_y = max(max_y, item.center[1] - origin_y + item.radius)

        if not found:
            # Fall back to component bounds with margin
            for fp in pcb.footprints:
                x, y = fp.position[0], fp.position[1]
                min_x = min(min_x, x - 10)
                min_y = min(min_y, y - 10)
                max_x = max(max_x, x + 10)
                max_y = max(max_y, y + 10)

            if min_x == float("inf"):
                return None

        return Rectangle(min_x, min_y, max_x, max_y)

    def _position_within_bounds(self, x: float, y: float, bounds: Rectangle) -> bool:
        """Check if a position is within board bounds (with margin)."""
        return (
            bounds.min_x + self.BOARD_EDGE_MARGIN <= x <= bounds.max_x - self.BOARD_EDGE_MARGIN
            and bounds.min_y + self.BOARD_EDGE_MARGIN <= y <= bounds.max_y - self.BOARD_EDGE_MARGIN
        )
