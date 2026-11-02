"""Tests for ``kct pcb prune`` / ``kicad_tools.pcb_prune`` (issue #8216)."""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path

from kicad_tools.pcb_prune import prune_board
from kicad_tools.schema.pcb import PCB

HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
"""

_uid = count(1)


def _fp(ref: str, x: float, y: float, w: float = 1, h: float = 1) -> str:
    return (
        f'  (footprint "X:P" (layer "F.Cu") (uuid "f-{ref}") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
        f'    (pad "1" smd rect (at 0 0) (size {w} {h}) (layers "F.Cu") (net 1 "SIG"))\n'
        "  )\n"
    )


def _seg(a, b, layer="F.Cu", w=0.2) -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) "
        f'(layer "{layer}") (net 1) (uuid "s-{next(_uid)}"))\n'
    )


def _via(x, y) -> str:
    return (
        f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) '
        f'(uuid "v-{next(_uid)}"))\n'
    )


def _board(tmp_path: Path, pads, segs, vias=()) -> Path:
    body = HEADER + "".join(_fp(*pad) for pad in pads)
    body += "".join(_seg(*s) if isinstance(s[0], tuple) else _seg(s) for s in segs)
    body += "".join(_via(*v) for v in vias) + ")\n"
    p = tmp_path / "b.kicad_pcb"
    p.write_text(body)
    return p


def _run(p: Path):
    pcb, res = prune_board(p, verify_drc=False)
    return pcb, res


def test_redundant_branch_next_to_via_is_removed_and_main_kept(tmp_path):
    segs = [
        ((100, 100), (102, 100)),
        ((102, 100), (105, 100)),
        ((105, 100), (110, 100)),
        # branch with an off-centre end landing on the via
        ((102, 100), (102, 102)),
        ((102, 102), (105.15, 102)),
        ((105.15, 102), (105.15, 100.25)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs, vias=[(105, 100)])
    pcb, res = _run(p)
    assert res.stats["segments_removed"] == 3
    assert len(pcb.segments) == 3
    assert {s.start for s in pcb.segments} | {s.end for s in pcb.segments} >= {
        (100, 100),
        (110, 100),
    }


def test_overlap_only_off_centre_ends_are_snapped(tmp_path):
    segs = [((100, 100), (104.8, 100)), ((110, 100), (105.2, 100))]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs, vias=[(105, 100)])
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 2}
    ends = sorted(s.end for s in pcb.segments)
    assert ends == [(105, 100), (105, 100)]
    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)
    assert sorted(s.end for s in PCB.load(out).segments) == [(105, 100), (105, 100)]


def test_same_layer_loop_removes_longer_detour(tmp_path):
    segs = [
        ((100, 100), (102, 100)),
        ((102, 100), (103, 100)),
        ((103, 100), (110, 100)),
        ((102, 100), (102, 101)),
        ((102, 101), (103, 101)),
        ((103, 101), (103, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs)
    pcb, res = _run(p)
    assert res.stats["segments_removed"] == 3
    assert ((102, 100), (103, 100)) in {(s.start, s.end) for s in pcb.segments}


def test_removal_that_would_disconnect_a_pad_is_kept(tmp_path):
    segs = [
        ((100, 100), (110, 100)),
        ((105, 100), (105, 103)),  # spur to pad C: not redundant
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100), ("C", 105, 103)], segs)
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 0}
    assert len(pcb.segments) == 2


def test_cli_dry_run_json_leaves_file_untouched(tmp_path, capsys):
    from kicad_tools.cli.parser import create_parser

    segs = [
        ((100, 100), (102, 100)),
        ((102, 100), (103, 100)),
        ((103, 100), (110, 100)),
        ((102, 100), (102, 101)),
        ((102, 101), (103, 101)),
        ((103, 101), (103, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs)
    before = p.read_text()
    args = create_parser().parse_args(
        ["pcb", "prune", str(p), "--dry-run", "--no-drc", "--format", "json"]
    )
    from kicad_tools.cli.commands.pcb import _run_prune_command

    assert _run_prune_command(args, p) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["removed"]["segments"] == 3 and out["drc_verified"] is False
    assert p.read_text() == before


def _connected(pcb, a, b) -> bool:
    """Endpoint union-find: is ``a`` linked to ``b`` through the segments?"""
    adj: dict = {}
    for s in pcb.segments:
        adj.setdefault(s.start, set()).add(s.end)
        adj.setdefault(s.end, set()).add(s.start)
    seen, stack = {a}, [a]
    while stack:
        for v in adj.get(stack.pop(), ()):
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return b in seen


def test_wide_pad_off_centre_entry_keeps_whole_net(tmp_path):
    """A track entering a 3x0.5 pad 1.2 mm off-centre is anchored by the full
    pad shape, not the inscribed circle; the net must survive intact."""
    segs = [
        ((101.2, 100), (105, 100)),
        ((105, 100), (107, 100)),
        ((107, 100), (110, 100)),
        ((105, 100), (105, 102)),
        ((105, 102), (107, 102)),
        ((107, 102), (107, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100, 3, 0.5), ("B", 110, 100)], segs)
    pcb, _ = _run(p)
    # The entry end sits inside the pad, so it is anchored (it may be snapped
    # to the pad centre) and the pad stays connected to B.
    entry = min(pcb.segments, key=lambda s: s.start[0]).start
    assert entry in {(101.2, 100), (100, 100)}
    assert _connected(pcb, entry, (110, 100))


def test_tangent_capsule_jog_is_kept(tmp_path):
    """Neighbours whose capsules are exactly tangent are not joined by KiCad."""
    segs = [
        ((100, 100), (101, 100)),
        ((101, 100), (101, 100.2)),  # the jog
        ((101, 100.2), (102, 100.2)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 102, 100.2)], segs)
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 0}
    assert len(pcb.segments) == 3


def test_collinear_step_run_is_not_thinned(tmp_path):
    """Removing alternate 0.05 mm steps would leave overlap-only joints."""
    segs = [((100 + 0.05 * k, 100), (100 + 0.05 * (k + 1), 100)) for k in range(10)]
    p = _board(tmp_path, [("A", 100, 100), ("B", 100.5, 100)], segs)
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 0}
    assert len(pcb.segments) == 10


def _cli(tmp_path, p, *extra):
    from kicad_tools.cli.commands.pcb import _run_prune_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(["pcb", "prune", str(p), "--format", "json", *extra])
    return _run_prune_command(args, p)


def test_cli_refuses_to_write_unverified_result(tmp_path, capsys):
    segs = [
        ((100, 100), (102, 100)),
        ((102, 100), (103, 100)),
        ((103, 100), (110, 100)),
        ((102, 100), (102, 101)),
        ((102, 101), (103, 101)),
        ((103, 101), (103, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs)
    before = p.read_text()
    assert _cli(tmp_path, p, "--no-drc") == 1
    assert p.read_text() == before
    capsys.readouterr()
    assert _cli(tmp_path, p, "--no-drc", "--unverified") == 0
    assert p.read_text() != before
