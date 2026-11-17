"""Router-coupled pairs KiCad does not recognise by name (Issue #6429).

The router couples ``X_POS``/``X_NEG``, ``x_p``/``x_n`` and ``USB_dp``/``USB_dn``
(case-insensitive suffixes, ``_POS``/``_NEG``, declared pairs) at its class's
``intra_pair_clearance``, but KiCad's ``isCoupledDiffPair()`` only pairs a
trailing ``+``/``-`` or uppercase ``P``/``N``.  The routed ``.kicad_pro``
now records those pairs under ``meta.kct_router_diff_pairs``; the router-class
``.kicad_dru`` rule exempts them by exact net name in both orderings, and
``kct check`` reads the same record through ``is_router_coupled_pair``.
"""

from __future__ import annotations

import itertools
import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.manufacturers import write_drc_constraints
from kicad_tools.manufacturers.netclass_dru import (
    COUPLED_DIFF_PAIR,
    coupled_pair_exemption,
    is_router_coupled_pair,
    netclass_condition,
)
from kicad_tools.manufacturers.project_generator import (
    ROUTER_DIFF_PAIRS_META_KEY,
    apply_router_coupled_pairs,
    board_net_names,
    recorded_router_coupled_pairs,
)
from kicad_tools.router.diffpair_detection import coupled_engaged_pairs
from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED, NetClassRouting, net_class_map_from_dict

from .test_route_sidecar_netclass_clearance_6390 import (
    RULES,
    _has_kicad_cli,
    _kct_errors,
    _kicad_cli_clearance,
    _pair_board,
)

REPO = Path(__file__).resolve().parents[1]

#: Pairs the router couples that KiCad's MatchDpSuffix does not recognise.
_UNRECOGNISED = [("X_POS", "X_NEG"), ("x_p", "x_n"), ("USB_dp", "USB_dn")]


def _high_speed_map(*nets: str) -> dict[str, NetClassRouting]:
    assert NET_CLASS_HIGH_SPEED.coupled_routing
    assert NET_CLASS_HIGH_SPEED.clearance == pytest.approx(0.15)
    return dict.fromkeys(nets, NET_CLASS_HIGH_SPEED)


def _route_sidecars(pcb: Path, a: str, b: str, *, mode=None, net_class_map=None) -> None:
    """What ``kct route`` writes: the router map plus the pairs it coupled."""
    class_map = net_class_map if net_class_map is not None else _high_speed_map(a, b)
    write_drc_constraints(
        pcb,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=class_map,
        router_coupled_pairs=coupled_engaged_pairs(board_net_names(pcb) or set(), class_map),
        preserve_board_rules=mode,
    )


def _project(pcb: Path) -> dict:
    return json.loads(pcb.with_suffix(".kicad_pro").read_text())


# ------------------------------------------------------------- the predicate


@pytest.mark.parametrize(
    "a, b, recorded, expect",
    [
        ("USB_D+", "USB_D-", [], True),  # KiCad-named: no record needed
        ("TX_P", "TX_N", [], True),
        ("X_POS", "X_NEG", [], False),
        ("X_POS", "X_NEG", [("X_POS", "X_NEG")], True),
        ("X_NEG", "X_POS", [("X_POS", "X_NEG")], True),  # order-insensitive
        ("X_POS", "Y_NEG", [("X_POS", "X_NEG")], False),  # unrelated net
        ("x_pos", "x_neg", [("X_POS", "X_NEG")], False),  # exact, case-sensitive
        ("X_POS", "X_POS", [("X_POS", "X_POS")], False),
        (None, "X_NEG", [("X_POS", "X_NEG")], False),
    ],
)
def test_is_router_coupled_pair(a, b, recorded, expect):
    assert is_router_coupled_pair(a, b, recorded) is expect


def test_condition_without_extra_pairs_is_unchanged():
    assert coupled_pair_exemption() == COUPLED_DIFF_PAIR
    assert netclass_condition("HighSpeed", router=True) == (
        "(A.NetClass == 'HighSpeed' || B.NetClass == 'HighSpeed') && "
        "!(A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference) && "
        "!A.isCoupledDiffPair()"
    )
    assert netclass_condition("HighSpeed", router=True, extra_pairs=[]) == netclass_condition(
        "HighSpeed", router=True
    )


