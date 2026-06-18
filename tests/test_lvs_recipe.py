"""Tests for the shared recipe LVS step (issue #3762).

``kicad_tools.lvs.write_lvs_report`` is the extracted, parametrized core of
board-00's ``run_lvs()``.  These tests cover:

* clean board -> writes ``lvs.json`` with ``clean:true``, does not raise;
* dirty board + ``require_clean=True`` -> raises ``BoardNetlistMismatch``
  but still writes the report;
* dirty board + ``require_clean=False`` (advisory) -> writes the report and
  returns the dirty flags without raising;
* copper-only gating (``run_label=False``) -> ignores label-only mismatches
  (the board-06/07 floating-pin case);
* the ``ADVISORY_LVS_BOARDS`` allowlist constant exists and is auditable.

The gating-logic tests monkeypatch the two comparators so they are fast and
fixture-free; two integration tests run the real comparators against
committed board outputs (board 01 clean; board 04 copper-dirty).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_tools.lvs.recipe as recipe
from kicad_tools.lvs import (
    ADVISORY_LVS_BOARDS,
    BoardNetlistMismatch,
    write_lvs_report,
)
from kicad_tools.lvs.board_lvs import LVSMismatch, LVSResult
from kicad_tools.lvs.copper_lvs import CopperLVSMismatch, CopperLVSResult

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Monkeypatch helpers: stub the two comparators with canned results so the
# gating logic is exercised without parsing real KiCad files.
# ---------------------------------------------------------------------------


def _patch_comparators(
    monkeypatch: pytest.MonkeyPatch,
    *,
    copper: CopperLVSResult,
    label: LVSResult,
) -> None:
    monkeypatch.setattr(recipe, "compare_copper_netlist", lambda s, p: copper)
    monkeypatch.setattr(recipe, "compare_netlists", lambda s, p: label)


_CLEAN_COPPER = CopperLVSResult(clean=True, mismatches=())
_CLEAN_LABEL = LVSResult(clean=True, mismatches=())
_DIRTY_COPPER = CopperLVSResult(
    clean=False,
    mismatches=(
        CopperLVSMismatch(kind="short", net_a="+5V", net_b="GND", pad_a="U1.1", pad_b="U1.2"),
    ),
)
_DIRTY_LABEL = LVSResult(
    clean=False,
    mismatches=(LVSMismatch(ref="D1", pad="1", schematic_net="LED_ANODE", pcb_net="GND"),),
)
# Label-only floating-pin mismatch (board 06/07 shape): schematic_net=None.
_FLOATING_LABEL = LVSResult(
    clean=False,
    mismatches=(LVSMismatch(ref="J1", pad="1", schematic_net=None, pcb_net="USB_DP"),),
)


def _read(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "lvs.json").read_text())


# ---------------------------------------------------------------------------
# Clean board
# ---------------------------------------------------------------------------


def test_clean_board_writes_report_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_comparators(monkeypatch, copper=_CLEAN_COPPER, label=_CLEAN_LABEL)
    copper_clean, label_clean = write_lvs_report(
        Path("sch"), Path("pcb"), tmp_path, require_clean=True
    )
    assert (copper_clean, label_clean) == (True, True)
    data = _read(tmp_path)
    assert data["clean"] is True
    assert data["mismatches"] == []
    assert data["copper_mismatches"] == []
    assert data["$schema"] == "https://kicad-tools.org/schemas/lvs/v1.json"


# ---------------------------------------------------------------------------
# Dirty board, hard gate
# ---------------------------------------------------------------------------


def test_dirty_label_hard_gate_raises_but_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_comparators(monkeypatch, copper=_CLEAN_COPPER, label=_DIRTY_LABEL)
    with pytest.raises(BoardNetlistMismatch):
        write_lvs_report(Path("sch"), Path("pcb"), tmp_path, require_clean=True)
    # Report is written even though the call raised.
    data = _read(tmp_path)
    assert data["clean"] is False
    assert len(data["mismatches"]) == 1


def test_dirty_copper_hard_gate_raises_but_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_comparators(monkeypatch, copper=_DIRTY_COPPER, label=_CLEAN_LABEL)
    with pytest.raises(BoardNetlistMismatch):
        write_lvs_report(Path("sch"), Path("pcb"), tmp_path, require_clean=True)
    data = _read(tmp_path)
    assert data["clean"] is False
    assert data["copper_mismatches"][0]["kind"] == "short"


# ---------------------------------------------------------------------------
# Advisory (require_clean=False)
# ---------------------------------------------------------------------------


def test_advisory_dirty_board_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_comparators(monkeypatch, copper=_DIRTY_COPPER, label=_DIRTY_LABEL)
    copper_clean, label_clean = write_lvs_report(
        Path("sch"), Path("pcb"), tmp_path, require_clean=False
    )
    assert (copper_clean, label_clean) == (False, False)
    data = _read(tmp_path)
    assert data["clean"] is False
    assert len(data["copper_mismatches"]) == 1
    assert len(data["mismatches"]) == 1


# ---------------------------------------------------------------------------
# Copper-only gating (board 06/07 floating-pin case)
# ---------------------------------------------------------------------------


def test_copper_only_gating_ignores_label_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Copper clean, label dirty with floating schematic pin -- with
    # run_label=False the label comparator is never run, so ``clean`` is
    # driven by copper alone and the call does not raise.
    called = {"label": False}

    def _label_should_not_run(s: Path, p: Path) -> LVSResult:
        called["label"] = True
        return _FLOATING_LABEL

    monkeypatch.setattr(recipe, "compare_copper_netlist", lambda s, p: _CLEAN_COPPER)
    monkeypatch.setattr(recipe, "compare_netlists", _label_should_not_run)

    copper_clean, label_clean = write_lvs_report(
        Path("sch"),
        Path("pcb"),
        tmp_path,
        require_clean=True,
        run_copper=True,
        run_label=False,
    )
    assert called["label"] is False  # label comparator skipped entirely
    assert (copper_clean, label_clean) == (True, True)
    data = _read(tmp_path)
    assert data["clean"] is True
    assert data["mismatches"] == []  # no label result -> empty


def test_no_comparator_selected_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_comparators(monkeypatch, copper=_CLEAN_COPPER, label=_CLEAN_LABEL)
    with pytest.raises(ValueError):
        write_lvs_report(
            Path("sch"),
            Path("pcb"),
            tmp_path,
            run_copper=False,
            run_label=False,
        )


# ---------------------------------------------------------------------------
# Advisory allowlist constant
# ---------------------------------------------------------------------------


def test_advisory_allowlist_contains_known_dirty_boards() -> None:
    assert "03-usb-joystick" in ADVISORY_LVS_BOARDS
    assert "04-stm32-devboard" in ADVISORY_LVS_BOARDS
    assert "05-bldc-motor-controller" in ADVISORY_LVS_BOARDS
    # Clean boards must NOT be exempted.
    assert "00-simple-led" not in ADVISORY_LVS_BOARDS
    assert "01-voltage-divider" not in ADVISORY_LVS_BOARDS
    assert isinstance(ADVISORY_LVS_BOARDS, frozenset)


# ---------------------------------------------------------------------------
# Integration: real committed board outputs
# ---------------------------------------------------------------------------


def test_board_01_real_outputs_are_clean(tmp_path: Path) -> None:
    """Board 01 is verified clean on both comparators -> no raise, clean:true."""
    out = REPO_ROOT / "boards" / "01-voltage-divider" / "output"
    sch = out / "voltage_divider.kicad_sch"
    pcb = out / "voltage_divider_routed.kicad_pcb"
    if not (sch.is_file() and pcb.is_file()):
        pytest.skip("board 01 committed outputs not present")
    copper_clean, label_clean = write_lvs_report(sch, pcb, tmp_path, require_clean=True)
    assert (copper_clean, label_clean) == (True, True)
    assert _read(tmp_path)["clean"] is True


def test_board_04_real_outputs_copper_clean_advisory(tmp_path: Path) -> None:
    """Board 04 is now copper-LVS clean (#3794) but stays advisory-classified.

    Before #3794 the committed board-04 routed PCB read 0 shorts / 20 opens
    on the copper comparator (same-net power-pad opens).  The #3794 Leg A
    extractor via-into-pour bond + Leg B ``tie_power_pads`` recipe step closed
    them, so ``compare_copper_netlist`` is now clean.  Board 04 remains in
    ``ADVISORY_LVS_BOARDS`` (graduation to a hard copper-LVS gate is #3795), so
    the recipe still writes the report in advisory mode (``require_clean`` off
    in ``generate_design.py``) — but the copper leg itself is clean here.
    """
    out = REPO_ROOT / "boards" / "04-stm32-devboard" / "output"
    sch = out / "stm32_devboard.kicad_sch"
    pcb = out / "stm32_devboard_routed.kicad_pcb"
    if not (sch.is_file() and pcb.is_file()):
        pytest.skip("board 04 committed outputs not present")
    # Mirror the recipe: only the copper comparator is the meaningful leg for
    # board 04 (run_label=False in generate_design.py).
    copper_clean, label_clean = write_lvs_report(
        sch, pcb, tmp_path, require_clean=False, run_copper=True, run_label=False
    )
    # Copper-LVS is now clean (#3794); the label leg is skipped (None -> True).
    assert copper_clean is True
    assert _read(tmp_path)["clean"] is True
    assert _read(tmp_path)["copper_mismatches"] == []
