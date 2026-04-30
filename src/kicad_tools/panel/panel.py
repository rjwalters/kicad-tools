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

import copy
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.core.sexp_file import load_pcb, save_pcb
from kicad_tools.pcb.board_geometry import BoardGeometry, has_shapely
from kicad_tools.sexp.builders import (
    fmt,
    gr_line_node,
    gr_rect_node,
    uuid_node,
)
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
from .tabs import Tab, compute_tabs_between_boards, compute_tabs_to_frame

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
        self._board_bounds: tuple[float, float, float, float] = (0, 0, 0, 0)
        self._instances: list[BoardInstance] = []
        self._tabs: list[Tab] = []
        self._panel_sexp: SExp | None = None
        self._net_map: dict[int, int] = {}  # source net -> panel net
        self._next_net: int = 1
        self._panel_bounds: tuple[float, float, float, float] = (0, 0, 0, 0)
        self._frame_config: FrameConfig | None = None

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, board_path: str | Path, config: PanelConfig) -> Panel:
        """Build a complete panel from a config in one call.

        Args:
            board_path: Path to the source ``.kicad_pcb`` file.
            config: Full panel configuration.

        Returns:
            A fully configured Panel ready for ``save()``.
        """
        panel = cls()
        panel.append_board(
            board_path,
            rows=config.rows,
            cols=config.cols,
            spacing=config.spacing,
            rotation=config.rotation,
        )

        if config.frame is not None:
            panel.make_frame(
                width=config.frame.width,
                space=config.frame.space,
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
    ) -> Panel:
        """Load a board and place it in a grid layout.

        Each board copy is offset so that there is *spacing* mm between
        adjacent board edges.  Net names are prefixed with the board
        index to prevent conflicts across copies.

        Args:
            board_path: Path to the source ``.kicad_pcb``.
            rows: Number of rows in the grid.
            cols: Number of columns in the grid.
            spacing: Gap between board edges in mm.
            rotation: Per-board rotation in degrees.

        Returns:
            ``self`` for method chaining.
        """
        board_path = Path(board_path)
        self._source_sexp = load_pcb(board_path)
        self._source_path = board_path

        # Extract board bounds using BoardGeometry
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(board_path)
        geom = BoardGeometry.from_pcb(pcb)
        min_x, min_y, max_x, max_y = geom.bounds
        board_w = max_x - min_x
        board_h = max_y - min_y
        self._board_bounds = (min_x, min_y, max_x, max_y)

        # Place board copies in grid
        self._instances.clear()
        for row in range(rows):
            for col in range(cols):
                idx = row * cols + col
                offset_x = col * (board_w + spacing)
                offset_y = row * (board_h + spacing)

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
                tabs = compute_tabs_between_boards(
                    a.bounds, b.bounds, config, "horizontal"
                )
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
                tabs = compute_tabs_between_boards(
                    a.bounds, b.bounds, config, "vertical"
                )
                self._tabs.extend(tabs)

        # Tabs to frame (if frame is configured)
        if self._frame_config is not None:
            frame_inner = self._get_frame_inner_bounds()
            for inst in self._instances:
                tabs = compute_tabs_to_frame(
                    inst.bounds, frame_inner, config
                )
                self._tabs.extend(tabs)

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
        self._mousebite_config = MousebiteConfig(
            diameter=diameter, spacing=spacing, offset=offset
        )
        return self

    def make_vcuts(
        self,
        line_width: float = 0.1,
    ) -> Panel:
        """Generate V-cut score lines between board rows/columns.

        V-cuts are straight lines that span the entire panel width
        or height.

        Args:
            line_width: Line width on Edge.Cuts in mm.

        Returns:
            ``self`` for method chaining.
        """
        self._vcut_config = VCutConfig(line_width=line_width)
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
        self._tooling_config = ToolingHoleConfig(
            diameter=diameter, offset=offset, pattern=pattern
        )
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
            raise ValueError(
                "No board loaded. Call append_board() before build()."
            )

        # Start with a skeleton PCB based on the source
        panel_sexp = self._build_skeleton()

        # Collect nets across all board instances
        self._build_nets(panel_sexp)

        # Place board copies
        for inst in self._instances:
            self._place_board_copy(panel_sexp, inst)

        # Add tabs as Edge.Cuts lines
        for tab in self._tabs:
            self._render_tab(panel_sexp, tab)

        # Add mousebite holes
        if hasattr(self, "_mousebite_config"):
            for tab in self._tabs:
                holes = generate_mousebite_holes(tab, self._mousebite_config)
                for hole in holes:
                    panel_sexp.append(mousebite_hole_to_sexp(hole))

        # Add V-cut lines
        if hasattr(self, "_vcut_config"):
            vcut_positions_h, vcut_positions_v = self._compute_vcut_positions()
            lines = generate_vcut_lines(
                self._panel_bounds, vcut_positions_h, "horizontal", self._vcut_config
            )
            lines.extend(
                generate_vcut_lines(
                    self._panel_bounds, vcut_positions_v, "vertical", self._vcut_config
                )
            )
            for line in lines:
                panel_sexp.append(vcut_line_to_sexp(line, self._vcut_config))

        # Add frame outline
        if self._frame_config is not None:
            self._render_frame(panel_sexp)

        # Add tooling holes
        if hasattr(self, "_tooling_config"):
            holes = compute_tooling_holes(self._panel_bounds, self._tooling_config)
            for hole in holes:
                panel_sexp.append(tooling_hole_to_sexp(hole))

        # Add fiducials
        if hasattr(self, "_fiducial_config"):
            fiducials = compute_fiducials(self._panel_bounds, self._fiducial_config)
            for fid in fiducials:
                panel_sexp.append(fiducial_to_sexp(fid))

        # Add panel-level Edge.Cuts outline
        self._render_panel_outline(panel_sexp)

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
            "version", "generator", "generator_version", "general",
            "paper", "layers", "setup",
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
        self._net_map[0] = 0
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
                panel_sexp.append(
                    SExp.list("net", panel_net_num, panel_net_name)
                )
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

        # Content node types to clone
        content_tags = {
            "footprint", "segment", "via", "zone", "gr_line", "gr_arc",
            "gr_rect", "gr_circle", "gr_text", "gr_poly",
        }

        src_min_x, src_min_y = self._board_bounds[0], self._board_bounds[1]

        for child in src.children:
            if child.name not in content_tags:
                continue

            # Skip Edge.Cuts graphics (we generate our own panel outline)
            if child.name in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly"):
                layer_node = child.find_child("layer")
                if layer_node and layer_node.get_string(0) == "Edge.Cuts":
                    continue

            cloned = _deep_copy_sexp(child)

            # Remap UUIDs
            _remap_uuids(cloned)

            # Remap net numbers
            _remap_nets(cloned, inst.index, self._get_panel_net)

            # Offset positions
            _offset_positions(cloned, inst.offset_x - src_min_x, inst.offset_y - src_min_y)

            # Remap reference designators for footprints
            if child.name == "footprint":
                _remap_reference(cloned, inst.index)

            panel_sexp.append(cloned)

    def _render_tab(self, panel_sexp: SExp, tab: Tab) -> None:
        """Render a tab as Edge.Cuts line segments.

        Draws the two edge lines of the tab (the sides perpendicular
        to the board edge).  The board-edge portion of the tab is
        implicit -- it replaces a section of the original board outline.
        """
        tab_uuid1 = str(uuid.uuid4())
        tab_uuid2 = str(uuid.uuid4())

        if tab.orientation == "horizontal":
            # Tab spans horizontally -- draw vertical side lines
            panel_sexp.append(
                gr_line_node(
                    tab.min_x, tab.min_y, tab.min_x, tab.max_y,
                    layer="Edge.Cuts", uuid_str=tab_uuid1,
                )
            )
            panel_sexp.append(
                gr_line_node(
                    tab.max_x, tab.min_y, tab.max_x, tab.max_y,
                    layer="Edge.Cuts", uuid_str=tab_uuid2,
                )
            )
        else:
            # Tab spans vertically -- draw horizontal side lines
            panel_sexp.append(
                gr_line_node(
                    tab.min_x, tab.min_y, tab.max_x, tab.min_y,
                    layer="Edge.Cuts", uuid_str=tab_uuid1,
                )
            )
            panel_sexp.append(
                gr_line_node(
                    tab.min_x, tab.max_y, tab.max_x, tab.max_y,
                    layer="Edge.Cuts", uuid_str=tab_uuid2,
                )
            )

    def _render_frame(self, panel_sexp: SExp) -> None:
        """Render the panel frame as Edge.Cuts lines."""
        if self._frame_config is None:
            return

        px0, py0, px1, py1 = self._panel_bounds
        w = self._frame_config.width

        # Outer frame rectangle
        outer = [
            (px0, py0, px1, py0),  # top
            (px1, py0, px1, py1),  # right
            (px1, py1, px0, py1),  # bottom
            (px0, py1, px0, py0),  # left
        ]
        for sx, sy, ex, ey in outer:
            panel_sexp.append(
                gr_line_node(sx, sy, ex, ey, layer="Edge.Cuts",
                             uuid_str=str(uuid.uuid4()))
            )

        # Inner frame rectangle
        inner = [
            (px0 + w, py0 + w, px1 - w, py0 + w),
            (px1 - w, py0 + w, px1 - w, py1 - w),
            (px1 - w, py1 - w, px0 + w, py1 - w),
            (px0 + w, py1 - w, px0 + w, py0 + w),
        ]
        for sx, sy, ex, ey in inner:
            panel_sexp.append(
                gr_line_node(sx, sy, ex, ey, layer="Edge.Cuts",
                             uuid_str=str(uuid.uuid4()))
            )

    def _render_panel_outline(self, panel_sexp: SExp) -> None:
        """Render per-board Edge.Cuts outlines (without frame)."""
        if self._frame_config is not None:
            # Frame handles the outer outline; render individual board outlines
            for inst in self._instances:
                bx0, by0, bx1, by1 = inst.bounds
                edges = [
                    (bx0, by0, bx1, by0),
                    (bx1, by0, bx1, by1),
                    (bx1, by1, bx0, by1),
                    (bx0, by1, bx0, by0),
                ]
                for sx, sy, ex, ey in edges:
                    panel_sexp.append(
                        gr_line_node(sx, sy, ex, ey, layer="Edge.Cuts",
                                     uuid_str=str(uuid.uuid4()))
                    )
        else:
            # No frame -- render a simple outline around the entire panel
            px0, py0, px1, py1 = self._panel_bounds
            edges = [
                (px0, py0, px1, py0),
                (px1, py0, px1, py1),
                (px1, py1, px0, py1),
                (px0, py1, px0, py0),
            ]
            for sx, sy, ex, ey in edges:
                panel_sexp.append(
                    gr_line_node(sx, sy, ex, ey, layer="Edge.Cuts",
                                 uuid_str=str(uuid.uuid4()))
                )

    def _compute_vcut_positions(
        self,
    ) -> tuple[list[float], list[float]]:
        """Compute V-cut line positions from the board grid.

        Returns:
            (horizontal_positions, vertical_positions) -- lists of Y and
            X coordinates where V-cut lines should be placed.
        """
        horizontal: list[float] = []
        vertical: list[float] = []

        # Group instances by row/col to find boundaries
        rows_set: dict[int, list[BoardInstance]] = {}
        cols_set: dict[int, list[BoardInstance]] = {}
        for inst in self._instances:
            rows_set.setdefault(inst.row, []).append(inst)
            cols_set.setdefault(inst.col, []).append(inst)

        # Horizontal V-cuts between rows
        sorted_rows = sorted(rows_set.keys())
        for i in range(len(sorted_rows) - 1):
            top_row = rows_set[sorted_rows[i]]
            bottom_row = rows_set[sorted_rows[i + 1]]
            # V-cut at midpoint between rows
            top_max_y = max(inst.bounds[3] for inst in top_row)
            bottom_min_y = min(inst.bounds[1] for inst in bottom_row)
            horizontal.append((top_max_y + bottom_min_y) / 2.0)

        # Vertical V-cuts between columns
        sorted_cols = sorted(cols_set.keys())
        for i in range(len(sorted_cols) - 1):
            left_col = cols_set[sorted_cols[i]]
            right_col = cols_set[sorted_cols[i + 1]]
            left_max_x = max(inst.bounds[2] for inst in left_col)
            right_min_x = min(inst.bounds[0] for inst in right_col)
            vertical.append((left_max_x + right_min_x) / 2.0)

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
    def board_count(self) -> int:
        """Return the number of board instances."""
        return len(self._instances)


