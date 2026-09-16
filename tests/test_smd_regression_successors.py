"""Preserve archived witnesses while qualifying corrected synthetic pad copper."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.manufacturers import get_profile, write_drc_constraints
from kicad_tools.sexp import parse_file, parse_string

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ("06-diffpair-test", "diffpair_test", {"U3": "generate_qfp48_pcie_sink"}),
    (
        "07-matchgroup-test",
        "matchgroup_test",
        {
            "U1": "generate_qfn48_ddr_controller",
            "U2": "generate_qfn48_ddr_sink",
            "U5": "generate_qfp48_addr_sink",
        },
    ),
]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def footprints(doc):
    return {
        next(x.get_string(1) for x in fp.find_all("fp_text") if x.get_string(0) == "reference"): fp
        for fp in doc.find_all("footprint")
    }


def semantic(node):
    return node.name, node.value, tuple(semantic(child) for child in node.children)


def pad_geometry(fp):
    return {
        p.get_string(0): tuple(semantic(p.find(key)) for key in ("at", "size", "layers", "net"))
        for p in fp.find_all("pad")
    }


@pytest.mark.parametrize("board,stem,functions", CASES)
def test_successor_changes_only_corner_lengths_and_matches_generator(board, stem, functions):
    base = ROOT / "boards" / board
    original = parse_file(base / "regression-fixture" / f"{stem}.kicad_pcb")
    successor = parse_file(base / "regression-input" / f"{stem}.kicad_pcb")
    generator = load(base / "generate_pcb.py", f"successor_{board}")
    fps = footprints(successor)
    for reference, function in functions.items():
        fp = fps[reference]
        generated = parse_string(getattr(generator, function)())
        assert pad_geometry(fp) == pad_geometry(generated)
        changed = []
        for pad in fp.find_all("pad"):
            size = pad.find("size")
            if 0.68 in size.values:
                changed.append(pad.get_string(0))
                size.set_value(size.values.index(0.68), 0.7)
        assert set(changed) == {"1", "12", "13", "24", "25", "36", "37", "48"}
    # Undo only the documented lengths; every other parsed token must match.
    assert semantic(successor) == semantic(original)


@pytest.mark.parametrize("board,stem,functions", CASES)
@pytest.mark.parametrize("use_successor", [False, True])
def test_successor_native_smd_clearance(tmp_path, board, stem, functions, use_successor):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    base = ROOT / "boards" / board
    pcb = tmp_path / f"{stem}.kicad_pcb"
    source = "regression-input" if use_successor else "regression-fixture"
    shutil.copyfile(base / source / pcb.name, pcb)
    shutil.copyfile(
        base / "regression-fixture" / f"{stem}.kicad_pro", pcb.with_suffix(".kicad_pro")
    )
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
    write_drc_constraints(pcb, rules, manufacturer_id="jlcpcb", layers=4)
    report = tmp_path / "native.json"
    subprocess.run(
        [str(cli), "pcb", "drc", str(pcb), "--format", "json", "-o", str(report)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    violations = json.loads(report.read_text())["violations"]
    assert not [v for v in violations if v["type"] == "drc_rule_error"], violations
    findings = [v for v in violations if "rule 'SMD Pad Clearance" in v["description"]]
    expected = 0 if use_successor else (2 if board.startswith("06") else 10)
    assert len(findings) == expected, violations


@pytest.mark.parametrize("tamper", [None, "regression-fixture", "regression-input"])
def test_staging_uses_only_hash_bound_successor(tmp_path, tamper):
    base = ROOT / "boards/06-diffpair-test"
    for name in ("regression-fixture", "regression-input"):
        shutil.copytree(base / name, tmp_path / name)
    helper = load(ROOT / "scripts/ci/board_recipe_artifacts.py", "successor_staging")
    filename = "diffpair_test.kicad_pcb"
    if tamper:
        path = tmp_path / tamper / filename
        path.write_bytes(path.read_bytes() + b"\n# changed\n")
        with pytest.raises(ValueError, match="hash mismatch"):
            helper.recipe_output_dir(tmp_path, prepare=True)
    else:
        before = (tmp_path / "regression-fixture" / filename).read_bytes()
        output = helper.recipe_output_dir(tmp_path, prepare=True)
        assert (output / filename).read_bytes() == (
            tmp_path / "regression-input" / filename
        ).read_bytes()
        assert (tmp_path / "regression-fixture" / filename).read_bytes() == before
