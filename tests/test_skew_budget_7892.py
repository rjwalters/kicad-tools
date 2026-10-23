"""Derived skew budgets in ps and the time-domain ``kct check`` (Issue #7892).

Step 1 of the operator ruling on #7883 (issuecomment-6370363844):

* the formula reproduces ``docs/research/diffpair-phase-skew-tolerance.md``'s
  tables (to the 0.1 ps they print, and within 1 % where that is finer);
* protocol identification is deterministic, severity follows Q-c;
* a length-matched pair split across layers of different delay fails in ps;
* an outer-layer-only pair keeps its verdict.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.physics import skew_budget as sb
from kicad_tools.physics.stackup import Stackup
from kicad_tools.router.pair_protocols import PairProtocol
from kicad_tools.router.rules import NetClassRouting

REPO = Path(__file__).resolve().parent.parent


def _approx_note(value: float) -> object:
    """The note prints budgets to 0.1 ps: match within 1 % or half that digit."""
    return pytest.approx(value, rel=0.01, abs=0.05)


# ---------------------------------------------------------------------------
# Formula
# ---------------------------------------------------------------------------


def test_minus_30_db_is_3_62_degrees_and_ui_over_50():
    assert math.degrees(sb.DPHI_MAX_RAD) == pytest.approx(3.624, abs=1e-3)
    assert sb.scd21_db(sb.DPHI_MAX_RAD) == pytest.approx(-30.0, abs=1e-9)
    # At Nyquist f = 1/(2 UI): dt = dphi * UI / pi = UI / 50 (within 1 %).
    for rate in (1.0, 2.5, 5.0, 8.0):
        ui_ps = 1e3 / rate
        assert sb.phase_skew_budget_ps(rate) == pytest.approx(ui_ps / 50, rel=0.01)


def test_knee_raises_the_evaluation_frequency_only_above_nyquist():
    assert pytest.approx(0.3497, abs=1e-4) == sb.KNEE_COEFF
    f, basis = sb.evaluation_frequency_ghz(0.48, 500.0)
    assert basis == "knee" and f == pytest.approx(0.699, abs=1e-3)
    f, basis = sb.evaluation_frequency_ghz(5.0, None)
    assert basis == "Nyquist" and f == 2.5
    # An edge floor slower than Nyquist does not lower f_eval.
    f, basis = sb.evaluation_frequency_ghz(5.0, 4000.0)
    assert basis == "Nyquist" and f == 2.5
    with pytest.raises(ValueError):
        sb.evaluation_frequency_ghz(0.0)


#: Note §5.1 "-30 dB: dphi / dt" column, ps.
NOTE_PROTOCOL_TABLE_PS = {
    "USB2_FS": 115.2,
    "USB2_HS": 14.4,
    "USB3_GEN1": 4.0,
    "USB3_GEN2": 2.0,
    "PCIE_GEN1": 8.1,
    "PCIE_GEN2": 4.0,
    "PCIE_GEN3": 2.5,
    "MIPI_DPHY": 8.1,  # 2.5 Gb/s row
    "HDMI_14": 5.9,
    "HDMI_20": 3.4,
    "LVDS": 17.3,
}


@pytest.mark.parametrize(("key", "note_ps"), sorted(NOTE_PROTOCOL_TABLE_PS.items()))
def test_protocol_table_matches_the_note(key: str, note_ps: float):
    point = sb.OPERATING_POINTS[key]
    assert sb.phase_skew_budget_ps(point.rate_gbps, point.edge_floor_ps) == _approx_note(note_ps)


def test_mipi_at_one_gbps_matches_the_note():
    assert sb.phase_skew_budget_ps(1.0) == _approx_note(20.1)


def test_every_operating_point_cites_a_note_source():
    for point in sb.OPERATING_POINTS.values():
        assert point.rate_source
        assert ("[" in point.sources) or "rate does not set" in point.rate_source, point
        if point.edge_floor_ps is not None:
            assert point.edge_source.startswith("[")
        if point.timing_limit_ps is not None:
            assert point.timing_source.startswith("[X1]")


def test_protocol_max_points_name_real_protocols():
    names = {p.name for p in PairProtocol}
    assert set(sb.PROTOCOL_MAX_POINT) <= names
    for protocol, _words in sb.NAME_RULES:
        assert protocol in names
    # The AC's protocol list is covered.
    assert {"USB2_FS", "USB2_HS", "USB3_SS", "PCIE_G1", "PCIE_G2_PLUS", "MIPI_DPHY"} <= set(
        sb.PROTOCOL_MAX_POINT
    )
    assert {"HDMI_TMDS", "LVDS", "DDR_DQS_CK"} <= set(sb.PROTOCOL_MAX_POINT)


def test_ddr_strobes_keep_the_ug583_timing_limit():
    budget = sb.skew_budget("DDR_DQS_CK")
    assert budget is not None
    assert budget.budget_ps == 2.0 and budget.basis == "timing"
    # The rate does not move it.
    declared = sb.skew_budget("DDR_DQS_CK", 6.4)
    assert declared is not None and declared.budget_ps == 2.0


# ---------------------------------------------------------------------------
# Identification and severity
# ---------------------------------------------------------------------------


def _nc(name: str, **kw) -> NetClassRouting:
    return NetClassRouting(name=name, coupled_routing=True, **kw)


def test_identification_order_is_class_field_then_class_name_then_pair_name():
    assert sb.identify_protocol(_nc("MIPI", pair_protocol="USB3_SS"), "X+", "X-") == (
        "USB3_SS",
        "class field",
    )
    assert sb.identify_protocol(_nc("HDMI_TMDS_LANES"), "MIPI_CLK_P", "MIPI_CLK_N") == (
        "HDMI_TMDS",
        "class name",
    )
    assert sb.identify_protocol(_nc("Default"), "USB3_TX1+", "USB3_TX1-") == (
        "USB3_SS",
        "pair name",
    )
    assert sb.identify_protocol(None, "FOO_P", "FOO_N") == (None, "none")


@pytest.mark.parametrize(
    ("name", "protocol"),
    [
        ("DDR_DQS", "DDR_DQS_CK"),
        ("DQS0_P", "DDR_DQS_CK"),
        ("MIPI_CSI_LANES", "MIPI_DPHY"),
        ("HDMI_TMDS_LANES", "HDMI_TMDS"),
        ("TMDS_D0_P", "HDMI_TMDS"),
        ("USB3_TX1+", "USB3_SS"),  # the USB 3 rule precedes the USB 2 rule
        ("USB_D+", "USB2_HS"),
        ("PCIE_TX+", "PCIE_G2_PLUS"),
        ("LVDS1_P", "LVDS"),
        ("HighSpeed", None),
        ("CLK_P", None),
    ],
)
def test_name_rules(name: str, protocol: str | None):
    assert sb.protocol_from_name(name) == protocol


def test_protocol_max_rate_is_a_warning_and_a_declared_rate_an_error():
    default = sb.resolve_skew_budget(_nc("USB3", pair_protocol="USB3_SS"), "A+", "A-")
    assert default is not None
    assert default.severity == "warning" and default.rate_gbps == 5.0
    assert default.budget_ps == pytest.approx(4.03, abs=0.01)
    declared = sb.resolve_skew_budget(
        _nc("USB3", pair_protocol="USB3_SS", data_rate_gbps=10.0), "A+", "A-"
    )
    assert declared is not None
    assert declared.severity == "error" and declared.budget_ps == pytest.approx(2.01, abs=0.01)
    assert "declared rate" in declared.provenance


def test_rate_without_protocol_derives_at_nyquist_as_an_error():
    budget = sb.resolve_skew_budget(_nc("Fast", data_rate_gbps=2.5), "FOO_P", "FOO_N")
    assert budget is not None
    assert budget.severity == "error" and budget.basis == "Nyquist"
    assert budget.budget_ps == _approx_note(8.1)


def test_ddr_severity_follows_how_the_protocol_was_identified():
    explicit = sb.resolve_skew_budget(_nc("S", pair_protocol="DDR_DQS_CK"), "DQS_P", "DQS_N")
    inferred = sb.resolve_skew_budget(_nc("DDR_DQS"), "DQS_P", "DQS_N")
    assert explicit is not None and explicit.severity == "error"
    assert inferred is not None and inferred.severity == "warning"
    assert "by class name" in inferred.provenance


def test_no_protocol_and_no_rate_gives_no_budget():
    assert sb.resolve_skew_budget(_nc("Default"), "FOO_P", "FOO_N") is None
    assert sb.resolve_skew_budget(_nc("S", pair_protocol="SATA"), "A+", "A-") is None


def test_data_rate_field_is_validated_and_round_trips():
    with pytest.raises(ValueError, match="data_rate_gbps"):
        NetClassRouting(name="x", data_rate_gbps=0.0)
    nc = NetClassRouting(name="x", data_rate_gbps=5.0)
    assert NetClassRouting.from_dict(nc.to_dict()).data_rate_gbps == 5.0


# ---------------------------------------------------------------------------
# Per-layer delay
# ---------------------------------------------------------------------------


def test_layer_delays_match_the_note_on_the_legacy_four_layer_model():
    delays = sb.layer_delays(Stackup.jlcpcb_4layer(), 0.2)
    assert delays.of("F.Cu") == pytest.approx(5.57, abs=0.01)
    inner = max(delays.of("In1.Cu"), delays.of("In2.Cu"))
    assert inner == pytest.approx(6.94, abs=0.01)
    assert delays.via_ps_per_mm == pytest.approx(inner)
    assert delays.slowest == pytest.approx(inner)
    # Note §5.1: USB3 Gen1 at -30 dB is 0.58 mm at the slowest layer.
    assert delays.mm_equivalent(sb.phase_skew_budget_ps(5.0)) == pytest.approx(0.58, abs=0.005)
    # An unknown layer is charged the slowest delay.
    assert delays.of("In9.Cu") == delays.slowest


def test_two_layer_board_charges_vias_at_its_dielectric():
    delays = sb.layer_delays(Stackup.default_2layer(), 0.2)
    assert set(delays.ps_per_mm) == {"F.Cu", "B.Cu"}
    assert delays.via_ps_per_mm > delays.of("F.Cu")


# ---------------------------------------------------------------------------
# kct check: the split-layer fixture and an outer-only pair
# ---------------------------------------------------------------------------

_PCB_HEAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "USB3_TX1+")
  (net 2 "USB3_TX1-")
"""


