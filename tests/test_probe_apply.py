"""``kct probe`` / ``kct apply --checked``: batch check-then-write for proposed copper (Issue #9590).

Mechanics are exercised on a small inline board (deterministic geometry);
the ``kct check`` agreement acceptance criterion runs on the committed
board ``boards/00-simple-led`` with item coordinates derived from the
board's own pads at runtime, so a board regeneration cannot silently
invalidate the fixtures.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli import main  # noqa: E402
from kicad_tools.cli.probe_cmd import (  # noqa: E402
    PROBE_CHECK_CATEGORIES,
    ItemsFileError,
    _append_for_write,
    load_items_file,
    parse_items,
)
from kicad_tools.schema.pcb import PCB  # noqa: E402

# 2-layer board, no stackup declaration (jlcpcb floors apply: 0.127 mm
# clearance / trace width).  R1.1 is a SIG pad at (110, 110); a FOE track
# runs vertically at x=115.  The outline is (100,100)-(130,120).
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
  (segment (start 115 100.6) (end 115 119.4) (width 0.2) (layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000010"))
)
"""

# The same board plus a pre-existing clearance error (FOE copper 0.1 mm from
# the R1.1 pad): a probe of clean items elsewhere must still pass, proving
# baseline subtraction keeps pre-existing board problems out of the verdict.
DIRTY_BOARD = BOARD.replace(
    """  (segment (start 115 100.6) (end 115 119.4) (width 0.2) (layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000010"))
""",
    """  (segment (start 115 100.6) (end 115 119.4) (width 0.2) (layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000010"))
  (segment (start 110.6 105) (end 110.6 115) (width 0.2) (layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000011"))
""",
)

# The same board plus one FOE via (0.6 mm size / 0.3 mm drill) at (120, 110):
# a via's dedupe key is net, position and layers only, so proposing a
# different size or drill at that exact position is a position collision,
# not a duplicate -- the requested copper is NOT on the board (Issue #9674
# review).
VIA_BOARD = BOARD.replace(
    """  (segment (start 115 100.6) (end 115 119.4) (width 0.2) (layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000010"))
""",
    """  (segment (start 115 100.6) (end 115 119.4) (width 0.2) (layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000010"))
  (via (at 120 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000020"))
""",
)

COMMITTED = (
    Path(__file__).resolve().parent.parent
    / "boards"
    / "00-simple-led"
    / "output"
    / "simple_led.kicad_pcb"
)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    return _write_board(tmp_path, BOARD)


@pytest.fixture
def via_board(tmp_path: Path) -> Path:
    return _write_board(tmp_path, VIA_BOARD)


