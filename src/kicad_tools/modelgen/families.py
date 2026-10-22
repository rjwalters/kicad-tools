"""Parametric package families: footprint + parameters -> body primitives.

A family turns a :class:`~kicad_tools.modelgen.footprint_geom.FootprintGeometry`
plus a parameter dictionary into a :class:`BodySpec` -- a list of simple
primitives (boxes, extruded lead profiles, cuts) in the footprint's own frame.
The CAD backend (:mod:`kicad_tools.modelgen.cadquery_backend`) turns the spec
into a STEP solid; nothing here needs a CAD kernel (issue #7687).

Every dimension is derived from the footprint's fab/courtyard outline and pad
field, so a generated body sits inside its courtyard with its long axis along
the pad field by construction.  Parameters override any derived value; a
family can also *synthesize* a footprint from parameters alone (``kct models
generate chip --param L=3.2 --param W=1.6 --param H=0.55``).

All geometry is original work of the kicad-tools project: nominal envelopes,
not vendor drawings and not derived from KiCad's GPL generator scripts.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .footprint_geom import BBox, FootprintGeometry, PadGeom

__all__ = [
    "FAMILIES",
    "BodySpec",
    "Box",
    "Family",
    "Hole",
    "ParamError",
    "Prism",
    "build_body",
    "build_placeholder",
    "family_for_lib_id",
    "get_family",
    "parse_params",
]

#: Named materials -> sRGB (0..1).  Kept deliberately small.
MATERIALS: dict[str, tuple[float, float, float]] = {
    "black": (0.10, 0.10, 0.11),
    "metal": (0.73, 0.74, 0.76),
    "tin": (0.82, 0.82, 0.84),
    "white": (0.93, 0.92, 0.86),
    "ceramic": (0.80, 0.78, 0.72),
    "ferrite": (0.18, 0.18, 0.20),
    "placeholder": (0.85, 0.35, 0.65),
    "marker": (0.85, 0.85, 0.85),
}


class ParamError(ValueError):
    """A family could not be built: a parameter is missing or invalid."""


@dataclass(frozen=True)
class Box:
    """Axis-aligned box in footprint-local mm (``+y`` down, ``z`` up from board)."""

    x0: float
    y0: float
    x1: float
    y1: float
    z0: float
    z1: float
    material: str = "black"
    fillet: float = 0.0  # radius on the vertical edges (CAD backend only)
    cut: bool = False  # subtract from the most recent non-cut solid


@dataclass(frozen=True)
class Prism:
    """A 2-D ``(u, z)`` profile extruded symmetrically across *width*.

    ``axis`` is the footprint axis ``u`` runs along; *across* is the centre
    coordinate on the other axis.  Used for gull-wing and J-bend leads.
    """

    axis: str  # "x" | "y"
    points: tuple[tuple[float, float], ...]
    across: float
    width: float
    material: str = "tin"


@dataclass(frozen=True)
class Hole:
    """Cylindrical cut from the top of the most recent non-cut solid."""

    x: float
    y: float
    r: float
    depth: float
    z_top: float


Primitive = Box | Prism | Hole


@dataclass
class BodySpec:
    """A buildable body: primitives plus the parameters that produced them."""

    name: str
    family: str
    params: dict[str, Any]
    parts: list[Primitive] = field(default_factory=list)
    placeholder: bool = False

    def extent(self) -> BBox | None:
        """Footprint-plane bounding box of the solid parts (cuts excluded)."""
        pts: list[tuple[float, float]] = []
        for part in self.parts:
            if isinstance(part, Box) and not part.cut:
                pts += [(part.x0, part.y0), (part.x1, part.y1)]
            elif isinstance(part, Prism):
                us = [p[0] for p in part.points]
                lo, hi = part.across - part.width / 2, part.across + part.width / 2
                if part.axis == "x":
                    pts += [(min(us), lo), (max(us), hi)]
                else:
                    pts += [(lo, min(us)), (hi, max(us))]
        return BBox.around(pts)

    def height(self) -> float:
        zs = [p.z1 for p in self.parts if isinstance(p, Box) and not p.cut]
        zs += [max(z for _, z in p.points) for p in self.parts if isinstance(p, Prism)]
        return max(zs) if zs else 0.0


@dataclass(frozen=True)
class ParamDoc:
    name: str
    help: str
    default: str = "derived from the footprint"


@dataclass(frozen=True)
class Family:
    name: str
    description: str
    params: tuple[ParamDoc, ...]
    lib_patterns: tuple[str, ...]
    build: Callable[[FootprintGeometry | None, dict[str, Any]], BodySpec]

    def matches(self, lib_id: str) -> bool:
        return any(re.search(p, lib_id, re.IGNORECASE) for p in self.lib_patterns)


# ---------------------------------------------------------------- parameters


def parse_params(items: list[str] | None) -> dict[str, Any]:
    """``["H=1.2", "terminals=ends"]`` -> ``{"H": 1.2, "terminals": "ends"}``."""
    out: dict[str, Any] = {}
    for item in items or []:
        for chunk in item.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise ParamError(f"parameter {chunk!r} is not KEY=VALUE")
            key, value = (s.strip() for s in chunk.split("=", 1))
            out[key] = _coerce(value)
    return out


def _coerce(value: str) -> Any:
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return float(value) if any(c in value for c in ".eE") else int(value)
    except ValueError:
        return value


def _num(params: Mapping[str, Any], key: str, default: float | None = None) -> float:
    value = params.get(key, default)
    if value is None:
        raise ParamError(f"missing parameter {key}= (mm)")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ParamError(f"parameter {key}={value!r} is not a number") from exc
    if number < 0:
        raise ParamError(f"parameter {key}={number} must be non-negative")
    return number


def _name_float(pattern: str, name: str) -> float | None:
    match = re.search(pattern, name, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _name_dims(name: str) -> tuple[float, float, float | None] | None:
    """``..._7.0x7.0x4.5mm`` / ``..._3x3mm`` -> (a, b, height or None)."""
    match = re.search(r"_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(?:x(\d+(?:\.\d+)?))?mm", name)
    if not match:
        return None
    c = float(match.group(3)) if match.group(3) else None
    return float(match.group(1)), float(match.group(2)), c


def _body_rect(geom: FootprintGeometry | None, params: Mapping[str, Any]) -> BBox:
    """Body rectangle: fab core, overridden by ``L``/``W`` along the pad axis."""
    core = geom.fab_core() if geom is not None else None
    axis = geom.pad_axis() if geom is not None else "x"
    if core is None:
        core = BBox(0, 0, 0, 0)
    along = core.width if axis == "x" else core.height
    across = core.height if axis == "x" else core.width
    length = _num(params, "L", along or None)
    width = _num(params, "W", across or None)
    if length <= 0 or width <= 0:
        raise ParamError("body L and W must be positive")
    dx, dy = (length, width) if axis == "x" else (width, length)
    return BBox(core.cx - dx / 2, core.cy - dy / 2, core.cx + dx / 2, core.cy + dy / 2)


def _check_fits(spec: BodySpec, geom: FootprintGeometry | None) -> BodySpec:
    """Record (do not enforce) whether the body sits inside the courtyard."""
    if geom is not None and geom.courtyard is not None:
        ext = spec.extent()
        spec.params["_inside_courtyard"] = bool(ext and geom.courtyard.contains(ext, tol=0.05))
    return spec


def _clean(params: Mapping[str, Any], **derived: Any) -> dict[str, Any]:
    out = dict(derived)
    out.update({k: v for k, v in params.items() if not k.startswith("_")})
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


def _pin1(geom: FootprintGeometry) -> PadGeom | None:
    for pad in geom.signal_pads:
        if pad.number in ("1", "A1"):
            return pad
    return None


def _name(geom: FootprintGeometry | None, family: str, params: Mapping[str, Any]) -> str:
    if geom is not None and geom.name:
        return geom.name
    dims = "_".join(
        f"{k}{v}" for k, v in sorted(params.items()) if isinstance(v, (int, float)) and k != "_"
    )
    return f"{family}_{dims}" if dims else family


# ---------------------------------------------------------- chip / box family

#: Nominal chip-resistor thickness by imperial size code (mm).
_CHIP_R_H = {"01005": 0.13, "0201": 0.23, "0402": 0.35, "0603": 0.45, "0805": 0.5}


def _chip_default_h(geom: FootprintGeometry | None, width: float) -> float:
    if geom is None:
        return round(max(0.3, 0.6 * width), 3)
    code = re.search(r"_(\d{4,5})_\d{4}Metric", geom.name)
    if geom.name.startswith(("C_", "L_")):
        return round(max(0.3, 0.85 * width), 3)
    if code and code.group(1) in _CHIP_R_H:
        return _CHIP_R_H[code.group(1)]
    return 0.55


def _terminal_boxes(rect: BBox, axis: str, t: float, h: float) -> list[Box]:
    """Two metal end caps of length *t* at the ends of *rect* along *axis*."""
    if axis == "x":
        return [
            Box(rect.x0, rect.y0, rect.x0 + t, rect.y1, 0.0, h, "tin"),
            Box(rect.x1 - t, rect.y0, rect.x1, rect.y1, 0.0, h, "tin"),
        ]
    return [
        Box(rect.x0, rect.y0, rect.x1, rect.y0 + t, 0.0, h, "tin"),
        Box(rect.x0, rect.y1 - t, rect.x1, rect.y1, 0.0, h, "tin"),
    ]


def build_chip(geom: FootprintGeometry | None, params: dict[str, Any]) -> BodySpec:
    """Two-terminal chip (R/C/L/fuse/LED ``NNNN_NNNNMetric``)."""
    if geom is None:
        geom = _synth_two_terminal(params)
    rect = _body_rect(geom, params)
    axis = geom.pad_axis()
    length = rect.width if axis == "x" else rect.height
    width = rect.height if axis == "x" else rect.width
    h = _num(params, "H", _chip_default_h(geom, width))
    t = _num(params, "T", round(min(0.25 * length, 0.6), 3))
    if 2 * t >= length:
        raise ParamError(f"terminal length T={t} must be under half the body length {length}")
    body_mat = "ceramic" if geom.name.startswith(("C_", "L_")) else "black"
    body = (
        BBox(rect.x0 + t, rect.y0, rect.x1 - t, rect.y1)
        if axis == "x"
        else BBox(rect.x0, rect.y0 + t, rect.x1, rect.y1 - t)
    )
    parts: list[Primitive] = [Box(body.x0, body.y0, body.x1, body.y1, 0.0, h, body_mat)]
    parts += _terminal_boxes(rect, axis, t, h)
    spec = BodySpec(
        _name(geom, "chip", params),
        "chip",
        _clean(params, L=length, W=width, H=h, T=t),
        parts,
    )
    return _check_fits(spec, geom)


def build_box(geom: FootprintGeometry | None, params: dict[str, Any]) -> BodySpec:
    """Rectangular body (shunts, power inductors, modules) from the fab outline."""
    if geom is None:
        geom = _synth_two_terminal(params)
    rect = _body_rect(geom, params)
    axis = geom.pad_axis()
    length = rect.width if axis == "x" else rect.height
    width = rect.height if axis == "x" else rect.width
    dims = _name_dims(geom.name)
    h_default = dims[2] if dims and dims[2] else None
    h = _num(params, "H", h_default)
    terminals = str(params.get("terminals", "ends" if geom.name.startswith("R_") else "pads"))
    parts: list[Primitive] = []
    derived: dict[str, Any] = {"L": length, "W": width, "H": h, "terminals": terminals}
    if terminals == "ends":
        t = _num(params, "T", _name_float(r"_T(\d+(?:\.\d+)?)mm", geom.name) or 0.2 * length)
        t = min(t, 0.45 * length)
        derived["T"] = t
        th = _num(params, "terminal_h", h)
        body = (
            BBox(rect.x0 + t, rect.y0, rect.x1 - t, rect.y1)
            if axis == "x"
            else BBox(rect.x0, rect.y0 + t, rect.x1, rect.y1 - t)
        )
        body_h = _num(params, "body_h", h)
        parts.append(Box(body.x0, body.y0, body.x1, body.y1, 0.0, body_h, "black"))
        parts += _terminal_boxes(rect, axis, t, th)
    elif terminals == "pads":
        standoff = _num(params, "standoff", 0.1)
        derived["standoff"] = standoff
        parts.append(
            Box(
                rect.x0,
                rect.y0,
                rect.x1,
                rect.y1,
                standoff,
                h,
                "ferrite",
                fillet=_num(params, "fillet", 0.0),
            )
        )
        for pad in geom.signal_pads:
            pb = pad.bbox
            x0, x1 = max(pb.x0, rect.x0), min(pb.x1, rect.x1)
            y0, y1 = max(pb.y0, rect.y0), min(pb.y1, rect.y1)
            if x1 - x0 > 0.05 and y1 - y0 > 0.05:
                parts.append(Box(x0, y0, x1, y1, 0.0, standoff, "tin"))
    elif terminals == "none":
        parts.append(Box(rect.x0, rect.y0, rect.x1, rect.y1, 0.0, h, "black"))
    else:
        raise ParamError(f"terminals={terminals!r}: expected ends, pads or none")
    spec = BodySpec(_name(geom, "box", params), "box", _clean(params, **derived), parts)
    return _check_fits(spec, geom)


def _synth_two_terminal(params: Mapping[str, Any]) -> FootprintGeometry:
    length, width = _num(params, "L"), _num(params, "W")
    pad = min(0.3 * length, 1.5)
    pads = [
        PadGeom("1", "smd", -length / 2 + pad / 2, 0.0, pad, width),
        PadGeom("2", "smd", length / 2 - pad / 2, 0.0, pad, width),
    ]
    fab = BBox(-length / 2, -width / 2, length / 2, width / 2)
    return FootprintGeometry("", "", pads=pads, fab=fab, courtyard=fab.inset(-0.25))


# ----------------------------------------------------- gull-wing (SO/SOT/QFP)

_GULLWING_H = (
    (r"^(SOIC|SO)-", 1.75),
    (r"^(SSOP|QSOP)-", 1.75),
    (r"^(HTSSOP|TSSOP|MSOP|HMSOP|VSSOP)-", 1.1),
    (r"^TSOT-23", 1.0),
    (r"^(SOT-23|SOT-323|SOT-353|SOT-363|SC-70|SC-74)", 1.1),
    (r"^(LQFP|TQFP)-", 1.6),
    (r"^QFP-", 2.4),
)


def _gullwing_default_h(name: str) -> float:
    for pattern, h in _GULLWING_H:
        if re.search(pattern, name):
            return h
    return 1.5


def _lead_dir(pad: PadGeom, body: BBox) -> tuple[str, int] | None:
    """Which body side a pad's lead leaves from: (axis, sign), or None if under body."""
    if body.contains(pad.bbox.inset(0.0), tol=1e-6):
        return None
    dx = max(body.x0 - pad.x, pad.x - body.x1, 0.0)
    dy = max(body.y0 - pad.y, pad.y - body.y1, 0.0)
    if dx == 0 and dy == 0:
        # centre is inside the body: use the side the pad overhangs most
        over_x = max(body.x0 - pad.bbox.x0, pad.bbox.x1 - body.x1)
        over_y = max(body.y0 - pad.bbox.y0, pad.bbox.y1 - body.y1)
        if over_x <= 0 and over_y <= 0:
            return None
        if over_x >= over_y:
            return "x", (-1 if body.x0 - pad.bbox.x0 > pad.bbox.x1 - body.x1 else 1)
        return "y", (-1 if body.y0 - pad.bbox.y0 > pad.bbox.y1 - body.y1 else 1)
    if dx >= dy:
        return "x", (-1 if pad.x < body.x0 else 1)
    return "y", (-1 if pad.y < body.y0 else 1)


