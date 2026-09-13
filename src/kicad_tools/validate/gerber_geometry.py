"""Strict polygon reconstruction of the macro-free native Gerber export profile.

Unknown commands reject the complete layer. Geometry is millimetres, board y-down.
The chord error is a numerical approximation budget, not a manufacturing limit.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

GERBER_CHORD_ERROR_MM = 0.000001


class GerberGeometryError(ValueError):
    """The complete layer cannot be reconstructed with known semantics."""


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
    """Read native macro-free Gerber; reject incomplete or unknown command streams."""
    from shapely.affinity import scale, translate  # type: ignore[import-untyped]
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

    def emit(shape):
        nonlocal geometry
        if not shape.is_valid:
            raise GerberGeometryError("Invalid Gerber polygon")
        geometry = geometry.union(shape) if dark else geometry.difference(shape)

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
                dark = command == "LPD"
            elif "*" not in command and re.fullmatch(
                r"(?:TF\.[^*]+|TA\.[^*]+|TO\.[^*]+|TD(?:\.[^*]+)?)", command
            ):
                pass  # X2 metadata does not change the image.
            elif match := re.fullmatch(r"ADD(\d+)([CROP]),(.+)", command):
                if units is None:
                    raise GerberGeometryError("Aperture before units")
                number, kind = int(match[1]), match[2]
                if number in apertures:
                    raise GerberGeometryError("Redefined aperture")
                values = [float(v) for v in match[3].split("X")]
                if not all(math.isfinite(v) for v in values):
                    raise GerberGeometryError("Nonfinite aperture")
                required = {"C": 1, "R": 2, "O": 2, "P": 3}[kind]
                if len(values) not in {required, required + 1}:
                    raise GerberGeometryError("Unsupported aperture parameters")
                width = values[0] * units
                if width <= 0:
                    raise GerberGeometryError("Nonpositive aperture")
                if kind == "C":
                    shape = _buffer(Point(0, 0), width / 2)
                elif kind in {"R", "O"}:
                    height = values[1] * units
                    if height <= 0:
                        raise GerberGeometryError("Nonpositive aperture")
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
                apertures[number] = (shape, kind, width, hole)
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
                    raise GerberGeometryError("Invalid region contour")
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
                aperture, kind, width, hole = apertures[selected]
                if hole or (interpolation != 1 and kind != "C"):
                    raise GerberGeometryError("Unsupported swept aperture")
                if kind == "C":
                    emit(_buffer(LineString(points), width / 2))
                else:
                    emit(
                        unary_union(
                            [translate(aperture, *position), translate(aperture, *endpoint)]
                        ).convex_hull
                    )
        elif operation == 3:
            if region is not None or selected is None:
                raise GerberGeometryError("Invalid flash")
            emit(translate(apertures[selected][0], *endpoint))
        else:
            raise GerberGeometryError("Coordinates without valid operation")
        position = endpoint
    if not ended or decimals is None or units is None:
        raise GerberGeometryError("Incomplete Gerber layer")
    return GerberGeometry(scale(geometry, xfact=1, yfact=-1, origin=(0, 0)), count)
