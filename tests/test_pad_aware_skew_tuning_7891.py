"""Pad-aware skew tuning: own-copper rule, escape hosts and the tail fold (Issue #7891).

The skew tuner's foreign-copper gate skips every pad of the bump's own net, so
before #7891 nothing stopped a bump from being laid over its own pad.  Such
copper shorts the bump out, and ``kct check`` / kicad-cli straighten a run end
inside pad or via copper, so they count less than the bump adds (#7864: a loop
seated in J1.B11 read +0.0697 mm of its 0.1045 mm).  ``OwnCopperRule`` holds
every bump off its own pads and vias by the board clearance, except at the
pad entry, and with that in place escape stubs may host bumps and the tuner
may build a **tail fold**: the shorter leg runs past its own terminal beside
the partner's uncoupled tail and returns into its pad.

#7542's 0.2 mm pair (P 30 mm, N 28 mm, skew 2.0 mm under a 2.1 mm uncoupled
limit) fits no bump (Issue #7546); the fold tunes it.  The pinned classes run
on CI's KiCad 10.0.6 image (locally ``KCT_KICAD_DOCKER=1``) and skip elsewhere.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

import pytest

from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairType,
    DifferentialSignal,
)
from kicad_tools.router.diffpair_bump_gates import MAX_EXCURSION_GAPS, MIN_JOG_WIDTHS
from kicad_tools.router.diffpair_detection import DetectedPair, DetectionSource
from kicad_tools.router.diffpair_length_tuning import (
    SKEW_BUMP_CHAMFER_WIDTHS,
    SKEW_BUMP_FOOT_CHAMFER_MM,
    tune_diff_pair_skew,
)
from kicad_tools.router.diffpair_own_copper import OwnCopperRule
from kicad_tools.router.diffpair_tail_fold import (
    TailFoldResult,
    fold_length_added,
    reversed_route,
)
from kicad_tools.router.grid import pad_segment_edge_clearance
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.serpentine import SerpentineConfig, SerpentineGenerator
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.diffpair_uncoupled_length import measure_pair

pytestmark = pytest.mark.timeout(300)

W = 0.2
GAP = 0.25
Y_P = 100.0
Y_N = Y_P + W + GAP
TOLERANCE = 0.127
LIMIT = 2.1
#: The board clearance the router would pass (``DesignRules.trace_clearance``).
CLEARANCE = 0.15


def _sig(name: str, net_id: int, polarity: str) -> DifferentialSignal:
    return DifferentialSignal(
        net_name=name, net_id=net_id, base_name="D", polarity=polarity, notation="underscore_pn"
    )


def _pair() -> DetectedPair:
    return DetectedPair(
        pair=DifferentialPair(
            name="D",
            positive=_sig("D_P", 1, "P"),
            negative=_sig("D_N", 2, "N"),
            pair_type=DifferentialPairType.USB3,
        ),
        source=DetectionSource.EXPLICIT,
    )


def _line(net: int, name: str, x1: float, x2: float, y: float) -> Route:
    return Route(net=net, net_name=name, segments=[Segment(x1, y, x2, y, W, Layer.F_CU, net, name)])


def _pad(x: float, y: float, w: float, h: float, net: int, ref: str) -> Pad:
    name = {1: "D_P", 2: "D_N"}[net]
    return Pad(
        x=x, y=y, width=w, height=h, net=net, net_name=name, layer=Layer.F_CU, ref=ref, pin="1"
    )


def _end_pads(w: float = 0.6, h: float = 0.3) -> list[Pad]:
    """Pads at both ends of #7542's pair: P 100 -> 130, N 100 -> 128."""
    return [
        _pad(100.0, Y_P, w, h, 1, "P1"),
        _pad(130.0, Y_P, w, h, 1, "P2"),
        _pad(100.0, Y_N, w, h, 2, "N1"),
        _pad(128.0, Y_N, w, h, 2, "N2"),
    ]


def _tune_7546(pads: list[Pad] | None, limit: float = LIMIT):
    p = _line(1, "D_P", 100.0, 130.0, Y_P)
    n = _line(2, "D_N", 100.0, 128.0, Y_N)
    rule = OwnCopperRule.from_pads(pads, CLEARANCE) if pads is not None else None
    p_out, n_out, result = tune_diff_pair_skew(
        _pair(),
        {1: p, 2: n},
        tolerance_mm=TOLERANCE,
        intra_pair_clearance_mm=GAP,
        max_uncoupled_mm=limit,
        own_copper=rule,
    )
    return p_out, n_out, result


