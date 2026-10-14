"""Issue #6615: the kct-only per-terminal uncoupled-length figure.

``diffpair_uncoupled_length`` charges each net's uncoupled copper that lies
outside every terminal's gather-ignore region (courtyard, optionally grown)
to its nearest terminal along the copper, and holds each terminal to
``max_uncoupled_per_terminal_mm``.  KiCad has no equivalent.

Board geometry used throughout (all on F.Cu unless stated)::

    U1 pads (1.5, 0) / (1.5, 0.4)               J1 pads (30, 0) / (30, 0.4)
    P: (1.5, 0) -------------------------------------------- (30, 0)
    N: (1.5, 0.4) ---------------- (27, 0.4) \\  (28.5, 1.9)  / (30, 0.4)

N leaves its partner at x = 27 with two 45-degree legs (2.121 mm each), so P
is uncoupled for 3 mm (x 27..30) and N for 4.243 mm, all near J1.
"""

from __future__ import annotations

import json
import math
import uuid
from pathlib import Path

import pytest

from kicad_tools.router.rules import NetClassRouting
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds
from kicad_tools.validate.rules.diffpair_uncoupled_length import (
    KCT_ONLY_NOTE,
    PAD_BBOX_FALLBACK_MARGIN_MM,
    DiffPairUncoupledLengthRule,
    is_per_terminal_finding,
    measure_pair,
)

pytest.importorskip("shapely")

REPO = Path(__file__).resolve().parents[1]

