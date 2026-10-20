"""kct's uncoupled-length measure leaves tuning patterns out (Issue #7521).

Operator ruling Q16 (recorded on #6858 and #7521): kct must measure exactly
as pinned kicad-cli 10.0.6 does.  Tracks owned by a KiCad tuning pattern are
dropped from the measure on both sides -- they are not uncoupled copper of
their net, and the other net does not couple to them -- so the partner's
copper opposite a skew bump still counts.

The pinned-toolchain class compares ``kct check``'s measure with kicad-cli's
own ``diff_pair_uncoupled`` figure on the same boards; it runs in CI's KiCad
10.0.6 image (locally ``KCT_KICAD_DOCKER=1``) and skips elsewhere.
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path

import pytest

from kicad_tools.router.diffpair_length_tuning import _pair_uncoupled_mm, tune_diff_pair_skew
from kicad_tools.router.optimizer.serpentine import SerpentineConfig
from kicad_tools.router.primitives import Route
from kicad_tools.router.tuning_pattern import skew_bump_pattern, tag_skew_bump
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.diffpair_uncoupled_length import (
    DiffPairUncoupledLengthRule,
    measure_pair,
)
from tests.test_tuning_pattern_7521 import (
    _HEADER,
    GAP,
    Y_N,
    Y_P,
    _hand_bump,
    _pair,
    _straight,
)

pytestmark = pytest.mark.timeout(300)

#: kct-style rules: the router writes only ``diff_pair_gap (min ..)``, so
#: KiCad applies no distance limit, matching ``measure_uncoupled``.  The
#: ``max 0.01`` limit makes kicad-cli report every pair's figure.
_RULES = """(version 1)
(rule "pair"
  (condition "A.inDiffPair('D_*')")
  (constraint diff_pair_gap (min 0.2mm))
  (constraint diff_pair_uncoupled (max 0.01mm)))
