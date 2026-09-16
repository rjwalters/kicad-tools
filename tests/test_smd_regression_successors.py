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


@pytest.mark.parametrize("board,stem,functions", CASES)
def test_staged_rule_context_matches_factory_and_preserves_archive(
    tmp_path, board, stem, functions
):
    import sys

    from kicad_tools.manufacturers.dru_generator import generate_dru, merge_dru_floors

    base = ROOT / "boards" / board
    for folder in ("regression-fixture", "regression-input"):
        shutil.copytree(base / folder, tmp_path / folder)
    before = {
        p.name: p.read_bytes() for p in (tmp_path / "regression-fixture").iterdir() if p.is_file()
    }
    staging = load(ROOT / "scripts/ci/board_recipe_artifacts.py", "rule_staging")
    output = staging.recipe_output_dir(tmp_path, prepare=True)
    expected = merge_dru_floors(
        None, generate_dru(get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1), "jlcpcb")
    )
    assert (output / f"{stem}_routed.kicad_dru").read_text() == expected
    assert (output / f"{stem}_routed.kicad_pcb").read_bytes() == before[f"{stem}_routed.kicad_pcb"]
    assert (output / f"{stem}_routed.kicad_pro").read_bytes() == before[f"{stem}_routed.kicad_pro"]
    assert before == {
        p.name: p.read_bytes() for p in (tmp_path / "regression-fixture").iterdir() if p.is_file()
    }
    spec = importlib.util.spec_from_file_location(
        "staged_escape", ROOT / "boards/06-diffpair-test/pour_escape.py"
    )
    escape = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = escape
    spec.loader.exec_module(escape)
    with pytest.raises(ValueError, match="custom DRC"):
        escape.EscapeRules.from_project(
            tmp_path / "regression-fixture" / f"{stem}_routed.kicad_pro"
        )
    rules = escape.EscapeRules.from_project(output / f"{stem}_routed.kicad_pro")
    assert rules.pth_hole_track == 0.28
    assert rules.inner_pth_hole_copper == 0.3
    assert rules.hole_gap == 0.5
    assert rules.width >= 0.2
    assert rules.clearance >= 0.2


@pytest.mark.parametrize("folder", ["regression-fixture", "regression-input"])
@pytest.mark.parametrize("fault", ["tamper", "missing", "symlink"])
def test_sidecar_binding_rejects_before_any_staging(tmp_path, folder, fault):
    base = ROOT / "boards/06-diffpair-test"
    for name in ("regression-fixture", "regression-input"):
        shutil.copytree(base / name, tmp_path / name)
    sidecar = tmp_path / folder / "diffpair_test_routed.kicad_dru"
    if fault == "tamper":
        sidecar.write_text(
            sidecar.read_text()
            + '\n(rule "Authored stronger" (constraint clearance (min 0.5mm)))\n'
        )
    elif fault == "missing":
        sidecar.unlink()
    else:
        original = sidecar.read_bytes()
        sidecar.unlink()
        target = tmp_path / "external.kicad_dru"
        target.write_bytes(original)
        sidecar.symlink_to(target)
    staging = load(ROOT / "scripts/ci/board_recipe_artifacts.py", "invalid_rule_staging")
    with pytest.raises(ValueError):
        staging.recipe_output_dir(tmp_path, prepare=True)
    assert not (tmp_path / "regression-output").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../outside.kicad_dru",
        "/tmp/outside.kicad_dru",
        "other.kicad_dru",
        "diffpair_test_routed.kicad_pcb",
        "generate_pcb.py",
        "diffpair_test_routed.kicad_pro",
        r"..\outside.kicad_dru",
    ],
)
def test_successor_sidecar_name_scope(tmp_path, name):
    base = ROOT / "boards/06-diffpair-test"
    for folder in ("regression-fixture", "regression-input"):
        shutil.copytree(base / folder, tmp_path / folder)
    path = tmp_path / "regression-input/manifest.json"
    manifest = json.loads(path.read_text())
    binding = next(iter(manifest["sidecars"].values()))
    manifest["sidecars"] = {name: binding}
    path.write_text(json.dumps(manifest))
    staging = load(ROOT / "scripts/ci/board_recipe_artifacts.py", "unsafe_rule_staging")
    with pytest.raises(ValueError, match="Unsafe.*sidecar"):
        staging.recipe_output_dir(tmp_path, prepare=True)
    assert not (tmp_path / "regression-output").exists()


