"""Tests for pcb move-via command (pcb_move_via module)."""

import json

import pytest

from kicad_tools.cli.pcb_move_via import run_move_via
from kicad_tools.schema.pcb import PCB


def _board(ox: float = 0.0, oy: float = 0.0, extra: str = "") -> str:
    def p(x, y):
        return f"{x + ox} {y + oy}"

    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "OTHER")
  (gr_rect (start {p(0, 0)}) (end {p(50, 50)}) (layer "Edge.Cuts") (stroke (width 0.1) (type default)))
  (segment (start {p(10, 10)}) (end {p(20, 10)}) (width 0.25) (layer "F.Cu") (net 1) (uuid "s1"))
  (segment (start {p(20, 10)}) (end {p(20, 20)}) (width 0.25) (layer "B.Cu") (net 1) (uuid "s2"))
  (segment (start {p(20, 10)}) (end {p(30, 10)}) (width 0.25) (layer "F.Cu") (net 2) (uuid "s3"))
  (via (at {p(20, 10)}) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
  (via blind (at {p(40, 40)}) (size 0.8) (drill 0.4) (layers "F.Cu" "In1.Cu") (net 1) (uuid "v2"))
  (via (at {p(40.02, 40)}) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "v3"))
{extra})
"""


@pytest.fixture
def pcb_file(tmp_path):
    f = tmp_path / "b.kicad_pcb"
    f.write_text(_board())
    return f


def _seg(pcb, uuid):
    return next(s for s in pcb.segments if s.uuid == uuid)


def test_move_by_coordinate_drags_same_net_only(pcb_file):
    assert run_move_via(pcb_file, at=(20, 10), to=(22, 12), net="SIG") == 0
    pcb = PCB.load(pcb_file)
    via = next(v for v in pcb.vias if v.uuid == "v1")
    assert via.position == pytest.approx((22, 12))
    assert (via.size, via.drill, via.layers, via.net_number) == (0.8, 0.4, ["F.Cu", "B.Cu"], 1)
    assert _seg(pcb, "s1").end == pytest.approx((22, 12))
    assert _seg(pcb, "s2").start == pytest.approx((22, 12))
    # Other-net track sharing the point must not move.
    assert _seg(pcb, "s3").start == pytest.approx((20, 10))
    # Far end untouched.
    assert _seg(pcb, "s1").start == pytest.approx((10, 10))


def test_select_by_uuid_and_by_offset(pcb_file):
    assert run_move_via(pcb_file, uuid="v1", by=(1, -1)) == 0
    via = next(v for v in PCB.load(pcb_file).vias if v.uuid == "v1")
    assert via.position == pytest.approx((21, 9))


def test_via_type_preserved(pcb_file):
    assert run_move_via(pcb_file, uuid="v2", by=(1, 0)) == 0
    via = next(v for v in PCB.load(pcb_file).vias if v.uuid == "v2")
    assert via.via_type == "blind"


def test_ambiguous_and_missing_do_not_write(pcb_file, capsys):
    before = pcb_file.read_text()
    assert run_move_via(pcb_file, at=(40, 40), tolerance=0.1, to=(1, 1)) == 1
    assert "Ambiguous" in capsys.readouterr().err
    assert run_move_via(pcb_file, at=(5, 5), to=(1, 1)) == 1
    assert run_move_via(pcb_file, at=(40, 40), tolerance=0.1, net="SIG", to=(41, 40)) == 0
    assert pcb_file.read_text() != before


def test_ambiguous_leaves_file_untouched(pcb_file):
    before = pcb_file.read_text()
    run_move_via(pcb_file, at=(40, 40), tolerance=0.1, to=(1, 1))
    assert pcb_file.read_text() == before


def test_dry_run_json(pcb_file, capsys):
    before = pcb_file.read_text()
    assert run_move_via(pcb_file, uuid="v1", to=(22, 12), dry_run=True, output_format="json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["via"]["old_position"] == [20, 10]
    assert out["via"]["new_position"] == [22, 12]
    assert out["endpoints_dragged"] == 2
    assert out["moved"] is False
    assert pcb_file.read_text() == before


def test_nonzero_origin_round_trip(tmp_path):
    f = tmp_path / "o.kicad_pcb"
    f.write_text(_board(116, 77))
    assert run_move_via(f, at=(20, 10), net="SIG", to=(22, 12)) == 0
    pcb = PCB.load(f)
    assert pcb.board_origin == pytest.approx((116, 77))
    via = next(v for v in pcb.vias if v.uuid == "v1")
    assert via.position == pytest.approx((22, 12))
    assert _seg(pcb, "s1").end == pytest.approx((22, 12))
    # Absolute coordinates address the same via.
    assert run_move_via(f, at=(138, 89), to=(139, 89), absolute=True) == 0
    via = next(v for v in PCB.load(f).vias if v.uuid == "v1")
    assert via.position == pytest.approx((23, 12))


def test_check_refuses_new_violation_and_allows_clean(tmp_path):
    # Foreign-net via parked near the destination so moving v1 onto it clashes.
    extra = '  (via (at 30 30) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "v4"))\n'
    f = tmp_path / "c.kicad_pcb"
    f.write_text(_board(extra=extra))
    before = f.read_text()
    assert run_move_via(f, uuid="v1", to=(30.3, 30), check=True) == 1
    assert f.read_text() == before
    assert run_move_via(f, uuid="v1", to=(22, 12), check=True) == 0
    assert f.read_text() != before