def build_gullwing(geom: FootprintGeometry | None, params: dict[str, Any]) -> BodySpec:
    """Gull-wing leaded packages: SOIC/SSOP/TSSOP/MSOP, SOT-23 variants, QFP."""
    if geom is None:
        geom = _synth_dual_row(params, lead_out=True)
    body = geom.fab_core()
    if body is None:
        raise ParamError("footprint has no fab outline; pass L= and W=")
    if "L" in params or "W" in params:
        body = _body_rect(geom, params)
    h = _num(params, "H", _gullwing_default_h(geom.name))
    a1 = _num(params, "A1", 0.1)
    c = _num(params, "c", 0.15 if h < 1.3 else 0.2)
    parts: list[Primitive] = [
        Box(body.x0, body.y0, body.x1, body.y1, a1, h, "black", fillet=_num(params, "fillet", 0.0))
    ]
    z_arm = a1 + (h - a1) * 0.4
    pitch_guess = _min_pitch(geom.signal_pads)
    for pad in geom.signal_pads:
        side = _lead_dir(pad, body)
        if side is None:
            continue  # exposed pad under the body
        axis, sign = side
        pad_box = pad.bbox
        if axis == "x":
            edge = body.x1 if sign > 0 else body.x0
            tip = pad_box.x1 - 0.1 * pad.w if sign > 0 else pad_box.x0 + 0.1 * pad.w
            across, pad_across = pad.y, pad.h
        else:
            edge = body.y1 if sign > 0 else body.y0
            tip = pad_box.y1 - 0.1 * pad.h if sign > 0 else pad_box.y0 + 0.1 * pad.h
            across, pad_across = pad.x, pad.w
        width = _num(params, "b", round(min(0.75 * pad_across, 0.6 * pitch_guess), 3))
        reach = abs(tip - edge)
        foot = min(_num(params, "foot", 0.6 * reach), reach * 0.8)
        bend = edge + sign * (reach - foot) * 0.5
        knee = tip - sign * foot
        profile = (
            (edge - sign * 0.05, z_arm),
            (bend, z_arm),
            (knee, c),
            (tip, c),
            (tip, 0.0),
            (knee - sign * c, 0.0),
            (bend - sign * c, z_arm - c),
            (edge - sign * 0.05, z_arm - c),
        )
        parts.append(Prism(axis, profile, across, width, "tin"))
    pin1 = _pin1(geom)
    if pin1 is not None and min(body.width, body.height) > 1.2:
        r = min(0.25, 0.12 * min(body.width, body.height))
        px = body.x0 + 2.5 * r if pin1.x <= body.cx else body.x1 - 2.5 * r
        py = body.y0 + 2.5 * r if pin1.y <= body.cy else body.y1 - 2.5 * r
        parts.append(Hole(px, py, r, 0.05, h))
    spec = BodySpec(
        _name(geom, "gullwing", params),
        "gullwing",
        _clean(params, H=h, A1=a1, c=c, body_x=body.width, body_y=body.height),
        parts,
    )
    return _check_fits(spec, geom)


