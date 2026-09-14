"""A logical schematic pin can have several independently connected lands."""

from pathlib import Path

import pytest

from kicad_tools.lvs.copper_lvs import compare_copper_netlist, compare_partitions
from kicad_tools.validate.connectivity import ConnectivityValidator
from tests.test_net_status_duplicate_pads import _board


@pytest.mark.parametrize("number", ["1", "SH"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("duplicate_uuid", [False, True])
@pytest.mark.parametrize(
    "connected,layer,islands", [(False, "F.Cu", 2), (True, "F.Cu", 1), (True, "B.Cu", 2)]
)
def test_public_paths_preserve_each_land(
    tmp_path, number, reverse, duplicate_uuid, connected, layer, islands
):
    path = tmp_path / "duplicates.kicad_pcb"
    path.write_text(
        _board(
            connected=connected, second_layer=layer, reverse=reverse, duplicate_uuid=duplicate_uuid
        ).replace('"SH"', f'"{number}"')
    )
    validator = ConnectivityValidator(path)
    result = validator.validate()
    assert result.connected_nets == (islands == 1)
    assert bool(result.issues) == (islands != 1)
    if result.issues:
        assert result.issues[0].islands == ((f"J1.{number}",), (f"J1.{number}",))
    groups, bindings = validator.extract_pad_occurrences()
    assert len(groups) == islands
    assert sum(map(len, groups)) == 2
    assert list(bindings.values()) == [("J1", number)] * 2
    schematic = {("J1", number): "GND"}
    for comparison in (
        compare_partitions(schematic, groups, pad_bindings=bindings),
        compare_partitions(schematic, validator.extract_pad_partition()),
    ):
        assert comparison.clean == (islands == 1)
        assert comparison.bound_pad_count == 1  # Logical pin count remains compatible.
        assert len(comparison.opens) == islands - 1


@pytest.mark.parametrize("connected", [False, True])
def test_file_backed_copper_lvs_binds_all_occurrences(tmp_path, connected):
    path = tmp_path / "duplicates.kicad_pcb"
    path.write_text(_board(connected=connected).replace('"J1"', '"R1"').replace('"SH"', '"1"'))
    schematic = Path(__file__).parent / "fixtures/hierarchical_lvs/root_lvs.kicad_sch"
    result = compare_copper_netlist(schematic, path)
    assert not result.vacuous
    assert result.bound_pad_count == 1
    assert result.clean == connected
    assert len(result.opens) == (not connected)


def test_duplicate_land_short_is_not_hidden_by_logical_binding():
    result = compare_partitions(
        {("J1", "SH"): "GND", ("U1", "1"): "VCC"},
        [frozenset({"land-a"}), frozenset({"land-b", "other"})],
        pad_bindings={"land-a": ("J1", "SH"), "land-b": ("J1", "SH"), "other": ("U1", "1")},
    )
    assert len(result.shorts) == 1
    assert len(result.opens) == 1
    assert result.bound_pad_count == 2


@pytest.mark.parametrize("mapping", [{}, {"land": ("J1", "SH")}])
def test_explicit_partition_rejects_missing_binding_or_repeated_occurrence(mapping):
    with pytest.raises(ValueError):
        compare_partitions({("J1", "SH"): "GND"}, [frozenset({"land"})] * 2, pad_bindings=mapping)


@pytest.mark.parametrize("shapely", [True, False])
def test_duplicate_trace_controls_without_geometry_backend(tmp_path, monkeypatch, shapely):
    monkeypatch.setattr("kicad_tools.validate.connectivity._has_shapely", lambda: shapely)
    for connected in (False, True):
        path = tmp_path / "duplicates.kicad_pcb"
        path.write_text(_board(connected=connected))
        validator = ConnectivityValidator(path)
        assert len(validator.extract_pad_partition()) == (1 if connected else 2)


def test_explicit_mapping_cannot_drop_a_physical_occurrence():
    with pytest.raises(ValueError, match="Every bound physical"):
        compare_partitions(
            {("J1", "SH"): "GND"},
            [frozenset({"a"})],
            pad_bindings={"a": ("J1", "SH"), "b": ("J1", "SH")},
        )


@pytest.mark.parametrize("layer,expected", [("F.Cu", 1), ("B.Cu", 2), ("*.Cu", 1)])
def test_coincident_duplicate_lands_respect_copper_layer(tmp_path, layer, expected):
    board = _board(connected=True, second_layer=layer).replace("(at 10 0)", "(at 0 0)")
    path = tmp_path / "coincident.kicad_pcb"
    path.write_text(board)
    validator = ConnectivityValidator(path)
    assert len(validator.extract_pad_partition()) == expected
    assert bool(validator.validate().issues) == (expected != 1)


@pytest.mark.parametrize("shapely", [True, False])
def test_pour_bonds_all_duplicate_lands(tmp_path, monkeypatch, shapely):
    zone = """(zone (net 1 "GND") (layer "F.Cu") (hatch edge 0.5)
      (connect_pads (clearance 0.2)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3))
      (polygon (pts (xy 8 8) (xy 22 8) (xy 22 12) (xy 8 12)))
      (filled_polygon (layer "F.Cu") (pts (xy 8 8) (xy 22 8) (xy 22 12) (xy 8 12))))"""
    path = tmp_path / "poured.kicad_pcb"
    path.write_text(_board()[:-1] + zone + ")")
    monkeypatch.setattr("kicad_tools.validate.connectivity._has_shapely", lambda: shapely)
    validator = ConnectivityValidator(path)
    groups, bindings = validator.extract_pad_occurrences()
    assert len(groups) == 1
    assert len(groups[0]) == len(bindings) == 2
    assert not validator.validate().issues


def test_ground_bridge_sees_edges_of_each_duplicate_land():
    from copy import deepcopy

    from kicad_tools.analysis.ground_topology import analyze_ground_topology
    from tests.test_ground_topology import _single_bridge_pcb

    pcb = _single_bridge_pcb(
        bridge_ref="FB1", bridge_name="Device:Ferrite_Bead", bridge_value="600R@100MHz"
    )
    bridge = next(fp for fp in pcb.footprints if fp.reference == "FB1")
    # The last occurrence is isolated; the first still wires the bridge.
    duplicate = deepcopy(bridge.pads[0])
    duplicate.position = (100, 100)
    bridge.pads.append(duplicate)
    result = analyze_ground_topology(pcb)
    assert result[0].bridge_count == 1


def test_legacy_advisory_hint_cannot_hide_duplicate_land_open():
    groups = [frozenset({"a"}), frozenset({"b"})]
    bindings = {"a": ("J1", "SH"), "b": ("J1", "SH")}
    enforced = compare_partitions({("J1", "SH"): "GND"}, groups, pad_bindings=bindings)
    advisory = compare_partitions(
        {("J1", "SH"): "GND"}, groups, frozenset({"GND"}), pad_bindings=bindings
    )
    assert len(enforced.opens) == 1
    assert advisory == enforced
    assert not advisory.clean
    assert advisory.bound_pad_count == enforced.bound_pad_count == 1


def test_foreign_copper_touching_one_duplicate_land_reports_short(tmp_path):
    other = """(footprint "Test:Other" (layer "F.Cu") (at 20 10)
      (property "Reference" "U1" (at 0 -2) (layer "F.SilkS"))
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "VCC")))"""
    board = _board().replace('(net 1 "GND")\n', '(net 1 "GND") (net 2 "VCC")\n')
    path = tmp_path / "foreign.kicad_pcb"
    path.write_text(board[:-1] + other + ")")
    groups, bindings = ConnectivityValidator(path).extract_pad_occurrences()
    result = compare_partitions(
        {("J1", "SH"): "GND", ("U1", "1"): "VCC"}, groups, pad_bindings=bindings
    )
    assert len(result.shorts) == len(result.opens) == 1
    assert result.bound_pad_count == 2


def test_board09_shield_occurrences_share_real_copper_without_source_changes():
    path = Path(__file__).parents[1] / "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb"
    before = path.read_bytes()
    groups, bindings = ConnectivityValidator(path).extract_pad_occurrences()
    shield = {node for node, pin in bindings.items() if pin == ("J1", "SH")}
    assert len(shield) == 4
    assert sum(bool(shield & group) for group in groups) == 1
    assert path.read_bytes() == before
