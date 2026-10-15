"""Pour welds clear the CORNERS of square BGA balls; one footprint cannot veto a round (#6671).

Board 06rf: every GND / +3V3 weld inside U2 (7x7 BGA, 1.27 mm pitch, 0.45 mm
SQUARE balls) landed 0.118-0.126 mm from a neighbouring ball although the
weld was placed at the ``Power`` class clearance of 0.200 mm.  The stitch
placer cleared foreign pads as discs of radius ``max(w, h) / 2`` -- the
INSCRIBED circle of a square, whose corners sit 0.318 mm from the centre --
so a dog-leg via beside a ball's corner passed.  Then the oracle loop's
2-retry budget ran out on those welds and discarded the ~120 clean closures
elsewhere on the board.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import (
    PadInfo,
    _pad_conflict,
    _pad_copper_gap,
    _same_net_via_spacing,
    calculate_dogleg_via_position,
    find_all_pad_bboxes,
    run_stitch,
)
from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
from kicad_tools.router.oracle_completion import (
    STOP_CONVERGED,
    STOP_NO_CLOSER,
    ClosureAttempt,
    _violation_signature,
    banned_footprints,
    closer_key_refs,
    footprint_key,
    run_oracle_completion,
    withdraw_copper,
)
from kicad_tools.sexp import parse_string

PITCH = 1.27
BALL = 0.45
CLEARANCE = 0.2  # the 06rf ``Power`` class
VIA, DRILL = 0.6, 0.3

# --------------------------------------------------------------------------
# Part 1: the weld placer keeps class clearance from square balls' corners
# --------------------------------------------------------------------------


def _box_gap(x: float, y: float, r: float, box: tuple) -> float:
    """Edge-to-edge gap between a disc and an axis-aligned box."""
    x0, y0, x1, y1 = box[:4]
    dx = max(x0 - x, 0.0, x - x1)
    dy = max(y0 - y, 0.0, y - y1)
    return math.hypot(dx, dy) - r


#: The 06rf neighbourhood of U2.C2 (+3V3), relative to that ball: the 3 x 3
#: balls around it, the USB3 escape copper within 2 mm, as routed (lattice,
#: seed 42).  ``(layer, net, x1, y1, x2, y2)`` tracks are 0.275 mm wide.
_C2_RING = {
    (-1, -1): "GND",
    (0, -1): "USB3_TX1+",
    (1, -1): "USB3_TX1-",
    (-1, 0): "GND",
    (0, 0): "+3V3",
    (1, 0): "+1V2",
    (-1, 1): "GND",
    (0, 1): "+1V2",
    (1, 1): "+1V2",
}
_C2_TRACKS = [
    ("B.Cu", "USB3_RX2-", -2.06, 1.27, -1.26, 1.27),
    ("B.Cu", "USB3_TX1+", -1.66, -1.93, -0.46, -0.73),
    ("F.Cu", "USB3_TX1+", -0.46, -0.73, -0.06, -1.13),
    ("B.Cu", "USB3_RX2-", -0.86, 1.67, -0.06, 1.67),
    ("B.Cu", "USB3_RX2-", -0.06, 1.67, 0.34, 2.07),
    ("B.Cu", "USB3_RX2-", -1.26, 1.27, -0.86, 1.67),
    ("F.Cu", "USB3_TX1+", -0.06, -1.21, 0.0, -1.27),
    ("F.Cu", "USB3_TX1+", -0.06, -1.13, -0.06, -1.21),
    ("F.Cu", "USB3_TX1-", 1.53, -1.53, 1.27, -1.27),
]
_C2_VIA = ("USB3_TX1+", -0.46, -0.73)  # 0.6 / 0.3 mm
_CX, _CY = 30.0, 30.0


def _bga_board(tmp_path: Path, shape: str = "rect") -> Path:
    """4-layer board holding the 06rf U2.C2 neighbourhood (``shape`` balls)."""
    nets = [""] + sorted(set(_C2_RING.values()) | {t[1] for t in _C2_TRACKS})
    num = {name: i for i, name in enumerate(nets)}
    pads = "".join(
        f'    (pad "{"BCD"[dy + 1]}{dx + 2}" smd {shape} (at {dx * PITCH:g} {dy * PITCH:g}) '
        f'(size {BALL} {BALL}) (layers "F.Cu") (net {num[net]} "{net}"))\n'
        for (dx, dy), net in sorted(_C2_RING.items())
    )
    tracks = "".join(
        f"  (segment (start {_CX + x1:g} {_CY + y1:g}) (end {_CX + x2:g} {_CY + y2:g}) "
        f'(width 0.275) (layer "{layer}") (net {num[net]}))\n'
        for layer, net, x1, y1, x2, y2 in _C2_TRACKS
    )
    vnet, vx, vy = _C2_VIA
    via = (
        f"  (via (at {_CX + vx:g} {_CY + vy:g}) (size 0.6) (drill 0.3) "
        f'(layers "F.Cu" "B.Cu") (net {num[vnet]}))\n'
    )
    zone = (
        f'  (zone (net {num["+3V3"]}) (net_name "+3V3") (layer "In2.Cu")\n'
        "    (polygon (pts (xy 0 0) (xy 60 0) (xy 60 60) (xy 0 60))))\n"
    )
    text = (
        '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
        '  (layers (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power) '
        '(31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        + "".join(f'  (net {i} "{n}")\n' for i, n in enumerate(nets))
        + '  (footprint "T:BGA" (layer "F.Cu")\n'
        f"    (at {_CX:g} {_CY:g})\n"
        '    (property "Reference" "U2" (at 0 0) (layer "F.SilkS"))\n'
        + pads
        + "  )\n"
        + tracks
        + via
        + zone
        + '  (gr_rect (start 0 0) (end 60 60) (layer "Edge.Cuts") (width 0.1))\n)\n'
    )
    path = tmp_path / "bga.kicad_pcb"
    path.write_text(text)
    return path


def _weld(path: Path):
    return run_stitch(
        path,
        ["+3V3"],
        via_size=VIA,
        drill=DRILL,
        clearance=CLEARANCE,
        offset=0.5,
        escape_distance=4.0,
        trace_width=0.25,
        avoid_pad_overlap=True,
        strict_drill_overlap=True,
        only_pads=frozenset({"U2.C2"}),
        force_pads=frozenset({"U2.C2"}),
        dry_run=True,
    )


class TestWeldClearsSquareBallCorners:
    def test_the_06rf_c2_weld_keeps_class_clearance_or_is_declined(self, tmp_path):
        # On the routed 06rf board (inner-layer fills included) the old placer
        # welded U2.C2 by a dog-leg to (+0.725, +0.5): 0.215 mm from C3's
        # inscribed disc, 0.122 mm from its corner (kicad-cli: 0.1182 mm
        # against the 0.200 mm Power rule).  This fill-less copy of the
        # neighbourhood checks the whole run_stitch path stays legal.
        path = _bga_board(tmp_path)
        result = _weld(path)
        boxes = find_all_pad_bboxes(parse_string(path.read_text()))
        for v in result.vias_added:
            for box in boxes:
                if box[4] == v.pad.net_number:
                    continue
                gap = _box_gap(v.via_x, v.via_y, v.size / 2, box)
                assert gap >= CLEARANCE - 1e-6, (
                    f"{v.pad.reference}.{v.pad.pad_number} via at "
                    f"({v.via_x - _CX:.3f}, {v.via_y - _CY:.3f}) is {gap:.4f} mm "
                    "from a foreign ball"
                )
        # Welded cleanly, or declined with a stated reason -- never shipped
        # below clearance.
        assert len(result.vias_added) + len(result.pads_skipped) == 1
        assert all(reason for _, reason in result.pads_skipped)

    def test_dogleg_site_beside_a_ball_corner_is_refused(self):
        # A ball ringed by eight foreign square balls.  The circle model
        # alone accepts a dog-leg 0.151 mm from a neighbour's corner; the
        # shape-aware boxes (#6683's model, which #6671 builds on) refuse it.
        pad = PadInfo("U2", "C2", 2, "+3V3", 0.0, 0.0, "F.Cu", BALL, BALL)
        h = BALL / 2
        ring = [(dx * PITCH, dy * PITCH) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
        circles = [(x, y, h, 3) for x, y in ring]
        boxes = [(x - h, y - h, x + h, y + h, 3, "rect", h, 0.0) for x, y in ring]

        def place(bboxes):
            return calculate_dogleg_via_position(
                pad,
                offset=0.5,
                via_size=VIA,
                existing_vias=[],
                clearance=CLEARANCE,
                other_net_pads=circles,
                trace_width=0.25,
                other_net_pad_bboxes=bboxes,
            )

        def worst(site) -> float:
            return min(_box_gap(site[0], site[1], VIA / 2, b) for b in boxes)

        old = place(None)
        assert old is not None and worst(old) < CLEARANCE  # the #6671 defect
        new = place(boxes)
        assert new is not None and worst(new) >= CLEARANCE - 1e-9

    def test_square_and_round_balls_carry_no_corner_radius(self, tmp_path):
        for shape in ("rect", "circle"):
            boxes = find_all_pad_bboxes(parse_string(_bga_board(tmp_path, shape).read_text()))
            assert len(boxes) == 9
            assert all(b[5] == shape and b[7] == 0.0 for b in boxes)


def _roundrect_board(fp_angle: float = 0.0, pad_extra: str = "", pad_angle: str = "") -> str:
    """One 0603-style 0.9 x 0.95 roundrect pad, rratio 0.25 (board 03 C5)."""
    at = f"0 0 {pad_angle}" if pad_angle else "0 0"
    return (
        '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        '  (net 0 "") (net 1 "GND")\n'
        f'  (footprint "T:C" (layer "F.Cu") (at 10 10 {fp_angle:g})\n'
        f'    (pad "2" smd roundrect (at {at}) (size 0.9 0.95) (layers "F.Cu") '
        f'(roundrect_rratio 0.25){pad_extra} (net 1 "GND"))\n'
        "  )\n)\n"
    )


class TestRoundrectCornerRadius:
    """#6699's box model refuses diagonal sites a roundrect's corner leaves free."""

    def test_find_all_pad_bboxes_records_the_corner_radius(self):
        (box,) = find_all_pad_bboxes(parse_string(_roundrect_board()))
        assert box[5] == "roundrect"
        assert box[7] == pytest.approx(0.225)  # 0.25 * min(0.9, 0.95)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"fp_angle": 45.0},
            {"pad_angle": "30"},
            {"pad_extra": " (chamfer_ratio 0.2) (chamfer top_left)"},
        ],
    )
    def test_radius_dropped_where_the_box_is_not_exact(self, kwargs):
        (box,) = find_all_pad_bboxes(parse_string(_roundrect_board(**kwargs)))
        assert box[7] == 0.0

    def test_pad_local_quarter_turn_keeps_a_containing_radius(self):
        # The box is the union of both orientations; rounding it by the same
        # radius still contains the true pad.
        (box,) = find_all_pad_bboxes(parse_string(_roundrect_board(pad_angle="90")))
        assert box[2] - box[0] == pytest.approx(0.95)
        assert box[3] - box[1] == pytest.approx(0.95)
        assert box[7] == pytest.approx(0.225)

    def test_diagonal_site_beside_the_corner_is_accepted(self):
        box = (0.0, 0.0, 0.9, 0.95, 7, "roundrect", 0.475, 0.225)
        plain = (*box[:7], 0.0)
        # 0.1 mm diagonally off the box corner = 0.1 + 0.093 from the copper.
        d = 0.1 / math.sqrt(2)
        x, y = 0.9 + d, 0.95 + d
        expected = math.hypot(x - 0.675, y - 0.725) - 0.225
        assert _pad_copper_gap(x, y, x, y, box) == pytest.approx(expected)
        assert expected == pytest.approx(0.1 + 0.225 * (math.sqrt(2) - 1), abs=1e-9)
        assert not _pad_conflict(x, y, x, y, 0.19, [], [box])
        assert _pad_conflict(x, y, x, y, 0.19, [], [plain])

    def test_straight_edges_and_segments_are_unchanged(self):
        box = (0.0, 0.0, 0.9, 0.95, 7, "roundrect", 0.475, 0.225)
        # Along a straight edge the radius changes nothing.
        assert _pad_copper_gap(1.0, 0.5, 1.0, 0.5, box) == pytest.approx(0.1)
        # A segment running past the long side.
        assert _pad_copper_gap(-1.0, 1.05, 2.0, 1.05, box) == pytest.approx(0.1)
        # Overlap is 0.
        assert _pad_copper_gap(0.45, 0.45, 0.45, 0.45, box) == 0.0

    def test_rounded_gap_never_exceeds_the_true_roundrect_gap(self):
        # Sample points around the pad: the model's gap is the exact gap to
        # the rounded rectangle, so it never under-checks.
        box = (0.0, 0.0, 0.9, 0.95, 7, "roundrect", 0.475, 0.225)
        r = 0.225
        for i in range(72):
            a = math.radians(i * 5)
            x, y = 0.45 + 0.9 * math.cos(a), 0.475 + 0.9 * math.sin(a)
            cx = min(max(x, r), 0.9 - r)
            cy = min(max(y, r), 0.95 - r)
            true_gap = max(0.0, math.hypot(x - cx, y - cy) - r)
            assert _pad_copper_gap(x, y, x, y, box) == pytest.approx(true_gap, abs=1e-9)
            assert _pad_copper_gap(x, y, x, y, box) >= _pad_copper_gap(x, y, x, y, (*box[:7], 0.0))


