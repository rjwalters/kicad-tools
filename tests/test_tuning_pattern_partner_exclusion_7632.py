"""Issue #7632 (operator ruling Q31 on #7521): black-box probes behind
docs/research/kicad-tuning-pattern-uncoupled-exclusion.md.

The question: when a skew bump is a KiCad tuning-pattern object, does pinned
kicad-cli 10.0.6 also leave the **partner** leg's copper beside the bump out
of its ``diff_pair_uncoupled`` figure?

Every probe is a two-net board (``D_P`` and ``D_N``) built with kct's own
writers (:meth:`Segment.to_sexp` and :meth:`TuningPattern.to_sexp`, with
deterministic uuids so the committed boards are reproducible), committed under
``tests/fixtures/tuning_pattern_partner_probes/``.  Each one records the
``diff_pair_uncoupled_length_too_long`` ``actual`` figure kicad-cli 10.0.6
reports for it (the maximum is 0.01 mm so the rule always fires), read off the
board alone: no KiCad source was consulted, in any form.

``test_kct_matches`` also holds kct's own measure (``kct check``) to every figure.

Regenerate the boards after editing the table::

    uv run python -m tests.test_tuning_pattern_partner_exclusion_7632

Re-read every figure on the pinned toolchain (``KCT_KICAD_DOCKER=1`` on a Mac)::

    KCT_KICAD_DOCKER=1 uv run pytest tests/test_tuning_pattern_partner_exclusion_7632.py --no-cov
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Segment
from kicad_tools.router.tuning_pattern import TuningPattern

pytestmark = pytest.mark.timeout(900)

PROBE_DIR = Path(__file__).resolve().parent / "fixtures" / "tuning_pattern_partner_probes"

#: Track width and nominal edge-to-edge pair gap (mm); pitch is W + GAP.
W = 0.2
GAP = 0.25
PITCH = W + GAP
Y_P = 100.0
Y_N = Y_P + PITCH
X0, X1 = 100.0, 120.0
#: Where every bump leaves N's host line.
XS = 108.0

#: Bump flat-top widths: the tuner's default loop width, and a long one.
SHORT = 0.4
LONG = 4.0

_HDR = """(kicad_pcb
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
\t\t(uuid "{edge}")
\t)
"""

#: kct-style rules (the router writes only ``diff_pair_gap (min ..)``); the
#: ``max 0.01`` limit makes kicad-cli report every pair's figure.
RULES = """(version 1)
(rule "pair"
  (condition "A.inDiffPair('D_*')")
  (constraint diff_pair_gap (min 0.2mm))
  (constraint diff_pair_uncoupled (max 0.01mm)))
"""


def _uid(*parts: object) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "kicad-tools/7632/" + "/".join(map(str, parts))))


Pt = tuple[float, float]


def _r(v: float) -> float:
    return round(v, 4)


def _path(points: list[Pt], net: int) -> list[Segment]:
    name = {1: "D_P", 2: "D_N"}[net]
    return [
        Segment(_r(a[0]), _r(a[1]), _r(b[0]), _r(b[1]), W, Layer.F_CU, net, name)
        for a, b in zip(points, points[1:], strict=False)
    ]


class Bump(NamedTuple):
    """N's path, which of its segments are bump copper, and the base line."""

    n: list[Segment]
    members: list[int]
    xs: float
    xe: float
    height: float


def square_bump(height: float, width: float) -> Bump:
    """N with one perpendicular-leg bump pushed ``height`` away from P (the
    shape ``tune_diff_pair_skew`` writes today)."""
    xe = XS + width
    yb = Y_N + height
    pts = [(X0, Y_N), (XS, Y_N), (XS, yb), (xe, yb), (xe, Y_N), (X1, Y_N)]
    return Bump(_path(pts, 2), [1, 2, 3], XS, xe, height)


