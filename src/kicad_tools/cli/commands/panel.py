"""Panel (panel) CLI command handler."""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["run_panel_command"]


def run_panel_command(args) -> int:
    """Handle the ``kct panel`` command.

    Creates a manufacturing panel from a source board PCB.
    """
    board_path = Path(args.panel_input)
    if not board_path.exists():
        print(f"Error: File not found: {board_path}", file=sys.stderr)
        return 1

    output_path = args.panel_output
    if output_path is None:
        output_path = str(board_path.with_stem(board_path.stem + "_panel"))

    try:
        from kicad_tools.panel import CutMethod, Panel
        from kicad_tools.panel.config import (
            FiducialConfig,
            FrameConfig,
            MousebiteConfig,
            PanelConfig,
            TabConfig,
            ToolingHoleConfig,
            VCutConfig,
        )
    except ImportError as exc:
        print(
            f"Error: {exc}\n"
            "Panelization requires Shapely. Install with: "
            "pip install kicad-tools[geometry]",
            file=sys.stderr,
        )
        return 1

    # Build config from CLI args
    cut_method = CutMethod.MOUSEBITE
    if hasattr(args, "panel_cut") and args.panel_cut == "vcut":
        cut_method = CutMethod.VCUT

    tabs = TabConfig(
        width=getattr(args, "panel_tab_width", 3.0),
        count=getattr(args, "panel_tab_count", 3),
    )

    mousebite = MousebiteConfig(
        diameter=getattr(args, "panel_mousebite_diameter", 0.5),
        spacing=getattr(args, "panel_mousebite_spacing", 0.8),
    )

    vcut = VCutConfig()

    frame = None
    if getattr(args, "panel_frame", False):
        frame = FrameConfig(
            width=getattr(args, "panel_frame_width", 5.0),
            space=getattr(args, "panel_frame_space", 2.0),
        )

    tooling = None
    if getattr(args, "panel_tooling_holes", False):
        tooling = ToolingHoleConfig()

    fiducials = None
    if getattr(args, "panel_fiducials", False):
        fiducials = FiducialConfig()

    config = PanelConfig(
        rows=getattr(args, "panel_rows", 2),
        cols=getattr(args, "panel_cols", 2),
        spacing=getattr(args, "panel_spacing", 2.0),
        cut_method=cut_method,
        tabs=tabs,
        mousebite=mousebite,
        vcut=vcut,
        frame=frame,
        tooling_holes=tooling,
        fiducials=fiducials,
    )

    try:
        panel = Panel.from_config(board_path, config)
        result_path = panel.save(output_path)
        print(f"Panel created: {result_path}")
        print(f"  Grid: {config.rows}x{config.cols} ({panel.board_count} boards)")
        print(f"  Tabs: {len(panel.tabs)}")
        print(f"  Cut method: {config.cut_method.value}")
        return 0
    except Exception as exc:
        print(f"Error creating panel: {exc}", file=sys.stderr)
        return 1
