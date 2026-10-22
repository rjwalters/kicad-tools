"""KiCad tuning-pattern objects for skew bumps (Issue #7521).

Step (2) of #7521 (operator ruling Q16 on #6858): the post-route skew tuner
could write each bump it commits as a KiCad 10 ``(generated ...)``
tuning-pattern object owning the bump's segments.  Operator ruling Q32 (on
#7521) has the gated tuner commit chamfered *plain* copper instead, because
KiCad counts a pattern's partner span in full; the pattern writer stays for
hand-built bumps and is exercised on them here.

The pinned-toolchain tests at the bottom load such boards in kicad-cli
10.0.6 (CI's image; locally ``KCT_KICAD_DOCKER=1``) and skip elsewhere.
"""

from __future__ import annotations

import dataclasses
import json
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
from kicad_tools.router.diffpair_length_tuning import tune_diff_pair_skew
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.serpentine import SerpentineConfig
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.tuning_pattern import (
    MODE_DIFF_PAIR_SKEW,
    STATUS_TOO_SHORT,
    TuningPattern,
    bump_members,
    read_tuning_patterns,
    retag,
    skew_bump_pattern,
    tag_skew_bump,
)

pytestmark = pytest.mark.timeout(300)

W = 0.2
GAP = 0.25
Y_P = 100.0
Y_N = Y_P + W + GAP


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


def _straight(net: int, name: str, y: float, x2: float) -> Route:
    return Route(
        net=net, net_name=name, segments=[Segment(100.0, y, x2, y, W, Layer.F_CU, net, name)]
    )


def _tuned_pair(skew: float = 1.5, tolerance: float = 0.127) -> tuple[Route, Route, object]:
    p = _straight(1, "D_P", Y_P, 130.0)
    n = _straight(2, "D_N", Y_N, 130.0 - skew)
    p_out, n_out, result = tune_diff_pair_skew(
        _pair(), {1: p, 2: n}, tolerance_mm=tolerance, intra_pair_clearance_mm=GAP
    )
    return p_out, n_out, result


def _hand_bump() -> tuple[Route, Route, list[Segment]]:
    """N leg with one 45-degree bump of excursion 0.4 mm away from P."""
    e = 0.4
    pts = [
        (100, Y_N),
        (110, Y_N),
        (110 + e, Y_N + e),
        (111 + e, Y_N + e),
        (111 + 2 * e, Y_N),
        (120, Y_N),
    ]
    segs = [
        Segment(a[0], a[1], b[0], b[1], W, Layer.F_CU, 2, "D_N")
        for a, b in zip(pts, pts[1:], strict=False)
    ]
    p = _straight(1, "D_P", Y_P, 120.0)
    return p, Route(net=2, net_name="D_N", segments=segs), segs


# ---------------------------------------------------------------------------
# Format and tagging
# ---------------------------------------------------------------------------


class TestBumpMembers:
    def test_on_line_runs_are_not_members(self):
        _p, _n, segs = _hand_bump()
        assert bump_members(segs) == segs[1:4]

    def test_pattern_geometry(self):
        p, _n, segs = _hand_bump()
        pattern, members = skew_bump_pattern(
            segs, p, diff_pair_gap=GAP, min_spacing=0.6, tolerance_mm=0.127
        )
        assert members == segs[1:4]
        assert pattern.base_line == ((110.0, Y_N), (111.8, Y_N))
        assert pattern.base_line_coupled == ((110.0, Y_P), (111.8, Y_P))
        assert pattern.max_amplitude == pytest.approx(0.4)
        # Board y grows down: a bump toward +y while travelling +x is "right".
        assert pattern.initial_side == "right"
        assert (pattern.target_skew_min, pattern.target_skew_max) == (-0.127, 0.127)
        assert pattern.tuning_mode == MODE_DIFF_PAIR_SKEW

    def test_straight_run_has_no_pattern(self):
        seg = Segment(0, 0, 5, 0, W, Layer.F_CU, 2, "D_N")
        assert (
            skew_bump_pattern([seg], None, diff_pair_gap=GAP, min_spacing=0.6, tolerance_mm=0.1)
            is None
        )


