"""Silkscreen validation rules.

This module implements DRC checks for silkscreen elements:
- Minimum line width
- Minimum text height
- Silkscreen-over-pad detection (legacy centroid heuristic)
- Silkscreen-over-copper detection (geometric, vs pad mask apertures)
- Silkscreen-over-silkscreen detection (geometric, silk vs silk)
- Silkscreen-to-edge clearance (geometric, vs Edge.Cuts outline)

**Cross-gate counting model.**  The three geometric checks are designed to be
count-comparable with ``kicad-cli pcb drc``, which is used as kct's referee at
manufacturing sign-off.  Each emits **one violation per colliding pair**:
``silk_over_copper`` per (silk item, mask aperture), ``silk_overlap`` per
unordered (silk item, silk item), ``silk_edge_clearance`` per (silk item,
outline) -- the outline being a single object, so that one is per silk item.
A count mismatch against kicad-cli is therefore a bug to investigate, not a
convention difference to explain away.  See #4612 for the measured parity table.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely

from ..violations import DRCResults, DRCViolation

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import (
        PCB,
        BoardGraphic,
        Footprint,
        FootprintGraphic,
        Pad,
        Setup,
        Via,
    )

    from ...manufacturers import DesignRules

# A shapely geometry.  shapely is dynamically imported (core dependency, see
# ``_shapely``) and has no first-class stubs in this project, so geometries are
# typed as ``Any`` -- matching the convention in ``clearance.py``.
_Geometry = Any
_Transform = Callable[[tuple[float, float]], tuple[float, float]]

# Silkscreen layer names
SILKSCREEN_LAYERS = ("F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen")

# Front/back silkscreen layer groupings, used to pair silk geometry with the
# pad copper apertures on the matching side.
FRONT_SILK_LAYERS = ("F.SilkS", "F.Silkscreen")
BACK_SILK_LAYERS = ("B.SilkS", "B.Silkscreen")

# Minimum silk-to-board-edge clearance (mm).  There is no dedicated profile
# field for silk-to-edge; this named constant keeps the intent explicit and
# distinct from the (stricter) copper-to-edge rule.  0.2 mm matches common
# fab silk-to-edge specs and kicad-cli's default ``silk_edge_clearance``.
SILK_EDGE_CLEARANCE_MM = 0.2

# Per-character width factor used to approximate a stroked-font text bounding
# box.  KiCad's true glyph metrics are not modeled; a simple per-character box
# is sufficient to reproduce the kicad-cli silk findings.
_TEXT_CHAR_WIDTH_FACTOR = 0.7

# Floating-point tolerance for clearance comparisons (0.1 micron), matching
# ``edge.py``'s ``_CLEARANCE_EPSILON_MM``.
_CLEARANCE_EPSILON_MM = 1e-4

# Minimum silk/aperture overlap area (mm^2) before ``silk_over_copper`` fires.
# The text bounding box is an axis-aligned over-approximation of the true
# stroked glyph outline, so a glyph whose real ink clears a pad can still
# produce a hairline (corner-only) AABB intersection.  Requiring a small but
# non-trivial overlap area suppresses those approximation artifacts while still
# catching genuine silk-over-copper (which overlaps by far more than this).
# Calibrated against boards 00-07 vs kicad-cli: genuine silk-over-copper
# findings overlap by >= 0.089 mm^2, whereas the stm32 U2 corner artifact
# overlaps by exactly 0.01 mm^2; a 0.05 mm^2 gate separates them with wide
# margin and drops no real finding.
_MIN_OVERLAP_AREA_MM2 = 0.05

# Minimum silk/silk overlap area (mm^2) before ``silk_overlap`` fires.
# A SIBLING of ``_MIN_OVERLAP_AREA_MM2`` rather than a reuse of it, because the
# calibration genuinely differs: silk-over-copper always pairs a silk element
# with a comparatively large pad aperture, whereas silk-over-silk routinely
# pairs two thin strokes.  Two minimum-width (0.15 mm) silk lines crossing at
# right angles overlap by only 0.0225 mm^2 -- well under the 0.05 mm^2 copper
# gate -- so reusing that constant would silently drop the single most obvious
# silk_overlap case.  0.005 mm^2 keeps right-angle stroke crossings (4.5x
# margin) while still rejecting numerically-degenerate hairline touches between
# two axis-aligned text bounding boxes.
_MIN_SILK_OVERLAP_AREA_MM2 = 0.005


def is_silkscreen_layer(layer: str) -> bool:
    """Check if a layer is a silkscreen layer."""
    return layer in SILKSCREEN_LAYERS


def is_library_footprint(footprint: Footprint) -> bool:
    """Check if a footprint comes from a standard KiCad library.

    A footprint is considered a library footprint if its ``name`` field
    contains a colon separator (e.g., ``Capacitor_SMD:C_0402_1005Metric``).
    Footprints placed from custom libraries or edited in-place typically
    lose this prefix.

    Args:
        footprint: The footprint to check.

    Returns:
        True if the footprint appears to originate from a KiCad library.
    """
    return ":" in footprint.name


def check_silkscreen_line_width(
    pcb: PCB,
    design_rules: DesignRules,
    *,
    suppress_library: bool = False,
) -> DRCResults:
    """Check silkscreen line width against minimum.

    Checks both:
    - Board-level graphics (gr_line, gr_rect, etc.) on silkscreen layers
    - Footprint graphics (fp_line, fp_rect, etc.) on silkscreen layers

    Args:
        pcb: The PCB to check
        design_rules: Design rules with min_silkscreen_width_mm
        suppress_library: If True, suppress warnings for footprints that
            originate from standard KiCad libraries (name contains ``:``)

    Returns:
        DRCResults containing any violations
    """
    results = DRCResults(rules_checked=1)
    min_width = design_rules.min_silkscreen_width_mm

    # Check board-level graphics (never suppressed -- no footprint context)
    for graphic in pcb.graphics:
        if not is_silkscreen_layer(graphic.layer):
            continue
        if graphic.stroke_width < min_width and graphic.stroke_width > 0:
            results.add(
                DRCViolation(
                    rule_id="silkscreen_line_width",
                    severity="warning",
                    message=(
                        f"Silkscreen line width {graphic.stroke_width:.2f}mm "
                        f"< minimum {min_width:.2f}mm"
                    ),
                    location=graphic.start,
                    layer=graphic.layer,
                    actual_value=graphic.stroke_width,
                    required_value=min_width,
                    items=(f"gr_{graphic.graphic_type}",),
                )
            )

    # Check footprint graphics
    for footprint in pcb.footprints:
        if suppress_library and is_library_footprint(footprint):
            # Count suppressed violations for this footprint instead of reporting
            for graphic in footprint.graphics:
                if not is_silkscreen_layer(graphic.layer):
                    continue
                if graphic.stroke_width < min_width and graphic.stroke_width > 0:
                    results.suppressed_count += 1
            continue

        for graphic in footprint.graphics:
            if not is_silkscreen_layer(graphic.layer):
                continue
            if graphic.stroke_width < min_width and graphic.stroke_width > 0:
                results.add(
                    DRCViolation(
                        rule_id="silkscreen_line_width",
                        severity="warning",
                        message=(
                            f"Silkscreen line width {graphic.stroke_width:.2f}mm "
                            f"< minimum {min_width:.2f}mm on {footprint.reference}"
                        ),
                        location=footprint.position,
                        layer=graphic.layer,
                        actual_value=graphic.stroke_width,
                        required_value=min_width,
                        items=(footprint.reference, f"fp_{graphic.graphic_type}"),
                    )
                )

    return results


def check_silkscreen_text_height(
    pcb: PCB,
    design_rules: DesignRules,
    *,
    suppress_library: bool = False,
) -> DRCResults:
    """Check silkscreen text height against minimum.

    Checks both:
    - Board-level text (gr_text) on silkscreen layers
    - Footprint text (fp_text) on silkscreen layers

    Args:
        pcb: The PCB to check
        design_rules: Design rules with min_silkscreen_height_mm
        suppress_library: If True, suppress warnings for footprints that
            originate from standard KiCad libraries (name contains ``:``)

    Returns:
        DRCResults containing any violations
    """
    results = DRCResults(rules_checked=1)
    min_height = design_rules.min_silkscreen_height_mm

    # Check board-level text (never suppressed -- no footprint context)
    for text in pcb.texts:
        if not is_silkscreen_layer(text.layer):
            continue
        if text.hidden:
            continue
        if text.font_height < min_height:
            results.add(
                DRCViolation(
                    rule_id="silkscreen_text_height",
                    severity="warning",
                    message=(
                        f"Silkscreen text height {text.font_height:.2f}mm "
                        f"< minimum {min_height:.2f}mm"
                    ),
                    location=text.position,
                    layer=text.layer,
                    actual_value=text.font_height,
                    required_value=min_height,
                    items=(text.text[:20] if text.text else "gr_text",),
                )
            )

    # Check footprint text
    for footprint in pcb.footprints:
        if suppress_library and is_library_footprint(footprint):
            # Count suppressed violations for this footprint
            for fp_text in footprint.texts:
                if not is_silkscreen_layer(fp_text.layer):
                    continue
                if fp_text.hidden:
                    continue
                if fp_text.font_height < min_height:
                    results.suppressed_count += 1
            continue

        for fp_text in footprint.texts:
            if not is_silkscreen_layer(fp_text.layer):
                continue
            if fp_text.hidden:
                continue
            if fp_text.font_height < min_height:
                # Build descriptive item name
                if fp_text.text_type == "reference":
                    item_name = f"{footprint.reference} (reference)"
                elif fp_text.text_type == "value":
                    item_name = f"{footprint.reference} (value)"
                else:
                    item_name = f"{footprint.reference} ({fp_text.text_type})"

                results.add(
                    DRCViolation(
                        rule_id="silkscreen_text_height",
                        severity="warning",
                        message=(
                            f"Silkscreen text height {fp_text.font_height:.2f}mm "
                            f"< minimum {min_height:.2f}mm on {footprint.reference}"
                        ),
                        location=footprint.position,
                        layer=fp_text.layer,
                        actual_value=fp_text.font_height,
                        required_value=min_height,
                        items=(item_name,),
                    )
                )

    return results


def check_silkscreen_over_pads(
    pcb: PCB,
    design_rules: DesignRules,
) -> DRCResults:
    """Check for silkscreen elements overlapping exposed pads.

    This is a simplified check that warns when silkscreen elements
    exist on the same layer as SMD pads in the footprint. A full
    geometric overlap check would require more complex calculations.

    For now, this checks footprint text that might overlap pads by
    checking if the text position is within the pad area.

    Args:
        pcb: The PCB to check
        design_rules: Design rules (not currently used for this check)

    Returns:
        DRCResults containing any warnings
    """
    results = DRCResults(rules_checked=1)

    for footprint in pcb.footprints:
        # Get exposed pads (SMD pads are always exposed)
        exposed_pads = [pad for pad in footprint.pads if pad.type == "smd"]

        if not exposed_pads:
            continue

        # Determine silkscreen layer for this footprint side
        if footprint.layer == "F.Cu":
            silk_layer = ("F.SilkS", "F.Silkscreen")
        else:
            silk_layer = ("B.SilkS", "B.Silkscreen")

        # Check footprint text elements
        for fp_text in footprint.texts:
            if fp_text.layer not in silk_layer:
                continue
            if fp_text.hidden:
                continue

            # Simple overlap check: see if text center is close to any pad
            # This is a simplified heuristic - full overlap detection would
            # require computing bounding boxes and intersections
            for pad in exposed_pads:
                # Calculate distance from text to pad center
                dx = fp_text.position[0] - pad.position[0]
                dy = fp_text.position[1] - pad.position[1]

                # Text is considered "over" pad if within half the pad size
                pad_half_width = pad.size[0] / 2
                pad_half_height = pad.size[1] / 2

                if abs(dx) < pad_half_width and abs(dy) < pad_half_height:
                    results.add(
                        DRCViolation(
                            rule_id="silkscreen_over_pad",
                            severity="warning",
                            message=(
                                f"Silkscreen text may overlap exposed pad on {footprint.reference}"
                            ),
                            location=footprint.position,
                            layer=fp_text.layer,
                            items=(footprint.reference, f"pad {pad.number}"),
                        )
                    )
                    break  # Only report once per text element

    return results


# ---------------------------------------------------------------------------
# Geometric silk checks (silk_over_copper, silk_edge_clearance)
# ---------------------------------------------------------------------------
#
# Footprint silk text/graphic coordinates are footprint-LOCAL.  To get board
# coordinates we apply the same transform as ``EdgeClearanceRule._check_pads``
# (``edge.py:193-207``): rotate by ``radians(-footprint.rotation)`` (KiCad
# negates the orientation angle vs CCW math, verified in #3739) then translate
# by ``footprint.position``.  Board-level text/graphics are already
# board-relative -- no transform needed.


def _silk_side(layer: str) -> str | None:
    """Return ``"F"`` / ``"B"`` for a silk layer, else ``None``."""
    if layer in FRONT_SILK_LAYERS:
        return "F"
    if layer in BACK_SILK_LAYERS:
        return "B"
    return None


def _fp_transform(footprint: Footprint) -> _Transform:
    """Return a ``(x, y) -> (X, Y)`` local->board transform for a footprint."""
    fp_x, fp_y = footprint.position
    fp_rotation = math.radians(-footprint.rotation)
    cos_rot = math.cos(fp_rotation)
    sin_rot = math.sin(fp_rotation)

    def transform(point: tuple[float, float]) -> tuple[float, float]:
        lx, ly = point
        return (
            fp_x + (lx * cos_rot - ly * sin_rot),
            fp_y + (lx * sin_rot + ly * cos_rot),
        )

    return transform


def _text_bbox_geometry(
    text: str,
    font_size: tuple[float, float],
    font_thickness: float,
    center: tuple[float, float],
) -> _Geometry | None:
    """Build a shapely box approximating a stroked-text bounding box.

    The box is centered on ``center`` (board coordinates).  Rotation /
    justification are modeled only to first order (axis-aligned box), which is
    sufficient to reproduce the kicad-cli silk findings.  Returns ``None`` for
    empty text.
    """
    from shapely.geometry import box  # type: ignore[import-untyped]

    n = len(text)
    if n == 0:
        return None
    width = font_size[0] * n * _TEXT_CHAR_WIDTH_FACTOR + font_thickness
    height = font_size[1] + font_thickness
    cx, cy = center
    return box(
        cx - width / 2.0,
        cy - height / 2.0,
        cx + width / 2.0,
        cy + height / 2.0,
    )


def _stroke_geometry(
    graphic: FootprintGraphic | BoardGraphic,
    transform: _Transform | None,
) -> _Geometry | None:
    """Build a shapely polygon for a silk line/rect graphic stroke.

    Returns ``None`` for graphic types we do not model (circle/arc) or zero
    stroke width.
    """
    from shapely.geometry import LineString

    if graphic.graphic_type not in ("line", "rect"):
        return None
    width = graphic.stroke_width
    if width <= 0:
        return None

    def xf(p: tuple[float, float]) -> tuple[float, float]:
        return transform(p) if transform is not None else p

    if graphic.graphic_type == "line":
        coords = [xf(graphic.start), xf(graphic.end)]
        if coords[0] == coords[1]:
            return None
        return LineString(coords).buffer(width / 2.0)

    # rect: expand to its 4 boundary segments (in local space), transform, then
    # buffer the closed ring.
    sx, sy = graphic.start
    ex, ey = graphic.end
    ring = [(sx, sy), (ex, sy), (ex, ey), (sx, ey), (sx, sy)]
    ring = [xf(p) for p in ring]
    return LineString(ring).buffer(width / 2.0)


def _pad_aperture_geometry(
    pad: Pad,
    pad_pos: tuple[float, float],
    min_mask_clearance: float,
) -> _Geometry:
    """Build a shapely polygon for a pad's solder-mask aperture (exposed copper).

    Aperture = pad copper expanded by the mask margin (``pad.solder_mask_margin``
    if set, else the profile default).  Approximated as an axis-aligned box;
    pad rotation is not modeled (first-order, matches the text-box
    approximation).
    """
    from shapely.geometry import box

    margin = pad.solder_mask_margin if pad.solder_mask_margin is not None else min_mask_clearance
    w = pad.size[0] + 2.0 * margin
    h = pad.size[1] + 2.0 * margin
    cx, cy = pad_pos
    return box(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def _via_aperture_geometry(via: Via, min_mask_clearance: float) -> _Geometry:
    """Build a shapely polygon for an untented via's solder-mask opening.

    Aperture = via annular copper (diameter ``via.size``) expanded by the
    profile's mask margin.  Approximated as an axis-aligned box to
    first-order-match :func:`_pad_aperture_geometry`'s convention (a circle
    inscribes the box, so this slightly over-approximates the corners --
    consistent with the pad/text AABB model used throughout this module).
    """
    from shapely.geometry import box

    d = via.size + 2.0 * min_mask_clearance
    cx, cy = via.position
    return box(cx - d / 2.0, cy - d / 2.0, cx + d / 2.0, cy + d / 2.0)


def _via_is_tented(via: Via, setup: Setup | None, side: str) -> bool:
    """Resolve whether ``via`` is tented (mask-covered) on ``side`` (``F``/``B``).

    Resolution order per side, matching KiCad's model (each step measured
    behaviorally against ``kicad-cli pcb drc --severity-all``, KiCad 10.0.5,
    2026-08-05, on synthetic probe boards -- see #4624):

    1. **Per-via override**: a ``(tenting (front yes|no|none) (back ...))``
       node on the via.  ``yes`` = tented, ``no`` = not tented, ``none`` (or
       the side absent from the node) = fall through to the board default.
       Measured: a ``(front no)`` via on a board-tented default IS reported
       by kicad-cli, and a ``(front yes)`` via on a board-untented default is
       NOT -- the override beats the setup default in both directions, and
       sides resolve independently.
    2. **Board default**: the setup-level ``(tenting (front yes|no) (back
       yes|no))`` node.
    3. **Absent-token default: tented.**  Measured: a board whose setup block
       has no ``(tenting ...)`` node at all produces ZERO
       ``silk_over_copper`` findings for silk crossing a via (KiCad 10.0.5),
       and a fresh pcbnew board writes ``(front yes) (back yes)`` -- so an
       unspecified board tents its vias.
    """
    override = via.tenting_front if side == "F" else via.tenting_back
    if override == "yes":
        return True
    if override == "no":
        return False
    # "none" or absent: inherit the board default.
    if setup is not None:
        default = setup.tenting_front if side == "F" else setup.tenting_back
        if default is not None:
            return default
    # Measured absent-token default (KiCad 10.0.5): tented.
    return True


def _iter_via_apertures(
    pcb: PCB, min_mask_clearance: float
) -> Iterator[tuple[str, _Geometry, str]]:
    """Yield ``(side, geom, label)`` for every *untented* via mask opening.

    Sibling of :func:`_iter_pad_apertures` (#4624): a via whose resolved
    tenting on a side is "not tented" exposes copper there exactly like a pad
    aperture, and kicad-cli reports silk crossing it as ``silk_over_copper``
    (measured -- see the deliberate-gap note that used to live on
    :func:`_iter_pad_apertures`, now closed by this function).  Tented sides
    yield nothing, so fully-tented fleets (every committed board sets
    ``(tenting (front yes) (back yes))``) produce zero via apertures and the
    #4612 pad-parity result is preserved.

    Tenting is resolved per side via :func:`_via_is_tented` (per-via override
    -> board setup default -> measured absent-token default).
    """
    setup = pcb.setup
    for via in pcb.vias:
        if via.size <= 0:
            continue
        geom: _Geometry | None = None
        for side in ("F", "B"):
            if _via_is_tented(via, setup, side):
                continue
            if geom is None:
                geom = _via_aperture_geometry(via, min_mask_clearance)
            x, y = via.position
            net = f" [{via.net_name}]" if via.net_name else ""
            label = f"via{net} at ({x:.3f}, {y:.3f})"
            yield side, geom, label


def _iter_silk_geometries(
    pcb: PCB,
) -> Iterator[tuple[str, _Geometry, str, tuple[float, float], str]]:
    """Yield ``(side, geom, label, location, layer)`` for every silk element.

    ``side`` is ``"F"`` or ``"B"``.  ``geom`` is a shapely geometry in board
    coordinates.  Covers footprint texts/graphics (transformed from local) and
    board-level gr_text/gr_graphics (already board-relative).  Hidden text is
    skipped; zero-length text and unmodeled graphics are skipped.
    """
    # Footprint silk
    for footprint in pcb.footprints:
        transform = _fp_transform(footprint)

        for fp_text in footprint.texts:
            side = _silk_side(fp_text.layer)
            if side is None or fp_text.hidden:
                continue
            center = transform(fp_text.position)
            geom = _text_bbox_geometry(
                fp_text.text, fp_text.font_size, fp_text.font_thickness, center
            )
            if geom is None:
                continue
            label = f"{footprint.reference} ({fp_text.text_type})"
            yield side, geom, label, center, fp_text.layer

        for graphic in footprint.graphics:
            side = _silk_side(graphic.layer)
            if side is None:
                continue
            geom = _stroke_geometry(graphic, transform)
            if geom is None:
                continue
            label = f"{footprint.reference} (fp_{graphic.graphic_type})"
            yield side, geom, label, footprint.position, graphic.layer

    # Board-level silk (already board-relative)
    for text in pcb.texts:
        side = _silk_side(text.layer)
        if side is None or text.hidden:
            continue
        geom = _text_bbox_geometry(text.text, text.font_size, text.font_thickness, text.position)
        if geom is None:
            continue
        label = text.text[:20] if text.text else "gr_text"
        yield side, geom, label, text.position, text.layer

    for board_graphic in pcb.graphics:
        side = _silk_side(board_graphic.layer)
        if side is None:
            continue
        geom = _stroke_geometry(board_graphic, None)
        if geom is None:
            continue
        label = f"gr_{board_graphic.graphic_type}"
        yield side, geom, label, board_graphic.start, board_graphic.layer


def _iter_pad_apertures(
    pcb: PCB, min_mask_clearance: float
) -> Iterator[tuple[str, _Geometry, str]]:
    """Yield ``(side, geom, label)`` for every exposed pad mask aperture.

    SMD pads expose copper on their own side; thru_hole pads expose copper on
    BOTH sides (so they are yielded for both ``"F"`` and ``"B"``).  ``side`` is
    the silk side the aperture must be checked against.

    This function covers **pads only**; *untented via* mask openings -- the
    one residue #4612 measured and deferred -- are yielded by the sibling
    :func:`_iter_via_apertures` (#4624), which resolves per-via tenting
    (per-via override -> board setup default -> measured absent-token
    default).  ``check_silk_over_copper`` chains both iterators into a single
    aperture index.
    """
    for footprint in pcb.footprints:
        transform = _fp_transform(footprint)
        for pad in footprint.pads:
            sides: tuple[str, ...]
            if pad.type == "smd":
                pad_side = "F" if footprint.layer == "F.Cu" else "B"
                sides = (pad_side,)
            elif pad.type == "thru_hole":
                sides = ("F", "B")
            else:
                # NPTH / connect / other: no exposed copper to smear silk onto.
                continue
            pad_pos = transform(pad.position)
            geom = _pad_aperture_geometry(pad, pad_pos, min_mask_clearance)
            label = f"{footprint.reference} pad {pad.number}"
            for side in sides:
                yield side, geom, label


def check_silk_over_copper(
    pcb: PCB,
    design_rules: DesignRules,
) -> DRCResults:
    """Geometric check: silk text/graphics over exposed copper.

    Builds a shapely geometry per silk element (text bbox or buffered graphic
    stroke) and tests it against the solder-mask apertures of all pads and
    untented vias on the matching silk side.  Emits a ``silk_over_copper``
    **warning** (matching kicad-cli severity) for any silk/aperture
    intersection.

    **Counting model: one violation per (silk item, mask aperture) pair,
    matching ``kicad-cli pcb drc``.**  A refdes straddling four pads yields four
    violations, not one -- so a kct count and a kicad-cli count are directly
    comparable, and every colliding pad is named rather than an arbitrary
    representative.  Before #4612 this de-duplicated to one violation per silk
    element, which under-reported by ~2x and named an R-tree-ordered arbitrary
    member of the collision set; a count mismatch against kicad-cli is now a
    bug, not a convention difference.  Violations are sorted by
    ``(silk label, pad label)`` because ``STRtree.query`` order is unspecified.

    The ``_MIN_OVERLAP_AREA_MM2`` gate is applied **per pair** (each silk/pad
    intersection must clear it independently), which is the reading the per-pair
    counting model implies.

    This supersedes the crude centroid heuristic in
    :func:`check_silkscreen_over_pads` (which is retained for backward
    compatibility with the ``silkscreen_over_pad`` rule_id), and unlike that
    heuristic it accounts for real text bounding boxes, graphic strokes,
    board-level silk, thru-hole apertures, and silk-over-other-footprint pads.

    The aperture set covers **pads and untented vias**: pads via
    :func:`_iter_pad_apertures`, untented-via mask openings via
    :func:`_iter_via_apertures` (#4624 closed the deliberate pads-only gap
    left by #4612).  Tented vias contribute no aperture, so fully-tented
    boards -- the entire committed fleet -- behave exactly as before.

    Args:
        pcb: The PCB to check.
        design_rules: Design rules providing the mask-clearance fallback.

    Returns:
        DRCResults containing any ``silk_over_copper`` warnings.
    """
    require_shapely("silk-over-copper geometry")
    from shapely import STRtree  # type: ignore[import-untyped]

    results = DRCResults(rules_checked=1)

    min_mask = design_rules.min_solder_mask_clearance_mm

    # Group apertures by side and index with an STRtree for fast lookup.
    # Pads and untented vias contribute to the same index (#4624).
    apertures: dict[str, list[tuple[_Geometry, str]]] = {"F": [], "B": []}
    for side, geom, label in _iter_pad_apertures(pcb, min_mask):
        apertures[side].append((geom, label))
    for side, geom, label in _iter_via_apertures(pcb, min_mask):
        apertures[side].append((geom, label))

    trees: dict[str, Any] = {}
    for side, entries in apertures.items():
        if entries:
            trees[side] = STRtree([g for g, _ in entries])

    found: list[DRCViolation] = []
    for side, geom, silk_label, location, layer in _iter_silk_geometries(pcb):
        side_entries = apertures.get(side)
        if not side_entries:
            continue
        tree = trees[side]
        # STRtree.query returns candidate indices whose envelopes overlap.
        for idx in tree.query(geom):
            ap_geom, aperture_label = side_entries[int(idx)]
            if not geom.intersects(ap_geom):
                continue
            # Require a non-trivial overlap area to reject hairline corner
            # touches that the AABB text approximation can manufacture.  Applied
            # per (silk, aperture) pair -- see the counting model above.
            if geom.intersection(ap_geom).area < _MIN_OVERLAP_AREA_MM2:
                continue
            # One violation per (silk item, mask aperture) PAIR, matching
            # kicad-cli.  Do NOT re-introduce a break here: it collapses a
            # multi-pad collision to a single arbitrary representative (#4612).
            found.append(
                DRCViolation(
                    rule_id="silk_over_copper",
                    severity="warning",
                    message=(
                        f"Silkscreen {silk_label} overlaps exposed copper of {aperture_label}"
                    ),
                    location=location,
                    layer=layer,
                    items=(silk_label, aperture_label),
                )
            )

    # STRtree.query order is unspecified; sort for deterministic reporting.
    for violation in sorted(found, key=lambda v: (v.items[0], v.items[1])):
        results.add(violation)

    return results


def check_silk_overlap(
    pcb: PCB,
    design_rules: DesignRules,
) -> DRCResults:
    """Geometric check: silkscreen printed on top of other silkscreen.

    Silk-over-silk renders both elements illegible (the refdes you need at
    assembly and rework time is the usual casualty).  kicad-cli reports this as
    ``silk_overlap``; before #4612 kct had no producer for it at all, so an
    entire kicad-cli rule class was invisible to a kct-only workflow even though
    :data:`~kicad_tools.drc.violation.ViolationType.SILK_OVERLAP` and its fix
    suggestions were already wired up downstream.

    **Counting model: one violation per unordered (silk item, silk item) pair,
    matching ``kicad-cli pcb drc``.**  A silk element never pairs with itself,
    and each colliding pair is emitted exactly once (never as both ``(A, B)``
    and ``(B, A)``).  Front and back silk are indexed separately, so F-side silk
    can never pair with B-side silk.

    **Same-footprint pairs DO count.**  A reference designator overlapping its
    own footprint's courtyard/outline art is a real, reportable finding, not a
    self-collision to filter out.  kicad-cli is the tiebreaker here and it was
    measured directly (2026-08-04, KiCad 10.0.5) on the synthetic probe fixture
    that mirrors ``tests/test_validate_silkscreen.py``'s
    ``test_same_footprint_refdes_over_own_graphic_flags``::

        silk_overlap | warning | Reference field of A1 <-> Segment of A1 on F.Silkscreen
        silk_overlap | warning | Reference field of B1 <-> Reference field of C1

    Both the same-footprint pair and the cross-footprint pair fire, so this
    function excludes neither.

    **Detection is bare intersection, not clearance.**  KiCad's silk-to-silk
    test enforces a configurable *clearance* (silk must be some distance apart);
    this check requires the geometries to actually overlap.  That is the
    deliberately conservative choice for an advisory rule -- it under-reports
    rather than over-reports relative to kicad-cli on boards that configure a
    non-zero silk clearance, and it needs no new profile field.

    Args:
        pcb: The PCB to check.
        design_rules: Design rules (unused; detection is bare geometric
            intersection, gated by :data:`_MIN_SILK_OVERLAP_AREA_MM2`).

    Returns:
        DRCResults containing any ``silk_overlap`` warnings.
    """
    del design_rules  # bare intersection; no profile field participates
    require_shapely("silk-overlap geometry")
    from shapely import STRtree

    results = DRCResults(rules_checked=1)

    # Reuse the single silk traversal shared with silk_over_copper /
    # silk_edge_clearance; partition by side so F silk never pairs with B silk.
    by_side: dict[str, list[tuple[_Geometry, str, tuple[float, float], str]]] = {"F": [], "B": []}
    for side, geom, label, location, layer in _iter_silk_geometries(pcb):
        by_side[side].append((geom, label, location, layer))

    found: list[DRCViolation] = []
    for entries in by_side.values():
        if len(entries) < 2:
            continue
        tree = STRtree([g for g, _, _, _ in entries])
        for i, (geom, label_a, location, layer) in enumerate(entries):
            for raw_idx in tree.query(geom):
                j = int(raw_idx)
                # Skip self-pairing, and emit each unordered pair once by
                # keeping only j > i.  Compared by INDEX, not geometry: two
                # identical refdes boxes on different parts are distinct
                # elements and must still pair with each other.
                if j <= i:
                    continue
                other_geom, label_b, _, _ = entries[j]
                if not geom.intersects(other_geom):
                    continue
                if geom.intersection(other_geom).area < _MIN_SILK_OVERLAP_AREA_MM2:
                    continue
                found.append(
                    DRCViolation(
                        rule_id="silk_overlap",
                        severity="warning",
                        message=(f"Silkscreen {label_a} overlaps silkscreen {label_b}"),
                        location=location,
                        layer=layer,
                        items=(label_a, label_b),
                    )
                )

    # STRtree.query order is unspecified; sort for deterministic reporting.
    for violation in sorted(found, key=lambda v: (v.items[0], v.items[1])):
        results.add(violation)

    return results


def check_silk_edge_clearance(
    pcb: PCB,
    design_rules: DesignRules,
) -> DRCResults:
    """Geometric check: silk text/graphics too close to the board edge.

    For each silk element, computes the minimum distance from its geometry to
    the ``Edge.Cuts`` outline.  Emits a ``silk_edge_clearance`` **warning** if
    the silk crosses the outline or sits within :data:`SILK_EDGE_CLEARANCE_MM`
    of it.  No-op when the board has no outline (mirrors
    ``EdgeClearanceRule.check``).

    **Counting model: one violation per (silk item, outline) pair, matching
    ``kicad-cli pcb drc``.**  The outline is treated as a single object, so this
    is one violation per offending silk item -- unlike ``silk_over_copper``,
    where each colliding aperture counts separately.

    Args:
        pcb: The PCB to check.
        design_rules: Design rules (unused; threshold is the module constant).

    Returns:
        DRCResults containing any ``silk_edge_clearance`` warnings.
    """
    del design_rules  # threshold is the named module constant
    require_shapely("silk-edge-clearance geometry")
    from shapely.geometry import MultiLineString

    results = DRCResults(rules_checked=1)

    outline_segments = pcb.get_board_outline_segments()
    if not outline_segments:
        return results

    outline = MultiLineString([[seg_start, seg_end] for seg_start, seg_end in outline_segments])

    for _side, geom, silk_label, location, layer in _iter_silk_geometries(pcb):
        distance = geom.distance(outline)
        if distance < SILK_EDGE_CLEARANCE_MM - _CLEARANCE_EPSILON_MM:
            results.add(
                DRCViolation(
                    rule_id="silk_edge_clearance",
                    severity="warning",
                    message=(
                        f"Silkscreen {silk_label} to board edge {distance:.3f}mm "
                        f"< minimum {SILK_EDGE_CLEARANCE_MM:.2f}mm"
                    ),
                    location=location,
                    layer=layer,
                    actual_value=distance,
                    required_value=SILK_EDGE_CLEARANCE_MM,
                    items=(silk_label, "Edge.Cuts"),
                )
            )

    return results


def check_all_silkscreen(
    pcb: PCB,
    design_rules: DesignRules,
    *,
    suppress_library: bool = False,
) -> DRCResults:
    """Run all silkscreen checks.

    Args:
        pcb: The PCB to check
        design_rules: Design rules from manufacturer profile
        suppress_library: If True, suppress warnings for footprints that
            originate from standard KiCad libraries (name contains ``:``)

    Returns:
        DRCResults containing all silkscreen violations
    """
    results = DRCResults()

    results.merge(check_silkscreen_line_width(pcb, design_rules, suppress_library=suppress_library))
    results.merge(
        check_silkscreen_text_height(pcb, design_rules, suppress_library=suppress_library)
    )
    results.merge(check_silkscreen_over_pads(pcb, design_rules))
    results.merge(check_silk_over_copper(pcb, design_rules))
    results.merge(check_silk_overlap(pcb, design_rules))
    results.merge(check_silk_edge_clearance(pcb, design_rules))

    return results
