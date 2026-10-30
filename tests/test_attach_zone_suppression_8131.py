"""The attach-zone exemption's suppression is observable (Issue #8131).

A designer clearance rule in ``.kicad_dru`` keeps the managed attach-zone block
out (#8060), so ``kct check`` exempts nothing.  Both ``kct route``'s DRU write
and ``kct check`` must say so, and ``kct check --format json`` must expose it.
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli.check_cmd import main
from kicad_tools.manufacturers import write_drc_constraints
from kicad_tools.manufacturers.attach_zone_dru import (
    ATTACH_ZONE_ACTIVE,
    ATTACH_ZONE_NOT_APPLICABLE,
    ATTACH_ZONE_SUPPRESSED,
    DRU_ATTACH_ZONE_BLOCK_BEGIN,
    attach_zone_exemption_status,
)
from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP

from .test_attach_zone_router_floor_8048 import _board
from .test_route_sidecar_netclass_clearance_6390 import RULES

_DESIGNER_DRU = (
    '(version 1)\n(rule "mine"\n  (condition "A.NetName == \'GND\'")\n'
    "  (constraint clearance (min 0.4mm)))\n"
)


def _write(tmp_path: Path, *, designer: bool, classes: bool = True, gap: float = 0.14) -> Path:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board(gap))
    if designer:
        pcb.with_suffix(".kicad_dru").write_text(_DESIGNER_DRU)
    write_drc_constraints(
        pcb,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes={"GND": DEFAULT_NET_CLASS_MAP["GND"]} if classes else None,
    )
    return pcb


def _check(pcb: Path, capsys) -> tuple[dict, str]:
    capsys.readouterr()
    main(
        [
            str(pcb),
            "--mfr",
            "jlcpcb",
            "--only",
            "netclass_clearance_copper",
            "--allow-incomplete",
            "--no-net-class-map",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    return json.loads(captured.out)["summary"], captured.err


def test_status_helper():
    assert attach_zone_exemption_status(None, True) == (ATTACH_ZONE_NOT_APPLICABLE, None)
    assert attach_zone_exemption_status(_DESIGNER_DRU, False)[0] == ATTACH_ZONE_NOT_APPLICABLE
    assert attach_zone_exemption_status(_DESIGNER_DRU, True) == (
        ATTACH_ZONE_SUPPRESSED,
        "designer_clearance_rule",
    )
    block = f"{DRU_ATTACH_ZONE_BLOCK_BEGIN}\n"
    assert attach_zone_exemption_status(_DESIGNER_DRU + block, True)[0] == ATTACH_ZONE_ACTIVE


def test_designer_rule_reports_suppressed_with_count(tmp_path, capsys):
    pcb = _write(tmp_path, designer=True)
    summary, err = _check(pcb, capsys)
    assert summary["attach_zone_exemption"] == "suppressed"
    assert summary["attach_zone_exemption_reason"] == "designer_clearance_rule"
    assert summary["attach_zone_unexempted"] >= 1
    assert "attach-zone clearance exemption is NOT active" in err
    assert f"{summary['attach_zone_unexempted']} netclass_clearance_copper" in err


def test_managed_block_reports_active_without_warning(tmp_path, capsys):
    pcb = _write(tmp_path, designer=False)
    summary, err = _check(pcb, capsys)
    assert summary["attach_zone_exemption"] == "active"
    assert "attach_zone_exemption_reason" not in summary
    assert "NOT active" not in err


def test_no_router_classes_is_not_applicable(tmp_path, capsys):
    pcb = _write(tmp_path, designer=True, classes=False)
    summary, err = _check(pcb, capsys)
    assert summary["attach_zone_exemption"] == "not_applicable"
    assert "NOT active" not in err


def test_unreadable_dru_is_not_applicable(tmp_path, capsys):
    pcb = _write(tmp_path, designer=False)
    pcb.with_suffix(".kicad_dru").write_bytes(b"\xff\xfe\x00bad")
    summary, _ = _check(pcb, capsys)
    assert summary["attach_zone_exemption"] == "not_applicable"


def test_route_dru_write_names_the_suppression(tmp_path, capsys):
    capsys.readouterr()
    _write(tmp_path, designer=True)
    assert "attach-zone exemption suppressed (designer_clearance_rule)" in capsys.readouterr().err


def test_route_dru_write_is_quiet_without_designer_rule(tmp_path, capsys):
    capsys.readouterr()
    _write(tmp_path, designer=False)
    assert "suppressed" not in capsys.readouterr().err
