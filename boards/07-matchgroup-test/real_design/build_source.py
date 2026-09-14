#!/usr/bin/env python3
"""Reproduce the reviewed six-layer physical source against a fresh circuit.

This copies an immutable authored placement/fanout fixture only after verifying
its identity and independently regenerating every electrical pin binding. It
makes no routing, signal-integrity, or manufacturing-readiness claim.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

from kicad_tools.schema.pcb import PCB

ROOT = Path(__file__).resolve().parent


def circuit_identity(pcb: PCB) -> dict:
    """Compare package identity and every pad, including intentionally unused pins."""
    result = {}
    for footprint in pcb.footprints:
        if footprint.reference in result:
            raise ValueError(f"Duplicate reference: {footprint.reference}")
        result[footprint.reference] = {
            "footprint": footprint.name,
            "value": footprint.value,
            "pads": sorted((pad.number, pad.net_name or "") for pad in footprint.pads),
        }
    return result


def build(output: Path) -> dict:
    fixture = ROOT / "authored-source"
    manifest = json.loads((fixture / "manifest.json").read_text())
    for name, expected in manifest["sha256"].items():
        if hashlib.sha256((fixture / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Authored source hash mismatch: {name}")
    if output.resolve() == fixture.resolve() or fixture.resolve() in output.resolve().parents:
        raise ValueError("Output must not overwrite authored source")
    spec = importlib.util.spec_from_file_location("board07_fresh_generator", ROOT / "generate.py")
    generator = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = generator
    spec.loader.exec_module(generator)
    with tempfile.TemporaryDirectory(prefix="board07-electrical-") as temp:
        fresh = Path(temp)
        generator.generate(fresh, 0, 0.175)
        physical = PCB.load(fixture / "sdram_demo.kicad_pcb")
        electrical = PCB.load(fresh / "sdram_demo.kicad_pcb")
        if circuit_identity(physical) != circuit_identity(electrical):
            raise ValueError(
                "Authored placement disagrees with freshly generated packages/pin nets"
            )
        count = sum(bool(pad.net_name) for fp in physical.footprints for pad in fp.pads)
        if len(physical.footprints) != manifest["parts"] or count != manifest["bound_pads"]:
            raise ValueError("Unexpected part or connected-pad count")
        circuit = json.loads((fresh / "circuit.json").read_text())
        if any(not part.get("mpn") or not part.get("lcsc") for part in circuit["parts"]):
            raise ValueError("Fresh circuit lacks procurement identity")
        output.mkdir(parents=True, exist_ok=True)
        for path in fresh.iterdir():
            if path.is_file():
                shutil.copy2(path, output / path.name)
        for name in manifest["sha256"]:
            shutil.copy2(fixture / name, output / name)
        constraints = json.loads((output / "sdram_constraints.json").read_text())
        constraints.update(stackup=manifest["stackup"], trace_width_mm=0.18)
        (output / "sdram_constraints.json").write_text(json.dumps(constraints, indent=2) + "\n")
        # Electrical generation's preliminary placement is not the physical source.
        circuit["physical_source"] = {
            "stackup": manifest["stackup"],
            "fixture_sha256": manifest["sha256"],
        }
        (output / "circuit.json").write_text(json.dumps(circuit, indent=2) + "\n")
    status = {
        "status": "draft",
        "routed": False,
        "manufacturing_ready": False,
        "parts": manifest["parts"],
        "bound_pads": count,
        "electrical_identity_verified": True,
        "stackup": manifest["stackup"],
        "remaining": [
            "complete and tune bus and auxiliary routes",
            "independent native DRC/ERC/LVS",
            "timing, impedance, load and coupling review",
            "manufacturing export and verification",
        ],
    }
    (output / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(json.dumps(build(parser.parse_args().output), indent=2))
