"""``kct route-link``: targeted single-link routing and probe (Issue #8310)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli import main  # noqa: E402
from kicad_tools.cli.route_link_cmd import LinkSpecError, parse_link_spec  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402

# R1.1 (110,110) and R2.1 (120,110) are on SIG.  A foreign F.Cu+B.Cu-free wall
# (a FOE track on F.Cu, x=115, y 100..120) splits F.Cu; B.Cu is open.
BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "FOE")
  (gr_rect (start 100 100) (end 130 120) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000001") (at 110 110)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG")))
  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000002") (at 120 110)
    (fp_text reference "R2" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG")))
  (segment (start 115 100.6) (end 115 119.4) (width 0.2) (layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000010"))
)
"""
LINK = "SIG R1.1 R2.1"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(BOARD)
    return p


def _run(capsys, *argv: str) -> tuple[int, dict]:
    rc = main(["route-link", "--format", "json", *argv])
    out = capsys.readouterr().out
    return rc, json.loads(out)


class TestParse:
    def test_pad_and_xy_with_layers(self):
        s = parse_link_spec("GND U1.3 12.5,7@B.Cu")
        assert (s.net, s.a.ref, s.a.pad) == ("GND", "U1", "3")
        assert s.b.xy == (12.5, 7.0) and s.b.layer == "B.Cu"

    @pytest.mark.parametrize("bad", ["SIG R1.1", "SIG R1 R2.1", "SIG 1,x R2.1", "SIG R1.1@ R2.1"])
    def test_bad_specs(self, bad):
        with pytest.raises(LinkSpecError):
            parse_link_spec(bad)


class TestProbe:
    def test_probe_leaves_board_bytes_unchanged(self, board, capsys):
        before = board.read_bytes()
        rc, doc = _run(capsys, str(board), "--link", LINK, "--probe")
        assert rc == 0 and doc["ok"] and doc["written"] is None
        link = doc["links"][0]
        assert link["routable"] and link["length_mm"] >= 9.0
        assert link["vias"] == 2 and link["layers"] == ["B.Cu", "F.Cu"]
        assert board.read_bytes() == before

    def test_layer_restriction_blocks_and_reports_blocker(self, board, capsys):
        before = board.read_bytes()
        rc, doc = _run(capsys, str(board), "--link", LINK, "--link-layers", "F.Cu", "--probe")
        assert rc == 1 and not doc["ok"]
        link = doc["links"][0]
        assert not link["routable"]
        assert link["blocker"]["net"] == "FOE" and link["blocker"]["layer"] == "F.Cu"
        assert link["blocker"]["distance_mm"] < link["blocker"]["required_mm"]
        assert board.read_bytes() == before

    def test_back_layer_only_has_no_endpoint_copper(self, board, capsys):
        rc, doc = _run(capsys, str(board), "--link", LINK, "--link-layers", "B.Cu", "--probe")
        assert rc == 1
        assert "no copper on the allowed layers" in doc["links"][0]["reason"]

    def test_endpoint_layer_not_in_link_layers(self, board, capsys):
        rc, doc = _run(
            capsys, str(board), "--link", "SIG R1.1@B.Cu R2.1", "--link-layers", "F.Cu", "--probe"
        )
        assert rc == 1 and "no copper on layer B.Cu" in doc["links"][0]["reason"]

    def test_xy_endpoint_without_copper(self, board, capsys):
        rc, doc = _run(capsys, str(board), "--link", "SIG 105,105 R2.1", "--probe")
        assert rc == 1 and "no 'SIG' via or track" in doc["links"][0]["reason"]

    def test_wrong_net_pad(self, board, capsys):
        rc, doc = _run(capsys, str(board), "--link", "FOE R1.1 R2.1", "--probe")
        assert rc == 1 and "is on net" in doc["links"][0]["reason"]


class TestCommit:
    def test_commit_writes_copper(self, board, capsys):
        rc, doc = _run(capsys, str(board), "--link", LINK)
        assert rc == 0 and doc["written"] == str(board)
        pcb = PCB.load(str(board))
        sig = [s for s in pcb.segments if s.net_number == 1]
        assert sig and len(pcb.vias) == 2
        assert {s.layer for s in sig} == {"F.Cu", "B.Cu"}

    def test_output_file_leaves_input_alone(self, board, tmp_path, capsys):
        out = tmp_path / "out.kicad_pcb"
        before = board.read_bytes()
        rc, _ = _run(capsys, str(board), "--link", LINK, "-o", str(out))
        assert rc == 0 and board.read_bytes() == before and out.exists()

    def test_via_at_forces_waypoint(self, board, capsys):
        rc, doc = _run(capsys, str(board), "--link", LINK, "--via-at", "112,114", "--probe")
        assert rc == 0, doc
        # forced via plus the two the wall requires
        assert doc["links"][0]["vias"] >= 1
        _rc, _doc = _run(capsys, str(board), "--link", LINK, "--via-at", "112,114")
        pcb = PCB.load(str(board))
        ox, oy = pcb.board_origin
        assert any(
            abs(v.position[0] + ox - 112) < 0.02 and abs(v.position[1] + oy - 114) < 0.02
            for v in pcb.vias
        )

    def test_via_at_illegal_site(self, board, capsys):
        # on top of the foreign wall
        rc, doc = _run(capsys, str(board), "--link", LINK, "--via-at", "115,110", "--probe")
        assert rc == 1 and "via site" in doc["links"][0]["reason"]

    def test_via_at_must_follow_link(self, board):
        with pytest.raises(SystemExit):
            main(["route-link", str(board), "--via-at", "1,1", "--link", LINK])

    def test_no_link_is_error(self, board, capsys):
        rc, doc = _run(capsys, str(board))
        assert rc == 1 and not doc["ok"]