def _turns_deg(route: Route) -> list[float]:
    turns = []
    for a, b in zip(route.segments, route.segments[1:], strict=False):
        assert (a.x2, a.y2) == pytest.approx((b.x1, b.y1), abs=1e-9)
        da = math.atan2(a.y2 - a.y1, a.x2 - a.x1)
        db = math.atan2(b.y2 - b.y1, b.x2 - b.x1)
        turn = abs(math.degrees(db - da)) % 360.0
        turns.append(min(turn, 360.0 - turn))
    return turns


def _fold(result, route: Route) -> TailFoldResult:
    """The tail fold ``route`` carries (its segments are the route's own)."""
    kept = {id(s) for s in route.segments}
    [fold] = [
        r
        for r in result.serpentine_results
        if isinstance(r, TailFoldResult) and all(id(s) in kept for s in r.new_segments)
    ]
    return fold


# ---------------------------------------------------------------------------
# The own-copper rule
# ---------------------------------------------------------------------------


class TestOwnCopperRule:
    """A bump keeps the board clearance to its own pads and vias, except at
    the pad entry (the host-line lead its route already enters the pad on)."""

    HOST = Segment(100.0, 0.0, 110.0, 0.0, W, Layer.F_CU, 2, "D_N")
    PAD = Pad(x=100.0, y=0.0, width=1.0, height=0.6, net=2, net_name="D_N", layer=Layer.F_CU)

    def _bump(self, start: float) -> list[Segment]:
        """A 0.4 mm chamfered bump starting ``start`` mm along the host."""
        sub = Segment(100.0 + start, 0.0, 110.0, 0.0, W, Layer.F_CU, 2, "D_N")
        config = SerpentineConfig(
            amplitude=0.4,
            min_segment_length=0.0,
            side="outer",
            outer_normal_hint=(0.0, 1.0),
            corner_chamfer_widths=SKEW_BUMP_CHAMFER_WIDTHS,
            foot_chamfer_length_mm=SKEW_BUMP_FOOT_CHAMFER_MM,
        )
        serp = SerpentineGenerator(config).generate_trombone(sub, 0.3)
        assert serp.success
        lead = [Segment(100.0, 0.0, 100.0 + start, 0.0, W, Layer.F_CU, 2, "D_N")] if start else []
        return lead + serp.new_segments

    def _rule(self) -> OwnCopperRule:
        return OwnCopperRule.from_pads([self.PAD], CLEARANCE)

    def test_bump_over_its_own_pad_is_rejected(self):
        # The generator leads in by one loop gap (0.8 mm), still inside the
        # pad's 0.5 mm half-length plus clearance.
        why = self._rule().violation(self._bump(0.0), [self.HOST], [], 2)
        assert why is not None and "own pad" in why

    def test_bump_clear_of_its_own_pad_passes_with_the_pad_entry_lead(self):
        # The lead-in starts inside the pad (the pad entry) and is exempt.
        assert self._rule().violation(self._bump(1.0), [self.HOST], [], 2) is None

    def test_a_foreign_pad_is_not_own_copper(self):
        foreign = Pad(x=100.0, y=0.0, width=1.0, height=0.6, net=7, net_name="X", layer=Layer.F_CU)
        rule = OwnCopperRule.from_pads([foreign], CLEARANCE)
        assert rule.violation(self._bump(0.0), [self.HOST], [], 2) is None

    def test_bump_beside_its_own_via_is_rejected(self):
        bump = self._bump(1.0)
        top = max(bump, key=lambda s: s.y1 + s.y2)
        # A via just above the bump's top, 0.1 mm off its copper edge.
        via = Via((top.x1 + top.x2) / 2, top.y1 + 0.5, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=2)
        assert OwnCopperRule({}, 0.05).violation(bump, [self.HOST], [via], 2) is None
        why = OwnCopperRule({}, CLEARANCE).violation(bump, [self.HOST], [via], 2)
        assert why is not None and "own via" in why


