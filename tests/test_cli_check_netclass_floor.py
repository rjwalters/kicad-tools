"""CLI tests for ``kct check --only netclass_floor`` (Issue #5875).

The acceptance criterion this file owns: a board whose ``.kicad_pro``
declares a netclass below the ``--mfr`` floor must FAIL ``kct check``
**before any copper is laid**.  The board below carries no segments and no
vias at all, so every ``dimension_*`` rule is vacuous on it -- which is
exactly the gap the new category closes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Two copper layers, a board outline, zero segments, zero vias.
UNROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
)
"""

# The netclass every ``circuit-json-to-kicad`` project emits (Issue #5847).
BELOW_FLOOR_CLASS = {
    "name": "Default",
    "track_width": 0.1,
    "via_diameter": 0.3,
    "via_drill": 0.2,
    "clearance": 0.1,
}

CONFORMING_CLASS = {
    "name": "Default",
    "track_width": 0.25,
    "via_diameter": 0.6,
    "via_drill": 0.3,
    "clearance": 0.15,
}


def _board(tmp_path: Path, netclass: dict) -> Path:
    pcb_path = tmp_path / "unrouted.kicad_pcb"
    pcb_path.write_text(UNROUTED_PCB)
    (tmp_path / "unrouted.kicad_pro").write_text(
        json.dumps(
            {
                "board": {},
                "meta": {"version": 1},
                "net_settings": {
                    "classes": [netclass],
                    "meta": {"version": 3},
                    "netclass_patterns": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return pcb_path


class TestNetclassFloorCLI:
    def test_category_registered(self):
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "netclass_floor" in CHECK_CATEGORIES

    def test_below_floor_netclass_fails_before_any_copper(self, tmp_path: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        pcb_path = _board(tmp_path, BELOW_FLOOR_CLASS)
        json_out = tmp_path / "report.json"

        exit_code = main(
            [
                str(pcb_path),
                "--mfr",
                "jlcpcb",
                "--only",
                "netclass_floor",
                "--allow-incomplete",
                "--format",
                "json",
                "--output",
                str(json_out),
            ]
        )
        capsys.readouterr()

        assert exit_code != 0, "a below-floor netclass must fail the check"

        report = json.loads(json_out.read_text())
        rule_ids = {v["rule_id"] for v in report["violations"]}
        assert rule_ids == {
            "netclass_track_width",
            "netclass_clearance",
            "netclass_via_diameter",
            "netclass_via_drill",
            "netclass_annular_ring",
        }
        ring = next(v for v in report["violations"] if v["rule_id"] == "netclass_annular_ring")
        assert ring["severity"] == "error"
        assert ring["actual_value"] == pytest.approx(0.05)
        assert ring["required_value"] == pytest.approx(0.15)
        assert "Default" in ring["message"]

    def test_conforming_netclass_passes(self, tmp_path: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        pcb_path = _board(tmp_path, CONFORMING_CLASS)

        exit_code = main(
            [str(pcb_path), "--mfr", "jlcpcb", "--only", "netclass_floor", "--allow-incomplete"]
        )
        capsys.readouterr()

        assert exit_code == 0

    def test_other_mfr_profile_resolves_its_own_floor(self, tmp_path: Path, capsys):
        """``--mfr pcbway`` resolves pcbway's floors, not JLCPCB's.

        pcbway's 2-layer tier publishes a 0.2mm via drill (vs JLCPCB's
        0.3mm), so the tscircuit ``via_drill: 0.2`` conforms there and
        ``netclass_via_drill`` must NOT fire -- while its 0.4mm via OD still
        rejects the declared 0.3mm, with 0.4 as the reported required value.
        Both halves prove the floors come from the selected profile rather
        than a hardcoded JLCPCB table.
        """
        from kicad_tools.cli.check_cmd import main
        from kicad_tools.manufacturers import get_profile

        pcbway_rules = get_profile("pcbway").get_design_rules(layers=2)
        pcb_path = _board(tmp_path, BELOW_FLOOR_CLASS)
        json_out = tmp_path / "pcbway.json"

        main(
            [
                str(pcb_path),
                "--mfr",
                "pcbway",
                "--only",
                "netclass_floor",
                "--allow-incomplete",
                "--format",
                "json",
                "--output",
                str(json_out),
            ]
        )
        capsys.readouterr()

        violations = json.loads(json_out.read_text())["violations"]
        rule_ids = {v["rule_id"] for v in violations}
        assert "netclass_via_drill" not in rule_ids, (
            "pcbway's 0.2mm via-drill floor accepts the declared 0.2mm"
        )
        diameter = next(v for v in violations if v["rule_id"] == "netclass_via_diameter")
        assert diameter["required_value"] == pytest.approx(pcbway_rules.min_via_diameter_mm)
        assert diameter["required_value"] == pytest.approx(0.4)

    def test_single_layer_board_reports_vias_as_advisory(self, tmp_path: Path, capsys):
        """The closure threads the board's own copper-layer count through.

        A 1-layer board cannot carry a via, so the three via findings come
        back as warnings while the trace/space findings stay blocking.
        """
        from kicad_tools.cli.check_cmd import main

        pcb_path = _board(tmp_path, BELOW_FLOOR_CLASS)
        pcb_path.write_text(UNROUTED_PCB.replace('    (31 "B.Cu" signal)\n', ""))
        json_out = tmp_path / "single_layer.json"

        main(
            [
                str(pcb_path),
                "--mfr",
                "jlcpcb",
                "--only",
                "netclass_floor",
                "--allow-incomplete",
                "--format",
                "json",
                "--output",
                str(json_out),
            ]
        )
        capsys.readouterr()

        violations = json.loads(json_out.read_text())["violations"]
        by_rule = {v["rule_id"]: v["severity"] for v in violations}
        assert by_rule["netclass_track_width"] == "error"
        assert by_rule["netclass_clearance"] == "error"
        assert by_rule["netclass_via_diameter"] == "warning"
        assert by_rule["netclass_via_drill"] == "warning"
        assert by_rule["netclass_annular_ring"] == "warning"

    def test_skip_is_accepted(self, tmp_path: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        pcb_path = _board(tmp_path, BELOW_FLOOR_CLASS)

        exit_code = main([str(pcb_path), "--skip", "netclass_floor", "--allow-incomplete"])
        capsys.readouterr()

        assert isinstance(exit_code, int)
