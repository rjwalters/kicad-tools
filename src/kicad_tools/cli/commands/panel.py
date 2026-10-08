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


def _fab_vscore_clearance(cli_mfr: str | None, board_path: Path) -> tuple[float, str, str]:
    """Resolve the fab's copper-to-V-score clearance (Issue #6177).

    The fab comes from the shared ``--mfr`` resolver (explicit flag >
    ``fab_profile.json`` sidecar > ``project.kct`` ``target_fab`` >
    ``jlcpcb``).  Returns ``(clearance_mm, mfr_id, source)`` where *source*
    is ``"fab"`` for a published figure or ``"unsourced_default"`` when the
    profile carries none.

    Every profile's figure is stackup-independent (a test pins that), so the
    2-layer rules are consulted without loading the board to count layers.
    """
    from kicad_tools.manufacturers.resolve import resolve_cli_manufacturer
    from kicad_tools.manufacturers.vscore import vscore_clearance_for

    mfr = resolve_cli_manufacturer(cli_mfr, board_path)
    resolved = vscore_clearance_for(mfr)
    return resolved.mm, resolved.mfr, "fab" if resolved.sourced else "unsourced_default"


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

    vscore_clearance = getattr(args, "panel_vscore_clearance", None)
    vscore_mfr: str | None = getattr(args, "panel_mfr", None)
    vscore_source: str | None = None
    if cut_method == CutMethod.VCUT:
        if vscore_clearance is not None:
            vscore_source = "cli"
        else:
            vscore_clearance, vscore_mfr, vscore_source = _fab_vscore_clearance(
                vscore_mfr, board_path
            )
    vcut = VCutConfig(
        layer=getattr(args, "panel_vcut_layer", None) or VCutConfig.layer,
        clearance=VCutConfig.clearance if vscore_clearance is None else vscore_clearance,
    )

    frame = None
    if getattr(args, "panel_frame", False):
        frame = FrameConfig(
            width=getattr(args, "panel_frame_width", 5.0),
            # None: the cut method's default (0 for vcut, 2.0 otherwise).
            space=getattr(args, "panel_frame_space", None),
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
        # None: the cut method's default (0 for vcut, 2.0 otherwise).
        spacing=getattr(args, "panel_spacing", None),
        spacing_x=getattr(args, "panel_spacing_x", None),
        spacing_y=getattr(args, "panel_spacing_y", None),
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

    # What the panel actually used: a butted seam between non-straight
    # board sides is gapped instead (Issue #6158).
    gap_x, gap_y = panel.spacing
    frame_space = panel.frame_space
    is_vcut = config.cut_method == CutMethod.VCUT

    if as_json:
        emit_json(
            {
                "command": "panel",
                "input": str(board_path),
                "output": str(result_path),
                "grid": {
                    "rows": config.rows,
                    "cols": config.cols,
                    # A single number when both axes match; null for a
                    # mixed panel (see spacing_x_mm / spacing_y_mm).
                    "spacing_mm": gap_x if gap_x == gap_y else None,
                    "spacing_x_mm": gap_x,
                    "spacing_y_mm": gap_y,
                },
                "board_count": panel.board_count,
                "tabs": len(panel.tabs),
                "cut_method": config.cut_method.value,
                "vcut_layer": (config.vcut.layer if config.cut_method == CutMethod.VCUT else None),
                "tab_width_mm": config.tabs.width,
                "tab_count": config.tabs.count,
                "frame": config.frame is not None,
                "frame_space_mm": frame_space,
                "vscore_clearance_mm": config.vcut.clearance if is_vcut else None,
                # "cli" (--vscore-clearance), "fab" (published by the
                # profile) or "unsourced_default"; null unless --cut vcut.
                "vscore_clearance_source": vscore_source,
                "mfr": vscore_mfr if is_vcut else None,
                "warnings": panel.warnings,
                "tooling_holes": config.tooling_holes is not None,
                "fiducials": config.fiducials is not None,
                "success": True,
            }
        )
        return 0

    print(f"Panel created: {result_path}")
    print(f"  Grid: {config.rows}x{config.cols} ({panel.board_count} boards)")
    if gap_x == gap_y:
        print(f"  Spacing: {gap_x:g} mm")
    else:
        print(f"  Spacing: {gap_x:g} mm between columns, {gap_y:g} mm between rows")
    print(f"  Tabs: {len(panel.tabs)}")
    print(f"  Cut method: {config.cut_method.value}")
    if is_vcut:
        print(f"  V-score layer: {config.vcut.layer}")
        clearance_note = {
            "cli": "--vscore-clearance",
            "fab": f"{vscore_mfr} published",
            "unsourced_default": f"{vscore_mfr} publishes none; unsourced default",
        }.get(vscore_source or "", "default")
        print(f"  V-score clearance: {config.vcut.clearance:g} mm ({clearance_note})")
    for message in panel.warnings:
        print(f"Warning: {message}", file=sys.stderr)
    return 0
