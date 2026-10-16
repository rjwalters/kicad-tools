"""Issue #6820: two defects the #6818 Judge found next to ``routing_policy``.

1. A net-class-map ``length_constraint`` without ``net_id`` raised a bare
   ``KeyError`` out of ``LengthConstraint.from_dict``, which no consumer
   catches: ``kct check`` printed a traceback.  It must be a clean error that
   names the map file and the entry.
2. ``routing_policy`` deferred the via cap of every engaged, capped diff pair
   to ``diffpair_uncoupled_length``, which skips a pair unless both nets carry
   track.  A half-routed pair's via cap was therefore never checked.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from kicad_tools.router.rules import NetClassRouting, net_class_map_from_path
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from tests.test_validate_routing_policy_6044 import seg, via, write_board

_NO_NET_ID = {"FB": {"name": "Analog", "length_constraint": {"max_length": 5.0}}}


def _pad_footprint(net: str, at=(40, 5)) -> str:
    return (
        f'\t(footprint "TP" (layer "F.Cu") (uuid "{uuid.uuid4()}") (at {at[0]} {at[1]})\n'
        f'\t\t(property "Reference" "TP1" (at 0 0 0) (layer "F.SilkS") (uuid "{uuid.uuid4()}"))\n'
        f'\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "{net}") '
        f'(uuid "{uuid.uuid4()}"))\n'
        "\t)\n"
    )


# ---------------------------------------------------------------------------
# Defect 1: length_constraint without net_id
# ---------------------------------------------------------------------------


def test_loader_names_file_and_entry(tmp_path):
    ncm = tmp_path / "ncm.json"
    ncm.write_text(json.dumps(_NO_NET_ID))
    with pytest.raises(ValueError) as exc:
        net_class_map_from_path(ncm)
    msg = str(exc.value)
    assert str(ncm) in msg
    assert "'FB'" in msg
    assert "net_id" in msg


def test_check_explicit_map_is_clean_error(tmp_path, capsys):
    from kicad_tools.cli.check_cmd import main

    pcb = write_board(tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "FB")])
    ncm = tmp_path / "ncm.json"
    ncm.write_text(json.dumps(_NO_NET_ID))
    rc = main([str(pcb), "--net-class-map", str(ncm), "--only", "routing_policy"])
    err = capsys.readouterr().err
    assert rc == 1
    assert str(ncm) in err and "'FB'" in err and "net_id" in err
    assert "Traceback" not in err


def test_check_auto_discovered_map_does_not_crash(tmp_path, capsys):
    from kicad_tools.cli.check_cmd import main

    pcb = write_board(tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "FB")])
    (tmp_path / "net_class_map.json").write_text(json.dumps(_NO_NET_ID))
    main([str(pcb), "--only", "routing_policy", "--format", "json", "--allow-incomplete"])
    err = capsys.readouterr().err
    assert "net_id" in err and "'FB'" in err


def test_route_preload_is_clean_error(tmp_path, capsys):
    from kicad_tools.cli.route_cmd import main as route_main

    pcb = write_board(tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "FB")])
    ncm = tmp_path / "ncm.json"
    ncm.write_text(json.dumps(_NO_NET_ID))
    rc = route_main(
        [
            str(pcb),
            "--dry-run",
            "--quiet",
            "--net-class-map",
            str(ncm),
            "--output",
            str(tmp_path / "out.kicad_pcb"),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid net-class-map structure" in err and "net_id" in err


# ---------------------------------------------------------------------------
# Defect 2: half-routed capped pair
# ---------------------------------------------------------------------------


def _pair_map() -> dict:
    nc = NetClassRouting(name="HS", priority=2, coupled_routing=True, max_vias_per_net=1)
    return {
        "X_P": NetClassRouting(**{**nc.__dict__, "diffpair_partner": "X_N"}),
        "X_N": NetClassRouting(**{**nc.__dict__, "diffpair_partner": "X_P"}),
    }


def _half_routed_board(tmp_path: Path) -> Path:
    # X_P: track plus 3 vias against a cap of 1.  X_N: a pad only, no copper.
    return write_board(
        tmp_path / "b.kicad_pcb",
        [
            seg((0, 0), (20, 0), "X_P"),
            via((2, 0), "X_P"),
            via((5, 0), "X_P"),
            via((8, 0), "X_P"),
            _pad_footprint("X_N"),
        ],
    )


def test_half_routed_pair_not_deferred(tmp_path):
    pcb = _half_routed_board(tmp_path)
    checker = DRCChecker(
        PCB.load(str(pcb)), manufacturer="jlcpcb", layers=2, net_class_map=_pair_map()
    )
    assert checker.routing_policy_deferred_via_nets() == set()


def test_half_routed_pair_via_cap_is_reported(tmp_path):
    from kicad_tools.cli.check_cmd import run_selected_checks

    pcb = _half_routed_board(tmp_path)
    checker = DRCChecker(
        PCB.load(str(pcb)), manufacturer="jlcpcb", layers=2, net_class_map=_pair_map()
    )
    res = run_selected_checks(
        checker, {"routing_policy", "diffpair_uncoupled_length"}, set(), pcb_path=pcb
    )
    via_errs = [v for v in res.violations if v.severity == "error" and "vias" in v.message]
    assert [(v.rule_id, v.nets) for v in via_errs] == [("routing_policy", ("X_P",))]
    assert "3 vias" in via_errs[0].message and "max_vias_per_net 1" in via_errs[0].message
