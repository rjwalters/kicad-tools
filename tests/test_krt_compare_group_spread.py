"""`scripts/research/krt_compare.py` group-spread metric (Issue #5798).

Board 07's input gives every SDRAM net a short escape stub and finishes none
of them, so a capped run's per-group spread mixed "net not matched" with "net
not finished". The metric now reports the spread over *finished* members
separately; these tests pin that split on a tiny synthetic board.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "research" / "krt_compare.py"


@pytest.fixture(scope="module")
def krt_compare():
    spec = importlib.util.spec_from_file_location("krt_compare_group_spread", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _board(tracks: dict[str, tuple[float, float, float]]) -> str:
    """Two pads per net at (5, y) and (45, y); one F.Cu track from x0 to x1."""
    ids = {n: i + 1 for i, n in enumerate(tracks)}
    out = [
        "(kicad_pcb",
        "  (version 20240108)",
        '  (generator "test")',
        "  (general (thickness 1.6))",
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
        "  (setup (pad_to_mask_clearance 0))",
        '  (net 0 "")',
    ]
    out += [f'  (net {i} "{n}")' for n, i in ids.items()]
    k = 0
    for n, (y, _x0, _x1) in tracks.items():
        for x in (5, 45):
            k += 1
            out.append(
                f'  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-{k:012d}") '
                f"(at {x} {y}) "
                f'(property "Reference" "R{k}" (at 0 0 0) (layer "F.SilkS")) '
                f'(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") '
                f'(net {ids[n]} "{n}")))'
            )
    for n, (y, x0, x1) in tracks.items():
        out.append(
            f'  (segment (start {x0} {y}) (end {x1} {y}) (width 0.2) (layer "F.Cu") '
            f'(net {ids[n]}) (uuid "aaaaaaaa-0000-0000-0000-{ids[n]:012d}"))'
        )
    out.append(")")
    return "\n".join(out) + "\n"


def test_unfinished_members_are_separated_from_the_matched_spread(krt_compare, tmp_path):
    # A and B are finished pad to pad (40 mm each, plus a 3 mm jog on B);
    # STUB carries an 8 mm escape stub and never reaches its second pad.
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        _board({"A": (10, 5, 45), "B": (20, 5, 45), "STUB": (30, 5, 13)}).replace(
            '(segment (start 5 20) (end 45 20) (width 0.2) (layer "F.Cu") (net 2)',
            '(segment (start 5 20) (end 45 20) (width 0.2) (layer "F.Cu") (net 2) '
            '(uuid "aaaaaaaa-0000-0000-0000-000000000099"))\n'
            '  (segment (start 45 20) (end 45 23) (width 0.2) (layer "F.Cu") (net 2)',
        )
    )
    out = krt_compare.measure_group_spread(pcb, {"G": ["A", "B", "STUB"]})["G"]
    # The legacy number is dominated by the unfinished stub ...
    assert out["members"] == "3/3"
    assert out["spread_mm"] == pytest.approx(43.0 - 8.0)
    # ... the finished-only number is the real mismatch.
    assert out["complete"] == "2/3"
    assert out["spread_complete_mm"] == pytest.approx(3.0)


def test_fewer_than_two_finished_members_has_no_matched_spread(krt_compare, tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board({"A": (10, 5, 45), "STUB1": (20, 5, 13), "STUB2": (30, 5, 12)}))
    out = krt_compare.measure_group_spread(pcb, {"G": ["A", "STUB1", "STUB2"]})["G"]
    assert out["complete"] == "1/3"
    assert out["spread_complete_mm"] is None
    assert out["spread_mm"] == pytest.approx(40.0 - 7.0)


def test_group_spread_table_prints_both_numbers(krt_compare, capsys):
    results = {
        "07a/kct": {
            "board": "07a",
            "tool": "kct",
            "group_length_spread": {
                "G": {"spread_mm": 35.0, "complete": "2/3", "spread_complete_mm": 3.0},
                "H": {"spread_mm": 1.0, "complete": "1/3", "spread_complete_mm": None},
            },
        }
    }
    krt_compare.print_group_spread_table(results)
    out = capsys.readouterr().out
    assert "| 07a | kct | 35.0 / 3.0 (2/3) | 1.0 / -- (1/3) |" in out
