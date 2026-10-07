"""Identical copper must give a byte-identical pour fill (Issue #6052).

Root cause: KiCad's zone fill depends on the order it loads tracks in
(file order), ``kicad-cli --save-board`` writes tracks sorted by UUID, and
``kct route`` handed KiCad router-minted segment/via UUIDs that were not a
function of the copper (plus footprint pads/graphics with no UUID at all,
which KiCad fills in at random).  After the first ``--save-board`` the
track order was random, so every later refill of identical copper could
emit slightly different pour polygons.

:mod:`kicad_tools.core.canonical_uuids` makes the routed board's UUIDs --
and so its track order -- a function of its content before KiCad first
loads it.  The fast tests here pin that pass; the ``slow`` one runs the
real ``kct route`` fill path (``run_fill_zones``: refill, thermal
remediation, foreign-pad carve) through kicad-cli on two copies of one
board that differ only in UUIDs and copper order, and requires
byte-identical output.  The full board-03 route check lives in
``tests/test_pour_fill_determinism_5578.py::test_board03_pour_fill_is_reproducible``.
"""

from __future__ import annotations

import random
import re
import shutil
import uuid
from pathlib import Path

import pytest

from kicad_tools.core.canonical_uuids import (
    canonicalize_board_uuids,
    canonicalize_pcb_file_uuids,
    uuids_in_file,
)
from kicad_tools.sexp import parse_string

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "board06_pour_escape.kicad_pcb"

_BOARD = """(kicad_pcb (version 20240108) (generator "test")
  (property "Board" "level")
  (net 0 "") (net 1 "GND") (net 2 "SIG")
  (footprint "R" (layer "F.Cu") (uuid "11111111-1111-4111-8111-111111111111") (at 10 10)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (fp_line (start 0 0) (end 1 0) (layer "F.SilkS"))
    (fp_line (start 0 1) (end 1 1) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND")
      (primitives (gr_poly (pts (xy 0 0) (xy 1 0) (xy 1 1)))))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 2 "SIG"))
  )
  (zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "22222222-2222-4222-8222-222222222222")
    (polygon (pts (xy 0 0) (arc (start 0 0) (mid 1 1) (end 2 0)) (xy 20 20))))
  {copper}
)
"""

_COPPER = [
    '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 2) (uuid "{u}"))',
    '(segment (start 1 0) (end 2 0) (width 0.2) (layer "F.Cu") (net 2) (uuid "{u}"))',
    '(via (at 2 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "{u}"))',
    # A twin of the first segment: identical content, must still get its own UUID.
    '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 2) (uuid "{u}"))',
]


def _board(seed: int) -> str:
    """The same board with fresh random copper UUIDs in a shuffled order."""
    rng = random.Random(seed)
    copper = [
        c.replace("{u}", str(uuid.UUID(int=rng.getrandbits(128), version=4))) for c in _COPPER
    ]
    rng.shuffle(copper)
    return _BOARD.replace("{copper}", "\n  ".join(copper))


def _canonical_text(seed: int, keep=()) -> str:
    doc = parse_string(_board(seed))
    canonicalize_board_uuids(doc, keep=keep)
    return doc.to_string()


def _uuid_of(node) -> str | None:
    child = node.find_child("uuid")
    return child.get_string(0) if child is not None else None


def test_random_copper_uuids_and_order_canonicalize_identically():
    texts = {_canonical_text(seed) for seed in range(5)}
    assert len(texts) == 1


def test_missing_item_uuids_are_filled_deterministically():
    authored = {"11111111-1111-4111-8111-111111111111"}
    doc = parse_string(_board(0))
    canonicalize_board_uuids(doc, keep=authored)
    fp = doc.find("footprint")
    assert fp is not None
    for tag in ("property", "fp_line", "pad"):
        for node in fp.find_children(tag):
            assert _uuid_of(node), f"{tag} still has no uuid"
    assert len({_uuid_of(p) for p in fp.find_children("fp_line")}) == 2
    # Board-level properties, pad custom primitives and outline arcs are not
    # board items: KiCad gives them no UUID, so neither do we.
    board_prop = next(c for c in doc.children if c.name == "property")
    assert _uuid_of(board_prop) is None
    assert doc.find("gr_poly").find_child("uuid") is None
    outline_arc = doc.find("zone").find("polygon").find("arc")
    assert outline_arc.find_child("uuid") is None
    # The authored footprint/zone UUIDs survive.
    assert _uuid_of(fp) == "11111111-1111-4111-8111-111111111111"
    assert _uuid_of(doc.find("zone")) == "22222222-2222-4222-8222-222222222222"


def test_copper_is_unique_and_in_uuid_order():
    doc = parse_string(_board(3))
    canonicalize_board_uuids(doc)
    copper = [c for c in doc.children if c.name in ("segment", "via")]
    ids = [_uuid_of(c) for c in copper]
    assert len(set(ids)) == len(ids) == len(_COPPER)
    assert ids == sorted(ids)


def test_kept_copper_uuids_are_preserved():
    text = _board(1)
    authored = re.findall(r'\(segment[^\n]*\(uuid "([^"]+)"\)', text)[0]
    doc = parse_string(text)
    canonicalize_board_uuids(doc, keep={authored})
    assert authored in {_uuid_of(c) for c in doc.children if c.name == "segment"}