def _seg(x1, y1, x2, y2, layer, net, n):
    return (
        f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.275) (layer "{layer}") '
        f'(net {net}) (uuid "00000000-0000-0000-0000-{n:012d}"))\n'
    )


def _via(x, y, net, n):
    return (
        f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {net}) '
        f'(uuid "00000000-0000-0000-0000-{n:012d}"))\n'
    )


def _board(tmp_path: Path, p_items: str, n_items: str) -> Path:
    path = tmp_path / "pair.kicad_pcb"
    path.write_text(_PCB_HEAD + p_items + n_items + ")\n")
    return path


def _ncm(**kw) -> dict[str, NetClassRouting]:
    nc = NetClassRouting(
        name="USB3",
        coupled_routing=True,
        trace_width=0.275,
        intra_pair_clearance=0.1,
        pair_protocol="USB3_SS",
        **kw,
    )
    return {"USB3_TX1+": nc, "USB3_TX1-": nc}


def _p_leg_split() -> str:
    """P: 10 mm F.Cu, down to In1.Cu for 20 mm, back up, 10 mm F.Cu."""
    return (
        _seg(110, 120, 120, 120, "F.Cu", 1, 1)
        + _via(120, 120, 1, 2)
        + _seg(120, 120, 140, 120, "In1.Cu", 1, 3)
        + _via(140, 120, 1, 4)
        + _seg(140, 120, 150, 120, "F.Cu", 1, 5)
    )