def chamfer_bump(height: float, width: float) -> Bump:
    """N with one 45-degree-leg bump (fully chamfered corners)."""
    xe = XS + 2 * height + width
    yb = Y_N + height
    pts = [(X0, Y_N), (XS, Y_N), (XS + height, yb), (xe - height, yb), (xe, Y_N), (X1, Y_N)]
    return Bump(_path(pts, 2), [1, 2, 3], XS, xe, height)


def two_loop_bump(height: float) -> Bump:
    """N with the tuner's two-loop square bump (0.4 mm loops, 0.4 mm apart)."""
    yb = Y_N + height
    a, b, c, d = XS, XS + 0.4, XS + 0.8, XS + 1.2
    pts = [(X0, Y_N), (a, Y_N), (a, yb), (b, yb), (b, Y_N), (c, Y_N)]
    pts += [(c, yb), (d, yb), (d, Y_N), (X1, Y_N)]
    return Bump(_path(pts, 2), [1, 2, 3, 5, 6, 7], a, d, height)


def p_straight() -> list[Segment]:
    return _path([(X0, Y_P), (X1, Y_P)], 1)


def p_split(xs: float, xe: float) -> list[Segment]:
    """P straight, but cut into three pieces at the bump's base-line ends."""
    return _path([(X0, Y_P), (xs, Y_P), (xe, Y_P), (X1, Y_P)], 1)


def p_follow(b: Bump) -> list[Segment]:
    """P jogging with N's square bump at constant pitch (P on the inside)."""
    yb = Y_P + b.height
    xi, xo = b.xs + PITCH, b.xe - PITCH
    return _path([(X0, Y_P), (xi, Y_P), (xi, yb), (xo, yb), (xo, Y_P), (X1, Y_P)], 1)


def _pattern(b: Bump, *, net_name: str = "D_N", **changes: object) -> TuningPattern:
    pattern = TuningPattern(
        layer="F.Cu",
        net_name=net_name,
        base_line=((b.xs, Y_N), (b.xe, Y_N)),
        base_line_coupled=((b.xs, Y_P), (b.xe, Y_P)),
        track_width=W,
        diff_pair_gap=GAP,
        min_amplitude=b.height,
        max_amplitude=b.height,
        min_spacing=0.6,
        initial_side="right",
        target_skew_min=-0.127,
        target_skew_max=0.127,
    )
    return replace(pattern, **changes) if changes else pattern


class Group(NamedTuple):
    """One pattern: its definition and the (net, segment index) members."""

    pattern: TuningPattern
    members: list[tuple[str, int]]


def board_text(name: str, p: list[Segment], n: list[Segment], groups: list[Group]) -> str:
    """The probe board, written with kct's segment and tuning-pattern writers."""

    def seg_uid(net: str, i: int) -> str:
        return _uid(name, net, i)

    body = [
        s.to_sexp(name_only=True, uuid=seg_uid(net, i))
        for net, segs in (("D_P", p), ("D_N", n))
        for i, s in enumerate(segs)
    ]
    for k, g in enumerate(groups):
        uids = [seg_uid(net, i) for net, i in g.members]
        body.append(g.pattern.to_sexp(uids, _uid(name, "pattern", k)))
    return (
        _HDR.replace("{edge}", _uid("edge"))
        + "\t"
        + "\n\t".join(body)
        + "\n\t(embedded_fonts no)\n)\n"
    )


class Probe(NamedTuple):
    group: str
    p: list[Segment]
    n: list[Segment]
    patterns: list[Group]
    #: kicad-cli 10.0.6 ``actual`` (mm), the pair figure.
    kicad_mm: float
    what: str


