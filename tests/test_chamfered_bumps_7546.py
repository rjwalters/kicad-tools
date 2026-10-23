"""Chamfered trombone bumps: kct and pinned kicad-cli agree (Issue #7546).

When #7546 landed, a square trombone loop's flat top was coupled to the
partner in kct's uncoupled-length model but uncoupled in pinned kicad-cli
10.0.6 (a real skew bump read 2.0 mm in kct and 2.8 mm in kicad-cli, #7542),
and the engines agreed only once each corner carried a 45-degree chamfer of
at least half a trace width.  Issue #7565 closed that divergence: kct now
reads plain bumps of any chamfer, square included, as kicad-cli does
(Issue #7593 converted the divergence tests here into agreement tests).

The half-width floor still matters, for a different reason: below it a
chamfered bump reads *more* uncoupled than a square one, from it up *less*
on thin traces, in both engines.  ``SerpentineConfig.corner_chamfer_widths``
adds the chamfers.  The diff-pair skew tuner uses them for its plain-copper
fallback (``SKEW_BUMP_CHAMFER_WIDTHS``, 0.55) and then never sizes a
chamfered bump below one trace width of amplitude.

Only the *crown* chamfers (where a leg meets the flat top) let the top couple
to the partner; a *foot* chamfer (where a leg leaves the host line) just
leaves the partner copper beside it uncoupled.  So when the four-corner
ladder fails, the tuner retries with asymmetric chamfers: wide crowns on
short 0.02 mm feet (``SKEW_BUMP_FOOT_CHAMFER_MM``).  Operator ruling Q32
wants every corner chamfered, so the feet are never square.  A loop then
costs the partner two crown chamfers plus two short foot chamfers, which is
what tunes the 0.2 mm #7542 pair at skew 0.5 mm under its 0.8 mm limit
(0.76 mm in both engines).

The pinned classes run in CI's KiCad 10.0.6 image (locally
``KCT_KICAD_DOCKER=1``) and skip elsewhere.
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
from kicad_tools.router.diffpair_detection import DetectedPair, DetectionSource
from kicad_tools.router.diffpair_length_tuning import (
    SKEW_BUMP_CHAMFER_WIDTHS,
    SKEW_BUMP_FOOT_CHAMFER_MM,
    exact_fit_amplitude,
    tune_diff_pair_skew,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.serpentine import SerpentineConfig, SerpentineGenerator
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.quantize import verify_segment_45
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.diffpair_uncoupled_length import measure_pair

pytestmark = pytest.mark.timeout(300)

GAP = 0.25
Y_P = 100.0


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


def _straight(net: int, name: str, y: float, x2: float, width: float = 0.2) -> Route:
    return Route(
        net=net,
        net_name=name,
        segments=[Segment(100.0, y, x2, y, width, Layer.F_CU, net, name)],
    )


def _bump(
    width: float, amplitude: float, need: float, chamfer: float, foot: float | None = None
) -> Route:
    """A generated single-sided bump on N (bulging away from P at Y_P)."""
    y_n = Y_P + width + GAP
    host = Segment(100.0, y_n, 125.0, y_n, width, Layer.F_CU, 2, "D_N")
    config = SerpentineConfig(
        amplitude=amplitude,
        side="outer",
        outer_normal_hint=(0.0, 1.0),
        corner_chamfer_widths=chamfer,
        foot_chamfer_length_mm=foot,
    )
    result = SerpentineGenerator(config).generate_trombone(host, need)
    assert result.success
    return Route(net=2, net_name="D_N", segments=result.new_segments)


# ---------------------------------------------------------------------------
# Geometry and sizing (no kicad-cli)
# ---------------------------------------------------------------------------


class TestChamferedGeometry:
    @pytest.mark.parametrize("amplitude", [0.2, 0.3, 0.6, 1.0])
    def test_length_model_matches_generated_copper(self, amplitude):
        config = SerpentineConfig(amplitude=amplitude, corner_chamfer_widths=0.55)
        need = 3 * config.loop_length_added(0.2, amplitude) - 1e-6
        route = _bump(0.2, amplitude, need, 0.55)
        added = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments) - 25.0
        assert added == pytest.approx(3 * config.loop_length_added(0.2, amplitude), abs=1e-9)
        assert config.amplitude_for(0.2, config.loop_length_added(0.2, amplitude)) == (
            pytest.approx(amplitude)
        )

    def test_every_leg_is_on_the_45_degree_grid(self):
        for seg in _bump(0.2, 0.6, 2.0, 0.55).segments:
            verify_segment_45(seg.x1, seg.y1, seg.x2, seg.y2, context="test")
            dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
            assert abs(dx) < 1e-9 or abs(dy) < 1e-9 or abs(abs(dx) - abs(dy)) < 1e-9

    def test_square_default_is_unchanged(self):
        config = SerpentineConfig(amplitude=0.6)
        assert config.corner_chamfer_widths == 0.0
        assert config.loop_length_added(0.2) == 1.2
        route = _bump(0.2, 0.6, 2.0, 0.0)
        legs = [s for s in route.segments if abs(s.x2 - s.x1) < 1e-12]
        assert legs and all(abs(abs(s.y2 - s.y1) - 0.6) < 1e-12 for s in legs)

    def test_exact_fit_reaches_the_need(self):
        config = SerpentineConfig(amplitude=1.0, corner_chamfer_widths=0.55)
        for need in (0.2, 0.9, 1.5, 4.0):
            loops, amp = exact_fit_amplitude(need, 1.0, config, 0.2)
            assert loops * config.loop_length_added(0.2, amp) == pytest.approx(need, rel=1e-6)

    def test_skew_tuner_bumps_are_chamfered(self):
        p = _straight(1, "D_P", Y_P, 130.0)
        n = _straight(2, "D_N", Y_P + 0.45, 128.0)
        _p, n_out, result = tune_diff_pair_skew(
            _pair(),
            {1: p, 2: n},
            tolerance_mm=0.127,
            intra_pair_clearance_mm=GAP,
            corner_chamfer_widths=SKEW_BUMP_CHAMFER_WIDTHS,
        )
        assert result.reason == "tuned"
        diagonal = [
            s for s in n_out.segments if abs(s.x2 - s.x1) > 1e-9 and abs(s.y2 - s.y1) > 1e-9
        ]
        assert diagonal
        chamfer = SKEW_BUMP_CHAMFER_WIDTHS * 0.2
        assert all(abs(s.x2 - s.x1) >= chamfer - 1e-6 for s in diagonal)

    def test_skew_tuner_bumps_are_chamfered_plain_copper(self):
        """Operator ruling Q32 (#7521): the gated tuner commits 45-degree-chamfered
        plain copper, never a tuning pattern, so KiCad's uncoupled figure counts
        just the bump's legs and nothing is excluded by labelling."""
        p = _straight(1, "D_P", Y_P, 130.0)
        n = _straight(2, "D_N", Y_P + 0.45, 128.0)
        _p, n_out, result = tune_diff_pair_skew(
            _pair(), {1: p, 2: n}, tolerance_mm=0.127, intra_pair_clearance_mm=GAP
        )
        assert result.reason == "tuned"
        assert all(s.tuning_pattern is None for s in n_out.segments)
        assert any(abs(s.x2 - s.x1) > 1e-9 and abs(s.y2 - s.y1) > 1e-9 for s in n_out.segments)

    def test_small_need_is_met_with_a_one_width_bump(self):
        p = _straight(1, "D_P", Y_P, 130.0)
        n = _straight(2, "D_N", Y_P + 0.45, 129.8)  # skew 0.2 mm, need ~0.09 mm
        _p, n_out, result = tune_diff_pair_skew(
            _pair(),
            {1: p, 2: n},
            tolerance_mm=0.127,
            intra_pair_clearance_mm=GAP,
            corner_chamfer_widths=SKEW_BUMP_CHAMFER_WIDTHS,
        )
        assert result.reason == "tuned" and result.skew_after_mm <= 0.127
        offsets = [abs(y - (Y_P + 0.45)) for s in n_out.segments for y in (s.y1, s.y2)]
        assert max(offsets) >= 0.2 - 1e-6  # amplitude never below one trace width


# ---------------------------------------------------------------------------
# Agreement with pinned kicad-cli 10.0.6
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
\t\t(uuid "00000000-0000-4000-8000-000000007546")
\t)
"""

#: kct-style rules: ``diff_pair_gap (min ..)`` only, the form kct writes, so
#: KiCad applies no distance limit; the 0.01 mm limit makes kicad-cli report
#: every pair's figure.
_RULES = """(version 1)
(rule "pair"
  (condition "A.inDiffPair('D_*')")
  (constraint diff_pair_gap (min 0.1mm))
  (constraint diff_pair_uncoupled (max 0.01mm)))
