"""Tests for the wire-through-symbol schematic lint (Issue #7996)."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.validate.rules.schematic_wire_symbol import (
    RULE_WIRE_THROUGH_SYMBOL,
    check_schematic_wire_symbol,
)
from tests.test_check_schematic_fields import _symbol, _write_sch


def _wire(x1: float, y1: float, x2: float, y2: float, n: int = 1) -> str:
    return (
        f"\t(wire (pts (xy {x1} {y1}) (xy {x2} {y2}))"
        f' (stroke (width 0) (type default)) (uuid "w-{n}"))\n'
    )


# R1 at (100, 100): body x in [98.984, 101.016], y in [97.46, 102.54];
# pins at (100, 96.19) and (100, 103.81).
_R1 = _symbol("R1", (100, 100), (104, 100))


def test_clean_schematic_has_no_findings(tmp_path: Path) -> None:
    body = _R1 + _wire(100, 90, 100, 96.19) + _wire(100, 103.81, 100, 110, 2)
    results = check_schematic_wire_symbol(_write_sch(tmp_path, body))
    assert not results.violations
    assert results.rules_checked_by_rule[RULE_WIRE_THROUGH_SYMBOL] == 1


def test_wire_through_resistor_body_flagged(tmp_path: Path) -> None:
    body = _R1 + _wire(90, 100, 110, 100)
    results = check_schematic_wire_symbol(_write_sch(tmp_path, body))
    assert len(results.violations) == 1
    v = results.violations[0]
    assert v.rule_id == RULE_WIRE_THROUGH_SYMBOL
    assert v.severity == "warning"
    assert v.items == ("R1",)
    assert "R1" in v.message and "(90.00, 100.00)" in v.message
    assert results.error_count == 0


def test_wire_ending_on_own_pin_exempt(tmp_path: Path) -> None:
    # Runs from the top pin down into the body: a stub into its own pin.
    body = _R1 + _wire(100, 96.19, 100, 100)
    assert not check_schematic_wire_symbol(_write_sch(tmp_path, body)).violations


def test_wire_touching_only_bbox_edge_not_flagged(tmp_path: Path) -> None:
    body = _R1 + _wire(98.984, 90, 98.984, 110)  # runs along the left edge
    body += _wire(90, 97.46, 110, 97.46, 2)  # runs along the top edge
    assert not check_schematic_wire_symbol(_write_sch(tmp_path, body)).violations


def test_rotated_symbol_uses_rotated_body(tmp_path: Path) -> None:
    # Rotated 90 degrees the body spans x in [97.46, 102.54], y ~ ±1.016.
    body = _symbol("R1", (100, 100), (104, 104), rotation=90)
    clear = check_schematic_wire_symbol(_write_sch(tmp_path, body + _wire(100, 90, 100, 95)))
    assert not clear.violations
    crossing = check_schematic_wire_symbol(
        _write_sch(tmp_path, body + _wire(100, 90, 100, 110), name="b.kicad_sch")
    )
    assert len(crossing.violations) == 1
