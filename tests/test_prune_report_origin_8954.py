"""``kct pcb prune`` reports sheet-absolute coordinates (Issue #8954).

On a board whose ``board_origin`` is not ``(0, 0)`` every coordinate prune's
report quotes must be sheet-absolute -- ``stubs_added[].start``/``.end`` in
all three plan lists, the uncut-loop ``witness`` dict and the uncut-loop
segment text -- matching ``pcb reinforce`` and ``pcb current-paths-audit``
(#8493, #8798, #8928). The library-level values (``NetPlan.stubs``,
``PruneResult.uncut_loops``) stay board-relative; the CLI report shifts them
by ``board_origin`` exactly once.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kicad_tools.pcb_prune import NetPlan, prune_board
from kicad_tools.schema.pcb import PCB

_ORIGIN = (100.0, 80.0)

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "SIG2")
"""

_uid = iter(f"u{i}" for i in range(1, 500))


def _fp(ref: str, x: float, y: float, net: int = 1, name: str = "SIG") -> str:
    return (
        f'  (footprint "X:P" (layer "F.Cu") (uuid "f-{ref}") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
        f'    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") '
        f'(net {net} "{name}"))\n'
        "  )\n"
    )


def _seg(a, b, layer="F.Cu", w=0.2, net: int = 1) -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) "
        f'(layer "{layer}") (net {net}) (uuid "s-{next(_uid)}"))\n'
    )


def _via(x, y, net: int = 1) -> str:
    return (
        f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {net}) '
        f'(uuid "v-{next(_uid)}"))\n'
    )


def _origin_board(tmp_path: Path, copper: str) -> Path:
    """A board whose Edge.Cuts rect puts ``board_origin`` at ``_ORIGIN``."""
    ox, oy = _ORIGIN
    body = _HEADER
    body += (
        f"  (gr_rect (start {ox} {oy}) (end {ox + 160} {oy + 160}) "
        '(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") '
        '(uuid "edge-1"))\n'
    )
    body += copper + ")\n"
    p = tmp_path / "origin.kicad_pcb"
    p.write_text(body)
    # Fixture sanity (the #8879 pattern): without a non-zero origin this file
    # cannot catch a missing or double-applied shift.
    assert tuple(PCB.load(p).board_origin) == pytest.approx(_ORIGIN)
    return p


def _specs(tmp_path: Path) -> Path:
    cp = tmp_path / "cp.json"
    cp.write_text(
        json.dumps(
            {
                "paths": [
                    {
                        "name": "p",
                        "net": "SIG",
                        "source": {"ref": "A", "pad": "1"},
                        "sink": {"ref": "B", "pad": "1"},
                        "continuous_a": 1.0,
                    }
                ]
            }
        )
    )
    return cp


def _cli_prune(capsys, p: Path, *extra: str) -> dict:
    from kicad_tools.cli.commands.pcb import _run_prune_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(
        ["pcb", "prune", str(p), "--dry-run", "--no-drc", "--format", "json", *extra]
    )
    assert _run_prune_command(args, p) == 0
    return json.loads(capsys.readouterr().out)


def _stub(layer: str, start, end, width: float) -> dict:
    return {"layer": layer, "start": list(start), "end": list(end), "width": width}