# --------------------------------------------------------------------------
# Part 2: one footprint's regressing welds no longer veto the whole round
# --------------------------------------------------------------------------


def _err(at: tuple[float, float]) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.CLEARANCE,
        type_str="clearance",
        severity=Severity.ERROR,
        message="Clearance violation (rule 'Reviewed clearance - Power' clearance 0.2000 mm)",
        locations=[Location(*at)],
        items=["Via [GND] on F.Cu - B.Cu"],
    )


def _links(n: int) -> list[DRCViolation]:
    return [
        DRCViolation(
            type=ViolationType.UNCONNECTED_ITEMS,
            type_str="unconnected_items",
            severity=Severity.ERROR,
            message="Missing connection between items",
            locations=[Location(10.0 + i, 5.0), Location(20.0, 5.0)],
            items=[f"Pad {i + 1} [GND] of {ref} on F.Cu", "Zone [GND] on In1.Cu"],
        )
        for i, ref in enumerate(["U1", "U3", "U4", "J1", "U2", "U2"][:n])
    ]


class SixLinkBoard:
    """4 clean links (U1, U3, U4, J1) and 2 on a BGA (U2) whose welds always regress.

    The board file holds which links are closed and how many U2 welds are on
    it; every U2 weld the closer places surfaces a NEW clearance error at a
    fresh spot (the 06rf +10 / +1 / +1 sequence), so attribution never
    converges within the retry budget.
    """

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "b.kicad_pcb"
        self.path.write_text("")

    def state(self) -> list[str]:
        return [t for t in self.path.read_text().split(",") if t]

    def oracle(self, _path: Path) -> GeometricDRCResult:
        closed = self.state()
        links = [v for v in _links(6) if v.items[0].split(" of ")[1].split()[0] not in closed]
        u2 = [t for t in closed if t.startswith("U2#")]
        return GeometricDRCResult(
            ran=True,
            error_count=len(u2),
            error_violations=[_err((50.0 + i, 50.0)) for i in range(len(u2))],
            unconnected_items=links,
        )


