"""Stamp board identification (project / revision / date) on silkscreen.

``kct pcb add-board-info <pcb> --text "{project} {rev} {date}"`` writes a
board-level ``gr_text`` whose fields are resolved from the PCB title block
(``{project}`` falls back to the sibling ``.kicad_pro`` stem).  An
unresolvable field is an error that names it.

Placement is either explicit (``--at X Y``, KiCad file coordinates) or
``--auto``: scan the board for the free spot nearest the bottom-left corner
that clears pad apertures, other silk, component courtyards and the board
edge, using the same geometry helpers as ``kct place-silk-refs`` and the
silk DRC rules.  Back-side text (``B.SilkS``) is written mirrored.

An optional fab order-number placeholder (``--order-placeholder``, default
``JLCJLCJLCJLC``) is stamped as a second text next to the first.
"""

from __future__ import annotations

import json
import math
import re
import sys
import uuid as uuid_module
from pathlib import Path
from typing import Any

DEFAULT_TEXT = "{project} {rev} {date}"
DEFAULT_ORDER_PLACEHOLDER = "JLCJLCJLCJLC"
DEFAULT_FONT_SIZE_MM = 1.0
DEFAULT_FONT_THICKNESS_MM = 0.15
_FIELD_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_VALID_LAYERS = ("F.SilkS", "B.SilkS")
_SEARCH_STEP_MM = 0.5


class BoardInfoError(Exception):
    """User-facing failure (unresolvable field, no free region, ...)."""


def resolve_fields(template: str, pcb, pcb_path: Path) -> str:
    """Expand ``{project}``, ``{rev}`` and ``{date}`` in *template*.

    Raises:
        BoardInfoError: for an unknown field or one with no value.
    """
    project = (pcb.title or "").strip()
    if not project and pcb_path.with_suffix(".kicad_pro").exists():
        project = pcb_path.stem
    values = {
        "project": project,
        "rev": (pcb.revision or "").strip(),
        "date": (pcb.date or "").strip(),
    }

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise BoardInfoError(
                f"unknown field {{{name}}} (supported: {', '.join('{' + k + '}' for k in values)})"
            )
        if not values[name]:
            source = {
                "project": "title block 'title' (or a sibling .kicad_pro)",
                "rev": "title block 'rev'",
                "date": "title block 'date'",
            }[name]
            raise BoardInfoError(f"field {{{name}}} is unresolvable: no value in {source}")
        return values[name]

    return _FIELD_RE.sub(sub, template)


def _build_node(text: str, x: float, y: float, layer: str, size: float, thickness: float):
    from kicad_tools.sexp.builders import gr_text_node

    node = gr_text_node(
        text,
        x,
        y,
        layer=layer,
        font_size=size,
        font_thickness=thickness,
        uuid_str=str(uuid_module.uuid4()),
    )
    if layer.startswith("B."):
        from kicad_tools.sexp.parser import SExp

        effects = node.find("effects")
        if effects is not None:
            effects.append(SExp.list("justify", "mirror"))
    return node


def _obstacles(pcb, side: str, mask_clearance_mm: float):
    """Collect ``(apertures, silk, courtyards, material, outline)`` for *side*."""
    from shapely.affinity import translate  # type: ignore[import-untyped]
    from shapely.geometry import MultiLineString, Polygon  # type: ignore[import-untyped]

    from kicad_tools.geometry.courtyard import _courtyard_polygon, _side_has_geometry
    from kicad_tools.silkscreen.place_refs import (
        _board_material,
        _iter_owned_static_silk,
    )
    from kicad_tools.validate.rules.silkscreen import _iter_pad_apertures, _iter_via_apertures

    apertures = [
        (g, lbl) for s, g, lbl in _iter_pad_apertures(pcb, mask_clearance_mm) if s == side
    ] + [(g, lbl) for s, g, lbl in _iter_via_apertures(pcb, mask_clearance_mm) if s == side]
    # Footprint silk is already board-relative, but board-level gr_text /
    # gr_* graphics (owner None) keep the file's sheet-absolute coordinates;
    # shift them into the board-relative frame the search runs in.
    ox, oy = pcb._board_origin
    silk = []
    for s, g, lbl, owner in _iter_owned_static_silk(pcb):
        if s != side:
            continue
        if owner is None and (ox or oy):
            g = translate(g, xoff=-ox, yoff=-oy)
        silk.append((g, lbl))
    # Reference text is movable in place_refs, but here it is fixed: include it.
    from kicad_tools.silkscreen.place_refs import _visible_reference

    for fp in pcb.footprints:
        visible = _visible_reference(fp)
        if visible is not None and visible[1] == side:
            silk.append((visible[4], fp.reference))
    courtyards = []
    for fp in pcb.footprints:
        if _side_has_geometry(fp, side):
            poly = _courtyard_polygon(fp, side, Polygon)
            if poly is not None:
                courtyards.append((poly, fp.reference))
    segments, material, error = _board_material(pcb, Polygon)
    if error or material is None:
        raise BoardInfoError(error or "board outline unavailable")
    outline = MultiLineString([[a, b] for a, b in segments])
    return apertures, silk, courtyards, material, outline