def test_cli_stubs_are_sheet_absolute_in_all_three_plan_lists(tmp_path, capsys, monkeypatch):
    """nets / region_skipped stub coordinates shift by board_origin (#8954)."""

    # Two nets with the proven stub-producing geometry (pad + short segment
    # ending beside a via), one inside the region box and one outside it, so
    # the real pipeline fills both ``nets`` and ``region_skipped``.
    def net_copper(net: int, name: str, dx: float, dy: float) -> str:
        return (
            _fp("P" + name, 110 + dx, 90 + dy, net=net, name=name)
            + _seg((110 + dx, 90 + dy), (114.82 + dx, 90 + dy), net=net)
            + _via(115 + dx, 90.08 + dy, net=net)
        )

    copper = net_copper(1, "SIG", 0.0, 0.0) + net_copper(2, "SIG2", 20.0, 20.0)
    p = _origin_board(tmp_path, copper)
    region = (5.0, 5.0, 20.0, 20.0)  # board frame: covers SIG, not SIG2
    _, res = prune_board(p, verify_drc=False, region=region)
    assert res.accepted and res.region_skipped
    assert [pl.net_name for pl in res.accepted] == ["SIG"]
    assert [pl.net_name for pl in res.region_skipped] == ["SIG2"]

    out = _cli_prune(capsys, p, "--region", "5,5,20,20")
    ox, oy = _ORIGIN
    for plans, key in ((res.accepted, "nets"), (res.region_skipped, "region_skipped")):
        reported = out[key][0]["stubs_added"]
        library = [s for pl in plans for s in pl.stubs]
        assert library and len(reported) == len(library)
        for (layer, a, b, w), got in zip(library, reported, strict=True):
            assert got["layer"] == layer and got["width"] == w
            assert got["start"] == pytest.approx([a[0] + ox, a[1] + oy])
            assert got["end"] == pytest.approx([b[0] + ox, b[1] + oy])

    # The text report quotes the same sheet positions.
    from kicad_tools.cli.commands.pcb import _run_prune_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(
        ["pcb", "prune", str(p), "--dry-run", "--no-drc", "--region", "5,5,20,20"]
    )
    assert _run_prune_command(args, p) == 0
    text = capsys.readouterr().out
    layer, a, b, w = res.accepted[0].stubs[0]
    assert f"stub {layer} [{a[0] + ox}, {a[1] + oy}] -> [{b[0] + ox}, {b[1] + oy}] w={w}" in text


def test_cli_forced_plan_stubs_shift_by_exactly_the_origin(tmp_path, capsys, monkeypatch):
    """Forced plans: stub start/end are the board-relative value + board_origin."""
    import kicad_tools.pcb_prune as mod

    p = _origin_board(tmp_path, _fp("A", 110, 90) + _fp("B", 115, 90) + _seg((110, 90), (115, 90)))
    sig = NetPlan(1, "SIG", stubs=[("F.Cu", (10.0, 10.0), (12.5, 10.0), 0.2)])
    monkeypatch.setattr(mod, "plan_prune", lambda *a, **k: [sig])
    out = _cli_prune(capsys, p)
    assert out["nets"][0]["stubs_added"] == [_stub("F.Cu", (110.0, 90.0), (112.5, 90.0), 0.2)]


def test_cli_rejected_plan_stubs_are_sheet_absolute(tmp_path, capsys, monkeypatch):
    """rejected_by_drc stubs (the audit gate) quote sheet positions too."""
    import kicad_tools.pcb_prune as mod

    p = _origin_board(tmp_path, _fp("A", 110, 90) + _fp("B", 115, 90) + _seg((110, 90), (115, 90)))
    cp = _specs(tmp_path)
    # A second path beside the trunk: the gate refuses it as ambiguous.
    looping = NetPlan(
        1,
        "SIG",
        stubs=[("F.Cu", (10, 10), (12, 12), 0.2), ("F.Cu", (12, 12), (15, 10), 0.2)],
    )
    monkeypatch.setattr(mod, "plan_prune", lambda *a, **k: [looping])

    out = _cli_prune(capsys, p, "--current-paths", str(cp))
    assert out["nets"] == []
    assert out["rejected_by_drc"][0]["stubs_added"] == [
        _stub("F.Cu", (110.0, 90.0), (112.0, 92.0), 0.2),
        _stub("F.Cu", (112.0, 92.0), (115.0, 90.0), 0.2),
    ]


