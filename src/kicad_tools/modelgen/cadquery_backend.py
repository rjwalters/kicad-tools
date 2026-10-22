"""CadQuery backend: :class:`BodySpec` -> coloured STEP assembly (issue #7687).

CadQuery and its OCP bindings are Apache-2.0 and ship in the optional
``[models]`` extra (``pip install 'kicad-tools[models]'``); they are never a
core dependency.  This module imports ``cadquery`` lazily, inside
:func:`write_step`, so importing :mod:`kicad_tools.modelgen` stays free.

Only CadQuery's public API is used.  Nothing here is derived from KiCad's
GPL-3.0 generator scripts (``kicad-library-tools``); the family geometry in
:mod:`kicad_tools.modelgen.families` is our own.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

from .families import MATERIALS, BodySpec, Box, Hole, Prism
from .step_writer import format_header

__all__ = ["INSTALL_HINT", "available", "backend_version", "write_step"]

INSTALL_HINT = (
    "parametric 3D model generation needs the optional CadQuery extra:\n"
    "    pip install 'kicad-tools[models]'      (or: uv sync --extra models)\n"
    "Placeholder bodies need no extra: add --placeholder."
)


def available() -> bool:
    """True when ``cadquery`` is importable (without importing it)."""
    try:
        return importlib.util.find_spec("cadquery") is not None
    except (ImportError, ValueError):
        return False


def backend_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"cadquery {version('cadquery')}"
    except PackageNotFoundError:
        return "cadquery (unknown version)"


def _box(cq: Any, part: Box) -> Any:
    # footprint (+y down) -> model (+y up)
    x0, x1 = sorted((part.x0, part.x1))
    y0, y1 = sorted((-part.y0, -part.y1))
    dx, dy, dz = x1 - x0, y1 - y0, part.z1 - part.z0
    solid = cq.Workplane("XY").box(dx, dy, dz, centered=False).translate((x0, y0, part.z0))
    if part.fillet > 0:
        radius = min(part.fillet, 0.45 * min(dx, dy))
        solid = solid.edges("|Z").fillet(radius)
    return solid


def _prism(cq: Any, part: Prism) -> Any:
    if part.axis == "x":
        # profile in the XZ plane at model y = -across, extruded along +-y
        plane = cq.Plane(origin=(0, -part.across, 0), xDir=(1, 0, 0), normal=(0, -1, 0))
        pts = [(u, z) for u, z in part.points]
    else:
        plane = cq.Plane(origin=(part.across, 0, 0), xDir=(0, 1, 0), normal=(1, 0, 0))
        pts = [(-u, z) for u, z in part.points]
    return cq.Workplane(plane).polyline(pts).close().extrude(part.width / 2, both=True)


def _hole(cq: Any, part: Hole) -> Any:
    return (
        cq.Workplane("XY")
        .workplane(offset=part.z_top - part.depth)
        .center(part.x, -part.y)
        .circle(part.r)
        .extrude(part.depth + 0.01)
    )


def build_shapes(spec: BodySpec) -> list[tuple[str, Any]]:
    """``[(material, cadquery.Workplane)]`` with cuts applied and materials fused."""
    import cadquery as cq

    solids: list[tuple[str, Any]] = []
    for part in spec.parts:
        if isinstance(part, Box) and part.cut:
            if not solids:
                raise ValueError("cut before any solid")
            mat, shape = solids[-1]
            solids[-1] = (mat, shape.cut(_box(cq, part)))
        elif isinstance(part, Hole):
            target = next(
                (i for i in range(len(solids) - 1, -1, -1) if solids[i][0] != "tin"), None
            )
            if target is None:
                raise ValueError("hole before any body solid")
            mat, shape = solids[target]
            solids[target] = (mat, shape.cut(_hole(cq, part)))
        elif isinstance(part, Box):
            solids.append((part.material, _box(cq, part)))
        elif isinstance(part, Prism):
            solids.append((part.material, _prism(cq, part)))
    fused: dict[str, Any] = {}
    for mat, shape in solids:
        fused[mat] = shape if mat not in fused else fused[mat].union(shape)
    return list(fused.items())


_HEADER = re.compile(r"HEADER;.*?ENDSEC;\n?", re.DOTALL)
_NAUO = re.compile(r"(NEXT_ASSEMBLY_USAGE_OCCURRENCE\(\s*)'\d+'")


def write_step(spec: BodySpec, path: Path, description: list[str]) -> None:
    """Build *spec* with CadQuery and write a coloured STEP to *path*.

    The OCCT header carries a wall-clock timestamp and host details; it is
    replaced with :func:`~kicad_tools.modelgen.step_writer.format_header` so
    the provenance is in-band and the header is reproducible.  (OCCT orders
    the colour entities by in-memory handles, so the DATA section's bytes may
    differ between runs; its geometry and colours do not.)
    """
    import cadquery as cq

    assy = cq.Assembly(name=spec.name)
    for mat, shape in build_shapes(spec):
        r, g, b = MATERIALS.get(mat, MATERIALS["black"])
        assy.add(shape, name=f"{spec.name}_{mat}", color=cq.Color(r, g, b))
    path.parent.mkdir(parents=True, exist_ok=True)
    assy.export(str(path), exportType="STEP")
    text = path.read_text(encoding="utf-8", errors="replace")
    header = format_header(
        path.name, description, system=f"kicad-tools modelgen ({backend_version()})"
    )
    text = _HEADER.sub(lambda _m: header, text, count=1)
    # OCCT numbers assembly occurrences from a process-wide counter; renumber
    # them in file order so a second export in the same process is identical.
    counter = iter(range(1, 1 << 30))
    text = _NAUO.sub(lambda m: f"{m.group(1)}'{next(counter)}'", text)
    path.write_text(text, encoding="utf-8", newline="\n")