def _find_free_spot(
    text: str,
    size: float,
    thickness: float,
    obstacles,
    extra_silk: list,
    clearance_mm: float,
    edge_clearance_mm: float,
) -> tuple[float, float] | None:
    """Return the clear text centre nearest the bottom-left of the board."""
    from kicad_tools.silkscreen.place_refs import _candidate_clear
    from kicad_tools.validate.rules.silkscreen import _text_bbox_geometry

    apertures, silk, courtyards, material, outline = obstacles
    min_x, min_y, max_x, max_y = material.bounds
    half_w = (len(text) * size * 0.7 + thickness) / 2.0
    half_h = (size + thickness) / 2.0
    xs: list[float] = []
    x = min_x + half_w
    while x <= max_x - half_w:
        xs.append(x)
        x += _SEARCH_STEP_MM
    ys: list[float] = []
    y = max_y - half_h
    while y >= min_y + half_h:
        ys.append(y)
        y -= _SEARCH_STEP_MM
    # Bottom-left first (KiCad y grows downward, so "bottom" is max y).
    candidates = sorted(
        ((cx, cy) for cx in xs for cy in ys),
        key=lambda p: (p[0] - min_x) + (max_y - p[1]),
    )
    for cx, cy in candidates:
        geom = _text_bbox_geometry(text, (size, size), thickness, (cx, cy))
        if geom is None:
            continue
        ok, _why = _candidate_clear(
            geom,
            apertures=apertures,
            silk_obstacles=silk + extra_silk,
            courtyards=courtyards,
            outline=outline,
            board_material=material,
            outline_error="",
            clearance_mm=clearance_mm,
            edge_clearance_mm=edge_clearance_mm,
        )
        if ok:
            return cx, cy
    return None


def run_add_board_info(
    pcb_path: Path,
    text: str = DEFAULT_TEXT,
    layer: str = "F.SilkS",
    at: tuple[float, float] | None = None,
    auto: bool = False,
    order_placeholder: str | None = None,
    font_size: float = DEFAULT_FONT_SIZE_MM,
    font_thickness: float = DEFAULT_FONT_THICKNESS_MM,
    dry_run: bool = False,
    output_path: Path | None = None,
    output_format: str = "text",
) -> int:
    """Add board-info silkscreen text.  Returns 0 on success, 1 on error."""
    try:
        result = _run(
            pcb_path,
            text,
            layer,
            at,
            auto,
            order_placeholder,
            font_size,
            font_thickness,
            dry_run,
            output_path,
        )
    except BoardInfoError as e:
        _print_error(str(e), output_format)
        return 1
    except Exception as e:  # load/save failures
        _print_error(f"{type(e).__name__}: {e}", output_format)
        return 1

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"PCB Add Board Info{' (dry run)' if dry_run else ''}")
        print(f"  PCB: {pcb_path}")
        for item in result["items"]:
            print(f"  {item['layer']}: {item['text']!r} at ({item['x']:.3f}, {item['y']:.3f})")
        for w in result.get("warnings", []):
            print(f"  Warning: {w}", file=sys.stderr)
        if result["written"]:
            print(f"  Saved to: {result['output']}")
    return 0


