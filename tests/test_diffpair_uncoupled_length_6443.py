"""Issue #6443 piece (b): uncoupled-length rule, native pair rules, continuity demoted.

The kicad-cli tests pin how KiCad's ``diff_pair_uncoupled`` measures (kicad-cli
10.0.6, synthetic two-net boards) and check that kct's figure is the same.
They skip when kicad-cli is not installed.
"""

from __future__ import annotations

import json
import math
import subprocess
import uuid
from pathlib import Path

import pytest

from kicad_tools.manufacturers.netclass_dru import (
    DRU_PAIR_GAP_BLOCK_BEGIN,
    PairLimits,
    merge_pair_gap_block,
    pair_limit_rules,
    render_pair_limit_rules,
)
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds
from kicad_tools.validate.rules.diffpair_uncoupled_length import (
    DiffPairUncoupledLengthRule,
    measure_pair,
    measure_uncoupled,
    uncoupled_limit,
)

REPO = Path(__file__).resolve().parents[1]

_HDR = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
\t(setup (pad_to_mask_clearance 0))
"""


def seg(a, b, net, layer="F.Cu", width=0.2):
    return (
        f"\t(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "{layer}") (net "{net}") (uuid "{uuid.uuid4()}"))\n'
    )


def via(at, net):
    return (
        f'\t(via (at {at[0]} {at[1]}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        f'(net "{net}") (uuid "{uuid.uuid4()}"))\n'
    )


def write_board(path: Path, items: list[str], dru: str = "") -> Path:
    edge = (
        "\t(gr_rect (start -5 -10) (end 60 20) (stroke (width 0.1) (type default)) "
        f'(fill no) (layer "Edge.Cuts") (uuid "{uuid.uuid4()}"))\n'
    )
    path.write_text(_HDR + "".join(items) + edge + ")\n")
    path.with_suffix(".kicad_pro").write_text(
        '{"board":{"design_settings":{"rules":{"min_clearance":0.0}}},"meta":{"version":3}}'
    )
    path.with_suffix(".kicad_dru").write_text("(version 1)\n" + dru)
    return path


def _kicad_cli() -> Path:
    from kicad_tools.cli.runner import find_kicad_cli

    exe = find_kicad_cli()
    if exe is None:
        pytest.skip("kicad-cli not installed")
    return exe


def kicad_report(exe: Path, pcb: Path) -> list[dict]:
    out = pcb.with_suffix(".drc.json")
    subprocess.run(
        [str(exe), "pcb", "drc", "--format", "json", "--severity-all", "-o", str(out), str(pcb)],
        check=False,
        capture_output=True,
        timeout=300,
    )
    return json.loads(out.read_text()).get("violations", [])


def kicad_uncoupled(exe: Path, pcb: Path) -> float:
    """KiCad's own figure: reported as ``actual`` when the maximum is 0.01 mm."""
    rule = (
        '(rule "u"\n (condition "A.inDiffPair(\'T_\')")\n'
        " (constraint diff_pair_uncoupled (max 0.01mm)))\n"
    )
    pcb.with_suffix(".kicad_dru").write_text("(version 1)\n" + rule)
    for v in kicad_report(exe, pcb):
        if v["type"] == "diff_pair_uncoupled_length_too_long":
            return float(v["description"].rsplit("actual", 1)[1].split("mm")[0])
    return 0.0


def kct_uncoupled(pcb_path: Path) -> float:
    pcb = PCB.load(str(pcb_path))
    ids = {n.name: n.number for n in pcb.nets.values()}
    return measure_pair(pcb, ids["T_P"], ids["T_N"], "T_P", "T_N").uncoupled_mm


P20 = seg((0, 0), (20, 0), "T_P")


def _angled(deg: float) -> str:
    r = math.radians(deg)
    return seg((0, 0.4), (round(20 * math.cos(r), 6), round(0.4 + 20 * math.sin(r), 6)), "T_N")