#: name -> (pinned kicad-cli 10.0.6 ``actual`` in mm, what the figure shows).  Read off
#: the committed boards; ``test_kicad_figure`` re-reads every one.  h = push-out, B =
#: the bump's base-line length (where it leaves and rejoins N's host line).
KICAD: dict[str, tuple[float, str]] = {
    "ctl_straight": (0.0, "two straight 20 mm legs: fully coupled"),
    # A: square bump, plain copper.  Figure = B + 2h: the whole bump on N.
    "a_square_h05_short_plain": (0.65, "0.4 + 2 x 0.125: legs and top all uncoupled"),
    "a_square_h05_long_plain": (4.25, "4 + 2 x 0.125"),
    "a_square_h09_short_plain": (0.85, "0.4 + 2 x 0.225"),
    "a_square_h09_long_plain": (4.45, "4 + 2 x 0.225 (P's 4 mm beside the top is uncoupled too)"),
    "a_square_h20_short_plain": (1.4, "0.4 + 2 x 0.5"),
    "a_square_h20_long_plain": (5.0, "4 + 2 x 0.5"),
    # B: the same bump as kct's tuning pattern today.  Figure = B: P's span beside it.
    "b_square_h05_short_pattern": (0.4, "bump out; P's 0.4 mm span beside it counts"),
    "b_square_h05_long_pattern": (4.0, "bump out; P's 4 mm span beside it counts"),
    "b_square_h09_short_pattern": (0.4, "P's span, whatever the height"),
    "b_square_h09_long_pattern": (4.0, "P's span, whatever the height"),
    "b_square_h20_short_pattern": (0.4, "P's span, whatever the height"),
    "b_square_h20_long_pattern": (4.0, "P's span, whatever the height"),
    # C: 45-degree legs.  Plain: only the two legs (2 x 0.318); pattern: B = 2h + top.
    "c_chamfer_h09_short_plain": (0.6364, "only the 45-degree legs: the top couples to P"),
    "c_chamfer_h09_long_plain": (0.6364, "only the legs, independent of the top's length"),
    "c_chamfer_h09_short_pattern": (0.85, "P's span B = 0.4 + 2 x 0.225"),
    "c_chamfer_h09_long_pattern": (4.45, "P's span B = 4 + 2 x 0.225: 7x the plain figure"),
    # D: the tuner's two 0.4 mm loops, 0.4 mm apart.
    "d_two_loop_h09_plain": (1.7, "2 x (0.4 + 2 x 0.225)"),
    "d_two_loop_h09_pattern": (0.8, "P beside the two loops (2 x 0.4); the run between couples"),
    # E: P jogs to follow the bump at constant pitch (inside path).
    "e_follow_h09_plain": (1.35, "N's legs (0.45) + N's top beyond P's shorter top (0.9)"),
    "e_follow_h09_n_pattern": (4.45, "N's bump out: P's whole jog (B + 2h) is beside nothing"),
    "e_follow_h09_both_pattern": (0.9, "both jogs members: P's 2 x 0.45 run to its corners"),
    "e_follow_h20_plain": (1.8, "legs overlap 0.05 mm each: 0.9 + 0.9"),
    "e_follow_h20_n_pattern": (5.0, "P's whole jog, 4 + 2 x 0.5"),
    "e_follow_h20_both_pattern": (0.9, "same as at 0.9x"),
    # F: emission variants (square bump, h = 0.9 x gap).
    "f_short_split_plain": (0.85, "cutting P at the base-line ends changes nothing (= A)"),
    "f_long_split_plain": (4.45, "cutting P at the base-line ends changes nothing (= A)"),
    "f_short_split_n_pattern": (0.4, "nor with N's pattern (= B)"),
    "f_long_split_n_pattern": (4.0, "nor with N's pattern (= B)"),
    "f_short_no_coupled_baseline": (0.4, "base_line_coupled omitted: no effect (= B)"),
    "f_long_no_coupled_baseline": (4.0, "base_line_coupled omitted: no effect (= B)"),
    "f_short_partner_span_same_pattern": (0.0, "P's span a member of N's pattern: excluded"),
    "f_long_partner_span_same_pattern": (0.0, "P's span a member of N's pattern: excluded"),
    "f_short_partner_span_own_pattern": (0.0, "P's span in its own D_P pattern: excluded"),
    "f_long_partner_span_own_pattern": (0.0, "P's span in its own D_P pattern: excluded"),
    "f_short_mode_diff_pair_both": (0.0, "tuning_mode diff_pair owning both: excluded"),
    "f_long_mode_diff_pair_both": (0.0, "tuning_mode diff_pair owning both: excluded"),
    # G: membership is what counts, not geometry.
    "g_whole_partner_leg_member": (16.0, "all of P a member: N's 16 mm host is beside nothing"),
    "g_partner_detour_plain": (5.6569, "P's two 45-degree detour legs (2 x 2.83)"),
    "g_partner_detour_member": (8.0, "P's detour out: N's 8 mm opposite it counts"),
}


