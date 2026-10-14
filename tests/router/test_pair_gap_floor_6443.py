"""Coupled diff-pair gap floored at the authored board minimum (Issue #6443, P1 piece (c)).

Board 06's input project authors ``rules.min_clearance = 0.15``; the
classifier's ``HighSpeed`` class asks for an intra-pair gap of 0.1016 mm and
the lattice laid P/N at the 0.127 mm fab floor, which kicad-cli failed twice
per coupled segment pair (``diff_pair_gap_out_of_range`` and ``clearance``).

Covered here:

* :meth:`DesignRules.pair_gap` -- ``max(class, manufacturer, authored)``;
* the lattice fixture: authored 0.15 mm over a 0.075 mm class routes at
  >= 0.15 mm and prints one notice; with no authored minimum the gap is the
  class value and nothing is printed;
* ``kct route``'s clearance resolution carries the authored minimum;
* the ``diff_pair_gap`` managed ``.kicad_dru`` block: one rule per
  KiCad-recognised pair, accepted by the kicad-cli keyword guard, byte-stable
  across re-emits, and (when kicad-cli is installed) enforced by kicad-cli.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.manufacturers.netclass_dru import (
    DRU_PAIR_GAP_BLOCK_BEGIN,
    DRU_PAIR_GAP_BLOCK_END,
    merge_pair_gap_block,
    pair_gap_block_rules,
    render_pair_gap_rule,
)
from kicad_tools.router.board_clearance_rules import KICAD_CONSTRAINT_KEYWORDS, parse_dru
from kicad_tools.router.layers import Layer
from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

REPO = Path(__file__).resolve().parents[2]
BOARD06 = REPO / "boards/06-diffpair-test/output"


def _hs(intra: float | None = 0.075) -> NetClassRouting:
    return NetClassRouting(
        name="HS",
        trace_width=0.2,
        clearance=0.15,
        intra_pair_clearance=intra,
        coupled_routing=True,
    )


# ---------------------------------------------------------------------------
# DesignRules.pair_gap
# ---------------------------------------------------------------------------


def test_pair_gap_is_the_class_value_without_floors() -> None:
    gap = DesignRules().pair_gap(_hs(0.075))
    assert gap.gap_mm == pytest.approx(0.075)
    assert gap.class_mm == pytest.approx(0.075)
    assert not gap.raised


def test_pair_gap_is_raised_to_the_authored_board_minimum() -> None:
    gap = DesignRules(authored_min_clearance=0.15).pair_gap(_hs(0.075))
    assert gap.gap_mm == pytest.approx(0.15)
    assert gap.raised
    assert "authored" in (gap.source or "")


def test_pair_gap_takes_the_larger_of_the_two_floors() -> None:
    from kicad_tools.router.mfr_limits import get_mfr_limits

    fab = get_mfr_limits("jlcpcb").min_clearance
    below = DesignRules(manufacturer="jlcpcb", authored_min_clearance=fab - 0.02)
    assert below.pair_gap(_hs(0.075)).gap_mm == pytest.approx(fab)
    assert "jlcpcb" in (below.pair_gap(_hs(0.075)).source or "")
    above = DesignRules(manufacturer="jlcpcb", authored_min_clearance=fab + 0.02)
    assert above.pair_gap(_hs(0.075)).gap_mm == pytest.approx(fab + 0.02)


def test_pair_gap_honours_a_class_value_above_every_floor() -> None:
    gap = DesignRules(manufacturer="jlcpcb", authored_min_clearance=0.15).pair_gap(_hs(0.2))
    assert gap.gap_mm == pytest.approx(0.2)
    assert not gap.raised


def test_pair_gap_falls_back_to_class_clearance_when_intra_is_unset() -> None:
    # ``effective_intra_pair_clearance`` is ``clearance`` when no override.
    gap = DesignRules(authored_min_clearance=0.1).pair_gap(_hs(None))
    assert gap.gap_mm == pytest.approx(0.15)
    assert not gap.raised


# ---------------------------------------------------------------------------
# The lattice fixture
# ---------------------------------------------------------------------------


def _pad(x: float, y: float, net: int, name: str, ref: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=name,
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _route_pair(authored: float | None):
    from kicad_tools.router.core import Autorouter

    nc = _hs(0.075)
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, authored_min_clearance=authored)
    router = Autorouter(30, 12, rules=rules, strategy="lattice", net_class_map={"D+": nc, "D-": nc})
    pads = [
        _pad(3.0, 5.0, 1, "D+", "J1"),
        _pad(3.0, 6.0, 2, "D-", "J1"),
        _pad(27.0, 5.0, 1, "D+", "U1"),
        _pad(27.0, 6.0, 2, "D-", "U1"),
    ]
    for i, pad in enumerate(pads):
        key = (pad.ref, f"{i}")
        router.pads[key] = pad
        router.nets.setdefault(pad.net, []).append(key)
        router.net_names[pad.net] = pad.net_name
    routes_p = router.route_net(1)
    routes_n = router.route_net(2)
    return router, routes_p, routes_n


def _min_edge_gap(routes_p, routes_n) -> float:
    segs_p = [s for r in routes_p for s in r.segments]
    segs_n = [s for r in routes_n for s in r.segments]
    assert segs_p and segs_n
    best = float("inf")
    for a, b in itertools.product(segs_p, segs_n):
        if a.layer != b.layer:
            continue
        centre = seg_seg_dist((a.x1, a.y1), (a.x2, a.y2), (b.x1, b.y1), (b.x2, b.y2))
        best = min(best, centre - a.width / 2 - b.width / 2)
    return best


def test_lattice_floors_the_coupled_gap_at_the_authored_minimum(capsys) -> None:
    router, routes_p, routes_n = _route_pair(0.15)
    assert router._lattice_pair_outcomes == {"D": "coupled"}
    assert _min_edge_gap(routes_p, routes_n) >= 0.15 - 1e-6
    out = capsys.readouterr().out
    notices = [line for line in out.splitlines() if "Diff-pair gap:" in line]
    assert len(notices) == 1, out
    assert "class HS" in notices[0]
    assert "0.075mm raised to 0.15mm" in notices[0]


def test_lattice_gap_is_unchanged_without_an_authored_minimum(capsys) -> None:
    router, routes_p, routes_n = _route_pair(None)
    assert router._lattice_pair_outcomes == {"D": "coupled"}
    assert _min_edge_gap(routes_p, routes_n) == pytest.approx(0.075, abs=1e-6)
    assert "Diff-pair gap:" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# kct route resolution: the authored minimum reaches DesignRules
# ---------------------------------------------------------------------------


def _project(min_clearance: float | None) -> dict:
    rules = {} if min_clearance is None else {"min_clearance": min_clearance}
    return {"board": {"design_settings": {"rules": rules}}, "meta": {"version": 1}}


@pytest.mark.parametrize("authored", [0.15, None])
def test_route_clearance_resolution_records_the_authored_minimum(tmp_path, authored) -> None:
    from kicad_tools.cli.route_cmd import _resolve_route_clearance

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20240108) (generator "pcbnew"))\n')
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(_project(authored)))
    args = SimpleNamespace(clearance=0.15, manufacturer="jlcpcb", via_clearance=None)
    _resolve_route_clearance(args, pcb, [], quiet=True)
    assert args._authored_min_clearance == authored


def test_router_pair_gaps_use_the_routers_floored_gap() -> None:
    from kicad_tools.cli.route_cmd import _router_pair_gaps
    from kicad_tools.router.core import Autorouter

    nc = _hs(0.075)
    router = Autorouter(10, 10, rules=DesignRules(authored_min_clearance=0.15))
    gaps = _router_pair_gaps([("D+", "D-")], {"D+": nc, "D-": nc}, router)
    assert gaps == [("D+", "D-", pytest.approx(0.15))]
    assert _router_pair_gaps(None, {"D+": nc}, router) is None
    # No router: the class value, unfloored.
    assert _router_pair_gaps([("D+", "D-")], {"D+": nc, "D-": nc}) == [
        ("D+", "D-", pytest.approx(0.075))
    ]


# ---------------------------------------------------------------------------
# The diff_pair_gap managed block
# ---------------------------------------------------------------------------


def test_block_rules_keep_only_kicad_recognised_pairs_oriented_and_sorted() -> None:
    rules = pair_gap_block_rules(
        [
            ("LVDS2_N", "LVDS2_P", 0.15),
            ("USB_D+", "USB_D-", 0.127),
            ("X_POS", "X_NEG", 0.15),  # KiCad does not pair these: inert natively
            ("LVDS2_P", "LVDS2_N", 0.2),  # duplicate: the stricter gap wins
        ]
    )
    assert rules == [("LVDS2_P", "LVDS2_N", 0.2), ("USB_D+", "USB_D-", 0.127)]


def test_block_rules_drop_pairs_not_on_the_board() -> None:
    rules = pair_gap_block_rules([("A_P", "A_N", 0.15), ("B_P", "B_N", 0.15)], {"A_P", "A_N"})
    assert rules == [("A_P", "A_N", 0.15)]


def test_rendered_block_passes_the_kicad_cli_keyword_guard(tmp_path) -> None:
    assert "diff_pair_gap" in KICAD_CONSTRAINT_KEYWORDS
    text = merge_pair_gap_block(None, [("LVDS1_P", "LVDS1_N", 0.15), ("Q'_P", "Q'_N", 0.2)])
    assert text is not None
    dru = tmp_path / "b.kicad_dru"
    dru.write_text(text)
    rules, error = parse_dru(dru)
    assert error is None, error
    # Gap rules are not clearance rules: the route-auto clearance gate skips them.
    assert rules == []
    from kicad_tools.sexp import parse_string

    tree = parse_string("(rules " + text + ")")
    names = [str(r.get_atoms()[0]) for r in tree.find_all("rule")]
    assert names == ["kct diff pair gap - LVDS1_P/LVDS1_N", "kct diff pair gap - Q'_P/Q'_N"]
    assert "A.NetName == 'Q\\\\'_P'" in text


def test_block_merge_is_idempotent_and_removable() -> None:
    base = "(version 1)\n\n(rule \"mine\"\n  (constraint clearance (min 0.2mm)))\n"
    rules = [("LVDS1_P", "LVDS1_N", 0.15)]
    once = merge_pair_gap_block(base, rules)
    assert once is not None and once.count(DRU_PAIR_GAP_BLOCK_BEGIN) == 1
    assert merge_pair_gap_block(once, rules) == once
    assert "(rule \"mine\"" in once
    removed = merge_pair_gap_block(once, [])
    assert removed is not None and DRU_PAIR_GAP_BLOCK_END not in removed
    assert "(rule \"mine\"" in removed
    assert merge_pair_gap_block(base, []) == base


def _copy_board06(tmp_path: Path) -> Path:
    src = BOARD06 / "diffpair_test_routed.kicad_pcb"
    if not src.exists():
        pytest.skip("board 06 routed output not committed")
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copyfile(src.with_suffix(suffix), tmp_path / f"b{suffix}")
    return tmp_path / "b.kicad_pcb"


def test_write_drc_constraints_emits_the_block_byte_stably(tmp_path) -> None:
    from kicad_tools.manufacturers import get_profile, write_drc_constraints

    pcb = _copy_board06(tmp_path)
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    gaps = [(f"LVDS{i}_P", f"LVDS{i}_N", 0.15) for i in range(1, 5)]
    write_drc_constraints(pcb, rules, manufacturer_id="jlcpcb", router_pair_gaps=gaps)
    first = pcb.with_suffix(".kicad_dru").read_bytes()
    write_drc_constraints(pcb, rules, manufacturer_id="jlcpcb", router_pair_gaps=gaps)
    assert pcb.with_suffix(".kicad_dru").read_bytes() == first
    text = first.decode()
    for i in range(1, 5):
        assert render_pair_gap_rule(f"LVDS{i}_P", f"LVDS{i}_N", 0.15) in text
    # ``None`` keeps the block; an empty list removes it.
    write_drc_constraints(pcb, rules, manufacturer_id="jlcpcb")
    assert pcb.with_suffix(".kicad_dru").read_bytes() == first
    write_drc_constraints(pcb, rules, manufacturer_id="jlcpcb", router_pair_gaps=[])
    assert DRU_PAIR_GAP_BLOCK_BEGIN not in pcb.with_suffix(".kicad_dru").read_text()


def _kicad_cli() -> Path:
    from kicad_tools.cli.runner import find_kicad_cli

    exe = find_kicad_cli()
    if exe is None:
        pytest.skip("kicad-cli not installed")
    return exe


def _drc_types(exe: Path, pcb: Path) -> list[tuple[str, frozenset[str]]]:
    report = pcb.with_suffix(".drc.json")
    subprocess.run(
        [str(exe), "pcb", "drc", "--format", "json", "--severity-all", "-o", str(report), str(pcb)],
        check=False,
        capture_output=True,
        timeout=300,
    )
    data = json.loads(report.read_text())
    out = []
    for v in data.get("violations", []):
        nets = frozenset(
            item.get("description", "").rsplit("[", 1)[-1].split("]", 1)[0]
            for item in v.get("items", [])
        )
        out.append((v["type"], nets))
    return out


def test_kicad_cli_enforces_the_emitted_gap_rule(tmp_path) -> None:
    exe = _kicad_cli()
    pcb = _copy_board06(tmp_path)
    dru = pcb.with_suffix(".kicad_dru")
    original = dru.read_text()

    # The committed copper sits at the LVDS class gap (0.15 mm): a rule at the
    # resolved gap passes ...
    dru.write_text(merge_pair_gap_block(original, [("LVDS1_P", "LVDS1_N", 0.15)]) or "")
    gap_errors = [t for t in _drc_types(exe, pcb) if t[0] == "diff_pair_gap_out_of_range"]
    assert gap_errors == []

    # ... and one stricter than the copper is enforced, for that pair only.
    dru.write_text(merge_pair_gap_block(original, [("LVDS1_P", "LVDS1_N", 0.2)]) or "")
    gap_errors = [t for t in _drc_types(exe, pcb) if t[0] == "diff_pair_gap_out_of_range"]
    assert gap_errors, "kicad-cli ignored the emitted diff_pair_gap rule"
    assert all(nets <= {"LVDS1_P", "LVDS1_N"} for _, nets in gap_errors)