def _write_board(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(text)
    return path


def _items_file(tmp_path: Path, *items: dict) -> Path:
    path = tmp_path / "items.json"
    path.write_text(json.dumps({"items": list(items)}))
    return path


def _run(capsys, *argv: str) -> tuple[int, dict]:
    rc = main(list(argv))
    out = capsys.readouterr().out
    return rc, json.loads(out)


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _seg(net: str, x1: float, y1: float, x2: float, y2: float, width: float = 0.25):
    return {
        "type": "segment",
        "net": net,
        "layer": "F.Cu",
        "width": width,
        "start": [x1, y1],
        "end": [x2, y2],
    }


class TestParseItems:
    def test_minimal_shapes(self):
        items = parse_items(
            {
                "items": [
                    _seg("SIG", 1, 2, 3, 4),
                    {
                        "type": "arc",
                        "net": "SIG",
                        "layer": "F.Cu",
                        "width": 0.2,
                        "start": [3, 4],
                        "mid": [4, 5],
                        "end": [5, 4],
                    },
                    {"type": "via", "net": "GND", "at": [5, 6]},
                ]
            }
        )
        assert [i.kind for i in items] == ["segment", "arc", "via"]
        assert items[2].size == 0.6 and items[2].drill == 0.3
        assert items[2].layers == ("F.Cu", "B.Cu")

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"items": "no"},
            {"items": []},
            {"items": [{"type": "poly", "net": "GND"}]},
            {"items": [{"type": "segment", "net": "GND", "widht": 0.2}]},
            {"items": [{"type": "segment", "net": "GND", "layer": "F.Cu", "width": 0.2}]},
            {
                "items": [
                    {
                        "type": "segment",
                        "net": "GND",
                        "layer": "F.Cu",
                        "width": 0,
                        "start": [0, 0],
                        "end": [1, 1],
                    }
                ]
            },
            {"items": [{"type": "via", "net": "GND", "at": [1, 1], "size": 0.3, "drill": 0.3}]},
            {"items": [{"type": "via", "net": "GND", "at": [1, 1, 2]}]},
        ],
    )
    def test_rejected_payloads(self, payload):
        with pytest.raises(ItemsFileError):
            parse_items(payload)

    @pytest.mark.parametrize("kind", [["segment"], {"arc": 1}])
    def test_malformed_type_keeps_type_diagnostic(self, kind):
        # An unhashable 'type' must not raise TypeError from the dict
        # lookup; it gets the indexed diagnostic like any unknown type.
        with pytest.raises(ItemsFileError, match="item 1: 'type' must be segment, arc or via"):
            parse_items({"items": [{"type": kind, "net": "GND"}]})

    def test_load_items_file_missing(self, tmp_path):
        with pytest.raises(ItemsFileError, match="cannot read"):
            load_items_file(tmp_path / "absent.json")

    @pytest.mark.parametrize(
        ("bad_item", "expected"),
        [
            # A segment carrying 'mid' means an arc: without the hint the
            # author gets a chord probed and written, with no diagnostic.
            (_seg("SIG", 1, 2, 3, 4).copy() | {"mid": [2, 3]}, 'use "type": "arc"'),
            ({"type": "via", "net": "SIG", "at": [5, 6], "layer": "F.Cu"}, "'layer'"),
            ({"type": "via", "net": "SIG", "at": [5, 6], "width": 0.25}, "'width'"),
            # A missing arc midpoint must name the item, not raise KeyError.
            (
                {
                    "type": "arc",
                    "net": "SIG",
                    "layer": "F.Cu",
                    "width": 0.2,
                    "start": [3, 4],
                    "end": [5, 4],
                },
                "'mid'",
            ),
        ],
    )
    def test_fields_outside_the_items_type_are_rejected(self, bad_item, expected):
        # The malformed entry sits second in a batch: the error must name
        # the 1-based item index and the offending field.
        with pytest.raises(ItemsFileError, match="item 2") as exc_info:
            parse_items({"items": [_seg("SIG", 0, 0, 1, 1), bad_item]})
        assert expected in str(exc_info.value)