def _min_pitch(pads: list[PadGeom]) -> float:
    best = 2.54
    for i, a in enumerate(pads):
        for b in pads[i + 1 :]:
            d = max(abs(a.x - b.x), abs(a.y - b.y))
            if 0.2 < d < best:
                best = d
    return best


def _synth_dual_row(params: Mapping[str, Any], *, lead_out: bool) -> FootprintGeometry:
    """Two pad rows along y at +-x (SOIC-style numbering, counter-clockwise)."""
    pins = int(_num(params, "pins"))
    if pins < 2 or pins % 2:
        raise ParamError("pins= must be an even number >= 2")
    pitch = _num(params, "pitch")
    length = _num(params, "L")  # along the rows
    width = _num(params, "W")  # body, across the rows
    span = _num(params, "E", width + (2.0 if lead_out else 0.0))
    per_side = pins // 2
    pad_len = max(0.3, (span - width) / 2 + 0.3) if lead_out else 0.6
    pad_w = 0.6 * pitch
    x_pad = span / 2 - pad_len / 2 if lead_out else width / 2 - pad_len / 2 + 0.05
    pads: list[PadGeom] = []
    for i in range(per_side):
        y = -((per_side - 1) * pitch) / 2 + i * pitch
        pads.append(PadGeom(str(i + 1), "smd", -x_pad, y, pad_len, pad_w))
    for i in range(per_side):
        y = ((per_side - 1) * pitch) / 2 - i * pitch
        pads.append(PadGeom(str(per_side + i + 1), "smd", x_pad, y, pad_len, pad_w))
    fab = BBox(-width / 2, -length / 2, width / 2, length / 2)
    crt_x = max(span, width) / 2 + 0.25
    return FootprintGeometry(
        "",
        "",
        pads=pads,
        fab=fab,
        courtyard=BBox(-crt_x, -length / 2 - 0.25, crt_x, length / 2 + 0.25),
    )


