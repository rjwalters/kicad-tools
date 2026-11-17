"""Local net evidence compares findings and pads in one frame (issue #6055).

``kct check`` reports finding locations in sheet (file) coordinates, but
``PCB.load()`` stores footprint positions relative to the board origin (the
Edge.Cuts minimum corner).  Before the fix the ev2 nearby-pad sets were
computed from the board-relative positions, so on any board whose outline does
not start at (0, 0) they came out empty: adding or moving a pad right next to
a waived finding left its hash -- and the waiver -- untouched.

Every board here has a non-zero origin; the expectations are derived from the
raw file text (sheet coordinates), independently of the evidence code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCViolation
from kicad_tools.validate.evidence import (
    EvidenceContext,
    compute_evidence_hash,
    evidence_payload,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests/fixtures/projects/multilayer_zones.kicad_pcb"

# (board, footprint ref, pad number, pad net) -- an unrotated footprint whose
# pad anchors the synthetic finding.
CASES = [
    pytest.param(FIXTURE, "C1", "2", "GND", id="fixture-multilayer_zones"),
    pytest.param(
        REPO / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb",
        "C3",
        "2",
        "GND",
        id="fleet-03-usb_joystick",
    ),
]

_AT = re.compile(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)(?:\s+(-?[\d.]+))?\)")


def _footprint_span(text: str, ref: str) -> tuple[int, int]:
    """Start of ``ref``'s ``(footprint`` node and of its ``(at ...)`` child."""
    ref_at = text.index(f'"Reference" "{ref}"')
    start = text.rindex("(footprint", 0, ref_at)
    return start, text.index("(at ", start)


def _sheet_position(text: str, ref: str) -> tuple[float, float]:
    """Footprint position read straight from the file -- sheet coordinates."""
    _start, at = _footprint_span(text, ref)
    m = _AT.match(text, at)
    assert m is not None
    assert m.group(3) in (None, "0"), "test footprint must be unrotated"
    return float(m.group(1)), float(m.group(2))


def _move(text: str, ref: str, dx: float, dy: float) -> str:
    _start, at = _footprint_span(text, ref)
    m = _AT.match(text, at)
    assert m is not None
    x, y = float(m.group(1)) + dx, float(m.group(2)) + dy
    return text[: m.start()] + f"(at {x:g} {y:g})" + text[m.end() :]


def _net_number(text: str, net: str) -> str:
    m = re.search(rf'\(net\s+(\d+)\s+"{re.escape(net)}"\)', text)
    assert m is not None, f"net {net!r} not declared"
    return m.group(1)


def _add_probe(text: str, net: str, at: tuple[float, float]) -> str:
    """Append a one-pad footprint on ``net`` at sheet position ``at``."""
    num = _net_number(text, net)
    probe = (
        '\t(footprint "Test:Probe"\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(uuid "probe-6055-uuid")\n'
        f"\t\t(at {at[0]:g} {at[1]:g})\n"
        '\t\t(property "Reference" "TP6055" (at 0 0) (layer "F.SilkS") (uuid "probe-6055-ref"))\n'
        f'\t\t(pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net {num} "{net}"))\n'
        "\t)\n"
    )
    end = text.rstrip().rindex(")")
    return text[:end] + probe + text[end:]


def _load(text: str, tmp_path: Path, name: str) -> PCB:
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(text)
    return PCB.load(path)


def _setup(board: Path, ref: str, pad_number: str):
    if not board.is_file():
        pytest.skip(f"{board} not present")
    text = board.read_text()
    pcb = PCB.load(board)
    assert pcb.board_origin != (0.0, 0.0), "test needs a board with a non-zero origin"
    fp = next(f for f in pcb.footprints if f.reference == ref)
    pad = next(p for p in fp.pads if str(p.number) == pad_number)
    fx, fy = _sheet_position(text, ref)
    return text, pcb, (fx + pad.position[0], fy + pad.position[1])


