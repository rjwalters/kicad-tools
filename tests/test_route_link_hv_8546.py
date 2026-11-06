"""``kct route-link`` HV awareness: voltage-map floors, scoped --net-clearance (Issue #8546)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli import main  # noqa: E402
from kicad_tools.cli.route_link_cmd import (  # noqa: E402
    _rules_for_link,
    _rules_from_args,
    add_route_link_arguments,
)
from kicad_tools.router.link_router import (  # noqa: E402
    LinkRouteRules,
    _build_model,
    explain_link_failure,
    terminal_for_pad,
)
from kicad_tools.schema.pcb import PCB  # noqa: E402

_HEAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "HV")
  (net 3 "LV")
  (gr_rect (start 100 100) (end 130 120) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
"""


def _pad(n: int, x: float, y: float, net: int, name: str) -> str:
    return (
        f'  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-00000000000{n}") (at {x} {y})\n'
        f'    (fp_text reference "R{n}" (at 0 -1.5) (layer "F.SilkS"))\n'
        f'    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") '
        f'(net {net} "{name}")))\n'
    )


def _seg(n: int, x1, y1, x2, y2, net: int) -> str:
    return (
        f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.2) (layer "F.Cu") '
        f'(net {net}) (uuid "00000000-0000-0000-0000-0000000001{n:02d}"))\n'
    )


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_HEAD + body + ")\n")
    return p


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_route_link_arguments(parser)
    return parser.parse_args(argv)


class TestNetClearanceScoping:
    def test_scoped_to_preceding_link_and_global_before_any_link(self):
        a = _parse(
            "b.kicad_pcb",
            "--net-clearance", "G=1",
            "--link", "SIG R1.1 R2.1",
            "--net-clearance", "HV=5",
            "--link", "SIG R3.1 R4.1",
            "--net-clearance", "HV=2",
        )  # fmt: skip
        assert a.net_clearance == ["G=1"]
        assert a.links[0].net_clearance == ["HV=5"]
        assert a.links[1].net_clearance == ["HV=2"]

    def test_bad_value_rejected(self):
        with pytest.raises(SystemExit):
            _parse("b.kicad_pcb", "--link", "SIG R1.1 R2.1", "--net-clearance", "HV=abc")

    def test_two_links_get_different_floors(self, tmp_path):
        p = _write(tmp_path, _pad(1, 105, 105, 1, "SIG") + _seg(1, 110, 110, 115, 110, 2))
        pcb = PCB.load(str(p))
        a = _parse(
            str(p),
            "--link", "SIG R1.1 R1.1",
            "--net-clearance", "HV=5",
            "--link", "SIG R1.1 R1.1",
            "--net-clearance", "HV=2",
        )  # fmt: skip
        base = _rules_from_args(a, pcb)
        r0 = _rules_for_link(a, pcb, base, a.links[0])
        r1 = _rules_for_link(a, pcb, base, a.links[1])
        assert r0.clearance_for(2) == 5.0
        assert r1.clearance_for(2) == 2.0
        assert base.clearance_for(2) == base.clearance  # neither leaked into the base

    def test_no_flags_is_base_rules(self, tmp_path):
        p = _write(tmp_path, _pad(1, 105, 105, 1, "SIG"))
        pcb = PCB.load(str(p))
        a = _parse(str(p), "--link", "SIG R1.1 R1.1")
        a._pairwise_required = None
        base = _rules_from_args(a, pcb)
        assert _rules_for_link(a, pcb, base, a.links[0]) is base

    def test_unknown_scoped_net_is_reported(self, tmp_path, capsys):
        p = _write(tmp_path, _pad(1, 105, 105, 1, "SIG") + _pad(2, 125, 105, 1, "SIG"))
        rc = main(
            ["route-link", "--format", "json", "--probe", str(p), "--link", "SIG R1.1 R2.1",
             "--net-clearance", "NOPE=3"]
        )  # fmt: skip
        assert rc == 1
        captured = capsys.readouterr()
        assert "NOPE" in captured.out + captured.err


