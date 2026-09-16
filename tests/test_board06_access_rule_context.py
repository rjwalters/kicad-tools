"""Fresh and staged recipes use the same explicit native rule context."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def recipe(monkeypatch):
    base = ROOT / "boards/06-diffpair-test"

    def module(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        result = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, result)
        spec.loader.exec_module(result)
        return result

    module("generate_pcb", base / "generate_pcb.py")
    monkeypatch.syspath_prepend(str(base))
    return module("board06_access_context", base / "generate_design.py")


def test_fresh_context_creates_factory_rules_and_preserves_source(recipe, tmp_path):
    source, output = tmp_path / "input.kicad_pcb", tmp_path / "output.kicad_pcb"
    project = source.with_suffix(".kicad_pro")
    project.write_text(
        json.dumps({"board": {"design_settings": {"rules": {"min_clearance": 0.4}}}})
    )
    before = project.read_bytes()
    rules = recipe._prepare_plane_access_context(source, output)
    assert rules.clearance == 0.4
    assert (
        rules.pth_hole_track,
        rules.inner_pth_hole_copper,
        rules.edge_clearance,
        rules.hole_edge_clearance,
    ) == (0.28, 0.3, 0.3, 0.4)
    assert project.read_bytes() == before == output.with_suffix(".kicad_pro").read_bytes()
    assert not source.with_suffix(".kicad_dru").exists()
    snapshot = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert recipe._prepare_plane_access_context(source, output) == rules
    assert snapshot == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_existing_output_minima_and_bytes_preserved(recipe, tmp_path):
    source, output = tmp_path / "input.kicad_pcb", tmp_path / "output.kicad_pcb"
    source.with_suffix(".kicad_pro").write_text("{}")
    project = output.with_suffix(".kicad_pro")
    project.write_text(
        json.dumps({"board": {"design_settings": {"rules": {"min_track_width": 0.6}}}})
    )
    before = project.read_bytes()
    rules = recipe._prepare_plane_access_context(source, output)
    assert rules.width == 0.6 and project.read_bytes() == before


@pytest.mark.parametrize("stem", ["input", "output"])
def test_invalid_custom_rules_do_not_create_or_change_sidecars(recipe, tmp_path, stem):
    source, output = tmp_path / "input.kicad_pcb", tmp_path / "output.kicad_pcb"
    source.with_suffix(".kicad_pro").write_text("{}")
    (tmp_path / (stem + ".kicad_dru")).write_text('(version 1)\n(rule "Unsupported")')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError, match="custom DRC"):
        recipe._prepare_plane_access_context(source, output)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_staged_rule_context_is_not_rewritten(recipe, tmp_path):
    import shutil

    spec = importlib.util.spec_from_file_location(
        "context_stager", ROOT / "scripts/ci/board_recipe_artifacts.py"
    )
    staging = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(staging)
    base = ROOT / "boards/06-diffpair-test"
    for name in ("regression-fixture", "regression-input"):
        shutil.copytree(base / name, tmp_path / name)
    folder = staging.recipe_output_dir(tmp_path, prepare=True)
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    rules = recipe._prepare_plane_access_context(
        folder / "diffpair_test.kicad_pcb", folder / "diffpair_test_routed.kicad_pcb"
    )
    assert rules.pth_hole_track == 0.28
    assert before == {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


def test_access_targets_match_all_actual_split_pour_regions(recipe):
    from shapely.geometry import Polygon

    from kicad_tools.zones.generator import ZoneGenerator, _compute_pour_outlines

    source = ROOT / "boards/06-diffpair-test/regression-input/diffpair_test.kicad_pcb"
    generator = ZoneGenerator.from_pcb(source, edge_clearance=0.5)
    expected = [
        ("GND", "In1.Cu", 1),
        ("VBUS_USB", "In2.Cu", 1),
        ("+1V2", "In2.Cu", 2),
        ("+1V8", "B.Cu", 2),
        ("+3V3", "B.Cu", 1),
    ]
    outlines = _compute_pour_outlines(generator.pcb, expected, generator.board_outline)
    targets = recipe._plane_access_targets(source)
    assert {(t.net_name, t.layer) for t in targets} == {(n, l) for n, l, _ in expected}
    for target in targets:
        generator.add_zone(
            net=target.net_name, layer=target.layer, boundary=outlines[target.net_name]
        )
        assert target.region.is_valid and not target.region.is_empty
        assert target.region.equals(Polygon(generator._zones[-1].boundary))
    for a in targets:
        for b in targets:
            if a.net_name != b.net_name and a.layer == b.layer:
                assert a.region.intersection(b.region).area == 0


def test_all_small_power_pads_receive_access_including_u2_d2(recipe, tmp_path):
    import math
    import shutil

    from shapely.geometry import Point

    from kicad_tools.router import DesignRules, load_pcb_for_routing
    from kicad_tools.router.plane_access import PlaneAccessPolicy
    from kicad_tools.schema.pcb import PCB

    base = ROOT / "boards/06-diffpair-test"
    for name in ("regression-fixture", "regression-input"):
        shutil.copytree(base / name, tmp_path / name)
    spec = importlib.util.spec_from_file_location(
        "access_stager", ROOT / "scripts/ci/board_recipe_artifacts.py"
    )
    stager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stager)
    folder = stager.recipe_output_dir(tmp_path, prepare=True)
    source, output = folder / "diffpair_test.kicad_pcb", folder / "diffpair_test_routed.kicad_pcb"
    targets = recipe._plane_access_targets(source)
    rules = recipe._prepare_plane_access_context(source, output)
    router, _ = load_pcb_for_routing(
        str(source),
        skip_nets=recipe.POUR_NETS,
        plane_access_policy=PlaneAccessPolicy(targets, rules),
        rules=DesignRules(
            grid_resolution=0.05,
            trace_width=0.15,
            trace_clearance=0.15,
            via_drill=0.25,
            via_diameter=0.45,
            manufacturer="jlcpcb",
            min_trace_width=0.1016,
            neck_down_distance=1.0,
            neck_down_threshold=0.8,
        ),
    )
    board = PCB.load(source)
    ox, oy = board.board_origin
    starts = {
        (r.net_name, round(r.segments[0].x1, 6), round(r.segments[0].y1, 6))
        for r in router._plane_access_routes
    }
    expected = set()
    witnessed_failure = False
    for fp in board.footprints:
        a = math.radians(fp.rotation or 0)
        for pad in fp.pads:
            if (
                pad.type != "smd"
                or pad.net_name not in recipe.POUR_NETS
                or max(pad.size) > rules.diameter
            ):
                continue
            x, y = pad.position
            key = (
                pad.net_name,
                round(fp.position[0] + ox + x * math.cos(a) + y * math.sin(a), 6),
                round(fp.position[1] + oy - x * math.sin(a) + y * math.cos(a), 6),
            )
            expected.add(key)
            if fp.reference == "U2" and pad.number == "D2":
                assert key in starts
                witnessed_failure = True
    assert witnessed_failure and starts == expected
    regions = {t.net_name: t.region for t in targets}
    for route in router._plane_access_routes:
        via = route.vias[0]
        assert regions[route.net_name].covers(Point(via.x, via.y))
