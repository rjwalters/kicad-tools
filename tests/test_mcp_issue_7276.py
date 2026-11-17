"""MCP manufacturer/tier ids come from the registry (Issue #7276)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_tools.manufacturers import get_all_manufacturer_names
from kicad_tools.mcp.tools.registry import get_tool

REPO = Path(__file__).resolve().parent.parent
BOARD03 = REPO / "boards" / "03-usb-joystick"


def _enum(tool: str) -> list[str]:
    return get_tool(tool).parameters["properties"]["manufacturer"]["enum"]


def _cli_mfr_choices() -> set[str]:
    from kicad_tools.cli.export_cmd import build_parser

    parser = build_parser()
    for action in parser._actions:
        if "--mfr" in action.option_strings:
            return set(action.choices)
    raise AssertionError("--mfr not found")


def test_export_tool_enums_match_cli_mfr_choices():
    cli = _cli_mfr_choices()
    assert "jlcpcb-tier1" in cli
    assert set(_enum("export_assembly")) == cli
    assert set(_enum("export_gerbers")) == cli


def test_drc_enum_has_every_registry_id():
    assert set(_enum("get_drc_violations")) == set(get_all_manufacturer_names())


def test_get_drc_violations_rejects_unknown_id():
    from kicad_tools.mcp.tools.analysis import get_drc_violations

    pcb = next(BOARD03.glob("output/*_routed.kicad_pcb"))
    with pytest.raises(ValueError, match="jlcpcb-tier1"):
        get_drc_violations(str(pcb), rules="nope")


def test_export_assembly_rejects_unknown_id(tmp_path):
    from kicad_tools.mcp.tools.export import export_assembly

    pcb = next(BOARD03.glob("output/*_routed.kicad_pcb"))
    sch = next(BOARD03.glob("output/*.kicad_sch"))
    result = export_assembly(str(pcb), str(sch), str(tmp_path / "out"), manufacturer="nope")
    assert not result.success
    assert "jlcpcb-tier1" in result.error


def test_get_drc_violations_accepts_tier_id():
    from kicad_tools.mcp.tools.analysis import get_drc_violations

    pcb = next(BOARD03.glob("output/*_routed.kicad_pcb"))
    result = get_drc_violations(str(pcb), rules="jlcpcb-tier1", allow_incomplete=True)
    assert result.to_dict()["manufacturer"] == "jlcpcb-tier1"


@pytest.fixture(scope="module")
def board03_copy(tmp_path_factory):
    dest = tmp_path_factory.mktemp("board03") / "03-usb-joystick"
    shutil.copytree(BOARD03, dest, ignore=shutil.ignore_patterns("manufacturing", "__pycache__"))
    return dest


def _call(copy: Path, out: Path, **kw):
    pcb = next(copy.glob("output/*_routed.kicad_pcb"))
    sch = next(p for p in copy.glob("output/*.kicad_sch") if "routed" not in p.name)
    handler = get_tool("export_assembly").handler
    return handler({"pcb_path": str(pcb), "schematic_path": str(sch), "output_dir": str(out), **kw})


@pytest.mark.slow
def test_export_assembly_tier_omitted_and_jlcpcb(board03_copy, tmp_path):
    # Explicit tier id: strict export succeeds.
    ok = _call(board03_copy, tmp_path / "tier", manufacturer="jlcpcb-tier1")
    assert ok["success"], ok["error"]
    assert ok["manufacturer"] == "jlcpcb-tier1"
    assert ok["bom"] and ok["pnp"] and ok["gerbers"]

    # Omitted: resolved from project.kct target_fab.
    omitted = _call(board03_copy, tmp_path / "omitted")
    assert omitted["success"], omitted["error"]
    assert omitted["manufacturer"] == "jlcpcb-tier1"
    assert omitted["manufacturer_source"] in {"project_kct", "sidecar"}

    # Base tier is still refused by the provenance check, naming the fix.
    refused = _call(board03_copy, tmp_path / "base", manufacturer="jlcpcb")
    assert not refused["success"]
    assert refused["zip_file"] is None
    assert "jlcpcb-tier1" in refused["error"]
    assert "manufacturer" in refused["error"]
