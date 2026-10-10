"""Question-specific board images for agents (issue #6316).

``kct screenshot`` plots layers through ``kicad-cli``; the result carries no
net identity, no coordinates and no sign of what is unfinished.  This renderer
reads the board through :class:`~kicad_tools.schema.pcb.PCB` and draws, with
matplotlib, the things that plot lacks: mm axes in a named coordinate frame,
reference designators, pad net labels, one net highlighted, and the
connections still missing as ratsnest lines.

:func:`board_view` is the single entry point shared by ``kct board-view`` and
the ``board_view`` MCP tool.  It never raises for a user error: the result
dict carries ``success`` and ``error_message``, in the shape
``screenshot_board`` returns.

matplotlib is an optional extra and is imported only when an image is drawn;
the ``list`` view works without it.
"""

from __future__ import annotations

import base64
import difflib
import io
import math
import textwrap
from pathlib import Path
from typing import Any

from .model import (
    FRAME_BOARD,
    BoardGeometry,
    Box,
    LinkReport,
    PadGeo,
    Point,
    collect_geometry,
    missing_links,
)

__all__ = [
    "CAVEAT",
    "MATPLOTLIB_HINT",
    "MAX_VISION_API_PX",
    "VIEWS",
    "board_view",
    "draw_panel",
    "matplotlib_available",
    "net_window",
]

#: Longest image side, in pixels (same value as ``mcp.tools.screenshot``).
MAX_VISION_API_PX = 1568
MIN_SIZE_PX = 320

VIEWS = ("overview", "net", "layers", "crop", "list")

MATPLOTLIB_HINT = (
    "matplotlib is required to draw board-view images. "
    "Install with: pip install 'kicad-tools[visualization]' "
    "(the 'list' view works without it)"
)

#: One image-based diagnosis in the session that motivated this tool was
#: wrong and only a routing test caught it; every result repeats the warning.
CAVEAT = (
    "An image reading is a hypothesis, not a measurement: confirm it with "
    "kct net-status, kct check or kicad-cli pcb drc before acting on it. "
    "Clearances, widths, connectivity and counts come from those tools, "
    "not from the picture."
)

# (hex, word).  The word goes in the image title: a model cannot ask what a
# colour means.  Through-hole pads, vias and missing links have their own
# colours, kept apart from every layer colour.
_LAYER_STYLE: dict[str, tuple[str, str]] = {
    "F.Cu": ("#e0483c", "red"),
    "B.Cu": ("#3d7fe0", "blue"),
    "In1.Cu": ("#3fae4a", "green"),
    "In2.Cu": ("#2bc4c4", "cyan"),
    "In3.Cu": ("#9a6fd0", "purple"),
    "In4.Cu": ("#8a9a3a", "olive"),
}
_FALLBACK_STYLE = ("#a0785a", "brown")
_BG = "#101214"
_HILITE = "#ffffff"
_RATS = "#ff3df2"
_RATS_OTHER = "#9a9a9a"
_THRU_PAD = "#e8c21a"
_VIA = "#d8d8d8"
_OUTLINE = "#8c8c8c"

_DPI = 100
_TITLE_FONT_PT = 9.0
_TITLE_CHAR_PX = 7.6  # DejaVu Sans Mono advance at 9 pt, 100 dpi (7.53) + slack
_TITLE_LINE_PX = 15.5
# Fixed pixel margins round each panel's plot area.
_M_LEFT, _M_RIGHT, _M_BOTTOM = 48, 14, 26
_M_TOP_PLAIN, _M_TOP_PANEL = 6, 22
#: Pad net labels are drawn only at or above this scale (about a 35 mm window).
PAD_LABEL_MIN_PX_PER_MM = 40.0


def matplotlib_available() -> bool:
    """Probe for matplotlib (patched in tests to simulate its absence)."""
    try:
        import matplotlib  # noqa: F401
        from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: F401
    except ImportError:
        return False
    return True