class TestProbe:
    def test_good_batch_passes_and_writes_nothing(self, board, capsys):
        before = board.read_bytes()
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(board.parent, _seg("SIG", 105, 105, 112, 105))),
            "--format",
            "json",
        )
        assert rc == 0 and doc["ok"] is True
        assert doc["problems"] == [] and doc["written"] is None
        assert doc["command"] == "probe" and doc["error_count"] == 0
        assert set(PROBE_CHECK_CATEGORIES) <= set(doc["categories"])
        assert board.read_bytes() == before

    def test_short_to_foreign_net_names_item(self, board, capsys):
        # SIG copper laid straight over the FOE track: physical overlap.
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(board.parent, _seg("SIG", 115, 105, 115, 112))),
            "--format",
            "json",
        )
        assert rc == 1 and doc["ok"] is False
        rule_ids = {p["rule_id"] for p in doc["problems"]}
        assert rule_ids & {
            "clearance_segment_segment",
            "clearance_segment_pad",
            "clearance_segment_via",
        }
        short = next(p for p in doc["problems"] if p["message"].startswith("SHORT"))
        assert short["proposed_item"] == 1
        assert "'SIG'" in short["message"] and "'FOE'" in short["message"]

    def test_clearance_miss_fails(self, board, capsys):
        # Edge-to-edge gap 0.12 mm < 0.127 mm jlcpcb floor
        # (center distance 0.345 = 0.12 + FOE half-width 0.1 + item half-width 0.125).
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(board.parent, _seg("SIG", 115.345, 105, 115.345, 112))),
            "--format",
            "json",
        )
        assert rc == 1
        assert any(p["rule_id"] == "clearance_segment_segment" for p in doc["problems"])

    def test_gap_at_exact_limit_passes(self, board, capsys):
        # Center distance 0.352 = 0.127 clearance + half-widths (0.1 + 0.125):
        # exactly the limit must pass (#9592 row 7 -- same tolerance as kct check).
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(board.parent, _seg("SIG", 115.352, 105, 115.352, 112))),
            "--format",
            "json",
        )
        assert rc == 0 and doc["ok"] is True, doc["problems"]

    def test_same_net_slit_fails_via_fab_gap_pass(self, board, capsys):
        # Issue #9590 review: a same-net parallel track 0.08 mm from an
        # existing track passes every pairwise rule (same net, no short) but
        # fails ``kct check``'s default net-agnostic ``physical_copper_gap``
        # pass under jlcpcb -- the probe must run that category too.
        # Center distance 0.305 = 0.08 edge gap + half-widths (0.1 + 0.125).
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(board.parent, _seg("FOE", 115.305, 105, 115.305, 112))),
            "--format",
            "json",
        )
        assert rc == 1
        slit = next(p for p in doc["problems"] if p["rule_id"] == "physical_copper_gap")
        assert slit["severity"] == "error"
        assert slit["proposed_item"] == 1

    def test_edge_clearance_fails(self, board, capsys):
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(board.parent, _seg("SIG", 128, 105, 131.5, 105))),
            "--format",
            "json",
        )
        assert rc == 1
        edge = next(p for p in doc["problems"] if p["rule_id"] == "edge_clearance_trace")
        assert edge["proposed_item"] == 1

    def test_two_proposed_items_clash_each_other(self, board, capsys):
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(
                _items_file(
                    board.parent,
                    _seg("SIG", 105, 106, 112, 106),
                    _seg("FOE", 105, 106.3, 112, 106.3),  # 0.05 mm apart
                )
            ),
            "--format",
            "json",
        )
        assert rc == 1
        clash = next(p for p in doc["problems"] if p["rule_id"] == "clearance_segment_segment")
        assert clash["proposed_item"] in (1, 2)

    def test_preexisting_board_problems_do_not_fail_probe(self, tmp_path, capsys):
        dirty = _write_board(tmp_path, DIRTY_BOARD)
        # The FOE-at-0.1mm-from-pad error is pre-existing; a clean item far
        # from it must pass (baseline subtraction).
        rc, doc = _run(
            capsys,
            "probe",
            str(dirty),
            str(_items_file(tmp_path, _seg("SIG", 120, 105, 125, 105))),
            "--format",
            "json",
        )
        assert rc == 0 and doc["problems"] == [], doc["problems"]

    def test_unknown_net_and_layer_named(self, board, capsys):
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(
                _items_file(
                    board.parent, _seg("NOPE", 105, 105, 106, 105), _seg("SIG", 105, 107, 106, 107)
                )
            ),
            "--format",
            "json",
        )
        assert rc == 1
        assert len(doc["problems"]) == 1
        problem = doc["problems"][0]
        assert problem["rule_id"] == "items_file"
        assert problem["proposed_item"] == 1
        assert "unknown net" in problem["message"]

        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(
                _items_file(
                    board.parent, _seg("SIG", 105, 105, 106, 105).copy() | {"layer": "In9.Cu"}
                )
            ),
            "--format",
            "json",
        )
        assert rc == 1 and "unknown layer" in doc["problems"][0]["message"]

    def test_too_thin_track_fails(self, board, capsys):
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(board.parent, _seg("SIG", 105, 105, 112, 105, width=0.09))),
            "--format",
            "json",
        )
        assert rc == 1
        assert any(p["rule_id"] == "dimension_trace_width" for p in doc["problems"])

    def test_via_items(self, board, capsys):
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(board.parent, {"type": "via", "net": "SIG", "at": [120, 110]})),
            "--format",
            "json",
        )
        assert rc == 0, doc["problems"]

        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(
                _items_file(
                    board.parent, {"type": "via", "net": "SIG", "at": [120, 110], "drill": 0.2}
                )
            ),
            "--format",
            "json",
        )
        assert rc == 1
        assert any(
            "via" in p["rule_id"] and "drill" in p["message"].lower() for p in doc["problems"]
        )

    def test_arc_items(self, board, capsys):
        good_arc = {
            "type": "arc",
            "net": "SIG",
            "layer": "F.Cu",
            "width": 0.25,
            "start": [118, 104],
            "mid": [120, 105.5],
            "end": [122, 104],
        }
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(board.parent, good_arc)),
            "--format",
            "json",
        )
        assert rc == 0, doc["problems"]

        # The same arc flattened over the R1.1 pad on a foreign net must be
        # caught through the check-pass tessellation.
        bad = {
            "type": "arc",
            "net": "FOE",
            "layer": "F.Cu",
            "width": 0.25,
            "start": [108, 110],
            "mid": [110, 110.3],
            "end": [112, 110],
        }
        rc, doc = _run(
            capsys, "probe", str(board), str(_items_file(board.parent, bad)), "--format", "json"
        )
        assert rc == 1
        assert any(p["message"].startswith("SHORT") for p in doc["problems"])

    def test_missing_files(self, tmp_path, capsys):
        rc, doc = _run(
            capsys,
            "probe",
            str(tmp_path / "absent.kicad_pcb"),
            str(tmp_path / "x.json"),
            "--format",
            "json",
        )
        assert rc == 1 and "board not found" in doc["error"]


