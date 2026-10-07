"""Trim footprint silkscreen lines back to the fabricator's silk-to-pad floor.

Why this exists (Issue #5744)
-----------------------------
Board 03 places three footprints whose library silkscreen runs closer to
their own pads than the reviewed ``jlcpcb-tier1`` floor
(``min_silk_to_pad_clearance_mm = 0.15``) allows:

===============================  ===============================  =========
footprint                         library                          measured
===============================  ===============================  =========
``J3`` pin-header socket          ``Joystick:Samtec_TSM-103-...``  0.1150 mm
``R3`` / ``R4`` 0402 resistors    ``Resistor_SMD:R_0402_1005...``  0.1002 mm
===============================  ===============================  =========

21 ``silk_pad_clearance`` errors in total.  They were invisible for as long
as ``routing_plan.apply_plan()`` deleted every footprint silk graphic on
replay (the #5744 defect): the routed artifact simply carried no silk to
measure.  Restoring the geometry -- which is what #5744 asks for -- makes
them visible in the artifact that ``scripts/ci/check_routed_drc.py`` and the
recipe's own ``run_drc`` gate, so the geometry has to actually be
manufacturable, not merely present.

What it does
------------
The fabricator's own answer to silk near an aperture is to clip the silk, and
that is this pass's first move: each silkscreen ``fp_line`` whose stroke comes
within ``minimum`` of a pad aperture on the same side has its centerline
clipped against those apertures, buffered by ``minimum + stroke_width / 2``.
What survives is written back, as several shorter lines if the clip split it;
remnants shorter than :data:`MIN_SEGMENT_MM` are dropped.

Clipping cannot help a line running *parallel* to the pad it crowds -- the
whole length violates, so the clip consumes it.  Deleting such a line would be
wrong here, because on this board it is J3's **pin-1 marker**: the marker
class whose absence from the routed artifact surfaced #5744 to begin with.  So
a fully-consumed line is instead nudged perpendicular, away from the pad, by
the shortfall (bounded by :data:`MAX_NUDGE_MM`), and only dropped when even
that will not clear.  On board 03 the outcome is 12 lines clipped, 1 nudged
(J3's pin-1 marker, nudged outward; the nudge grows with the 0.15 mm stroke of #5762), 0 dropped -- all 139 footprint silk
graphics survive.

Scope is deliberately narrow:

* ``fp_line`` only.  ``fp_rect``/``fp_circle``/``fp_arc``/``fp_poly`` are left
  alone; board 03's violations are all lines, and clipping a closed primitive
  is a different (and here unneeded) problem.  Anything still violating after
  the pass -- including such a primitive -- lands in
  :pyattr:`SilkPadTrimResult.residual`, which the caller fails loud on.
* Placement stage only.  :func:`repair_board_silk_pad_clearance` is called
  from ``generate_pcb.write_pcb()``, i.e. the moment the unrouted board is
  written, so every entry point that generates this board inherits the repair
  and the routed replay inherits identical silk -- routing must never change
  footprint silk (see ``tests/test_board_silk_graphics_preserved_5744.py``).
* No text.  Reference-designator placement is reviewed plan data replayed by
  ``routing_plan.apply_plan()``; this pass does not touch it.

Geometry is computed with the same primitives the rule itself uses
(``validate.rules.factory_clearance.check_silk_pad_clearance`` ->
``clearance._pad_polygon`` / ``silkscreen._fp_transform``), so the repair and
the check cannot disagree about what a violation is.
"""

from __future__ import annotations

import math
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string, serialize_sexp
from kicad_tools.validate.rules.base import DRC_TOLERANCE

SILK_LAYERS = frozenset({"F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen"})

# A trimmed remnant shorter than this is visually meaningless at fab
# resolution and only invites sliver findings, so it is dropped instead.
MIN_SEGMENT_MM = 0.05

# Clip a hair beyond the floor.  Shapely approximates a buffer's arcs with
# chords, so a clip at exactly ``minimum + width / 2`` can leave an endpoint a
# few nanometres inside the floor and the rule -- which builds its own
# independently-approximated stroke polygon -- then still reports a violation.
# 10 um is two and a half times below the 1 mil (0.0254 mm) fab grid, so it
# costs nothing visually or manufacturably.  ``_BUFFER_QUAD_SEGS`` cuts the
# same approximation error at the source.
SAFETY_MM = 0.01
_BUFFER_QUAD_SEGS = 64