def test_condition_lists_both_orderings_of_each_recorded_pair():
    condition = netclass_condition("HighSpeed", router=True, extra_pairs=[("X_POS", "X_NEG")])
    assert condition.endswith(
        "!(A.isCoupledDiffPair() || (A.NetName == 'X_POS' && B.NetName == 'X_NEG') || "
        "(A.NetName == 'X_NEG' && B.NetName == 'X_POS'))"
    )
    # Authored classes never get the exemption.
    assert netclass_condition("HighSpeed", router=False, extra_pairs=[("X_POS", "X_NEG")]) == (
        "A.NetClass == 'HighSpeed' || B.NetClass == 'HighSpeed'"
    )


def test_condition_escapes_net_names():
    condition = coupled_pair_exemption([("A'b", "C\\d")])
    assert "A.NetName == 'A\\'b' && B.NetName == 'C\\\\d'" in condition


# ---------------------------------------------------------- the meta record


def test_record_round_trip_filters_kicad_named_and_absent_pairs():
    project: dict = {}
    apply_router_coupled_pairs(
        project,
        [("X_POS", "X_NEG"), ("USB_D+", "USB_D-"), ("TX_P", "TX_N"), ("a_p", "a_n"), ("Q", "Q")],
        net_names={"X_POS", "X_NEG", "USB_D+", "USB_D-", "TX_P", "TX_N", "a_p"},
    )
    assert project["meta"][ROUTER_DIFF_PAIRS_META_KEY] == [["X_POS", "X_NEG"]]
    assert recorded_router_coupled_pairs(project) == [("X_POS", "X_NEG")]
    # A stale record is dropped, not merged, and an empty result has no key.
    apply_router_coupled_pairs(project, [("USB_D+", "USB_D-")])
    assert ROUTER_DIFF_PAIRS_META_KEY not in project["meta"]
    assert recorded_router_coupled_pairs(project) == []


def test_malformed_record_reads_as_empty():
    for record in ("X_POS", [["X_POS"]], [[1, 2]], {"X_POS": "X_NEG"}):
        assert recorded_router_coupled_pairs({"meta": {ROUTER_DIFF_PAIRS_META_KEY: record}}) == []


def test_coupled_engaged_pairs_follows_the_router_gate():
    nets = {1: "X_POS", 2: "X_NEG", 3: "TX_P", 4: "TX_N", 5: "SIG"}
    assert coupled_engaged_pairs(nets, _high_speed_map("X_POS", "X_NEG", "TX_P", "TX_N")) == [
        ("TX_P", "TX_N"),
        ("X_POS", "X_NEG"),
    ]
    off = NetClassRouting(name="Slow", clearance=0.15, coupled_routing=False)
    assert coupled_engaged_pairs(nets, {"X_POS": off, "X_NEG": off}) == []
    assert coupled_engaged_pairs(nets, None) == []
    # Declared pairs couple whatever their names.
    partner = NetClassRouting(
        name="Link", clearance=0.15, coupled_routing=True, diffpair_partner="SIG"
    )
    assert ("SIG", "X_POS") in coupled_engaged_pairs(
        {1: "X_POS", 2: "SIG"}, {"X_POS": partner}
    ) or ("X_POS", "SIG") in coupled_engaged_pairs({1: "X_POS", 2: "SIG"}, {"X_POS": partner})


def _router_engaged_pairs(class_map, names):
    """The pairs ``core.py``'s engagement sites couple (``build_synth_routing``)."""
    from kicad_tools.router.diffpair import should_engage_coupled
    from kicad_tools.router.diffpair_detection import build_synth_routing, detect_diff_pairs

    synth, net_to_class = build_synth_routing(class_map)
    detected = detect_diff_pairs(
        dict(enumerate(names, start=1)), net_class_routing=synth, net_to_class=net_to_class
    )
    return sorted(
        (d.pair.positive.net_name, d.pair.negative.net_name)
        for d in detected
        if should_engage_coupled(d.pair, synth, net_to_class)[0]
    )


@pytest.mark.parametrize("off_first", [True, False], ids=["off-first", "on-first"])
def test_coupled_engaged_pairs_matches_router_for_mixed_class_variants(off_first):
    # One class name, per-net variants differing in ``coupled_routing``: the
    # recorded pairs must be exactly what the router couples, whichever variant
    # is seen first (the setdefault idiom let it answer for every member).
    on = NetClassRouting(name="HighSpeed", clearance=0.15, coupled_routing=True)
    off = NetClassRouting(name="HighSpeed", clearance=0.15, coupled_routing=False)
    on_nets = {"X_POS": on, "X_NEG": on}
    off_nets = {"A_POS": off, "A_NEG": off}
    class_map = {**off_nets, **on_nets} if off_first else {**on_nets, **off_nets}
    names = list(class_map)
    expected = [("X_POS", "X_NEG")]
    assert _router_engaged_pairs(class_map, names) == expected
    assert coupled_engaged_pairs(names, class_map) == expected