def test_canonicalization_is_idempotent(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    shutil.copy(FIXTURE, pcb)
    canonicalize_pcb_file_uuids(pcb)
    once = pcb.read_bytes()
    assert canonicalize_pcb_file_uuids(pcb) == 0
    assert pcb.read_bytes() == once


def test_route_fill_canonicalizes_before_kicad_sees_the_board(tmp_path, monkeypatch):
    """``_fill_zones_after_route`` must canonicalize before its first kicad-cli call."""
    from kicad_tools.cli import route_cmd, runner

    src = tmp_path / "src.kicad_pcb"
    out = tmp_path / "out.kicad_pcb"
    src.write_text(_board(0))
    out.write_text(_board(7))
    seen: list[str] = []

    def fake_fill(path, *a, **kw):
        seen.append(Path(path).read_text())
        return runner.KiCadCLIResult(success=False, stderr="stub")

    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/bin/true"))
    monkeypatch.setattr(runner, "run_fill_zones", fake_fill)

    class Args:
        pcb = str(src)
        manufacturer = None

    route_cmd._fill_zones_after_route(out, quiet=True, args=Args())
    assert len(seen) == 1
    # The input board's copper UUIDs are kept; the twin and everything else
    # the input did not carry are re-keyed, exactly as for a fresh board.
    expected = parse_string(_board(7))
    canonicalize_board_uuids(expected, keep=uuids_in_file(src))
    assert parse_string(seen[0]).to_string() == expected.to_string()


def _scramble(src: Path, dst: Path, seed: int) -> None:
    """Copy ``src`` with random copper UUIDs, shuffled copper and UUID-less items.

    This is what ``kct route`` used to hand KiCad: router copper keyed by a
    run-dependent RNG, and footprint children KiCad has to invent UUIDs for.
    """
    from kicad_tools.core.sexp_file import load_pcb, save_pcb

    rng = random.Random(seed)
    doc = load_pcb(src)
    slots = [i for i, c in enumerate(doc.children) if c.name in ("segment", "via", "arc")]
    copper = [doc.children[i] for i in slots]
    rng.shuffle(copper)
    for i, node in zip(slots, copper, strict=True):
        doc.children[i] = node
        node.find_child("uuid").set_value(0, str(uuid.UUID(int=rng.getrandbits(128), version=4)))
    for fp in doc.find_all("footprint"):
        for child in fp.children:
            if child.name in ("pad", "fp_line", "fp_rect", "fp_text", "property"):
                own = child.find_child("uuid")
                if own is not None:
                    child.children.remove(own)
    save_pcb(doc, dst)


@pytest.mark.slow
@pytest.mark.timeout(600)
def test_route_fill_is_byte_identical_for_identical_copper(tmp_path):
    """Two boards with the same copper must leave the route fill byte-identical.

    Runs the same steps as ``kct route``'s fill (``_canonicalize_routed_uuids``
    then ``run_fill_zones``) through kicad-cli.
    """
    pytest.importorskip("shapely")
    from kicad_tools.cli.runner import find_kicad_cli, run_fill_zones

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        pytest.skip("kicad-cli not installed -- zone fill is a no-op, nothing to compare")

    # As ``kct route`` does: the input board's own UUIDs are kept.
    keep = uuids_in_file(FIXTURE)
    outputs = []
    for seed in (1, 2):
        pcb = tmp_path / f"run{seed}" / "board.kicad_pcb"
        pcb.parent.mkdir()
        _scramble(FIXTURE, pcb, seed)
        canonicalize_pcb_file_uuids(pcb, keep=keep)
        result = run_fill_zones(pcb, kicad_cli=kicad_cli)
        assert result.success, result.stderr
        # The route's closing pass replaces the UUIDs KiCad gave the
        # footprint fields it added on load (Issue #6052).
        canonicalize_pcb_file_uuids(pcb, keep=keep)
        outputs.append(pcb.read_text())

    assert "(filled_polygon" in outputs[0], "kicad-cli poured nothing -- the comparison is vacuous"
    assert outputs[0] == outputs[1], (
        "the route fill of two boards with identical copper differs; KiCad saw a "
        "different track order or invented UUIDs (Issue #6052)"
    )


def test_kicad_invented_uuids_are_rekeyed_but_authored_and_zone_uuids_kept():
    """A random v4 UUID that is not in the input board was invented by KiCad."""
    authored = "11111111-1111-4111-8111-111111111111"
    doc = parse_string(_board(0))
    canonicalize_board_uuids(doc, keep={authored})
    fp = doc.find("footprint")
    # Simulate KiCad adding a mandatory field with a random UUID on load.
    invented = str(uuid.uuid4())
    fp.append(parse_string(f'(property "Datasheet" "" (at 0 0 0) (uuid "{invented}"))'))
    zone_uuid = _uuid_of(doc.find("zone"))
    canonicalize_board_uuids(doc, keep={authored})
    ids = {_uuid_of(p) for p in fp.find_children("property")}
    assert invented not in ids
    assert all(uuid.UUID(i).version == 5 for i in ids)
    assert _uuid_of(fp) == authored
    assert _uuid_of(doc.find("zone")) == zone_uuid
