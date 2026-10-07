"""Panel builder -- core panelization engine.

Implements the :class:`Panel` builder class that orchestrates board
placement, tab generation, separation feature rendering, and panel
furniture placement.  Operates entirely on S-expression trees, with
no dependency on ``pcbnew``.

Usage::

    panel = Panel()
    panel.append_board("board.kicad_pcb", rows=2, cols=2)
    panel.make_tabs(width=3.0, count=3)
    panel.make_mousebites(diameter=0.5, spacing=0.8)
    panel.save("panel.kicad_pcb")
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kicad_tools.core.sexp_file import load_pcb, save_pcb
from kicad_tools.pcb.board_geometry import BoardGeometry, has_shapely
from kicad_tools.schema.pcb import FOOTPRINT_TAGS, _is_footprint_tag
from kicad_tools.sexp.builders import gr_line_node, keepout_node
from kicad_tools.sexp.parser import SExp

from .config import (
    CutMethod,
    FiducialConfig,
    FrameConfig,
    MousebiteConfig,
    PanelConfig,
    TabConfig,
    ToolingHoleConfig,
    VCutConfig,
)
from .cuts import (
    generate_mousebite_holes,
    generate_vcut_lines,
    mousebite_hole_to_sexp,
    vcut_line_to_sexp,
)
from .furniture import (
    compute_fiducials,
    compute_tooling_holes,
    fiducial_to_sexp,
    tooling_hole_to_sexp,
)
from .tabs import BUTT_TOLERANCE_MM, Tab, compute_tabs_between_boards, compute_tabs_to_frame
from .vscore import (
    EdgeCopper,
    VScoreClearanceFinding,
    edge_copper_clearances,
    source_side_for,
)

logger = logging.getLogger(__name__)


def _require_shapely() -> None:
    if not has_shapely():
        raise ImportError(
            "Shapely is required for panelization.  "
            "Install it with:  pip install kicad-tools[geometry]"
        )


@dataclass
class BoardInstance:
    """A single copy of a board placed in the panel.

    Attributes:
        index: Board index (0-based) within the panel.
        row: Grid row (0-based).
        col: Grid column (0-based).
        offset_x: X offset in panel coordinates (mm).
        offset_y: Y offset in panel coordinates (mm).
        rotation: Rotation in degrees.
        bounds: (min_x, min_y, max_x, max_y) in panel coordinates.
    """

    index: int
    row: int
    col: int
    offset_x: float
    offset_y: float
    rotation: float
    bounds: tuple[float, float, float, float]


class Panel:
    """Builder-pattern API for creating manufacturing panels.

    Wraps a source ``.kicad_pcb`` and provides methods for grid
    placement, tab generation, separation feature rendering, and panel
    furniture.

    Typical usage::

        panel = Panel()
        panel.append_board("board.kicad_pcb", rows=2, cols=2, spacing=2.0)
        panel.make_tabs(width=3.0, count=3)
        panel.make_mousebites(diameter=0.5, spacing=0.8)
        panel.save("panel.kicad_pcb")

    Or with a full config::

        cfg = PanelConfig(rows=3, cols=3, spacing=2.0)
        panel = Panel.from_config("board.kicad_pcb", cfg)
        panel.save("panel.kicad_pcb")
    """

    def __init__(self) -> None:
        _require_shapely()

        self._source_sexp: SExp | None = None
        self._source_path: Path | None = None
        # Source board bounds in sheet-absolute coordinates (the frame of
        # the raw S-expression nodes that get cloned).
        self._board_bounds: tuple[float, float, float, float] = (0, 0, 0, 0)
        self._instances: list[BoardInstance] = []
        self._tabs: list[Tab] = []
        self._panel_sexp: SExp | None = None
        self._net_map: dict[int, int] = {}  # source net -> panel net
        self._next_net: int = 1
        self._panel_bounds: tuple[float, float, float, float] = (0, 0, 0, 0)
        self._frame_config: FrameConfig | None = None
        self._mousebite_config: MousebiteConfig | None = None
        self._vcut_config: VCutConfig | None = None
        self._tooling_config: ToolingHoleConfig | None = None
        self._fiducial_config: FiducialConfig | None = None
        self._source_pcb: Any = None
        self._source_rel_bounds: tuple[float, float, float, float] = (0, 0, 0, 0)
        self._edge_copper: dict[str, EdgeCopper] | None = None
        self._warnings: list[str] = []
        self._vscore_findings: list[VScoreClearanceFinding] = []

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, board_path: str | Path, config: PanelConfig) -> Panel:
        """Build a complete panel from a config in one call.

        V-cut panels are butted by default: copies sit edge to edge (and
        against the rails when there is a frame), score lines go on the
        shared edges and there are no tabs (Issue #6164).  A seam given a
        gap -- ``spacing``, ``spacing_x``/``spacing_y`` or ``frame.space``
        -- cannot be V-scored, so it is tab-routed with mousebites
        instead and :attr:`warnings` says so.  Different gaps per axis
        make a mixed panel: one axis scored, the other tabbed.

        Args:
            board_path: Path to the source ``.kicad_pcb`` file.
            config: Full panel configuration.

        Returns:
            A fully configured Panel ready for ``save()``.
        """
        panel = cls()
        gap_x, gap_y = config.resolved_spacing()
        panel.append_board(
            board_path,
            rows=config.rows,
            cols=config.cols,
            rotation=config.rotation,
            spacing_x=gap_x,
            spacing_y=gap_y,
        )

        if config.frame is not None:
            panel.make_frame(
                width=config.frame.width,
                space=config.frame.resolved_space(config.cut_method),
            )

        panel.make_tabs(
            width=config.tabs.width,
            count=config.tabs.count,
            spacing=config.tabs.spacing,
        )

        if config.cut_method == CutMethod.MOUSEBITE:
            panel.make_mousebites(
                diameter=config.mousebite.diameter,
                spacing=config.mousebite.spacing,
                offset=config.mousebite.offset,
            )
        elif config.cut_method == CutMethod.VCUT:
            panel.make_vcuts(
                line_width=config.vcut.line_width,
                layer=config.vcut.layer,
                clearance=config.vcut.clearance,
            )
            if panel.tabs:
                # Gapped seams (mixed panel or a spacing override) are
                # tab-routed; perforate their tabs so they still break.
                panel.make_mousebites(
                    diameter=config.mousebite.diameter,
                    spacing=config.mousebite.spacing,
                    offset=config.mousebite.offset,
                )

        if config.tooling_holes is not None:
            panel.make_tooling_holes(
                diameter=config.tooling_holes.diameter,
                offset=config.tooling_holes.offset,
                pattern=config.tooling_holes.pattern,
            )

        if config.fiducials is not None:
            panel.make_fiducials(
                diameter=config.fiducials.diameter,
                mask_margin=config.fiducials.mask_margin,
                offset=config.fiducials.offset,
            )

        return panel

    # ------------------------------------------------------------------
    # Board placement
    # ------------------------------------------------------------------

    def append_board(
        self,
        board_path: str | Path,
        rows: int = 1,
        cols: int = 1,
        spacing: float = 2.0,
        rotation: float = 0.0,
        spacing_x: float | None = None,
        spacing_y: float | None = None,
    ) -> Panel:
        """Load a board and place it in a grid layout.

        Each board copy is offset so that there is *spacing* mm between
        adjacent board edges.  Net names are prefixed with the board
        index to prevent conflicts across copies.

        Args:
            board_path: Path to the source ``.kicad_pcb``.
            rows: Number of rows in the grid.
            cols: Number of columns in the grid.
            spacing: Gap between board edges in mm.  0 butts the copies
                edge to edge, as a V-scored panel needs.
            rotation: Per-board rotation in degrees, a multiple of 90
                (KiCad's CCW-positive convention).  Each copy is rotated
                rigidly about its own bounding box, then translated into
                its grid cell.
            spacing_x: Gap between columns in mm, overriding *spacing*.
            spacing_y: Gap between rows in mm, overriding *spacing*.

        Returns:
            ``self`` for method chaining.

        Raises:
            ValueError: If *rotation* is not a multiple of 90 degrees --
                the per-board Edge.Cuts outline, tabs and V-cuts all
                assume an axis-aligned board rectangle.
        """
        rotation = _normalize_rotation(rotation)
        gap_x = spacing if spacing_x is None else spacing_x
        gap_y = spacing if spacing_y is None else spacing_y
        if gap_x < 0 or gap_y < 0:
            raise ValueError(f"Panel spacing must be >= 0 mm, got ({gap_x}, {gap_y})")
        board_path = Path(board_path)
        self._source_sexp = load_pcb(board_path)
        self._source_path = board_path

        # Extract board bounds using BoardGeometry
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(board_path)
        geom = BoardGeometry.from_pcb(pcb)
        # ``BoardGeometry`` bounds are board-relative (the board origin is
        # subtracted), but the cloned S-expression nodes are sheet-absolute.
        # Mixing the two left every copy's content displaced from its own
        # Edge.Cuts rectangle by the board origin (Issue #6125).
        ox, oy = pcb.board_origin
        rel_min_x, rel_min_y, rel_max_x, rel_max_y = geom.bounds
        min_x, min_y = rel_min_x + ox, rel_min_y + oy
        max_x, max_y = rel_max_x + ox, rel_max_y + oy
        board_w = max_x - min_x
        board_h = max_y - min_y
        self._board_bounds = (min_x, min_y, max_x, max_y)
        self._source_pcb = pcb
        self._source_rel_bounds = (rel_min_x, rel_min_y, rel_max_x, rel_max_y)
        self._edge_copper = None
        if rotation in (90.0, 270.0):
            # A quarter turn swaps the footprint the copy occupies.
            board_w, board_h = board_h, board_w

        # Place board copies in grid
        self._instances.clear()
        for row in range(rows):
            for col in range(cols):
                idx = row * cols + col
                offset_x = col * (board_w + gap_x)
                offset_y = row * (board_h + gap_y)

                inst_bounds = (
                    offset_x,
                    offset_y,
                    offset_x + board_w,
                    offset_y + board_h,
                )
                self._instances.append(
                    BoardInstance(
                        index=idx,
                        row=row,
                        col=col,
                        offset_x=offset_x,
                        offset_y=offset_y,
                        rotation=rotation,
                        bounds=inst_bounds,
                    )
                )

        # Compute panel bounds (without frame)
        if self._instances:
            all_min_x = min(i.bounds[0] for i in self._instances)
            all_min_y = min(i.bounds[1] for i in self._instances)
            all_max_x = max(i.bounds[2] for i in self._instances)
            all_max_y = max(i.bounds[3] for i in self._instances)
            self._panel_bounds = (all_min_x, all_min_y, all_max_x, all_max_y)

        return self

    # ------------------------------------------------------------------
    # Frame
    # ------------------------------------------------------------------

    def make_frame(
        self,
        width: float = 5.0,
        space: float = 2.0,
    ) -> Panel:
        """Add a frame (rail) around the panel.

        Args:
            width: Frame rail width in mm.
            space: Gap between board edge and inner frame edge in mm.

        Returns:
            ``self`` for method chaining.
        """
        self._frame_config = FrameConfig(width=width, space=space)

        # Expand panel bounds to include frame
        px0, py0, px1, py1 = self._panel_bounds
        self._panel_bounds = (
            px0 - space - width,
            py0 - space - width,
            px1 + space + width,
            py1 + space + width,
        )

        return self

    # ------------------------------------------------------------------
    # Tab generation
    # ------------------------------------------------------------------

    def make_tabs(
        self,
        width: float = 3.0,
        count: int = 3,
        spacing: float | None = None,
    ) -> Panel:
        """Generate breakaway tabs between boards and to frame.

        Args:
            width: Tab width in mm.
            count: Number of tabs per edge.
            spacing: If set, compute tab count from spacing instead.

        Returns:
            ``self`` for method chaining.
        """
        config = TabConfig(width=width, count=count, spacing=spacing)
        self._tabs.clear()

        # Tabs between horizontally adjacent boards
        rows_set: dict[int, list[BoardInstance]] = {}
        for inst in self._instances:
            rows_set.setdefault(inst.row, []).append(inst)

        for row_insts in rows_set.values():
            row_insts.sort(key=lambda i: i.col)
            for j in range(len(row_insts) - 1):
                a = row_insts[j]
                b = row_insts[j + 1]
                tabs = compute_tabs_between_boards(a.bounds, b.bounds, config, "horizontal")
                self._tabs.extend(tabs)

        # Tabs between vertically adjacent boards
        cols_set: dict[int, list[BoardInstance]] = {}
        for inst in self._instances:
            cols_set.setdefault(inst.col, []).append(inst)

        for col_insts in cols_set.values():
            col_insts.sort(key=lambda i: i.row)
            for j in range(len(col_insts) - 1):
                a = col_insts[j]
                b = col_insts[j + 1]
                tabs = compute_tabs_between_boards(a.bounds, b.bounds, config, "vertical")
                self._tabs.extend(tabs)

        # Tabs to frame (if frame is configured)
        if self._frame_config is not None:
            frame_inner = self._get_frame_inner_bounds()
            for inst in self._instances:
                tabs = compute_tabs_to_frame(inst.bounds, frame_inner, config)
                # Only edges that face the rail get a rail tab.  An
                # interior edge's "tab to frame" would run straight through
                # the neighbouring board copies (Issue #6143).
                others = [o.bounds for o in self._instances if o is not inst]
                self._tabs.extend(t for t in tabs if not _tab_crosses_any(t, others))

        return self

    def _get_frame_inner_bounds(self) -> tuple[float, float, float, float]:
        """Get the inner edge of the frame."""
        if self._frame_config is None:
            return self._panel_bounds

        px0, py0, px1, py1 = self._panel_bounds
        w = self._frame_config.width
        return (px0 + w, py0 + w, px1 - w, py1 - w)

    # ------------------------------------------------------------------
    # Separation features
    # ------------------------------------------------------------------

    def make_mousebites(
        self,
        diameter: float = 0.5,
        spacing: float = 0.8,
        offset: float = 0.0,
    ) -> Panel:
        """Generate mousebite perforations along all tabs.

        Args:
            diameter: NPTH hole diameter in mm.
            spacing: Hole center-to-center spacing in mm.
            offset: Inward offset from tab edges in mm.

        Returns:
            ``self`` for method chaining.
        """
        self._mousebite_config = MousebiteConfig(diameter=diameter, spacing=spacing, offset=offset)
        return self

    def make_vcuts(
        self,
        line_width: float = 0.1,
        layer: str = VCutConfig.layer,
        clearance: float = VCutConfig.clearance,
    ) -> Panel:
        """Generate V-cut score lines on the panel's butted seams.

        V-cuts are straight lines that span the entire panel width
        or height.  They are drawn on a documentation layer
        (``Cmts.User`` by default), not Edge.Cuts: an open score line on
        Edge.Cuts is a malformed outline (Issue #6143).

        A score line goes exactly on each edge two solids share: two
        butted copies, or a copy and a butted rail.  A seam with a gap is
        a routed slot; scoring across it would cut mostly air, so it gets
        no score line (Issue #6164) -- bridge it with :meth:`make_tabs`
        and :meth:`make_mousebites`.

        Args:
            line_width: Score line width in mm.
            layer: Layer the score lines are drawn on.
            clearance: Copper clearance to each score line in mm.  Board
                copper closer than this is reported in :attr:`warnings`,
                and a copper-pour keepout this wide is added each side of
                every score line.  0 disables both.

        Returns:
            ``self`` for method chaining.
        """
        if clearance < 0:
            raise ValueError(f"V-score clearance must be >= 0 mm, got {clearance}")
        self._vcut_config = VCutConfig(line_width=line_width, layer=layer, clearance=clearance)
        return self

    # ------------------------------------------------------------------
    # Furniture
    # ------------------------------------------------------------------

    def make_tooling_holes(
        self,
        diameter: float = 3.0,
        offset: float = 3.5,
        pattern: int = 3,
    ) -> Panel:
        """Add tooling holes to the panel frame.

        Args:
            diameter: Hole diameter in mm.
            offset: Distance from panel corner in mm.
            pattern: 3 or 4 holes.

        Returns:
            ``self`` for method chaining.
        """
        self._tooling_config = ToolingHoleConfig(diameter=diameter, offset=offset, pattern=pattern)
        return self

    def make_fiducials(
        self,
        diameter: float = 1.0,
        mask_margin: float = 2.0,
        offset: float = 5.0,
    ) -> Panel:
        """Add fiducial marks to the panel frame.

        Args:
            diameter: Copper pad diameter in mm.
            mask_margin: Solder mask margin in mm.
            offset: Distance from panel corner in mm.

        Returns:
            ``self`` for method chaining.
        """
        self._fiducial_config = FiducialConfig(
            diameter=diameter, mask_margin=mask_margin, offset=offset
        )
        return self

    # ------------------------------------------------------------------
    # Save / build
    # ------------------------------------------------------------------

    def save(self, output_path: str | Path) -> Path:
        """Build the panel S-expression tree and write to disk.

        This is the terminal method that assembles all configured
        elements into a valid ``.kicad_pcb`` file.

        Args:
            output_path: Path for the output panel PCB file.

        Returns:
            The resolved output path.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sexp = self.build()
        save_pcb(sexp, output_path)
        logger.info("Panel saved to %s", output_path)
        return output_path

    def build(self) -> SExp:
        """Assemble the panel S-expression tree.

        Returns:
            A complete ``kicad_pcb`` S-expression ready for
            serialization.
        """
        if self._source_sexp is None:
            raise ValueError("No board loaded. Call append_board() before build().")

        self._warnings = []
        self._vscore_findings = []

        # Start with a skeleton PCB based on the source
        panel_sexp = self._build_skeleton()

        # Collect nets across all board instances
        self._build_nets(panel_sexp)

        # Place board copies
        for inst in self._instances:
            self._place_board_copy(panel_sexp, inst)

        # Add mousebite holes
        if self._mousebite_config is not None:
            for tab in self._tabs:
                holes = generate_mousebite_holes(tab, self._mousebite_config)
                for hole in holes:
                    panel_sexp.append(mousebite_hole_to_sexp(hole))

        # Add V-cut lines
        if self._vcut_config is not None:
            self._add_vcuts(panel_sexp, self._vcut_config)

        # Add tooling holes
        if self._tooling_config is not None:
            holes = compute_tooling_holes(self._panel_bounds, self._tooling_config)
            for hole in holes:
                panel_sexp.append(tooling_hole_to_sexp(hole))

        # Add fiducials
        if self._fiducial_config is not None:
            fiducials = compute_fiducials(self._panel_bounds, self._fiducial_config)
            for fid in fiducials:
                panel_sexp.append(fiducial_to_sexp(fid))

        # Edge.Cuts: closed boundary of boards + tabs (+ frame rail)
        self._render_outline(panel_sexp)

        self._panel_sexp = panel_sexp
        return panel_sexp

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_skeleton(self) -> SExp:
        """Create a skeleton kicad_pcb from the source.

        Copies the header (version, generator, general, layers, setup)
        but not the board content (footprints, segments, zones, etc.).
        """
        src = self._source_sexp
        assert src is not None

        panel = SExp.list("kicad_pcb")

        # Copy structural header nodes
        header_tags = {
            "version",
            "generator",
            "generator_version",
            "general",
            "paper",
            "layers",
            "setup",
        }
        for child in src.children:
            if child.name in header_tags:
                panel.append(_deep_copy_sexp(child))

        return panel

    def _build_nets(self, panel_sexp: SExp) -> None:
        """Collect and remap nets for all board instances.

        Each board instance gets its own net namespace prefixed with
        the board index to prevent conflicts.  Net 0 ("") is shared.
        """
        src = self._source_sexp
        assert src is not None

        # Always include net 0
        panel_sexp.append(SExp.list("net", 0, ""))
        self._net_map.clear()
        self._next_net = 1

        # Collect source nets
        source_nets: list[tuple[int, str]] = []
        for child in src.children:
            if child.name == "net":
                net_num = child.get_value(0)
                net_name = child.get_string(1) or ""
                if isinstance(net_num, int) and net_num != 0:
                    source_nets.append((net_num, net_name))

        # Create prefixed nets for each board instance
        for inst in self._instances:
            for src_num, src_name in source_nets:
                panel_net_num = self._next_net
                self._next_net += 1
                panel_net_name = f"B{inst.index}/{src_name}"
                panel_sexp.append(SExp.list("net", panel_net_num, panel_net_name))
                # Store mapping: (instance_index, source_net) -> panel_net
                self._net_map[(inst.index, src_num)] = panel_net_num

    def _get_panel_net(self, instance_index: int, source_net: int) -> int:
        """Look up the panel net number for a source net in a board instance."""
        if source_net == 0:
            return 0
        return self._net_map.get((instance_index, source_net), 0)

    def _place_board_copy(self, panel_sexp: SExp, inst: BoardInstance) -> None:
        """Clone all board content from source and place at instance offset.

        Clones footprints, segments, vias, zones, and graphic elements,
        remapping UUIDs and net numbers.
        """
        src = self._source_sexp
        assert src is not None

        # Content node types to clone. Includes both footprint spellings
        # (modern "footprint" and legacy pre-KiCad-6 "module") so legacy
        # boards are panelized instead of silently losing every component
        # (issue #4891) -- the child.name == "footprint" check below would
        # otherwise be unreachable for "module" nodes since they'd already
        # be filtered out here.
        content_tags = {
            *FOOTPRINT_TAGS,
            "segment",
            "arc",
            "via",
            "zone",
            "gr_text",
            *_GRAPHIC_TAGS,
        }

        mapper = self._instance_mapper(inst)

        for child in src.children:
            if child.name not in content_tags:
                continue

            # Skip Edge.Cuts graphics (we generate our own panel outline)
            if child.name in _GRAPHIC_TAGS:
                layer_node = child.find_child("layer")
                if layer_node and layer_node.get_string(0) == "Edge.Cuts":
                    continue

            cloned = _deep_copy_sexp(child)

            # Remap UUIDs
            _remap_uuids(cloned)

            # Remap net numbers
            _remap_nets(cloned, inst.index, self._get_panel_net)

            # Rigidly move the copy into its panel cell (Issue #6125).
            if _is_footprint_tag(child.name):
                _transform_footprint(cloned, mapper, inst.rotation)
                # Remap reference designators for footprints
                _remap_reference(cloned, inst.index)
            else:
                _transform_board_item(cloned, mapper, inst.rotation)

            panel_sexp.append(cloned)

    def _instance_mapper(self, inst: BoardInstance) -> _PointMapper:
        """Return the source-board -> panel point map for *inst*.

        The copy is rotated by ``inst.rotation`` about the source board's
        bounding-box centre (KiCad's Y-down ``RotatePoint``, so positive
        angles turn counter-clockwise on screen), then translated so its
        bounding box lands on ``inst.bounds``.  A rigid motion: every copy
        is congruent to the source board (Issue #6125).
        """
        min_x, min_y, max_x, max_y = self._board_bounds
        cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
        half_w, half_h = (max_x - min_x) / 2.0, (max_y - min_y) / 2.0
        if inst.rotation in (90.0, 270.0):
            half_w, half_h = half_h, half_w
        # Centre of the copy's cell in panel coordinates.
        tx = inst.offset_x + half_w
        ty = inst.offset_y + half_h
        return _rigid_mapper(cx, cy, inst.rotation, tx, ty)

    def outline_geometry(self) -> Any:
        """Return the panel's solid region as a Shapely geometry.

        The region is the union of every board copy's rectangle, every
        tab's rectangle and (when configured) the frame rail ring.  Its
        boundary is exactly what Edge.Cuts must trace: each tab splices
        into the board edge it attaches to (and into the rail or the
        neighbouring board on its far side), so the result is a set of
        closed rings with no dangling segments (Issue #6143).

        Coordinates are snapped to KiCad's 1 nm internal grid before the
        union so that tab and board edges computed by different float
        expressions still coincide exactly.
        """
        from shapely import set_precision  # type: ignore[import-untyped]
        from shapely.geometry import box  # type: ignore[import-untyped]
        from shapely.ops import unary_union  # type: ignore[import-untyped]

        parts = [box(*inst.bounds) for inst in self._instances]
        parts.extend(box(tab.min_x, tab.min_y, tab.max_x, tab.max_y) for tab in self._tabs)
        if self._frame_config is not None:
            outer = box(*self._panel_bounds)
            inner = box(*self._get_frame_inner_bounds())
            parts.append(outer.difference(inner))

        snapped = [set_precision(p, _OUTLINE_GRID_MM) for p in parts]
        region = set_precision(unary_union(snapped), _OUTLINE_GRID_MM)
        # Drop the collinear vertices the union leaves behind, so each
        # straight edge is one segment.
        return region.simplify(0)

    def _outline_rings(self) -> list[list[tuple[float, float]]]:
        """Return the closed Edge.Cuts rings (exteriors and holes)."""
        region = self.outline_geometry()
        polygons = list(getattr(region, "geoms", [region]))
        rings: list[list[tuple[float, float]]] = []
        for poly in polygons:
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
            for ring in (poly.exterior, *poly.interiors):
                rings.append([(float(x), float(y)) for x, y in ring.coords])
        return rings

    def _render_outline(self, panel_sexp: SExp) -> None:
        """Render the panel's Edge.Cuts as closed loops.

        Previously each board rectangle, the frame and every tab's two
        side lines were drawn independently.  The board rectangles were
        never opened where a tab joined them, so the tab sides dangled
        as T-junctions and ``kicad-cli pcb drc`` rejected the outline as
        malformed (Issue #6143).  Drawing the boundary of the unioned
        solid region instead guarantees every segment endpoint is shared
        by exactly two segments.
        """
        for ring in self._outline_rings():
            for (sx, sy), (ex, ey) in zip(ring[:-1], ring[1:], strict=True):
                panel_sexp.append(
                    gr_line_node(
                        sx,
                        sy,
                        ex,
                        ey,
                        layer="Edge.Cuts",
                        uuid_str=str(uuid.uuid4()),
                    )
                )

    def _add_vcuts(self, panel_sexp: SExp, config: VCutConfig) -> None:
        """Draw score lines on butted seams, plus their copper keepouts."""
        vcut_positions_h, vcut_positions_v = self._compute_vcut_positions()
        lines = generate_vcut_lines(self._panel_bounds, vcut_positions_h, "horizontal", config)
        lines.extend(generate_vcut_lines(self._panel_bounds, vcut_positions_v, "vertical", config))
        if lines:
            self._ensure_layer(panel_sexp, config.layer)
        for line in lines:
            panel_sexp.append(vcut_line_to_sexp(line, config))

        if config.clearance > 0:
            c = config.clearance
            for line in lines:
                x0 = min(line.start_x, line.end_x)
                x1 = max(line.start_x, line.end_x)
                y0 = min(line.start_y, line.end_y)
                y1 = max(line.start_y, line.end_y)
                if y0 == y1:
                    y0, y1 = y0 - c, y1 + c
                else:
                    x0, x1 = x0 - c, x1 + c
                # Rule area, not copper: forbids zone fill near the score so
                # a refill stops short of the blade (KiKit's V-cut
                # ``clearance``).  Tracks/vias stay legal -- the source
                # board's own placement is reported, not rewritten.
                panel_sexp.append(
                    keepout_node(
                        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                        layers=["*.Cu"],
                        no_tracks=False,
                        no_vias=False,
                        no_pour=True,
                        uuid_str=str(uuid.uuid4()),
                        name="V-score clearance",
                    )
                )
            self._vscore_findings = self._check_vscore_clearance(
                vcut_positions_h, vcut_positions_v, c
            )
            self._warnings.extend(f.message() for f in self._vscore_findings)

        gapped = self._gapped_seam_count()
        if gapped:
            self._warnings.append(
                f"V-cut panel: {gapped} seam(s) keep a gap, so they are tab-routed "
                f"({len(self._tabs)} tab(s)), not V-scored; fabs V-score only butted "
                "boards -- use spacing 0 (and frame space 0) to score every seam"
            )
        if not lines:
            self._warnings.append("V-cut panel has no butted seams, so no score lines were drawn")
        # ``warnings`` is the reporting channel (the CLI prints it); log at
        # info so library callers can opt in without the CLI echoing twice.
        for message in self._warnings:
            logger.info(message)

    def _gapped_seam_count(self) -> int:
        """Board/board and board/rail seams that keep a non-zero gap."""
        count = 0
        by_pos = {(i.row, i.col): i for i in self._instances}
        for (row, col), inst in by_pos.items():
            right = by_pos.get((row, col + 1))
            if right is not None and right.bounds[0] - inst.bounds[2] > BUTT_TOLERANCE_MM:
                count += 1
            below = by_pos.get((row + 1, col))
            if below is not None and below.bounds[1] - inst.bounds[3] > BUTT_TOLERANCE_MM:
                count += 1
        if self._frame_config is not None and self._instances:
            fx0, fy0, fx1, fy1 = self._get_frame_inner_bounds()
            bx0 = min(i.bounds[0] for i in self._instances)
            by0 = min(i.bounds[1] for i in self._instances)
            bx1 = max(i.bounds[2] for i in self._instances)
            by1 = max(i.bounds[3] for i in self._instances)
            gaps = (bx0 - fx0, by0 - fy0, fx1 - bx1, fy1 - by1)
            count += sum(1 for g in gaps if g > BUTT_TOLERANCE_MM)
        return count

    def _check_vscore_clearance(
        self,
        horizontal: list[float],
        vertical: list[float],
        clearance: float,
    ) -> list[VScoreClearanceFinding]:
        """Report source-board copper closer than *clearance* to a score.

        Each copy edge lying on a score line is mapped back, through the
        copy's rotation, to the source board edge it came from; that edge's
        closest copper is measured once on the source board.
        """
        if self._source_pcb is None:
            return []
        if self._edge_copper is None:
            self._edge_copper = edge_copper_clearances(self._source_pcb, self._source_rel_bounds)

        scored: dict[str, set[int]] = {}
        tol = BUTT_TOLERANCE_MM
        for inst in self._instances:
            x0, y0, x1, y1 = inst.bounds
            panel_sides = []
            for y in horizontal:
                if abs(y - y0) <= tol:
                    panel_sides.append("top")
                if abs(y - y1) <= tol:
                    panel_sides.append("bottom")
            for x in vertical:
                if abs(x - x0) <= tol:
                    panel_sides.append("left")
                if abs(x - x1) <= tol:
                    panel_sides.append("right")
            for side in panel_sides:
                scored.setdefault(source_side_for(side, inst.rotation), set()).add(inst.index)

        findings = []
        for side, copies in sorted(scored.items()):
            copper = self._edge_copper.get(side)
            if copper is None or copper.gap >= clearance - 1e-4:
                continue
            findings.append(
                VScoreClearanceFinding(
                    source_side=side,
                    gap=copper.gap,
                    required=clearance,
                    item=copper.item,
                    copies=tuple(sorted(copies)),
                )
            )
        return findings

    def _ensure_layer(self, panel_sexp: SExp, layer_name: str) -> None:
        """Add *layer_name* to the panel's layer table if it is missing.

        Only the KiCad user-comment / user-drawing layers are supported;
        their ordinal depends on the file's layer numbering scheme
        (KiCad 9+ renumbered layers so ``Edge.Cuts`` is 25, not 44).
        """
        layers = panel_sexp.find_child("layers")
        if layers is None:
            return
        # Each entry is ``(<ordinal> "<canonical>" <type> ["<user name>"])``
        # -- the ordinal is the list's name.
        names: set[str | None] = set()
        edge_id: str | None = None
        for child in layers.children:
            if child.is_atom:
                continue
            name = child.get_string(0)
            names.add(name)
            if len(child.children) > 2:
                names.add(child.get_string(2))
            if name == "Edge.Cuts":
                edge_id = child.name
        if layer_name in names:
            return
        modern = edge_id == "25"
        table = {
            "Dwgs.User": (17 if modern else 40, "User.Drawings"),
            "Cmts.User": (19 if modern else 41, "User.Comments"),
            "Eco1.User": (21 if modern else 42, "User.Eco1"),
            "Eco2.User": (23 if modern else 43, "User.Eco2"),
        }
        if layer_name not in table:
            return
        ordinal, user_name = table[layer_name]
        layers.append(SExp.list(str(ordinal), layer_name, "user", user_name))

    def _compute_vcut_positions(
        self,
    ) -> tuple[list[float], list[float]]:
        """Compute V-cut line positions: every butted seam.

        A position is scored when solid material lies on both sides of it
        with no gap: two butted copies, or a copy butted against the rail
        (frame space 0).  The score goes exactly on the shared edge.  The
        old midpoint-of-the-gap placement scored across an empty routed
        slot (Issue #6164).

        Returns:
            (horizontal_positions, vertical_positions) -- lists of Y and
            X coordinates where V-cut lines should be placed.
        """
        tol = BUTT_TOLERANCE_MM
        inner = self._get_frame_inner_bounds() if self._frame_config is not None else None

        def scored(
            low_edges: list[float],
            high_edges: list[float],
            rail_low: float | None,
            rail_high: float | None,
        ) -> list[float]:
            # ``low_edges``: where a solid *ends* (copy's max side);
            # ``high_edges``: where a solid *starts* (copy's min side).
            ends = list(low_edges)
            starts = list(high_edges)
            if rail_low is not None:
                ends.append(rail_low)  # the near rail ends at the inner edge
            if rail_high is not None:
                starts.append(rail_high)  # the far rail starts at the inner edge
            out: list[float] = []
            for pos in sorted(set(ends)):
                if any(abs(pos - other) <= tol for other in starts) and not any(
                    abs(pos - p) <= tol for p in out
                ):
                    out.append(pos)
            return out

        horizontal = scored(
            [i.bounds[3] for i in self._instances],
            [i.bounds[1] for i in self._instances],
            inner[1] if inner else None,
            inner[3] if inner else None,
        )
        vertical = scored(
            [i.bounds[2] for i in self._instances],
            [i.bounds[0] for i in self._instances],
            inner[0] if inner else None,
            inner[2] if inner else None,
        )
        return horizontal, vertical

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def instances(self) -> list[BoardInstance]:
        """Return the list of board instances in the panel."""
        return list(self._instances)

    @property
    def tabs(self) -> list[Tab]:
        """Return the list of tabs in the panel."""
        return list(self._tabs)

    @property
    def panel_bounds(self) -> tuple[float, float, float, float]:
        """Return (min_x, min_y, max_x, max_y) of the full panel."""
        return self._panel_bounds

    @property
    def warnings(self) -> list[str]:
        """Warnings from the last :meth:`build` (V-score gaps, clearance)."""
        return list(self._warnings)

    @property
    def vscore_findings(self) -> list[VScoreClearanceFinding]:
        """Copper-to-score clearance findings from the last :meth:`build`."""
        return list(self._vscore_findings)

    @property
    def board_count(self) -> int:
        """Return the number of board instances."""
        return len(self._instances)


# ======================================================================
# S-expression manipulation helpers
# ======================================================================


def _tab_crosses_any(tab: Tab, boxes: list[tuple[float, float, float, float]]) -> bool:
    """True if *tab*'s rectangle overlaps the interior of any of *boxes*."""
    eps = 1e-6
    for x0, y0, x1, y1 in boxes:
        if (
            tab.min_x < x1 - eps
            and tab.max_x > x0 + eps
            and tab.min_y < y1 - eps
            and tab.max_y > y0 + eps
        ):
            return True
    return False


def _deep_copy_sexp(node: SExp) -> SExp:
    """Create a deep copy of an S-expression tree.

    This avoids Python's ``copy.deepcopy`` which is slow for large
    trees. Instead we walk the tree manually. Round-trip metadata
    (_original_str for numerics, _originally_quoted for strings) is
    propagated so the cloned subtree serializes identically.
    """
    if node.is_atom:
        new_atom = SExp(value=node.value)
        new_atom._original_str = node._original_str
        new_atom._originally_quoted = node._originally_quoted
        return new_atom

    new_node = SExp(name=node.name)
    for child in node.children:
        new_node.children.append(_deep_copy_sexp(child))
    return new_node


def _remap_uuids(node: SExp) -> None:
    """Replace all (uuid ...) nodes in the tree with fresh UUIDs."""
    if node.name == "uuid" and node.children:
        node.children[0] = SExp(value=str(uuid.uuid4()))
        return

    for child in node.children:
        if not child.is_atom:
            _remap_uuids(child)


def _remap_nets(
    node: SExp,
    instance_index: int,
    net_lookup: Any,  # Callable[[int, int], int]
) -> None:
    """Remap net references using the panel net lookup function.

    Handles every spelling a cloned item can carry: ``(net N)``,
    ``(net N "name")`` (pads -- KiCad matches the name against the net
    table, so an unprefixed name leaves the pad on ``<no net>``) and the
    KiCad 10 name-only ``(net "name")`` (zones).  Names get the same
    ``B{index}/`` prefix as the panel net table (Issue #6125).
    """
    if node.name == "net" and node.children:
        first = node.children[0]
        if first.is_atom and isinstance(first.value, int) and not isinstance(first.value, bool):
            new_net = net_lookup(instance_index, first.value)
            node.children[0] = SExp(value=new_net)
            if len(node.children) > 1:
                name = node.children[1]
                if name.is_atom and isinstance(name.value, str):
                    new_name = f"B{instance_index}/{name.value}" if new_net else ""
                    node.children[1] = SExp.quoted_atom(new_name)
            return
        if first.is_atom and isinstance(first.value, str):
            if first.value:
                node.children[0] = SExp.quoted_atom(f"B{instance_index}/{first.value}")
            return

    # Also handle (net_name ...) inside pads
    if node.name == "net_name" and node.children:
        first = node.children[0]
        if first.is_atom and isinstance(first.value, str):
            node.children[0] = SExp(value=f"B{instance_index}/{first.value}")
            return

    for child in node.children:
        if not child.is_atom:
            _remap_nets(child, instance_index, net_lookup)


_GRAPHIC_TAGS = frozenset(
    {"gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly", "gr_curve", "gr_text_box"}
)

# Point-bearing nodes in board coordinates: ``(at X Y [A])`` for vias and
# texts, ``start``/``mid``/``end``/``center`` for tracks and graphics, and
# ``xy`` vertices of zone, ``gr_poly`` and text render-cache outlines.
_POINT_TAGS = frozenset({"at", "start", "mid", "end", "center", "xy"})

# Footprint children whose ``(at X Y A)`` angle is board-absolute in the
# KiCad file format (it already includes the footprint orientation), so a
# rotated copy must turn it too.  Their *positions* are footprint-local.
_FP_ABSOLUTE_ANGLE_TAGS = frozenset({"pad", "property", "fp_text"})

_TEXT_TAGS = frozenset({"gr_text", "gr_text_box"})

_PointMapper = Any  # Callable[[float, float], tuple[float, float]]

# Snap grid for the Edge.Cuts union: KiCad's internal unit is 1 nm.
_OUTLINE_GRID_MM = 1e-6


def _normalize_rotation(rotation: float) -> float:
    """Validate a per-board rotation and fold it into ``{0, 90, 180, 270}``."""
    folded = float(rotation) % 360.0
    quarter = round(folded / 90.0)
    if not math.isclose(folded, quarter * 90.0, abs_tol=1e-9):
        raise ValueError(f"Panel board rotation must be a multiple of 90 degrees, got {rotation!r}")
    return float((quarter % 4) * 90)


def _rigid_mapper(cx: float, cy: float, rotation: float, tx: float, ty: float) -> _PointMapper:
    """Map a point: rotate about ``(cx, cy)`` by *rotation*, then move the
    centre to ``(tx, ty)``.

    Uses KiCad's Y-down ``RotatePoint`` (``x' = x cos a + y sin a``,
    ``y' = -x sin a + y cos a``).  Quarter turns use exact integer
    cos/sin so a 90-degree copy has no floating-point drift.
    """
    quarter: dict[float, tuple[float, float]] = {
        0.0: (1.0, 0.0),
        90.0: (0.0, 1.0),
        180.0: (-1.0, 0.0),
        270.0: (0.0, -1.0),
    }
    c: float
    s: float
    if rotation in quarter:
        c, s = quarter[rotation]
    else:  # pragma: no cover - append_board rejects non-quarter angles
        a = math.radians(rotation)
        c, s = math.cos(a), math.sin(a)

    def mapper(x: float, y: float) -> tuple[float, float]:
        dx, dy = x - cx, y - cy
        return (dx * c + dy * s + tx, -dx * s + dy * c + ty)

    return mapper


def _mm_atom(value: float) -> SExp:
    """A numeric atom with KiCad's own 6-decimal precision.

    ``builders.fmt`` rounds to 0.01 mm, which would snap a 0.0254 mm grid
    copy off its source geometry -- the copy must stay congruent.
    """
    text = f"{round(value, 6):.6f}".rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return SExp(value=float(text), _original_str=text)


def _numeric(node: SExp, index: int) -> float | None:
    if len(node.children) <= index:
        return None
    child = node.children[index]
    if (
        child.is_atom
        and isinstance(child.value, (int, float))
        and not isinstance(child.value, bool)
    ):
        return float(child.value)
    return None


def _map_point_node(node: SExp, mapper: _PointMapper) -> None:
    """Map the leading ``X Y`` atoms of a point node in place."""
    x, y = _numeric(node, 0), _numeric(node, 1)
    if x is None or y is None:
        return
    nx, ny = mapper(x, y)
    node.children[0] = _mm_atom(nx)
    node.children[1] = _mm_atom(ny)


def _rotate_at_angle(at_node: SExp, rotation: float) -> None:
    """Add *rotation* to the angle of an ``(at X Y [A])`` node."""
    if rotation == 0.0:
        return
    if _numeric(at_node, 0) is None or _numeric(at_node, 1) is None:
        return
    angle = _numeric(at_node, 2)
    new_angle = ((angle or 0.0) + rotation) % 360.0
    atom = _mm_atom(new_angle)
    if angle is None:
        at_node.children.insert(2, atom)
    else:
        at_node.children[2] = atom


def _transform_board_item(node: SExp, mapper: _PointMapper, rotation: float) -> None:
    """Rigidly move a board-level item (track, via, arc, zone, ``gr_*``).

    Every point-bearing node in the subtree is in board coordinates, so
    each is mapped -- including the ``(xy ...)`` vertices of zone and
    ``gr_poly`` outlines and zone fills, which the old offset walk missed
    (Issue #6125).  Text ``(at)`` angles turn with the copy; a via's
    angle-less ``(at X Y)`` keeps its shape.
    """
    for child in node.children:
        if child.is_atom:
            continue
        if child.name in _POINT_TAGS:
            _map_point_node(child, mapper)
            if child.name == "at" and (node.name in _TEXT_TAGS or _numeric(child, 2) is not None):
                _rotate_at_angle(child, rotation)
        else:
            _transform_board_item(child, mapper, rotation)


def _transform_footprint(node: SExp, mapper: _PointMapper, rotation: float) -> None:
    """Rigidly move a footprint copy.

    Only the footprint's own ``(at ...)`` moves; pad, text and ``fp_*``
    coordinates are footprint-local and stay put (Issue #6125).  Embedded
    ``(zone ...)`` children -- an RF module's antenna keepout -- are stored
    in board coordinates with the placement already applied, so they move
    point-for-point, the same frame change as
    ``transform_footprint_zone_nodes`` (Issue #6119).  For a rotated copy,
    the board-absolute angles of pads and texts turn with the footprint.
    """
    for child in node.children:
        if child.is_atom:
            continue
        if child.name == "at":
            _map_point_node(child, mapper)
            _rotate_at_angle(child, rotation)
        elif child.name == "zone":
            _transform_board_item(child, mapper, rotation)
        elif child.name in _FP_ABSOLUTE_ANGLE_TAGS and rotation != 0.0:
            at_node = child.find_child("at")
            if at_node is not None:
                _rotate_at_angle(at_node, rotation)


def _remap_reference(footprint_node: SExp, instance_index: int) -> None:
    """Prefix footprint reference designators with board index.

    Changes e.g. "R1" to "B0_R1" for board instance 0. Prefer the modern
    Reference property; fall back to legacy fp_text reference fields.
    """
    for child in footprint_node.children:
        if child.name == "property" and len(child.children) >= 2:
            name_child = child.children[0]
            value_child = child.children[1]
            if (
                name_child.is_atom
                and name_child.value == "Reference"
                and value_child.is_atom
                and isinstance(value_child.value, str)
            ):
                child.children[1] = SExp(value=f"B{instance_index}_{value_child.value}")
                return

    for child in footprint_node.children:
        if child.name == "fp_text" and len(child.children) >= 2:
            kind, value = child.children[:2]
            if (
                kind.is_atom
                and kind.value == "reference"
                and value.is_atom
                and isinstance(value.value, str)
            ):
                child.children[1] = SExp(value=f"B{instance_index}_{value.value}")
                return