def _bga_closer(board: SixLinkBoard):
    calls = {"n": 0, "banned": []}

    def closer(path, links, banned):
        calls["n"] += 1
        calls["banned"].append(set(banned))
        closed: list[str] = []
        att = ClosureAttempt()
        for ref in ("U1", "U3", "U4", "J1"):
            key = f"pad:GND:{ref}.1"
            if key not in banned:
                closed.append(ref)
                att.footprints[key] = [(0.0, 0.0, 0.0, 0.0, 0.3)]
                att.applied += 1
        if "U2" not in banned_footprints(banned):
            # Each attempt welds a fresh U2 ball next to a fresh error spot.
            n = calls["n"]
            key = f"pad:GND:U2.A{n}"
            closed += ["U2", f"U2#{n}"]
            att.footprints[key] = [(50.0 + 0, 50.0, 50.0 + 0, 50.0, 0.3)]
            att.applied += 1
        path.write_text(",".join(closed))
        return att

    closer.calls = calls  # type: ignore[attr-defined]
    return closer


class TestFootprintWithdrawal:
    def test_clean_closures_elsewhere_are_kept(self, tmp_path):
        b = SixLinkBoard(tmp_path)
        closer = _bga_closer(b)
        lines: list[str] = []
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=closer, nets={"GND"}, max_retries=2, log=lines.append
        )
        first = res.rounds[0]
        # Before #6671: 6 -> 6 (drc_regressed), the 4 clean closures discarded.
        assert first.kept and (first.links_before, first.links_after) == (6, 2)
        assert first.errors_after == 0
        assert footprint_key("U2") in first.banned
        assert set(b.state()) == {"U1", "U3", "U4", "J1"}
        # Later rounds may retry the footprint; here its welds still regress
        # every time, so they end with nothing placed and the board as kept.
        assert res.stop_reason == STOP_NO_CLOSER
        assert all(not r.kept for r in res.rounds[1:])
        assert set(b.state()) == {"U1", "U3", "U4", "J1"}
        # Round 1: 1 + 2 retries plus the footprint-withdrawal attempt.
        assert closer.calls["n"] >= 4
        assert all("U2" in banned_footprints(bn) for bn in closer.calls["banned"][3:4])
        assert any("withdrawing every closure on U2" in line for line in lines)

    def test_a_clean_board_never_escalates(self, tmp_path):
        b = SixLinkBoard(tmp_path)

        def closer(path, links, banned):
            path.write_text("U1,U3,U4,J1,U2")
            return ClosureAttempt(applied=5, footprints={"pad:GND:U1.1": []})

        res = run_oracle_completion(b.path, oracle=b.oracle, closer=closer, nets={"GND"})
        assert res.stop_reason == STOP_CONVERGED
        assert not any(e.startswith("fp:") for e in res.rounds[0].banned)

    def test_closer_key_refs(self):
        assert closer_key_refs("pad:GND:U2.A1") == {"U2"}
        assert closer_key_refs("pad:+3V3:U2.C6") == {"U2"}
        assert closer_key_refs("route:GND:Pad E2 [GND] of U2|Pad 3 [GND] of C7#plane") == {
            "U2",
            "C7",
        }
        assert closer_key_refs("site:pad:GND:U2.A1@1.0000,2.0000") == set()


