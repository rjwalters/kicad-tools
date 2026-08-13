"""Panel (panel) CLI command handler.

Machine output (``--format json``, issue #4674): ``kct panel`` prints exactly
one document describing the panel it built -- the resolved ``input``/``output``
paths, the ``grid`` geometry, ``board_count``, ``tabs``, ``cut_method`` and the
optional frame features -- or ``{"error": ..., "success": false}`` on any
failure, with the exit code unchanged.  See
``docs/reference/machine-output.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..format_options import FORMAT_JSON, emit_json

__all__ = ["run_panel_command"]


def _fail(as_json: bool, board: str, message: str, *, text: str | None = None) -> int:
    """Report a panel failure as a document (JSON) or prose (text)."""
    if as_json:
        emit_json(
            {
                "command": "panel",
                "input": board,
                "error": message,
                "success": False,
            }
        )
    else:
        print(text if text is not None else f"Error: {message}", file=sys.stderr)
    return 1


def run_panel_command(args) -> int:
    """Handle the ``kct panel`` command.

    Creates a manufacturing panel from a source board PCB.
    """
    as_json = getattr(args, "format", "text") == FORMAT_JSON

    board_path = Path(args.panel_input)
    if not board_path.exists():
        return _fail(as_json, str(board_path), f"File not found: {board_path}")

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
        return _fail(
            as_json,
            str(board_path),
            f"{exc}\n"
            "Panelization requires Shapely. Install with: "
            "pip install kicad-tools[geometry]",
        )

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
    except Exception as exc:
        return _fail(
            as_json,
            str(board_path),
            f"creating panel: {exc}",
            text=f"Error creating panel: {exc}",
        )

    if as_json:
        emit_json(
            {
                "command": "panel",
                "input": str(board_path),
                "output": str(result_path),
                "grid": {
                    "rows": config.rows,
                    "cols": config.cols,
                    "spacing_mm": config.spacing,
                },
                "board_count": panel.board_count,
                "tabs": len(panel.tabs),
                "cut_method": config.cut_method.value,
                "tab_width_mm": config.tabs.width,
                "tab_count": config.tabs.count,
                "frame": config.frame is not None,
                "tooling_holes": config.tooling_holes is not None,
                "fiducials": config.fiducials is not None,
                "success": True,
            }
        )
        return 0

    print(f"Panel created: {result_path}")
    print(f"  Grid: {config.rows}x{config.cols} ({panel.board_count} boards)")
    print(f"  Tabs: {len(panel.tabs)}")
    print(f"  Cut method: {config.cut_method.value}")
    return 0