class TestApplyChecked:
    def test_apply_writes_and_backs_up(self, board, capsys):
        original = board.read_bytes()
        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(board.parent, _seg("SIG", 105, 105, 112, 105))),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 0 and doc["ok"] is True
        assert doc["written"] == str(board)
        assert doc["backup"] == str(board) + ".bak"
        assert doc["items_written"] == 1 and doc["items_skipped_as_duplicate"] == 0
        reloaded = PCB.load(str(board))
        assert any(s.net_name == "SIG" and s.layer == "F.Cu" for s in reloaded.segments)
        assert (board.parent / "b.kicad_pcb.bak").read_bytes() == original

    def test_apply_all_duplicate_reports_zero_written(self, board, capsys):
        # Issue #9674: proposing the board's own FOE track verbatim is clean
        # (the copper is there) and add_trace dedupe skips the write; the
        # report must not claim an item was written.
        original = board.read_bytes()
        duplicate = _seg("FOE", 115, 100.6, 115, 119.4, width=0.2)
        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(board.parent, duplicate)),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 0 and doc["ok"] is True, doc["problems"]
        assert doc["items_written"] == 0
        assert doc["items_skipped_as_duplicate"] == 1
        # No node was added: still exactly the one segment the fixture has.
        reloaded = PCB.load(str(board))
        assert len(reloaded.segments) == 1
        assert (board.parent / "b.kicad_pcb.bak").read_bytes() == original

    def test_apply_mixed_batch_splits_written_and_skipped(self, board, capsys):
        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(
                _items_file(
                    board.parent,
                    _seg("FOE", 115, 100.6, 115, 119.4, width=0.2),  # exact duplicate
                    _seg("SIG", 105, 105, 112, 105),  # new copper
                )
            ),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 0 and doc["ok"] is True, doc["problems"]
        assert doc["items_written"] == 1
        assert doc["items_skipped_as_duplicate"] == 1
        reloaded = PCB.load(str(board))
        assert len(reloaded.segments) == 2

    def test_apply_text_output_distinguishes_skipped_duplicates(self, board, capsys):
        rc = main(
            [
                "apply",
                str(board),
                str(_items_file(board.parent, _seg("FOE", 115, 100.6, 115, 119.4, width=0.2))),
                "--checked",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert "wrote 0 item(s)" in captured.out
        assert "skipped 1 item(s) already on the board" in captured.out

    def test_apply_via_size_mismatch_is_conflicting_not_duplicate(self, via_board, capsys):
        # Issue #9674 review: a via's dedupe key is net, position and layers
        # only -- size and drill are outside it -- so a 1.2/0.6 request at a
        # 0.6/0.3 via's position is a position collision, not a duplicate.
        # The report must not tell the agent the requested copper is there.
        rc, doc = _run(
            capsys,
            "apply",
            str(via_board),
            str(
                _items_file(
                    via_board.parent,
                    {"type": "via", "net": "FOE", "at": [120, 110], "size": 1.2, "drill": 0.6},
                )
            ),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 0 and doc["ok"] is True, doc["problems"]
        assert doc["items_written"] == 0
        assert doc["items_skipped_as_duplicate"] == 0
        assert doc["items_skipped_conflicting"] == 1
        (detail,) = doc["conflicting_via_details"]
        assert "0.6" in detail and "1.2" in detail
        # The board keeps the original via: nothing was up-sized.
        reloaded = PCB.load(str(via_board))
        (via,) = reloaded.vias
        assert via.size == pytest.approx(0.6) and via.drill == pytest.approx(0.3)

    def test_apply_exact_duplicate_via_is_skipped_as_duplicate(self, via_board, capsys):
        # The matching half of the split: an exact 0.6/0.3 re-proposal is a
        # true duplicate and stays in items_skipped_as_duplicate.
        rc, doc = _run(
            capsys,
            "apply",
            str(via_board),
            str(
                _items_file(
                    via_board.parent,
                    {"type": "via", "net": "FOE", "at": [120, 110], "size": 0.6, "drill": 0.3},
                )
            ),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 0 and doc["ok"] is True, doc["problems"]
        assert doc["items_written"] == 0
        assert doc["items_skipped_as_duplicate"] == 1
        assert doc["items_skipped_conflicting"] == 0
        assert doc["conflicting_via_details"] == []

    def test_apply_text_output_names_existing_via_size(self, via_board, capsys):
        rc = main(
            [
                "apply",
                str(via_board),
                str(
                    _items_file(
                        via_board.parent,
                        {"type": "via", "net": "FOE", "at": [120, 110], "size": 1.2, "drill": 0.6},
                    )
                ),
                "--checked",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert "held by a different via (not written)" in captured.out
        assert "board has size 0.6 / drill 0.3" in captured.out
        assert "batch asked size 1.2 / drill 0.6" in captured.out

    def test_apply_arc_writes_arc_node_and_round_trips(self, board, capsys):
        # AC #9618: pin the deliberate check/write divergence -- the arc is
        # checked as a tessellation but written as a real (arc ...) node.
        # Same clean SIG arc as TestProbe.test_arc_items; the fixture board's
        # origin is (100, 100), so written geometry is board-relative.
        arc = {
            "type": "arc",
            "net": "SIG",
            "layer": "F.Cu",
            "width": 0.25,
            "start": [118, 104],
            "mid": [120, 105.5],
            "end": [122, 104],
        }
        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(board.parent, arc)),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 0 and doc["ok"] is True, doc["problems"]

        text = board.read_text()
        assert "(arc" in text and "(mid 120 105.5)" in text

        reloaded = PCB.load(str(board))
        assert len(reloaded.segments) == 1  # the original FOE straight segment
        (written,) = reloaded.arcs
        assert written.net_name == "SIG" and written.layer == "F.Cu"
        assert written.width == pytest.approx(0.25)
        assert written.start == pytest.approx((18.0, 4.0))
        assert written.mid == pytest.approx((20.0, 5.5))
        assert written.end == pytest.approx((22.0, 4.0))

    def test_apply_failure_leaves_file_byte_identical(self, board, capsys):
        before = _md5(board)
        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(board.parent, _seg("SIG", 115, 105, 115, 112))),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 1 and doc["written"] is None
        assert _md5(board) == before
        assert not (board.parent / "b.kicad_pcb.bak").exists()

    def test_apply_refuses_concurrent_modification(self, board, monkeypatch, capsys):
        racing_bytes = b"(kicad_pcb (version 20240108))\n"

        real_save = PCB.save

        def racing_save(self, path=None):
            # Simulate another writer landing bytes between our read and write.
            board.write_bytes(racing_bytes)
            return real_save(self, path)

        monkeypatch.setattr(PCB, "save", racing_save)
        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(board.parent, _seg("SIG", 105, 105, 112, 105))),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 2
        assert doc["written"] is None and doc["refused"]
        assert "changed on disk" in doc["refused"]
        assert board.read_bytes() == racing_bytes  # the racing writer's bytes survive
        assert not (board.parent / "b.kicad_pcb.bak").exists()

    def test_apply_custom_backup_path(self, board, tmp_path, capsys):
        backup = tmp_path / "custom.bak"
        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(board.parent, _seg("SIG", 105, 105, 112, 105))),
            "--checked",
            "--backup",
            str(backup),
            "--format",
            "json",
        )
        assert rc == 0 and backup.exists()

    def test_apply_requires_checked_flag(self, board, capsys):
        with pytest.raises(SystemExit):
            main(["apply", str(board), str(_items_file(board.parent, _seg("SIG", 1, 1, 2, 2)))])


class TestMcpTools:
    def test_registered_and_callable(self, board, tmp_path):
        from kicad_tools.mcp.tools.registry import TOOL_REGISTRY, get_tool

        assert get_tool("probe_items") is not None
        assert get_tool("apply_items_checked") is not None
        items = [_seg("SIG", 105, 105, 112, 105)]

        probe = TOOL_REGISTRY["probe_items"].handler({"pcb_path": str(board), "items": items})
        assert probe["success"] is True and probe["error_count"] == 0

        applied = TOOL_REGISTRY["apply_items_checked"].handler(
            {"pcb_path": str(board), "items": items, "backup_path": str(tmp_path / "mb.bak")}
        )
        assert applied["success"] is True and applied["written"] == str(board)

        bad = TOOL_REGISTRY["apply_items_checked"].handler(
            {"pcb_path": str(board), "items": [_seg("SIG", 115, 105, 115, 112)]}
        )
        assert bad["success"] is False and bad["written"] is None


@pytest.fixture(scope="class")
def committed(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("committed") / "simple_led.kicad_pcb"
    path.write_bytes(COMMITTED.read_bytes())
    return path


class TestCheckAgreement:
    """AC: probe agrees with ``kct check`` on the committed board (Issue #9590)."""

    #: The reference is the *default* ``kct check`` category set (no
    #: ``--only``): restricting it to the probe's own list is how the
    #: ``physical_copper_gap`` omission shipped unnoticed (Issue #9590
    #: review).  Findings are filtered to the copper-gap/short rule ids so
    #: categories outside the probe's copper scope (silkscreen, netlist,
    #: placement, ...) keep their pre-existing counts and never gate the
    #: before/after comparison.
    COPPER_RULE_PREFIXES = ("clearance", "physical_copper_gap")

    @staticmethod
    def _sheet_pad(pcb: PCB, ref: str, pad: str) -> tuple[float, float]:
        pos = pcb.get_pad_position(ref, pad)
        ox, oy = pcb.board_origin
        return (pos[0] + ox, pos[1] + oy)

    def _check_errors(self, capsys, board: Path) -> int:
        rc, doc = _run(capsys, "check", str(board), "--format", "json")
        assert rc in (0, 1, 2), doc
        return sum(
            1
            for v in doc["violations"]
            if v["severity"] == "error" and v["rule_id"].startswith(self.COPPER_RULE_PREFIXES)
        )

    def test_good_and_bad_items_agree_with_kct_check(self, committed, tmp_path, capsys):
        pcb = PCB.load(str(committed))
        j1 = self._sheet_pad(pcb, "J1", "1")  # VCC pin-header pad
        good = {
            "type": "segment",
            "net": "VCC",
            "layer": "F.Cu",
            "width": 0.25,
            "start": list(j1),
            "end": [j1[0] + 5.0, j1[1]],
        }
        # GND copper straight over the VCC pad: a hard short.
        bad = dict(good, net="GND")

        errors_before = self._check_errors(capsys, committed)

        # Good batch: probe clean, apply writes, kct check gains no errors.
        rc, doc = _run(
            capsys, "probe", str(committed), str(_items_file(tmp_path, good)), "--format", "json"
        )
        assert rc == 0 and doc["ok"], doc["problems"]
        rc, doc = _run(
            capsys,
            "apply",
            str(committed),
            str(_items_file(tmp_path, good)),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 0
        assert self._check_errors(capsys, committed) == errors_before

        # Bad batch: probe fails, and the same copper force-applied makes
        # kct check fail with the same clearance decision.
        rc, doc = _run(
            capsys, "probe", str(committed), str(_items_file(tmp_path, bad)), "--format", "json"
        )
        assert rc == 1
        assert any(p["message"].startswith("SHORT") for p in doc["problems"])

        with_bad = tmp_path / "with_bad.kicad_pcb"
        with_bad.write_bytes((committed.parent / "simple_led.kicad_pcb.bak").read_bytes())
        merged = PCB.load(str(with_bad))
        _append_for_write(merged, load_items_file(_items_file(tmp_path, bad)))
        merged.save(str(with_bad))
        errors_after_bad = self._check_errors(capsys, with_bad)
        assert errors_after_bad > errors_before
