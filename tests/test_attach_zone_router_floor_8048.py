"""Attach zones exempt router-class clearance floors in both engines (Issue #8048).

A footprint's courtyard, between its own pad nets, is spaced by the part's pin
pitch: the emitted ``.kicad_dru`` (kicad-cli) and ``kct check`` lift the
router's net-class floor there -- never a designer's, never the base clearance.
The kicad-cli tests are black-box: only the emitted DRU and the report are
read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.manufacturers import write_drc_constraints
from kicad_tools.manufacturers.attach_zone_dru import (
    DRU_ATTACH_ZONE_BLOCK_BEGIN,
    FloorZone,
    ZoneItem,
    build_floor_zones,
    has_designer_clearance_rule,
    merge_attach_zone_block,
)
from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP
from kicad_tools.schema.pcb import PCB

from .test_route_sidecar_netclass_clearance_6390 import (
    RULES,
    _has_kicad_cli,
    _kct_errors,
    _kicad_cli_clearance,
)

#: Rows of GND-vs-X track pairs: name -> (y of the GND track, layer, other net).
#: U1 sits at (5, 5) with a +-3 mm courtyard (y 2..8); rows are 0.2 mm tracks.
_ROWS = {
    "in_zone": (2.5, "F.Cu", 2),  # GND/SIG, both pad nets, inside the courtyard
    "inner_layer": (3.5, "B.Cu", 2),  # same nets, a layer U1's SMD pads do not occupy
    "non_member": (6.5, "F.Cu", 3),  # GND vs OTH, which is no pad net of U1
    "outside": (12.0, "F.Cu", 2),  # GND/SIG outside the courtyard
}


def _board(gap: float) -> str:
    segs = []
    for y, layer, other in _ROWS.values():
        segs.append(
            f'(segment (start 3.2 {y}) (end 4.4 {y}) (width 0.2) (layer "{layer}") (net 1))'
        )
        y2 = round(y + 0.2 + gap, 4)
        segs.append(
            f'(segment (start 3.2 {y2}) (end 4.4 {y2}) (width 0.2) (layer "{layer}") (net {other}))'
        )
    return f"""(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user) (45 "F.CrtYd" user))
  (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "GND") (net 2 "SIG") (net 3 "OTH")
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1) (type default))
    (fill none) (layer "Edge.Cuts"))
  (footprint "T:Two" (layer "F.Cu") (at 5 5)
    (property "Reference" "U1" (at 0 -2) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -3 -3) (end 3 3) (stroke (width 0.05) (type default)) (fill none)
      (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 0.3 1) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.45 0) (size 0.3 1) (layers "F.Cu") (net 2 "SIG")))
  {chr(10).join(segs)})