# A line that runs PARALLEL to the pad it crowds cannot be saved by clipping
# -- the whole length violates, so the clip consumes it.  Deleting it is the
# wrong answer when the line is a pin-1 marker (exactly J3's case here, and
# the marker whose absence surfaced #5744 in the first place), so such a line
# is first nudged perpendicular, AWAY from the pad, by the shortfall.  Bounded:
# a nudge larger than this is a footprint-geometry problem, not something a
# board recipe should paper over, so the line is dropped instead.
MAX_NUDGE_MM = 0.2

# Neighbour endpoints this close to a nudged line's old endpoint are treated
# as having been joined to it (covers the clip's own few-um..tens-of-um trim).
JOINT_SNAP_MM = 0.05

# Deterministic UUID namespace for lines this pass has to SPLIT (one input
# line -> several output lines).  A random uuid4 would make an otherwise
# reproducible artifact differ between runs for no reason.
_SPLIT_NAMESPACE = _uuid.UUID("5744c1ea-0000-4000-8000-000000005744")


@dataclass
class SilkPadTrimResult:
    """What a :func:`trim_silk_to_pad_clearance` pass did."""

    minimum_mm: float = 0.0
    lines_examined: int = 0
    lines_trimmed: int = 0
    lines_nudged: int = 0
    lines_removed: int = 0
    lines_added: int = 0
    joints_reattached: int = 0
    trimmed_references: list[str] = field(default_factory=list)
    # ``"<reference> <graphic_type>"`` for each violation still present after
    # the pass (a primitive this pass does not rewrite, or copper/silk
    # geometry no clip can save).  Callers MUST fail loud on a non-empty list
    # rather than shipping the board.
    residual: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.lines_trimmed or self.lines_nudged or self.lines_removed or self.lines_added
        )


def _side_of(layer: str) -> str:
    return "F" if layer.startswith("F.") else "B"


def _pad_apertures_by_side(pcb: PCB) -> dict[str, list[Any]]:
    """Mask apertures (copper + mask margin) per board side.

    Mirrors ``check_silk_pad_clearance``'s aperture construction exactly --
    including the ``pad_to_mask_clearance`` fallback and the unexposed-pad
    (copper-only) case -- so the repair clips against the same shapes the
    rule measures against.
    """
    from kicad_tools.validate.rules.clearance import _pad_polygon

    apertures: dict[str, list[Any]] = {"F": [], "B": []}
    for footprint in pcb.footprints:
        for pad in footprint.pads:
            if pad.type not in ("smd", "thru_hole", "connect"):
                continue
            margin = pad.solder_mask_margin
            if margin is None:
                margin = pcb.setup.pad_to_mask_clearance if pcb.setup is not None else 0
            copper = _pad_polygon(pad, footprint)
            for side in ("F", "B"):
                if "*.Cu" not in pad.layers and f"{side}.Cu" not in pad.layers:
                    continue
                exposed = "*.Mask" in pad.layers or f"{side}.Mask" in pad.layers
                geom = copper.buffer(max(margin or 0, 0)) if exposed else copper
                if not geom.is_empty:
                    apertures[side].append(geom)
    return apertures


def _footprint_frame(footprint: Any) -> tuple[float, float, float, float]:
    """``(x, y, cos, sin)`` of a footprint's local->board transform.

    Same convention (and same board-origin-relative frame) as
    ``silkscreen._fp_transform``, which is what ``clearance._pad_polygon``
    places pads in: KiCad negates the orientation angle relative to CCW math.
    Reading the raw ``(at ...)`` node instead would silently use *page*
    coordinates and miss every violation on a board with a non-zero origin.
    """
    fp_x, fp_y = footprint.position
    rotation = math.radians(-footprint.rotation)
    return fp_x, fp_y, math.cos(rotation), math.sin(rotation)


def _points_of(node: Any, tag: str) -> tuple[float, float]:
    child = node.find_child(tag)
    return (child.get_float(0) or 0.0, child.get_float(1) or 0.0)


def _stroke_width_of(node: Any) -> float:
    stroke = node.find_child("stroke")
    if stroke is not None:
        width = stroke.find_child("width")
        if width is not None:
            return width.get_float(0) or 0.0
    width = node.find_child("width")
    return (width.get_float(0) or 0.0) if width is not None else 0.0