def _layer_style(layer: str) -> tuple[str, str]:
    return _LAYER_STYLE.get(layer, _FALLBACK_STYLE)


def _color_key(copper: list[str]) -> dict[str, str]:
    key = {layer: _layer_style(layer)[1] for layer in copper}
    key.update(
        {
            "through_hole_pads": "gold",
            "vias": "light grey with a black hole",
            "board_outline": "grey",
            "missing_links": "magenta dashed",
            "missing_links_other_nets": "grey dotted (only when one net is highlighted)",
            "highlighted_net": "white outline",
        }
    )
    return key


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _frame_note(geo: BoardGeometry) -> str:
    ox, oy = _fmt(geo.origin[0]), _fmt(geo.origin[1])
    if geo.frame == FRAME_BOARD:
        return (
            "Axes in mm, board-relative: (0, 0) is the Edge.Cuts minimum corner and y grows "
            "downward. This is the frame of kct net-status and kct pcb move-footprint --to. "
            f"kct check reports sheet-absolute locations = axis value + ({ox}, {oy})."
        )
    return (
        "Axes in mm, sheet-absolute (the coordinates in the file, as kct check reports them); "
        f"y grows downward. Board-relative position = axis value - ({ox}, {oy})."
    )


def net_window(
    geo: BoardGeometry, report: LinkReport, net: str, margin_mm: float = 4.0
) -> Box | None:
    """Window round a net's missing links, or round its pads when complete."""
    points: list[Point] = [
        (end.x, end.y) for link in report.links.get(net, []) for end in (link.a, link.b)
    ]
    if not points:
        points = [(p.x, p.y) for p in geo.pads if p.net == net]
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x0, x1 = min(xs) - margin_mm, max(xs) + margin_mm
    y0, y1 = min(ys) - margin_mm, max(ys) + margin_mm
    # Keep the crop from being a sliver: at least 12 mm on each side.
    if x1 - x0 < 12:
        centre = (x0 + x1) / 2
        x0, x1 = centre - 6, centre + 6
    if y1 - y0 < 12:
        centre = (y0 + y1) / 2
        y0, y1 = centre - 6, centre + 6
    return (x0, y0, x1, y1)


def _tick_step(span: float) -> float:
    for step in (0.5, 1, 2, 5, 10, 20, 50, 100):
        if span / step <= 16:
            return step
    return 200


def _box_hits(box: Box, x0: float, y0: float, x1: float, y1: float, pad: float = 0.0) -> bool:
    return not (x1 < box[0] - pad or x0 > box[2] + pad or y1 < box[1] - pad or y0 > box[3] + pad)