class TestTunerNeverLaysABumpOnItsOwnPad:
    """N's only host starts at its own 2.4 mm pad.  Without the rule (main
    before #7891) the committed bump sits on that pad; with it, the bump is
    placed clear of it."""

    @staticmethod
    def _tune(rule: OwnCopperRule | None):
        p = _line(1, "D_P", 100.0, 104.3, Y_P)
        n = _line(2, "D_N", 100.0, 104.0, Y_N)
        return n, *tune_diff_pair_skew(
            _pair(), {1: p, 2: n}, tolerance_mm=0.1, intra_pair_clearance_mm=GAP, own_copper=rule
        )

    PADS = [_pad(100.0, Y_N, 2.4, 0.3, 2, "U1"), _pad(104.0, Y_N, 0.3, 0.3, 2, "U2")]

    def test_without_the_rule_the_bump_touches_its_own_pad(self):
        n, _p, n_out, result = self._tune(None)
        assert result.reason == "tuned"
        why = OwnCopperRule.from_pads(self.PADS, CLEARANCE).violation(
            n_out.segments, n.segments, [], 2
        )
        assert why is not None and "U1.1" in why

    def test_with_the_rule_the_bump_clears_it(self):
        rule = OwnCopperRule.from_pads(self.PADS, CLEARANCE)
        n, _p, n_out, result = self._tune(rule)
        assert result.reason == "tuned" and result.skew_after_mm <= 0.1
        assert rule.violation(n_out.segments, n.segments, [], 2) is None
        assert "own pad U1.1" in result.gate_refusal  # the overlapping first try


# ---------------------------------------------------------------------------
# The tail fold on #7542's 0.2 mm pair (skew 2.0, limit 2.1)
# ---------------------------------------------------------------------------


class TestTailFold:
    def test_padless_pair_is_tuned(self):
        _p, n_out, result = _tune_7546(None)
        assert result.reason == "tuned" and result.skew_after_mm <= TOLERANCE
        fold = _fold(result, n_out)
        assert fold.amplitude == pytest.approx(W, rel=1e-5)  # the lowest fold reads least

    def test_only_room_is_beside_its_own_pad(self):
        """With pads at every end, the fold's top must clear N's end pad by the
        board clearance: it runs at 0.4 mm (pad half-height 0.15 + W/2 + 0.15)."""
        pads = _end_pads()
        _p, n_out, result = _tune_7546(pads)
        assert result.reason == "tuned" and result.skew_after_mm <= TOLERANCE
        fold = _fold(result, n_out)
        assert fold.amplitude == pytest.approx(0.4, abs=1e-6)
        n_end = pads[3]
        off_line = [s for s in n_out.segments if s.y1 != Y_N or s.y2 != Y_N]
        assert (
            min(pad_segment_edge_clearance(n_end, s.x1, s.y1, s.x2, s.y2, W / 2) for s in off_line)
            >= CLEARANCE - 1e-9
        )
        # The route still ends at N's pad centre, entered from the far side.
        last = n_out.segments[-1]
        assert (last.x2, last.y2) == pytest.approx((128.0, Y_N))
        assert last.x1 > 128.0

    def test_every_corner_is_45_degrees_and_gated(self):
        """Operator ruling Q32 (every corner chamfered, feet included); Q16
        jogs; the 2x-gap excursion cap; no via."""
        _p, n_out, result = _tune_7546(_end_pads())
        turns = _turns_deg(n_out)
        assert all(
            t == pytest.approx(0.0, abs=1e-6) or t == pytest.approx(45.0, abs=1e-6) for t in turns
        )
        assert max(abs(y - Y_N) for s in n_out.segments for y in (s.y1, s.y2)) <= (
            MAX_EXCURSION_GAPS * GAP + 1e-9
        )
        tops = [s for s in n_out.segments if s.y1 == s.y2 and s.y1 != Y_N]
        assert len(tops) == 2  # split before the pad (see diffpair_tail_fold)
        assert all(abs(s.x2 - s.x1) >= MIN_JOG_WIDTHS * W - 1e-9 for s in tops)
        assert not n_out.vias

    def test_fold_stays_beside_the_partner_tail(self):
        _p, n_out, _result = _tune_7546(_end_pads())
        assert 128.0 < max(max(s.x1, s.x2) for s in n_out.segments) <= 130.0

    def test_fold_length_model(self):
        _p, n_out, result = _tune_7546(None)
        fold = _fold(result, n_out)
        copper = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in n_out.segments)
        crown, foot = SKEW_BUMP_CHAMFER_WIDTHS * W, SKEW_BUMP_FOOT_CHAMFER_MM
        assert copper - 28.0 == pytest.approx(
            fold_length_added(fold.amplitude, fold.overrun, crown, foot), abs=1e-9
        )

    def test_a_pad_too_tall_to_clear_refuses_the_fold(self):
        """A 0.6 mm-tall end pad needs a 0.55 mm fold, over the 0.495 mm cap."""
        _p, _n, result = _tune_7546(_end_pads(0.6, 0.6))
        assert result.inserts_applied == 0

    def test_the_fold_must_fit_the_limit_in_both_stored_directions(self):
        """kicad-cli anchors the pairing at P's stored start: the padless fold
        reads 0.62 mm one way and 1.53 mm the other, so a 1.5 mm limit
        refuses it and 1.6 mm accepts it."""
        assert _tune_7546(None, limit=1.5)[2].inserts_applied == 0
        assert _tune_7546(None, limit=1.6)[2].reason == "tuned"

    def test_kct_reads_it_under_the_limit_both_ways(self, tmp_path):
        for label, pads in (("padless", None), ("pads", _end_pads())):
            p_out, n_out, _r = _tune_7546(pads)
            for p in (p_out, reversed_route(p_out)):
                assert _kct_uncoupled(_write(tmp_path, label, [p, n_out], pads)) <= LIMIT