def test_cli_plan_net_uncut_loop_text_is_sheet_absolute(tmp_path, capsys):
    """_plan_net's all-wide loop quotes sheet positions in its segment text."""
    segs = [
        ((200, 180), (200, 183), "F.Cu", 0.6),
        ((200, 183), (210, 183), "F.Cu", 0.6),
        ((210, 183), (210, 180), "F.Cu", 0.6),
        ((200, 180), (202, 180), "F.Cu", 0.6),
        ((202, 180), (208, 180), "B.Cu", 0.6),
        ((208, 180), (210, 180), "F.Cu", 0.6),
    ]
    copper = (
        _fp("A", 200, 180)
        + _fp("B", 210, 180)
        + "".join(_seg(*s) for s in segs)
        + _via(202, 180)
        + _via(208, 180)
    )
    p = _origin_board(tmp_path, copper)
    cp = _specs(tmp_path)

    out = _cli_prune(capsys, p, "--current-paths", str(cp), "--wide", "0.5")
    (loop,) = out["uncut_loops"]
    assert loop["net"] == "SIG"
    assert "witness" not in loop
    assert loop["segments"] == [
        "(200.0, 180.0)->(200.0, 183.0) F.Cu",
        "(200.0, 183.0)->(210.0, 183.0) F.Cu",
        "(210.0, 183.0)->(210.0, 180.0) F.Cu",
    ]


def _crossing_origin_board(tmp_path: Path) -> tuple[Path, Path]:
    """The #8368 midspan-crossing board at ``_ORIGIN``: the audit, not prune's
    model, reports the loop, so prune quotes its witness."""
    copper = (
        _fp("A", 200, 184)
        + _fp("B", 204, 180)
        + _fp("C", 209.4, 174.4)
        + _seg((200, 184), (200, 180), w=1.2)
        + _seg((200, 180), (204, 180), w=2.6)
        + _seg((200.06, 183.8), (209.4, 174.4), w=0.5)
    )
    return _origin_board(tmp_path, copper), _specs(tmp_path)


def test_cli_uncut_witness_is_sheet_absolute_and_agrees_with_the_audit(tmp_path, capsys):
    """The audit-reported loop's witness dict and segment text are sheet-absolute,
    and equal what ``pcb current-paths-audit`` reports for the same copper."""
    pcb_path, cp = _crossing_origin_board(tmp_path)
    out = _cli_prune(capsys, pcb_path, "--current-paths", str(cp))
    (loop,) = out["uncut_loops"]
    assert loop["witness"]["start"] == pytest.approx([200.0, 180.0])
    assert loop["witness"]["end"] == pytest.approx([204.0, 180.0])
    assert loop["segments"] == ["segment (200.000, 180.000)->(204.000, 180.000) on F.Cu"]
    # Blocking branches are shifted too; ``width`` (not a coordinate) is not.
    branches = loop["witness"]["blocking_branches"]
    assert branches[0]["start"] == pytest.approx([200.06, 183.8])
    assert branches[0]["end"] == pytest.approx([209.4, 174.4])
    assert loop["witness"]["width"] == 2.6
    assert "position" not in loop["witness"]

    from kicad_tools.cli.commands.pcb import run_pcb_command

    args = argparse.Namespace(
        pcb_command="current-paths-audit",
        pcb=str(pcb_path),
        current_paths=str(cp),
        format="json",
    )
    assert run_pcb_command(args) == 0
    (res,) = json.loads(capsys.readouterr().out)["resolutions"]
    # Same copper, same sheet coordinate from both commands.
    assert res["witness"]["start"] == pytest.approx(loop["witness"]["start"])
    assert "segment (200.000, 180.000)->(204.000, 180.000)" in res["reason"]