class TestEmission:
    def test_untagged_route_emits_no_generator(self):
        p = _straight(1, "D_P", Y_P, 120.0)
        assert "(generated" not in p.to_sexp()

    def test_route_emits_one_pattern_naming_its_segments(self):
        p, n, segs = _hand_bump()
        pattern, members = skew_bump_pattern(
            segs, p, diff_pair_gap=GAP, min_spacing=0.6, tolerance_mm=0.127
        )
        tag_skew_bump(members, pattern)
        text = n.to_sexp(name_only=True)
        records = read_tuning_patterns(text)
        assert len(records) == 1
        rec = records[0]
        assert (rec.tuning_mode, rec.net_name, rec.layer) == (MODE_DIFF_PAIR_SKEW, "D_N", "F.Cu")
        seg_uuids = re.findall(r'\(segment.*?\(uuid "([^"]+)"\)', text, re.S)
        assert len(seg_uuids) == 5
        # KiCad keeps members sorted by uuid (and re-sorts them on save).
        assert rec.members == tuple(sorted(seg_uuids[1:4]))

    def test_tag_survives_copy_and_is_not_compared(self):
        _p, _n, segs = _hand_bump()
        pattern = TuningPattern(
            layer="F.Cu",
            net_name="D_N",
            base_line=((0, 0), (1, 0)),
            base_line_coupled=None,
            track_width=W,
            diff_pair_gap=GAP,
            min_amplitude=0.4,
            max_amplitude=0.4,
            min_spacing=0.6,
        )
        tag_skew_bump(segs[1:2], pattern)
        copy = dataclasses.replace(segs[1])
        assert copy.tuning_pattern is pattern
        assert copy == dataclasses.replace(segs[1], tuning_pattern=None)

    def test_pattern_members_formatting(self):
        pattern = TuningPattern(
            layer="B.Cu",
            net_name="/USB_D+",
            base_line=((1, 2), (3, 2)),
            base_line_coupled=None,
            track_width=W,
            diff_pair_gap=GAP,
            min_amplitude=0.4,
            max_amplitude=0.4,
            min_spacing=0.6,
        )
        text = pattern.to_sexp(["a", "b", "c"], "u")
        assert '(members "a" "b"\n\t\t\t"c"\n\t\t)' in text
        assert "base_line_coupled" not in text
        assert pattern.to_sexp([], "u").count("(members)") == 1
        assert read_tuning_patterns(text)[0].members == ("a", "b", "c")


class TestTunerCommitsPlainBumps:
    def test_committed_bump_is_plain_copper(self):
        """Operator ruling Q32: no tuning pattern on the tuner's bumps."""
        p, n, result = _tuned_pair()
        assert result.reason == "tuned" and result.inserts_applied >= 1
        assert all(s.tuning_pattern is None for s in p.segments)
        assert all(s.tuning_pattern is None for s in n.segments)
        assert len(n.segments) > 1

    def test_unreached_target_stays_plain(self):
        p = _straight(1, "D_P", Y_P, 130.0)
        n = _straight(2, "D_N", Y_N, 110.0)
        _p, n_out, result = tune_diff_pair_skew(
            _pair(),
            {1: p, 2: n},
            tolerance_mm=0.127,
            intra_pair_clearance_mm=GAP,
            max_inserts=1,
            config=SerpentineConfig(amplitude=0.2),
        )
        assert result.reason == "exceeded_max_inserts" and result.inserts_applied == 1
        assert all(s.tuning_pattern is None for s in n_out.segments)


# ---------------------------------------------------------------------------
# Pinned kicad-cli 10.0.6
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
\t\t(uuid "00000000-0000-4000-8000-000000007521")
\t)
"""

_RULES = """(version 1)
(rule "pair"
  (condition "A.inDiffPair('D_*')")
  (constraint diff_pair_gap (min 0.2mm) (opt 0.25mm) (max 0.3mm))
  (constraint diff_pair_uncoupled (max 1.0mm)))
