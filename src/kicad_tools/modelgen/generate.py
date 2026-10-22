"""Orchestrate ``kct models generate``: resolve, build, write, record provenance.

Every generated body gets a ``<stem>.provenance.json`` sidecar (generator,
version, backend, family, parameters, source footprint and its sha256, the
STEP sha256 and the license) and the same facts in the STEP header's
``FILE_DESCRIPTION`` (issue #7687).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .families import (
    FAMILIES,
    MATERIALS,
    BodySpec,
    Box,
    ParamError,
    build_body,
    build_placeholder,
    family_for_lib_id,
    get_family,
)
from .footprint_geom import (
    FootprintGeometry,
    find_footprint_file,
    load_footprint_file,
    load_footprint_from_pcb,
)

__all__ = [
    "CACHE_ENV_VAR",
    "LICENSE_OURS",
    "ExtraMissingError",
    "GenerateResult",
    "default_output_dir",
    "generate_model",
    "register_local_model",
]

#: Overrides the default output directory for generated bodies.
CACHE_ENV_VAR = "KCT_GENERATED_3D_DIR"

LICENSE_OURS = (
    "original work of the kicad-tools project (MIT, same as this repository); "
    "nominal-envelope geometry from the footprint and parameters, no vendor, "
    "EasyEDA or KiCad-generator geometry"
)

PROVENANCE_SUFFIX = ".provenance.json"


class ExtraMissingError(RuntimeError):
    """The CadQuery extra is required but not installed."""


@dataclass
class GenerateResult:
    path: Path
    provenance_path: Path
    provenance: dict[str, Any]
    spec: BodySpec | None = None
    registered: Path | None = None
    attached: list[str] = field(default_factory=list)


def default_output_dir() -> Path:
    root = os.environ.get(CACHE_ENV_VAR)
    return Path(root) if root else Path.home() / ".cache" / "kicad-tools" / "generated-3d"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", name).strip("_") or "body"


def _version() -> str:
    try:
        from kicad_tools import __version__

        return str(__version__)
    except ImportError:  # pragma: no cover - package always importable here
        return "unknown"


def resolve_footprint(
    target: str,
    *,
    footprint: Path | None = None,
    pcb: Path | None = None,
    reference: str | None = None,
) -> FootprintGeometry | None:
    """Footprint geometry for *target*, or ``None`` for a bare family name."""
    is_family = target in FAMILIES
    if footprint is not None:
        lib_id = None if is_family else target
        return load_footprint_file(footprint, lib_id=lib_id)
    if pcb is not None and (reference is not None or not is_family):
        return load_footprint_from_pcb(
            pcb, reference=reference, lib_id=None if (is_family or reference) else target
        )
    if is_family:
        return None
    if ":" not in target:
        raise ParamError(
            f"{target!r} is neither a family ({', '.join(sorted(FAMILIES))}) "
            "nor a Library:Footprint lib_id"
        )
    path = find_footprint_file(target, project_dir=pcb.parent if pcb else None)
    if path is None:
        raise LookupError(
            f"footprint {target} not found in the KiCad libraries; "
            "pass --footprint FILE or --pcb BOARD [--ref REF]"
        )
    return load_footprint_file(path, lib_id=target)


def register_local_model(out_dir: Path, lib_id: str, filename: str) -> Path:
    """Add ``lib_id -> filename`` to ``<out_dir>/local_models.json``.

    That manifest is what :func:`kicad_tools.pcb.models3d.attach_local_models`
    reads (issue #7685) when *out_dir* is a project's ``3dshapes`` directory.
    """
    manifest = out_dir / "local_models.json"
    data: dict[str, str] = {}
    if manifest.is_file():
        data = json.loads(manifest.read_text())
    data[lib_id] = filename
    manifest.write_text(json.dumps(dict(sorted(data.items())), indent=2) + "\n")
    return manifest


def _description(prov: dict[str, Any]) -> list[str]:
    fp = prov.get("footprint") or {}
    return [
        f"{prov['generator']} {prov['generator_version']}; backend {prov['backend']}",
        f"family {prov['family']}{' (placeholder)' if prov['placeholder'] else ''}",
        "params " + json.dumps(prov["params"], sort_keys=True, separators=(",", ":")),
        f"footprint {fp.get('lib_id', '-')} sha256 {fp.get('sha256', '-') or '-'}",
        f"license: {prov['license']}",
    ]


def generate_model(
    target: str,
    *,
    params: dict[str, Any] | None = None,
    family: str | None = None,
    footprint: Path | None = None,
    pcb: Path | None = None,
    reference: str | None = None,
    placeholder: bool = False,
    out_dir: Path | None = None,
    name: str | None = None,
    register: bool = False,
    attach: bool = False,
) -> GenerateResult:
    """Generate one STEP body; see ``kct models generate --help``."""
    params = dict(params or {})
    if not placeholder:
        from . import cadquery_backend

        if not cadquery_backend.available():
            raise ExtraMissingError(cadquery_backend.INSTALL_HINT)
    geom = resolve_footprint(target, footprint=footprint, pcb=pcb, reference=reference)

    if attach:
        if pcb is None:
            raise ParamError("--attach needs --pcb (the board whose 3dshapes/ receives the body)")
        shapes = pcb.parent / "3dshapes"
        if out_dir is not None and out_dir.resolve() != shapes.resolve():
            raise ParamError(
                f"--attach writes to {shapes} (what ${{KIPRJMOD}}/3dshapes resolves to)"
            )
        out_dir = shapes
        register = True

    if placeholder:
        if geom is None:
            raise ParamError("placeholder mode sizes the box from a footprint; give a lib_id")
        spec = build_placeholder(geom, params)
        backend = "kicad-tools builtin STEP writer"
    else:
        if family is not None:
            fam = get_family(family)
        elif target in FAMILIES:
            fam = FAMILIES[target]
        else:
            matched = family_for_lib_id(geom.lib_id if geom else target)
            if matched is None:
                raise ParamError(
                    f"no parametric family matches {target}; pass --family "
                    f"({', '.join(sorted(FAMILIES))}) or use --placeholder"
                )
            fam = matched
        from . import cadquery_backend

        spec = build_body(fam, geom, params)
        backend = cadquery_backend.backend_version()

    out_dir = (out_dir or default_output_dir()).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(name or spec.name)
    if spec.placeholder and "placeholder" not in stem.lower():
        stem += "_placeholder"
    path = out_dir / f"{stem}.step"

    inside = spec.params.pop("_inside_courtyard", None)
    prov: dict[str, Any] = {
        "generator": "kicad-tools modelgen",
        "generator_version": _version(),
        "backend": backend,
        "family": spec.family,
        "placeholder": spec.placeholder,
        "params": spec.params,
        "footprint": (
            {"lib_id": geom.lib_id, "source": geom.source, "sha256": geom.sha256}
            if geom is not None
            else None
        ),
        "inside_courtyard": inside,
        "license": LICENSE_OURS,
        "issue": 7687,
    }

    if spec.placeholder:
        from .step_writer import StepBox, write_boxes_step

        boxes = [
            StepBox(p.x0, p.y0, p.x1, p.y1, p.z0, p.z1, MATERIALS[p.material], name=spec.name)
            for p in spec.parts
            if isinstance(p, Box)
        ]
        write_boxes_step(str(path), boxes, product=spec.name, description=_description(prov))
    else:
        from .cadquery_backend import write_step

        write_step(spec, path, _description(prov))

    prov["step"] = {"file": path.name, "sha256": _sha256_file(path)}
    prov_path = path.with_name(path.stem + PROVENANCE_SUFFIX)
    prov_path.write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n")

    result = GenerateResult(path=path, provenance_path=prov_path, provenance=prov, spec=spec)
    if register:
        if geom is None or not geom.lib_id:
            raise ParamError("--register/--attach needs a footprint lib_id")
        result.registered = register_local_model(out_dir, geom.lib_id, path.name)
    if attach and pcb is not None:
        from kicad_tools.pcb.models3d import attach_local_models

        result.attached = attach_local_models(pcb)
    return result