"""


def _no_tail_fold():
    """Bumps only (Issue #7891 tries a tail fold once no bump closes a pair)."""
    from unittest.mock import patch

    import kicad_tools.router.diffpair_tail_fold as fold

    return patch.object(fold, "tail_fold_options", lambda **_kw: iter(()))


def _write(directory: Path, name: str, routes: list[Route]) -> Path:
    body = "\t" + "\n\t".join(r.to_sexp(name_only=True) for r in routes) + "\n"
    pcb = directory / f"{name}.kicad_pcb"
    pcb.write_text(_HEADER + body + "\t(embedded_fonts no)\n)\n")
    (directory / f"{name}.kicad_dru").write_text(_RULES)
    (directory / f"{name}.kicad_pro").write_text(
        json.dumps({"meta": {"filename": f"{name}.kicad_pro", "version": 3}})
    )
    return pcb


def _plain(route: Route) -> Route:
    import dataclasses

    return Route(
        net=route.net,
        net_name=route.net_name,
        segments=[dataclasses.replace(s, tuning_pattern=None) for s in route.segments],
    )


def _kct(pcb_path: Path) -> float:
    pcb = PCB.load(str(pcb_path))
    ids = {n.name: n.number for n in pcb.nets.values() if n.name}
    return measure_pair(pcb, ids["D_P"], ids["D_N"], "D_P", "D_N").uncoupled_mm


@pytest.fixture(scope="module")
def pinned_cli(tmp_path_factory):
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

    root = tmp_path_factory.mktemp("chamfer_7546")
    try:
        with evidence_toolchain([root]) as toolchain:
            yield toolchain.kicad_cli, root
    except KiCadEnvironmentError as exc:
        pytest.skip(f"pinned kicad-cli unavailable: {exc}")


def _kicad(cli: str, pcb: Path) -> float:
    report = pcb.with_suffix(".drc.json")
    subprocess.run(
        [cli, "pcb", "drc", "--format", "json", "--severity-all", "-o", str(report), str(pcb)],
        check=True,
        capture_output=True,
    )
    figures = [
        float(re.search(r"actual ([0-9.]+) mm", v["description"]).group(1))
        for v in json.loads(report.read_text()).get("violations", [])
        if v["type"] == "diff_pair_uncoupled_length_too_long"
    ]
    return max(figures, default=0.0)


class TestPlainBumpsAgreeWithPinnedKicadCli:
    @pytest.mark.parametrize(
        ("width", "amplitude", "need"),
        [
            (0.2, 0.2, 0.15),  # the one-width floor, a single 45-degree leg
            (0.2, 0.6, 0.8),
            (0.2, 1.0, 4.0),  # several loops
            (0.15, 0.5, 1.5),
            (0.3, 0.8, 2.0),
        ],
    )
    def test_generated_chamfered_bump(self, pinned_cli, width, amplitude, need):
        cli, root = pinned_cli
        p = _straight(1, "D_P", Y_P, 125.0, width)
        n = _bump(width, amplitude, need, SKEW_BUMP_CHAMFER_WIDTHS)
        pcb = _write(root, f"bump_{width}_{amplitude}_{need}", [p, n])
        kicad = _kicad(cli, pcb)
        assert kicad > 0.0
        assert _kct(pcb) == pytest.approx(kicad, abs=1e-3)

    @pytest.mark.parametrize("n_end", [128.0, 129.5])
    def test_skew_tuner_output_as_plain_copper(self, pinned_cli, n_end):
        cli, root = pinned_cli
        p = _straight(1, "D_P", Y_P, 130.0)
        n = _straight(2, "D_N", Y_P + 0.45, n_end)
        p_out, n_out, result = tune_diff_pair_skew(
            _pair(),
            {1: p, 2: n},
            tolerance_mm=0.127,
            intra_pair_clearance_mm=GAP,
            corner_chamfer_widths=SKEW_BUMP_CHAMFER_WIDTHS,
        )
        assert result.reason == "tuned"
        pcb = _write(root, f"tuned_{n_end}", [_plain(p_out), _plain(n_out)])
        assert _kct(pcb) == pytest.approx(_kicad(cli, pcb), abs=1e-3)

    def test_square_bump_agrees(self, pinned_cli):
        """The pre-#7546 square loop: both engines read its flat tops as uncoupled.

        kct used to read it lower than kicad-cli; Issue #7565 closed that
        (``docs/research/diffpair-primitive-pairing-spec.md``, group B).
        """
        cli, root = pinned_cli
        p = _straight(1, "D_P", Y_P, 125.0)
        pcb = _write(root, "square", [p, _bump(0.2, 0.6, 0.8, 0.0)])
        assert _kct(pcb) == pytest.approx(_kicad(cli, pcb), abs=1e-3)

    def test_thin_chamfer_agrees(self, pinned_cli):
        """A chamfer of 0.4 widths (0.08 mm) reads the same in both engines too."""
        cli, root = pinned_cli
        p = _straight(1, "D_P", Y_P, 125.0)
        pcb = _write(root, "thin_chamfer", [p, _bump(0.2, 0.6, 0.8, 0.4)])
        assert _kct(pcb) == pytest.approx(_kicad(cli, pcb), abs=1e-3)


class TestBindingLimitFallsBackToChamferedPlainCopper:
    """Issue #7546 AC3: with a binding uncoupled limit, a bump whose pattern
    form fails is retried as chamfered plain copper, which both engines
    measure the same way, and committed only if that honest figure passes."""

    @staticmethod
    def _tune(width: float, n_end: float, limit: float | None):
        p = _straight(1, "D_P", Y_P, 130.0, width)
        n = _straight(2, "D_N", Y_P + width + GAP, n_end, width)
        p_out, n_out, result = tune_diff_pair_skew(
            _pair(),
            {1: p, 2: n},
            tolerance_mm=0.127,
            intra_pair_clearance_mm=GAP,
            max_uncoupled_mm=limit,
        )
        return p, n, p_out, n_out, result

    def test_bump_is_plain_even_when_a_pattern_form_would_pass(self):
        # A generous limit: the old pattern-first search would have committed a
        # tuning pattern here.  Q32 emits chamfered plain copper regardless.
        *_, n_out, result = self._tune(0.2, 128.0, 8.0)
        assert result.reason == "tuned"
        assert all(s.tuning_pattern is None for s in n_out.segments)

    def test_unguarded_tuning_is_plain_too(self):
        *_, n_out, result = self._tune(0.127, 129.5, None)
        assert result.reason == "tuned"
        assert all(s.tuning_pattern is None for s in n_out.segments)

    @pytest.mark.parametrize(("n_end", "limit"), [(129.5, 1.1), (129.2, 1.7)])
    def test_thin_trace_pair_is_tuned_plain_and_engines_agree(self, pinned_cli, n_end, limit):
        cli, root = pinned_cli
        p, n, p_out, n_out, result = self._tune(0.127, n_end, limit)
        assert result.reason == "tuned" and result.skew_after_mm <= 0.127
        # Plain copper (Q32); the four-corner ladder closes both pairs, its
        # capped bumps reading 0.78 / 1.08 mm in both engines.
        assert all(s.tuning_pattern is None for s in n_out.segments)
        diagonal = [
            s for s in n_out.segments if abs(s.x2 - s.x1) > 1e-9 and abs(s.y2 - s.y1) > 1e-9
        ]
        assert diagonal
        pcb = _write(root, f"thin_{n_end}_{limit}", [p_out, n_out])
        kicad = _kicad(cli, pcb)
        assert _kct(pcb) == pytest.approx(kicad, abs=1e-3)
        assert kicad <= limit + 1e-3

    def test_wide_trace_pair_at_skew_2_and_limit_2_1_fits_no_bump(self, pinned_cli):
        """#7542's 0.2 mm pair, skew 2.0 mm under a 2.1 mm limit: the 2.0 mm
        tail leaves 0.1 mm, and every loop costs the partner two crown
        chamfers plus two foot chamfers (0.26 mm), so no bump fits."""
        cli, root = pinned_cli
        with _no_tail_fold():
            *_, p_out, n_out, result = self._tune(0.2, 128.0, 2.1)
        assert result.inserts_applied == 0
        pcb = _write(root, "wide_128.0_2.1", [p_out, n_out])
        assert _kct(pcb) == pytest.approx(_kicad(cli, pcb), abs=1e-3)

    def test_wide_trace_pair_at_skew_2_and_limit_2_1_is_tuned_by_a_tail_fold(self, pinned_cli):
        """Issue #7891: N folds past its own end into P's uncoupled tail; the
        pair reads 0.622 mm in both engines (and 1.531 mm with P stored the
        other way, ``tests/test_pad_aware_skew_tuning_7891.py``)."""
        cli, root = pinned_cli
        *_, p_out, n_out, result = self._tune(0.2, 128.0, 2.1)
        assert result.reason == "tuned" and result.skew_after_mm <= 0.127
        pcb = _write(root, "wide_fold_128.0_2.1", [p_out, n_out])
        kicad = _kicad(cli, pcb)
        assert _kct(pcb) == pytest.approx(kicad, abs=1e-3)
        assert kicad == pytest.approx(0.622, abs=1e-3)
        _assert_no_square_corner(n_out)


# ---------------------------------------------------------------------------
# Asymmetric chamfers (Issue #7546): wide crowns, short (never square) feet
# ---------------------------------------------------------------------------

FOOT = SKEW_BUMP_FOOT_CHAMFER_MM


def _turns_deg(route: Route) -> list[float]:
    """The direction change (degrees) at every vertex of ``route``'s chain."""
    segs = route.segments
    turns = []
    for a, b in zip(segs, segs[1:], strict=False):
        assert (a.x2, a.y2) == pytest.approx((b.x1, b.y1), abs=1e-9)
        da = math.atan2(a.y2 - a.y1, a.x2 - a.x1)
        db = math.atan2(b.y2 - b.y1, b.x2 - b.x1)
        turn = abs(math.degrees(db - da)) % 360.0
        turns.append(min(turn, 360.0 - turn))
    return turns


def _assert_no_square_corner(route: Route) -> None:
    """Every vertex of a generated bump turns by 45 degrees (or not at all):
    no 90-degree corner anywhere, feet included (operator ruling Q32)."""
    turns = _turns_deg(route)
    assert turns
    for turn in turns:
        assert turn == pytest.approx(0.0, abs=1e-6) or turn == pytest.approx(45.0, abs=1e-6), turns


class TestAsymmetricChamferGeometry:
    def test_foot_chamfer_is_0_02_mm(self):
        assert FOOT == 0.02

    @pytest.mark.parametrize("amplitude", [0.2, 0.3, 0.495])
    def test_length_model_matches_generated_copper(self, amplitude):
        config = SerpentineConfig(
            amplitude=amplitude, corner_chamfer_widths=0.55, foot_chamfer_length_mm=FOOT
        )
        added = config.loop_length_added(0.2, amplitude)
        assert added == pytest.approx(2 * amplitude - 2 * (0.11 + FOOT) * (2 - math.sqrt(2)))
        route = _bump(0.2, amplitude, 2 * added - 1e-6, 0.55, FOOT)
        copper = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments) - 25.0
        assert copper == pytest.approx(2 * added, abs=1e-9)
        assert config.amplitude_for(0.2, added) == pytest.approx(amplitude)
        assert config.loop_forward(0.2, amplitude) == pytest.approx(0.8 + 2 * (0.11 + FOOT))

    def test_crowns_are_wide_and_feet_short(self):
        """Diagonals meeting the flat top advance 0.11 mm (>= W/2); those
        leaving the host line advance 0.02 mm."""
        route = _bump(0.2, 0.4, 1.0, 0.55, FOOT)
        y_n = Y_P + 0.2 + GAP
        diagonal = [
            s for s in route.segments if abs(s.x2 - s.x1) > 1e-9 and abs(s.y2 - s.y1) > 1e-9
        ]
        crowns = [s for s in diagonal if max(s.y1, s.y2) == pytest.approx(y_n + 0.4)]
        feet = [s for s in diagonal if min(s.y1, s.y2) == pytest.approx(y_n)]
        assert crowns and feet and len(crowns) + len(feet) == len(diagonal)
        for s in diagonal:
            verify_segment_45(s.x1, s.y1, s.x2, s.y2, context="test")
        assert all(abs(s.x2 - s.x1) == pytest.approx(0.11) for s in crowns)
        assert all(abs(s.x2 - s.x1) == pytest.approx(FOOT) for s in feet)
        assert 0.11 >= 0.2 / 2

    @pytest.mark.parametrize("amplitude", [0.1, 0.13, 0.2, 0.4, 0.495])
    def test_no_generated_corner_is_square(self, amplitude):
        """Down to amplitudes where crown + foot span the whole leg (one 45
        degree run), every bump corner is chamfered (operator ruling Q32)."""
        config = SerpentineConfig(
            amplitude=amplitude, corner_chamfer_widths=0.55, foot_chamfer_length_mm=FOOT
        )
        route = _bump(
            0.2, amplitude, 3 * config.loop_length_added(0.2, amplitude) - 1e-6, 0.55, FOOT
        )
        _assert_no_square_corner(route)

    def test_symmetric_default_is_unchanged(self):
        """``foot_chamfer_length_mm=None`` keeps all four corners alike (#7564)."""
        a = SerpentineConfig(amplitude=0.6, corner_chamfer_widths=0.55)
        b = SerpentineConfig(amplitude=0.6, corner_chamfer_widths=0.55, foot_chamfer_length_mm=0.11)
        for amp in (0.15, 0.2, 0.6):
            assert a.loop_length_added(0.2, amp) == pytest.approx(b.loop_length_added(0.2, amp))
            assert a.chamfer_mm(0.2, amp) == pytest.approx(min(0.11, amp / 2))
            assert a.foot_chamfer_mm(0.2, amp) == a.chamfer_mm(0.2, amp)
        assert [(s.x1, s.y1, s.x2, s.y2) for s in _bump(0.2, 0.6, 2.0, 0.55).segments] == [
            (s.x1, s.y1, s.x2, s.y2) for s in _bump(0.2, 0.6, 2.0, 0.55, 0.11).segments
        ]

    def test_binding_limit_bumps_have_short_chamfered_feet(self):
        """The four-corner ladder cannot fit this pair under 0.8 mm; the
        asymmetric-chamfer cascade that follows can (Issue #7546), and every
        corner of its bump is chamfered, feet included."""
        p = _straight(1, "D_P", Y_P, 130.0)
        n = _straight(2, "D_N", Y_P + 0.45, 129.5)
        _p, n_out, result = tune_diff_pair_skew(
            _pair(),
            {1: p, 2: n},
            tolerance_mm=0.127,
            intra_pair_clearance_mm=GAP,
            max_uncoupled_mm=0.8,
        )
        assert result.reason == "tuned"
        diagonal = [
            s for s in n_out.segments if abs(s.x2 - s.x1) > 1e-9 and abs(s.y2 - s.y1) > 1e-9
        ]
        assert {round(abs(s.x2 - s.x1), 6) for s in diagonal} == {FOOT, 0.11}
        _assert_no_square_corner(n_out)

    def test_pair_the_ladder_tunes_keeps_four_chamfered_corners(self):
        """A pair the four-corner ladder closes keeps its bumps (Issue #7546:
        asymmetric chamfers are only tried after the ladder fails)."""
        p = _straight(1, "D_P", Y_P, 120.0)
        n = _straight(2, "D_N", Y_P + 0.45, 119.88)
        _p, n_out, result = tune_diff_pair_skew(
            _pair(), {1: p, 2: n}, tolerance_mm=0.05, intra_pair_clearance_mm=GAP
        )
        assert result.reason == "tuned" and result.skew_after_mm <= 0.05
        y_n = Y_P + 0.45
        assert any(
            min(s.y1, s.y2) == pytest.approx(y_n) and abs(s.x2 - s.x1) > 1e-9
            for s in n_out.segments
            if abs(s.y2 - s.y1) > 1e-9
        )