def _measure(path: Path, net_id: int) -> float:
    from kicad_tools.router.kicad_length import measure_pcb_net
    from kicad_tools.schema.pcb import PCB

    return measure_pcb_net(PCB.load(str(path)), net_id, num_copper_hint=4)


def _skew_findings(path: Path, ncm) -> list:
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    checker = DRCChecker(PCB.load(str(path)), layers=4, net_class_map=ncm)
    return [v for v in checker.check_diffpair_length_skew().violations if v.severity != "info"]


def _legacy_findings(path: Path, ncm) -> list:
    """The pre-#7892 length-only judgement of the same board."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_engagement import derive_engagement_state
    from kicad_tools.validate.diffpair_skew import derive_skew_limits
    from kicad_tools.validate.rules.diffpair_length_skew import DiffPairLengthSkewRule

    pcb = PCB.load(str(path))
    skew_data, limits = derive_skew_limits(pcb, ncm, num_copper_layers=4)
    engaged, _ = derive_engagement_state(pcb, ncm)
    rule = DiffPairLengthSkewRule(
        skew_data=skew_data,
        engaged_pairs=engaged,
        threshold_map={k: t.skew_mm for k, t in limits.items()},
        severity_map={k: t.skew_severity for k, t in limits.items()},
    )
    return [v for v in rule.check(pcb, None).violations if v.severity != "info"]


@pytest.fixture
def split_layer_board(tmp_path: Path) -> Path:
    """Length-matched to the micron, but the P leg runs 20 mm on In1.Cu."""
    probe = _board(tmp_path, _p_leg_split(), "")
    p_len = _measure(probe, 1)
    n_items = _seg(110, 121, round(110 + p_len, 6), 121, "F.Cu", 2, 6)
    path = _board(tmp_path, _p_leg_split(), n_items)
    assert abs(_measure(path, 1) - _measure(path, 2)) < 1e-3
    return path


def test_split_layer_pair_is_length_matched_but_fails_in_ps(split_layer_board: Path):
    ncm = _ncm(skew_tolerance_mm=0.5)
    assert _legacy_findings(split_layer_board, ncm) == []  # passes in mm
    findings = _skew_findings(split_layer_board, ncm)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "warning"  # protocol max rate (Q-c)
    assert "propagation-delay skew" in finding.message
    assert "budget 4.03 ps" in finding.message
    assert "mm at the slowest layer" in finding.message
    assert "kct-only" in finding.message
    assert finding.actual_value > finding.required_value
    # The skew is the extra inner-layer delay of the 20 mm on In1.Cu, plus
    # the two via barrels P carries (N has none).
    assert finding.actual_value > 20.0


def test_split_layer_pair_with_a_declared_rate_is_an_error(split_layer_board: Path):
    findings = _skew_findings(split_layer_board, _ncm(data_rate_gbps=5.0))
    assert [f.severity for f in findings] == ["error"]


@pytest.mark.parametrize(("skew_mm", "fails"), [(0.3, False), (1.0, True)])
def test_outer_layer_only_pair_keeps_its_verdict(tmp_path: Path, skew_mm: float, fails: bool):
    path = _board(
        tmp_path,
        _seg(110, 120, 140, 120, "F.Cu", 1, 1),
        _seg(110, 120.375, round(140 + skew_mm, 6), 120.375, "F.Cu", 2, 2),
    )
    ncm = _ncm(skew_tolerance_mm=0.5)
    legacy = _legacy_findings(path, ncm)
    new = _skew_findings(path, ncm)
    assert bool(legacy) is fails
    assert bool(new) is fails
    if fails:
        # The class millimetre cap still fails it as an error (until #7894),
        # and the time budget agrees.
        assert {f.severity for f in new} == {"error", "warning"}
        assert all("kct-only" not in f.message for f in new)


def test_kicad_dru_rule_is_the_tightest_length_form(split_layer_board: Path):
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_engagement import resolve_pair_skew_limits

    stackup = Stackup.from_pcb(PCB.load(str(split_layer_board)))
    # Class cap 0.5 mm is tighter than the budget's 0.58 mm: it is exported.
    capped = resolve_pair_skew_limits(
        _ncm(skew_tolerance_mm=0.5), "USB3_TX1+", "USB3_TX1-", stackup
    )
    assert capped.kicad_skew_mm == pytest.approx(0.5)
    assert capped.kicad_skew_severity == "error"
    # Without a cap the rule is the derived budget at the slowest layer.  The
    # Q16 table's 0.127 mm was the router's target, and so a floor, until
    # Issue #7893 switched the router to the budget; it is no longer one.
    bare = resolve_pair_skew_limits(_ncm(), "USB3_TX1+", "USB3_TX1-", stackup)
    assert bare.budget_mm_equivalent == pytest.approx(0.58, abs=0.01)
    assert bare.kicad_skew_mm == pytest.approx(bare.budget_mm_equivalent)
    assert bare.kicad_skew_severity == "warning"  # the budget's own severity


def test_no_budget_keeps_the_legacy_length_check(tmp_path: Path):
    path = _board(
        tmp_path,
        _seg(110, 120, 140, 120, "F.Cu", 1, 1),
        _seg(110, 120.375, 140.7, 120.375, "F.Cu", 2, 2),
    )
    nc = NetClassRouting(name="Default", coupled_routing=True, trace_width=0.275)
    # Rename the nets so no name rule identifies a protocol.
    text = path.read_text().replace("USB3_TX1", "FOO")
    path.write_text(text)
    ncm = {"FOO+": nc, "FOO-": nc}
    findings = _skew_findings(path, ncm)
    assert len(findings) == 1
    assert "length-skew 0.700 mm exceeds tolerance 0.500 mm" in findings[0].message


def test_parts_total_equals_net_length(split_layer_board: Path):
    from kicad_tools.router.kicad_length import (
        KicadStackup,
        measure_pcb_net,
        measure_pcb_net_parts,
    )
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(split_layer_board))
    stack = KicadStackup.from_pcb(pcb, 4)
    for net in (1, 2):
        parts = measure_pcb_net_parts(pcb, net, stack)
        assert parts.total == measure_pcb_net(pcb, net, stack)
    parts = measure_pcb_net_parts(pcb, 1, stack)
    assert parts.by_layer["In1.Cu"] == pytest.approx(20.0, abs=0.5)
    assert parts.via_mm > 0


# ---------------------------------------------------------------------------
# The committed 06rf fixture: the note's headline example
# ---------------------------------------------------------------------------


def test_06rf_usb3_tx1_newly_fails_in_ps():
    from kicad_tools.router.rules import net_class_map_from_path
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_skew import derive_pair_skews

    root = REPO / "boards/06-diffpair-test/regression-fixture"
    pcb_path = root / "diffpair_test_routed.kicad_pcb"
    pcb = PCB.load(str(pcb_path))
    ncm = net_class_map_from_path(root / "net_class_map.json", pcb_path=pcb_path)
    by_pair = {m.p_name: m for m in derive_pair_skews(pcb, ncm, num_copper_layers=4)}
    tx1 = by_pair["USB3_TX1+"]
    assert tx1.skew_mm < 0.02  # length-matched (note §5.3: 0.015 mm)
    assert tx1.skew_ps == pytest.approx(27.5, rel=0.1)  # note: 27.5 ps
    assert tx1.limits.budget is not None
    assert tx1.limits.budget.budget_ps == pytest.approx(4.03, abs=0.01)
    assert tx1.skew_ps > tx1.limits.budget.budget_ps
