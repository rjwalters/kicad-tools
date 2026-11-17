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
import re
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
            # The authored fixture is hash-bound and carries bare names, while
            # the fresh PCB carries Library:Footprint lib IDs (Issue #7213).
            "footprint": footprint.name.split(":")[-1],
            "value": footprint.value,
            "pads": sorted((pad.number, pad.net_name or "") for pad in footprint.pads),
        }
    return result


def annotate_pads(pcb: Path, schematic: Path) -> int:
    """Copy schematic pin names/electrical types onto ``pcb``'s pads (issue #6025).

    ``kct detect-mistakes`` reads the ``(pintype ...)`` evidence for rails.
    Every numbered pad must map to a schematic pin.
    """
    from kicad_tools.operations.pintype import annotate_pcb_file_pintypes

    annotation = annotate_pcb_file_pintypes(pcb, schematic)
    if annotation.missing_pads:
        raise ValueError(f"Pads without a schematic pin: {annotation.missing_pads}")
    return annotation.annotated


def apply_library_ids(pcb: Path, schematic: Path) -> int:
    """Write each schematic symbol's ``Library:Footprint`` ID onto ``pcb`` (issue #7213).

    The hash-bound fixtures carry bare footprint names, which kicad-cli reports
    as ``footprint_symbol_mismatch`` against the schematic's Footprint field.
    Only the footprint name atom changes; every other byte is kept. A footprint
    whose reference has no schematic footprint, or whose footprint name differs
    from the schematic's, is an error.
    """
    from kicad_tools.schema.schematic import Schematic

    lib_ids = {sym.reference: sym.footprint for sym in Schematic.load(schematic).symbols}
    text = pcb.read_text(encoding="utf-8")
    heads = list(re.finditer(r'^\t\(footprint "([^"]+)"', text, re.M))
    edits = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        ref = re.search(r'\(property "Reference" "([^"]+)"', text[head.start() : end])
        lib_id = lib_ids.get(ref.group(1)) if ref else None
        if not lib_id or ":" not in lib_id:
            raise ValueError(f"No schematic library footprint for {ref and ref.group(1)}")
        if lib_id.split(":", 1)[1] != head.group(1).split(":")[-1]:
            raise ValueError(f"{ref.group(1)}: {head.group(1)} differs from schematic {lib_id}")
        edits.append((head.start(1), head.end(1), lib_id))
    for start, end, lib_id in reversed(edits):
        text = text[:start] + lib_id + text[end:]
    pcb.write_text(text, encoding="utf-8")
    return len(edits)


def enable_length_matching(net_class_map: Path) -> int:
    """Turn route-time length matching on in the published class map (issue #7423).

    The operator ruled (#5798 Q4/Q8) that board 07's SDRAM match groups tune
    at route time, i.e. ``length_critical: true`` on every net that declares a
    ``length_match_group`` -- without it ``kct route --length-match-groups``
    leaves every group ``not_length_critical``.  The authored fixture's map is
    hash-bound and immutable, so the flag is set on the copy this recipe
    publishes, like the stackup it stamps into ``sdram_constraints.json``.
    Returns the number of nets turned on.
    """
    rules = json.loads(net_class_map.read_text())
    count = 0
    for rule in rules.values():
        if rule.get("length_match_group"):
            rule["length_critical"] = True
            count += 1
    net_class_map.write_text(json.dumps(rules, indent=2) + "\n")
    return count


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
        enable_length_matching(output / "net_class_map.json")
        # Issue #6025: the authored fixture is hash-bound and immutable, so the
        # schematic pin names/types are carried onto the pads of the copy,
        # the first unrouted PCB this recipe writes.
        annotate_pads(output / "sdram_demo.kicad_pcb", output / "sdram_demo.kicad_sch")
        apply_library_ids(output / "sdram_demo.kicad_pcb", output / "sdram_demo.kicad_sch")
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
