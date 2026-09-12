"""Immutable full-context native object attribution for mask exposure checking."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mask_export_geometry import MaskExportOptions, _inventory, inspect_exported_mask_geometry
from .mask_geometry import _validate_structure


@dataclass
class AttributedMaskGeometry:
    exported: Any
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def inspect_attributed_mask_geometry(
    path,
    *,
    options=None,
    native_command=None,
    native_python_command=None,
    artifact_dir=None,
    scratch_dir=None,
):
    """Capture one source set and read UUID polygons without editing that board.

    KiCad's object shape APIs retain overlapping contributors independently.
    Full-layer native export parity is an additional coverage check, never an
    inference of an object's ownership from subtraction of a copper union.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    from kicad_tools.sexp import parse_string

    path = Path(path)
    captured = {
        suffix: path.with_suffix(suffix).read_bytes() if path.with_suffix(suffix).exists() else None
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
    }
    raw = captured[".kicad_pcb"]
    if raw is None:
        raise ValueError("Missing PCB source")
    _validate_structure(raw.decode())
    tree = parse_string(raw.decode())
    inventory = _inventory(tree)
    options = options or MaskExportOptions(standalone_sources=False)
    # Separate standalone derivatives are unnecessary: attribution is native
    # original-context object geometry, including all overlapping neighbors.
    from dataclasses import replace

    options = replace(options, standalone_sources=False)
    with tempfile.TemporaryDirectory(prefix="kct-mask-objects-", dir=scratch_dir) as temp:
        stage = Path(temp)
        board = stage / path.name
        for suffix, content in captured.items():
            if content is not None:
                board.with_suffix(suffix).write_bytes(content)
        exported = inspect_exported_mask_geometry(
            board,
            options=options,
            native_command=native_command,
            artifact_dir=artifact_dir,
            scratch_dir=scratch_dir,
        )
        result = AttributedMaskGeometry(exported)
        result.errors.extend(f"{e['feature']}: {e['reason']}" for e in exported.unsupported)
        # Native pcbnew LoadBoard has no exposed general DRC-expression loader.
        # These rule constraints do not change plotted shape. Anything else is
        # an explicit context gap, even if a whole-layer union happens to agree.
        if captured[".kicad_dru"]:
            rule_tree = parse_string("(rules " + captured[".kicad_dru"].decode() + ")")
            safe = {
                "clearance",
                "physical_clearance",
                "edge_clearance",
                "hole_clearance",
                "hole_to_hole",
                "track_width",
                "hole_size",
                "via_diameter",
                "annular_width",
            }
            for constraint in rule_tree.find_all("constraint"):
                if constraint.get_string(0) not in safe:
                    result.errors.append(
                        "Native object rule context unsupported: " + str(constraint.get_string(0))
                    )
        # LoadBoard does not expose the CLI project/--define-var text resolver.
        # Fail visibly for variable-bearing plotted text rather than use a
        # substituted or context-free glyph and rely on an overlapping union.
        for feature in inventory:
            if feature["kind"] in ("property", "fp_text", "gr_text", "fp_text_box", "gr_text_box"):
                atoms = feature["node"].get_atoms()
                if any(isinstance(v, str) and "${" in v for v in atoms):
                    result.errors.append(
                        feature["source_uuid"]
                        + ": native plotted text-variable context unsupported"
                    )
        parents = {}
        for parent in tree.find_all("footprint") + tree.find_all("module"):
            field = parent.find_child("uuid")
            if field:
                parents[field.get_string(0)] = parent
        setup = tree.find_child("setup")
        board_margin = setup.find_child("pad_to_mask_clearance") if setup else None
        worker = Path(__file__).with_name("_native_mask_objects.py")
        staged_worker = stage / worker.name
        worker_raw = worker.read_bytes()
        staged_worker.write_bytes(worker_raw)
        request = stage / "request.json"
        request.write_text(json.dumps({"source_uuids": [f["source_uuid"] for f in inventory]}))
        output = stage / "objects.json"
        command = list(native_python_command or ["/usr/bin/python3"])
        process = subprocess.run(
            [*command, str(staged_worker), str(board), str(request), str(output)],
            capture_output=True,
            text=True,
            timeout=options.timeout_seconds,
        )
        if process.returncode or not output.exists():
            result.errors.append(
                "Native per-object geometry unavailable: " + process.stderr.strip()
            )
            return result
        data = _read_worker_output(output.read_text(), {f["source_uuid"] for f in inventory})
        if data.get("source_sha256") != exported.source_sha256 or not str(
            data.get("native_version", "")
        ).startswith("10."):
            result.errors.append("Native object/source/version binding mismatch")
            return result
        if str(data["native_version"]).split()[0] != exported.native_version.split()[0]:
            result.errors.append("Native object/export versions differ")
        result.errors.extend(data["errors"])
        result.provenance = {
            "worker_sha256": _digest(worker_raw),
            "native_python_command": command,
            "native_version": data["native_version"],
            "objects_sha256": _digest(output.read_bytes()),
            "max_error_mm_per_construction": data["max_error_mm"],
            "attribution": "native full-board object UUID; no neighbor removal",
        }
        for feature in inventory:
            identity = feature["source_uuid"]
            item = data["objects"].get(identity)
            if item is None:
                continue
            geometries = {}
            for layer, regions in item["layers"].items():
                polygons = [Polygon(region["shell"], region["holes"]) for region in regions]
                if any(not p.is_valid for p in polygons):
                    result.errors.append(f"{identity}/{layer}: invalid native object polygon")
                    continue
                geometries[layer] = unary_union(polygons)
            own_margin = feature["node"].find_child("solder_mask_margin")
            parent = parents.get(feature["parent_uuid"])
            parent_margin = parent.find_child("solder_mask_margin") if parent else None
            if own_margin is not None:
                margin_source = (
                    "object:" + identity + ":solder_mask_margin=" + str(own_margin.get_value(0))
                )
            elif parent_margin is not None:
                margin_source = (
                    "footprint:"
                    + feature["parent_uuid"]
                    + ":solder_mask_margin="
                    + str(parent_margin.get_value(0))
                )
            elif board_margin is not None:
                margin_source = "board:pad_to_mask_clearance=" + str(board_margin.get_value(0))
            else:
                margin_source = "native-default (no explicit object/footprint/board margin)"
            result.objects[identity] = {
                **{k: v for k, v in feature.items() if k != "node"},
                "net": item["net"],
                "layers_geometry": geometries,
                "margin_mm": item["margin_mm"],
                "margin_source": margin_source,
            }
        # Verify source and all sidecar bytes (including their absence) after
        # both native operations. The author files themselves were never passed.
        for suffix, content in captured.items():
            sibling = board.with_suffix(suffix)
            if (sibling.read_bytes() if sibling.exists() else None) != content:
                result.errors.append("Native operation mutated captured source: " + suffix)
        for suffix, content in captured.items():
            current = path.with_suffix(suffix)
            if (current.read_bytes() if current.exists() else None) != content:
                result.errors.append("Original source set changed during inspection: " + suffix)
        if staged_worker.read_bytes() != worker_raw:
            result.errors.append("Native worker bytes changed during invocation")
        if artifact_dir:
            destination = Path(artifact_dir)
            shutil.copy2(output, destination / "native-objects.json")
            (destination / "object-attribution.json").write_text(
                json.dumps(result.provenance, indent=2)
            )
        # A few nanometres cover native and Gerber polygon chord error plus
        # coordinate quantization. This is geometric uncertainty, not a rule waiver.
        budget = 0.000004
        for layer in ("F.Cu", "B.Cu"):
            if layer not in exported.layers:
                continue
            union = unary_union(
                [
                    o["layers_geometry"][layer]
                    for o in result.objects.values()
                    if layer in o["layers_geometry"]
                ]
            )
            native = exported.layers[layer]
            if (
                not union.difference(native.buffer(budget)).is_empty
                or not native.difference(union.buffer(budget)).is_empty
            ):
                result.errors.append(layer + ": native per-object/full-layer material mismatch")
        return result


