"""Panelization support for KiCad PCB files.

Provides :class:`Panel` -- a builder-pattern API for creating
manufacturing panels from individual board designs. Supports grid
layout, tab generation, mousebite/V-cut separation features, and
panel furniture (tooling holes, fiducials).

Usage::

    from kicad_tools.panel import Panel

    panel = Panel()
    panel.append_board("board.kicad_pcb", rows=2, cols=2)
    panel.make_tabs(width=3.0, count=3)
    panel.make_mousebites(diameter=0.5, spacing=0.8)
    panel.save("panel.kicad_pcb")
"""

from .config import CutMethod, PanelConfig, TabConfig
from .panel import Panel

__all__ = [
    "CutMethod",
    "Panel",
    "PanelConfig",
    "TabConfig",
]