#: name -> (copper, KiCad's figure in mm, what it pins).  KiCad's figures were
#: read off kicad-cli 10.0.6; the test re-reads them and requires kct to match.
PINNED = {
    "coupled": ([P20, seg((0, 0.4), (20, 0.4), "T_N")], 0.0, "fully coupled"),
    "partial_overlap": ([P20, seg((4, 0.4), (16, 0.4), "T_N")], 8.0, "projection overlap"),
    "per_net_not_summed": (
        [P20, seg((4, 0.4), (16, 0.4), "T_N"), seg((30, 5), (34, 5), "T_N")],
        8.0,
        "the larger net, not P + N (12)",
    ),
    "both_sides_extra": (
        [
            P20,
            seg((4, 0.4), (16, 0.4), "T_N"),
            seg((30, 5), (34, 5), "T_N"),
            seg((30, 8), (33, 8), "T_P"),
        ],
        8.0,
        "extras that run parallel are coupled: kct emits no diff_pair_gap maximum",
    ),
    "nonparallel_leg": (
        [P20, seg((0, 0.4), (10, 0.4), "T_N"), seg((10, 0.4), (20, 5), "T_N")],
        11.0073,
        "a diagonal leg is uncoupled",
    ),
    "angle_0_005_deg": ([P20, _angled(0.005)], 0.0, "within ~0.01 degrees is parallel"),
    "angle_0_02_deg": ([P20, _angled(0.02)], 20.0, "0.02 degrees is not parallel"),
    "anti_parallel": ([P20, seg((20, 0.4), (0, 0.4), "T_N")], 0.0, "direction is irrelevant"),
    "other_layer": ([P20, seg((0, 0.4), (20, 0.4), "T_N", "B.Cu")], 20.0, "layers must match"),
    "far_parallel": ([P20, seg((0, 10), (20, 10), "T_N")], 0.0, "no distance limit"),
    "split_partner": (
        [P20, seg((0, 0.4), (10, 0.4), "T_N"), seg((10, 0.4), (20, 0.4), "T_N")],
        0.0,
        "a partner split in two",
    ),
    "diagonal_stagger": (
        [seg((0, 0), (10, 10), "T_P"), seg((2.4, 1.6), (8.4, 7.6), "T_N")],
        5.6569,
        "projection along a diagonal",
    ),
    "aligned_vias": (
        [
            seg((0, 0), (10, 0), "T_P"),
            via((10, 0), "T_P"),
            seg((10, 0), (20, 0), "T_P", "B.Cu"),
            seg((0, 0.8), (10, 0.8), "T_N"),
            via((10, 0.8), "T_N"),
            seg((10, 0.8), (20, 0.8), "T_N", "B.Cu"),
        ],
        0.0,
        "via barrels are not length",
    ),
    "offset_vias": (
        [
            seg((0, 0), (8, 0), "T_P"),
            via((8, 0), "T_P"),
            seg((8, 0), (20, 0), "T_P", "B.Cu"),
            seg((0, 0.8), (10, 0.8), "T_N"),
            via((10, 0.8), "T_N"),
            seg((10, 0.8), (20, 0.8), "T_N", "B.Cu"),
        ],
        2.0,
        "only the unmatched track copper counts, not the barrel",
    ),
}


@pytest.mark.parametrize("case", sorted(PINNED))
def test_kct_measures_uncoupled_length_as_kicad_does(case, tmp_path) -> None:
    items, expected, _why = PINNED[case]
    pcb = write_board(tmp_path / "t.kicad_pcb", items)
    assert kct_uncoupled(pcb) == pytest.approx(expected, abs=1e-3)
    # ... and KiCad reports the same figure.
    assert kicad_uncoupled(_kicad_cli(), pcb) == pytest.approx(expected, abs=1e-3)


def test_measure_uncoupled_is_exact_for_collinear_overlap() -> None:
    own = [(0.0, 0.0, 20.0, 0.0, "F.Cu")]
    assert measure_uncoupled(own, [(4.0, 1.0, 16.0, 1.0, "F.Cu")]) == pytest.approx(8.0)
    assert measure_uncoupled(own, [(4.0, 1.0, 16.0, 1.0, "B.Cu")]) == pytest.approx(20.0)
    # overlapping partners are not double counted
    both = [(0.0, 1.0, 12.0, 1.0, "F.Cu"), (8.0, 2.0, 20.0, 2.0, "F.Cu")]
    assert measure_uncoupled(own, both) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# The rule: limits, severities, unrouted pairs, vias
