"""Panel configuration dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CutMethod(Enum):
    """Separation method for panelized boards."""

    MOUSEBITE = "mousebite"
    VCUT = "vcut"


# Gap between copies (and between copies and the rail) for tab-routed
# panels.  V-scored panels default to 0: the boards are butted and the
# score is the separation (Issue #6164).
DEFAULT_GAP_MM = 2.0


@dataclass
class TabConfig:
    """Configuration for breakaway tabs between boards.

    Attributes:
        width: Tab width in mm.
        count: Number of tabs per board edge (when using fixed count).
        spacing: Tab spacing in mm (when using spacing-based placement).
            If set, overrides *count*.
        min_length: Minimum tab length along the board edge in mm.
    """

    width: float = 3.0
    count: int = 3
    spacing: float | None = None
    min_length: float = 2.0


@dataclass
class MousebiteConfig:
    """Configuration for mousebite perforations.

    Attributes:
        diameter: NPTH hole diameter in mm.
        spacing: Center-to-center distance between holes in mm.
        offset: Gap in mm between the tab's side edges and the edge of the
            first/last hole (0 = tangent; holes never overlap the slot).
    """

    diameter: float = 0.5
    spacing: float = 0.8
    offset: float = 0.0


@dataclass
class VCutConfig:
    """Configuration for V-cut score lines.

    V-cuts are straight horizontal or vertical score lines across the
    full panel width/height.  They are rendered as graphic lines on a
    documentation layer (``Cmts.User``), the convention fabs and KiKit
    use.  They must not go on Edge.Cuts: an open score line there makes
    the board outline malformed (Issue #6143).

    A V-score only separates boards that are butted edge to edge, so
    score lines go exactly on shared board edges (and on board/rail edges
    when the frame is butted too); seams that keep a gap are tab-routed
    instead (Issue #6164).

    Attributes:
        line_width: Width of the V-cut line in mm.
        layer: Layer name for V-cut lines.
        clearance: Minimum copper distance from a score line in mm.  The
            panel warns about board copper closer than this to a scored
            edge and adds a copper-pour keepout this wide on each side of
            every score line, so a zone refill stops short of the score.
            Fabs typically ask for 0.3--0.5 mm; 0 disables both.  The
            0.4 mm library default matches JLCPCB and PCBWay; ``kct panel``
            replaces it with the resolved fab's
            ``DesignRules.min_copper_to_vscore_mm`` (see
            :func:`kicad_tools.manufacturers.vscore.vscore_clearance_for`).
    """

    line_width: float = 0.1
    layer: str = "Cmts.User"
    clearance: float = 0.4


@dataclass
class FrameConfig:
    """Configuration for the panel frame (rails).

    Attributes:
        width: Frame rail width in mm.
        space: Gap between board edge and inner frame edge in mm.  ``None``
            picks the cut method's default: 0 for V-cut panels (the rails
            butt against the boards and are V-scored off, KiKit's
            ``space: 0mm`` convention) and 2.0 for mousebite panels.
    """

    width: float = 5.0
    space: float | None = None

    def resolved_space(self, cut_method: CutMethod) -> float:
        """The board-to-rail gap, applying the cut method's default."""
        if self.space is not None:
            return self.space
        return 0.0 if cut_method == CutMethod.VCUT else DEFAULT_GAP_MM


@dataclass
class ToolingHoleConfig:
    """Configuration for tooling holes.

    Attributes:
        diameter: Hole diameter in mm.
        offset: Distance from frame corner to hole center in mm.
        pattern: Number of holes -- 3 or 4.
    """

    diameter: float = 3.0
    offset: float = 3.5
    pattern: int = 3


@dataclass
class FiducialConfig:
    """Configuration for fiducial marks.

    Attributes:
        diameter: Copper pad diameter in mm.
        mask_margin: Solder mask opening margin in mm.
        offset: Distance from frame corner to fiducial center in mm.
    """

    diameter: float = 1.0
    mask_margin: float = 2.0
    offset: float = 5.0


@dataclass
class PanelConfig:
    """Top-level panel configuration.

    Attributes:
        rows: Number of board rows.
        cols: Number of board columns.
        spacing: Gap between board instances in mm.  ``None`` picks the cut
            method's default: 0 (butted) for V-cut, 2.0 for mousebite.
        spacing_x: Gap between columns in mm, overriding *spacing*.
        spacing_y: Gap between rows in mm, overriding *spacing*.  Giving
            the two axes different gaps under V-cut builds a mixed panel:
            the butted axis is V-scored, the gapped axis is tab-routed
            with mousebites.
        rotation: Per-board rotation in degrees (0, 90, 180, 270).
        cut_method: Separation method (mousebite or vcut).
        tabs: Tab configuration.
        mousebite: Mousebite configuration (used when cut_method is MOUSEBITE).
        vcut: V-cut configuration (used when cut_method is VCUT).
        frame: Frame/rail configuration. None means no frame.
        tooling_holes: Tooling hole config. None means no tooling holes.
        fiducials: Fiducial config. None means no fiducials.
    """

    rows: int = 2
    cols: int = 2
    spacing: float | None = None
    spacing_x: float | None = None
    spacing_y: float | None = None
    rotation: float = 0.0
    cut_method: CutMethod = CutMethod.MOUSEBITE
    tabs: TabConfig = field(default_factory=TabConfig)
    mousebite: MousebiteConfig = field(default_factory=MousebiteConfig)
    vcut: VCutConfig = field(default_factory=VCutConfig)
    frame: FrameConfig | None = None
    tooling_holes: ToolingHoleConfig | None = None
    fiducials: FiducialConfig | None = None

    def resolved_spacing(self) -> tuple[float, float]:
        """``(gap between columns, gap between rows)`` in mm."""
        if self.spacing is not None:
            base = self.spacing
        else:
            base = 0.0 if self.cut_method == CutMethod.VCUT else DEFAULT_GAP_MM
        gap_x = self.spacing_x if self.spacing_x is not None else base
        gap_y = self.spacing_y if self.spacing_y is not None else base
        return gap_x, gap_y
