"""Issue #9580: copper carried through a route pass keeps its input uuids.

``kct route`` strips every ``(segment ...)`` / ``(via ...)`` block from the
input and re-emits the copper it wants in the output (#2976).  Re-emitted
preserved copper used to get a freshly minted uuid per item, so after a
``--complete`` pass nothing but geometry distinguished router-added copper
from copper that was already on the board -- comparing by uuid, the natural
way, silently selected every track on the board.

KiCad itself keeps item uuids stable across saves.  The parser now captures
each item's ``(uuid ...)`` onto the ``Segment`` / ``Via`` and ``to_sexp``
re-writes it; router-created items (no input uuid) still mint fresh ones,
deterministic-uuid mode (#3272) unchanged.

What is pinned here
-------------------

1. End to end: routing one link on a board that already carries copper
   (``--preserve-existing``) leaves every input uuid in the output exactly
   once, and the newly routed copper's uuids are all new (no duplicates).
2. The KiCad-10 name-only path: the name->id rekey that preserved copper
   goes through on such a board rides the uuid along.
3. A ``(via micro ...)`` with an input uuid keeps both the token and the
   uuid (span keywords and identity survive together, #3124 / #9580).
4. Deterministic mode: a uuid-less segment mints from the seeded RNG; a
   segment carrying an input uuid emits that uuid whatever the mode.
"""

from __future__ import annotations

import random
import re
import uuid as uuid_mod
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.pcb import _extract_balanced_blocks, parse_segments, parse_vias
from kicad_tools.router.primitives import (
    Segment,
    Via,
    enable_deterministic_uuids,
    reset_deterministic_uuids,
)

_SEG_UUID = "11111111-1111-4111-8111-111111111111"
_SEG2_UUID = "22222222-2222-4222-8222-222222222222"
_VIA_UUID = "33333333-3333-4333-8333-333333333333"
_INPUT_UUIDS = {_SEG_UUID, _SEG2_UUID, _VIA_UUID}


def _two_lane_board(*, micro_via: bool = False) -> str:
    """A DONE_NET lane pre-routed WITH uuids, plus one open lane to route.

    ``micro_via`` makes the stitch via a ``(via micro ...)`` so the type
    token and the uuid have to survive together.
    """

    def ref(number: int, name: str) -> str:
        return f"(net {number})"

    via_token = " micro" if micro_via else ""
    y_done = 6.0
    y_open = 14.0
    nodes = [
        '(kicad_pcb (version 20240108) (generator "test")',
        "(general (thickness 1.6))",
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
        '(net 0 "")',
        '(net 1 "DONE_NET")',
        '(net 2 "OPEN_NET")',
        "(gr_rect (start 0 0) (end 24 20) (stroke (width 0.1) (type default)) "
        '(fill none) (layer "Edge.Cuts"))',
    ]
    for number, (name, y) in enumerate((("DONE_NET", y_done), ("OPEN_NET", y_open)), 1):
        for side, x in enumerate((5, 19)):
            nodes.append(
                f'(footprint "Test:Pad" (layer "F.Cu") (at {x} {y}) '
                f'(property "Reference" "J{number}{side}" (at 0 -1) (layer "F.SilkS")) '
                f'(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") {ref(number, name)}))'
            )
        if name == "DONE_NET":
            nodes.append(
                f'(segment (start 5 {y}) (end 12 {y}) (width 0.25) (layer "F.Cu") '
                f'{ref(1, name)} (uuid "{_SEG_UUID}"))'
            )
            nodes.append(
                f'(segment (start 12 {y}) (end 19 {y}) (width 0.25) (layer "F.Cu") '
                f'{ref(1, name)} (uuid "{_SEG2_UUID}"))'
            )
            nodes.append(
                f"(via{via_token} (at 12 {y}) (size 0.6) (drill 0.3) "
                f'(layers "F.Cu" "B.Cu") {ref(1, name)} (uuid "{_VIA_UUID}"))'
            )
    return "\n".join([*nodes, ")"])


def _copper_uuids(text: str) -> list[str]:
    """Every uuid on a top-level ``(segment ...)`` / ``(via ...)`` block."""
    found: list[str] = []
    for kind in ("segment", "via"):
        for _start, _end, block in _extract_balanced_blocks(text, kind):
            m = re.search(r'\(uuid\s+"([^"]+)"\)', block)
            found.append(m.group(1) if m else "")
    return found


def _run_route(tmp_path: Path, pcb_text: str, *, extra_argv: list[str] | None = None) -> str:
    in_path = tmp_path / "in.kicad_pcb"
    out_path = tmp_path / "out.kicad_pcb"
    in_path.write_text(pcb_text)
    argv = [
        str(in_path),
        "--output",
        str(out_path),
        "--no-optimize",
        "--force",
        "--quiet",
        *(extra_argv or []),
    ]
    route_main(argv)
    assert out_path.exists(), "route did not produce an output file"
    return out_path.read_text()


