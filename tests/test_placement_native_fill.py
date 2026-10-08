"""Real native fills must respect fixed copper, not just restore it afterwards."""

import json

import pytest
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from kicad_tools.router.optimizer.pcb import _extract_balanced_blocks
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.zones.placement_fill import (
    MULTILAYER_FILL_MIN_VERSION,
    fill_around_fixed_copper,
    find_kicad_python,
    kicad_python_version,
    multilayer_fill_supported,
    parse_pcbnew_version,
)
from tests.test_routing_placement_disposition import board_text

NATIVE_PYTHON = find_kicad_python()
NATIVE_VERSION = kicad_python_version(NATIVE_PYTHON) if NATIVE_PYTHON else None


def _zone_content(zone_text: str) -> str:
    """A zone's content with its identity and source formatting dropped.

    ``kct route`` canonicalizes the routed board's UUIDs before KiCad first
    loads it (Issue #6052): it re-serializes the board and gives every
    UUID-less node -- including an authored zone -- a deterministic ``uuid5``.
    The excluded zone's *content* (net, layer, settings, outline and fill)
    must survive untouched; its byte formatting and new identity need not.
    """
    tree = parse_string(zone_text)
    tree.children = [c for c in tree.children if c.name not in {"uuid", "tstamp"}]
    return tree.to_string(compact=True)


def _count_zone(board_text: str, zone_text: str) -> int:
    expected = _zone_content(zone_text)
    return sum(
        _zone_content(zone) == expected
        for _, _, zone in _extract_balanced_blocks(board_text, "zone")
    )