# ---------------------------------------------------------------------------
# Writing and measuring boards
# ---------------------------------------------------------------------------

_HEADER = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general
\t\t(thickness 1.6)
\t\t(legacy_teardrops no)
\t)
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(25 "Edge.Cuts" user)
\t)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t)
\t(net 0 "")
\t(net 1 "D_P")
\t(net 2 "D_N")
\t(gr_rect
\t\t(start 90 90)
\t\t(end 140 110)
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type default)
\t\t)
\t\t(fill no)
\t\t(layer "Edge.Cuts")
\t\t(uuid "00000000-0000-4000-8000-000000007891")
\t)
"""


def _footprint(i: int, pad: Pad) -> str:
    return (
        f'(footprint "T:P{i}" (layer "F.Cu") (uuid "00000000-0000-4000-8000-0000000001{i:02d}") '
        f'(at {pad.x} {pad.y})\n\t\t(pad "1" smd rect (at 0 0) (size {pad.width} {pad.height}) '
        f'(layers "F.Cu") (net {pad.net} "{pad.net_name}") '
        f'(uuid "00000000-0000-4000-8000-0000000002{i:02d}")))'
    )


def _write(
    directory: Path, name: str, routes: list[Route], pads: list[Pad] | None, rules: str = ""
) -> Path:
    items = [_footprint(i, pad) for i, pad in enumerate(pads or [])]
    items += [r.to_sexp(name_only=True) for r in routes]
    pcb = directory / f"{name}.kicad_pcb"
    pcb.write_text(_HEADER + "\t" + "\n\t".join(items) + "\n\t(embedded_fonts no)\n)\n")
    (directory / f"{name}.kicad_dru").write_text(
        rules
        or (
            "(version 1)\n"
            '(rule "pair" (condition "A.inDiffPair(\'D_*\')")\n'
            "  (constraint diff_pair_gap (min 0.1mm))\n"
            "  (constraint diff_pair_uncoupled (max 0.01mm)))\n"
        )
    )
    (directory / f"{name}.kicad_pro").write_text(
        json.dumps({"meta": {"filename": f"{name}.kicad_pro", "version": 3}})
    )
    return pcb


def _ids(pcb: PCB) -> dict[str, int]:
    return {n.name: n.number for n in pcb.nets.values() if n.name}


def _kct_uncoupled(path: Path) -> float:
    pcb = PCB.load(str(path))
    ids = _ids(pcb)
    return measure_pair(pcb, ids["D_P"], ids["D_N"], "D_P", "D_N").uncoupled_mm


def _kct_length(path: Path, net: str) -> float:
    from kicad_tools.router.kicad_length import measure_pcb_net

    pcb = PCB.load(str(path))
    return measure_pcb_net(pcb, _ids(pcb)[net])


@pytest.fixture(scope="module")
def pinned_cli(tmp_path_factory):
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

    root = tmp_path_factory.mktemp("pad_aware_7891")
    try:
        with evidence_toolchain([root]) as toolchain:
            yield toolchain.kicad_cli, root
    except KiCadEnvironmentError as exc:
        pytest.skip(f"pinned kicad-cli unavailable: {exc}")


def _kicad_actual(cli: str, pcb: Path, kind: str) -> float:
    report = pcb.with_suffix(".drc.json")
    subprocess.run(
        [cli, "pcb", "drc", "--format", "json", "--severity-all", "-o", str(report), str(pcb)],
        check=True,
        capture_output=True,
    )
    figures = [
        float(re.search(r"actual ([0-9.]+) mm", v["description"]).group(1))
        for v in json.loads(report.read_text()).get("violations", [])
        if v["type"] == kind
    ]
    return max(figures, default=0.0)


def _kicad_length(cli: str, root: Path, name: str, routes, pads, net: str) -> float:
    rules = (
        "(version 1)\n"
        f'(rule "len" (constraint length (max 0.001mm)) (condition "A.NetName == \'{net}\'"))\n'
    )
    pcb = _write(root, f"{name}_len_{net}", routes, pads, rules)
    return _kicad_actual(cli, pcb, "length_out_of_range")


class TestTailFoldAgreesWithPinnedKicadCli:
    """Issue #7546 AC3 / #7891 AC4: the 2.0/2.1 pair, tuned, read by both engines."""

    @pytest.mark.parametrize("with_pads", [False, True])
    def test_skew_and_uncoupled_agree_in_both_directions(self, pinned_cli, with_pads):
        cli, root = pinned_cli
        pads = _end_pads() if with_pads else None
        p_out, n_out, result = _tune_7546(pads)
        assert result.reason == "tuned"
        label = f"fold_{with_pads}"
        for direction, p in (("fwd", p_out), ("rev", reversed_route(p_out))):
            pcb = _write(root, f"{label}_{direction}", [p, n_out], pads)
            kicad = _kicad_actual(cli, pcb, "diff_pair_uncoupled_length_too_long")
            assert _kct_uncoupled(pcb) == pytest.approx(kicad, abs=1e-3)
            assert kicad <= LIMIT
        lengths = {}
        for net in ("D_P", "D_N"):
            pcb = _write(root, f"{label}_kct_{net}", [p_out, n_out], pads)
            kicad = _kicad_length(cli, root, label, [p_out, n_out], pads, net)
            assert _kct_length(pcb, net) == pytest.approx(kicad, abs=1e-3)
            lengths[net] = kicad
        assert abs(lengths["D_P"] - lengths["D_N"]) <= TOLERANCE


