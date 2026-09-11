"""Source-bound standard-pad/via mask geometry, not a clearance verdict.

Only native KiCad 10 default Gerber plotting is modeled. Unsupported features
make the snapshot incomplete; consumers must not promote it to a DRC pass.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools._shapely import require_shapely
from kicad_tools.core.geometry import rotate_pad_offset
from kicad_tools.schema.pcb import PCB

# Maximum chord error per circular construction, mm. Not a process tolerance.
CURVE_ERROR_MM = 0.000001


@dataclass
class MaskOpening:
    source_uuid: str
    layer: str
    kind: str
    margin_mm: float
    margin_source: str
    mask_defined: bool
    geometry: Any
    copper: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_uuid": self.source_uuid,
            "layer": self.layer,
            "kind": self.kind,
            "margin_mm": self.margin_mm,
            "margin_source": self.margin_source,
            "mask_defined": self.mask_defined,
            "geometry_wkt": self.geometry.wkt,
            "owning_copper_wkt": self.copper.wkt,
        }


@dataclass
class MaskGeometrySnapshot:
    source_sha256: str
    project_sha256: str | None
    rules_sha256: str | None
    openings: list[MaskOpening] = field(default_factory=list)
    unsupported: list[dict[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.unsupported

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_sha256": self.source_sha256,
            "project_sha256": self.project_sha256,
            "rules_sha256": self.rules_sha256,
            "coverage": "complete" if self.complete else "incomplete",
            "scope": "standard pad/via mask geometry; no clearance verdict",
            "curve_error_mm_per_construction": CURVE_ERROR_MM,
            "openings": [opening.to_dict() for opening in self.openings],
            "unsupported": self.unsupported,
        }


def _buffer(geometry: Any, distance: float) -> Any:
    if distance == 0:
        return geometry
    radius = abs(distance)
    angle = math.acos(max(-1.0, 1 - CURVE_ERROR_MM / radius))
    segments = max(16, math.ceil(math.pi / (4 * angle))) if angle else 4096
    return geometry.buffer(distance, quad_segs=segments)


def _pad_shape(pad: Any) -> Any:
    from shapely.geometry import LineString, Point, box  # type: ignore[import-untyped]

    width, height = pad.size
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise ValueError("nonpositive/nonfinite pad dimensions")
    if pad.shape == "rect":
        return box(-width / 2, -height / 2, width / 2, height / 2)
    if pad.shape == "circle":
        if width != height:
            raise ValueError("noncircular circle pad")
        return _buffer(Point(0, 0), width / 2)
    if pad.shape == "oval":
        radius = min(width, height) / 2
        dx, dy = width / 2 - radius, height / 2 - radius
        core = LineString([(-dx, -dy), (dx, dy)]) if dx or dy else Point(0, 0)
        return _buffer(core, radius)
    if pad.shape == "roundrect":
        ratio = pad.roundrect_rratio
        if not math.isfinite(ratio) or not 0 <= ratio <= 0.5:
            raise ValueError("invalid roundrect radius ratio")
        radius = ratio * min(width, height)
        dx, dy = width / 2 - radius, height / 2 - radius
        core = box(-dx, -dy, dx, dy) if dx and dy else LineString([(-dx, -dy), (dx, dy)])
        if not dx and not dy:
            core = Point(0, 0)
        return _buffer(core, radius)
    raise ValueError(f"unsupported pad shape {pad.shape}")


def _validate_structure(text: str) -> None:
    """The shared recovery parser tolerates EOF; snapshots require closed input."""
    depth = 0
    quoted = escaped = comment = False
    roots = 0
    for char in text:
        if comment:
            comment = char != "\n"
        elif quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char in ";#":
            comment = True
        elif char == '"':
            quoted = True
        elif char == "(":
            roots += depth == 0
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced PCB source")
        elif depth == 0 and not char.isspace():
            raise ValueError("Unexpected text outside PCB root")
    if depth or quoted or roots != 1:
        raise ValueError("Incomplete or multiple PCB roots")


def _raw_geometry_errors(tree: Any) -> list[str]:
    """Validate present source fields before schema recovery loses their validity."""
    errors: list[str] = []

    def numeric(node: Any, tag: str, lengths: set[int], *, positive: bool = False) -> None:
        fields = [child for child in node.children if child.name == tag]
        if not fields:
            return  # Absence keeps the native/schema default; malformed presence does not.
        if len(fields) != 1:
            errors.append(f"Repeated {node.name} {tag}")
            return
        field = fields[0]
        values = [field.get_float(i) for i in range(len(field.children))]
        if len(values) not in lengths or any(
            not atom.is_atom
            or value is None
            or not math.isfinite(value)
            or (positive and value <= 0)
            for atom, value in zip(field.children, values, strict=True)
        ):
            errors.append(f"Invalid {node.name} {tag}")
        elif tag == "roundrect_rratio" and not 0 <= values[0] <= 0.5:
            errors.append("Invalid pad roundrect_rratio")

    def scan(node: Any) -> None:
        if node.name in {"footprint", "module", "pad", "via"}:
            numeric(node, "at", {2} if node.name == "via" else {2, 3})
            numeric(node, "solder_mask_margin", {1})
        if node.name == "pad":
            numeric(node, "size", {2}, positive=True)
            numeric(node, "roundrect_rratio", {1})
        if node.name == "via":
            numeric(node, "size", {1}, positive=True)
        if node.name == "setup":
            numeric(node, "pad_to_mask_clearance", {1})
            numeric(node, "solder_mask_min_width", {1})
        if node.name in {"setup", "via"}:
            tenting = [child for child in node.children if child.name == "tenting"]
            if len(tenting) > 1:
                errors.append(f"Repeated {node.name} tenting")
            for settings in tenting:
                seen: set[str] = set()
                allowed = {"yes", "no", "none"} if node.name == "via" else {"yes", "no"}
                for side in settings.children:
                    if (
                        side.name not in {"front", "back"}
                        or side.name in seen
                        or len(side.children) != 1
                        or not side.children[0].is_atom
                        or side.get_string(0) not in allowed
                    ):
                        errors.append(f"Invalid {node.name} tenting")
                    seen.add(side.name)
        for child in node.children:
            if not child.is_atom:
                scan(child)

    scan(tree)
    return errors


def inspect_mask_geometry(pcb_path: str | Path) -> MaskGeometrySnapshot:
    """Inspect immutable source files; partial results always carry coverage gaps.

    Front/back mask openings only. Custom primitives, graphics/text, mask zones,
    minimum-web merging and advanced padstacks are deliberately not approximated.
    No global/manufacturer mask expansion or copper-clearance floor is invented.
    """
    require_shapely("soldermask geometry")
    from shapely.affinity import rotate, translate  # type: ignore[import-untyped]
    from shapely.geometry import Point

    path = Path(pcb_path)
    raw = path.read_bytes()
    project = path.with_suffix(".kicad_pro")
    project_raw = project.read_bytes() if project.exists() else None
    rules_path = path.with_suffix(".kicad_dru")
    rules_raw = rules_path.read_bytes() if rules_path.exists() else None
    snapshot = MaskGeometrySnapshot(
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha256(project_raw).hexdigest() if project_raw is not None else None,
        hashlib.sha256(rules_raw).hexdigest() if rules_raw is not None else None,
    )
    # Parse exactly the bytes hashed, avoiding a second, racy source read.
    from kicad_tools.sexp import parse_string

    text = raw.decode()
    _validate_structure(text)
    tree = parse_string(text)
    if tree.name != "kicad_pcb":
        raise ValueError("Expected kicad_pcb root")
    raw_errors = _raw_geometry_errors(tree)
    if raw_errors:
        snapshot.unsupported = [
            {"source_uuid": "", "feature": "source-geometry", "reason": reason}
            for reason in sorted(set(raw_errors))
        ]
        return snapshot
    pcb = PCB(tree, path)

    def unsupported(uuid: str, feature: str, reason: str) -> None:
        snapshot.unsupported.append({"source_uuid": uuid, "feature": feature, "reason": reason})

    if rules_raw is not None:
        unsupported("", "custom-rules", "Native custom rule evaluation is not implemented")

    if project_raw is not None:
        try:
            settings = json.loads(project_raw)["board"]["design_settings"]
            rules = settings.get("rules", {})
            if any(rules.get(key, 0) for key in ("solder_mask_clearance", "solder_mask_min_width")):
                unsupported(
                    "",
                    "project-mask-settings",
                    "Nonzero project mask settings require native resolution",
                )
        except (ValueError, KeyError, TypeError, AttributeError):
            unsupported("", "project-settings", "Cannot establish project mask settings")

    setup = pcb._sexp.find_child("setup")
    margin_node = setup.find_child("pad_to_mask_clearance") if setup else None
    board_margin = margin_node.get_float(0) if margin_node else 0.0
    if board_margin is None or not math.isfinite(board_margin):
        unsupported("", "board-margin", "Invalid board mask expansion")
        return snapshot
    if setup:
        minimum = setup.find_child("solder_mask_min_width")
        if minimum and minimum.get_float(0) != 0:
            unsupported(
                "", "mask-web-merging", "Nonzero mask minimum width requires merged native geometry"
            )

    # Raw traversal catches graphics and advanced features omitted by schema models.
    def scan(node: Any) -> None:
        layer = node.find_child("layer")
        layers = node.find_child("layers")
        if (
            layers
            and node.name not in {"pad", "footprint"}
            and any(str(atom.value) in {"F.Mask", "B.Mask", "*.Mask"} for atom in layers.children)
        ):
            unsupported("", node.name, "Multi-layer mask feature not implemented")
        if node.name in {"segment", "arc", "via"} and node.find_child("solder_mask_margin"):
            unsupported("", node.name, "Per-track/via mask expansion not implemented")
        if (
            node.name not in {"pad", "footprint"}
            and layer
            and layer.get_string(0) in {"F.Mask", "B.Mask"}
        ):
            identity = node.find_child("uuid")
            unsupported(
                identity.get_string(0) if identity else "",
                node.name,
                "Mask graphic/text/zone geometry not implemented",
            )
        for child in node.children:
            if not child.is_atom:
                scan(child)

    scan(pcb._sexp)
    for warning in pcb.parse_warnings:
        unsupported("", "source-parse", warning)
    seen: set[str] = set()
    for fp in pcb.footprints:
        fp_node = fp._sexp_node
        fp_margin_node = fp_node.find_child("solder_mask_margin") if fp_node else None
        for pad in fp.pads:
            sides = [
                side
                for side in ("F", "B")
                if f"{side}.Mask" in pad.layers or "*.Mask" in pad.layers
            ]
            if not sides:
                continue
            if not pad.uuid or pad.uuid in seen:
                unsupported(pad.uuid, "source-identity", "Missing or duplicate pad UUID")
            seen.add(pad.uuid)
            node = pad._sexp_node
            if node and any(
                node.find_child(name) is not None
                for name in (
                    "primitives",
                    "chamfer",
                    "chamfer_ratio",
                    "padstack",
                    "options",
                    "tenting",
                )
            ):
                unsupported(
                    pad.uuid, "advanced-pad", "Custom/chamfered/padstack geometry not implemented"
                )
                continue
            margin = pad.solder_mask_margin
            source = "pad"
            if margin is None:
                margin = fp_margin_node.get_float(0) if fp_margin_node else board_margin
                source = (
                    "footprint"
                    if fp_margin_node
                    else "board"
                    if margin_node
                    else "native-default-zero"
                )
            if margin is None or not math.isfinite(margin):
                unsupported(pad.uuid, "mask-margin", "Invalid mask expansion")
                continue
            try:
                copper = _pad_shape(pad)
            except ValueError as exc:
                unsupported(pad.uuid, "pad-geometry", str(exc))
                continue
            opening = _buffer(copper, margin)
            dx, dy = rotate_pad_offset(*pad.position, fp.rotation)
            x, y = fp.position[0] + dx, fp.position[1] + dy
            # KiCad pad angles are board-frame; y-down coordinates negate rotation.
            copper = translate(rotate(copper, -pad.rotation, origin=(0, 0)), x, y)
            opening = translate(rotate(opening, -pad.rotation, origin=(0, 0)), x, y)
            for side in sides:
                if f"{side}.Cu" not in pad.layers and "*.Cu" not in pad.layers:
                    unsupported(
                        pad.uuid,
                        "non-copper-pad",
                        "Mask-only pad inheritance needs native resolution",
                    )
                    continue
                snapshot.openings.append(
                    MaskOpening(
                        pad.uuid, f"{side}.Mask", "pad", margin, source, margin < 0, opening, copper
                    )
                )

    for via in pcb.vias:
        if not via.uuid or via.uuid in seen:
            unsupported(via.uuid, "source-identity", "Missing or duplicate via UUID")
        seen.add(via.uuid)
        if set(via.layers) != {"F.Cu", "B.Cu"}:
            unsupported(via.uuid, "via-stack", "Blind/buried via geometry not implemented")
            continue
        if not all(math.isfinite(v) for v in (*via.position, via.size)) or via.size <= 0:
            unsupported(via.uuid, "via-geometry", "Invalid via position or diameter")
            continue
        copper = _buffer(Point(*via.position), via.size / 2)
        for side, setting in (("F", via.tenting_front), ("B", via.tenting_back)):
            default = getattr(pcb.setup, "tenting_front" if side == "F" else "tenting_back")
            if setting not in {None, "none", "yes", "no"}:
                unsupported(via.uuid, "tenting", "Unrecognized via tenting value")
                continue
            tented = setting == "yes" if setting in {"yes", "no"} else default is not False
            if not tented:
                snapshot.openings.append(
                    MaskOpening(
                        via.uuid,
                        f"{side}.Mask",
                        "via",
                        board_margin,
                        "board",
                        board_margin < 0,
                        _buffer(copper, board_margin),
                        copper,
                    )
                )
    snapshot.openings.sort(key=lambda item: (item.layer, item.source_uuid))
    snapshot.unsupported.sort(
        key=lambda item: (item["source_uuid"], item["feature"], item["reason"])
    )
    return snapshot