class TestAsymmetricChamferBumpsAgreeWithPinnedKicadCli:
    """The crown-chamfer rule measured on pinned kicad-cli 10.0.6.

    A 0.2 mm pair, N a 25 mm host with one generated bump, P a straight 25 mm
    run: the partner loses exactly the length of copper beside no parallel N
    copper, so each loop costs it its two crown chamfers plus its two foot
    chamfers."""

    @pytest.mark.parametrize(
        ("width", "amplitude", "need"),
        [(0.2, 0.2, 0.25), (0.2, 0.495, 1.6), (0.15, 0.3, 1.0), (0.3, 0.6, 1.5)],
    )
    def test_asymmetric_bump_agrees(self, pinned_cli, width, amplitude, need):
        cli, root = pinned_cli
        p = _straight(1, "D_P", Y_P, 125.0, width)
        n = _bump(width, amplitude, need, SKEW_BUMP_CHAMFER_WIDTHS, FOOT)
        pcb = _write(root, f"foot_{width}_{amplitude}_{need}", [p, n])
        kicad = _kicad(cli, pcb)
        assert kicad > 0.0
        assert _kct(pcb) == pytest.approx(kicad, abs=1e-3)

    @pytest.mark.parametrize(("foot", "expected"), [(FOOT, 0.26), (0.05, 0.32), (0.11, 0.44)])
    def test_partner_loses_the_chamfers_beside_it(self, pinned_cli, foot, expected):
        """One loop, 0.11 mm crowns: P loses 2 x 0.11 mm plus both feet's
        chamfers (2 x 0.02 / 0.05 / 0.11 mm)."""
        cli, root = pinned_cli
        p = _straight(1, "D_P", Y_P, 125.0)
        config = SerpentineConfig(
            amplitude=0.4, corner_chamfer_widths=0.55, foot_chamfer_length_mm=foot
        )
        n = _bump(0.2, 0.4, config.loop_length_added(0.2, 0.4) - 1e-6, 0.55, foot)
        pcb = _write(root, f"foot_cost_{foot}", [p, n])
        pcb_pcb = PCB.load(str(pcb))
        ids = {net.name: net.number for net in pcb_pcb.nets.values() if net.name}
        measured = measure_pair(pcb_pcb, ids["D_P"], ids["D_N"], "D_P", "D_N")
        assert measured.p_uncoupled_mm == pytest.approx(expected, abs=1e-3)
        assert max(measured.p_uncoupled_mm, measured.n_uncoupled_mm) == pytest.approx(
            _kicad(cli, pcb), abs=1e-3
        )


