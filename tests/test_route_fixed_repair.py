"""Fixed-copper publication checks include failures and useful valid edits."""

import pytest

from kicad_tools.cli.route_fixed_repair import repair_fixed_copper
from kicad_tools.placement.routing import analyze_routing_placement
from tests.test_routing_placement_disposition import board_text


@pytest.mark.parametrize("change", ["segment", "via", "footprint", "net", "exception", "valid"])
def test_staged_repair_preserves_partial_and_accepts_only_valid_changes(tmp_path, change):
    board = tmp_path / "partial.kicad_pcb"
    original = board_text()
    board.write_text(original)
    disposition = analyze_routing_placement(board)

    def repair(candidate):
        assert candidate != board
        assert board.read_text() == original
        text = candidate.read_text()
        if change == "exception":
            candidate.write_text("broken")
            raise RuntimeError("repair crashed")
        if change == "segment":
            text = text.replace("(start 105 103)", "(start 105 104)")
        elif change == "via":
            text = text.replace("(at 106 103)", "(at 106 104)")
        elif change == "footprint":
            text = text.replace("(at 125 105)", "(at 124 105)")
        elif change == "net":
            text = text.replace('(net 2 "GOOD")', '(net 2 "RENAMED")')
        else:
            text = (
                text[:-1]
                + '(segment (start 105 107) (end 110 107) (width 0.2) (layer "F.Cu") (net 2)))'
            )
        candidate.write_text(text)
        if change == "valid":
            from kicad_tools.drc.repair_clearance import ClearanceRepairer

            ClearanceRepairer(candidate).save(candidate)
        assert board.read_text() == original
        return 0

    code, error = repair_fixed_copper(board, disposition, repair)
    if change == "valid":
        assert code == 0 and error is None
        assert board.read_text() != original
    else:
        assert code == 3 and error
        assert board.read_text() == original


def test_auto_fix_dispatch_rejects_fixed_copper_change(tmp_path, monkeypatch):
    from kicad_tools.cli import fix_drc_cmd, route_cmd, route_placement

    board = tmp_path / "partial.kicad_pcb"
    original = board_text()
    board.write_text(original)
    args = route_cmd._route_parser().parse_args([str(board)])
    route_placement.prepare(args, board)

    def repair(argv):
        from pathlib import Path

        candidate = Path(argv[0])
        assert candidate != board
        candidate.write_text(candidate.read_text().replace("(at 125 105)", "(at 124 105)"))
        return 0

    monkeypatch.setattr(fix_drc_cmd, "main", repair)
    assert route_cmd._run_auto_fix(board, args=args, quiet=True) == 3
    assert args._placement_repair_error
    assert board.read_text() == original
    assert route_placement.finish(args, 0) == 3


@pytest.mark.parametrize(
    "reference_style", ["absent", "empty_property", "empty_legacy", "shadowed_legacy"]
)
@pytest.mark.parametrize("change", ["position", "pad_geometry", "valid_copper"])
def test_anonymous_fixed_footprint_repair_guard(tmp_path, reference_style, change):
    from kicad_tools.schema.pcb import PCB

    board = tmp_path / "partial.kicad_pcb"
    reference = '(property "Reference" "X1" (at 0 0) (layer "F.SilkS"))'
    replacement = {
        "absent": "",
        "empty_property": reference.replace('"X1"', '""'),
        "empty_legacy": '(fp_text reference "" (at 0 0) (layer "F.SilkS"))',
        "shadowed_legacy": (
            '(fp_text reference "STALE" (at 0 0) (layer "F.SilkS"))'
            + reference.replace('"X1"', '""')
        ),
    }[reference_style]
    original = board_text().replace(reference, replacement)
    board.write_text(original)
    disposition = analyze_routing_placement(board)
    assert disposition.invalid_references == frozenset({""})
    assert disposition.direct_invalid_nets == frozenset({"BAD"})

    def repair(candidate):
        text = candidate.read_text()
        if change == "position":
            text = text.replace("(at 125 105)", "(at 124 105)")
        elif change == "pad_geometry":
            text = text.replace("(size 0.6 0.6)", "(size 0.8 0.6)", 1)
        else:
            text = (
                text[:-1]
                + '(segment (start 105 107) (end 110 107) (width 0.2) (layer "F.Cu") (net 2)))'
            )
        candidate.write_text(text)
        return 0

    code, error = repair_fixed_copper(board, disposition, repair)
    if change == "valid_copper":
        assert code == 0 and error is None
        assert any(segment.net_name == "GOOD" for segment in PCB.load(board).segments)
    else:
        assert code == 3 and error
        assert board.read_text() == original