class TestVoltageMapFloors:
    BODY = (
        _pad(1, 105, 110, 1, "SIG")
        + _pad(2, 125, 110, 1, "SIG")
        + _seg(1, 108, 111, 122, 111, 2)  # HV track 1 mm off the straight line
    )

    def _probe(self, tmp_path, capsys, *extra):
        p = _write(tmp_path, self.BODY)
        rc = main(
            ["route-link", "--format", "json", "--probe", str(p), "--link", "SIG R1.1 R2.1",
             "--link-layers", "F.Cu", *extra]
        )  # fmt: skip
        return rc, json.loads(capsys.readouterr().out)

    def test_without_map_routes_straight(self, tmp_path, capsys):
        rc, out = self._probe(tmp_path, capsys)
        assert rc == 0
        assert 18.5 < out["links"][0]["length_mm"] < 20.2

    def test_map_derived_floor_moves_the_route(self, tmp_path, capsys):
        vm = tmp_path / "vm.json"
        vm.write_text(json.dumps({"SIG": 0, "HV": 300, "LV": 0}))
        rc, out = self._probe(tmp_path, capsys, "--voltage-map", str(vm))
        link = out["links"][0]
        # The 300 V pair needs far more than 1 mm: the straight line is illegal.
        assert not link["routable"] or link["length_mm"] > 20.5

    def test_larger_floor_wins_with_net_clearance(self, tmp_path):
        from kicad_tools.cli.route_cmd import _preload_voltage_map

        vm = tmp_path / "vm.json"
        vm.write_text(json.dumps({"SIG": 0, "HV": 300}))
        p = _write(tmp_path, self.BODY)
        pcb = PCB.load(str(p))
        a = _parse(
            str(p), "--voltage-map", str(vm),
            "--link", "SIG R1.1 R2.1", "--net-clearance", "HV=0.5",
        )  # fmt: skip
        assert _preload_voltage_map(a) == 0
        derived = _rules_for_link(a, pcb, _rules_from_args(a, pcb), a.links[0]).clearance_for(2)
        assert derived > 0.5
        b = _parse(
            str(p), "--voltage-map", str(vm),
            "--link", "SIG R1.1 R2.1", "--net-clearance", "HV=99",
        )  # fmt: skip
        assert _preload_voltage_map(b) == 0
        assert _rules_for_link(b, pcb, _rules_from_args(b, pcb), b.links[0]).clearance_for(2) == 99

    def test_unmapped_link_net_gets_hv_floor(self, tmp_path):
        """Map lists only HV: the unmapped link net is 0 V, so the 300 V floor applies."""
        from kicad_tools.cli.route_cmd import _preload_voltage_map

        vm = tmp_path / "vm.json"
        vm.write_text(json.dumps({"HV": 300}))
        p = _write(tmp_path, self.BODY)
        pcb = PCB.load(str(p))
        a = _parse(str(p), "--voltage-map", str(vm), "--link", "SIG R1.1 R2.1")
        assert _preload_voltage_map(a) == 0
        base = _rules_from_args(a, pcb)
        assert _rules_for_link(a, pcb, base, a.links[0]).clearance_for(2) > 1.0

    def test_unmapped_link_net_probe_detours(self, tmp_path, capsys):
        vm = tmp_path / "vm.json"
        vm.write_text(json.dumps({"HV": 300}))
        rc, out = self._probe(tmp_path, capsys, "--voltage-map", str(vm))
        link = out["links"][0]
        assert not link["routable"] or link["length_mm"] > 20.5

    def test_unmapped_neighbour_gets_hv_floor(self, tmp_path):
        """Link net is the HV one; an unmapped neighbour is 0 V and still gets the floor."""
        from kicad_tools.cli.route_cmd import _preload_voltage_map

        vm = tmp_path / "vm.json"
        vm.write_text(json.dumps({"SIG": 300}))
        p = _write(tmp_path, self.BODY)
        pcb = PCB.load(str(p))
        a = _parse(str(p), "--voltage-map", str(vm), "--link", "SIG R1.1 R2.1")
        assert _preload_voltage_map(a) == 0
        base = _rules_from_args(a, pcb)
        assert _rules_for_link(a, pcb, base, a.links[0]).clearance_for(2) > 1.0


class TestCorridorBound:
    def _explain(self, tmp_path, gap_half: float):
        body = (
            _pad(1, 105, 110, 1, "SIG")
            + _pad(2, 125, 110, 1, "SIG")
            + _seg(1, 110, 110 - gap_half - 0.1, 120, 110 - gap_half - 0.1, 2)
            + _seg(2, 110, 110 + gap_half + 0.1, 120, 110 + gap_half + 0.1, 3)
        )
        pcb = PCB.load(str(_write(tmp_path, body)))
        a = terminal_for_pad(pcb, "R1", "1")
        b = terminal_for_pad(pcb, "R2", "1")
        return explain_link_failure(pcb, 1, a, b, LinkRouteRules(), model=_build_model(pcb))

    def test_names_the_geometric_bound(self, tmp_path):
        out = self._explain(tmp_path, 0.2)  # 0.4 mm gap < 0.2 + 0.2 + 0.2
        assert "corridor too narrow" in out["reason"]
        assert out["corridor"]["gap_mm"] == pytest.approx(0.4, abs=0.01)
        assert out["corridor"]["required_mm"] == pytest.approx(0.6, abs=0.01)

    def test_wide_corridor_has_no_bound(self, tmp_path):
        out = self._explain(tmp_path, 1.0)
        assert "corridor" not in out


class TestGateUsesVoltageMap:
    def test_census_gate_active_with_map_only(self, tmp_path):
        from kicad_tools.cli.route_link_cmd import _census_failures

        vm = tmp_path / "vm.json"
        vm.write_text(json.dumps({"SIG": 0, "HV": 300}))
        body = (
            _pad(1, 105, 110, 1, "SIG")
            + _seg(1, 108, 110, 120, 110, 1)
            + _seg(2, 108, 110.8, 120, 110.8, 2)  # HV 0.6 mm from SIG: far below 300 V creepage
        )
        p = _write(tmp_path, body)
        a = _parse(str(p), "--voltage-map", str(vm), "--link", "SIG R1.1 R1.1")
        failing = _census_failures(p, a)
        assert failing and any({"SIG", "HV"} <= set(k[:2]) for k in failing)
        # Without --min or --voltage-map the gate stays off (unchanged behaviour).
        assert _census_failures(p, _parse(str(p), "--link", "SIG R1.1 R1.1")) is None
