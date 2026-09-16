"""Real native rule-context controls for staged via relocation."""

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli import relocate_with_refill as repair
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.manufacturers import get_profile
from tests.test_relocate_with_refill import board as _board_fixture

board = _board_fixture
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("introduce_collision", [False, True])
def test_native_transaction_preserves_context_and_rejects_new_copper(
    board, monkeypatch, introduce_collision
):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad is required")
    board.with_suffix(".kicad_dru").write_bytes(
        (
            ROOT / "boards/07-matchgroup-test/regression-input/matchgroup_test_routed.kicad_dru"
        ).read_bytes()
    )
    # Canonicalize this deliberately minimal fixture once so both staged
    # native runs share stable source UUIDs, including originally unnamed items.
    repair._native_refill(board, cli)
    original = {
        p: p.read_bytes()
        for p in (board, board.with_suffix(".kicad_pro"), board.with_suffix(".kicad_dru"))
    }
    reports = []
    native = repair._native_refill

    def observe(path, executable):
        assert (
            path.with_suffix(".kicad_pro").read_bytes() == original[board.with_suffix(".kicad_pro")]
        )
        assert (
            path.with_suffix(".kicad_dru").read_bytes() == original[board.with_suffix(".kicad_dru")]
        )
        report = native(path, executable)
        # Retain exact native artifacts beside the pytest fixture for diagnosis.
        evidence = board.parent / ("baseline" if not reports else "candidate")
        evidence.mkdir()
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            shutil.copyfile(path.with_suffix(suffix), evidence / ("board" + suffix))
        (evidence / "native.json").write_text(json.dumps(report, indent=2))
        reports.append(report)
        return report

    monkeypatch.setattr(repair, "_native_refill", observe)
    if introduce_collision:
        plan = repair._plan

        def corrupt_candidate(source, rules):
            candidate, result = plan(source, rules)
            via = candidate.vias[0]
            x, y = via.position
            # Anchor to an existing OTHER pad: native loading can reassign
            # a floating trace to the net of the copper it touches.
            candidate.add_trace((12, 10.47), (x, y), width=0.2, layer="In2.Cu", net="OTHER")
            return candidate, result

        monkeypatch.setattr(repair, "_plan", corrupt_candidate)
        with pytest.raises(RuntimeError, match="native violation identities"):
            repair.relocate_in_pad_vias_with_refill(
                board, get_profile("jlcpcb").get_design_rules(layers=4), kicad_cli=cli
            )
        assert {p: p.read_bytes() for p in original} == original
        assert repair._violation_identities(reports[1]) - repair._violation_identities(reports[0])
        assert any(
            entry["type"] in {"shorting_items", "clearance"} for entry in reports[1]["violations"]
        )
    else:
        result = repair.relocate_in_pad_vias_with_refill(
            board, get_profile("jlcpcb").get_design_rules(layers=4), kicad_cli=cli
        )
        assert result.relocation.changed
        assert len(reports) == 2
        assert not (
            repair._violation_identities(reports[1]) - repair._violation_identities(reports[0])
        )
        for path in original:
            if path != board:
                assert path.read_bytes() == original[path]