def _finding(location: tuple[float, float], net: str) -> DRCViolation:
    # Items deliberately do not name the anchoring footprint, so only the
    # local net evidence (not the named-footprint evidence) can see it.
    return DRCViolation(
        rule_id="clearance_pad_segment",
        severity="error",
        message="too close",
        location=location,
        layer="F.Cu",
        actual_value=0.1,
        required_value=0.2,
        items=("Trace-seg-6055",),
        nets=(net,),
    )


def _hash(pcb: PCB, v: DRCViolation) -> str:
    return compute_evidence_hash(v, EvidenceContext(pcb))


@pytest.mark.parametrize(("board", "ref", "pad_number", "net"), CASES)
class TestNonZeroOriginBoards:
    def test_pad_at_the_finding_is_evidence(self, board, ref, pad_number, net):
        _text, pcb, loc = _setup(board, ref, pad_number)
        nets = evidence_payload(_finding(loc, net), EvidenceContext(pcb))["nets"]
        assert [ref + "." + pad_number, round(loc[0], 3), round(loc[1], 3)] in nets[net]

    def test_adding_a_nearby_pad_changes_hash(self, board, ref, pad_number, net, tmp_path):
        text, pcb, loc = _setup(board, ref, pad_number)
        v = _finding(loc, net)
        added = _load(_add_probe(text, net, (loc[0] + 1.0, loc[1])), tmp_path, "near")
        assert added.board_origin == pcb.board_origin
        assert _hash(added, v) != _hash(pcb, v)

    def test_moving_a_nearby_pad_changes_hash(self, board, ref, pad_number, net, tmp_path):
        text, pcb, loc = _setup(board, ref, pad_number)
        v = _finding(loc, net)
        moved = _load(_move(text, ref, 0.2, 0.0), tmp_path, "moved")
        assert moved.board_origin == pcb.board_origin
        assert _hash(moved, v) != _hash(pcb, v)

    def test_far_away_pad_does_not_change_hash(self, board, ref, pad_number, net, tmp_path):
        text, pcb, loc = _setup(board, ref, pad_number)
        v = _finding(loc, net)
        far = _load(_add_probe(text, net, (loc[0] + 30.0, loc[1] + 30.0)), tmp_path, "far")
        assert far.board_origin == pcb.board_origin
        assert _hash(far, v) == _hash(pcb, v)


def test_issue_repro_net_evidence_is_not_empty():
    """The exact repro from issue #6055."""
    pcb = PCB.load(FIXTURE)
    assert pcb.board_origin == (100.0, 100.0)
    v = DRCViolation(
        rule_id="clearance_pad_segment",
        severity="error",
        message="x",
        location=(115.52, 120.0),
        layer="F.Cu",
        items=("C1-1", "Trace-seg-1"),
        nets=("GND", "+3V3"),
    )
    nets = evidence_payload(v, EvidenceContext(pcb))["nets"]
    assert nets["GND"] == [["C1.2", 115.48, 120.0]]
    assert ["C1.1", 114.52, 120.0] in nets["+3V3"]


def test_kct_check_findings_share_the_pad_frame(capsys):
    """A real ``kct check`` finding sees the pad it is reported at.

    Ties the producer frame (``DRCChecker._absolutize``) to the evidence
    frame: the ``clearance_pad_segment`` finding on pad C2-2 must list C2.2
    in its ``GND`` evidence on this origin-(100, 100) board.
    """
    from kicad_tools.cli import check_cmd

    check_cmd.main([str(FIXTURE), "--format", "json", "--drc-only"])
    report = json.loads(capsys.readouterr().out)
    finding = next(
        f
        for f in report["violations"]
        if f["rule_id"] == "clearance_pad_segment" and "C2-2" in f["items"]
    )
    v = DRCViolation(
        rule_id=finding["rule_id"],
        severity=finding["severity"],
        message=finding["message"],
        location=tuple(finding["location"]),
        layer=finding.get("layer"),
        items=tuple(finding["items"]),
        nets=tuple(finding["nets"]),
    )
    nets = evidence_payload(v, EvidenceContext(PCB.load(FIXTURE)))["nets"]
    assert "C2.2" in {row[0] for row in nets["GND"]}