# --------------------------------------------------------- no-lead (QFN/DFN)


def build_nolead(geom: FootprintGeometry | None, params: dict[str, Any]) -> BodySpec:
    """QFN/DFN with optional exposed pad: terminals flush with the body underside."""
    if geom is None:
        geom = _synth_dual_row(params, lead_out=False)
    body = _body_rect(geom, params) if ("L" in params or "W" in params) else geom.fab_core()
    if body is None:
        raise ParamError("footprint has no fab outline; pass L= and W=")
    dims = _name_dims(geom.name)
    h = _num(params, "H", 0.75 if geom.name.startswith("DFN") else 0.85)
    if dims and dims[2]:
        h = _num(params, "H", dims[2])
    t = _num(params, "terminal_h", 0.2)
    parts: list[Primitive] = [Box(body.x0, body.y0, body.x1, body.y1, 0.0, h, "black")]
    for pad in geom.signal_pads:
        pb = pad.bbox
        x0, x1 = max(pb.x0, body.x0), min(pb.x1, body.x1)
        y0, y1 = max(pb.y0, body.y0), min(pb.y1, body.y1)
        if x1 - x0 <= 0.02 or y1 - y0 <= 0.02:
            continue
        # a terminal on a body edge stands 0.02 proud so the side shows it
        x0 -= 0.02 if abs(x0 - body.x0) < 1e-6 else 0.0
        x1 += 0.02 if abs(x1 - body.x1) < 1e-6 else 0.0
        y0 -= 0.02 if abs(y0 - body.y0) < 1e-6 else 0.0
        y1 += 0.02 if abs(y1 - body.y1) < 1e-6 else 0.0
        parts.append(Box(x0, y0, x1, y1, -0.0, t, "tin"))
    pin1 = _pin1(geom)
    if pin1 is not None and min(body.width, body.height) > 1.0:
        r = min(0.2, 0.1 * min(body.width, body.height))
        px = body.x0 + 2.5 * r if pin1.x <= body.cx else body.x1 - 2.5 * r
        py = body.y0 + 2.5 * r if pin1.y <= body.cy else body.y1 - 2.5 * r
        parts.insert(1, Hole(px, py, r, 0.03, h))
    spec = BodySpec(
        _name(geom, "nolead", params),
        "nolead",
        _clean(params, H=h, terminal_h=t, body_x=body.width, body_y=body.height),
        parts,
    )
    return _check_fits(spec, geom)