# ------------------------------------------------- both engines, recorded pair


@pytest.mark.parametrize("mode", [None, "0"])
@pytest.mark.parametrize("a, b", _UNRECOGNISED)
def test_kct_skips_a_recorded_pair(tmp_path, capsys, mode, a, b):
    pcb = _pair_board(tmp_path, a, b, 0.127)
    _route_sidecars(pcb, a, b, mode=mode)
    assert _project(pcb)["meta"][ROUTER_DIFF_PAIRS_META_KEY] == [[a, b]]
    errors = _kct_errors(pcb, tmp_path)
    capsys.readouterr()
    assert not errors, errors


@pytest.mark.skipif(not _has_kicad_cli(), reason="kicad-cli not installed")
@pytest.mark.parametrize("mode", [None, "0"])
@pytest.mark.parametrize("a, b", _UNRECOGNISED)
def test_kicad_cli_skips_a_recorded_pair(tmp_path, mode, a, b):
    pcb = _pair_board(tmp_path, a, b, 0.127)
    _route_sidecars(pcb, a, b, mode=mode)
    assert not _kicad_cli_clearance(pcb)


def _control(tmp_path: Path, case: str) -> Path:
    """Geometry the exemption must NOT cover: still flagged by both engines."""
    if case == "unrecorded":  # class opts out of coupling: router never coupled it
        pcb = _pair_board(tmp_path, "X_POS", "X_NEG", 0.127)
        off = NetClassRouting(name="HighSpeed", clearance=0.15, coupled_routing=False)
        _route_sidecars(pcb, "X_POS", "X_NEG", net_class_map={"X_POS": off, "X_NEG": off})
        assert ROUTER_DIFF_PAIRS_META_KEY not in _project(pcb).get("meta", {})
    elif case == "unrelated":  # X_POS against Y_NEG, with X_POS/X_NEG recorded
        pcb = _pair_board(tmp_path, "X_POS", "Y_NEG", 0.127)
        write_drc_constraints(
            pcb,
            RULES,
            manufacturer_id="jlcpcb",
            router_net_classes=_high_speed_map("X_POS", "Y_NEG"),
            router_coupled_pairs=[("X_POS", "X_NEG")],
        )
        project = _project(pcb)
        project["meta"][ROUTER_DIFF_PAIRS_META_KEY] = [["X_POS", "X_NEG"], ["Y_POS", "Y_NEG"]]
        pcb.with_suffix(".kicad_pro").write_text(json.dumps(project))
        write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb")
    else:  # recorded pair under an authored class keeps KiCad semantics
        pcb = _pair_board(tmp_path, "X_POS", "X_NEG", 0.127)
        pcb.with_suffix(".kicad_pro").write_text(
            json.dumps(
                {
                    "meta": {ROUTER_DIFF_PAIRS_META_KEY: [["X_POS", "X_NEG"]]},
                    "net_settings": {
                        "meta": {"version": 3},
                        "classes": [
                            {"name": "Default", "clearance": 0.1},
                            {"name": "HighSpeed", "clearance": 0.15},
                        ],
                        "netclass_assignments": {"X_POS": "HighSpeed", "X_NEG": "HighSpeed"},
                    },
                }
            )
        )
        write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb")
        assert recorded_router_coupled_pairs(_project(pcb)) == [("X_POS", "X_NEG")]
    return pcb


_CONTROLS = ["unrecorded", "unrelated", "authored"]


@pytest.mark.parametrize("case", _CONTROLS)
def test_kct_still_flags_controls(tmp_path, capsys, case):
    pcb = _control(tmp_path, case)
    errors = _kct_errors(pcb, tmp_path)
    capsys.readouterr()
    assert errors
    assert all(e["required_value"] == pytest.approx(0.15) for e in errors)


@pytest.mark.skipif(not _has_kicad_cli(), reason="kicad-cli not installed")
@pytest.mark.parametrize("case", _CONTROLS)
def test_kicad_cli_still_flags_controls(tmp_path, case):
    assert _kicad_cli_clearance(_control(tmp_path, case))