# ---------------------------------------------------------------------------


def _pcb(tmp_path, items):
    return PCB.load(str(write_board(tmp_path / "r.kicad_pcb", items)))


def _classes(**fields) -> dict[str, NetClassRouting]:
    p = NetClassRouting(name="HS", priority=2, coupled_routing=True, **fields)
    return {"T_P": p, "T_N": p}


def _run(pcb, ncm, **kw):
    ids = {n.name: n.number for n in pcb.nets.values()}
    key = (ids["T_P"], ids["T_N"])
    rule = DiffPairUncoupledLengthRule(
        engaged_pairs={key},
        limits={key: resolve_pair_thresholds(ncm, "T_P", "T_N")},
        **kw,
    )
    return rule.check(pcb, None)  # type: ignore[arg-type]


LONG = [P20, seg((4, 0.4), (16, 0.4), "T_N")]  # 8 mm uncoupled


def test_cited_protocol_limit_is_an_error(tmp_path) -> None:
    # USB2_FS: 6.35 mm per terminal, no pads on the board -> one terminal.
    res = _run(_pcb(tmp_path, LONG), _classes(pair_protocol="USB2_FS"))
    [v] = [v for v in res.violations if v.rule_id == "diffpair_uncoupled_length"]
    assert v.severity == "error"
    assert v.actual_value == pytest.approx(8.0)
    assert v.required_value == pytest.approx(6.35)
    assert "protocol:USB2_FS" in v.message


def test_limit_scales_with_components_on_the_net(tmp_path) -> None:
    pcb = _pcb(tmp_path, LONG)
    t = resolve_pair_thresholds(_classes(pair_protocol="USB2_FS"), "T_P", "T_N")
    assert uncoupled_limit(t, 3)[0] == pytest.approx(19.05)
    assert uncoupled_limit(t, 0)[0] == pytest.approx(6.35)  # at least one terminal
    assert pcb  # board loads


def test_explicit_total_beats_the_protocol_and_is_an_error(tmp_path) -> None:
    ncm = _classes(pair_protocol="USB2_FS", max_uncoupled_total_mm=9.0)
    assert not _run(_pcb(tmp_path, LONG), ncm).violations
    ncm = _classes(pair_protocol="USB2_FS", max_uncoupled_total_mm=7.0)
    [v] = _run(_pcb(tmp_path, LONG), ncm).violations
    assert (v.severity, v.required_value) == ("error", 7.0)


def test_class_without_a_limit_is_not_judged(tmp_path) -> None:
    res = _run(_pcb(tmp_path, LONG), _classes())
    assert not res.violations


def test_uncited_limit_is_only_a_warning() -> None:
    from kicad_tools.router.pair_protocols import PROTOCOL_DEFAULTS, PairProtocol, Sourced

    t = resolve_pair_thresholds(_classes(pair_protocol="LVDS"), "T_P", "T_N")
    assert t.max_uncoupled_per_terminal_mm is None  # LVDS has no uncoupled value
    # Every uncoupled / via value in the table carries a severity that follows its tag.
    for proto, row in PROTOCOL_DEFAULTS.items():
        nc = NetClassRouting(name="X", priority=2, coupled_routing=True, pair_protocol=proto.name)
        th = resolve_pair_thresholds({"A_P": nc, "A_N": nc}, "A_P", "A_N")
        for fld, sev in (
            ("max_uncoupled_per_terminal_mm", th.uncoupled_severity),
            ("max_vias_per_net", th.vias_severity),
        ):
            val = getattr(row, fld)
            if isinstance(val, Sourced):
                assert sev == ("error" if val.cited else "warning"), (proto, fld)
    assert PairProtocol.USB2_FS in PROTOCOL_DEFAULTS