_HDR = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)
\t\t(46 "B.CrtYd" user "B.Courtyard") (47 "F.CrtYd" user "F.Courtyard"))
\t(setup (pad_to_mask_clearance 0))
"""

LEG = 1.5 * math.sqrt(2.0)  # one 45-degree leg of N's detour


def seg(a, b, net, layer="F.Cu"):
    return (
        f"\t(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) "
        f'(layer "{layer}") (net "{net}") (uuid "{uuid.uuid4()}"))\n'
    )


def via(at, net):
    return (
        f'\t(via (at {at[0]} {at[1]}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        f'(net "{net}") (uuid "{uuid.uuid4()}"))\n'
    )


def footprint(ref, at, pads, courtyard=None):
    """A footprint at ``at``; ``pads`` are ``(local xy, net)``; ``courtyard`` a local rect."""
    crtyd = ""
    if courtyard is not None:
        (x0, y0), (x1, y1) = courtyard
        crtyd = (
            f"\t\t(fp_rect (start {x0} {y0}) (end {x1} {y1}) "
            '(stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))\n'
        )
    pad_text = "".join(
        f'\t\t(pad "{i + 1}" smd rect (at {x} {y}) (size 1 1) '
        f'(layers "F.Cu" "F.Mask") (net "{net}"))\n'
        for i, ((x, y), net) in enumerate(pads)
    )
    return (
        f'\t(footprint "Test:{ref}" (layer "F.Cu") (uuid "{uuid.uuid4()}") '
        f"(at {at[0]} {at[1]})\n"
        f'\t\t(property "Reference" "{ref}" (at 0 -3 0) (layer "F.SilkS") '
        f'(uuid "{uuid.uuid4()}"))\n' + crtyd + pad_text + "\t)\n"
    )


def u1():
    return footprint("U1", (0, 0), [((1.5, 0), "T_P"), ((1.5, 0.4), "T_N")], ((-2, -2), (2, 2)))


def j1(courtyard=((-1, -2), (1, 2))):
    return footprint("J1", (30, 0), [((0, 0), "T_P"), ((0, 0.4), "T_N")], courtyard)


COPPER = [
    seg((1.5, 0), (30, 0), "T_P"),
    seg((1.5, 0.4), (27, 0.4), "T_N"),
    seg((27, 0.4), (28.5, 1.9), "T_N"),
    seg((28.5, 1.9), (30, 0.4), "T_N"),
]


def load(tmp_path: Path, items: list[str]) -> PCB:
    path = tmp_path / "t.kicad_pcb"
    edge = (
        "\t(gr_rect (start -5 -10) (end 40 20) (stroke (width 0.1) (type default)) "
        f'(fill no) (layer "Edge.Cuts") (uuid "{uuid.uuid4()}"))\n'
    )
    path.write_text(_HDR + "".join(items) + edge + ")\n")
    return PCB.load(str(path))


def measure(pcb: PCB, gather="courtyard"):
    ids = {n.name: n.number for n in pcb.nets.values()}
    return measure_pair(pcb, ids["T_P"], ids["T_N"], "T_P", "T_N", gather_ignore=gather)


def figures(terminals) -> dict[str, float]:
    return {ref: mm for ref, mm, _ in terminals}


def classes(**fields) -> dict[str, NetClassRouting]:
    nc = NetClassRouting(name="HS", priority=2, coupled_routing=True, **fields)
    return {"T_P": nc, "T_N": nc}


def run(pcb: PCB, ncm, **kw):
    ids = {n.name: n.number for n in pcb.nets.values()}
    key = (ids["T_P"], ids["T_N"])
    rule = DiffPairUncoupledLengthRule(
        engaged_pairs={key}, limits={key: resolve_pair_thresholds(ncm, "T_P", "T_N")}, **kw
    )
    return rule.check(pcb, None)  # type: ignore[arg-type]


def per_terminal(results):
    return [v for v in results.violations if is_per_terminal_finding(v)]


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def test_uncoupled_copper_inside_the_courtyard_is_not_charged(tmp_path) -> None:
    # J1's courtyard spans x 26..34: every uncoupled millimetre is inside it.
    pcb = load(tmp_path, [u1(), j1(((-4, -2), (4, 2))), *COPPER])
    m = measure(pcb)
    assert m.p_uncoupled_mm == pytest.approx(3.0)
    assert m.n_uncoupled_mm == pytest.approx(2 * LEG)
    assert figures(m.p_terminals) == pytest.approx({"J1": 0.0, "U1": 0.0})
    assert figures(m.n_terminals) == pytest.approx({"J1": 0.0, "U1": 0.0})
    assert m.per_terminal_mm == pytest.approx(0.0)


def test_uncoupled_copper_outside_the_courtyard_is_charged_to_the_near_terminal(
    tmp_path,
) -> None:
    # J1's courtyard spans x 29..31: x 27..29 is outside.
    pcb = load(tmp_path, [u1(), j1(), *COPPER])
    m = measure(pcb)
    assert figures(m.p_terminals) == pytest.approx({"J1": 2.0, "U1": 0.0})
    # N: the whole first leg, and the second leg up to x = 29.
    assert figures(m.n_terminals) == pytest.approx({"J1": LEG + 0.5 * math.sqrt(2), "U1": 0.0})
    assert {src for _, _, src in m.p_terminals} == {"courtyard"}


def test_gather_ignore_radius_grows_the_region(tmp_path) -> None:
    pcb = load(tmp_path, [u1(), j1(), *COPPER])
    assert measure(pcb, gather=2.0).per_terminal_mm == pytest.approx(0.0)
    # Grown by 0.5 mm the region starts at x = 28.5: only N's first leg is left.
    assert measure(pcb, gather=0.5).per_terminal_mm == pytest.approx(LEG)


def test_gather_ignore_none_charges_all_uncoupled_copper(tmp_path) -> None:
    pcb = load(tmp_path, [u1(), j1(), *COPPER])
    m = measure(pcb, gather="none")
    assert figures(m.p_terminals) == pytest.approx({"J1": 3.0, "U1": 0.0})
    assert figures(m.n_terminals) == pytest.approx({"J1": 2 * LEG, "U1": 0.0})
    assert {src for _, _, src in m.n_terminals} == {"none"}


def test_footprint_without_courtyard_falls_back_to_the_pad_bounding_box(tmp_path) -> None:
    pcb = load(tmp_path, [u1(), j1(courtyard=None), *COPPER])
    m = measure(pcb)
    # J1's pads (1 x 1 mm at x = 30) as circumscribed circles, plus the margin.
    edge = 30.0 - math.hypot(1, 1) / 2 - PAD_BBOX_FALLBACK_MARGIN_MM
    assert figures(m.p_terminals)["J1"] == pytest.approx(edge - 27.0)
    assert figures(m.n_terminals)["J1"] == pytest.approx(LEG + (edge - 28.5) * math.sqrt(2))
    assert {r: s for r, _, s in m.p_terminals} == {"J1": "pad bbox", "U1": "courtyard"}


def test_each_terminal_is_charged_its_own_end(tmp_path) -> None:
    # Mirror the detour at the U1 end: each end now carries the same figure.
    copper = [
        seg((1.5, 0), (30, 0), "T_P"),
        seg((1.5, 0.4), (3, 1.9), "T_N"),
        seg((3, 1.9), (4.5, 0.4), "T_N"),
        seg((4.5, 0.4), (27, 0.4), "T_N"),
        seg((27, 0.4), (28.5, 1.9), "T_N"),
        seg((28.5, 1.9), (30, 0.4), "T_N"),
    ]
    pcb = load(tmp_path, [u1(), j1(), *copper])
    m = measure(pcb, gather="none")
    assert figures(m.n_terminals) == pytest.approx({"J1": 2 * LEG, "U1": 2 * LEG})
    assert figures(m.p_terminals) == pytest.approx({"J1": 3.0, "U1": 3.0})


def test_single_terminal_net_takes_everything(tmp_path) -> None:
    pcb = load(tmp_path, [u1(), *COPPER])  # J1 not on the board
    m = measure(pcb)
    assert figures(m.p_terminals) == pytest.approx({"U1": 3.0})
    assert figures(m.n_terminals) == pytest.approx({"U1": 2 * LEG})


def test_via_inside_the_gather_region_joins_layers(tmp_path) -> None:
    # P drops to B.Cu through a via inside J1's courtyard (x 29..31).
    copper = [
        seg((1.5, 0), (29.5, 0), "T_P"),
        via((29.5, 0), "T_P"),
        seg((29.5, 0), (30, 0), "T_P", "B.Cu"),
        seg((1.5, 0.4), (30, 0.4), "T_N"),
    ]
    pcb = load(tmp_path, [u1(), j1(), *copper])
    m = measure(pcb)
    assert m.p_uncoupled_mm == pytest.approx(0.5)  # the total still sees it
    assert m.per_terminal_mm == pytest.approx(0.0)  # ... inside the courtyard


def test_via_carries_attribution_across_layers(tmp_path) -> None:
    # P runs on B.Cu from x = 20 (via outside every region): 10 mm uncoupled,
    # charged up to the meeting point along the copper.
    copper = [
        seg((1.5, 0), (20, 0), "T_P"),
        via((20, 0), "T_P"),
        seg((20, 0), (30, 0), "T_P", "B.Cu"),
        seg((1.5, 0.4), (30, 0.4), "T_N"),
    ]
    pcb = load(tmp_path, [u1(), j1(), *copper])
    m = measure(pcb, gather="none")
    p = figures(m.p_terminals)
    assert sum(p.values()) == pytest.approx(m.p_uncoupled_mm) == pytest.approx(10.0)
    assert p["J1"] == pytest.approx(10.0)  # U1 reaches it only via 18.5 mm of P


def test_net_without_terminals_has_no_per_terminal_figure(tmp_path) -> None:
    pcb = load(tmp_path, COPPER)
    m = measure(pcb)
    assert m.p_terminals == () and m.n_terminals == () and m.per_terminal_mm is None
    assert not per_terminal(run(pcb, classes(max_uncoupled_per_terminal_mm=0.1), emit_info=True))


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_per_terminal_fails_while_the_total_passes(tmp_path) -> None:
    # 2.5 mm per terminal: total limit 2.5 x 2 components = 5.0 > 4.243 passes;
    # N's 2.828 mm outside J1's courtyard fails.
    pcb = load(tmp_path, [u1(), j1(), *COPPER])
    res = run(pcb, classes(max_uncoupled_per_terminal_mm=2.5))
    [v] = res.violations
    assert is_per_terminal_finding(v)
    assert v.severity == "error"
    assert v.nets == ("T_N",)
    assert v.actual_value == pytest.approx(2 * math.sqrt(2), abs=1e-4)
    assert v.required_value == pytest.approx(2.5)
    assert "at J1" in v.message and "courtyard" in v.message
    assert KCT_ONLY_NOTE in v.message and "kct-only" in v.message


def test_same_copper_inside_the_courtyard_passes(tmp_path) -> None:
    pcb = load(tmp_path, [u1(), j1(((-4, -2), (4, 2))), *COPPER])
    res = run(pcb, classes(max_uncoupled_per_terminal_mm=2.5), emit_info=True)
    assert not [v for v in res.violations if v.severity != "info"]
    [info] = per_terminal(res)
    assert info.severity == "info" and info.actual_value == 0.0
    assert KCT_ONLY_NOTE in info.message


def test_class_gather_ignore_reaches_the_check(tmp_path) -> None:
    pcb = load(tmp_path, [u1(), j1(), *COPPER])
    assert not run(pcb, classes(max_uncoupled_per_terminal_mm=2.5, gather_ignore=2.0)).violations


def test_fallback_is_named_in_the_finding(tmp_path) -> None:
    pcb = load(tmp_path, [u1(), j1(courtyard=None), *COPPER])
    [v] = per_terminal(run(pcb, classes(max_uncoupled_per_terminal_mm=2.5)))
    assert "pad bounding box" in v.message and "no courtyard" in v.message


def _patched_usb(monkeypatch, cited: bool) -> None:
    from kicad_tools.router import pair_protocols as pp

    row = pp.PROTOCOL_DEFAULTS[pp.PairProtocol.USB2_FS]
    source = "Test Source §1" if cited else pp.UNCITED
    monkeypatch.setitem(
        pp.PROTOCOL_DEFAULTS,
        pp.PairProtocol.USB2_FS,
        pp.ProtocolDefaults(
            protocol=row.protocol,
            max_skew_mm=row.max_skew_mm,
            max_uncoupled_per_terminal_mm=pp.Sourced(2.5, source),
        ),
    )


@pytest.mark.parametrize(("cited", "severity"), [(True, "error"), (False, "warning")])
def test_severity_follows_the_limit_source(monkeypatch, tmp_path, cited, severity) -> None:
    _patched_usb(monkeypatch, cited)
    pcb = load(tmp_path, [u1(), j1(), *COPPER])
    [v] = per_terminal(run(pcb, classes(pair_protocol="USB2_FS")))
    assert v.severity == severity
    assert "protocol:USB2_FS" in v.message


def test_explicit_total_does_not_promote_an_uncited_per_terminal_limit(
    monkeypatch, tmp_path
) -> None:
    _patched_usb(monkeypatch, cited=False)
    pcb = load(tmp_path, [u1(), j1(), *COPPER])
    res = run(pcb, classes(pair_protocol="USB2_FS", max_uncoupled_total_mm=100.0))
    [v] = per_terminal(res)
    assert v.severity == "warning"
    assert not [x for x in res.violations if not is_per_terminal_finding(x)]


def test_pair_without_a_per_terminal_limit_is_not_checked(tmp_path) -> None:
    pcb = load(tmp_path, [u1(), j1(), *COPPER])
    res = run(pcb, classes(max_uncoupled_total_mm=100.0), emit_info=True)
    assert not per_terminal(res)


def test_measurement_summary_labels_per_terminal_rows(tmp_path, capsys) -> None:
    from kicad_tools.cli.check_cmd import _print_measurement_summary

    pcb = load(tmp_path, [u1(), j1(), *COPPER])
    _print_measurement_summary(run(pcb, classes(max_uncoupled_per_terminal_mm=2.5)).violations)
    out = capsys.readouterr().out
    assert "uncpl/term" in out


# ---------------------------------------------------------------------------
# gather_ignore field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["courtyard", "none", 0, 0.5])
def test_gather_ignore_accepts_documented_values(value) -> None:
    nc = NetClassRouting(name="X", gather_ignore=value)
    assert NetClassRouting.from_dict(nc.to_dict()).gather_ignore == value


@pytest.mark.parametrize("value", ["pads", -1.0, True, float("nan")])
def test_gather_ignore_rejects_other_values(value) -> None:
    with pytest.raises(ValueError, match="gather_ignore"):
        NetClassRouting(name="X", gather_ignore=value)


def test_gather_ignore_defaults_to_courtyard_and_old_sidecars_load() -> None:
    assert NetClassRouting(name="X").gather_ignore == "courtyard"
    assert NetClassRouting.from_dict({"name": "X"}).gather_ignore == "courtyard"
    t = resolve_pair_thresholds(classes(gather_ignore=1.5), "T_P", "T_N")
    assert t.gather_ignore == 1.5


# ---------------------------------------------------------------------------
# Regression: the KiCad-equivalent total is unchanged on the committed boards
# ---------------------------------------------------------------------------

COMMITTED = {
    "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb": {
        ("USB_D+", "USB_D-"): 10.4052,
        ("USB_MCU_D+", "USB_MCU_D-"): 0.9635,
    },
    "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb": {
        (f"LVDS{i}_P", f"LVDS{i}_N"): 1.5412 for i in (1, 2, 3, 4)
    },
}


@pytest.mark.parametrize("pcb_rel", sorted(COMMITTED))
def test_committed_boards_total_matches_kicad_and_per_terminal_conserves(pcb_rel) -> None:
    pcb = PCB.load(str(REPO / pcb_rel))
    ids = {n.name: n.number for n in pcb.nets.values()}
    for (p, n), total in COMMITTED[pcb_rel].items():
        m = measure_pair(pcb, ids[p], ids[n], p, n)
        assert m.uncoupled_mm == pytest.approx(total, abs=1e-3)
        # Every terminal well within the 6.35 mm USB2 allowance.
        assert m.per_terminal_mm is not None and m.per_terminal_mm < 6.35
        # With nothing ignored, the per-terminal figures add up to each net's total.
        bare = measure_pair(pcb, ids[p], ids[n], p, n, gather_ignore="none")
        for net_total, terms in (
            (bare.p_uncoupled_mm, bare.p_terminals),
            (bare.n_uncoupled_mm, bare.n_terminals),
        ):
            assert sum(mm for _, mm, _ in terms) == pytest.approx(net_total, abs=1e-6)


@pytest.mark.parametrize(
    ("pcb_rel", "ncm_rel"),
    [
        (
            "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb",
            "boards/03-usb-joystick/output/net_class_map.json",
        ),
        (
            "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb",
            "boards/06-diffpair-test/output/net_class_map.json",
        ),
    ],
)
def test_committed_boards_stay_clean(pcb_rel, ncm_rel) -> None:
    from kicad_tools.router.rules import net_class_map_from_dict
    from kicad_tools.validate.checker import DRCChecker

    ncm = net_class_map_from_dict(json.loads((REPO / ncm_rel).read_text()))
    checker = DRCChecker(
        PCB.load(str(REPO / pcb_rel)), manufacturer="jlcpcb", layers=4, net_class_map=ncm
    )
    res = checker.check_diffpair_uncoupled_length()
    assert [v for v in res.violations if v.severity in ("error", "warning")] == []