class TestPreservedUuidsSurviveARoutePass:
    """AC: routing one link on a board with existing copper keeps input uuids."""

    def test_input_uuids_appear_exactly_once_and_new_copper_is_new(self, tmp_path):
        board = _two_lane_board()
        out_text = _run_route(tmp_path, board, extra_argv=["--preserve-existing"])

        segs = parse_segments(out_text)
        vias = parse_vias(out_text)
        assert segs.get("OPEN_NET"), "the open lane was not routed"
        # The complete lane is still there, and it is the INPUT copper: same
        # two segments, same uuids -- not a fresh re-route.
        done = segs.get("DONE_NET", [])
        assert len(done) == 2, "the complete lane's segment count changed"
        assert {s.uuid for s in done} == {_SEG_UUID, _SEG2_UUID}, (
            "the complete lane's copper is not the preserved input copper"
        )

        out_uuids = _copper_uuids(out_text)
        # Every input uuid survives, exactly once.
        for u in _INPUT_UUIDS:
            assert out_uuids.count(u) == 1, f"input uuid {u} appears {out_uuids.count(u)}x"
        # No uuid is duplicated anywhere in the output copper.
        assert len(out_uuids) == len(set(out_uuids)), "duplicate uuids in output copper"
        # Every uuid on the newly routed lane is fresh.
        new_uuids = {s.uuid for s in segs["OPEN_NET"] if s.uuid} | {
            v.uuid for v in vias.get("OPEN_NET", []) if v.uuid
        }
        assert new_uuids, "router-added copper carried no uuid at all"
        assert not (new_uuids & _INPUT_UUIDS), "router-added copper reused an input uuid"

    def test_name_only_rekey_keeps_the_uuid(self):
        """KiCad-10 dialect: the name->id rekey rebuilds each item with
        ``dataclasses.replace``; the input uuid must ride along."""
        from kicad_tools.router.io import rekey_name_only_copper

        seg = Segment(x1=0, y1=0, x2=1, y2=0, width=0.2, layer=Layer.F_CU, uuid=_SEG_UUID)
        via = Via(
            x=1, y=0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), uuid=_VIA_UUID
        )
        _net, segs, vias = rekey_name_only_copper(0, "DONE_NET", [seg], [via], {"DONE_NET": 7})
        assert _net == 7
        assert segs[0].uuid == _SEG_UUID, "the name-only rekey dropped the input uuid"
        assert vias[0].uuid == _VIA_UUID, "the name-only rekey dropped the input uuid"

    def test_micro_via_keeps_token_and_uuid(self, tmp_path):
        board = _two_lane_board(micro_via=True)
        out_text = _run_route(tmp_path, board, extra_argv=["--preserve-existing"])

        assert "(via micro" in out_text, "the micro token was dropped"
        assert _copper_uuids(out_text).count(_VIA_UUID) == 1


class TestUuidEmissionUnits:
    """Unit level: input uuid wins; router-created items mint fresh ones."""

    def test_segment_rewrites_its_input_uuid(self):
        seg = Segment(x1=0, y1=0, x2=1, y2=0, width=0.2, layer=Layer.F_CU, uuid=_SEG_UUID)
        assert seg.to_sexp() == seg.to_sexp(), "emission is not stable"
        assert _SEG_UUID in seg.to_sexp()

    def test_segment_without_uuid_mints_a_fresh_one(self):
        seg = Segment(x1=0, y1=0, x2=1, y2=0, width=0.2, layer=Layer.F_CU)
        u1 = re.search(r'\(uuid\s+"([^"]+)"\)', seg.to_sexp()).group(1)
        u2 = re.search(r'\(uuid\s+"([^"]+)"\)', seg.to_sexp()).group(1)
        assert u1 != u2, "uuid-less segment reused a uuid across emissions"

    def test_deterministic_mode_tracks_the_seeded_rng_for_uuidless_items(self):
        try:
            seg = Segment(x1=0, y1=0, x2=1, y2=0, width=0.2, layer=Layer.F_CU)
            kept = Segment(x1=0, y1=0, x2=1, y2=0, width=0.2, layer=Layer.F_CU, uuid=_SEG_UUID)
            enable_deterministic_uuids(True)
            # The uuid a uuid-less segment emits must be exactly the next
            # draw from the seeded RNG.
            random.seed(20260101)
            expected = str(uuid_mod.UUID(int=random.getrandbits(128), version=4))
            random.seed(20260101)
            minted = re.search(r'\(uuid\s+"([^"]+)"\)', seg.to_sexp()).group(1)
            assert minted == expected, "deterministic mode no longer tracks the RNG"
            # The kept uuid is emitted verbatim, deterministic mode or not
            # (and consumes no draw).
            assert _SEG_UUID in kept.to_sexp()
            random.seed(20260101)
            again = re.search(r'\(uuid\s+"([^"]+)"\)', seg.to_sexp()).group(1)
            assert again == expected
        finally:
            reset_deterministic_uuids()

    def test_via_rewrites_its_input_uuid(self):
        via = Via(
            x=1,
            y=2,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            uuid=_VIA_UUID,
        )
        assert via.to_sexp() == via.to_sexp()
        assert _VIA_UUID in via.to_sexp()


@pytest.mark.parametrize(
    "block",
    [
        f'(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1) (uuid "{_SEG_UUID}"))',
        f'(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net "GND") '
        f'(uuid "{_SEG_UUID}"))',
        f'(via (at 0 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "{_VIA_UUID}"))',
    ],
)
def test_parse_captures_uuid_in_both_net_dialects(block: str):
    pcb = f'(kicad_pcb (net 1 "GND")\n  {block}\n)'
    if block.startswith("(segment"):
        (segs,) = parse_segments(pcb).values()
        assert segs[0].uuid == _SEG_UUID
    else:
        (vias,) = parse_vias(pcb).values()
        assert vias[0].uuid == _VIA_UUID


def test_parse_still_accepts_blocks_without_uuid():
    pcb = (
        '(kicad_pcb (net 1 "GND")\n'
        '  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1))\n)'
    )
    (segs,) = parse_segments(pcb).values()
    assert segs[0].uuid is None
