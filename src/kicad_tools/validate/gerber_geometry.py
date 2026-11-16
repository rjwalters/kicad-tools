"""Strict polygon reconstruction of the native Gerber export profile.

Unknown commands reject the complete layer. Geometry is millimetres, board y-down.
The chord error is a numerical approximation budget, not a manufacturing limit.

Aperture macros are expanded for exactly the subset KiCad 10 emits (%AMRoundRect):
comment primitives and primitives 1 (circle), 4 (outline) and 20 (vector line),
with literal, $n and single-binary-`+` field arithmetic; macro lengths scale
with the units command, and a macro aperture may only be flashed, never drawn.
Any other macro construct rejects the complete layer rather than guessing pad
geometry.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

GERBER_CHORD_ERROR_MM = 0.000001

# Macro outline vertex-count bounds: the spec requires at least three vertices
# and the cap keeps a hostile body from exploding the geometry budget.
_MACRO_OUTLINE_VERTICES = (3, 500)

# Relative tolerance for accepting a repaired self-touching region contour:
# the repair must preserve the contour's own (shoelace) area.
REGION_REPAIR_AREA_TOLERANCE = 1e-9


class GerberGeometryError(ValueError):
    """The complete layer cannot be reconstructed with known semantics."""


@dataclass
class _MacroPrimitive:
    code: int
    exposure: int
    args: list[str]
    rotation: str | None


def _macro_number(text: str, params: list[float], name: str) -> float:
    """Evaluate a macro field: a literal, an $n reference, or one binary + over two."""
    total = 0.0
    parts = text.split("+")
    if not 1 <= len(parts) <= 2:
        raise GerberGeometryError(f"Unsupported arithmetic in macro {name}: {text.strip()!r}")
    for part in parts:
        part = part.strip()
        if part.startswith("$"):
            index = part[1:]
            if not index.isdigit():
                raise GerberGeometryError(
                    f"Unsupported arithmetic in macro {name}: {text.strip()!r}"
                )
            if not 1 <= int(index) <= len(params):
                raise GerberGeometryError(f"Unbound macro parameter in macro {name}: {part}")
            total += params[int(index) - 1]
            continue
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", part):
            raise GerberGeometryError(f"Unsupported arithmetic in macro {name}: {text.strip()!r}")
        total += float(part)
    if not math.isfinite(total):
        raise GerberGeometryError(f"Nonfinite macro arithmetic in macro {name}")
    return total


def _parse_aperture_macro(name: str, body: str) -> list[_MacroPrimitive]:
    """Validate a macro body at definition time; comments are dropped."""
    primitives: list[_MacroPrimitive] = []
    for token in body.split("*"):
        token = token.strip()
        if not token:
            continue
        if re.split(r"\s+", token, maxsplit=1)[0] == "0":
            continue  # Comment primitive: carries no geometry.
        fields = token.split(",")
        if not re.fullmatch(r"[124][0-9]?", fields[0].strip()) or int(fields[0]) not in {1, 4, 20}:
            raise GerberGeometryError(f"Unsupported macro primitive {fields[0]} in macro {name}")
        code = int(fields[0])
        if len(fields) < 2 or fields[1].strip() not in {"0", "1"}:
            raise GerberGeometryError(f"Unsupported macro exposure in macro {name}: {token[:60]}")
        if code == 4:
            if len(fields) < 3 or not fields[2].strip().isdigit():
                raise GerberGeometryError(f"Invalid macro outline vertex count in macro {name}")
            count = int(fields[2])
            if not _MACRO_OUTLINE_VERTICES[0] <= count <= _MACRO_OUTLINE_VERTICES[1]:
                raise GerberGeometryError(
                    f"Macro outline vertex count out of range in macro {name}"
                )
            base, rotated = 2 * count + 5, 2 * count + 6
        else:
            base, rotated = (5, 6) if code == 1 else (7, 8)
        if len(fields) not in {base, rotated}:
            raise GerberGeometryError(
                f"Unsupported macro primitive {code} parameter count in macro {name}"
            )
        args = fields[2:base]
        rotation = fields[rotated - 1].strip() if len(fields) == rotated else None
        primitives.append(_MacroPrimitive(code, int(fields[1]), args, rotation))
    if not primitives:
        raise GerberGeometryError(f"Macro {name} defines no geometry")
    return primitives


def _expand_aperture_macro(
    name: str,
    primitives: list[_MacroPrimitive],
    params: list[float],
    *,
    units: float,
    Point: Any,
    LineString: Any,
    Polygon: Any,
    rotate: Any,
    _buffer: Any,
) -> Any:
    """Build one aperture shape: dark primitives union, clear primitives erase.

    Length fields are scaled by ``units`` so the shape is millimetres under
    both unit modes (the outline vertex count and rotations stay unscaled).
    Shapely callables are passed in by the caller so the module keeps exactly
    one (lazy) import site per shapely module.
    """

    def evaluate(text: str) -> float:
        return _macro_number(text, params, name)

    def length(text: str) -> float:
        return evaluate(text) * units

    shape = None
    for primitive in primitives:
        if primitive.code == 1:
            diameter, cx, cy = (length(a) for a in primitive.args)
            if diameter <= 0:
                raise GerberGeometryError(f"Nonpositive macro circle in macro {name}")
            piece = _buffer(Point(cx, cy), diameter / 2)
        elif primitive.code == 4:
            count = int(evaluate(primitive.args[0]))
            coords = [length(a) for a in primitive.args[1:]]
            points = list(zip(coords[0::2], coords[1::2], strict=False))
            if len(points) != count + 1 or points[0] != points[-1]:
                raise GerberGeometryError(f"Open or degenerate macro outline in macro {name}")
            piece = Polygon(points)
        else:
            width, x1, y1, x2, y2 = (length(a) for a in primitive.args)
            if width <= 0:
                raise GerberGeometryError(f"Nonpositive macro line in macro {name}")
            piece = _buffer(LineString([(x1, y1), (x2, y2)]), width / 2)
        angle = evaluate(primitive.rotation) if primitive.rotation is not None else 0.0
        if angle:
            piece = rotate(piece, angle, origin=(0, 0))
        if piece.is_empty or not piece.is_valid:
            raise GerberGeometryError(f"Invalid macro primitive polygon in macro {name}")
        if shape is None:
            if not primitive.exposure:
                # The macro image starts empty; a leading clear erases nothing.
                continue
            shape = piece
        elif primitive.exposure:
            shape = shape.union(piece)
        else:
            shape = shape.difference(piece)
    if shape is None or shape.is_empty:
        raise GerberGeometryError(f"Macro {name} produced no geometry")
    return shape


@dataclass
class GerberGeometry:
    geometry: Any
    command_count: int


def _arc(start, end, center, clockwise, tolerance):
    radius = math.dist(start, center)
    if radius <= 0 or abs(math.dist(end, center) - radius) > max(tolerance * 4, 0.00001):
        raise GerberGeometryError("Invalid circular interpolation radius")
    a = math.atan2(start[1] - center[1], start[0] - center[0])
    b = math.atan2(end[1] - center[1], end[0] - center[0])
    sweep = ((a - b) if clockwise else (b - a)) % (2 * math.pi)
    if math.dist(start, end) < tolerance:
        sweep = 2 * math.pi
    step = 2 * math.acos(max(-1, min(1, 1 - tolerance / radius)))
    n = max(1, math.ceil(sweep / step))
    if n > 1_000_000:
        raise GerberGeometryError("Circular interpolation exceeds geometry budget")
    return (
        [start]
        + [
            (
                center[0] + radius * math.cos(a + (-1 if clockwise else 1) * sweep * i / n),
                center[1] + radius * math.sin(a + (-1 if clockwise else 1) * sweep * i / n),
            )
            for i in range(1, n)
        ]
        + [end]
    )


def parse_gerber_geometry(data: str) -> GerberGeometry:
    """Read native Gerber with the KiCad %AMRoundRect macro subset; reject the rest."""
    from shapely import make_valid  # type: ignore[import-untyped]
    from shapely.affinity import rotate, scale, translate  # type: ignore[import-untyped]
    from shapely.geometry import (  # type: ignore[import-untyped]
        GeometryCollection,
        LineString,
        Point,
        Polygon,
        box,
    )
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    from .mask_geometry import _buffer

    geometry = GeometryCollection()
    apertures = {}
    macros: dict[str, list[_MacroPrimitive]] = {}
    selected = None
    units = None
    decimals = None
    position = (0.0, 0.0)
    interpolation = 1
    operation = None
    dark = True
    region: list[list[tuple[float, float]]] | None = None
    contour: list[tuple[float, float]] = []
    ended = False
    count = 0

    pending: list[Any] = []

    def flush():
        # Collapse the same-polarity batch with one unary_union instead of
        # re-unioning the accumulated geometry per shape. Order between dark
        # and clear batches is preserved because callers flush on polarity change.
        nonlocal geometry
        if not pending:
            return
        batch = unary_union(pending)
        pending.clear()
        geometry = geometry.union(batch) if dark else geometry.difference(batch)

    def emit(shape):
        if not shape.is_valid:
            raise GerberGeometryError("Invalid Gerber polygon")
        pending.append(shape)

    def coord(value):
        if decimals is None or units is None:
            raise GerberGeometryError("Coordinates before format/units")
        return int(value) * units / 10**decimals

    cursor = 0
    while cursor < len(data):
        if data[cursor].isspace():
            cursor += 1
            continue
        if ended:
            raise GerberGeometryError("Commands after end of file")
        extended = data[cursor] == "%"
        stop = data.find("%" if extended else "*", cursor + 1)
        if stop < 0:
            raise GerberGeometryError("Truncated command")
        command = data[cursor + 1 : stop] if extended else data[cursor:stop]
        command = command.strip()
        cursor = stop + 1
        count += 1
        if extended:
            if not command.endswith("*"):
                raise GerberGeometryError("Unterminated extended command")
            command = command[:-1]
            match = re.fullmatch(r"FSLAX([246])([56])Y\1\2", command)
            if match:
                if decimals is not None:
                    raise GerberGeometryError("Repeated coordinate format")
                decimals = int(match[2])
            elif command in {"MOMM", "MOIN"}:
                if units is not None:
                    raise GerberGeometryError("Repeated units")
                units = 1.0 if command == "MOMM" else 25.4
            elif command in {"LPD", "LPC"}:
                if region is not None:
                    raise GerberGeometryError("Polarity inside region")
                flush()
                dark = command == "LPD"
            elif "*" not in command and re.fullmatch(
                r"(?:TF\.[^*]+|TA\.[^*]+|TO\.[^*]+|TD(?:\.[^*]+)?)", command
            ):
                pass  # X2 metadata does not change the image.
            elif match := re.fullmatch(r"AM([A-Za-z0-9_+\-]+)\*(.+)", command, re.DOTALL):
                name = match[1]
                if name in macros:
                    raise GerberGeometryError("Redefined aperture macro")
                macros[name] = _parse_aperture_macro(name, match[2])
            elif match := re.fullmatch(r"ADD(\d+)([A-Za-z][A-Za-z0-9_+\-]*)(?:,(.+))?", command):
                if units is None:
                    raise GerberGeometryError("Aperture before units")
                number, kind = int(match[1]), match[2]
                if number in apertures:
                    raise GerberGeometryError("Redefined aperture")
                if kind not in {"C", "R", "O", "P"} and kind not in macros:
                    raise GerberGeometryError(f"Unknown aperture template: {kind}")
                try:
                    values = [float(v) for v in match[3].split("X")] if match[3] else []
                except ValueError:
                    raise GerberGeometryError(f"Invalid aperture parameters: {kind}") from None
                if not all(math.isfinite(v) for v in values):
                    raise GerberGeometryError("Nonfinite aperture")
                if kind in macros:
                    # Macro apertures are flashed; width/hole only apply to draws.
                    # nonpositive is False: the macro's own expansion rejects empty
                    # geometry, and a draw must reach the macro-kind raise below.
                    apertures[number] = (
                        _expand_aperture_macro(
                            kind,
                            macros[kind],
                            values,
                            units=units,
                            Point=Point,
                            LineString=LineString,
                            Polygon=Polygon,
                            rotate=rotate,
                            _buffer=_buffer,
                        ),
                        kind,
                        0.0,
                        False,
                        False,
                    )
                    continue
                required = {"C": 1, "R": 2, "O": 2, "P": 3}[kind]
                if len(values) not in {required, required + 1}:
                    raise GerberGeometryError("Unsupported aperture parameters")
                width = values[0] * units
                # A zero-size aperture may be defined and selected (KiCad uses
                # one as the current aperture while emitting G36/G37 regions);
                # only flashing or stroking it is illegal, so the rejection
                # lives at the use sites below.
                nonpositive = width <= 0
                if kind == "C":
                    shape = _buffer(Point(0, 0), width / 2)
                elif kind in {"R", "O"}:
                    height = values[1] * units
                    if height <= 0:
                        nonpositive = True
                    if kind == "R":
                        shape = box(-width / 2, -height / 2, width / 2, height / 2)
                    else:
                        radius = min(width, height) / 2
                        dx, dy = width / 2 - radius, height / 2 - radius
                        shape = _buffer(
                            LineString([(-dx, -dy), (dx, dy)]) if dx or dy else Point(0, 0), radius
                        )
                else:
                    n, angle = int(values[1]), math.radians(values[2])
                    if n != values[1] or not 3 <= n <= 12:
                        raise GerberGeometryError("Invalid polygon aperture")
                    shape = Polygon(
                        [
                            (
                                width / 2 * math.cos(angle + i * 2 * math.pi / n),
                                width / 2 * math.sin(angle + i * 2 * math.pi / n),
                            )
                            for i in range(n)
                        ]
                    )
                if len(values) > required and values[-1] < 0:
                    raise GerberGeometryError("Negative aperture hole")
                hole = len(values) > required and values[-1] > 0
                if hole:
                    shape = shape.difference(_buffer(Point(0, 0), values[-1] * units / 2))
                apertures[number] = (shape, kind, width, hole, nonpositive)
            else:
                raise GerberGeometryError(f"Unsupported extended command: {command[:80]}")
            continue
        if command.startswith("G04"):
            continue
        if command in {"G75", "G01", "G02", "G03"}:
            if command != "G75":
                interpolation = int(command[-1])
            continue
        if command == "G36":
            if region is not None:
                raise GerberGeometryError("Nested region")
            region, contour = [], []
            continue
        if command == "G37":
            if region is None:
                raise GerberGeometryError("Region end without start")
            if contour:
                region.append(contour)
            shape = GeometryCollection()
            for points in region:
                if len(points) < 4 or points[0] != points[-1]:
                    raise GerberGeometryError("Open or degenerate region")
                polygon = Polygon(points)
                if not polygon.is_valid:
                    # A self-touching contour is normal fill output (a pour
                    # pinching off at a point) and carries unambiguous area:
                    # keep the polygonal parts of the repair and require the
                    # shoelace area to survive it. An ambiguous interior
                    # (e.g. a self-intersecting bow tie) changes the area and
                    # is still rejected. make_valid can also yield
                    # lower-dimensional parts at the pinch, so filtering only
                    # a Polygon/MultiPolygon top-level result is not enough.
                    repaired = make_valid(polygon)
                    parts = [
                        part
                        for part in getattr(repaired, "geoms", [repaired])
                        if part.geom_type in {"Polygon", "MultiPolygon"}
                    ]
                    if not parts:
                        raise GerberGeometryError("Invalid region contour")
                    repaired = unary_union(parts)
                    if abs(repaired.area - polygon.area) > REGION_REPAIR_AREA_TOLERANCE * max(
                        polygon.area, 1.0
                    ):
                        raise GerberGeometryError("Invalid region contour")
                    polygon = repaired
                shape = shape.symmetric_difference(polygon)
            emit(shape)
            region, contour = None, []
            continue
        if command == "M02":
            if region is not None:
                raise GerberGeometryError("Unterminated region")
            ended = True
            continue
        match = re.fullmatch(
            r"(?:G0?([123]))?(?:X([+-]?\d+))?(?:Y([+-]?\d+))?(?:I([+-]?\d+))?(?:J([+-]?\d+))?(?:D0?(\d+))?",
            command,
        )
        if not match or not any(match.groups()):
            raise GerberGeometryError(f"Unsupported command: {command[:80]}")
        g, x, y, i, j, d = match.groups()
        if g:
            interpolation = int(g)
        if d and int(d) >= 10:
            if any((x, y, i, j)) or int(d) not in apertures:
                raise GerberGeometryError("Invalid aperture selection")
            selected = int(d)
            continue
        if d:
            operation = int(d)
        endpoint = (coord(x) if x else position[0], coord(y) if y else position[1])
        if operation == 2:
            if region is not None:
                if contour:
                    region.append(contour)
                contour = [endpoint]
        elif operation == 1:
            points = [position, endpoint]
            if interpolation != 1:
                center = (
                    position[0] + (coord(i) if i else 0),
                    position[1] + (coord(j) if j else 0),
                )
                points = _arc(position, endpoint, center, interpolation == 2, GERBER_CHORD_ERROR_MM)
            elif i or j:
                raise GerberGeometryError("Arc offsets in linear interpolation")
            if region is not None:
                if not contour:
                    contour = [position]
                contour.extend(points[1:])
            else:
                if selected is None:
                    raise GerberGeometryError("Draw before aperture selection")
                aperture, kind, width, hole, nonpositive = apertures[selected]
                if nonpositive:
                    raise GerberGeometryError("Nonpositive aperture")
                if hole or (interpolation != 1 and kind != "C"):
                    raise GerberGeometryError("Unsupported swept aperture")
                if kind == "C":
                    emit(_buffer(LineString(points), width / 2))
                elif kind in {"R", "O", "P"}:
                    emit(
                        unary_union(
                            [translate(aperture, *position), translate(aperture, *endpoint)]
                        ).convex_hull
                    )
                else:
                    raise GerberGeometryError(f"Draw with macro aperture: {kind}")
        elif operation == 3:
            if region is not None or selected is None:
                raise GerberGeometryError("Invalid flash")
            aperture, _, _, _, nonpositive = apertures[selected]
            if nonpositive:
                raise GerberGeometryError("Nonpositive aperture")
            emit(translate(aperture, *endpoint))
        else:
            raise GerberGeometryError("Coordinates without valid operation")
        position = endpoint
    if not ended or decimals is None or units is None:
        raise GerberGeometryError("Incomplete Gerber layer")
    flush()
    return GerberGeometry(scale(geometry, xfact=1, yfact=-1, origin=(0, 0)), count)