def test_skew_signature_ignores_the_anchor():
    """kicad-cli re-anchors a net-level skew error between runs (06rf, #6671)."""

    def skew(at: tuple[float, float], item: str) -> DRCViolation:
        return DRCViolation(
            type=ViolationType.UNKNOWN,
            type_str="skew_out_of_range",
            severity=Severity.ERROR,
            message=(
                "Skew between tracks out of range (rule 'kct diff pair skew - "
                "USB2_D+/USB2_D-' max skew 1.2700 mm; actual -4.8743 mm)"
            ),
            locations=[Location(*at)],
            items=[item],
            nets=["USB2_D+"],
        )

    a = skew((113.1, 54.9), "Via [USB2_D+] on F.Cu - B.Cu")
    b = skew((120.0, 60.0), "Track [USB2_D+] on B.Cu, length 0.4000 mm")
    assert _violation_signature(a) == _violation_signature(b)
    assert _violation_signature(_err((1.0, 1.0))) != _violation_signature(_err((2.0, 1.0)))


# --------------------------------------------------------------------------
# Part 2b: a regression withdraws just its culprits from the attempted board
# --------------------------------------------------------------------------

_HEAD = (
    '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
    '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
    '  (net 0 "")\n  (net 1 "GND")\n'
)
_OLD_VIA = '  (via (at 5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'