def test_uncited_table_value_never_produces_an_error(monkeypatch, tmp_path) -> None:
    from kicad_tools.router import pair_protocols as pp

    row = pp.PROTOCOL_DEFAULTS[pp.PairProtocol.USB2_FS]
    patched = pp.ProtocolDefaults(
        protocol=row.protocol,
        max_skew_mm=row.max_skew_mm,
        max_uncoupled_per_terminal_mm=pp.Sourced(6.35, pp.UNCITED),
        max_vias_per_net=pp.Sourced(0, pp.UNCITED),
    )
    monkeypatch.setitem(pp.PROTOCOL_DEFAULTS, pp.PairProtocol.USB2_FS, patched)
    items = [*LONG, via((3, 3), "T_P")]
    res = _run(_pcb(tmp_path, items), _classes(pair_protocol="USB2_FS"))
    findings = [v for v in res.violations if v.severity != "info"]
    assert findings
    assert not [v for v in findings if v.severity == "error" and "unequal" not in v.message]
    assert {v.severity for v in findings} == {"warning"}


def test_via_count_limit_and_unequal_vias(tmp_path) -> None:
    items = [
        seg((0, 0), (20, 0), "T_P"),
        seg((0, 0.4), (20, 0.4), "T_N"),
        *(via((i, 5), "T_P") for i in range(5)),
    ]
    res = _run(_pcb(tmp_path, items), _classes(pair_protocol="USB2_FS"))
    msgs = {(v.severity, v.message.split(":")[-1].strip()[:30]) for v in res.violations}
    over = [v for v in res.violations if "vias, limit 4" in v.message]
    assert len(over) == 1 and over[0].severity == "error" and over[0].nets == ("T_P",)
    assert [v for v in res.violations if "unequal via counts" in v.message][0].severity == "warning"
    assert msgs


def test_unrouted_pair_has_no_uncoupled_number(tmp_path) -> None:
    # N is on the board but carries no copper length (a zero-length stub).
    pcb = _pcb(tmp_path, [P20, seg((30, 5), (30, 5), "T_N")])
    res = _run(pcb, _classes(pair_protocol="USB2_FS"), emit_info=True)
    assert not res.violations
    assert res.rules_checked == 0


def test_kct_only_note_for_names_kicad_does_not_pair(tmp_path) -> None:
    pcb = _pcb(tmp_path, LONG)
    ids = {n.name: n.number for n in pcb.nets.values()}
    key = (ids["T_P"], ids["T_N"])
    rule = DiffPairUncoupledLengthRule(
        engaged_pairs={key}, limits={}, kct_only={key: "KiCad does not recognise T_P/T_N"}
    )
    [v] = rule.check(pcb, None).violations  # type: ignore[arg-type]
    assert v.severity == "info" and "kct only" in v.message


def test_passing_pair_reports_a_measurement_when_asked(tmp_path) -> None:
    pcb = _pcb(tmp_path, [P20, seg((0, 0.4), (20, 0.4), "T_N")])
    res = _run(pcb, _classes(pair_protocol="USB2_FS"), emit_info=True)
    [v] = [v for v in res.violations if "uncoupled length" in v.message]
    assert v.severity == "info" and v.actual_value == 0.0


def test_continuity_is_info_only() -> None:
    from kicad_tools.validate.rules.diffpair_routing_continuity import (
        DiffPairRoutingContinuityRule,
    )

    class _P:  # a pair that never couples
        pass

    rule = DiffPairRoutingContinuityRule(engaged_pairs={(1, 2)})
    v = rule._make_violation(1, 2, "A", "B", 0.1, 0.7)
    assert v.severity == "info"


def test_coupled_continuity_threshold_is_deprecated() -> None:
    with pytest.warns(DeprecationWarning, match="coupled_continuity_threshold"):
        NetClassRouting(name="X", priority=2, coupled_continuity_threshold=0.5)


def test_board03_no_longer_overrides_continuity() -> None:
    text = (REPO / "boards/03-usb-joystick/generate_design.py").read_text()
    assert "coupled_continuity_threshold=0.25" not in text
    sidecar = json.loads((REPO / "boards/03-usb-joystick/output/net_class_map.json").read_text())
    assert all(c.get("coupled_continuity_threshold") is None for c in sidecar.values())