# ======================================================================
# S-expression manipulation helpers
# ======================================================================


def _deep_copy_sexp(node: SExp) -> SExp:
    """Create a deep copy of an S-expression tree.

    This avoids Python's ``copy.deepcopy`` which is slow for large
    trees. Instead we walk the tree manually.
    """
    if node.is_atom:
        return SExp(value=node.value)

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
    """Remap (net N) nodes using the panel net lookup function."""
    if node.name == "net" and node.children:
        first = node.children[0]
        if first.is_atom and isinstance(first.value, int):
            new_net = net_lookup(instance_index, first.value)
            node.children[0] = SExp(value=new_net)
            # Also remap net_name if present
            if len(node.children) > 1 and node.children[1].is_atom:
                # This is a net definition node, skip
                pass
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


def _offset_positions(node: SExp, dx: float, dy: float) -> None:
    """Offset all position-bearing nodes by (dx, dy).

    Handles (at X Y ...), (start X Y), (end X Y), (mid X Y).
    """
    position_tags = {"at", "start", "end", "mid"}

    if node.name in position_tags and len(node.children) >= 2:
        x_child = node.children[0]
        y_child = node.children[1]
        if x_child.is_atom and isinstance(x_child.value, (int, float)):
            if y_child.is_atom and isinstance(y_child.value, (int, float)):
                node.children[0] = SExp(value=fmt(float(x_child.value) + dx))
                node.children[1] = SExp(value=fmt(float(y_child.value) + dy))

    for child in node.children:
        if not child.is_atom:
            _offset_positions(child, dx, dy)


def _remap_reference(footprint_node: SExp, instance_index: int) -> None:
    """Prefix footprint reference designators with board index.

    Changes e.g. "R1" to "B0_R1" for board instance 0.
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
                child.children[1] = SExp(
                    value=f"B{instance_index}_{value_child.value}"
                )
                return
