"""Panel configuration dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CutMethod(Enum):
    """Separation method for panelized boards."""

    MOUSEBITE = "mousebite"
    VCUT = "vcut"


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
        offset: Offset from tab edge in mm (inward).
    """

    diameter: float = 0.5
    spacing: float = 0.8
    offset: float = 0.0


@dataclass
class VCutConfig:
    """Configuration for V-cut score lines.

    V-cuts are straight horizontal or vertical score lines across the
    full panel width/height. They are rendered as graphic lines on the
    Edge.Cuts layer.

    Attributes:
        line_width: Width of the V-cut line on Edge.Cuts in mm.
        layer: Layer name for V-cut lines.
    """

    line_width: float = 0.1
    layer: str = "Edge.Cuts"


@dataclass
class FrameConfig:
    """Configuration for the panel frame (rails).

    Attributes:
        width: Frame rail width in mm.
        space: Gap between board edge and inner frame edge in mm.
    """

    width: float = 5.0
    space: float = 2.0


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
        spacing: Gap between board instances in mm.
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
    spacing: float = 2.0
    rotation: float = 0.0
    cut_method: CutMethod = CutMethod.MOUSEBITE
    tabs: TabConfig = field(default_factory=TabConfig)
    mousebite: MousebiteConfig = field(default_factory=MousebiteConfig)
    vcut: VCutConfig = field(default_factory=VCutConfig)
    frame: FrameConfig | None = None
    tooling_holes: ToolingHoleConfig | None = None
    fiducials: FiducialConfig | None = None