# --------------------------------------------------- pin headers / JST-style


def _header_defaults(geom: FootprintGeometry) -> dict[str, Any]:
    name, lib = geom.name, geom.lib_id.split(":", 1)[0]
    pitch = _name_float(r"_P(\d+(?:\.\d+)?)mm", name) or 2.54
    horizontal = bool(re.search(r"(Horizontal|Angled|RightAngle)", name))
    shrouded = bool(re.search(r"(JST|Molex|Box|Shrouded|IDC)", lib + name))
    if shrouded:
        h = 6.0 if pitch <= 2.0 else 7.0
        if horizontal and pitch <= 2.0:
            h = 6.0
    else:
        h = 2.5 if pitch >= 2.5 else 2.0
    return {
        "pitch": pitch,
        "orientation": "horizontal" if horizontal else "vertical",
        "shrouded": shrouded,
        "H": h,
        "pin": 0.64 if pitch >= 2.5 else 0.5,
    }


def build_header(geom: FootprintGeometry | None, params: dict[str, Any]) -> BodySpec:
    """2.00/2.50/2.54 mm pin headers and JST PH/XH-style shrouded connectors."""
    if geom is None:
        geom = _synth_header(params)
    defaults = _header_defaults(geom)
    orientation = str(params.get("orientation", defaults["orientation"]))
    shrouded = bool(params.get("shrouded", defaults["shrouded"]))
    h = _num(params, "H", defaults["H"])
    pin = _num(params, "pin", defaults["pin"])
    wall = _num(params, "wall", 0.6)
    housing = geom.fab_core()
    if housing is None:
        raise ParamError("footprint has no fab outline; pass the housing size")
    sig = geom.signal_pads
    if not sig:
        raise ParamError("header footprint has no signal pads")
    tht = any(p.kind == "thru_hole" for p in sig)
    parts: list[Primitive] = []

    if orientation == "vertical":
        parts.append(
            Box(
                housing.x0,
                housing.y0,
                housing.x1,
                housing.y1,
                0.0,
                h,
                "black" if not shrouded else "white",
            )
        )
        if shrouded:
            floor = min(1.5, 0.3 * h)
            parts.append(
                Box(
                    housing.x0 + wall,
                    housing.y0 + wall,
                    housing.x1 - wall,
                    housing.y1 - wall,
                    floor,
                    h + 0.01,
                    cut=True,
                )
            )
            top = h - 0.5
        else:
            top = h + _num(params, "pin_above", 6.0 if pin > 0.6 else 4.0)
        bottom = -3.0 if tht else 0.0
        for pad in sig:
            parts.append(
                Box(
                    pad.x - pin / 2,
                    pad.y - pin / 2,
                    pad.x + pin / 2,
                    pad.y + pin / 2,
                    bottom,
                    top,
                    "metal",
                )
            )
    elif orientation == "horizontal":
        # Side entry: the mating face points away from the signal-pad row.
        sy = sum(p.y for p in sig) / len(sig)
        sx = sum(p.x for p in sig) / len(sig)
        axis = "y" if abs(sy - housing.cy) >= abs(sx - housing.cx) else "x"
        away = (1 if sy < housing.cy else -1) if axis == "y" else (1 if sx < housing.cx else -1)
        mat = "white" if shrouded else "black"
        parts.append(Box(housing.x0, housing.y0, housing.x1, housing.y1, 0.0, h, mat))
        back_wall = _num(params, "back_wall", 1.2)
        if shrouded:
            if axis == "y":
                y_open0 = housing.y0 + back_wall if away > 0 else housing.y0 - 0.01
                y_open1 = housing.y1 + 0.01 if away > 0 else housing.y1 - back_wall
                parts.append(
                    Box(
                        housing.x0 + wall,
                        y_open0,
                        housing.x1 - wall,
                        y_open1,
                        wall,
                        h - wall,
                        cut=True,
                    )
                )
            else:
                x_open0 = housing.x0 + back_wall if away > 0 else housing.x0 - 0.01
                x_open1 = housing.x1 + 0.01 if away > 0 else housing.x1 - back_wall
                parts.append(
                    Box(
                        x_open0,
                        housing.y0 + wall,
                        x_open1,
                        housing.y1 - wall,
                        wall,
                        h - wall,
                        cut=True,
                    )
                )
        z_pin = h / 2
        for pad in sig:
            if axis == "y":
                near = housing.y0 if away > 0 else housing.y1
                far = housing.y1 - 0.8 if away > 0 else housing.y0 + 0.8
                lo, hi = sorted((near - away * 0.05, far))
                # contact pin inside the housing, along the mating axis
                parts.append(
                    Box(
                        pad.x - pin / 2,
                        lo,
                        pad.x + pin / 2,
                        hi,
                        z_pin - pin / 2,
                        z_pin + pin / 2,
                        "metal",
                    )
                )
                # lead: riser at the housing edge and a foot on the pad
                foot_end = pad.bbox.y0 + 0.2 if away > 0 else pad.bbox.y1 - 0.2
                f0, f1 = sorted((foot_end, near + away * 0.3))
                parts.append(Box(pad.x - pin / 2, f0, pad.x + pin / 2, f1, 0.0, 0.2, "tin"))
                r0, r1 = sorted((near - away * 0.3, near + away * 0.05))
                parts.append(
                    Box(pad.x - pin / 2, r0, pad.x + pin / 2, r1, 0.0, z_pin + pin / 2, "tin")
                )
            else:
                near = housing.x0 if away > 0 else housing.x1
                far = housing.x1 - 0.8 if away > 0 else housing.x0 + 0.8
                lo, hi = sorted((near - away * 0.05, far))
                parts.append(
                    Box(
                        lo,
                        pad.y - pin / 2,
                        hi,
                        pad.y + pin / 2,
                        z_pin - pin / 2,
                        z_pin + pin / 2,
                        "metal",
                    )
                )
                foot_end = pad.bbox.x0 + 0.2 if away > 0 else pad.bbox.x1 - 0.2
                f0, f1 = sorted((foot_end, near + away * 0.3))
                parts.append(Box(f0, pad.y - pin / 2, f1, pad.y + pin / 2, 0.0, 0.2, "tin"))
                r0, r1 = sorted((near - away * 0.3, near + away * 0.05))
                parts.append(
                    Box(r0, pad.y - pin / 2, r1, pad.y + pin / 2, 0.0, z_pin + pin / 2, "tin")
                )
    else:
        raise ParamError(f"orientation={orientation!r}: expected vertical or horizontal")

    # hold-down tabs on mounting pads (clipped to the courtyard-safe pad area)
    for pad in geom.mounting_pads:
        pb = pad.bbox.inset(min(0.15, 0.2 * min(pad.w, pad.h)))
        parts.append(Box(pb.x0, pb.y0, pb.x1, pb.y1, 0.0, 0.25, "tin"))

    spec = BodySpec(
        _name(geom, "header", params),
        "header",
        _clean(
            params,
            pitch=defaults["pitch"],
            orientation=orientation,
            shrouded=shrouded,
            H=h,
            pin=pin,
            housing_x=housing.width,
            housing_y=housing.height,
        ),
        parts,
    )
    return _check_fits(spec, geom)