@pytest.mark.skipif(not _has_kicad_cli(), reason="kicad-cli not installed")
@pytest.mark.parametrize("keep", ["P->N", "N->P"])
def test_kicad_cli_needs_both_orderings(tmp_path, keep):
    """A one-sided ``NetName`` term leaves the pair flagged in kicad-cli 10."""
    pcb = _pair_board(tmp_path, "X_POS", "X_NEG", 0.127)
    _route_sidecars(pcb, "X_POS", "X_NEG")
    assert not _kicad_cli_clearance(pcb)
    dru = pcb.with_suffix(".kicad_dru")
    text = dru.read_text()
    forward = " || (A.NetName == 'X_POS' && B.NetName == 'X_NEG')"
    backward = " || (A.NetName == 'X_NEG' && B.NetName == 'X_POS')"
    assert forward in text and backward in text
    dru.write_text(text.replace(backward if keep == "P->N" else forward, ""))
    assert _kicad_cli_clearance(pcb)


# --------------------------------------- router pairs == engine pairs, boards


_BOARDS = [
    ("03-usb-joystick", "usb_joystick_routed"),
    ("06-diffpair-test", "diffpair_test_routed"),
]


def _board_map(board: str) -> dict[str, NetClassRouting]:
    data = json.loads((REPO / "boards" / board / "output" / "net_class_map.json").read_text())
    return net_class_map_from_dict(data)


def _pair_sets(net_names: set[str], class_map) -> tuple[set, set, list]:
    router = coupled_engaged_pairs(net_names, class_map)
    project: dict = {}
    apply_router_coupled_pairs(project, router, net_names)
    recorded = recorded_router_coupled_pairs(project)
    # Nets the router put in a coupling-enabled class: the candidates whose
    # pairing either engine could act on under a router class.
    coupled_nets = sorted(
        n for n in net_names if getattr(class_map.get(n), "coupled_routing", False)
    )
    engines = {
        frozenset(pair)
        for pair in itertools.combinations(coupled_nets, 2)
        if is_router_coupled_pair(*pair, recorded)
    }
    return {frozenset(p) for p in router}, engines, router


def _cases():
    yield from _BOARDS
    yield "fixture", None


@pytest.mark.parametrize("board, stem", list(_cases()))
def test_router_pairs_equal_engine_pairs(tmp_path, capsys, board, stem):
    if stem is None:
        nets = {"X_POS", "X_NEG", "SIG"}
        class_map = _high_speed_map("X_POS", "X_NEG", "SIG")
    else:
        nets = board_net_names(REPO / "boards" / board / "output" / f"{stem}.kicad_pcb") or set()
        class_map = _board_map(board)
    router, engines, pairs = _pair_sets(nets, class_map)
    assert router, "fixture must carry at least one coupled pair"
    assert router == engines
    # kct and kicad-cli agree on each pair's 0.127 mm P/N gap: exempt.
    for i, (p, n) in enumerate(pairs):
        work = tmp_path / str(i)
        work.mkdir()
        pcb = _pair_board(work, p, n, 0.127)
        _route_sidecars(pcb, p, n)
        assert not _kct_errors(pcb, work), (p, n)
        if _has_kicad_cli():
            assert not _kicad_cli_clearance(pcb), (p, n)
    capsys.readouterr()