PROBES: dict[str, Probe] = {}


def _add(name: str, group: str, p, n, patterns) -> None:
    assert name not in PROBES, name
    kicad_mm, what = KICAD[name]
    PROBES[name] = Probe(group, p, n, patterns, kicad_mm, what)


def _n_only(b: Bump, **changes: object) -> list[Group]:
    """kct's emission today: one pattern owning N's off-line segments."""
    return [Group(_pattern(b, **changes), [("D_N", i) for i in b.members])]


_HEIGHTS = {"h05": 0.5 * GAP, "h09": 0.9 * GAP, "h20": 2.0 * GAP}
_WIDTHS = {"short": SHORT, "long": LONG}

# -- Group 0: controls --------------------------------------------------------------
_add("ctl_straight", "0", p_straight(), _path([(X0, Y_N), (X1, Y_N)], 2), [])

# -- Groups A/B: square bump (the tuner's shape), plain copper vs tuning pattern --------
for hk, h in _HEIGHTS.items():
    for wk, w in _WIDTHS.items():
        b = square_bump(h, w)
        _add(f"a_square_{hk}_{wk}_plain", "A", p_straight(), b.n, [])
        _add(f"b_square_{hk}_{wk}_pattern", "B", p_straight(), b.n, _n_only(b))

# -- Group C: 45-degree (chamfered) bump at the compliant 0.9x height -----------------
for wk, w in _WIDTHS.items():
    b = chamfer_bump(0.9 * GAP, w)
    _add(f"c_chamfer_h09_{wk}_plain", "C", p_straight(), b.n, [])
    _add(f"c_chamfer_h09_{wk}_pattern", "C", p_straight(), b.n, _n_only(b))

# -- Group D: the tuner's two-loop bump at 0.9x ----------------------------------------
_b2 = two_loop_bump(0.9 * GAP)
_add("d_two_loop_h09_plain", "D", p_straight(), _b2.n, [])
_add("d_two_loop_h09_pattern", "D", p_straight(), _b2.n, _n_only(_b2))

# -- Group E: partner jogging to follow the bump (long bump only: the inside path needs
# the bump wider than 2 pitches) ---------------------------------------------------------
for hk in ("h09", "h20"):
    b = square_bump(_HEIGHTS[hk], LONG)
    pf = p_follow(b)
    _add(f"e_follow_{hk}_plain", "E", pf, b.n, [])
    _add(f"e_follow_{hk}_n_pattern", "E", pf, b.n, _n_only(b))
    both = [Group(_pattern(b), [("D_N", i) for i in b.members] + [("D_P", i) for i in (1, 2, 3)])]
    _add(f"e_follow_{hk}_both_pattern", "E", pf, b.n, both)

