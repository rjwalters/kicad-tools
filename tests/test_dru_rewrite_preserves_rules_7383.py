"""Guard: re-emitting a ``.kicad_dru`` never drops rules it did not generate.

Issue #7383 reported that ``kct export --mfr`` dropped board 05's managed
``kct keepout`` block.  Investigation: the export writer re-derives that
block from the board (``keepout_dru.apply_keepout_rules``) and, from KiCad
10.0.6, deliberately omits it because native DRC enforces keepout rule
areas itself (Issue #7198).  On the pinned 10.0.6 image an injected fill
inside a board 05 keepout is flagged exactly once with or without the block.
The removal used to be silent, and it now prints a notice.

The invariant these tests pin for every in-place ``.kicad_dru`` writer:

* Every pre-existing ``(rule ...)`` survives a re-emit (rule-name superset).
* The single exception is the ``kct keepout`` block on kicad-cli >= 10.0.6.
  It is re-derived as empty, never just lost, and the writer says so on
  stderr.
* Below 10.0.6 (or with an unknown version) the re-derived keepout block is
  byte-identical to the one already on disk.

``kct init`` used to overwrite an existing ``.kicad_dru`` wholesale. It is
covered here too, along with the board 05 repro from the issue on a staged
copy.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.manufacturers import get_profile, write_drc_constraints
from kicad_tools.manufacturers.keepout_dru import (
    DRU_KEEPOUT_BLOCK_BEGIN,
    DRU_KEEPOUT_BLOCK_END,
    KEEPOUT_RULE_PREFIX,
    keepout_rules_for_board,
    merge_keepout_block,
)
from tests.test_keepout_dru import _board

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_05_OUTPUT = REPO_ROOT / "boards" / "05-bldc-motor-controller" / "output"
BOARD_05_PCB = BOARD_05_OUTPUT / "bldc_controller_routed.kicad_pcb"

_RULE_NAME_RE = re.compile(r'^\(rule "((?:[^"\\]|\\.)*)"', re.MULTILINE)

#: Content no kct writer generates for this board, so every rule in it must
#: survive any re-emit: a hand-written rule, the creepage exporter's block
#: (#4508) and the native zone-clearance block.
FOREIGN = """(version 1)

(rule "User HV keepaway"
  (condition "A.NetName == '/HV' && B.NetName != '/HV'")
  (constraint clearance (min 2.5mm)))

# BEGIN kct creepage rules (Issue #4508) -- managed, do not edit
(rule "kct creepage HV-LV"
  (condition "A.hasNetclass('HV') && B.hasNetclass('LV')")
  (constraint clearance (min 3.0mm)))
# END kct creepage rules

# BEGIN kct native zone clearance
(rule "Native zone clearance F.Cu GND"
  (layer "F.Cu")
  (condition "A.Type == 'Zone' && A.NetName == 'GND'")
  (constraint clearance (min 0.3mm)))
# END kct native zone clearance
"""


def _names(content: str) -> set[str]:
    return set(_RULE_NAME_RE.findall(content))


def _keepout_block(content: str) -> str | None:
    match = re.search(
        re.escape(DRU_KEEPOUT_BLOCK_BEGIN) + r".*?" + re.escape(DRU_KEEPOUT_BLOCK_END),
        content,
        re.DOTALL,
    )
    return match.group(0) if match else None


def _pin_version(monkeypatch, version: str | None) -> None:
    from kicad_tools.manufacturers import project_generator

    monkeypatch.setattr(project_generator, "_installed_kicad_cli_version", lambda: version)


def _seed(board: Path) -> str:
    """Sidecar holding the foreign rules plus the board's own keepout block."""
    seeded = merge_keepout_block(FOREIGN, keepout_rules_for_board(board))
    assert seeded is not None and DRU_KEEPOUT_BLOCK_BEGIN in seeded
    board.with_suffix(".kicad_dru").write_text(seeded, encoding="utf-8")
    return seeded


def _jlc_rules():
    return get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1.0)


def _via_export(board: Path) -> None:
    from kicad_tools.export.manufacturing import ManufacturingPackage, ManufacturingResult

    result = ManufacturingResult(output_dir=board.parent / "manufacturing")
    ManufacturingPackage(board, manufacturer="jlcpcb")._write_drc_constraints(result)
    assert result.drc_constraint_paths, result.warnings


def _via_write_drc_constraints(board: Path) -> None:
    write_drc_constraints(board, _jlc_rules(), manufacturer_id="jlcpcb", layers=2)


def _via_check_dru_only(board: Path) -> None:
    from kicad_tools.cli.check_cmd import _emit_drc_sidecars

    _emit_drc_sidecars(
        board,
        SimpleNamespace(design_rules=_jlc_rules()),
        manufacturer_id="jlcpcb",
        layers=2,
        copper_oz=1.0,
        net_class_map=None,
        emit_both=False,
    )


def _via_init(board: Path) -> None:
    from kicad_tools.cli.init_cmd import init_project

    assert init_project(target=str(board.with_suffix("")), manufacturer="jlcpcb") == 0


WRITERS = {
    "kct export --mfr": _via_export,
    "write_drc_constraints": _via_write_drc_constraints,
    "kct check --emit-drc-constraints (dru only)": _via_check_dru_only,
    "kct init": _via_init,
}