def draw_panel(
    ax: Any,
    geo: BoardGeometry,
    report: LinkReport,
    box: Box,
    *,
    net: str | None = None,
    only_layer: str | None = None,
    px_per_mm: float = 20.0,
    labels: bool = True,
) -> None:
    """Draw one panel into *ax*, whose data coordinates are frame mm."""
    from matplotlib.collections import LineCollection, PatchCollection, PolyCollection
    from matplotlib.patches import Circle

    x0, y0, x1, y1 = box
    ax.set_facecolor(_BG)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y1, y0)  # KiCad y grows downward
    ax.set_aspect("equal", adjustable="box")

    def lw(mm: float) -> float:
        return max(0.4, mm * px_per_mm * 72.0 / _DPI)

    def inside(x: float, y: float, pad: float = 0.0) -> bool:
        return x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad

    for line in geo.outline:
        xs, ys = [p[0] for p in line], [p[1] for p in line]
        ax.plot(xs, ys, color=_OUTLINE, linewidth=1.0, zorder=0.5)

    alpha = 0.85 if only_layer else 0.6
    for layer in reversed(geo.copper):  # back first, front on top
        if only_layer and layer != only_layer:
            continue
        color = _layer_style(layer)[0]
        dim: list[Any] = []
        dim_w: list[float] = []
        hot: list[Any] = []
        hot_w: list[float] = []
        for track in geo.tracks:
            if track.layer != layer:
                continue
            xs, ys = [p[0] for p in track.points], [p[1] for p in track.points]
            if not _box_hits(box, min(xs), min(ys), max(xs), max(ys), track.width):
                continue
            if net and track.net == net:
                hot.append(track.points)
                hot_w.append(lw(track.width))
            else:
                dim.append(track.points)
                dim_w.append(lw(track.width))
        if dim:
            ax.add_collection(
                LineCollection(
                    dim, colors=color, linewidths=dim_w, alpha=alpha, capstyle="round", zorder=1
                )
            )
        if hot:
            ax.add_collection(
                LineCollection(
                    hot,
                    colors=_HILITE,
                    linewidths=[w + 1.8 for w in hot_w],
                    capstyle="round",
                    zorder=2,
                )
            )
            ax.add_collection(
                LineCollection(hot, colors=color, linewidths=hot_w, capstyle="round", zorder=2.1)
            )

    shown: list[PadGeo] = []
    polys: list[Any] = []
    faces: list[str] = []
    edges: list[str] = []
    holes: list[Any] = []
    for pad in geo.pads:
        hw, hh = pad.half_extents
        if not _box_hits(box, pad.x - hw, pad.y - hh, pad.x + hw, pad.y + hh):
            continue
        if only_layer and only_layer not in pad.layers:
            continue
        shown.append(pad)
        polys.append(pad.outline())
        faces.append(_THRU_PAD if len(pad.layers) > 1 else _layer_style(next(iter(pad.layers)))[0])
        edges.append(_HILITE if (net and pad.net == net) else "none")
        if pad.drill > 0:
            holes.append(Circle((pad.x, pad.y), pad.drill / 2))
    if polys:
        ax.add_collection(
            PolyCollection(
                polys, facecolors=faces, edgecolors=edges, linewidths=1.8, alpha=0.9, zorder=3
            )
        )
    if holes:
        ax.add_collection(PatchCollection(holes, facecolors=_BG, edgecolors="none", zorder=3.5))

    barrels: list[Any] = []
    barrel_edges: list[str] = []
    barrel_lw: list[float] = []
    drills: list[Any] = []
    for via in geo.vias:
        r = via.size / 2
        if not _box_hits(box, via.x - r, via.y - r, via.x + r, via.y + r):
            continue
        if only_layer and only_layer not in via.layers:
            continue
        is_hot = bool(net and via.net == net)
        barrels.append(Circle((via.x, via.y), r))
        barrel_edges.append(_HILITE if is_hot else "#555555")
        barrel_lw.append(1.8 if is_hot else 0.5)
        drills.append(Circle((via.x, via.y), via.drill / 2))
    if barrels:
        ax.add_collection(
            PatchCollection(
                barrels, facecolors=_VIA, edgecolors=barrel_edges, linewidths=barrel_lw, zorder=4
            )
        )
        ax.add_collection(
            PatchCollection(drills, facecolors="#000000", edgecolors="none", zorder=5)
        )

    for name, links in report.links.items():
        other = bool(net and name != net)
        for link in links:
            if not _box_hits(
                box,
                min(link.a.x, link.b.x),
                min(link.a.y, link.b.y),
                max(link.a.x, link.b.x),
                max(link.a.y, link.b.y),
            ):
                continue
            xs, ys = [link.a.x, link.b.x], [link.a.y, link.b.y]
            if other:
                ax.plot(
                    xs, ys, color=_RATS_OTHER, linewidth=0.9, linestyle=":", alpha=0.85, zorder=6
                )
            else:
                ax.plot(xs, ys, color=_RATS, linewidth=1.8, linestyle="--", zorder=6.5)
                ax.plot(
                    xs,
                    ys,
                    linestyle="none",
                    marker="o",
                    markersize=9,
                    markerfacecolor="none",
                    markeredgecolor=_RATS,
                    markeredgewidth=1.4,
                    zorder=6.6,
                )

    if labels:
        _draw_labels(ax, shown, geo.pads, box, net=net, px_per_mm=px_per_mm, inside=inside)

    step = _tick_step(max(x1 - x0, y1 - y0))
    ax.set_xticks([t * step for t in range(math.ceil(x0 / step), math.floor(x1 / step) + 1)])
    ax.set_yticks([t * step for t in range(math.ceil(y0 / step), math.floor(y1 / step) + 1)])
    ax.grid(color="#ffffff", alpha=0.13, linewidth=0.5)
    ax.tick_params(colors="#cccccc", labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#666666")


def _draw_labels(
    ax: Any,
    shown: list[PadGeo],
    all_pads: list[PadGeo],
    box: Box,
    *,
    net: str | None,
    px_per_mm: float,
    inside: Any,
) -> None:
    span = max(box[2] - box[0], box[3] - box[1])
    # A reference goes at the centre of its footprint's whole pad box, pulled
    # inside the window when the footprint is only partly in view.
    visible = {pad.ref for pad in shown if pad.ref}
    extent: dict[str, list[float]] = {}
    for pad in all_pads:
        if pad.ref not in visible:
            continue
        hw, hh = pad.half_extents
        e = extent.setdefault(pad.ref, [pad.x - hw, pad.y - hh, pad.x + hw, pad.y + hh])
        e[0], e[1] = min(e[0], pad.x - hw), min(e[1], pad.y - hh)
        e[2], e[3] = max(e[2], pad.x + hw), max(e[3], pad.y + hh)
    inset_x = min(18.0 / px_per_mm, (box[2] - box[0]) / 2)
    inset_y = min(10.0 / px_per_mm, (box[3] - box[1]) / 2)
    ref_at: dict[str, Point] = {
        ref: (
            min(max((e[0] + e[2]) / 2, box[0] + inset_x), box[2] - inset_x),
            min(max((e[1] + e[3]) / 2, box[1] + inset_y), box[3] - inset_y),
        )
        for ref, e in extent.items()
    }

    if px_per_mm >= PAD_LABEL_MIN_PX_PER_MM:
        for pad in shown:
            if not pad.net or not inside(pad.x, pad.y):
                continue
            is_hot = bool(net and pad.net == net)
            hw, hh = pad.half_extents
            if min(hw, hh) * 2 * px_per_mm < 9 and not is_hot:
                continue  # too small a pad for a legible label
            # An elongated pad (an IC pin) carries its label on the copper,
            # along its long axis, so it stays in the pin's own lane; a
            # squarish pad gets it just above, clear of the reference label.
            elongated = max(hw, hh) >= 1.5 * min(hw, hh)
            if elongated and pad.ref in ref_at:
                rx, ry = ref_at[pad.ref]
                if math.hypot(pad.x - rx, pad.y - ry) * px_per_mm < 30:
                    elongated = False  # a centre pad: the reference label sits here
            ax.text(
                pad.x,
                pad.y if elongated else pad.y - hh - 0.04,
                f"{pad.number}:{pad.net}" if is_hot else pad.net,
                color=_RATS if is_hot else "#e4e4e4",
                fontsize=7.5 if is_hot else 5.5,
                weight="bold" if is_hot else "normal",
                ha="center",
                va="center" if elongated else "bottom",
                rotation=90 if (elongated and hh > hw) else 0,
                zorder=9.5 if is_hot else 9,
                clip_on=True,
                bbox={"facecolor": "#000000", "alpha": 0.6, "pad": 0.6, "edgecolor": "none"},
            )

    for ref, (cx, cy) in ref_at.items():
        ax.text(
            cx,
            cy,
            ref,
            color="#ffffff",
            fontsize=9 if span < 40 else 7,
            weight="bold",
            ha="center",
            va="center",
            zorder=8,
            clip_on=True,
            bbox={"facecolor": "#000000", "alpha": 0.55, "pad": 1, "edgecolor": "none"},
        )


def _wrap(paragraphs: list[str], chars: int) -> list[str]:
    lines: list[str] = []
    for paragraph in paragraphs:
        lines.extend(textwrap.wrap(paragraph, width=max(chars, 20)) or [""])
    return lines


def _plan(
    box: Box, rows: int, cols: int, max_px: int, paragraphs: list[str], panel_titles: bool
) -> tuple[int, int, float, list[str]]:
    """Figure size in pixels, scale, and the wrapped title.

    The title band's height depends on the figure width (wrapping) and the
    figure width can depend on the band's height, so settle it by trying
    successively taller bands.
    """
    w_mm, h_mm = box[2] - box[0], box[3] - box[1]
    m_top = _M_TOP_PANEL if panel_titles else _M_TOP_PLAIN
    chrome_w = cols * (_M_LEFT + _M_RIGHT)
    plan: tuple[int, int, float, list[str]] | None = None
    for n_lines in range(1, 40):
        band = 10 + _TITLE_LINE_PX * n_lines
        chrome_h = band + rows * (m_top + _M_BOTTOM)
        ppm = min((max_px - chrome_w) / (cols * w_mm), (max_px - chrome_h) / (rows * h_mm))
        if ppm <= 0:
            break
        fig_w = math.floor(chrome_w + cols * w_mm * ppm)
        fig_h = math.floor(chrome_h + rows * h_mm * ppm)
        lines = _wrap(paragraphs, int((fig_w - 16) / _TITLE_CHAR_PX))
        plan = (fig_w, fig_h, ppm, lines)
        if len(lines) <= n_lines:
            break
    if plan is None:
        raise ValueError(f"window {w_mm:g} x {h_mm:g} mm cannot be drawn in {max_px} px")
    return plan


def _render(
    geo: BoardGeometry,
    report: LinkReport,
    box: Box,
    *,
    net: str | None,
    per_layer: bool,
    max_px: int,
    paragraphs: list[str],
) -> tuple[bytes, int, int, float, str]:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    panels: list[str | None] = list(geo.copper) if per_layer else [None]
    cols = 1 if len(panels) == 1 else (2 if len(panels) <= 4 else 3)
    rows = math.ceil(len(panels) / cols)
    fig_w, fig_h, ppm, lines = _plan(box, rows, cols, max_px, paragraphs, per_layer)
    w_mm, h_mm = box[2] - box[0], box[3] - box[1]
    m_top = _M_TOP_PANEL if per_layer else _M_TOP_PLAIN
    band = fig_h - rows * (m_top + _M_BOTTOM + h_mm * ppm)

    # A Figure with its own Agg canvas: no pyplot, so no global backend or
    # figure registry is touched (this runs inside the MCP server process).
    fig = Figure(figsize=(fig_w / _DPI, fig_h / _DPI), dpi=_DPI, facecolor=_BG)
    FigureCanvasAgg(fig)
    title = "\n".join(lines)
    fig.text(
        8 / fig_w,
        1 - 6 / fig_h,
        title,
        color="#ffffff",
        fontsize=_TITLE_FONT_PT,
        family="monospace",
        va="top",
        ha="left",
        linespacing=1.15,
    )

    cell_w = _M_LEFT + w_mm * ppm + _M_RIGHT
    cell_h = m_top + h_mm * ppm + _M_BOTTOM
    for index, layer in enumerate(panels):
        row, col = divmod(index, cols)
        left = col * cell_w + _M_LEFT
        top = band + row * cell_h + m_top
        ax = fig.add_axes(
            (
                left / fig_w,
                1 - (top + h_mm * ppm) / fig_h,
                (w_mm * ppm) / fig_w,
                (h_mm * ppm) / fig_h,
            )
        )
        draw_panel(ax, geo, report, box, net=net, only_layer=layer, px_per_mm=ppm)
        if layer is not None:
            hex_color, word = _layer_style(layer)
            ax.set_title(f"{layer} ({word})", color=hex_color, fontsize=9, pad=4)

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=_DPI, facecolor=fig.get_facecolor())
    png = buffer.getvalue()
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    return png, width, height, ppm, title