def _synth_header(params: Mapping[str, Any]) -> FootprintGeometry:
    positions = int(_num(params, "positions"))
    rows = int(_num(params, "rows", 1))
    pitch = _num(params, "pitch", 2.54)
    if positions < 1 or rows not in (1, 2):
        raise ParamError("positions= must be >= 1 and rows= 1 or 2")
    pads = [
        PadGeom(
            str(r * positions + i + 1),
            "thru_hole",
            i * pitch,
            r * pitch,
            1.7 * pitch / 2.54,
            1.7 * pitch / 2.54,
            1.0,
        )
        for r in range(rows)
        for i in range(positions)
    ]
    margin = _num(params, "margin", pitch / 2 if not params.get("shrouded") else 2.45)
    fab = BBox(-margin, -margin, (positions - 1) * pitch + margin, (rows - 1) * pitch + margin)
    name = (
        f"Header_{rows}x{positions:02d}_P{pitch}mm_{params.get('orientation', 'vertical').title()}"
    )
    lib = "Connector_JST" if params.get("shrouded") else "Connector_PinHeader"
    return FootprintGeometry(
        f"{lib}:{name}", name, pads=pads, fab=fab, courtyard=fab.inset(-0.5), attrs={"through_hole"}
    )


# ------------------------------------------------------------- placeholder