def _via(x: float, y: float) -> str:
    return f'  (via (at {x:g} {y:g}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'


def _seg(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f"  (segment (start {x1:g} {y1:g}) (end {x2:g} {y2:g}) (width 0.25) "
        '(layer "F.Cu") (net 1))\n'
    )


class TestWithdrawCopper:
    def test_removes_only_the_named_new_copper(self, tmp_path):
        before = (_HEAD + _OLD_VIA + ")\n").encode()
        path = tmp_path / "b.kicad_pcb"
        # The attempt added two welds; the old via at (5, 5) predates it.
        path.write_text(
            _HEAD + _OLD_VIA + _via(10, 10) + _seg(9, 10, 10, 10) + _via(20, 20) + ")\n"
        )
        # The writer rounds to 0.01 mm: a weld planned at 10.004 is found.
        caps = [(10.004, 10.0, 10.004, 10.0, 0.3), (10.0, 10.0, 9.0, 10.0, 0.125)]
        # (5, 5) is named too, but it was on the board before the attempt.
        caps.append((5.0, 5.0, 5.0, 5.0, 0.3))
        assert withdraw_copper(path, before, caps) == 2
        text = path.read_text()
        assert "(at 10 10)" not in text and "(start 9 10)" not in text
        assert "(at 20 20)" in text and "(at 5 5)" in text

    def test_nothing_matched_leaves_the_file_alone(self, tmp_path):
        path = tmp_path / "b.kicad_pcb"
        path.write_text(_HEAD + _via(20, 20) + ")\n")
        mtime = path.stat().st_mtime_ns
        assert withdraw_copper(path, (_HEAD + ")\n").encode(), [(1, 1, 1, 1, 0.3)]) == 0
        assert path.stat().st_mtime_ns == mtime


class WeldBoard:
    """Board text = the list of welded refs; an oracle that hates ``U2``.

    A weld on ``U2`` raises one clearance error at its site; every other
    weld closes its link cleanly.
    """

    REFS = ("U1", "U3", "U4", "J1", "U2")

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "b.kicad_pcb"
        self.path.write_text("")

    def welded(self) -> list[str]:
        return [t for t in self.path.read_text().split(",") if t]

    def oracle(self, _path: Path) -> GeometricDRCResult:
        welded = self.welded()
        links = [v for v, ref in zip(_links(5), self.REFS, strict=True) if ref not in welded]
        bad = [_err((50.0, 50.0))] if "U2" in welded else []
        return GeometricDRCResult(
            ran=True, error_count=len(bad), error_violations=bad, unconnected_items=links
        )