@pytest.mark.skipif(NATIVE_PYTHON is None, reason="KiCad Python runtime unavailable")
@pytest.mark.parametrize(
    "project_clearance",
    [None, 0.8, "custom", "zone_pair", "group", "protected_group", "legacy_group"],
)
@pytest.mark.parametrize("multilayer", [False, True])
@pytest.mark.parametrize("name_only", [False, True])
def test_native_eligible_fill_clears_and_preserves_fixed_zone(
    tmp_path, project_clearance, multilayer, name_only
):
    if multilayer and not multilayer_fill_supported(NATIVE_VERSION):
        pytest.skip(
            f"multilayer native fill needs KiCad >= "
            f"{'.'.join(map(str, MULTILAYER_FILL_MIN_VERSION))} (found {NATIVE_VERSION}); "
            "fills come out empty on older pcbnew (Issue #6101)"
        )
    zone = """(zone (net 1) (net_name "BAD") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110)))
      (filled_polygon (layer "F.Cu") (pts (xy 104 102) (xy 109 102) (xy 109 104) (xy 104 104))))"""
    eligible = """(zone (net 2) (net_name "GOOD") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110))))"""
    if multilayer:
        zone = zone.replace('(layer "F.Cu")', '(layers "F.Cu" "B.Cu")', 1)
        zone = (
            zone[:-1]
            + """(filled_polygon (layer "B.Cu")
          (pts (xy 111 102) (xy 116 102) (xy 116 104) (xy 111 104))))"""
        )
        eligible = eligible.replace('(layer "F.Cu")', '(layers "F.Cu" "B.Cu")', 1)
        eligible = eligible.replace("(fill yes", "(fill yes (island_removal_mode 0)")
    if name_only:
        zone = zone.replace("(net 1)", '(net "BAD")')
        eligible = eligible.replace("(net 2)", '(net "GOOD")')
    group = ""
    if project_clearance in {"group", "protected_group", "legacy_group"}:
        identity = "00000000-0000-4000-8000-000000000789"
        if project_clearance == "legacy_group":
            identity = "00000000-0000-0000-0000-00001234abcd"
            eligible = eligible.replace("(zone", "(zone (tstamp 1234abcd)", 1)
        elif project_clearance == "protected_group":
            zone = zone.replace("(zone", f'(zone (uuid "{identity}")', 1)
        else:
            eligible = eligible.replace("(zone", f'(zone (uuid "{identity}")', 1)
        group = f'''(group "WideCopper" (id "00000000-0000-4000-8000-000000000790")
          (members "{identity}"))'''
    source = board_text(name_only=name_only).rstrip()[:-1] + zone + eligible + group + ")"
    board = tmp_path / "mixed.kicad_pcb"
    board.write_text(source)
    if isinstance(project_clearance, str):
        condition = (
            "(condition \"A.memberOf('WideCopper') || B.memberOf('WideCopper')\")"
            if project_clearance in {"group", "protected_group", "legacy_group"}
            else "(condition \"A.Type == 'Zone' && B.Type == 'Zone'\")"
            if project_clearance == "zone_pair"
            else ""
        )
        board.with_suffix(".kicad_pro").write_text("{}")
        board.with_suffix(".kicad_dru").write_text(
            f'(version 1)\n(rule "Wide" {condition} (constraint clearance (min 0.8mm)))\n'
        )
    elif project_clearance is not None:
        board.with_suffix(".kicad_pro").write_text(
            json.dumps(
                {
                    "board": {"design_settings": {"rules": {"min_clearance": project_clearance}}},
                    "net_settings": {
                        "classes": [{"name": "Default", "clearance": project_clearance}]
                    },
                }
            )
        )
    before = PCB.load(board)
    fill_around_fixed_copper(board, frozenset({"BAD"}), python=NATIVE_PYTHON)
    result = board.read_text()
    assert result.count(zone) == 1
    # Only the eligible zone may change, including native-added timestamps.
    new_eligible = next(z for _, _, z in _extract_balanced_blocks(result, "zone") if '"GOOD"' in z)
    assert result.replace(new_eligible, eligible) == source
    after = PCB.load(board)
    fixed = next(z for z in before.zones if z.net_name == "BAD")
    filled = next(z for z in after.zones if z.net_name == "GOOD")
    for layer in ["F.Cu", "B.Cu"] if multilayer else ["F.Cu"]:
        fixed_shape = unary_union(
            [
                Polygon(p).buffer(fixed.fill_inflation())
                for p, polygon_layer in zip(
                    fixed.filled_polygons, fixed.filled_polygon_layers, strict=True
                )
                if polygon_layer == layer
            ]
        )
        shape = unary_union(
            [
                Polygon(p)
                for p, polygon_layer in zip(
                    filled.filled_polygons, filled.filled_polygon_layers, strict=True
                )
                if polygon_layer == layer
            ]
        )
        assert not fixed_shape.is_empty
        assert shape.area > 100
        assert shape.intersection(fixed_shape).area == 0
        assert shape.distance(fixed_shape) >= (0.8 if project_clearance else 0.3) - 0.001
    assert filled.fill_inflation() == 0


def test_protected_only_zones_need_no_native_runtime(tmp_path, monkeypatch):
    from kicad_tools.zones import placement_fill

    board = tmp_path / "fixed.kicad_pcb"
    source = '(kicad_pcb (net 1 "BAD") (zone (net 1) (layer "F.Cu")))'
    board.write_text(source)
    monkeypatch.setattr(placement_fill, "find_kicad_python", lambda: None)
    fill_around_fixed_copper(board, frozenset({"BAD"}))
    assert board.read_text() == source


@pytest.mark.skipif(NATIVE_PYTHON is None, reason="KiCad Python runtime unavailable")
@pytest.mark.parametrize("filled", [False, True], ids=["empty", "hole"])
def test_native_fill_preserves_empty_space_in_protected_zone(tmp_path, filled):
    # The weakly-simple contour is KiCad's bridged representation of a hole.
    copper = (
        """(filled_polygon (layer "F.Cu")
      (pts (xy 103 101) (xy 118 101) (xy 118 111) (xy 103 111) (xy 103 101)
           (xy 112 103) (xy 112 108) (xy 116 108) (xy 116 103) (xy 112 103)
           (xy 103 101)))"""
        if filled
        else ""
    )
    fixed = f"""(zone (net 1) (net_name "BAD") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (filled_areas_thickness no)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 111) (xy 102 111)))
      {copper})"""
    eligible = """(zone (net 2) (net_name "GOOD") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (island_removal_mode 0) (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 101 101) (xy 119 101) (xy 119 111) (xy 101 111))))"""
    board = tmp_path / "mixed.kicad_pcb"
    board.write_text(board_text().rstrip()[:-1] + fixed + eligible + ")")
    fill_around_fixed_copper(board, frozenset({"BAD"}), python=NATIVE_PYTHON)
    assert board.read_text().count(fixed) == 1
    zone = next(z for z in PCB.load(board).zones if z.net_name == "GOOD")
    shape = unary_union([Polygon(p).buffer(0) for p in zone.filled_polygons])
    # Schema coordinates are normalized to the board outline origin (100, 100).
    assert shape.contains(Point(14, 5))
    if filled:
        assert not shape.contains(Point(10, 5))


