"""Per-fab copper clearance to a V-score line (Issue #6177).

``DesignRules.min_copper_to_vscore_mm`` carries each fab's published
copper-to-score clearance; profiles without a published figure leave it
unset and resolve to an explicit, flagged-unsourced default.  ``kct panel
--cut vcut`` resolves the fab with the shared ``--mfr`` resolver and uses
that fab's value unless ``--vscore-clearance`` overrides it.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.manufacturers import get_manufacturer_ids, get_profile
from kicad_tools.manufacturers.vscore import (
    UNSOURCED_VSCORE_CLEARANCE_MM,
    vscore_clearance_for,
)

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "src" / "kicad_tools" / "manufacturers" / "data"
FIXTURE = REPO / "tests" / "fixtures" / "projects" / "test_project.kicad_pcb"

# Fabs with a published copper-to-V-score figure, and that figure (mm).
SOURCED = {"jlcpcb": 0.4, "jlcpcb-tier1": 0.4, "pcbway": 0.4}
UNSOURCED = {"oshpark", "seeed", "flashpcb"}


def _yaml_header(mfr: str) -> str:
    text = (DATA / f"{mfr.replace('-', '_')}.yaml").read_text()
    return text.split("design_rules:", 1)[0]


def test_every_profile_is_classified() -> None:
    assert set(get_manufacturer_ids()) == set(SOURCED) | UNSOURCED


@pytest.mark.parametrize("mfr", sorted(SOURCED))
def test_sourced_profiles_carry_the_published_value(mfr: str) -> None:
    profile = get_profile(mfr)
    values = {r.min_copper_to_vscore_mm for r in profile.design_rules.values()}
    # Stackup-independent: kct panel reads the 2-layer rules without
    # counting the board's layers.
    assert values == {SOURCED[mfr]}
    resolved = vscore_clearance_for(mfr)
    assert resolved == (SOURCED[mfr], mfr, True)


@pytest.mark.parametrize("mfr", sorted(SOURCED))
def test_sourced_profiles_cite_a_url(mfr: str) -> None:
    header = _yaml_header(mfr)
    block = header[header.index("V-score copper clearance") :]
    assert "https://" in block
    assert "verified" in block


@pytest.mark.parametrize("mfr", sorted(UNSOURCED))
def test_unsourced_profiles_are_marked_and_fall_back(mfr: str) -> None:
    assert "UNSOURCED" in _yaml_header(mfr)
    profile = get_profile(mfr)
    assert all(r.min_copper_to_vscore_mm is None for r in profile.design_rules.values())
    resolved = vscore_clearance_for(mfr)
    assert resolved.sourced is False
    assert resolved.mm >= UNSOURCED_VSCORE_CLEARANCE_MM
    # Never looser than the fab's own routed-edge clearance.
    for layers in (2, 4, 6):
        rules = profile.get_design_rules(layers=layers)
        assert vscore_clearance_for(mfr, layers).mm >= rules.min_copper_to_edge_mm


def test_flashpcb_fallback_honours_its_routed_edge_minimum() -> None:
    # FlashPCB's instant tier asks 1 mm copper-to-edge, above the 0.5 default.
    assert vscore_clearance_for("flashpcb").mm == 1.0


def test_alias_resolves_to_canonical_id() -> None:
    assert vscore_clearance_for("jlc").mfr == "jlcpcb"


def test_unknown_manufacturer_raises() -> None:
    with pytest.raises(ValueError):
        vscore_clearance_for("nope")


# ---------------------------------------------------------------------------
# kct panel --cut vcut
# ---------------------------------------------------------------------------

needs_shapely = pytest.mark.skipif(
    importlib.util.find_spec("shapely") is None, reason="Shapely required for panel tests"
)


def _run(tmp_path: Path, capsys, board: Path, *extra: str) -> dict:
    from kicad_tools.cli.commands.panel import run_panel_command
    from kicad_tools.cli.parser import create_parser

    out = tmp_path / "panel.kicad_pcb"
    args = create_parser().parse_args(
        ["panel", str(board), "-o", str(out), "--cut", "vcut", "--format", "json", *extra]
    )
    assert run_panel_command(args) == 0
    return json.loads(capsys.readouterr().out)


@needs_shapely
def test_cli_defaults_to_jlcpcb_published_value(tmp_path: Path, capsys) -> None:
    doc = _run(tmp_path, capsys, FIXTURE)
    assert doc["mfr"] == "jlcpcb"
    assert doc["vscore_clearance_mm"] == 0.4
    assert doc["vscore_clearance_source"] == "fab"


@needs_shapely
def test_cli_mfr_selects_unsourced_default(tmp_path: Path, capsys) -> None:
    doc = _run(tmp_path, capsys, FIXTURE, "--mfr", "seeed")
    assert doc["mfr"] == "seeed"
    assert doc["vscore_clearance_mm"] == UNSOURCED_VSCORE_CLEARANCE_MM
    assert doc["vscore_clearance_source"] == "unsourced_default"


@needs_shapely
def test_cli_explicit_clearance_wins(tmp_path: Path, capsys) -> None:
    doc = _run(tmp_path, capsys, FIXTURE, "--mfr", "flashpcb", "--vscore-clearance", "0.3")
    assert doc["vscore_clearance_mm"] == 0.3
    assert doc["vscore_clearance_source"] == "cli"


@needs_shapely
def test_cli_resolves_project_kct_target_fab(tmp_path: Path, capsys) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    board = proj / FIXTURE.name
    shutil.copy(FIXTURE, board)
    (proj / "project.kct").write_text(
        'kct_version: "1.0"\n'
        "project:\n"
        "  name: T\n"
        "requirements:\n"
        "  manufacturing:\n"
        "    target_fab: flashpcb\n"
    )
    doc = _run(tmp_path, capsys, board)
    assert doc["mfr"] == "flashpcb"
    assert doc["vscore_clearance_mm"] == 1.0
    assert doc["vscore_clearance_source"] == "unsourced_default"


@needs_shapely
def test_cli_mousebite_skips_fab_resolution(tmp_path: Path, capsys) -> None:
    from kicad_tools.cli.commands.panel import run_panel_command
    from kicad_tools.cli.parser import create_parser

    out = tmp_path / "panel.kicad_pcb"
    args = create_parser().parse_args(["panel", str(FIXTURE), "-o", str(out), "--format", "json"])
    assert run_panel_command(args) == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["mfr"] is None
    assert doc["vscore_clearance_source"] is None
    assert "auto-loaded fab profile" not in captured.err


@needs_shapely
def test_cli_text_reports_clearance_source(tmp_path: Path, capsys) -> None:
    from kicad_tools.cli.commands.panel import run_panel_command
    from kicad_tools.cli.parser import create_parser

    out = tmp_path / "panel.kicad_pcb"
    args = create_parser().parse_args(
        ["panel", str(FIXTURE), "-o", str(out), "--cut", "vcut", "--mfr", "oshpark"]
    )
    assert run_panel_command(args) == 0
    text = capsys.readouterr().out
    assert "V-score clearance: 0.5 mm (oshpark publishes none; unsourced default)" in text