class WithdrawingCloser:
    """A closer with ``withdraw``: retries never re-run the closer."""

    def __init__(self, board: WeldBoard):
        self.board = board
        self.calls = 0
        self.withdrawn: list[list[str]] = []

    def __call__(self, path, links, banned):
        self.calls += 1
        att = ClosureAttempt()
        for ref in WeldBoard.REFS:
            key = f"pad:GND:{ref}.1"
            if key in banned:
                continue
            at = (50.0, 50.0) if ref == "U2" else (float(len(att.footprints)), 0.0)
            att.footprints[key] = [(*at, *at, 0.3)]
            att.sites[key] = [at]
            att.applied += 1
        path.write_text(",".join(k.split(":")[2].split(".")[0] for k in att.footprints))
        return att

    def withdraw(self, path, before, footprints):
        refs = [k.split(":")[2].split(".")[0] for k in footprints]
        self.withdrawn.append(sorted(refs))
        path.write_text(",".join(r for r in self.board.welded() if r not in refs))
        return len(refs)


class TestRegressionWithdrawsInPlace:
    def test_culprit_is_withdrawn_and_the_rest_kept_without_rerunning(self, tmp_path):
        b = WeldBoard(tmp_path)
        closer = WithdrawingCloser(b)
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=closer, nets={"GND"}, max_rounds=1
        )
        assert closer.calls == 1  # the retry re-checked the board, no new closer run
        assert closer.withdrawn == [["U2"]]
        first = res.rounds[0]
        assert first.kept and (first.links_before, first.links_after) == (5, 1)
        assert first.errors_after == 0 and first.attempts == 2
        assert set(b.welded()) == {"U1", "U3", "U4", "J1"}

    def test_withdrawn_weld_site_stays_banned_in_later_rounds(self, tmp_path):
        b = WeldBoard(tmp_path)
        closer = WithdrawingCloser(b)
        seen: list[frozenset[str]] = []

        def spy(path, links, banned):
            seen.append(banned)
            return closer(path, links, banned)

        # No ``withdraw`` on ``spy``: the loop restores and re-runs, and the
        # DRC culprit's weld site is remembered into the next round.
        run_oracle_completion(b.path, oracle=b.oracle, closer=spy, nets={"GND"}, max_rounds=2)
        assert len(seen) >= 3
        assert "site:pad:GND:U2.1@50.0000,50.0000" in seen[2]

    def test_a_closer_without_withdraw_reruns_as_before(self, tmp_path):
        b = WeldBoard(tmp_path)
        closer = WithdrawingCloser(b)
        res = run_oracle_completion(
            b.path,
            oracle=b.oracle,
            closer=lambda p, lk, bn: closer(p, lk, bn),
            nets={"GND"},
            max_rounds=1,
        )
        assert closer.calls == 2 and closer.withdrawn == []
        assert res.rounds[0].kept and res.rounds[0].links_after == 1