# -- Group F: emission variants that might take the partner span out (0.9x) ------------
for wk, w in _WIDTHS.items():
    b = square_bump(0.9 * GAP, w)
    ps = p_split(b.xs, b.xe)
    n_mem = [("D_N", i) for i in b.members]
    _add(f"f_{wk}_split_plain", "F", ps, b.n, [])
    _add(f"f_{wk}_split_n_pattern", "F", ps, b.n, _n_only(b))
    _add(
        f"f_{wk}_no_coupled_baseline",
        "F",
        p_straight(),
        b.n,
        _n_only(b, base_line_coupled=None),
    )
    _add(
        f"f_{wk}_partner_span_same_pattern",
        "F",
        ps,
        b.n,
        [Group(_pattern(b), [*n_mem, ("D_P", 1)])],
    )
    own = _pattern(
        b,
        net_name="D_P",
        base_line=((b.xs, Y_P), (b.xe, Y_P)),
        base_line_coupled=((b.xs, Y_N), (b.xe, Y_N)),
    )
    _add(
        f"f_{wk}_partner_span_own_pattern",
        "F",
        ps,
        b.n,
        [*_n_only(b), Group(own, [("D_P", 1)])],
    )
    _add(
        f"f_{wk}_mode_diff_pair_both",
        "F",
        ps,
        b.n,
        [Group(_pattern(b, tuning_mode="diff_pair"), [*n_mem, ("D_P", 1)])],
    )

# -- Group G: membership is a blanket exemption, whatever the copper's geometry ------
_bg = square_bump(0.9 * GAP, LONG)
_add(
    "g_whole_partner_leg_member",
    "G",
    p_straight(),
    _bg.n,
    [Group(_pattern(_bg), [*(("D_N", i) for i in _bg.members), ("D_P", 0)])],
)
# P detours 2 mm away from N over x 104..112 (genuinely uncoupled copper): plain, and
# with the detour as a pattern on P.  N is straight.
_detour = _path([(X0, Y_P), (104, Y_P), (106, Y_P - 2), (110, Y_P - 2), (112, Y_P), (X1, Y_P)], 1)
_bd = Bump(_path([(X0, Y_N), (X1, Y_N)], 2), [], 104.0, 112.0, 2.0)
_add("g_partner_detour_plain", "G", _detour, _bd.n, [])
_add(
    "g_partner_detour_member",
    "G",
    _detour,
    _bd.n,
    [
        Group(
            _pattern(
                _bd,
                net_name="D_P",
                base_line=((104, Y_P), (112, Y_P)),
                base_line_coupled=((104, Y_N), (112, Y_N)),
                initial_side="left",
            ),
            [("D_P", 1), ("D_P", 2), ("D_P", 3)],
        )
    ],
)