def _failure(view: str, pcb_path: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "view": view,
        "pcb_path": pcb_path,
        "image_base64": None,
        "width_px": 0,
        "height_px": 0,
        "output_path": None,
        "error_message": message,
    }


def _paragraphs(
    view: str,
    name: str,
    geo: BoardGeometry,
    report: LinkReport,
    net: str | None,
    box: Box,
) -> list[str]:
    n_nets, n_links = len(report.links), report.link_count
    if net is None:
        what = "window" if view == "crop" else "overview"
        head = (
            f"{name}: {what}, all copper layers. {n_nets} unfinished nets, {n_links} missing "
            "links (magenta dashed, circled ends)."
        )
    else:
        own = len(report.links.get(net, []))
        state = (
            f"its {own} missing link{'s' if own != 1 else ''} magenta dashed with circled ends"
            if own
            else "it has no missing links"
        )
        layout = (
            "one panel per copper layer, same window" if view == "layers" else "all copper layers"
        )
        head = (
            f"{name}: net {net}, {layout}. Its tracks, pads and vias are outlined white and its "
            f"pads labelled pad:net when the scale allows; {state}; other nets' missing links "
            f"grey dotted ({n_links} links on {n_nets} nets board-wide)."
        )
    if report.pour_advisory:
        names = ", ".join(entry["net"] for entry in report.pour_advisory)
        head += (
            f" Pour nets with pads the fill does not reach (advisory in kct check, no link "
            f"drawn): {names}."
        )
    window = ", ".join(_fmt(v) for v in box)
    frame = f"{_frame_note(geo)} Window x0, y0, x1, y1 = {window}."
    layers = ", ".join(f"{layer} {_layer_style(layer)[1]}" for layer in geo.copper)
    key = (
        f"Colours: {layers}; through-hole pads gold; vias light grey with a black hole; board "
        "outline grey. Zone pours are not drawn, so a pad with no track may still be connected "
        "through a pour. " + CAVEAT
    )
    return [head, frame, key]