@pytest.mark.parametrize("board, stem", _BOARDS)
def test_kicad_named_boards_keep_byte_identical_sidecars(tmp_path, capsys, board, stem):
    """Boards 03/06 only carry KiCad-named pairs: no record, same bytes."""
    from kicad_tools.cli import route_cmd

    src = REPO / "boards" / board / "output"
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copyfile(src / f"{stem}{suffix}", tmp_path / f"{stem}{suffix}")
    pcb = tmp_path / f"{stem}.kicad_pcb"
    project = json.loads(pcb.with_suffix(".kicad_pro").read_text())
    manufacturer = project["meta"]["manufacturer"]
    layers = int(project["meta"].get("layers", 2))
    class_map = _board_map(board)
    pairs = route_cmd._router_coupled_pairs(pcb, class_map)
    assert pairs and not recorded_router_coupled_pairs(
        apply_router_coupled_pairs({}, pairs, board_net_names(pcb))
    )

    route_cmd._write_drc_constraint_sidecars(
        pcb, manufacturer, layers, quiet=True, net_class_map=class_map
    )
    before = pcb.with_suffix(".kicad_pro").read_bytes(), pcb.with_suffix(".kicad_dru").read_bytes()
    route_cmd._write_drc_constraint_sidecars(
        pcb, manufacturer, layers, quiet=True, net_class_map=class_map, coupled_pairs=pairs
    )
    after = pcb.with_suffix(".kicad_pro").read_bytes(), pcb.with_suffix(".kicad_dru").read_bytes()
    capsys.readouterr()
    assert after == before
    assert ROUTER_DIFF_PAIRS_META_KEY not in _project(pcb)["meta"]
    assert b"isCoupledDiffPair() ||" not in after[1]

    # Every router-class rule matches one in the committed routed .kicad_dru
    # (the fab-floor block depends on profile inputs this test does not pin).
    def router_rules(text: bytes) -> list[bytes]:
        return [line for line in text.splitlines() if b"isCoupledDiffPair" in line]

    committed = router_rules((src / f"{stem}.kicad_dru").read_bytes())
    assert committed and set(router_rules(after[1])) <= set(committed)


# ------------------------------------------------- idempotent and stale-safe


@pytest.mark.parametrize("mode", [None, "0"])
def test_rerouting_is_idempotent_and_drops_stale_records(tmp_path, mode):
    pcb = _pair_board(tmp_path, "X_POS", "X_NEG", 0.127)
    _route_sidecars(pcb, "X_POS", "X_NEG", mode=mode)
    first = pcb.with_suffix(".kicad_pro").read_bytes(), pcb.with_suffix(".kicad_dru").read_bytes()
    assert b"A.NetName == 'X_NEG'" in first[1]
    _route_sidecars(pcb, "X_POS", "X_NEG", mode=mode)
    assert (
        pcb.with_suffix(".kicad_pro").read_bytes(),
        pcb.with_suffix(".kicad_dru").read_bytes(),
    ) == first

    # The pair is no longer coupled: the record and its DRU terms go away.
    off = NetClassRouting(name="HighSpeed", clearance=0.15, coupled_routing=False)
    _route_sidecars(pcb, "X_POS", "X_NEG", mode=mode, net_class_map={"X_POS": off, "X_NEG": off})
    assert ROUTER_DIFF_PAIRS_META_KEY not in _project(pcb)["meta"]
    assert "NetName" not in pcb.with_suffix(".kicad_dru").read_text()

    # ``None`` (no router map this pass) keeps the previous record.
    _route_sidecars(pcb, "X_POS", "X_NEG", mode=mode)
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", preserve_board_rules=mode)
    assert recorded_router_coupled_pairs(_project(pcb)) == [("X_POS", "X_NEG")]


# ------------------------------------------------------------------ warning


class _Router:
    def __init__(self, nets: list[str]):
        self.net_names = dict(enumerate(nets, start=1))
        self.net_class_map = _high_speed_map(*nets)


@pytest.mark.parametrize(
    "nets, warned",
    [(["X_POS", "X_NEG"], True), (["TX_P", "TX_N"], False)],
)
def test_route_warns_once_per_unrecognised_coupled_pair(
    tmp_path, capsys, monkeypatch, nets, warned
):
    from kicad_tools.cli import route_cmd

    calls: dict = {}

    def fake(output_path, manufacturer, layers, **kwargs):
        calls.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(route_cmd, "_write_drc_constraint_sidecars", fake)
    pcb = _pair_board(tmp_path, *nets, 0.127)
    router = _Router(nets)
    with pytest.raises(RuntimeError, match="stop"):
        route_cmd.run_post_route_drc(
            pcb, "jlcpcb", 2, net_class_map=router.net_class_map, router=router
        )
    out = capsys.readouterr().out
    assert calls["coupled_pairs"] == [tuple(nets)]
    lines = [line for line in out.splitlines() if "does not recognise it as a pair" in line]
    if warned:
        assert len(lines) == 1
        assert "X_POS/X_NEG" in lines[0] and "uppercase 'P'/'N'" in lines[0]
    else:
        assert not lines


def test_warning_respects_quiet(capsys):
    from kicad_tools.cli import route_cmd

    route_cmd._warn_unrecognised_coupled_pairs([("X_POS", "X_NEG")], quiet=True)
    assert not capsys.readouterr().out