# ---------------------------------------------------------------------------
# Native rules in the .kicad_dru
# ---------------------------------------------------------------------------

USB = PairLimits("USB_D+", "USB_D-", 19.05, "error", 2.5, "error", 4, "error")


def test_recognised_pair_gets_all_three_native_rules() -> None:
    uncoupled, skew, vias = render_pair_limit_rules(USB)
    assert "(condition \"A.inDiffPair('USB_D')\")" in uncoupled
    assert "(constraint diff_pair_uncoupled (max 19.05mm))" in uncoupled
    assert "(constraint skew (max 2.5mm) (within_diff_pairs))" in skew
    assert "(constraint via_count (max 4))" in vias
    assert "(severity" not in uncoupled  # error is KiCad's default


def test_uncited_limit_is_written_as_a_warning() -> None:
    warn = PairLimits("USB_D+", "USB_D-", 19.05, "warning", None, "error", None, "error")
    [rule] = render_pair_limit_rules(warn)
    assert "(severity warning)" in rule


def test_unrecognised_pair_gets_skew_and_via_count_by_net_name_only() -> None:
    odd = PairLimits("X_POS", "X_NEG", 6.0, "error", 1.0, "error", 3, "error")
    skew, vias = render_pair_limit_rules(odd)  # no diff_pair_uncoupled rule
    for rule in (skew, vias):
        assert "A.NetName == 'X_POS' || A.NetName == 'X_NEG'" in rule
    assert "within_diff_pairs" not in skew
    assert "diff_pair_uncoupled" not in skew + vias


def test_limit_rules_are_sorted_oriented_and_deduplicated() -> None:
    rules = pair_limit_rules(
        [
            PairLimits("B_N", "B_P", 5.0, "error", 1.0, "error", None, "error"),
            PairLimits("A_P", "A_N", 5.0, "error", 1.0, "error", None, "error"),
            PairLimits("A_P", "A_N", 4.0, "error", 2.0, "error", None, "error"),
            PairLimits("Z_P", "Z_N"),  # nothing to say
        ]
    )
    assert [(r.p, r.n) for r in rules] == [("A_P", "A_N"), ("B_P", "B_N")]
    assert rules[0].uncoupled_mm == 4.0 and rules[0].skew_mm == 1.0


def test_block_merge_is_idempotent_and_removable() -> None:
    base = "(version 1)\n"
    merged = merge_pair_gap_block(base, [], [USB])
    assert merged and DRU_PAIR_GAP_BLOCK_BEGIN in merged
    assert merge_pair_gap_block(merged, [], [USB]) == merged
    assert DRU_PAIR_GAP_BLOCK_BEGIN not in (merge_pair_gap_block(merged, [], []) or "")


