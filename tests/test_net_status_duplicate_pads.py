"""Physical pad occurrences must not collapse into logical pin names (#5061)."""

from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer


def _board(*, connected=False, second_layer="F.Cu", reverse=False, duplicate_uuid=False):
    pads = [
        f'(pad "SH" smd rect (at {x} 0) (size 1 1) '
        f'(layers "{layer}") (net 1 "GND")'
        + (' (uuid "aaaabbbb-0000-4000-8000-000000000001")' if duplicate_uuid else "")
        + ")"
        for x, layer in ((0, "F.Cu"), (10, second_layer))
    ]
    if reverse:
        pads.reverse()
    tracks = (
        '(segment (start 10 10) (end 20 10) (width 0.3) (layer "F.Cu") (net 1))'
        if connected
        else ""
    )
    return f"""(kicad_pcb (version 20240108) (generator "test")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (net 0 "") (net 1 "GND")
      (footprint "Test:Shield" (layer "F.Cu") (at 10 10)
        (property "Reference" "J1" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        (duplicate_pad_numbers_are_jumpers no)
        {" ".join(pads)})
      {tracks}
      (gr_rect (start 0 0) (end 30 20) (stroke (width 0.05) (type solid))
        (fill none) (layer "Edge.Cuts")))"""


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("duplicate_uuid", [False, True])
@pytest.mark.parametrize(
    "connected,second_layer,islands",
    [
        (False, "F.Cu", 2),
        (True, "F.Cu", 1),
        (True, "B.Cu", 2),
    ],
)
def test_duplicate_occurrences_keep_physical_connectivity(
    tmp_path, reverse, duplicate_uuid, connected, second_layer, islands
):
    path = tmp_path / "duplicates.kicad_pcb"
    path.write_text(
        _board(
            connected=connected,
            second_layer=second_layer,
            reverse=reverse,
            duplicate_uuid=duplicate_uuid,
        )
    )
    status = NetStatusAnalyzer(path).analyze().get_net("GND")
    assert status.total_pads == 2
    assert status.island_count == islands
    assert status.connected_count == (2 if islands == 1 else 1)
    assert status.unconnected_count == (0 if islands == 1 else 1)
    # Public names remain logical names; positions distinguish physical lands.
    output = status.to_dict()
    reported = output["connected_pads"] + output["unconnected_pads"]
    assert {pad["name"] for pad in reported} == {"J1.SH"}
    assert {tuple(pad["position"]) for pad in reported} == {(10, 10), (20, 10)}


def test_board09_all_four_shield_lands_reach_ground_without_source_changes():
    path = (
        Path(__file__).resolve().parents[1]
        / "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb"
    )
    original = path.read_bytes()
    status = NetStatusAnalyzer(path).analyze().get_net("GND")
    assert status.island_count == 1
    assert status.connected_count == status.total_pads == 35
    assert len([p for p in status.connected_pads if p.full_name == "J1.SH"]) == 4
    assert {"J1.A1", "J1.A12", "J1.B1", "J1.B12"} <= {p.full_name for p in status.connected_pads}
    assert path.read_bytes() == original


def test_legacy_logical_name_behavior_remains_explicit(tmp_path):
    path = tmp_path / "legacy.kicad_pcb"
    path.write_text(_board())
    status = NetStatusAnalyzer(path, strict=False).analyze().get_net("GND")
    assert status.island_count == 1


@pytest.mark.parametrize("connected,islands", [(False, 2), (True, 1)])
def test_pcb_query_uses_physical_islands_with_logical_display_names(
    tmp_path, capsys, connected, islands
):
    import argparse
    import json

    from kicad_tools.cli.pcb_query import cmd_nets
    from kicad_tools.schema.pcb import PCB

    path = tmp_path / "query.kicad_pcb"
    path.write_text(_board(connected=connected))
    cmd_nets(
        PCB.load(str(path)),
        argparse.Namespace(filter=None, sorted=False, check_connectivity=True, format="json"),
    )
    result = json.loads(capsys.readouterr().out)[0]
    assert result["island_count"] == islands
    assert result["is_complete"] == connected
    assert sorted(pad for island in result["islands"] for pad in island) == ["J1.SH", "J1.SH"]


@pytest.mark.parametrize("connected", [False, True])
def test_native_duplicate_number_connectivity_control(tmp_path, connected):
    import json
    import subprocess

    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not available")
    path = tmp_path / "native.kicad_pcb"
    path.write_text(_board(connected=connected))
    original = path.read_bytes()
    report = tmp_path / "drc.json"
    subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = json.loads(report.read_text())
    assert len(result["unconnected_items"]) == (0 if connected else 1)
    strict = NetStatusAnalyzer(path).analyze().get_net("GND")
    assert strict.open_connections == len(result["unconnected_items"])
    assert path.read_bytes() == original