def _nudged_clear_of(centerline: Any, apertures: Any, required: float) -> Any | None:
    """Translate ``centerline`` perpendicular, away from ``apertures``.

    ``apertures`` is the *unbuffered* pad geometry and ``required`` the
    centerline clearance the stroke needs (``minimum + width / 2 + safety``).
    Returns the shifted line once it clears, or ``None`` when no shift within
    :data:`MAX_NUDGE_MM` does.
    """
    from shapely.geometry import LineString

    (sx, sy), (ex, ey) = centerline.coords[0], centerline.coords[-1]
    length = math.hypot(ex - sx, ey - sy)
    if length == 0:
        return None
    normal = (-(ey - sy) / length, (ex - sx) / length)

    best: Any | None = None
    for sign in (1.0, -1.0):
        shifted = centerline
        travelled = 0.0
        for _ in range(4):
            deficit = required - shifted.distance(apertures)
            if deficit <= 0:
                break
            step = max(deficit, SAFETY_MM)
            if travelled + step > MAX_NUDGE_MM:
                shifted = None  # type: ignore[assignment]
                break
            travelled += step
            dx, dy = normal[0] * sign * travelled, normal[1] * sign * travelled
            shifted = LineString([(sx + dx, sy + dy), (ex + dx, ey + dy)])
        if shifted is not None and shifted.distance(apertures) >= required:
            if best is None or shifted.distance(centerline) < best.distance(centerline):
                best = shifted
    return best


