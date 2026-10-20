"""Expected CPL coordinates in the Gerber/drill frame, independent of ``kct export`` (Issue #7199).

``kct export`` writes ``Mid X``/``Mid Y`` in the frame the Gerbers and drill
files use (#7059): the aux axis origin (or the page origin) is the zero point
and Y points up.  This module recomputes that frame from the routed PCB's raw
footprint ``(at x y)`` so a committed CPL can be checked against something
other than the exporter that produced it.  Pinned kicad-cli 10.0.6
``pcb export pos --use-drill-file-origin`` agrees with it to 0.0000 mm on all
eight fleet boards.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

from kicad_tools.sexp import parse_file

#: Largest accepted disagreement between a CPL coordinate and the PCB frame (mm).
TOLERANCE_MM = 0.01


def _tht_centroid_offset(footprint, angle_deg: float) -> tuple[float, float]:
    """Board-frame (Y down) offset from the origin to the numbered-pad bbox centre of a THT part.

    JLCPCB's Mid X/Mid Y is the component centroid, so a through-hole part
    (pin 1 at the origin on a header) is placed at its pad-bbox centre
    (Issue #7433).  Written from the raw ``(pad ...)`` nodes, not from the
    exporter, to stay an independent check.
    """
    pads = []
    for pad in footprint.find_all("pad"):
        number = pad.get_string(0)
        at, size = pad.find("at"), pad.find("size")
        if not number or at is None or size is None:
            continue
        pads.append((number, pad.get_string(1), at, size))
    smd = {n for n, kind, _, _ in pads if kind == "smd"}
    tht = [(a, z) for n, kind, a, z in pads if kind == "thru_hole" and n not in smd]
    if not tht:
        return (0.0, 0.0)
    theta = math.radians(angle_deg)
    xs: list[float] = []
    ys: list[float] = []
    for at, size in tht:
        lx, ly = float(at.get_value(0)), float(at.get_value(1))
        rot = float(at.get_value(2)) if len(at.children) > 2 else 0.0
        w, h = float(size.get_value(0)), float(size.get_value(1))
        if round(rot / 90.0) % 2:
            w, h = h, w
        rx = lx * math.cos(theta) + ly * math.sin(theta)
        ry = -lx * math.sin(theta) + ly * math.cos(theta)
        xs += [rx - w / 2, rx + w / 2]
        ys += [ry - h / 2, ry + h / 2]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def drill_frame_positions(pcb: Path) -> dict[str, tuple[float, float]]:
    """Return ``{reference: (x, y)}`` in mm in the Gerber/drill frame of ``pcb``."""
    root = parse_file(pcb)
    origin = root.find("setup")
    aux = origin.find("aux_axis_origin") if origin is not None else None
    ox, oy = (float(aux.get_value(0)), float(aux.get_value(1))) if aux is not None else (0.0, 0.0)
    positions: dict[str, tuple[float, float]] = {}
    for footprint in root.find_all("footprint"):
        reference = next(
            (
                prop.get_string(1)
                for prop in footprint.find_all("property")
                if prop.get_string(0) == "Reference"
            ),
            None,
        )
        at = footprint.find("at")
        if reference is None or at is None:
            continue
        angle = float(at.get_value(2)) if len(at.children) > 2 else 0.0
        dx, dy = _tht_centroid_offset(footprint, angle)
        positions[reference] = (
            float(at.get_value(0)) + dx - ox,
            -(float(at.get_value(1)) + dy - oy),
        )
    return positions


def read_cpl(path: Path | str | bytes) -> list[dict[str, str]]:
    """Parse a ``cpl_jlcpcb.csv`` (path, or its raw bytes)."""
    text = path.decode() if isinstance(path, bytes) else Path(path).read_text()
    return list(csv.DictReader(text.splitlines()))


def _mm(value: str) -> float:
    return float(value.removesuffix("mm"))


def coordinate_mismatches(
    cpl_rows: list[dict[str, str]],
    pcb: Path,
    tolerance_mm: float = TOLERANCE_MM,
) -> list[str]:
    """Describe every CPL row whose ``Mid X``/``Mid Y`` disagrees with ``pcb``'s drill frame."""
    expected = drill_frame_positions(pcb)
    problems: list[str] = []
    for row in cpl_rows:
        reference = row["Designator"]
        if reference not in expected:
            problems.append(f"{reference}: not a footprint on {pcb.name}")
            continue
        ex, ey = expected[reference]
        gx, gy = _mm(row["Mid X"]), _mm(row["Mid Y"])
        delta = max(abs(gx - ex), abs(gy - ey))
        if delta > tolerance_mm:
            problems.append(
                f"{reference}: CPL ({gx:.4f}, {gy:.4f}) mm vs drill frame ({ex:.4f}, {ey:.4f}) mm"
                f" (|d|={delta:.4f})"
            )
    return problems


def only_coordinates_differ(old: bytes, new: bytes) -> bool:
    """True when two CPLs list the same rows and differ solely in ``Mid X``/``Mid Y``."""
    a, b = read_cpl(old), read_cpl(new)
    if len(a) != len(b):
        return False
    coords = {"Mid X", "Mid Y"}
    for ra, rb in zip(a, b, strict=True):
        if ra.keys() != rb.keys():
            return False
        if any(ra[k] != rb[k] for k in ra if k not in coords):
            return False
    return True
