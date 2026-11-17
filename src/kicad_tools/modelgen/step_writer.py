"""Dependency-free STEP (AP214) writer for axis-aligned boxes.

Placeholder mode must work without the ``[models]`` extra, so this module
writes a valid ISO-10303-21 file directly: one coloured ``MANIFOLD_SOLID_BREP``
per box, planar faces only (issue #7687).  It is deliberately limited to boxes;
real package bodies go through the CadQuery backend.

Geometry is supplied in KiCad footprint-local mm (``+y`` down); the writer
emits model coordinates (``+y`` up), the frame a KiCad ``(model ...)`` node
with an identity transform expects.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["StepBox", "format_header", "write_boxes_step"]


@dataclass(frozen=True)
class StepBox:
    x0: float
    y0: float
    x1: float
    y1: float
    z0: float
    z1: float
    rgb: tuple[float, float, float]
    name: str = "body"


def _r(v: float) -> str:
    """STEP REAL literal: always carries a decimal point."""
    v = 0.0 if abs(v) < 5e-10 else v
    text = f"{v:.6f}".rstrip("0")
    return text  # "1." is a valid STEP REAL


def _s(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def format_header(
    file_name: str, description: Sequence[str], *, system: str, author: str = "kicad-tools"
) -> str:
    """The ``HEADER`` section; the timestamp is fixed so output is reproducible."""
    desc = ",".join(_s(d) for d in description) or "''"
    return (
        "HEADER;\n"
        f"FILE_DESCRIPTION(({desc}),'2;1');\n"
        f"FILE_NAME({_s(file_name)},'1970-01-01T00:00:00',({_s(author)}),"
        f"({_s(author)}),{_s(system)},{_s(system)},'');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\n"
        "ENDSEC;\n"
    )


class _Data:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, entity: str) -> int:
        self.lines.append(entity)
        return len(self.lines)

    def render(self) -> str:
        return "".join(f"#{i}={e};\n" for i, e in enumerate(self.lines, start=1))


def _box_solid(d: _Data, box: StepBox) -> int:
    # model frame: flip y
    xa, xb = sorted((box.x0, box.x1))
    ya, yb = sorted((-box.y0, -box.y1))
    za, zb = sorted((box.z0, box.z1))
    if xb - xa <= 0 or yb - ya <= 0 or zb - za <= 0:
        raise ValueError(f"degenerate box {box}")
    corners = {
        (i, j, k): (xb if i else xa, yb if j else ya, zb if k else za)
        for i in (0, 1)
        for j in (0, 1)
        for k in (0, 1)
    }
    vertex: dict[tuple[int, int, int], int] = {}
    for key, (x, y, z) in corners.items():
        pt = d.add(f"CARTESIAN_POINT('',({_r(x)},{_r(y)},{_r(z)}))")
        vertex[key] = d.add(f"VERTEX_POINT('',#{pt})")

    edges: dict[tuple[tuple[int, int, int], tuple[int, int, int]], int] = {}

    def edge(a: tuple[int, int, int], b: tuple[int, int, int]) -> tuple[int, bool]:
        key = (a, b) if a < b else (b, a)
        if key not in edges:
            pa, pb = corners[key[0]], corners[key[1]]
            vec = tuple(q - p for p, q in zip(pa, pb, strict=True))
            length = max(abs(c) for c in vec)
            unit = tuple(c / length for c in vec)
            p0 = d.add(f"CARTESIAN_POINT('',({_r(pa[0])},{_r(pa[1])},{_r(pa[2])}))")
            dr = d.add(f"DIRECTION('',({_r(unit[0])},{_r(unit[1])},{_r(unit[2])}))")
            ve = d.add(f"VECTOR('',#{dr},{_r(length)})")
            line = d.add(f"LINE('',#{p0},#{ve})")
            edges[key] = d.add(f"EDGE_CURVE('',#{vertex[key[0]]},#{vertex[key[1]]},#{line},.T.)")
        return edges[key], (a, b) == key

    # (axis index, side, outward normal, ordered corner keys CCW seen from outside)
    faces = []
    for axis in range(3):
        for side in (0, 1):
            normal = [0.0, 0.0, 0.0]
            normal[axis] = 1.0 if side else -1.0
            u, v = [(1, 2), (2, 0), (0, 1)][axis]  # right-handed (u x v = +axis)
            ring = [(0, 0), (1, 0), (1, 1), (0, 1)]
            if not side:
                ring = list(reversed(ring))
            keys: list[tuple[int, int, int]] = []
            for a, b in ring:
                idx = [0, 0, 0]
                idx[axis], idx[u], idx[v] = side, a, b
                keys.append((idx[0], idx[1], idx[2]))
            faces.append((axis, side, normal, keys))

    face_ids = []
    for axis, side, normal, keys in faces:
        oes = []
        for n in range(4):
            eid, same = edge(keys[n], keys[(n + 1) % 4])
            oes.append(d.add(f"ORIENTED_EDGE('',*,*,#{eid},{'.T.' if same else '.F.'})"))
        loop = d.add("EDGE_LOOP('',(" + ",".join(f"#{o}" for o in oes) + "))")
        bound = d.add(f"FACE_OUTER_BOUND('',#{loop},.T.)")
        origin = corners[keys[0]]
        ref = [0.0, 0.0, 0.0]
        ref[(axis + 1) % 3] = 1.0
        po = d.add(f"CARTESIAN_POINT('',({_r(origin[0])},{_r(origin[1])},{_r(origin[2])}))")
        dn = d.add(f"DIRECTION('',({_r(normal[0])},{_r(normal[1])},{_r(normal[2])}))")
        dref = d.add(f"DIRECTION('',({_r(ref[0])},{_r(ref[1])},{_r(ref[2])}))")
        ax = d.add(f"AXIS2_PLACEMENT_3D('',#{po},#{dn},#{dref})")
        plane = d.add(f"PLANE('',#{ax})")
        face_ids.append(d.add(f"ADVANCED_FACE('',(#{bound}),#{plane},.T.)"))
    shell = d.add("CLOSED_SHELL('',(" + ",".join(f"#{f}" for f in face_ids) + "))")
    return d.add(f"MANIFOLD_SOLID_BREP({_s(box.name)},#{shell})")


def write_boxes_step(
    path: str,
    boxes: Sequence[StepBox],
    *,
    product: str,
    description: Sequence[str] = (),
    system: str = "kicad-tools modelgen",
) -> str:
    """Write *boxes* as one STEP part; return the file text."""
    d = _Data()
    app = d.add("APPLICATION_CONTEXT('core data for automotive mechanical design processes')")
    d.add(
        f"APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#{app})"
    )
    pctx = d.add(f"PRODUCT_CONTEXT('',#{app},'mechanical')")
    prod = d.add(f"PRODUCT({_s(product)},{_s(product)},'',(#{pctx}))")
    d.add(f"PRODUCT_RELATED_PRODUCT_CATEGORY('part',$,(#{prod}))")
    pdf = d.add(f"PRODUCT_DEFINITION_FORMATION('','',#{prod})")
    pdctx = d.add(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{app},'design')")
    pdef = d.add(f"PRODUCT_DEFINITION('design','',#{pdf},#{pdctx})")
    pds = d.add(f"PRODUCT_DEFINITION_SHAPE('','',#{pdef})")

    mm = d.add("( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
    rad = d.add("( NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.) )")
    sr = d.add("( NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT() )")
    unc = d.add(
        f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#{mm},"
        "'distance_accuracy_value','confusion accuracy')"
    )
    ctx = d.add(
        f"( GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{unc})) "
        f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{mm},#{rad},#{sr})) "
        "REPRESENTATION_CONTEXT('Context #1','3D Context with UNIT and UNCERTAINTY') )"
    )
    o = d.add("CARTESIAN_POINT('',(0.,0.,0.))")
    dz = d.add("DIRECTION('',(0.,0.,1.))")
    dx = d.add("DIRECTION('',(1.,0.,0.))")
    placement = d.add(f"AXIS2_PLACEMENT_3D('',#{o},#{dz},#{dx})")

    solids = [_box_solid(d, box) for box in boxes]
    rep = d.add(
        "ADVANCED_BREP_SHAPE_REPRESENTATION("
        + _s(product)
        + ",("
        + ",".join(f"#{i}" for i in [placement, *solids])
        + f"),#{ctx})"
    )
    d.add(f"SHAPE_DEFINITION_REPRESENTATION(#{pds},#{rep})")

    styled = []
    for solid, box in zip(solids, boxes, strict=True):
        r, g, b = box.rgb
        col = d.add(f"COLOUR_RGB('',{_r(r)},{_r(g)},{_r(b)})")
        fasc = d.add(f"FILL_AREA_STYLE_COLOUR('',#{col})")
        fas = d.add(f"FILL_AREA_STYLE('',(#{fasc}))")
        ssfa = d.add(f"SURFACE_STYLE_FILL_AREA(#{fas})")
        sss = d.add(f"SURFACE_SIDE_STYLE('',(#{ssfa}))")
        ssu = d.add(f"SURFACE_STYLE_USAGE(.BOTH.,#{sss})")
        psa = d.add(f"PRESENTATION_STYLE_ASSIGNMENT((#{ssu}))")
        styled.append(d.add(f"STYLED_ITEM('color',(#{psa}),#{solid})"))
    d.add(
        "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION('',("
        + ",".join(f"#{s}" for s in styled)
        + f"),#{ctx})"
    )

    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    text = (
        "ISO-10303-21;\n"
        + format_header(name, description, system=system)
        + "DATA;\n"
        + d.render()
        + "ENDSEC;\nEND-ISO-10303-21;\n"
    )
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write(text)
    return text