def build_placeholder(geom: FootprintGeometry, params: Mapping[str, Any]) -> BodySpec:
    """A courtyard-extruded box: shows *something*, still counted as a gap.

    The file name carries ``placeholder`` so :mod:`kicad_tools.pcb.model_audit`
    classifies it as ``placeholder`` (not covered), never ``resolved``.
    """
    area = geom.courtyard or geom.body_outline() or geom.pad_extent
    if area is None:
        raise ParamError("footprint has no courtyard, fab outline or pads to size a placeholder")
    inset = _num(params, "inset", 0.05)
    area = area.inset(inset)
    h = _num(params, "H", 1.0)
    spec = BodySpec(
        f"{geom.name or 'body'}_placeholder",
        "placeholder",
        _clean(params, H=h, inset=inset),
        [Box(area.x0, area.y0, area.x1, area.y1, 0.0, h, "placeholder")],
        placeholder=True,
    )
    return _check_fits(spec, geom)


# ------------------------------------------------------------------ registry

FAMILIES: dict[str, Family] = {
    f.name: f
    for f in (
        Family(
            "chip",
            "Two-terminal chip R/C/L/fuse/LED (NNNN_NNNNMetric)",
            (
                ParamDoc("L", "body length along the terminals"),
                ParamDoc("W", "body width"),
                ParamDoc("H", "body height", "size-code table"),
                ParamDoc("T", "end-terminal length", "min(0.25 L, 0.6)"),
            ),
            (
                r"^(Resistor_SMD|Capacitor_SMD|Inductor_SMD|Fuse|LED_SMD|Diode_SMD):"
                r"(R|C|L|Fuse|LED|D)_\d{4,5}_\d{4,5}Metric",
            ),
            build_chip,
        ),
        Family(
            "box",
            "Rectangular body (shunts, power inductors, modules) plus a height",
            (
                ParamDoc("H", "body height (required unless the name carries AxBxCmm)", "none"),
                ParamDoc("L", "body length along the pad field"),
                ParamDoc("W", "body width"),
                ParamDoc("terminals", "ends | pads | none", "ends for R_*, else pads"),
                ParamDoc("T", "end-terminal length (terminals=ends)", "_T<x>mm in the name"),
                ParamDoc("standoff", "terminal plate height (terminals=pads)", "0.1"),
            ),
            (r"^Resistor_SMD:R_Shunt_", r"^Inductor_SMD:", r"^Resistor_SMD:R_.*_\d+x\d+"),
            build_box,
        ),
        Family(
            "gullwing",
            "Gull-wing leads: SOIC/SSOP/TSSOP/MSOP, SOT-23 variants, QFP",
            (
                ParamDoc("H", "overall height", "package table"),
                ParamDoc("A1", "standoff", "0.1"),
                ParamDoc("b", "lead width", "0.75 pad width"),
                ParamDoc("c", "lead thickness", "0.15/0.2"),
                ParamDoc("pins/pitch/L/W/E", "synthesize without a footprint", "-"),
            ),
            (
                r"^Package_SO:",
                r"^Package_TO_SOT_SMD:(SOT-23|TSOT-23|SOT-323|SOT-353|SOT-363|SC-70|SC-74)",
                r"^Package_QFP:",
            ),
            build_gullwing,
        ),
        Family(
            "nolead",
            "QFN/DFN (and EP variants): terminals flush with the underside",
            (
                ParamDoc("H", "body height", "0.85 QFN / 0.75 DFN"),
                ParamDoc("terminal_h", "terminal thickness", "0.2"),
                ParamDoc("pins/pitch/L/W", "synthesize without a footprint", "-"),
            ),
            (r"^Package_DFN_QFN:",),
            build_nolead,
        ),
        Family(
            "header",
            "2.00/2.50/2.54 mm headers, shrouded (JST PH/XH style) or not",
            (
                ParamDoc("H", "housing height", "6.0/7.0 shrouded, 2.5 base"),
                ParamDoc("orientation", "vertical | horizontal", "from the name"),
                ParamDoc("shrouded", "true | false", "JST/Molex/Box -> true"),
                ParamDoc("pin", "square pin size", "0.64 / 0.5"),
                ParamDoc("positions/rows/pitch", "synthesize without a footprint", "-"),
            ),
            (
                r"^Connector_JST:",
                r"^Connector_PinHeader_(2\.00|2\.54)mm:",
                r"^Connector_PinSocket_(2\.00|2\.54)mm:",
            ),
            build_header,
        ),
    )
}


def get_family(name: str) -> Family:
    try:
        return FAMILIES[name]
    except KeyError:
        raise ParamError(
            f"unknown family {name!r}; expected one of {', '.join(sorted(FAMILIES))}"
        ) from None


def family_for_lib_id(lib_id: str) -> Family | None:
    for family in FAMILIES.values():
        if family.matches(lib_id):
            return family
    return None


def build_body(
    family: Family, geom: FootprintGeometry | None, params: Mapping[str, Any]
) -> BodySpec:
    """Run *family* on *geom* (or synthesize one from *params*)."""
    return family.build(geom, dict(params))
