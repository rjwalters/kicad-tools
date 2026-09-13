"""Source-bound native-export mask/copper geometry for advanced KiCad features.

The complete plotted layer is authoritative, including merged openings. Object
inventory is provenance, not a claim that merged material belongs to one pad.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import tempfile
import uuid as uuid_module
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from .gerber_geometry import GERBER_CHORD_ERROR_MM, GerberGeometryError, parse_gerber_geometry
from .mask_geometry import _validate_structure

LAYERS = ("F.Mask", "B.Mask", "F.Cu", "B.Cu")


@dataclass(frozen=True)
class MaskExportOptions:
    """Native plot policy; stored board options are an explicit separate mode."""

    board_plot_params: bool = False
    variables: tuple[tuple[str, str], ...] = ()
    timeout_seconds: float = 120
    standalone_sources: bool = True


@dataclass
class ExportedMaskGeometry:
    source_sha256: str
    project_sha256: str | None
    rules_sha256: str | None
    native_version: str
    export_identity: dict[str, Any]
    features: list[dict[str, Any]] = field(default_factory=list)
    layers: dict[str, Any] = field(default_factory=dict)
    gerber_sha256: dict[str, str] = field(default_factory=dict)
    unsupported: list[dict[str, str]] = field(default_factory=list)
    source_geometries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.unsupported and set(self.layers) == set(LAYERS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_sha256": self.source_sha256,
            "project_sha256": self.project_sha256,
            "rules_sha256": self.rules_sha256,
            "native_version": self.native_version,
            "export_identity": self.export_identity,
            "features": self.features,
            "coverage": "complete" if self.complete else "incomplete",
            "scope": "native exported mask and copper; no clearance or exposure verdict",
            "coordinate_frame": "native plot coordinates in mm, y down",
            "curve_error_mm_per_construction": GERBER_CHORD_ERROR_MM,
            "layers_wkt": {k: v.wkt for k, v in sorted(self.layers.items())},
            "gerber_sha256": self.gerber_sha256,
            "unsupported": self.unsupported,
            "source_geometries": {
                key: {
                    **{k: v for k, v in value.items() if k != "layers"},
                    "layers_wkt": {k: v.wkt for k, v in value["layers"].items()},
                }
                for key, value in self.source_geometries.items()
            },
        }


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inventory(tree):
    result = []
    object_tags = {
        "pad",
        "via",
        "segment",
        "arc",
        "zone",
        "property",
        "fp_text",
        "gr_text",
        "fp_text_box",
        "gr_text_box",
    }

    def visit(node, parent="", parent_placement=None):
        if node.name in {"footprint", "module"}:
            identity = node.find_child("uuid")
            parent = identity.get_string(0) if identity else ""
            at = node.find_child("at")
            parent_placement = {
                "at": at.get_atoms() if at else [0, 0],
                "layer": node.find_child("layer").get_string(0) if node.find_child("layer") else "",
                "coordinate_frame": "board",
            }
        is_object = node.name in object_tags or node.name.startswith(("fp_", "gr_"))
        if is_object:
            layer = node.find_child("layer")
            layers = node.find_child("layers")
            names = [layer.get_string(0)] if layer else []
            if layers:
                names += [str(c.value) for c in layers.children if c.is_atom]
            # Property metadata with no physical layer is not plotted geometry.
            if any(name in {*LAYERS, "*.Cu", "*.Mask"} for name in names):
                identity = node.find_child("uuid")
                uuid = (identity.get_string(0) or "") if identity else ""
                kind = node.name
                if node.name == "pad":
                    kind = "pad:" + (node.get_string(2) or "unknown")
                    if node.find_child("padstack"):
                        kind += ":padstack"
                    if node.find_child("chamfer"):
                        kind += ":chamfer"
                result.append(
                    {
                        "source_uuid": uuid,
                        "parent_uuid": parent,
                        "kind": kind,
                        "layers": sorted(names),
                        "node": node,
                        "authored_at": node.find_child("at").get_atoms()
                        if node.find_child("at")
                        else None,
                        "position_frame": "footprint-local" if parent_placement else "board",
                        "angle_frame": "board"
                        if node.name in {"pad", "fp_text", "property", "gr_text"}
                        else None,
                        "parent_placement": parent_placement,
                    }
                )
                # Nested custom primitives share the pad UUID; they are not
                # independent source objects or parent-frame board graphics.
                return
        for child in node.children:
            if not child.is_atom:
                visit(child, parent, parent_placement)

    visit(tree)
    return result


def inspect_exported_mask_geometry(
    pcb_path: str | Path,
    *,
    options: MaskExportOptions | None = None,
    native_command: Sequence[str] | None = None,
    artifact_dir: str | Path | None = None,
    scratch_dir: str | Path | None = None,
) -> ExportedMaskGeometry:
    """Export immutable source copies and reconstruct full F/B mask and copper.

    ``native_command`` may be an executable or a pinned container invocation;
    the latter must mount the temporary directory at the same absolute path.
    ``artifact_dir`` retains exact native outputs and the source-bound manifest.
    No zone refill, source write, manufacturer policy or exposure waiver occurs.
    """
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.sexp import parse_string

    options = options or MaskExportOptions()
    if not math.isfinite(options.timeout_seconds) or options.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if native_command is None:
        executable = find_kicad_cli()
        if executable is None:
            raise ValueError("Native KiCad is required for exported mask geometry")
        native_command = [str(executable)]
    if not native_command:
        raise ValueError("Empty native command")
    path = Path(pcb_path)
    raw = path.read_bytes()
    text = raw.decode()
    _validate_structure(text)
    tree = parse_string(text)
    if tree.name != "kicad_pcb":
        raise ValueError("Expected kicad_pcb root")
    siblings = {
        suffix: path.with_suffix(suffix).read_bytes() if path.with_suffix(suffix).exists() else None
        for suffix in (".kicad_pro", ".kicad_dru")
    }
    version = subprocess.run(
        [*native_command, "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=options.timeout_seconds,
    ).stdout.strip()
    args = [
        "pcb",
        "export",
        "gerbers",
        "--layers",
        ",".join(LAYERS),
        "--disable-aperture-macros",
        "--no-protel-ext",
        "--precision",
        "6",
    ]
    if options.board_plot_params:
        args.append("--board-plot-params")
    for key, value in sorted(options.variables):
        args += ["--define-var", f"{key}={value}"]
    snapshot = ExportedMaskGeometry(
        _sha(raw),
        _sha(siblings[".kicad_pro"]) if siblings[".kicad_pro"] is not None else None,
        _sha(siblings[".kicad_dru"]) if siblings[".kicad_dru"] is not None else None,
        version,
        {
            "command": list(native_command),
            "arguments": args,
            "profile": "macro-free-native-gerber",
            "board_plot_params": options.board_plot_params,
            "variables": sorted(options.variables),
        },
    )
    command_file = Path(native_command[0])
    if command_file.is_file():
        snapshot.export_identity["command_file_sha256"] = _sha(command_file.read_bytes())
    if not re.match(r"^10\.", version):
        snapshot.unsupported.append(
            {
                "source_uuid": "",
                "feature": "native-version",
                "reason": "Export reconstruction profile is qualified for KiCad 10 only",
            }
        )
    inventory = _inventory(tree)
    seen = set()
    for feature in inventory:
        snapshot.features.append({k: v for k, v in feature.items() if k != "node"})
        uuid = feature["source_uuid"]
        try:
            canonical = str(uuid_module.UUID(uuid))
        except ValueError:
            canonical = ""
        if not canonical or canonical != uuid.lower() or canonical in seen:
            snapshot.unsupported.append(
                {
                    "source_uuid": uuid,
                    "feature": "source-identity",
                    "reason": "Missing, malformed or duplicate source UUID",
                }
            )
        seen.add(canonical)
    snapshot.features.sort(key=lambda f: (f["source_uuid"], f["kind"]))
    # KiCad 10 deliberately does not strict-parse padstack layer fields. Reject
    # unknown fields here so native's permissive loader cannot erase coverage.
    stack_fields = {
        "shape",
        "size",
        "offset",
        "rect_delta",
        "roundrect_rratio",
        "chamfer_ratio",
        "chamfer",
        "thermal_bridge_width",
        "thermal_gap",
        "thermal_bridge_angle",
        "zone_connect",
        "clearance",
        "tenting",
        "options",
        "primitives",
    }
    for feature in inventory:
        stack = feature["node"].find_child("padstack")
        if stack:
            for node in stack.children:
                if node.is_atom or node.name not in {"mode", "layer"}:
                    snapshot.unsupported.append(
                        {
                            "source_uuid": feature["source_uuid"],
                            "feature": "padstack",
                            "reason": "Unknown padstack field",
                        }
                    )
                elif node.name == "layer":
                    for value in node.children[1:]:
                        if value.name == "options":
                            allowed = {
                                "anchor": {"circle", "rect"},
                                "clearance": {"outline", "convexhull"},
                            }
                            for option in value.children:
                                if (
                                    option.name not in allowed
                                    or len(option.children) != 1
                                    or option.get_string(0) not in allowed[option.name]
                                ):
                                    snapshot.unsupported.append(
                                        {
                                            "source_uuid": feature["source_uuid"],
                                            "feature": "padstack",
                                            "reason": f"Unknown padstack option {option.name}",
                                        }
                                    )
                        if value.is_atom or value.name not in stack_fields:
                            snapshot.unsupported.append(
                                {
                                    "source_uuid": feature["source_uuid"],
                                    "feature": "padstack",
                                    "reason": f"Unknown layer field {value.name}",
                                }
                            )

    with tempfile.TemporaryDirectory(prefix="kct-mask-export-", dir=scratch_dir) as temp:
        stage = Path(temp)
        staged = stage / path.name
        plot_raw = raw
        if options.board_plot_params:
            # Board plot parameters override CLI serialization flags in KiCad.
            # Disable macros in an explicitly hashed representation-only copy;
            # keep all geometry-affecting plot settings and the original source.
            plot_tree = parse_string(text)
            setup = plot_tree.find_child("setup")
            if setup is None:
                setup = plot_tree.add(parse_string("(setup)"))
            params = setup.find_child("pcbplotparams")
            if params is None:
                params = setup.add(parse_string("(pcbplotparams)"))
            macros = params.find_child("disableapertmacros")
            if macros is None:
                params.add(parse_string("(disableapertmacros true)"))
            else:
                macros.set_atom(0, "true")
            plot_raw = plot_tree.to_string().encode()
            snapshot.export_identity["representation_overrides"] = {"disableapertmacros": True}
        snapshot.export_identity["plot_profile_source_sha256"] = _sha(plot_raw)
        staged.write_bytes(plot_raw)
        for suffix, content in siblings.items():
            if content is not None:
                staged.with_suffix(suffix).write_bytes(content)
        output = stage / "gerbers"
        completed = subprocess.run(
            [*native_command, *args, "-o", str(output), str(staged)],
            capture_output=True,
            text=True,
            timeout=options.timeout_seconds,
        )
        snapshot.export_identity["stdout"] = re.sub(re.escape(temp), "<scratch>", completed.stdout)
        snapshot.export_identity["stderr"] = re.sub(re.escape(temp), "<scratch>", completed.stderr)
        if completed.returncode or re.search(r"\b(error|warning)\b", completed.stderr, re.I):
            snapshot.unsupported.append(
                {
                    "source_uuid": "",
                    "feature": "native-export",
                    "reason": f"Native export returned {completed.returncode}: {snapshot.export_identity['stderr']}",
                }
            )
        if staged.read_bytes() != plot_raw or any(
            content is not None and staged.with_suffix(suffix).read_bytes() != content
            for suffix, content in siblings.items()
        ):
            snapshot.unsupported.append(
                {
                    "source_uuid": "",
                    "feature": "source-mutation",
                    "reason": "Native export changed its staged source inputs",
                }
            )
        for layer in LAYERS:
            extension = {"F.Mask": ".gts", "B.Mask": ".gbs", "F.Cu": ".gtl", "B.Cu": ".gbl"}[layer]
            files = list(output.glob(f"*-{layer.replace('.', '_')}.gbr")) + list(
                output.glob(f"*{extension}")
            )
            if len(files) != 1:
                snapshot.unsupported.append(
                    {
                        "source_uuid": "",
                        "feature": layer,
                        "reason": "Missing or ambiguous native layer export",
                    }
                )
                continue
            content = files[0].read_bytes()
            snapshot.gerber_sha256[layer] = _sha(content)
            try:
                snapshot.layers[layer] = parse_gerber_geometry(content.decode()).geometry
            except (GerberGeometryError, ValueError) as exc:
                snapshot.unsupported.append(
                    {"source_uuid": "", "feature": layer, "reason": str(exc)}
                )
            if artifact_dir:
                destination = Path(artifact_dir)
                destination.mkdir(parents=True, exist_ok=True)
                (destination / f"{path.stem}-{layer.replace('.', '_')}.gbr").write_bytes(content)
        if options.standalone_sources and snapshot.complete:
            for feature in inventory:
                if not (
                    feature["kind"].startswith("pad:")
                    and any(word in feature["kind"] for word in ("custom", "chamfer", "padstack"))
                ):
                    continue
                identity = feature["source_uuid"]
                derivative = parse_string(text)
                removable = {
                    id(item["node"])
                    for item in _inventory(derivative)
                    if item["source_uuid"] != identity
                }

                def prune(node):
                    kept = []
                    for child in node.children:
                        if id(child) in removable:
                            # Preserve reference/value metadata for native custom
                            # rule matching, hiding only the derivative's text.
                            is_reference = child.name == "fp_text" and child.get_string(0) in {
                                "reference",
                                "value",
                            }
                            is_property = child.name == "property" and child.get_string(0) in {
                                "Reference",
                                "Value",
                            }
                            if is_reference or is_property:
                                if is_property:
                                    child.add(parse_string("(hide yes)"))
                                else:
                                    effects = child.find_child("effects")
                                    if effects:
                                        effects.add("hide")
                                kept.append(child)
                            continue
                        if not child.is_atom:
                            prune(child)
                        kept.append(child)
                    node.children[:] = kept

                prune(derivative)
                source_dir = stage / identity
                source_dir.mkdir()
                source_path = source_dir / path.name
                source_path.write_text(derivative.to_string())
                for suffix, content in siblings.items():
                    if content is not None:
                        source_path.with_suffix(suffix).write_bytes(content)
                part = inspect_exported_mask_geometry(
                    source_path,
                    options=replace(options, standalone_sources=False),
                    native_command=native_command,
                    scratch_dir=scratch_dir,
                    artifact_dir=Path(artifact_dir) / "standalone" / identity
                    if artifact_dir
                    else None,
                )
                if not part.complete:
                    snapshot.unsupported.append(
                        {
                            "source_uuid": identity,
                            "feature": "standalone-export",
                            "reason": str(part.unsupported),
                        }
                    )
                    continue
                snapshot.source_geometries[identity] = {
                    "semantics": "standalone derivative; excludes neighbors and merged bridges",
                    "derivative_sha256": part.source_sha256,
                    "gerber_sha256": part.gerber_sha256,
                    "layers": part.layers,
                }
    snapshot.unsupported.sort(key=lambda x: (x["source_uuid"], x["feature"], x["reason"]))
    if artifact_dir:
        Path(artifact_dir).mkdir(parents=True, exist_ok=True)
        source_archive = Path(artifact_dir) / "source"
        source_archive.mkdir(exist_ok=True)
        (source_archive / path.name).write_bytes(raw)
        for suffix, content in siblings.items():
            if content is not None:
                (source_archive / path.with_suffix(suffix).name).write_bytes(content)
        (Path(artifact_dir) / "mask-geometry.json").write_text(
            json.dumps(snapshot.to_dict(), indent=2) + "\n"
        )
    return snapshot