class TestWriteRoundingGuard:
    """Board writers round to 0.01 mm; drill spacing keeps that slack (#6671)."""

    def test_same_net_spacing_takes_the_drill_floor_and_the_guard(self):
        from kicad_tools.cli.stitch_cmd import COORD_WRITE_GUARD_MM, _same_net_via_spacing

        # 0.6 + 0.15 copper < 0.5 + 0.3 drill floor.
        assert _same_net_via_spacing(0.6, 0.15, 0.3, 0.5) == pytest.approx(
            0.8 + COORD_WRITE_GUARD_MM
        )
        assert _same_net_via_spacing(0.6, 0.4, 0.3, 0.5) == pytest.approx(
            1.0 + COORD_WRITE_GUARD_MM
        )

    def test_a_via_exactly_at_the_floor_is_refused(self):
        from kicad_tools.cli.stitch_cmd import calculate_via_position

        pad = PadInfo("U2", "C7", 1, "GND", 0.0, 0.0, "F.Cu", BALL, BALL)
        first = calculate_via_position(pad, 0.5, VIA, [], 0.2, via_drill=DRILL)
        assert first == pytest.approx((0.725, 0.0))
        # A same-net via 0.800 mm away: on the floor, but the written
        # coordinates may land 0.797 mm apart.
        again = calculate_via_position(pad, 0.5, VIA, [(0.725 + 0.8, 0.0, 1)], 0.2, via_drill=DRILL)
        assert again is not None and again != pytest.approx((0.725, 0.0))

    @staticmethod
    def _closest_written(spacing: float) -> float:
        """Smallest written centre distance of two NEW vias planned ``spacing`` apart.

        Both centres are unrounded when checked and both go through
        :func:`fmt` when written; sweep the pair's 0.01 mm grid phase and
        direction over a quadrant.
        """
        from kicad_tools.sexp.builders import fmt

        worst = math.inf
        for k in range(361):  # 0 .. 90 degrees in 0.25 degree steps
            a = math.radians(k * 0.25)
            ux, uy = math.cos(a), math.sin(a)
            for i in range(21):
                for j in range(21):
                    ax, ay = 10.0 + i * 0.0005, 20.0 + j * 0.0005
                    bx, by = ax + spacing * ux, ay + spacing * uy
                    worst = min(worst, math.hypot(fmt(bx) - fmt(ax), fmt(by) - fmt(ay)))
        return worst

    def test_two_new_vias_never_land_under_the_floor_when_written(self):
        # Judge finding (a): with both vias new, each rounds by up to
        # 0.0071 mm, so a diagonal pair closes by up to ~0.0142 mm.
        from kicad_tools.cli.stitch_cmd import COORD_WRITE_GUARD_MM

        floor = 0.5 + DRILL  # hole-to-hole floor + drill = centre floor
        assert 0.01 * math.sqrt(2) <= COORD_WRITE_GUARD_MM
        planned = _same_net_via_spacing(VIA, 0.15, DRILL, 0.5)
        assert self._closest_written(planned) >= floor - 1e-9
        # The old 0.01 mm guard did not cover it (0.7965 mm written).
        assert self._closest_written(floor + 0.01) < floor - 0.003

    def test_drill_checks_keep_the_full_guard_past_the_dfm_tolerance(self):
        # ``drill_hole_to_hole_clear`` forgives 1e-3; the stitch call sites
        # add it back, so the effective slack still covers two new vias.
        from kicad_tools.cli.stitch_cmd import DRILL_FLOOR_SLACK_MM
        from kicad_tools.router.via_clearance import drill_hole_to_hole_clear

        floor = 0.5 + DRILL
        d = floor + 0.01 * math.sqrt(2) + 1e-5
        dd = d / math.sqrt(2)
        drills = [(0.0, 0.0, DRILL)]
        assert drill_hole_to_hole_clear(dd, dd, DRILL, drills, 0.5 + 0.015)  # old net slack
        assert not drill_hole_to_hole_clear(dd, dd, DRILL, drills, 0.5 + DRILL_FLOOR_SLACK_MM)

    def test_dogleg_refuses_a_site_inside_the_guard_of_another_net_drill(self):
        # Every candidate the dog-leg returns keeps the full guard from a
        # foreign drill, not just the bare floor.
        pad = PadInfo("U2", "C2", 2, "+3V3", 0.0, 0.0, "F.Cu", BALL, BALL)
        drills = [(0.725 + 0.81, 0.0, DRILL, 9), (-(0.725 + 0.81), 0.0, DRILL, 9)]
        site = calculate_dogleg_via_position(
            pad,
            offset=0.5,
            via_size=VIA,
            existing_vias=[],
            clearance=CLEARANCE,
            trace_width=0.25,
            other_net_drills=drills,
            via_drill=DRILL,
        )
        if site is not None:
            for dx, dy, dd, _ in drills:
                edge = math.hypot(site[0] - dx, site[1] - dy) - DRILL / 2 - dd / 2
                assert edge >= 0.5 + 0.0142


class FenceBoard:
    """Board 03's C5: a VCC weld that closes its link but fences GND C5.2 in.

    The board text lists the closures on it.  ``vcc-weld`` closes the VCC
    link and strands the GND pad; ``vcc-route`` closes it harmlessly; the
    GND pad can only be welded while ``vcc-weld`` is absent.
    """

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "b.kicad_pcb"
        self.path.write_text("")

    def on(self) -> set[str]:
        return {t for t in self.path.read_text().split(",") if t}

    def oracle(self, _path: Path) -> GeometricDRCResult:
        on = self.on()
        items = []
        if not on & {"vcc-weld", "vcc-route"}:
            items.append(("Pad 1 [VCC] of C5 on F.Cu", "Zone [VCC] on In2.Cu", (141.7, 94.5)))
        if "vcc-weld" in on or "gnd-weld" not in on:
            items.append(("Pad 2 [GND] of C5 on F.Cu", "Zone [GND] on In1.Cu", (143.3, 94.5)))
        links = [
            DRCViolation(
                type=ViolationType.UNCONNECTED_ITEMS,
                type_str="unconnected_items",
                severity=Severity.ERROR,
                message="Missing connection between items",
                locations=[Location(*at), Location(0.0, 0.0)],
                items=[a, b],
            )
            for a, b, at in items
        ]
        return GeometricDRCResult(ran=True, error_count=0, unconnected_items=links)