def _run(
    pcb_path: Path,
    template: str,
    layer: str,
    at: tuple[float, float] | None,
    auto: bool,
    order_placeholder: str | None,
    font_size: float,
    font_thickness: float,
    dry_run: bool,
    output_path: Path | None,
) -> dict[str, Any]:
    from kicad_tools._shapely import require_shapely
    from kicad_tools.core.sexp_file import save_pcb
    from kicad_tools.sexp.parser import parse_file
    from kicad_tools.silkscreen._silk_defaults import (
        DEFAULT_CLEARANCE_MM,
        SILK_EDGE_CLEARANCE_MM,
    )
    from kicad_tools.silkscreen.place_refs import _SilkscreenPCB
    from kicad_tools.validate.rules.silkscreen import _text_bbox_geometry

    if layer not in _VALID_LAYERS:
        raise BoardInfoError(f"--layer must be one of {', '.join(_VALID_LAYERS)}")
    if auto and at is not None:
        raise BoardInfoError("--at and --auto are mutually exclusive")
    if not auto and at is None:
        raise BoardInfoError("either --at X Y or --auto is required")
    for name, value in (("--font-size", font_size), ("--font-thickness", font_thickness)):
        if not math.isfinite(value) or value <= 0:
            raise BoardInfoError(f"{name} must be positive")

    pcb = _SilkscreenPCB(parse_file(pcb_path), pcb_path)
    text = resolve_fields(template, pcb, pcb_path)
    if not text.strip():
        raise BoardInfoError("resolved text is empty")
    texts = [text]
    if order_placeholder:
        texts.append(order_placeholder)

    # Idempotence: drop any earlier stamp with the same text on this layer, then
    # rebuild the PCB view so the old stamp is neither duplicated nor an obstacle.
    doc = parse_file(pcb_path)
    for node in list(doc.find_all("gr_text")):
        node_layer = node.find("layer")
        if (
            node.get_string(0) in texts
            and node_layer is not None
            and node_layer.get_string(0) == layer
        ):
            doc.remove(node)
    pcb = _SilkscreenPCB(doc, pcb_path)

    ox, oy = pcb._board_origin  # schema coords are board-relative; file coords add this
    side = "F" if layer == "F.SilkS" else "B"
    line_pitch = font_size + 0.5
    placements: list[tuple[str, float, float]] = []  # file coordinates
    warnings: list[str] = []

    if auto:
        require_shapely("board-info placement")
        obstacles = _obstacles(pcb, side, 0.05)
        placed_geoms: list = []
        for t in texts:
            spot = _find_free_spot(
                t,
                font_size,
                font_thickness,
                obstacles,
                placed_geoms,
                DEFAULT_CLEARANCE_MM,
                SILK_EDGE_CLEARANCE_MM,
            )
            if spot is None:
                raise BoardInfoError(
                    f"no free silkscreen region on {layer} fits {t!r} "
                    f"(clear of pads, silk, courtyards and the board edge)"
                )
            geom = _text_bbox_geometry(t, (font_size, font_size), font_thickness, spot)
            placed_geoms.append((geom, "board-info"))
            placements.append((t, spot[0] + ox, spot[1] + oy))
    else:
        assert at is not None
        for i, t in enumerate(texts):
            placements.append((t, at[0], at[1] + i * line_pitch))
        warnings.extend(_check_explicit(pcb, placements, side, layer, font_size, font_thickness))

    result: dict[str, Any] = {
        "pcb": str(pcb_path),
        "dry_run": dry_run,
        "items": [{"text": t, "layer": layer, "x": x, "y": y} for t, x, y in placements],
        "written": False,
        "warnings": warnings,
    }
    if not dry_run:
        for t, x, y in placements:
            doc.append(_build_node(t, x, y, layer, font_size, font_thickness))
        save_path = output_path or pcb_path
        save_pcb(doc, save_path)
        result["written"] = True
        result["output"] = str(save_path)
    return result


def _check_explicit(pcb, placements, side, layer, size, thickness) -> list[str]:
    """Warn when an explicit ``--at`` stamp is not clear of pads/silk/courtyards/edge."""
    try:
        from kicad_tools._shapely import require_shapely

        require_shapely("board-info clearance check")
        from kicad_tools.silkscreen._silk_defaults import (
            DEFAULT_CLEARANCE_MM,
            SILK_EDGE_CLEARANCE_MM,
        )
        from kicad_tools.silkscreen.place_refs import _candidate_clear
        from kicad_tools.validate.rules.silkscreen import _text_bbox_geometry

        apertures, silk, courtyards, material, outline = _obstacles(pcb, side, 0.05)
    except Exception:  # check is best-effort; never block an explicit placement
        return []
    ox, oy = pcb._board_origin
    out: list[str] = []
    placed: list = []
    for t, x, y in placements:
        geom = _text_bbox_geometry(t, (size, size), thickness, (x - ox, y - oy))
        if geom is None:
            continue
        ok, why = _candidate_clear(
            geom,
            apertures=apertures,
            silk_obstacles=silk + placed,
            courtyards=courtyards,
            outline=outline,
            board_material=material,
            outline_error="",
            clearance_mm=DEFAULT_CLEARANCE_MM,
            edge_clearance_mm=SILK_EDGE_CLEARANCE_MM,
        )
        placed.append((geom, "board-info"))
        if not ok:
            out.append(f"{t!r} at ({x:.3f}, {y:.3f}) on {layer} is not clear: {why}")
    return out


def _print_error(message: str, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps({"error": message}, indent=2))
    else:
        print(f"Error: {message}", file=sys.stderr)