def test_unavailable_native_runtime_preserves_partial_board(tmp_path, monkeypatch):
    from kicad_tools.zones import placement_fill

    board = tmp_path / "partial.kicad_pcb"
    source = board_text()[:-1] + '(zone (net 2) (layer "F.Cu")))'
    board.write_text(source)
    monkeypatch.setattr(placement_fill, "find_kicad_python", lambda: None)
    with pytest.raises(RuntimeError, match="selective zone-fill support is unavailable"):
        fill_around_fixed_copper(board, frozenset({"BAD"}))
    assert board.read_text() == source


@pytest.mark.skipif(NATIVE_PYTHON is None, reason="KiCad Python runtime unavailable")
@pytest.mark.parametrize("auto_fix", [False, True])
@pytest.mark.parametrize("invalid_net", ["BAD", "GND"])
def test_real_cli_routes_and_fills_without_changing_excluded_zone(
    tmp_path, auto_fix, invalid_net, capsys
):
    from kicad_tools.cli.route_cmd import main

    fixed = """(zone (net 1) (net_name "BAD") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110)))
      (filled_polygon (layer "F.Cu") (pts (xy 104 102) (xy 109 102) (xy 109 104) (xy 104 104))))"""
    plane = """(zone (net 4) (net_name "PLANE") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110))))"""
    source = tmp_path / "source.kicad_pcb"
    if invalid_net == "GND":
        # An uninset power zone used to be replaced by auto-pour before the
        # preservation-aware loader could capture its original geometry.
        fixed = fixed.replace('"BAD"', '"GND"').replace(
            "(xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110)",
            "(xy 100 100) (xy 120 100) (xy 120 112) (xy 100 112)",
        )
    original = board_text().replace('"BAD"', f'"{invalid_net}"')[:-1] + fixed + plane + ")"
    source.write_text(original)
    output = tmp_path / "routed.kicad_pcb"
    report = tmp_path / "report.json"
    options = ["--auto-fix"] if auto_fix else []
    result = main([str(source), "-o", str(output), "--complete-report", str(report), *options])
    assert result in {2, 3}
    if auto_fix:
        assert "Auto-Fix DRC Violations" in capsys.readouterr().out
    assert source.read_text() == original
    # Content equality, not byte equality: Issue #6052's UUID canonicalization
    # re-serializes the routed board and stamps a uuid5 on the UUID-less zone.
    assert _count_zone(output.read_text(), fixed) == 1
    pcb = PCB.load(output)
    assert any(s.net_name == "GOOD" for s in pcb.segments)
    assert any(z.net_name == "PLANE" and z.filled_polygons for z in pcb.zones)
    disposition = json.loads(report.read_text())["placement_disposition"]
    assert disposition["requested_blocked_nets"] == [invalid_net]
    assert disposition.get("zone_fill_status") != "failed"


def test_cli_fill_failure_retains_routes_and_cannot_report_success(tmp_path, monkeypatch, capsys):
    from kicad_tools.cli.route_cmd import main
    from kicad_tools.zones import placement_fill

    source = tmp_path / "mixed.kicad_pcb"
    original = (
        board_text()[:-1]
        + """(zone (net 4) (net_name "PLANE") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110)))))"""
    )
    source.write_text(original)
    output = tmp_path / "routed.kicad_pcb"
    report = tmp_path / "report.json"
    monkeypatch.setattr(placement_fill, "find_kicad_python", lambda: None)
    result = main(
        [str(source), "-o", str(output), "--nets", "GOOD", "--complete-report", str(report)]
    )
    assert result != 0
    assert source.read_text() == original
    assert any(s.net_name == "GOOD" for s in PCB.load(output).segments)
    disposition = json.loads(report.read_text())["placement_disposition"]
    assert disposition["requested_blocked_nets"] == []
    assert disposition["zone_fill_status"] == "failed"
    assert disposition["clean_success"] is False
    assert "SUCCESS:" not in capsys.readouterr().out