@pytest.mark.parametrize("writer", list(WRITERS), ids=list(WRITERS))
@pytest.mark.parametrize("version", ["10.0.1", None], ids=["10.0.1", "unknown"])
def test_rewrite_is_a_rule_superset_below_native_keepouts(tmp_path, monkeypatch, writer, version):
    _pin_version(monkeypatch, version)
    board = _board(tmp_path / "cross.kicad_pcb")
    before = _seed(board)

    WRITERS[writer](board)

    after = board.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert _names(before) <= _names(after), _names(before) - _names(after)
    assert _keepout_block(after) == _keepout_block(before)


@pytest.mark.parametrize("writer", list(WRITERS), ids=list(WRITERS))
def test_rewrite_on_native_release_drops_only_the_keepout_block_and_says_so(
    tmp_path, monkeypatch, capsys, writer
):
    _pin_version(monkeypatch, "10.0.6")
    board = _board(tmp_path / "cross.kicad_pcb")
    before = _seed(board)

    WRITERS[writer](board)

    after = board.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    dropped = _names(before) - _names(after)
    assert all(name.startswith(f"{KEEPOUT_RULE_PREFIX} ") for name in dropped), dropped
    assert _names(FOREIGN) <= _names(after)
    if dropped:
        err = capsys.readouterr().err
        assert "removed the kct keepout-rule block (1 rule(s))" in err
        assert "Issue #7198" in err
    else:
        # ``kct init`` does not manage keepouts, so it leaves the block alone.
        assert writer == "kct init"
        assert _keepout_block(after) == _keepout_block(before)


def test_native_release_rewrite_without_a_block_is_silent(tmp_path, monkeypatch, capsys):
    _pin_version(monkeypatch, "10.0.6")
    board = _board(tmp_path / "cross.kicad_pcb")
    board.with_suffix(".kicad_dru").write_text(FOREIGN, encoding="utf-8")
    _via_write_drc_constraints(board)
    assert "keepout-rule block" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# kct init: no longer a blanket overwrite
# ---------------------------------------------------------------------------


def test_init_rerun_on_its_own_file_is_unchanged(tmp_path):
    from kicad_tools.cli.init_cmd import init_project

    target = tmp_path / "proj"
    assert init_project(target=str(target), manufacturer="jlcpcb") == 0
    first = target.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert init_project(target=str(target), manufacturer="jlcpcb") == 0
    assert target.with_suffix(".kicad_dru").read_text(encoding="utf-8") == first


def test_init_switching_manufacturer_replaces_its_own_rules(tmp_path):
    from kicad_tools.cli.init_cmd import init_project

    target = tmp_path / "proj"
    assert init_project(target=str(target), manufacturer="jlcpcb") == 0
    assert init_project(target=str(target), manufacturer="pcbway", layers=4) == 0
    content = target.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert "JLCPCB" not in content
    assert "PCBWAY" in content


def test_init_keeps_a_hand_added_rule_in_its_own_file(tmp_path):
    from kicad_tools.cli.init_cmd import init_project

    target = tmp_path / "proj"
    assert init_project(target=str(target), manufacturer="jlcpcb") == 0
    dru = target.with_suffix(".kicad_dru")
    dru.write_text(
        dru.read_text(encoding="utf-8") + '\n(rule "Mine"\n  (constraint clearance (min 1.0mm)))\n',
        encoding="utf-8",
    )
    assert init_project(target=str(target), manufacturer="jlcpcb") == 0
    content = dru.read_text(encoding="utf-8")
    assert "Mine" in _names(content)
    # Idempotent from here on: the floors live in the managed block.
    assert init_project(target=str(target), manufacturer="jlcpcb") == 0
    assert dru.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# Board 05: the issue's repro on a staged copy
# ---------------------------------------------------------------------------


@pytest.fixture
def staged_board_05(tmp_path) -> Path:
    if not BOARD_05_PCB.is_file():
        pytest.skip("board 05 output not present")
    stage = tmp_path / "output"
    stage.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        source = BOARD_05_PCB.with_suffix(suffix)
        if source.is_file():
            shutil.copy2(source, stage / source.name)
    return stage / BOARD_05_PCB.name


def test_board_05_export_keeps_every_rule_below_native_keepouts(staged_board_05, monkeypatch):
    _pin_version(monkeypatch, "10.0.1")
    before = staged_board_05.with_suffix(".kicad_dru").read_text(encoding="utf-8")

    _via_export(staged_board_05)

    after = staged_board_05.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert _names(before) <= _names(after), _names(before) - _names(after)
    derived = {rule.name for rule in keepout_rules_for_board(staged_board_05)}
    assert derived and derived <= _names(after)
    if _keepout_block(before) is not None:
        assert _keepout_block(after) == _keepout_block(before)


def test_board_05_export_on_native_release_drops_only_keepout_rules(
    staged_board_05, monkeypatch, capsys
):
    _pin_version(monkeypatch, "10.0.6")
    before = staged_board_05.with_suffix(".kicad_dru").read_text(encoding="utf-8")

    _via_export(staged_board_05)

    after = staged_board_05.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    dropped = _names(before) - _names(after)
    derived = {rule.name for rule in keepout_rules_for_board(staged_board_05)}
    assert dropped <= derived, dropped - derived
    assert _keepout_block(after) is None
    if dropped:
        assert "removed the kct keepout-rule block" in capsys.readouterr().err