def board_view(
    pcb_path: str,
    view: str = "overview",
    *,
    net: str | None = None,
    window: tuple[float, float, float, float] | list[float] | None = None,
    absolute: bool = False,
    margin_mm: float = 4.0,
    max_size_px: int = MAX_VISION_API_PX,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Render one question-specific view of a board, or list its missing links.

    Args:
        pcb_path: Path to a ``.kicad_pcb`` file.
        view: ``overview`` (whole board), ``net`` (crop round one net),
            ``layers`` (that crop, one panel per copper layer), ``crop`` (an
            explicit window) or ``list`` (missing links only, no image).
        net: Net to highlight.  Required for ``net`` and ``layers``, optional
            for ``crop``, ignored otherwise.
        window: ``(x0, y0, x1, y1)`` in mm for ``crop``, in the selected frame.
        absolute: Draw in sheet-absolute coordinates instead of board-relative.
        margin_mm: Margin round the net for ``net`` and ``layers``.
        max_size_px: Longest image side (default 1568).
        output_path: Where to save the PNG, if anywhere.

    Returns:
        A dict with ``success`` and ``error_message``; on success the frame,
        the missing links and, for image views, ``image_base64``, ``width_px``,
        ``height_px`` and ``output_path``.
    """
    path = Path(pcb_path)
    if view not in VIEWS:
        return _failure(
            view, pcb_path, f"Unknown view {view!r} (expected one of: {', '.join(VIEWS)})"
        )
    if not path.exists():
        return _failure(view, pcb_path, f"PCB file not found: {pcb_path}")
    if path.suffix != ".kicad_pcb":
        return _failure(
            view, pcb_path, f"Invalid file extension: {path.suffix} (expected .kicad_pcb)"
        )
    if view in ("net", "layers") and not net:
        return _failure(view, pcb_path, f"The {view!r} view needs a net name")
    if view in ("overview", "list"):
        net = None
    if view != "list":
        if not MIN_SIZE_PX <= max_size_px <= 8192:
            return _failure(
                view,
                pcb_path,
                f"max_size_px must be between {MIN_SIZE_PX} and 8192, got {max_size_px}",
            )
        if not matplotlib_available():
            return _failure(view, pcb_path, MATPLOTLIB_HINT)

    from kicad_tools.schema.pcb import PCB

    try:
        pcb = PCB.load(path)
        geo = collect_geometry(pcb, absolute=absolute)
        report = missing_links(pcb, absolute=absolute)
    except Exception as exc:  # a malformed board is a user error, not a crash
        return _failure(view, pcb_path, f"Could not read {pcb_path}: {exc}")

    result: dict[str, Any] = {
        "success": True,
        "view": view,
        "pcb_path": str(pcb_path),
        "net": net,
        "units": "mm",
        "frame": geo.frame,
        "board_origin_mm": [geo.origin[0], geo.origin[1]],
        "frame_note": _frame_note(geo),
        "unfinished_net_count": len(report.links),
        "missing_link_count": report.link_count,
        "missing_links": report.links_to_dict(),
        "pour_advisory": report.pour_advisory,
        "caveat": CAVEAT,
        "error_message": None,
    }
    if geo.outline_error:
        result["outline_error"] = geo.outline_error
    if view == "list":
        return result

    if net is not None and net not in geo.net_names():
        close = difflib.get_close_matches(net, sorted(geo.net_names()), n=5, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return _failure(view, pcb_path, f"Net {net!r} has no pads or copper on this board.{hint}")

    box: Box | None
    if view == "crop":
        if window is None or len(window) != 4:
            return _failure(view, pcb_path, "The 'crop' view needs a window: x0 y0 x1 y1 in mm")
        wx0, wy0, wx1, wy1 = (float(v) for v in window)
        box = (min(wx0, wx1), min(wy0, wy1), max(wx0, wx1), max(wy0, wy1))
    elif view == "overview":
        if geo.bounds is None:
            return _failure(view, pcb_path, "Nothing to draw: no board outline, pads or copper")
        bx0, by0, bx1, by1 = geo.bounds
        box = (bx0 - 1.0, by0 - 1.0, bx1 + 1.0, by1 + 1.0)
    else:
        assert net is not None
        box = net_window(geo, report, net, margin_mm)
        if box is None:
            return _failure(view, pcb_path, f"Net {net!r} has no pads to centre a view on")
    if box[2] - box[0] < 0.5 or box[3] - box[1] < 0.5:
        return _failure(view, pcb_path, "Window is smaller than 0.5 mm on a side")

    paragraphs = _paragraphs(view, path.name, geo, report, net, box)
    try:
        png, width, height, ppm, title = _render(
            geo,
            report,
            box,
            net=net,
            per_layer=view == "layers",
            max_px=max_size_px,
            paragraphs=paragraphs,
        )
    except ValueError as exc:
        return _failure(view, pcb_path, str(exc))

    saved: str | None = None
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(png)
        saved = str(out)

    result.update(
        {
            "image_base64": base64.b64encode(png).decode("ascii"),
            "width_px": width,
            "height_px": height,
            "output_path": saved,
            "window_mm": [round(v, 4) for v in box],
            "px_per_mm": round(ppm, 3),
            "pad_labels_drawn": ppm >= PAD_LABEL_MIN_PX_PER_MM,
            "title": title,
            "color_key": _color_key(geo.copper),
            "pours_drawn": False,
        }
    )
    return result