def test_cli_via_witness_shifts_position_without_injecting_keys(tmp_path, capsys, monkeypatch):
    """A via-kind witness has ``position`` only; no ``start``/``end`` is injected."""
    import kicad_tools.pcb_prune as mod

    p = _origin_board(tmp_path, _fp("A", 110, 90) + _fp("B", 115, 90) + _seg((110, 90), (115, 90)))
    cp = _specs(tmp_path)
    via_entry = {
        "net": "SIG",
        "segments": ["via barrel at (10.000, 20.000)"],
        "min_width": 0.3,
        "reason": "the current-path audit still sees a loop/parallel path",
        "witness": {"kind": "via", "layer": "F.Cu", "position": [10.0, 20.0]},
    }
    monkeypatch.setattr(mod, "audit_loops_left", lambda *a, **k: [via_entry])

    out = _cli_prune(capsys, p, "--current-paths", str(cp))
    (loop,) = out["uncut_loops"]
    assert loop["witness"] == {"kind": "via", "layer": "F.Cu", "position": [110.0, 100.0]}
    assert loop["segments"] == ["via barrel at (110.000, 100.000)"]


def test_zero_origin_report_is_unshifted(tmp_path, capsys):
    """Origin (0, 0): the shift is a no-op -- every value keeps its board frame."""
    from tests.test_pcb_prune import _specs as _gate_specs_file
    from tests.test_pcb_prune import _via_loop_board

    p = _via_loop_board(tmp_path, trunk_w=0.6, jog_w=0.6)
    assert tuple(PCB.load(p).board_origin) == (0.0, 0.0)
    cp = _gate_specs_file(tmp_path)

    out = _cli_prune(capsys, p, "--current-paths", str(cp), "--wide", "0.5")
    (loop,) = out["uncut_loops"]
    # Byte-identical to the pre-#8954 board-relative rendering (same sort).
    assert loop["segments"] == [
        "(100.0, 100.0)->(100.0, 103.0) F.Cu",
        "(100.0, 103.0)->(110.0, 103.0) F.Cu",
        "(110.0, 103.0)->(110.0, 100.0) F.Cu",
    ]


def test_zero_origin_stubs_are_unshifted(tmp_path, capsys, monkeypatch):
    import kicad_tools.pcb_prune as mod
    from tests.test_pcb_prune import _board

    p = _board(tmp_path, [("A", 100, 100)], [((100, 100), (104.82, 100))], vias=[(105, 100.08)])
    sig = NetPlan(1, "SIG", stubs=[("F.Cu", (10.0, 10.0), (12.5, 10.0), 0.2)])
    monkeypatch.setattr(mod, "plan_prune", lambda *a, **k: [sig])
    out = _cli_prune(capsys, p)
    assert out["nets"][0]["stubs_added"] == [_stub("F.Cu", (10.0, 10.0), (12.5, 10.0), 0.2)]


def test_library_values_stay_board_relative(tmp_path):
    """The library keeps its frame: PruneResult.uncut_loops and NetPlan.stubs
    are board-relative; only the CLI report shifts them."""
    from tests.test_pcb_prune import _via_loop_board

    p = _via_loop_board(tmp_path, trunk_w=0.6, jog_w=0.6)
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=0.5)
    (loop,) = res.uncut_loops
    assert loop["segments"][0].startswith("(100.0, 100.0)")
    assert loop["segment_ends"][0] == {"a": [100.0, 100.0], "b": [100.0, 103.0], "layer": "F.Cu"}
    assert "witness" not in loop

    # Real planning on the non-zero-origin board: the same board-frame
    # geometry as the proven zero-origin stub case, so the library's stubs
    # must come out board-relative (sheet coords would exceed the origin).
    copper = _fp("A", 110, 90) + _seg((110, 90), (114.82, 90)) + _via(115, 90.08)
    plain = _origin_board(tmp_path, copper)
    _, res2 = prune_board(plain, verify_drc=False)
    stubs = [s for pl in res2.accepted for s in pl.stubs]
    assert stubs
    for _layer, a, b, _w in stubs:
        for x, y in (a, b):
            assert x < _ORIGIN[0] and y < _ORIGIN[1]