def trim_silk_to_pad_clearance(
    pcb_path: Path,
    *,
    minimum_mm: float,
    output_path: Path | None = None,
) -> SilkPadTrimResult:
    """Clip footprint silk lines that crowd a pad aperture, in place.

    Args:
        pcb_path: Board to repair (read, and written back unless
            ``output_path`` is given).
        minimum_mm: The fabricator's ``min_silk_to_pad_clearance_mm``.
        output_path: Optional separate destination.

    Returns:
        A :class:`SilkPadTrimResult`.  ``residual`` lists violations still
        present after the pass (re-measured with the real rule on the written
        bytes) -- callers should treat a non-empty list as a hard error rather
        than shipping the board.
    """
    from shapely import unary_union
    from shapely.geometry import LineString, MultiLineString

    pcb = PCB.load(pcb_path)
    apertures = _pad_apertures_by_side(pcb)
    result = SilkPadTrimResult(minimum_mm=minimum_mm)

    for footprint in pcb.footprints:
        footprint_node = footprint._sexp_node
        if footprint_node is None:  # pragma: no cover -- linked by PCB.__init__
            raise ValueError(f"Footprint {footprint.reference} has no linked S-expression node")
        fp_x, fp_y, cos_rot, sin_rot = _footprint_frame(footprint)
        reference = footprint.reference

        def to_board(point: tuple[float, float]) -> tuple[float, float]:
            lx, ly = point
            return (fp_x + lx * cos_rot - ly * sin_rot, fp_y + lx * sin_rot + ly * cos_rot)

        def to_local(point: tuple[float, float]) -> tuple[float, float]:
            dx, dy = point[0] - fp_x, point[1] - fp_y
            return (dx * cos_rot + dy * sin_rot, -dx * sin_rot + dy * cos_rot)

        replacements: list[tuple[Any, list[Any]]] = []
        # (old_local_endpoint, new_local_endpoint) for each endpoint of a
        # nudged line, so joined neighbours can be re-attached afterwards.
        moved_joints: list[tuple[tuple[float, float], tuple[float, float]]] = []
        nudged_nodes: set[int] = set()
        for node in list(footprint_node.children):
            layer_node = node.find_child("layer")
            if layer_node is None or layer_node.get_string(0) not in SILK_LAYERS:
                continue
            side = _side_of(layer_node.get_string(0) or "")
            side_apertures = apertures.get(side) or []
            if not side_apertures:
                continue

            if node.name != "fp_line":
                # Not rewritten -- see the module docstring.  Anything left
                # violating is reported by :func:`residual_violations` and
                # fails the caller loud rather than shipping quietly.
                continue

            width = _stroke_width_of(node)
            if width <= 0:
                continue
            start = to_board(_points_of(node, "start"))
            end = to_board(_points_of(node, "end"))
            if start == end:
                continue
            result.lines_examined += 1
            centerline = LineString([start, end])
            halo = minimum_mm + width / 2.0
            offenders = [
                aperture
                for aperture in side_apertures
                if centerline.distance(aperture) + DRC_TOLERANCE < halo
            ]
            if not offenders:
                continue

            keepout = unary_union(
                [
                    aperture.buffer(halo + SAFETY_MM, quad_segs=_BUFFER_QUAD_SEGS)
                    for aperture in offenders
                ]
            )
            remaining = centerline.difference(keepout)
            if remaining.is_empty:
                pieces: list[Any] = []
            elif isinstance(remaining, MultiLineString):
                pieces = list(remaining.geoms)
            else:
                pieces = [remaining]
            pieces = [p for p in pieces if p.length >= MIN_SEGMENT_MM]

            if not pieces:
                # Parallel-to-pad line: clipping consumes it.  Try a bounded
                # perpendicular nudge before giving up on the graphic (this is
                # what keeps J3's pin-1 marker on the board).
                nudged = _nudged_clear_of(centerline, unary_union(offenders), halo + SAFETY_MM)
                if nudged is None:
                    replacements.append((node, []))
                    result.lines_removed += 1
                    result.trimmed_references.append(reference)
                    continue
                pieces = [nudged]
                result.lines_nudged += 1
                nudged_nodes.add(id(node))
                old_ends = (_points_of(node, "start"), _points_of(node, "end"))
                new_coords = list(nudged.coords)
                new_ends = (to_local(new_coords[0]), to_local(new_coords[-1]))
                moved_joints.extend(zip(old_ends, new_ends, strict=True))
            else:
                result.lines_trimmed += 1

            rewritten: list[Any] = []
            for index, piece in enumerate(pieces):
                coords = list(piece.coords)
                local_start = to_local(coords[0])
                local_end = to_local(coords[-1])
                if index == 0:
                    clone = node
                else:
                    clone = parse_string(serialize_sexp(node))
                    clone.remove_child("uuid")
                    clone.add(
                        parse_string(
                            f'(uuid "{_uuid.uuid5(_SPLIT_NAMESPACE, f"{reference}:{coords}")}")'
                        )
                    )
                    result.lines_added += 1
                for tag, point in (("start", local_start), ("end", local_end)):
                    child = clone.find_child(tag)
                    child.set_value(0, round(point[0], 6))
                    child.set_value(1, round(point[1], 6))
                rewritten.append(clone)
            replacements.append((node, rewritten))
            result.trimmed_references.append(reference)

        # Issue #5762: a nudged line that used to share an endpoint with a
        # neighbour (J3's pin-1 marker meets the body outline) must stay
        # joined, or ``silk_overlap`` sees two crossing strokes instead of a
        # corner.  Re-attach neighbour endpoints that sat on the old joint.
        # Only ever moves an endpoint onto the nudged line's new endpoint
        # (<= JOINT_SNAP_MM away); ``residual_violations`` re-verifies.
        for node in footprint_node.children:
            if node.name != "fp_line" or id(node) in nudged_nodes:
                continue
            layer_node = node.find_child("layer")
            if layer_node is None or layer_node.get_string(0) not in SILK_LAYERS:
                continue
            for tag in ("start", "end"):
                point = _points_of(node, tag)
                for old, new in moved_joints:
                    if math.dist(point, old) <= JOINT_SNAP_MM:
                        child = node.find_child(tag)
                        child.set_value(0, round(new[0], 6))
                        child.set_value(1, round(new[1], 6))
                        result.joints_reattached += 1
                        break

        if replacements:
            rewritten_children: list[Any] = []
            replaced = {id(original): pieces for original, pieces in replacements}
            for child in footprint_node.children:
                if id(child) in replaced:
                    rewritten_children.extend(replaced[id(child)])
                else:
                    rewritten_children.append(child)
            footprint_node.children = rewritten_children

    result.trimmed_references = sorted(set(result.trimmed_references))
    written = Path(output_path) if output_path is not None else Path(pcb_path)
    if result.changed or output_path is not None:
        pcb.save(written)
    result.residual = residual_violations(written, minimum_mm=minimum_mm)
    return result


def silk_to_pad_floor(manufacturer: str = "jlcpcb-tier1", *, layers: int = 4) -> float:
    """The reviewed fabricator's ``min_silk_to_pad_clearance_mm``.

    Read from the manufacturer profile rather than restated here so the
    repair, ``kct check --mfr <same profile>`` and the emitted ``.kicad_dru``
    can never drift apart.
    """
    from kicad_tools.manufacturers import get_profile

    rules = get_profile(manufacturer).get_design_rules(layers=layers)
    if rules.min_silk_to_pad_clearance_mm is None:
        raise ValueError(f"{manufacturer} declares no min_silk_to_pad_clearance_mm")
    return rules.min_silk_to_pad_clearance_mm