class TestOwnPadLengthWithPinnedKicadCli:
    """Why the rule: a bump on its own pad adds less than its copper in both
    engines (the run end is straightened through the pad); a bump the rule
    accepts adds exactly its copper."""

    def test_bump_on_its_own_pad_is_partly_uncounted(self, pinned_cli):
        cli, root = pinned_cli
        pads = TestTunerNeverLaysABumpOnItsOwnPad.PADS + [
            _pad(100.0, Y_P, 0.3, 0.3, 1, "U3"),
            _pad(104.3, Y_P, 0.3, 0.3, 1, "U4"),
        ]
        n, p_out, n_out_on, _r = TestTunerNeverLaysABumpOnItsOwnPad._tune(None)
        _n, _p, n_out_clear, _r2 = TestTunerNeverLaysABumpOnItsOwnPad._tune(
            OwnCopperRule.from_pads(pads, CLEARANCE)
        )

        def copper(route: Route) -> float:
            return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)

        base = _kicad_length(cli, root, "own_base", [p_out, n], pads, "D_N")
        gains = {}
        for label, n_out in (("on", n_out_on), ("clear", n_out_clear)):
            kicad = _kicad_length(cli, root, f"own_{label}", [p_out, n_out], pads, "D_N")
            pcb = _write(root, f"own_{label}_kct", [p_out, n_out], pads)
            assert _kct_length(pcb, "D_N") == pytest.approx(kicad, abs=1e-3)
            gains[label] = (kicad - base, copper(n_out) - copper(n))
        measured, geometric = gains["clear"]
        assert measured == pytest.approx(geometric, abs=1e-3)
        measured, geometric = gains["on"]
        assert measured < geometric - 0.01