class TestBindingLimitTunesTheWidePairs:
    """Issue #7546 AC3 on #7542's 0.2 mm pairs (P 30 mm, N shorter)."""

    @staticmethod
    def _tune(n_end: float, limit: float):
        return TestBindingLimitFallsBackToChamferedPlainCopper._tune(0.2, n_end, limit)

    def test_skew_0_5_is_tuned_under_its_0_8_limit(self, pinned_cli):
        """Four chamfered corners read 0.94 mm and the square loop 0.90 mm
        (the ladder refuses the pair); 0.11 mm crowns on 0.02 mm feet read
        0.76 mm in both engines."""
        cli, root = pinned_cli
        *_, p_out, n_out, result = self._tune(129.5, 0.8)
        assert result.reason == "tuned" and result.skew_after_mm <= 0.127
        assert all(s.tuning_pattern is None for s in n_out.segments)
        pcb = _write(root, "wide_129.5_0.8", [p_out, n_out])
        kicad = _kicad(cli, pcb)
        assert _kct(pcb) == pytest.approx(kicad, abs=1e-3)
        assert kicad <= 0.8
        assert kicad == pytest.approx(0.76, abs=1e-3)
        _assert_no_square_corner(n_out)

    def test_skew_0_5_is_tuned_without_kicad(self):
        *_, result = self._tune(129.5, 0.8)
        assert result.reason == "tuned" and result.skew_after_mm <= 0.127

    def test_skew_2_0_under_2_5_closes_more_of_the_skew(self, pinned_cli):
        """Under the 2x-gap excursion cap the asymmetric-chamfer bump closes
        0.84 mm of the skew and reads 2.26 mm against the 2.5 mm limit; the
        four-corner ladder left 1.27 mm of skew at 2.44 mm.  (Bumps only: the
        tail fold of Issue #7891 closes this pair outright.)"""
        cli, root = pinned_cli
        with _no_tail_fold():
            *_, p_out, n_out, result = self._tune(128.0, 2.5)
        assert result.inserts_applied == 1
        assert result.skew_after_mm == pytest.approx(1.162, abs=1e-3)
        pcb = _write(root, "wide_128.0_2.5", [p_out, n_out])
        kicad = _kicad(cli, pcb)
        assert _kct(pcb) == pytest.approx(kicad, abs=1e-3)
        assert kicad == pytest.approx(2.26, abs=1e-3)
        _assert_no_square_corner(n_out)
