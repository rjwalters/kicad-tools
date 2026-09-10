#!/usr/bin/env python3
"""Rebuild the reviewed SDRAM routing and independently repeat its release gates.

Routing is a reviewed, hash-bound physical artifact. This command does not claim
that an unmeasured fresh autorouter run will reproduce the same placement or
length tuning. The logical circuit is regenerated independently on every build.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(f"board07_release_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(output: Path) -> dict:
    fixture = ROOT / "reviewed-routing"
    if (
        output.resolve() == ROOT
        or fixture == output.resolve()
        or fixture in output.resolve().parents
    ):
        raise ValueError("Output must not overwrite the reviewed design sources")
    manifest = json.loads((fixture / "manifest.json").read_text())
    authored = json.loads((ROOT / "authored-source/manifest.json").read_text())
    if manifest["authored_source_sha256"] != authored["sha256"]:
        raise ValueError("Reviewed routing belongs to a different authored physical source")
    for name, expected in manifest["sha256"].items():
        if hashlib.sha256((fixture / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Reviewed routing hash mismatch: {name}")

    source_builder, validator = load_module("build_source"), load_module("validate")
    source_builder.build(output)
    candidate = output / "sdram_demo_routed.kicad_pcb"
    shutil.copy2(fixture / "sdram_demo.kicad_pcb", candidate)
    evidence = output / "validation"
    if not validator.check(candidate, output, evidence):
        raise RuntimeError(f"Reviewed routing failed fresh validation; inspect {evidence}")
    # Publish the exact board checked after native zone refill and rule emission.
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copy2(
            (evidence / "checked.kicad_pcb").with_suffix(suffix), candidate.with_suffix(suffix)
        )
    result = {
        "status": "validated routing",
        "parts": authored["parts"],
        "bound_pads": authored["bound_pads"],
        "pcb": candidate.name,
        "evidence": "validation/validation.json",
        "routing_method": "reviewed artifact; independently regenerated circuit and fresh gates",
        "hardware_tested": False,
        "routed": True,
        "manufacturing_complete": False,
        "manufacturing_export_required": True,
    }
    (output / "build.json").write_text(json.dumps(result, indent=2) + "\n")
    (output / "status.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(json.dumps(build(parser.parse_args().output), indent=2))
