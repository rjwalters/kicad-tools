"""--project / --allow-default-rules for DRC-verified commands (Issue #8369)."""

from __future__ import annotations

import pytest

from kicad_tools.sidecars import (
    MissingProjectRulesError,
    resolve_project_rules,
    stage_project_rules,
)


def _board(d, name="b"):
    p = d / f"{name}.kicad_pcb"
    p.write_text("(kicad_pcb)")
    return p


def test_no_rules_refuses_with_actionable_message(tmp_path):
    with pytest.raises(MissingProjectRulesError, match="--allow-default-rules"):
        resolve_project_rules(_board(tmp_path))


def test_allow_default_returns_empty(tmp_path):
    assert resolve_project_rules(_board(tmp_path), allow_default=True) == []


def test_sidecars_beside_board(tmp_path):
    pcb = _board(tmp_path)
    pcb.with_suffix(".kicad_dru").write_text("(version 1)")
    assert resolve_project_rules(pcb) == [pcb.with_suffix(".kicad_dru")]


def test_explicit_project_wins_and_stages_dru(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    pro = other / "p.kicad_pro"
    pro.write_text("{}")
    pro.with_suffix(".kicad_dru").write_text("(version 1)")
    pcb = _board(tmp_path)
    pcb.with_suffix(".kicad_dru").write_text("(version 1) ; beside")
    rules = resolve_project_rules(pcb, pro)
    assert {r.suffix for r in rules} == {".kicad_pro", ".kicad_dru"}
    stage = tmp_path / "stage"
    stage.mkdir()
    stage_project_rules(rules, stage / "trial.kicad_pcb")
    assert (stage / "trial.kicad_dru").read_text() == "(version 1)"
    assert (stage / "trial.kicad_pro").exists()


def test_pro_without_dru_is_ok(tmp_path):
    pro = tmp_path / "p.kicad_pro"
    pro.write_text("{}")
    assert resolve_project_rules(_board(tmp_path, "x"), pro) == [pro]


def test_missing_explicit_project_is_error_even_with_allow(tmp_path):
    with pytest.raises(MissingProjectRulesError, match="not found"):
        resolve_project_rules(_board(tmp_path), tmp_path / "nope.kicad_pro", allow_default=True)


def test_prune_board_refuses_without_rules(tmp_path, monkeypatch):
    from kicad_tools import pcb_prune

    pcb = _board(tmp_path)
    monkeypatch.setattr(pcb_prune.PCB, "load", lambda p: object())
    monkeypatch.setattr(pcb_prune, "plan_prune", lambda *a, **k: ["plan"])
    with pytest.raises(MissingProjectRulesError):
        pcb_prune.prune_board(pcb, apply=False)