def test_worker_failure_surfaces_its_stderr(tmp_path):
    """A failing fill worker's traceback reaches the error message (Issue #6101)."""
    from kicad_tools.zones.placement_fill import NativeFillWorkerError

    fake = tmp_path / "fake_python.sh"
    fake.write_text("#!/bin/sh\necho 'Traceback: pcbnew exploded' >&2\nexit 1\n")
    fake.chmod(0o755)
    board = tmp_path / "board.kicad_pcb"
    source = board_text()[:-1] + '(zone (net 2) (net_name "GOOD") (layer "F.Cu")))'
    board.write_text(source)
    with pytest.raises(NativeFillWorkerError, match="pcbnew exploded") as info:
        fill_around_fixed_copper(board, frozenset({"BAD"}), python=fake)
    assert info.value.returncode == 1
    assert board.read_text() == source


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("10.0.1", False),
        ("10.0.2", True),
        ("10.0.6", True),
        ("10.1.0", True),
        ("9.0.7", False),
        ("10.0.1-rc1", False),
        ("(10.0.1)", True),  # unparseable: do not skip
        (None, True),
    ],
)
def test_multilayer_fill_version_gate(version, expected):
    """Version gate for multilayer fills works on mocked version strings (Issue #6101)."""
    assert multilayer_fill_supported(version) is expected


def test_parse_pcbnew_version():
    assert parse_pcbnew_version("10.0.6") == (10, 0, 6)
    assert parse_pcbnew_version("10.1") == (10, 1, 0)
    assert parse_pcbnew_version("garbage") is None


_MULTILAYER_ZONE = (
    '(zone (net 2) (net_name "GOOD") (layers "F.Cu" "B.Cu") '
    "(polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110))))"
)


def test_multilayer_fill_on_old_pcbnew_fails_loudly(tmp_path, monkeypatch):
    """Old pcbnew + multilayer zone raises instead of shipping empty copper (#6213)."""
    from kicad_tools.zones import placement_fill

    board = tmp_path / "board.kicad_pcb"
    source = board_text()[:-1] + _MULTILAYER_ZONE + ")"
    board.write_text(source)
    monkeypatch.setattr(placement_fill, "kicad_python_version", lambda python: "10.0.1")
    with pytest.raises(placement_fill.MultilayerFillUnsupportedError) as info:
        fill_around_fixed_copper(board, frozenset({"BAD"}), python=tmp_path / "py")
    assert "10.0.2" in str(info.value) and "10.0.1" in str(info.value)
    assert board.read_text() == source


def test_single_layer_fill_not_blocked_by_old_pcbnew(tmp_path, monkeypatch):
    """The version gate applies only to multilayer zones."""
    from kicad_tools.zones import placement_fill

    fake = tmp_path / "fake_python.sh"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    board = tmp_path / "board.kicad_pcb"
    board.write_text(board_text()[:-1] + '(zone (net 2) (net_name "GOOD") (layer "F.Cu")))')
    monkeypatch.setattr(placement_fill, "kicad_python_version", lambda python: "10.0.1")
    with pytest.raises(placement_fill.NativeFillWorkerError):
        fill_around_fixed_copper(board, frozenset({"BAD"}), python=fake)


def test_find_kicad_python_retries_cold_start_timeout(monkeypatch):
    """A first probe that times out is retried rather than yielding None."""
    import subprocess

    from kicad_tools.zones import placement_fill

    calls = []

    def fake_run(cmd, **kw):
        calls.append(kw["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(placement_fill.subprocess, "run", fake_run)
    assert placement_fill.find_kicad_python() is not None
    assert len(calls) == 2 and calls[0] > 5