class FenceCloser:
    def __init__(self, board: FenceBoard):
        self.board = board

    def __call__(self, path, links, banned):
        on = self.board.on()
        att = ClosureAttempt()
        nets = {lk.net for lk in links}
        if "VCC" in nets:
            key = "pad:VCC:C5.1" if "pad:VCC:C5.1" not in banned else "route:VCC:C5.1"
            on.add("vcc-weld" if key.startswith("pad") else "vcc-route")
            # The weld's dog-leg runs right past C5.2.
            att.footprints[key] = [(141.7, 95.3, 144.2, 95.3, 0.1)]
            att.applied += 1
        if "GND" in nets and "vcc-weld" not in on:
            on.add("gnd-weld")
            att.footprints["pad:GND:C5.2"] = [(143.3, 93.5, 143.3, 93.5, 0.3)]
            att.applied += 1
        path.write_text(",".join(sorted(on)))
        return att

    def withdraw(self, path, before, footprints):
        gone = {"pad:VCC:C5.1": "vcc-weld", "pad:GND:C5.2": "gnd-weld"}
        on = self.board.on() - {gone[k] for k in footprints if k in gone}
        path.write_text(",".join(sorted(on)))
        return len(footprints)

    def stranded_points(self, path, links):
        return [(lk.net, lk.a.x, lk.a.y) for lk in links]


class TestRepairRound:
    def test_a_kept_weld_that_strands_another_net_is_replaced(self, tmp_path):
        b = FenceBoard(tmp_path)
        lines: list[str] = []
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=FenceCloser(b), nets={"GND", "VCC"}, log=lines.append
        )
        assert res.stop_reason == STOP_CONVERGED and res.final_links == 0
        assert b.on() == {"vcc-route", "gnd-weld"}
        assert any("withdrawing pad:VCC:C5.1" in line for line in lines)

    def test_a_repair_that_does_not_pay_off_is_undone(self, tmp_path):
        b = FenceBoard(tmp_path)
        closer = FenceCloser(b)

        def stubborn(path, links, banned):
            # Never finds another way to close VCC.
            att = closer(path, links, banned - {"pad:VCC:C5.1"})
            return att

        stubborn.withdraw = closer.withdraw  # type: ignore[attr-defined]
        stubborn.stranded_points = closer.stranded_points  # type: ignore[attr-defined]
        res = run_oracle_completion(b.path, oracle=b.oracle, closer=stubborn, nets={"GND", "VCC"})
        # Round 1 closed VCC (2 -> 1); the repair re-welds VCC the same way,
        # so it is undone and the board keeps round 1's copper.
        assert res.final_links == 1
        assert b.on() == {"vcc-weld"}

    def test_a_repair_that_raises_drc_against_the_last_kept_board_is_undone(self, tmp_path):
        # Judge finding (b): the round after a repair gates DRC against the
        # POST-WITHDRAWAL board.  Here the withdrawal itself leaves a
        # dangling stub (one DRC error) that the repair round does not
        # raise any further, so only a gate against the last KEPT board
        # (0 errors) refuses it -- even though the links drop 1 -> 0.
        b = FenceBoard(tmp_path)
        closer = FenceCloser(b)
        base_oracle = b.oracle

        def oracle(path):
            geo = base_oracle(path)
            geo.error_count = 1 if "stub" in b.on() else 0
            return geo

        def withdraw(path, before, footprints):
            n = closer.withdraw(path, before, footprints)
            path.write_text(",".join(sorted(b.on() | {"stub"})))
            return n

        def leaky(path, links, banned):
            return closer(path, links, banned)

        leaky.withdraw = withdraw  # type: ignore[attr-defined]
        leaky.stranded_points = closer.stranded_points  # type: ignore[attr-defined]
        lines: list[str] = []
        res = run_oracle_completion(
            b.path, oracle=oracle, closer=leaky, nets={"GND", "VCC"}, log=lines.append
        )
        assert any("repair raised KiCad DRC by 1" in line for line in lines)
        # Round 1's board is back: VCC welded, GND still open, no stub.
        assert b.on() == {"vcc-weld"}
        assert res.final_links == 1
        kept = [r for r in res.rounds if r.kept]
        assert kept and all(r.errors_after == 0 for r in kept[:1])