def _read_worker_output(text, identities):
    """Validate external native-worker data before constructing any geometry."""
    import math

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate native JSON key: " + key)
            result[key] = value
        return result

    data = json.loads(text, object_pairs_hook=unique)
    if data["schema"] != "kct.native-mask-objects.v1":
        raise ValueError("Unsupported native worker schema/error budget")
    error = data["max_error_mm"]
    if (
        isinstance(error, bool)
        or not isinstance(error, (int, float))
        or not math.isfinite(error)
        or error < 0
    ):
        raise ValueError("Invalid native construction error")
    if not isinstance(data["errors"], list) or any(not isinstance(e, str) for e in data["errors"]):
        raise ValueError("Invalid native coverage errors")
    missing = identities - set(data["objects"])
    if set(data["objects"]) - identities:
        raise ValueError("Unrequested native object identity")
    if missing:
        data["errors"].extend("Missing native object: " + identity for identity in sorted(missing))
    for identity, item in data["objects"].items():
        if not isinstance(item["net"], str) or not isinstance(item["kind"], str):
            raise ValueError("Invalid native object metadata")
        if set(item["layers"]) - {"F.Cu", "B.Cu", "F.Mask", "B.Mask"}:
            raise ValueError("Unknown native material layer")
        if set(item["margin_mm"]) - {"F.Mask", "B.Mask"}:
            raise ValueError("Unknown native mask margin side")
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in item["margin_mm"].values()
        ):
            raise ValueError("Invalid native mask expansion")
        for regions in item["layers"].values():
            for region in regions:
                for ring in [region["shell"], *region["holes"]]:
                    if len(ring) < 3:
                        raise ValueError("Degenerate native polygon")
                    for point in ring:
                        if len(point) != 2 or any(
                            isinstance(v, bool)
                            or not isinstance(v, (int, float))
                            or not math.isfinite(v)
                            for v in point
                        ):
                            raise ValueError("Invalid native polygon coordinate")
    return data