def test_committed_boards_carry_the_native_rules() -> None:
    b03 = (REPO / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_dru").read_text()
    assert "diff_pair_uncoupled (max 19.05mm)" in b03
    assert "diff_pair_uncoupled (max 12.7mm)" in b03
    assert "skew (max 2.5mm) (within_diff_pairs)" in b03
    assert "via_count (max 4)" in b03
    b06 = (REPO / "boards/06-diffpair-test/output/diffpair_test_routed.kicad_dru").read_text()
    assert "skew (max 0.1mm) (within_diff_pairs)" in b06


def test_native_rules_are_accepted_and_enforced_by_kicad_cli(tmp_path) -> None:
    exe = _kicad_cli()
    # P 20 mm / N 17 mm (skew 3), 8 mm uncoupled, 2 vias on P.
    items = [
        seg((0, 0), (20, 0), "T_P"),
        seg((4, 0.4), (16, 0.4), "T_N"),
        seg((30, 5), (35, 5), "T_N"),
        via((3, 5), "T_P"),
        via((6, 5), "T_P"),
    ]

    def run(limits: PairLimits) -> dict[str, str]:
        pcb = write_board(tmp_path / f"n{limits.skew_mm}.kicad_pcb", items)
        dru = merge_pair_gap_block("(version 1)\n", [], [limits]) or ""
        pcb.with_suffix(".kicad_dru").write_text(dru)
        return {
            v["type"]: v["severity"]
            for v in kicad_report(exe, pcb)
            if v["type"]
            in ("skew_out_of_range", "diff_pair_uncoupled_length_too_long", "too_many_vias")
        }

    # An honest limit passes; a breached one is reported at its severity.
    assert run(PairLimits("T_P", "T_N", 20.0, "error", 4.0, "error", 4, "error")) == {}
    got = run(PairLimits("T_P", "T_N", 5.0, "error", 2.0, "warning", 1, "error"))
    assert got == {
        "diff_pair_uncoupled_length_too_long": "error",
        "skew_out_of_range": "warning",
        "too_many_vias": "error",
    }


def test_unrecognised_pair_skew_and_vias_are_checked_by_both_engines(tmp_path) -> None:
    exe = _kicad_cli()
    items = [
        seg((0, 0), (20, 0), "X_POS"),
        seg((0, 0.4), (17, 0.4), "X_NEG"),
        via((3, 5), "X_POS"),
        via((6, 5), "X_POS"),
    ]
    pcb = write_board(tmp_path / "x.kicad_pcb", items)
    limits = PairLimits("X_POS", "X_NEG", 1.0, "error", 2.0, "error", 1, "error")
    pcb.with_suffix(".kicad_dru").write_text(
        merge_pair_gap_block("(version 1)\n", [], [limits]) or ""
    )
    types = {v["type"] for v in kicad_report(exe, pcb)}
    assert {"skew_out_of_range", "too_many_vias"} <= types
    assert "diff_pair_uncoupled_length_too_long" not in types  # kct-only for this name

    # kct agrees on skew and vias for the same copper, and says uncoupled is kct-only.
    from kicad_tools.validate.checker import DRCChecker

    nc = NetClassRouting(
        name="HS",
        priority=2,
        coupled_routing=True,
        skew_tolerance_mm=2.0,
        max_vias_per_net=1,
        max_uncoupled_total_mm=1.0,
    )
    ncm = {
        "X_POS": NetClassRouting(**{**nc.__dict__, "diffpair_partner": "X_NEG"}),
        "X_NEG": NetClassRouting(**{**nc.__dict__, "diffpair_partner": "X_POS"}),
    }
    pcb_obj = PCB.load(str(pcb))
    checker = DRCChecker(pcb_obj, manufacturer="jlcpcb", layers=2, net_class_map=ncm)
    skew = checker.check_diffpair_length_skew()
    assert [v.severity for v in skew.violations] == ["error"]
    unc = checker.check_diffpair_uncoupled_length()
    msgs = " | ".join(v.message for v in unc.violations)
    assert "vias, limit 1" in msgs
    assert "KiCad does not recognise" in msgs and "kct only" in msgs


# ---------------------------------------------------------------------------
# Agreement on the committed boards
# ---------------------------------------------------------------------------

COMMITTED = [
    (
        "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb",
        "boards/03-usb-joystick/output/net_class_map.json",
    ),
    (
        "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb",
        "boards/06-diffpair-test/output/net_class_map.json",
    ),
]


@pytest.mark.parametrize(("pcb_rel", "ncm_rel"), COMMITTED)
def test_committed_boards_are_clean_in_both_engines_for_every_pair_rule(
    pcb_rel, ncm_rel, tmp_path
) -> None:
    exe = _kicad_cli()
    import shutil

    from kicad_tools.router.rules import net_class_map_from_dict
    from kicad_tools.validate.checker import DRCChecker

    src = REPO / pcb_rel
    work = tmp_path / src.name
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copyfile(src.with_suffix(suffix), work.with_suffix(suffix))
    kicad = [
        v
        for v in kicad_report(exe, work)
        if v["type"]
        in (
            "skew_out_of_range",
            "diff_pair_uncoupled_length_too_long",
            "too_many_vias",
            "diff_pair_gap_out_of_range",
        )
    ]
    assert kicad == []

    ncm = net_class_map_from_dict(json.loads((REPO / ncm_rel).read_text()))
    checker = DRCChecker(PCB.load(str(work)), manufacturer="jlcpcb", layers=4, net_class_map=ncm)
    for result in (checker.check_diffpair_length_skew(), checker.check_diffpair_uncoupled_length()):
        assert [v for v in result.violations if v.severity in ("error", "warning")] == []


def test_known_divergence_kct_never_exceeds_kicad_on_committed_copper(tmp_path) -> None:
    """kct's uncoupled figure is a lower bound of KiCad's on the committed boards.

    KiCad pairs primitives in a way kct does not reproduce (it will not couple
    two parallel runs that have a nearer non-parallel neighbour), so its figure
    is larger: 10.405 / 0.964 mm for board 03's USB_D / USB_MCU_D and 1.541 mm
    for each board 06 LVDS pair, against kct's 8.130 / 0.849 / 1.216 mm.  The
    documented gap (docs/guides/diff-pairs/08-uncoupled-length.md) is bounded here, so
    a change that widens it is noticed.
    """
    exe = _kicad_cli()
    import shutil

    expected = {
        "USB_D": (10.4052, 8.1302),
        "USB_MCU_D": (0.9635, 0.8485),
        "LVDS1": (1.5412, 1.2162),
    }
    for pcb_rel, _ in COMMITTED:
        src = REPO / pcb_rel
        work = tmp_path / src.name
        for suffix in (".kicad_pcb", ".kicad_pro"):
            shutil.copyfile(src.with_suffix(suffix), work.with_suffix(suffix))
        work.with_suffix(".kicad_dru").write_text(
            '(version 1)\n(rule "u"\n (condition "A.inDiffPair(\'*\')")\n'
            " (constraint diff_pair_uncoupled (max 0.01mm)))\n"
        )
        seen = {}
        for v in kicad_report(exe, work):
            if v["type"] != "diff_pair_uncoupled_length_too_long":
                continue
            actual = float(v["description"].rsplit("actual", 1)[1].split("mm")[0])
            nets = {
                i["description"].rsplit("[", 1)[-1].split("]", 1)[0] for i in v.get("items", [])
            }
            seen[tuple(sorted(nets))] = actual
        pcb = PCB.load(str(work))
        ids = {n.name: n.number for n in pcb.nets.values()}
        for base, (kicad_mm, kct_mm) in expected.items():
            for p, n in ((f"{base}+", f"{base}-"), (f"{base}_P", f"{base}_N")):
                if p not in ids or n not in ids:
                    continue
                mine = measure_pair(pcb, ids[p], ids[n], p, n).uncoupled_mm
                assert mine == pytest.approx(kct_mm, abs=1e-3)
                theirs = seen[tuple(sorted((p, n)))]
                assert theirs == pytest.approx(kicad_mm, abs=1e-3)
                assert mine <= theirs <= mine + 3.0


def test_limit_only_block_has_no_gap_header() -> None:
    merged = merge_pair_gap_block("(version 1)\n", [], [USB])
    assert merged and "The gap the router laid" not in merged
    assert "Uncoupled length, skew and via count" in merged
    with_gap = merge_pair_gap_block("(version 1)\n", [("A_P", "A_N", 0.2)], [USB])
    assert with_gap and "The gap the router laid" in with_gap


def test_measurement_summary_labels_via_findings_vias(tmp_path, capsys) -> None:
    from kicad_tools.cli.check_cmd import _print_measurement_summary

    items = [
        seg((0, 0), (20, 0), "T_P"),
        seg((0, 0.4), (20, 0.4), "T_N"),
        *(via((i, 5), "T_P") for i in range(5)),
    ]
    res = _run(_pcb(tmp_path, items), _classes(pair_protocol="USB2_FS"))
    _print_measurement_summary(res.violations)
    rows = [ln.split() for ln in capsys.readouterr().out.splitlines() if "T_N" in ln]
    assert len([r for r in rows if "vias" in r]) == 2  # cap breach + unequal
    assert not [r for r in rows if "uncoupled" in r]
