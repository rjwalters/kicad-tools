"""Bind a reviewed factory ordering choice to the PCB's actual layer model."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from kicad_tools.physics import Stackup
from kicad_tools.schema.pcb import PCB


def stackup_ordering_record(pcb_path: str | Path, factory_id: str) -> dict:
    """Reject a mismatching or implicit stack; never substitute factory geometry.

    Copper thickness and dielectric thickness/permittivity must match the
    selected published construction (1e-6 absolute numerical tolerance).
    Soldermask/finish and loss tangent are outside this construction comparison.
    """
    path = Path(pcb_path)
    before = path.read_bytes()
    pcb = PCB.load(path)
    actual = Stackup.from_pcb(pcb)
    expected = Stackup.jlcpcb_named(factory_id)
    general = pcb._sexp.find_child("general")
    thickness = general.find_child("thickness") if general is not None else None
    nominal = thickness.get_float(0) if thickness is not None else None
    if nominal is None or not math.isclose(nominal, 1.6, rel_tol=0, abs_tol=1e-6):
        raise ValueError("Stackup mismatch: nominal board thickness")
    if [layer.name for layer in pcb.copper_layers] != ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]:
        raise ValueError("Stackup mismatch: declared PCB copper layers")
    if not actual.has_explicit_data:
        raise ValueError("Stackup mismatch: source PCB has no explicit construction")
    copper_indices = [i for i, layer in enumerate(actual.layers) if layer.is_copper]
    if not copper_indices:
        raise ValueError("Stackup mismatch: missing copper layers")
    layers = actual.layers[copper_indices[0] : copper_indices[-1] + 1]
    if len(layers) != len(expected.layers):
        raise ValueError("Stackup mismatch: layer count")
    for index, (source, target) in enumerate(zip(layers, expected.layers, strict=True)):
        if source.layer_type != target.layer_type or not math.isclose(
            source.thickness_mm, target.thickness_mm, rel_tol=0, abs_tol=1e-6
        ):
            raise ValueError(f"Stackup mismatch at layer {index}: type/thickness")
        if source.is_copper and source.name != target.name:
            raise ValueError(f"Stackup mismatch at layer {index}: copper identity")
        if source.is_dielectric and not math.isclose(
            source.epsilon_r, target.epsilon_r, rel_tol=0, abs_tol=1e-6
        ):
            raise ValueError(f"Stackup mismatch at layer {index}: dielectric constant")
    if path.read_bytes() != before:
        raise ValueError("PCB changed while reading stackup")
    return {
        "schema_version": 1,
        "pcb": path.name,
        "pcb_sha256": hashlib.sha256(before).hexdigest(),
        "construction": expected.construction,
        "actual_stackup": actual.summary(),
        "validation_scope": "Layer construction match only; not factory acceptance or manufacturing qualification",
    }