@pytest.mark.parametrize("board,stem,functions", CASES)
def test_staged_native_rules_effective_without_regeneration(tmp_path, board, stem, functions):
    """Legacy invalid mask rule silently disables native coverage; staged rules must load."""
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    base = ROOT / "boards" / board
    for folder in ("regression-fixture", "regression-input"):
        shutil.copytree(base / folder, tmp_path / folder)
    staging = load(ROOT / "scripts/ci/board_recipe_artifacts.py", "native_rule_staging")
    output = staging.recipe_output_dir(tmp_path, prepare=True)
    factory = load(ROOT / "tests/test_factory_object_clearance.py", "factory_rule_probe")
    for label, rules in [("old", tmp_path / "regression-fixture"), ("new", output)]:
        case = tmp_path / label
        case.mkdir()
        pcb = factory._two_pad_board(case / "probe.kicad_pcb", 0.12, same_footprint=True)
        shutil.copyfile(rules / f"{stem}_routed.kicad_dru", pcb.with_suffix(".kicad_dru"))
        shutil.copyfile(rules / f"{stem}_routed.kicad_pro", pcb.with_suffix(".kicad_pro"))
        report = case / "drc.json"
        subprocess.run(
            [
                str(cli),
                "pcb",
                "drc",
                str(pcb),
                "--all-track-errors",
                "--format",
                "json",
                "-o",
                str(report),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        findings = json.loads(report.read_text())["violations"]
        assert not [v for v in findings if v["type"] == "drc_rule_error"], findings
        smd = [v for v in findings if "rule 'SMD Pad Clearance" in v["description"]]
        assert bool(smd) == (label == "new"), findings


def test_valid_archived_scalars_are_not_weakened():
    import re

    # Explicit semantics, not positional/name-only equivalence: obsolete mask
    # and broad hole rules are documented migrations, never custom-rule ignores.
    preserved = {
        "Trace Width": 0.1016,
        "Clearance": 0.1016,
        "Via Drill": 0.2,
        "Via Diameter": 0.45,
        "Annular Ring": 0.1,
        "Copper to Edge": 0.3,
        "Silkscreen Width": 0.15,
        "Silkscreen Height": 1.0,
        "Hole to Edge": 0.4,
    }
    for board, stem, _ in CASES:
        base = ROOT / "boards" / board
        for folder in ("regression-fixture", "regression-input"):
            text = (base / folder / f"{stem}_routed.kicad_dru").read_text()
            for family, value in preserved.items():
                match = re.search(
                    r'\(rule "' + family + r' - jlcpcb".*?\(min ([0-9.]+)mm\)', text, re.S
                )
                assert match and float(match[1]) == value
        profile = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
        assert profile.min_solder_mask_clearance_mm == 0.05
        assert profile.min_solder_mask_dam_mm == 0.1


def test_stronger_authored_project_minima_survive_staging(tmp_path):
    import sys

    base = ROOT / "boards/06-diffpair-test"
    for folder in ("regression-fixture", "regression-input"):
        shutil.copytree(base / folder, tmp_path / folder)
    project = tmp_path / "regression-fixture/diffpair_test_routed.kicad_pro"
    data = json.loads(project.read_text())
    data["board"]["design_settings"]["rules"]["min_clearance"] = 0.4
    data["board"]["design_settings"]["rules"]["min_track_width"] = 0.35
    project.write_text(json.dumps(data))
    original = project.read_bytes()
    staging = load(ROOT / "scripts/ci/board_recipe_artifacts.py", "stronger_context_staging")
    output = staging.recipe_output_dir(tmp_path, prepare=True)
    assert project.read_bytes() == (output / project.name).read_bytes() == original
    spec = importlib.util.spec_from_file_location(
        "stronger_escape", ROOT / "boards/06-diffpair-test/pour_escape.py"
    )
    escape = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = escape
    spec.loader.exec_module(escape)
    rules = escape.EscapeRules.from_project(output / project.name)
    assert rules.clearance == 0.4
    assert rules.width == 0.35


def test_bad_sidecar_does_not_modify_existing_staging(tmp_path):
    base = ROOT / "boards/06-diffpair-test"
    for folder in ("regression-fixture", "regression-input"):
        shutil.copytree(base / folder, tmp_path / folder)
    output = tmp_path / "regression-output"
    output.mkdir()
    marker = output / "diffpair_test.kicad_pcb"
    marker.write_text("Existing staging must survive invalid context")
    sidecar = tmp_path / "regression-input/diffpair_test_routed.kicad_dru"
    sidecar.write_text("invalid")
    staging = load(ROOT / "scripts/ci/board_recipe_artifacts.py", "atomic_context_staging")
    with pytest.raises(ValueError, match="hash mismatch"):
        staging.recipe_output_dir(tmp_path, prepare=True)
    assert list(output.iterdir()) == [marker]
    assert marker.read_text() == "Existing staging must survive invalid context"