"""


def _write_board(directory: Path, name: str, routes: list[Route]) -> Path:
    body = "\t" + "\n\t".join(r.to_sexp(name_only=True) for r in routes) + "\n"
    pcb = directory / f"{name}.kicad_pcb"
    pcb.write_text(_HEADER + body + "\t(embedded_fonts no)\n)\n")
    (directory / f"{name}.kicad_dru").write_text(_RULES)
    (directory / f"{name}.kicad_pro").write_text(
        json.dumps({"meta": {"filename": f"{name}.kicad_pro", "version": 3}})
    )
    return pcb


def _strip(route: Route) -> Route:
    return Route(
        net=route.net,
        net_name=route.net_name,
        segments=[dataclasses.replace(s, tuning_pattern=None) for s in route.segments],
    )


@pytest.fixture(scope="module")
def pinned_cli(tmp_path_factory):
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

    root = tmp_path_factory.mktemp("tuning_pattern_7521")
    try:
        with evidence_toolchain([root]) as toolchain:
            yield toolchain.kicad_cli, root
    except KiCadEnvironmentError as exc:
        pytest.skip(f"pinned kicad-cli unavailable: {exc}")


def _drc(cli: str, pcb: Path) -> list[dict]:
    report = pcb.with_suffix(".drc.json")
    subprocess.run(
        [cli, "pcb", "drc", "--format", "json", "--severity-all", "-o", str(report), str(pcb)],
        check=True,
        capture_output=True,
    )
    return json.loads(report.read_text()).get("violations", [])


def _uncoupled(violations: list[dict]) -> float:
    figures = [
        float(re.search(r"actual ([0-9.]+) mm", v["description"]).group(1))
        for v in violations
        if v["type"] == "diff_pair_uncoupled_length_too_long"
    ]
    return max(figures, default=0.0)


def _segments(text: str) -> list[tuple[float, ...]]:
    found = re.findall(r"\(segment\s*\(start ([^)]*)\)\s*\(end ([^)]*)\)", text)
    return sorted(tuple(float(v) for v in f"{a} {b}".split()) for a, b in found)


class TestPinnedKicadCli:
    def test_patterns_survive_load_and_save(self, pinned_cli):
        cli, root = pinned_cli
        p, n, segs = _hand_bump()
        pattern, members = skew_bump_pattern(
            segs, p, diff_pair_gap=GAP, min_spacing=0.6, tolerance_mm=0.127
        )
        tag_skew_bump(members, pattern)
        pcb = _write_board(root, "tuned", [p, n])
        before = pcb.read_text()
        assert len(read_tuning_patterns(before)) == 1
        _drc(cli, pcb)  # loads and checks without error

        saved = root / "tuned_saved.kicad_pcb"
        saved.write_text(before)
        subprocess.run(
            [cli, "pcb", "upgrade", "--force", str(saved)], check=True, capture_output=True
        )
        after = saved.read_text()
        assert [r.members for r in read_tuning_patterns(after)] == [
            r.members for r in read_tuning_patterns(before)
        ]
        assert _segments(after) == _segments(before)
        assert '(tuning_mode "diff_pair_skew")' in after
        assert '(last_status "tuned")' in after

    def test_too_short_status_round_trips(self, pinned_cli):
        cli, root = pinned_cli
        p, n, segs = _hand_bump()
        pattern, members = skew_bump_pattern(
            segs, p, diff_pair_gap=GAP, min_spacing=0.6, tolerance_mm=0.127
        )
        retag(members, pattern, last_status=STATUS_TOO_SHORT)
        pcb = _write_board(root, "too_short", [p, n])
        subprocess.run(
            [cli, "pcb", "upgrade", "--force", str(pcb)], check=True, capture_output=True
        )
        after = pcb.read_text()
        assert '(last_status "too_short")' in after
        assert len(read_tuning_patterns(after)) == 1

    def test_bump_members_leave_the_uncoupled_total(self, pinned_cli):
        cli, root = pinned_cli
        p, n, segs = _hand_bump()
        plain = _drc(cli, _write_board(root, "bump_plain", [p, _strip(n)]))
        pattern, members = skew_bump_pattern(
            segs, p, diff_pair_gap=GAP, min_spacing=0.6, tolerance_mm=0.127
        )
        tag_skew_bump(members, pattern)
        tuned = _drc(cli, _write_board(root, "bump_pattern", [p, n]))

        # Plain copper: the N bump itself (2 x 0.566 + 1.0 mm) is the figure,
        # and the bump trips the gap check.
        assert _uncoupled(plain) == pytest.approx(2.1314, abs=1e-3)
        assert any(v["type"] == "diff_pair_gap_out_of_range" for v in plain)
        # As a tuning pattern the bump is excluded; what remains is the P leg
        # opposite it (the base line, 1.8 mm), which KiCad still counts.
        assert _uncoupled(tuned) == pytest.approx(1.8, abs=1e-3)
        assert not any(v["type"] == "diff_pair_gap_out_of_range" for v in tuned)
