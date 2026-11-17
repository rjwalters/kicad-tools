"""Explicit per-board non-HV declarations for the HV vacuity guard (issue #8278).

The #4354 vacuity guard forces ``NOT_READY`` when HV-net resolution is empty
but the board carries mains-*named* copper.  Board 05's 12-24 V ``FUSED`` rail
matches :data:`MAINS_NAME_RE` by name only.  A board's net-class map may now
declare such nets explicitly non-HV via a reserved ``non_hv_nets`` block
(``{net: reason}``).  The declaration exempts only the name-based guard, is
always reported, needs a reason, and fails closed when malformed.
"""

from __future__ import annotations

import json

import pytest

from kicad_tools.audit import ManufacturingAudit
from kicad_tools.cli import audit_cmd
from kicad_tools.creepage.engine import (
    NON_HV_NETS_KEY,
    non_hv_nets_from_dict,
)
from kicad_tools.router.rules import net_class_map_from_dict
from tests.creepage.fixtures import board_mains_named_source

POWER = {"name": "Power"}
MAINS_NETS = ("AC_LINE", "AC_NEUTRAL", "FUSED_LINE")


def _board(tmp_path):
    p = tmp_path / "board.kicad_pcb"
    p.write_text(board_mains_named_source())
    return p


def _map(tmp_path, declared=None):
    data: dict = {name: dict(POWER) for name in MAINS_NETS}
    if declared is not None:
        data[NON_HV_NETS_KEY] = declared
    p = tmp_path / "net_class_map.json"
    p.write_text(json.dumps(data))
    return p


def _isolation(tmp_path, declared=None, **hv):
    audit = ManufacturingAudit(
        _board(tmp_path),
        manufacturer="jlcpcb",
        net_class_map_path=_map(tmp_path, declared),
        **hv,
    )
    return audit.run()


ALL_DECLARED = dict.fromkeys(MAINS_NETS, "24 V SELV test rail")


def test_parser_returns_empty_without_block():
    assert non_hv_nets_from_dict({"FUSED": POWER}) == {}


def test_parser_reads_reasons():
    got = non_hv_nets_from_dict({NON_HV_NETS_KEY: {"FUSED": "  12-24 V rail  "}})
    assert got == {"FUSED": "12-24 V rail"}


@pytest.mark.parametrize(
    "block",
    [{}, [], "FUSED", {"FUSED": ""}, {"FUSED": "   "}, {"FUSED": None}, {"": "x"}],
)
def test_parser_rejects_malformed_block(block):
    with pytest.raises(ValueError):
        non_hv_nets_from_dict({NON_HV_NETS_KEY: block})


def test_net_class_map_parser_skips_reserved_block():
    data = {"FUSED": POWER, NON_HV_NETS_KEY: {"FUSED": "12-24 V rail"}}
    assert set(net_class_map_from_dict(data)) == {"FUSED"}


def test_without_declaration_guard_still_fires(tmp_path):
    iso = _isolation(tmp_path).isolation
    assert iso.mains_suspected_unclassified is True
    assert set(MAINS_NETS) <= set(iso.mains_suspect_nets)
    assert iso.declared_non_hv_nets == {}


def test_full_declaration_exempts_and_discloses(tmp_path, capsys):
    result = _isolation(tmp_path, ALL_DECLARED)
    iso = result.isolation
    assert iso.mains_suspected_unclassified is False
    assert iso.mains_suspect_nets == []
    assert iso.declared_non_hv_nets == ALL_DECLARED
    assert "declared non-HV" in iso.details
    assert result.to_dict()["isolation"]["declared_non_hv_nets"] == ALL_DECLARED
    actions = [a.description for a in result.action_items]
    for name in MAINS_NETS:
        assert any(name in d and "declared non-HV" in d for d in actions)
    audit_cmd.output_table(result)
    out = capsys.readouterr().out
    assert "declared non-HV" in out
    assert "FUSED_LINE: 24 V SELV test rail" in out


def test_partial_declaration_keeps_undeclared_suspects(tmp_path):
    iso = _isolation(tmp_path, {"FUSED_LINE": "24 V rail"}).isolation
    assert iso.mains_suspected_unclassified is True
    assert "FUSED_LINE" not in iso.mains_suspect_nets
    assert {"AC_LINE", "AC_NEUTRAL"} <= set(iso.mains_suspect_nets)
    assert iso.declared_non_hv_nets == {"FUSED_LINE": "24 V rail"}


def test_mains_working_voltage_still_trips_guard(tmp_path):
    iso = _isolation(
        tmp_path,
        ALL_DECLARED,
        hv_standard="iec60664",
        hv_working_voltage=250.0,
        hv_pollution_degree=2,
    ).isolation
    assert iso.mains_suspected_unclassified is True


def test_malformed_declaration_fails_closed(tmp_path):
    iso = _isolation(tmp_path, {"FUSED_LINE": ""}).isolation
    assert iso.mains_suspected_unclassified is True
    assert iso.declared_non_hv_nets == {}


def test_declaration_never_removes_a_class_hv_net(tmp_path):
    """A net mapped to the HV class stays HV even if also declared non-HV."""
    data = {name: dict(POWER) for name in MAINS_NETS}
    data["AC_LINE"] = {"name": "HV"}
    data[NON_HV_NETS_KEY] = ALL_DECLARED
    p = tmp_path / "net_class_map.json"
    p.write_text(json.dumps(data))
    result = ManufacturingAudit(_board(tmp_path), manufacturer="jlcpcb", net_class_map_path=p).run()
    assert result.isolation.hv_present is True
    assert "AC_LINE" in result.isolation.hv_nets


# ---------------------------------------------------------------------------
# ``kct creepage`` honours the same declaration (it ran its own guard and
# disagreed with ``kct audit`` on Board 05's FUSED rail).
# ---------------------------------------------------------------------------


def _creepage(tmp_path, declared, *extra):
    from kicad_tools._shapely import has_shapely
    from kicad_tools.cli.commands.creepage import run_creepage_command
    from kicad_tools.cli.parser import create_parser

    if not has_shapely():
        pytest.skip("creepage requires shapely")
    argv = ["creepage", str(_board(tmp_path)), "--net-class-map", str(_map(tmp_path, declared))]
    return run_creepage_command(create_parser().parse_args([*argv, *(extra or ("--min", "0.5"))]))


def test_creepage_cli_exempts_declared_nets_and_says_so(tmp_path, capsys):
    rc = _creepage(tmp_path, ALL_DECLARED)
    err = capsys.readouterr().err
    assert rc != 2
    assert "declared non-HV" in err and "FUSED_LINE (24 V SELV test rail)" in err


def test_creepage_cli_partial_declaration_still_trips_guard(tmp_path, capsys):
    rc = _creepage(tmp_path, {"FUSED_LINE": "24 V rail"})
    err = capsys.readouterr().err
    assert rc == 2
    assert "AC_LINE" in err


def test_creepage_cli_mains_working_voltage_still_trips_guard(tmp_path, capsys):
    rc = _creepage(
        tmp_path,
        ALL_DECLARED,
        "--standard",
        "iec60664",
        "--working-voltage",
        "250",
        "--pollution-degree",
        "2",
    )
    assert rc == 2


def test_creepage_cli_malformed_declaration_is_an_error(tmp_path, capsys):
    assert _creepage(tmp_path, {"FUSED_LINE": ""}) == 1
    assert "non_hv_nets" in capsys.readouterr().err