"""


def _routed(tmp_path: Path, gap: float, *, designer: bool = False, name: str = "az") -> Path:
    """What ``kct route`` leaves behind: the board, its project and its DRU."""
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(_board(gap))
    if designer:
        # GND carries the designer's own 0.3 mm class instead of the router's.
        pcb.with_suffix(".kicad_pro").write_text(
            json.dumps(
                {
                    "net_settings": {
                        "meta": {"version": 3},
                        "classes": [
                            {"name": "Default", "clearance": 0.1},
                            {"name": "HV", "clearance": 0.3},
                        ],
                        "netclass_assignments": {"GND": "HV"},
                    }
                }
            )
        )
        write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb")
    else:
        write_drc_constraints(
            pcb,
            RULES,
            manufacturer_id="jlcpcb",
            router_net_classes={"GND": DEFAULT_NET_CLASS_MAP["GND"]},
        )
    return pcb


def _row_of(y: float) -> str | None:
    for name, (row_y, _layer, _other) in _ROWS.items():
        if row_y - 0.3 <= y <= row_y + 0.5:
            return name
    return None


def _kct_rows(pcb: Path, tmp_path: Path, capsys) -> set[str]:
    errors = _kct_errors(pcb, tmp_path)
    capsys.readouterr()
    return {r for e in errors if (r := _row_of(e["location"][1])) is not None}


def _cli_rows(pcb: Path) -> set[str]:
    rows = set()
    for violation in _kicad_cli_clearance(pcb):
        ys = [item["pos"]["y"] for item in violation["items"]]
        if (r := _row_of(min(ys))) is not None:
            rows.add(r)
    return rows


# ------------------------------------------------------------ the shared zone


def test_zone_is_courtyard_scoped_and_net_scoped(tmp_path):
    pcb = _routed(tmp_path, 0.14)
    zones = build_floor_zones(PCB.load(str(pcb)).footprints)
    assert [z.ref for z in zones] == ["U1"]
    assert {name for name, _ in zones[0].members} == {"GND", "SIG"}


def test_footprint_without_courtyard_has_no_zone(tmp_path):
    pcb = tmp_path / "nocy.kicad_pcb"
    pcb.write_text(_board(0.14).replace('(layer "F.CrtYd")', '(layer "F.Fab")'))
    assert build_floor_zones(PCB.load(str(pcb)).footprints) == ()


def test_condition_names_both_sides_nets_and_layers(tmp_path):
    zone = build_floor_zones(PCB.load(str(_routed(tmp_path, 0.14))).footprints)[0]
    cond = zone.condition()
    assert cond.startswith("A.intersectsCourtyard('U1') && B.intersectsCourtyard('U1') && ")
    for side in "AB":
        assert f"{side}.NetName == 'GND'" in cond and f"{side}.NetName == 'SIG'" in cond
        assert f"{side}.Layer == 'F.Cu'" in cond and f"{side}.Type == 'Via'" in cond


def test_through_hole_net_is_a_member_on_every_layer():
    zone = FloorZone("J1", (("A", frozenset({"*.Cu"})), ("B", frozenset({"F.Cu"}))))
    assert "A.Layer" not in zone.condition().split("&& (")[1]
    assert zone.member_layers("B") == frozenset({"F.Cu"})


def test_exempts_requires_both_items_in_courtyard_member_nets_and_layer(tmp_path):
    from shapely.geometry import box

    zone = build_floor_zones(PCB.load(str(_routed(tmp_path, 0.14))).footprints)[0]
    inside, outside = box(4, 4, 4.5, 4.5), box(30, 30, 31, 31)

    def item(shape, net, layer="F.Cu", kind="segment"):
        return ZoneItem(shape, net, layer, kind)

    assert zone.exempts(item(inside, "GND"), item(inside, "SIG"))
    assert not zone.exempts(item(inside, "GND"), item(outside, "SIG"))
    assert not zone.exempts(item(inside, "GND"), item(inside, "OTH"))
    assert not zone.exempts(item(inside, "GND"), item(inside, "GND"))
    assert not zone.exempts(item(inside, "GND", "B.Cu"), item(inside, "SIG", "B.Cu"))
    assert zone.exempts(item(inside, "GND", None, "via"), item(inside, "SIG", "F.Cu"))


# ------------------------------------------------------------ emitted DRU


def test_block_is_appended_last_idempotent_and_removable(tmp_path):
    pcb = _routed(tmp_path, 0.14)
    dru = pcb.with_suffix(".kicad_dru").read_text()
    assert DRU_ATTACH_ZONE_BLOCK_BEGIN in dru
    assert dru.rstrip().endswith("# END kct attach-zone router-class exemptions")
    zones = build_floor_zones(PCB.load(str(pcb)).footprints)
    again = merge_attach_zone_block(dru, zones, 0.127)
    assert again == merge_attach_zone_block(again, zones, 0.127)
    removed = merge_attach_zone_block(dru, (), 0.127)
    assert DRU_ATTACH_ZONE_BLOCK_BEGIN not in removed
    assert not has_designer_clearance_rule(dru)


def test_rewriting_the_sidecars_is_byte_stable(tmp_path):
    pcb = _routed(tmp_path, 0.14)
    first = pcb.with_suffix(".kicad_dru").read_bytes()
    write_drc_constraints(
        pcb,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes={"GND": DEFAULT_NET_CLASS_MAP["GND"]},
    )
    assert pcb.with_suffix(".kicad_dru").read_bytes() == first


def test_no_block_without_router_classes(tmp_path):
    pcb = tmp_path / "plain.kicad_pcb"
    pcb.write_text(_board(0.14))
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb")
    assert DRU_ATTACH_ZONE_BLOCK_BEGIN not in pcb.with_suffix(".kicad_dru").read_text()


def test_designer_clearance_rule_suppresses_the_block(tmp_path):
    pcb = tmp_path / "mine.kicad_pcb"
    pcb.write_text(_board(0.14))
    pcb.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "mine"\n  (condition "A.NetName == \'GND\'")\n'
        "  (constraint clearance (min 0.4mm)))\n"
    )
    write_drc_constraints(
        pcb,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes={"GND": DEFAULT_NET_CLASS_MAP["GND"]},
    )
    dru = pcb.with_suffix(".kicad_dru").read_text()
    assert "mine" in dru and DRU_ATTACH_ZONE_BLOCK_BEGIN not in dru


@pytest.mark.parametrize("mode", [None, "1", "0"])
def test_block_is_emitted_in_every_preserve_mode(tmp_path, mode):
    pcb = tmp_path / "m.kicad_pcb"
    pcb.write_text(_board(0.14))
    write_drc_constraints(
        pcb,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes={"GND": DEFAULT_NET_CLASS_MAP["GND"]},
        preserve_board_rules=mode,
    )
    assert DRU_ATTACH_ZONE_BLOCK_BEGIN in pcb.with_suffix(".kicad_dru").read_text()


# ------------------------------------------------------------ parity / oracle

_GAPS = [0.14, 0.18, 0.25]  # below / at-or-above the Power class floor (0.2 mm)


@pytest.mark.skipif(not _has_kicad_cli(), reason="kicad-cli not installed")
@pytest.mark.parametrize("gap", _GAPS)
def test_kicad_cli_exempts_only_inside_the_zone(tmp_path, gap):
    rows = _cli_rows(_routed(tmp_path, gap))
    if gap >= 0.2:
        assert rows == set()
    else:
        # Inside the zone passes; the same gap outside it, on a layer the part
        # has no copper on, or against a non-member net still fails.
        assert rows == {"inner_layer", "non_member", "outside"}


@pytest.mark.parametrize("gap", _GAPS)
def test_kct_check_exempts_only_inside_the_zone(tmp_path, capsys, gap):
    rows = _kct_rows(_routed(tmp_path, gap), tmp_path, capsys)
    assert rows == (set() if gap >= 0.2 else {"inner_layer", "non_member", "outside"})


@pytest.mark.skipif(not _has_kicad_cli(), reason="kicad-cli not installed")
@pytest.mark.parametrize("gap", [0.13, 0.14, 0.16, 0.18, 0.19, 0.21, 0.25])
@pytest.mark.parametrize("designer", [False, True])
def test_kct_check_and_kicad_cli_agree_on_every_row(tmp_path, capsys, gap, designer):
    pcb = _routed(tmp_path, gap, designer=designer)
    assert _kct_rows(pcb, tmp_path, capsys) == _cli_rows(pcb)


@pytest.mark.skipif(not _has_kicad_cli(), reason="kicad-cli not installed")
def test_designer_class_is_never_relaxed(tmp_path, capsys):
    pcb = _routed(tmp_path, 0.14, designer=True)
    assert DRU_ATTACH_ZONE_BLOCK_BEGIN not in pcb.with_suffix(".kicad_dru").read_text()
    assert "in_zone" in _cli_rows(pcb)
    assert "in_zone" in _kct_rows(pcb, tmp_path, capsys)


@pytest.mark.skipif(not _has_kicad_cli(), reason="kicad-cli not installed")
def test_check_ignores_zones_when_the_dru_lacks_the_block(tmp_path, capsys):
    pcb = _routed(tmp_path, 0.14)
    dru = pcb.with_suffix(".kicad_dru")
    dru.write_text(merge_attach_zone_block(dru.read_text(), (), 0.127))
    assert "in_zone" in _kct_rows(pcb, tmp_path, capsys)
    assert "in_zone" in _cli_rows(pcb)
