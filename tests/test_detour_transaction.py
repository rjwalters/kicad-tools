"""Failed native or pair validation must never publish candidate copper."""

import hashlib
from pathlib import Path

import pytest

from kicad_tools.zones import detour_transaction as transaction
from kicad_tools.zones.local_detour import LocalDetour, PairMatch
from kicad_tools.zones.pour_escape import Escape, EscapeRules


@pytest.fixture
def staged_board(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("""(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "") (net 1 "N") (net 2 "VCC")
  (segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu")
    (net 1) (uuid "cut")))""")
    board.with_suffix(".kicad_pro").write_text("{}")
    rules = EscapeRules()
    plan = LocalDetour(
        "N",
        ("cut",),
        Escape(((20, 20), (21, 20)), False, rules),
        (("F.Cu", Escape(((0, 0), (10, 0)), False, rules)),),
    )
    return board, plan


def _report(path, _executable):
    return {
        "violations": [],
        "unconnected_items": (
            [{"type": "unconnected_items", "items": [{"uuid": "a"}, {"uuid": "b"}]}]
            if path.parent.name == "baseline"
            else []
        ),
    }


def _publish(board, plan, validate_pair=lambda _: None):
    return transaction.publish_local_detour(
        board,
        plan,
        PairMatch(0),
        power_net="VCC",
        power_layer="F.Cu",
        source_sha256=hashlib.sha256(board.read_bytes()).hexdigest(),
        validate_pair=validate_pair,
        kicad_cli=Path("kicad-cli"),
    )


def test_success_publishes_complete_candidate_and_preserves_rules(staged_board, monkeypatch):
    board, plan = staged_board
    original = board.read_bytes()
    monkeypatch.setattr(transaction, "_native_refill_report", _report)
    monkeypatch.setattr(transaction, "_recipe_refill_report", _report)
    _publish(board, plan)
    assert board.read_bytes() != original
    assert "(start 20 20)" in board.read_text()
    assert board.with_suffix(".kicad_pro").read_text() == "{}"


@pytest.mark.parametrize("failure", ["native", "finding", "partition", "pair"])
def test_validation_failure_preserves_source_bytes(staged_board, monkeypatch, failure):
    board, plan = staged_board
    original = board.read_bytes()

    def native(path, executable):
        if failure == "native":
            raise RuntimeError("native failure")
        result = _report(path, executable)
        if failure == "finding" and path.parent.name == "candidate":
            result["violations"] = [{"type": "short", "items": [{"uuid": "new"}]}]
        return result

    def validate(_):
        if failure == "pair":
            raise RuntimeError("pair failure")

    if failure == "partition":
        calls = []

        def groups(_):
            calls.append(None)
            return (
                [frozenset({"a", "b"})] if len(calls) == 1 else [frozenset({"a"}), frozenset({"b"})]
            ), {"a": ("R1", "1"), "b": ("R2", "1")}

        monkeypatch.setattr(transaction.ConnectivityValidator, "extract_pad_occurrences", groups)
    monkeypatch.setattr(transaction, "_native_refill_report", native)
    monkeypatch.setattr(transaction, "_recipe_refill_report", native)
    with pytest.raises(RuntimeError):
        _publish(board, plan, validate)
    assert board.read_bytes() == original
    assert board.with_suffix(".kicad_pro").read_text() == "{}"


def test_intervening_edit_is_not_overwritten(staged_board, monkeypatch):
    board, plan = staged_board
    monkeypatch.setattr(transaction, "_native_refill_report", _report)
    monkeypatch.setattr(transaction, "_recipe_refill_report", _report)

    def edit_source(_):
        board.write_text("external edit")

    with pytest.raises(RuntimeError, match="changed during"):
        _publish(board, plan, edit_source)
    assert board.read_text() == "external edit"


def test_settled_fill_must_improve_even_when_raw_native_improves(staged_board, monkeypatch):
    board, plan = staged_board
    original = board.read_bytes()
    monkeypatch.setattr(transaction, "_native_refill_report", _report)
    monkeypatch.setattr(
        transaction, "_recipe_refill_report", lambda *_: _report(Path("baseline/board"), None)
    )
    with pytest.raises(RuntimeError, match="did not improve"):
        _publish(board, plan)
    assert board.read_bytes() == original


def test_raw_native_need_not_improve_when_settled_fill_does(staged_board, monkeypatch):
    board, plan = staged_board
    monkeypatch.setattr(
        transaction, "_native_refill_report", lambda *_: _report(Path("candidate/board"), None)
    )
    monkeypatch.setattr(transaction, "_recipe_refill_report", _report)
    before, after = _publish(board, plan)
    assert len(before["unconnected_items"]) == 1
    assert after["unconnected_items"] == []


def test_settled_fill_cannot_add_findings(staged_board, monkeypatch):
    board, plan = staged_board
    original = board.read_bytes()
    monkeypatch.setattr(transaction, "_native_refill_report", _report)

    def settled(path, executable):
        result = _report(path, executable)
        if path.parent.name == "candidate":
            result["violations"] = [{"type": "copper_sliver", "items": []}]
        return result

    monkeypatch.setattr(transaction, "_recipe_refill_report", settled)
    with pytest.raises(RuntimeError, match="settled DRC"):
        _publish(board, plan)
    assert board.read_bytes() == original