"""


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
    return Route(
        net=route.net,
        net_name=route.net_name,
        segments=[dataclasses.replace(s, tuning_pattern=None) for s in route.segments],
    )


def _kct(pcb_path: Path):
    pcb = PCB.load(str(pcb_path))
    ids = {n.name: n.number for n in pcb.nets.values() if n.name}
    return measure_pair(pcb, ids["D_P"], ids["D_N"], "D_P", "D_N")


def _tagged_hand_bump() -> tuple[Route, Route]:
    p, n, segs = _hand_bump()
    pattern, members = skew_bump_pattern(
        segs, p, diff_pair_gap=GAP, min_spacing=0.6, tolerance_mm=0.127
    )
    tag_skew_bump(members, pattern)
    return p, n


def _tuned_pair() -> tuple[Route, Route]:
    """A real ``tune_diff_pair_skew`` bump on the 2 mm shorter N leg."""
    p = _straight(1, "D_P", Y_P, 130.0)
    n = _straight(2, "D_N", Y_N, 128.0)
    p_out, n_out, result = tune_diff_pair_skew(
        _pair(),
        {1: p, 2: n},
        tolerance_mm=0.127,
        intra_pair_clearance_mm=GAP,
        config=SerpentineConfig(amplitude=0.6),
    )
    assert result.reason == "tuned"
    return p_out, n_out


class TestKctMeasure:
    def test_hand_bump_plain(self, tmp_path):
        p, n = _tagged_hand_bump()
        m = _kct(_write(tmp_path, "plain", [p, _plain(n)]))
        # The bump's two 45-degree legs (2 x 0.566 mm) on N; P opposite them.
        assert m.n_uncoupled_mm == pytest.approx(1.1314, abs=1e-3)
        assert m.p_uncoupled_mm == pytest.approx(0.8, abs=1e-3)
        assert m.n_pattern_mm == 0.0

    def test_hand_bump_as_pattern(self, tmp_path):
        p, n = _tagged_hand_bump()
        m = _kct(_write(tmp_path, "pattern", [p, n]))
        # N's bump is out; the P span opposite the whole bump now counts.
        assert m.n_uncoupled_mm == pytest.approx(0.0, abs=1e-6)
        assert m.p_uncoupled_mm == pytest.approx(1.8, abs=1e-3)
        assert m.n_pattern_mm == pytest.approx(2.1314, abs=1e-3)
        assert m.uncoupled_mm == pytest.approx(1.8, abs=1e-3)

    def test_router_measure_agrees_with_kct_check(self, tmp_path):
        for name, (p, n) in {"hand": _tagged_hand_bump(), "tuned": _tuned_pair()}.items():
            m = _kct(_write(tmp_path, name, [p, n]))
            assert _pair_uncoupled_mm(p, n) == pytest.approx(m.uncoupled_mm, abs=1e-3)

    def test_finding_names_excluded_pattern_copper(self, tmp_path):
        from kicad_tools.validate.diffpair_engagement import PairThresholds

        p, n = _tagged_hand_bump()
        pcb = PCB.load(str(_write(tmp_path, "finding", [p, n])))
        ids = {net.name: net.number for net in pcb.nets.values() if net.name}
        key = (ids["D_P"], ids["D_N"])
        limits = PairThresholds(
            continuity=0.0, skew_mm=0.127, source_net=None, max_uncoupled_total_mm=1.0
        )
        rule = DiffPairUncoupledLengthRule(engaged_pairs={key}, limits={key: limits})
        results = rule.check(pcb, None)  # type: ignore[arg-type]
        [finding] = [v for v in results.violations if "uncoupled length" in v.message]
        assert finding.severity == "error"
        assert "1.800 mm" in finding.message
        assert "2.131 mm of tuning-pattern copper excluded" in finding.message


# ---------------------------------------------------------------------------
# Agreement with pinned kicad-cli 10.0.6
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pinned_cli(tmp_path_factory):
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

    root = tmp_path_factory.mktemp("uncoupled_7521")
    try:
        with evidence_toolchain([root]) as toolchain:
            yield toolchain.kicad_cli, root
    except KiCadEnvironmentError as exc:
        pytest.skip(f"pinned kicad-cli unavailable: {exc}")


def _kicad_figure(cli: str, pcb: Path) -> float:
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


class TestAgreesWithPinnedKicadCli:
    @pytest.mark.parametrize(
        ("case", "as_pattern"), [("hand", False), ("hand", True), ("tuned", True)]
    )
    def test_same_figure(self, pinned_cli, case, as_pattern):
        cli, root = pinned_cli
        p, n = _tagged_hand_bump() if case == "hand" else _tuned_pair()
        if not as_pattern:
            n = _plain(n)
        pcb = _write(root, f"{case}_{'pattern' if as_pattern else 'plain'}", [p, n])
        kicad = _kicad_figure(cli, pcb)
        assert kicad > 0.0
        assert _kct(pcb).uncoupled_mm == pytest.approx(kicad, abs=1e-3)

    def test_plain_trombone_agrees(self, pinned_cli):
        """The tuner's trombone as *plain* copper reads the same in both engines.

        Until Issue #7565 kct read it lower: the trombone's legs join the trace
        they leave, and kct took copper touching a trace anywhere as the same
        conductor, where kicad-cli decides at the pairing probe's ends
        (``docs/research/diffpair-primitive-pairing-spec.md``, group B).
        """
        cli, root = pinned_cli
        p, n = _tuned_pair()
        pcb = _write(root, "tuned_plain", [p, _plain(n)])
        assert _kct(pcb).uncoupled_mm == pytest.approx(_kicad_figure(cli, pcb), abs=1e-3)


class TestGuardedTunerAgreesWithPinnedKicadCli:
    """Issue #7521: with a binding uncoupled limit, what the tuner commits
    reads the same in kct and kicad-cli (no plain-copper fallback)."""

    @pytest.mark.parametrize(
        ("n_end", "limit"),
        [(128.0, 2.5), (128.0, 2.1), (129.5, 0.8)],
        ids=["skew2-limit2.5", "skew2-limit2.1", "skew0.5-limit0.8"],
    )
    def test_committed_result_agrees(self, pinned_cli, n_end, limit):
        cli, root = pinned_cli
        p = _straight(1, "D_P", Y_P, 130.0)
        n = _straight(2, "D_N", Y_N, n_end)
        before = _pair_uncoupled_mm(p, n)
        p_out, n_out, result = tune_diff_pair_skew(
            _pair(),
            {1: p, 2: n},
            tolerance_mm=0.127,
            intra_pair_clearance_mm=GAP,
            config=SerpentineConfig(amplitude=0.6),
            max_uncoupled_mm=limit,
        )
        pcb = _write(root, f"guarded_{n_end}_{limit}", [p_out, n_out])
        kct = _kct(pcb).uncoupled_mm
        kicad = _kicad_figure(cli, pcb)
        assert kct == pytest.approx(kicad, abs=1e-3)
        # The #6795 rule, judged by KiCad's own figure: within the limit, or
        # no worse than before the insert.
        assert kicad <= limit + 1e-3 or kicad <= before + 1e-3
        # Every committed bump is a pattern; none was demoted to plain copper.
        bump = [s for s in n_out.segments if s.tuning_pattern is not None]
        assert bool(bump) == bool(result.inserts_applied)

    def test_over_long_pair_is_refused_in_both_engines(self, pinned_cli):
        """P 20 / N 25 mm, 5 mm apart: any bump on P takes KiCad's figure 5.0 -> 6.2."""
        cli, root = pinned_cli
        p = _straight(1, "D_P", 100.0, 120.0)
        n = _straight(2, "D_N", 105.0, 125.0)
        p_out, n_out, result = tune_diff_pair_skew(
            _pair(),
            {1: p, 2: n},
            tolerance_mm=0.5,
            intra_pair_clearance_mm=0.1,
            max_uncoupled_mm=4.0,
        )
        assert result.inserts_applied == 0
        pcb = _write(root, "over_long", [p_out, n_out])
        assert _kct(pcb).uncoupled_mm == pytest.approx(_kicad_figure(cli, pcb), abs=1e-3)
        assert _kicad_figure(cli, pcb) == pytest.approx(5.0, abs=1e-3)