def widen_silk_strokes(pcb_path: Path, *, manufacturer: str = "jlcpcb-tier1") -> int:
    """Raise footprint silk graphic strokes to the fabricator's width floor.

    Covers ``fp_line``/``fp_rect``/``fp_circle``/``fp_arc``/``fp_poly`` via
    ``drc.repair_silkscreen`` (graphics only; silk text is untouched).
    Returns the number of graphics widened (Issue #5762).
    """
    from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer
    from kicad_tools.manufacturers import get_profile

    floor = get_profile(manufacturer).get_design_rules(layers=4).min_silkscreen_width_mm
    if floor is None:
        raise ValueError(f"{manufacturer} declares no min_silkscreen_width_mm")
    repairer = SilkscreenRepairer(pcb_path)
    result = repairer.repair_line_widths(floor)
    if result.total_fixed:
        repairer.save()
    return result.total_fixed


def residual_violations(pcb_path: Path, *, minimum_mm: float) -> list[str]:
    """Re-measure ``silk_pad_clearance`` with the real rule, on real bytes.

    Deliberately re-reads the saved file and calls
    ``check_silk_pad_clearance`` rather than reusing this module's own
    geometry: the repair is only trustworthy if the rule that gates CI agrees
    with it on the bytes that ship.
    """
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.validate.rules.factory_clearance import check_silk_pad_clearance

    rules = DesignRules(
        min_trace_width_mm=0.0,
        min_clearance_mm=0.0,
        min_via_drill_mm=0.0,
        min_via_diameter_mm=0.0,
        min_annular_ring_mm=0.0,
        min_silk_to_pad_clearance_mm=minimum_mm,
    )
    return sorted(
        " ".join(violation.items)
        for violation in check_silk_pad_clearance(PCB.load(pcb_path), rules).violations
    )


def repair_board_silk_pad_clearance(
    pcb_path: Path,
    *,
    manufacturer: str = "jlcpcb-tier1",
    verbose: bool = True,
) -> SilkPadTrimResult:
    """Bring a freshly generated board's silk inside the fabricator's floor.

    This is the recipe-facing entry point, called from
    ``generate_pcb.write_pcb()`` so it runs at the PLACEMENT stage for
    *every* consumer of the generated board -- ``generate_design.py``, a bare
    ``python generate_pcb.py <dir>``, ``route_demo.py`` replaying into a temp
    directory and the regeneration fixtures in
    ``tests/test_board_03_regression.py`` alike.  Running it only inside
    ``generate_design.py:main()`` is NOT enough: the other entry points would
    then hand an unrepaired board to ``routing_plan.apply_plan()``, which
    (correctly, post-#5744) carries footprint silk through untouched, and the
    replayed board would carry 21 ``silk_pad_clearance`` errors.

    Placement stage also means the routed replay inherits byte-identical
    footprint silk: routing must never be the thing that changes silkscreen
    (asserted by ``tests/test_board_silk_graphics_preserved_5744.py``).

    Raises:
        RuntimeError: if any ``silk_pad_clearance`` violation survives the
            pass.  Fails loud rather than shipping a board whose silk the
            fabricator will clip.
    """
    minimum = silk_to_pad_floor(manufacturer)
    # Issue #5762: widen FIRST, clip SECOND.  The clip buffers each pad
    # aperture by ``minimum + stroke_width / 2``, so it must see the final
    # stroke width or the wider ink would re-enter the keepout.
    widened = widen_silk_strokes(pcb_path, manufacturer=manufacturer)
    if verbose:
        print(f"   {manufacturer} silk stroke floor: {widened} graphic(s) widened")
    result = trim_silk_to_pad_clearance(pcb_path, minimum_mm=minimum)
    if verbose:
        print(
            f"   {manufacturer} silk-to-pad floor {minimum}mm: "
            f"{result.lines_trimmed} line(s) clipped, {result.lines_nudged} nudged, "
            f"{result.lines_removed} dropped, {result.lines_added} split out "
            f"({', '.join(result.trimmed_references) or 'no footprint affected'})"
        )
    if result.residual:
        raise RuntimeError(
            f"{len(result.residual)} silk_pad_clearance violation(s) survive the "
            f"repair pass on {pcb_path}: {sorted(set(result.residual))}. Fix the "
            "footprint geometry -- do not ship silk the fabricator will clip."
        )
    return result