def write_probes(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, probe in PROBES.items():
        (directory / f"{name}.kicad_pcb").write_text(
            board_text(name, probe.p, probe.n, probe.patterns)
        )


# ---------------------------------------------------------------------------
# kicad-cli
# ---------------------------------------------------------------------------


def kicad_report(cli: str, pcb: Path) -> list[dict]:
    report = pcb.with_suffix(".drc.json")
    proc = subprocess.run(
        [cli, "pcb", "drc", "--format", "json", "--severity-all", "-o", str(report), str(pcb)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    # A killed kicad-cli reads as a clean report (#6724): never accept one.
    assert proc.returncode in (0, 5), proc.stderr
    return json.loads(report.read_text()).get("violations", [])


def kicad_uncoupled(cli: str, pcb: Path) -> float:
    figures = [
        float(re.search(r"actual ([0-9.]+) mm", v["description"]).group(1))
        for v in kicad_report(cli, pcb)
        if v["type"] == "diff_pair_uncoupled_length_too_long"
    ]
    return max(figures, default=0.0)


def work_copy(name: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    work = directory / f"{name}.kicad_pcb"
    shutil.copyfile(PROBE_DIR / f"{name}.kicad_pcb", work)
    work.with_suffix(".kicad_dru").write_text(RULES)
    work.with_suffix(".kicad_pro").write_text(
        json.dumps({"meta": {"filename": f"{name}.kicad_pro", "version": 3}})
    )
    return work


@pytest.fixture(scope="module")
def pinned_cli(tmp_path_factory):
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

    root = tmp_path_factory.mktemp("partner_7632")
    try:
        with evidence_toolchain([root]) as toolchain:
            yield str(toolchain.kicad_cli), root
    except KiCadEnvironmentError as exc:
        pytest.skip(f"pinned kicad-cli unavailable: {exc}")


@pytest.mark.parametrize("name", sorted(PROBES))
def test_committed_board_is_the_probe(name) -> None:
    """The committed board is exactly what the table builds (regenerate on edit)."""
    probe = PROBES[name]
    committed = (PROBE_DIR / f"{name}.kicad_pcb").read_text()
    assert committed == board_text(name, probe.p, probe.n, probe.patterns)


@pytest.mark.parametrize("name", sorted(PROBES))
def test_kicad_figure(name, pinned_cli) -> None:
    """Pinned kicad-cli 10.0.6 reads the recorded figure on the committed board."""
    cli, root = pinned_cli
    work = work_copy(name, root)
    assert kicad_uncoupled(cli, work) == pytest.approx(PROBES[name].kicad_mm, abs=1e-3)


@pytest.mark.parametrize("name", sorted(PROBES))
def test_kct_matches(name) -> None:
    """``kct check``'s measure reads the figure kicad-cli does (no kicad-cli needed)."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.diffpair_uncoupled_length import measure_pair

    pcb = PCB.load(str(PROBE_DIR / f"{name}.kicad_pcb"))
    ids = {n.name: n.number for n in pcb.nets.values() if n.name}
    measured = measure_pair(pcb, ids["D_P"], ids["D_N"], "D_P", "D_N").uncoupled_mm
    assert measured == pytest.approx(PROBES[name].kicad_mm, abs=1e-3)


#: The emissions that take the partner span out (group F).
PARTNER_EXCLUDED = [n for n in sorted(PROBES) if n.startswith("f_long_") and KICAD[n][0] == 0.0]


@pytest.mark.parametrize("name", PARTNER_EXCLUDED)
def test_partner_exclusion_survives_a_kicad_resave(name, pinned_cli) -> None:
    """``kicad-cli pcb upgrade`` keeps every pattern's members, and the figure stays 0."""
    from kicad_tools.router.tuning_pattern import read_tuning_patterns

    cli, root = pinned_cli
    work = work_copy(name, root / "resave")
    before = {p.uuid: sorted(p.members) for p in read_tuning_patterns(work.read_text())}
    subprocess.run([cli, "pcb", "upgrade", "--force", str(work)], check=True, capture_output=True)
    after = {p.uuid: sorted(p.members) for p in read_tuning_patterns(work.read_text())}
    assert after == before
    assert kicad_uncoupled(cli, work) == pytest.approx(0.0, abs=1e-3)


#: Skew rule added to the uncoupled one: membership must not take copper out of length.
SKEW_RULES = RULES.replace(
    "(constraint diff_pair_uncoupled (max 0.01mm)))",
    "(constraint diff_pair_uncoupled (max 0.01mm))\n  (constraint skew (max 0.001mm)))",
)


@pytest.mark.parametrize(
    "name",
    [
        "a_square_h09_long_plain",
        "b_square_h09_long_pattern",
        "f_long_partner_span_same_pattern",
        "f_long_partner_span_own_pattern",
    ],
)
def test_members_still_count_in_skew(name, pinned_cli) -> None:
    """KiCad's skew figure is the same (N 20.45 vs P 20.0 mm) whoever owns the copper."""
    cli, root = pinned_cli
    work = work_copy(name, root / "skew")
    work.with_suffix(".kicad_dru").write_text(SKEW_RULES)
    [skew] = [v for v in kicad_report(cli, work) if v["type"] == "skew_out_of_range"]
    assert "actual -0.4500 mm" in skew["description"]
    assert "target net length 20.4500 mm (from D_N); actual 20.0000 mm" in skew["description"]


if __name__ == "__main__":
    write_probes(Path(sys.argv[1]) if len(sys.argv) > 1 else PROBE_DIR)
