"""Diff-pair benchmark harness and router/checker threshold parity (Issue #6442).

Two things are pinned here, neither of which routes anything:

* ``scripts/research/diffpair_bench.py`` refuses inputs that already carry copper
  on a pair net and refuses saved-plan replay, so a pair result is always
  router-produced;
* the thresholds the router prints for a pair are the ones ``kct check``
  applies -- including on board 03, whose ``HighSpeed`` class has per-net
  variants (USB_D carries a 25 % continuity threshold, USB_MCU_D the default).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "research" / "diffpair_bench.py"
B03 = REPO_ROOT / "boards" / "03-usb-joystick" / "output"
B06 = REPO_ROOT / "boards" / "06-diffpair-test" / "output"


def _load():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("diffpair_bench_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bench = _load()
ere = bench.ere


# ---------------------------------------------------------------------------
# Router-produced guard
# ---------------------------------------------------------------------------


def test_committed_board_06_has_pair_copper_and_is_refused():
    ncm = bench.load_net_class_map(B06 / "net_class_map.json")
    with pytest.raises(bench.PairBenchError, match="LVDS1_P"):
        bench.assert_zero_pair_copper(B06 / "diffpair_test.kicad_pcb", ncm)


def test_06b_is_stripped_to_zero_pair_copper(tmp_path):
    inp, facts = bench.prepare_input(ere.BOARDS["06b"], tmp_path)
    assert facts["stripped_items"] > 0
    assert facts["pair_nets"] == sorted(f"LVDS{i}_{s}" for i in range(1, 5) for s in "NP")
    assert len(facts["input_sha256"]) == 64
    assert inp.is_file()
    # What survives is the plane copper, never a pair net.
    ncm = bench.load_net_class_map(B06 / "net_class_map.json")
    assert bench.assert_zero_pair_copper(inp, ncm)


def test_unstripped_06_row_is_refused_before_routing(tmp_path):
    unstripped = ere.Board(
        "06x",
        "boards/06-diffpair-test/output/diffpair_test.kicad_pcb",
        layers=4,
        net_class_map="boards/06-diffpair-test/output/net_class_map.json",
    )
    with pytest.raises(bench.PairBenchError, match="already has copper on pair nets"):
        bench.prepare_input(unstripped, tmp_path)
    row = bench.run_cell(unstripped, "lattice", 42, 1, tmp_path)
    assert row["valid"] is False
    assert "input has pair-net copper" in row["invalid_reasons"]
    assert "routed" not in row  # nothing was routed


def test_boards_03_and_06rf_inputs_are_clean(tmp_path):
    for key in ("03", "06rf"):
        _inp, facts = bench.prepare_input(ere.BOARDS[key], tmp_path / key)
        assert facts["pair_nets"], key


@pytest.mark.parametrize("flag", bench.REPLAY_FLAGS)
def test_saved_plan_replay_flags_are_refused(flag):
    with pytest.raises(bench.PairBenchError, match="replayed routing plan"):
        bench.check_no_replay(["--seed", "42", flag, "plan.json"])
    with pytest.raises(bench.PairBenchError):
        bench.check_no_replay([f"{flag}=plan.json"])


def test_engine_commands_carry_no_replay_flag():
    for engine in bench.ENGINES:
        bench.check_no_replay(bench.engine_args(engine, 4))
    assert "--deterministic-rescue" in bench.engine_args("grid", 4)
    assert bench.engine_args("lattice", 4)[:4] == [
        "--route-engine",
        "lattice",
        "--strategy",
        "basic",
    ]


def test_cli_extra_replay_flag_is_refused(tmp_path):
    with pytest.raises(bench.PairBenchError):
        bench.main(
            ["run", "--label", "x", "--work-dir", str(tmp_path), "--extra", "--resume p.kicad_pcb"]
        )


def test_replayed_committed_copper_is_detected():
    """A 'routed' file whose pair copper is the committed copper is not router-produced."""
    board = ere.BOARDS["06b"]
    ncm = bench.load_net_class_map(B06 / "net_class_map.json")
    nets = bench.pair_net_names(
        __import__("kicad_tools.schema.pcb", fromlist=["PCB"]).PCB.load(
            str(B06 / "diffpair_test.kicad_pcb")
        ),
        ncm,
    )
    match = bench.committed_copper_matches(B06 / "diffpair_test_routed.kicad_pcb", board, nets)
    assert match is not None and match.endswith("diffpair_test.kicad_pcb")


def test_replay_log_markers_are_recognised():
    assert bench._REPLAY_LOG.search("Applying saved routing plan from x.json")
    assert bench._REPLAY_LOG.search("routing_plan.apply_plan(...)")
    assert not bench._REPLAY_LOG.search("Diff pairs (lattice): 1/2 coupled")


def test_parse_pair_log_handles_chained_and_plain_logs():
    chained = (
        "  Diff pairs (lattice): 3/4 coupled\n"
        "    LVDS1: partially-coupled:1/2\n"
        "      LVDS1[0] U1.7->R1.1: uncoupled-fallback:pair-partner-pad\n"
        "      LVDS1[1] R1.1->U2.2: coupled\n"
        "    LVDS2: coupled\n"
        "      LVDS2[0] U3.7->R2.1: coupled\n"
        "  Diff-pair thresholds (as applied by kct check):\n"
        "    LVDS1_P/LVDS1_N: continuity threshold 80.0%, skew tolerance 0.100 mm\n"
    )
    parsed = bench.parse_pair_log(chained)
    assert parsed["outcomes"] == {"LVDS1": "partially-coupled:1/2", "LVDS2": "coupled"}
    assert parsed["segments"]["LVDS1"][0]["outcome"] == "uncoupled-fallback:pair-partner-pad"
    assert parsed["log_thresholds"]["LVDS1_P/LVDS1_N"] == {"continuity_pct": 80.0, "skew_mm": 0.1}
    plain = "  Diff pairs (lattice): 1/2 coupled\n    USB_D: uncoupled-fallback:pair-no-path\n    USB_MCU_D: coupled\n"
    assert bench.parse_pair_log(plain)["outcomes"]["USB_MCU_D"] == "coupled"


def test_compare_flags_differing_pair_metrics():
    a = {"rows": [{"board": "03", "engine": "grid", "seed": 42, "pair_digest": "aa"}]}
    same = {"rows": [{"board": "03", "engine": "grid", "seed": 42, "pair_digest": "aa"}]}
    diff = {"rows": [{"board": "03", "engine": "grid", "seed": 42, "pair_digest": "bb"}]}
    assert bench.compare(a, same) == [("03/grid/s42/committed", True)]
    assert bench.compare(a, diff) == [("03/grid/s42/committed", False)]


def test_route_subcommand_refuses_pair_bench_rows(tmp_path):
    with pytest.raises(SystemExit, match="pairs"):
        ere.main(["route", "--boards", "06b", "--work-dir", str(tmp_path)])


# ---------------------------------------------------------------------------
# Router <-> checker threshold parity (board 03 USB_D)
# ---------------------------------------------------------------------------


def _board03():
    from kicad_tools.router.rules import net_class_map_from_path
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(B03 / "usb_joystick_routed.kicad_pcb"))
    ncm = net_class_map_from_path(
        B03 / "net_class_map.json", pcb_path=B03 / "usb_joystick_routed.kicad_pcb"
    )
    return pcb, ncm


def test_board03_class_has_per_net_variants():
    """The precondition: same class name, different per-net variants.

    The variants no longer differ by ``coupled_continuity_threshold`` (removed
    from board 03 by #6443: continuity is info-only); each net still names its
    own partner, which is enough to make the class-name alias ambiguous.
    """
    _pcb, ncm = _board03()
    assert ncm["USB_D+"].name == ncm["USB_MCU_D+"].name == "HighSpeed"
    assert ncm["USB_D+"] != ncm["USB_MCU_D+"]
    assert ncm["USB_D+"].diffpair_partner == "USB_D-"
    assert ncm["USB_MCU_D+"].diffpair_partner == "USB_MCU_D-"
    assert ncm["USB_D+"].coupled_continuity_threshold is None


def test_build_synth_routing_does_not_collapse_variants():
    from kicad_tools.router.diffpair_detection import build_synth_routing

    _pcb, ncm = _board03()
    synth, net_to_class = build_synth_routing(ncm)
    assert net_to_class["USB_D+"] == "HighSpeed"
    # The old ``setdefault`` idiom left an arbitrary variant under "HighSpeed".
    assert "HighSpeed" not in synth
    # ``Power`` is ambiguous too (VCC is a pour net, VBUS is not).
    assert "Power" not in synth
    assert synth["USB_D+"] is ncm["USB_D+"]


def test_build_synth_routing_keeps_alias_for_identical_members():
    from kicad_tools.router.diffpair_detection import build_synth_routing

    _pcb, ncm = _board03()
    same = {"A": ncm["USB_D+"], "B": ncm["USB_D+"]}
    synth, _ = build_synth_routing(same)
    assert synth["HighSpeed"] is ncm["USB_D+"]


def test_router_report_and_kct_check_agree_on_board03_thresholds():
    from kicad_tools.router.core import Autorouter
    from kicad_tools.validate.diffpair_engagement import (
        derive_engagement_state,
        resolve_pair_thresholds,
    )
    from kicad_tools.validate.diffpair_skew import derive_skew_data

    pcb, ncm = _board03()
    names = {n.number: n.name for n in pcb.nets.values() if n.name}

    # What the router prints.
    stub = SimpleNamespace(net_names=names, net_class_map=ncm)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        Autorouter._print_pair_threshold_report(stub)
    report = buf.getvalue()

    # What kct check applies, per pair.
    engaged, continuity = derive_engagement_state(pcb, ncm)
    _skew, skew_tol = derive_skew_data(pcb, ncm, board_thickness_mm=1.6, num_copper_layers=4)
    ids = {n.name: n.number for n in pcb.nets.values()}
    assert engaged  # both USB pairs engage

    usb_d = (ids["USB_D+"], ids["USB_D-"])
    mcu = (ids["USB_MCU_D+"], ids["USB_MCU_D-"])
    # Issue #6443 removed board 03's 0.25 override: continuity is info-only.
    assert continuity[tuple(sorted(usb_d))] == 0.7
    assert continuity[tuple(sorted(mcu))] == 0.7

    for (p, n), key in (
        (("USB_D+", "USB_D-"), tuple(sorted(usb_d))),
        (("USB_MCU_D+", "USB_MCU_D-"), tuple(sorted(mcu))),
    ):
        line = next(ln for ln in report.splitlines() if ln.strip().startswith(f"{p}/{n}:"))
        assert f"continuity threshold {continuity[key] * 100:.1f}%" in line
        assert f"skew tolerance {skew_tol[key]:.3f} mm" in line
        assert resolve_pair_thresholds(ncm, p, n).continuity == continuity[key]
    assert "USB_D+/USB_D-: continuity threshold 70.0%" in report

    # Issue #6443: both pairs are declared USB2_FS, so the router report and
    # kct check quote 2.5 mm, and both name where the number came from.
    from kicad_tools.validate import DRCChecker

    checker = DRCChecker(
        pcb, manufacturer="jlcpcb-tier1", layers=4, net_class_map=ncm, verbose=True
    )
    findings = {v.nets: v for v in checker.check_diffpair_length_skew().violations}
    for p, n in (("USB_D+", "USB_D-"), ("USB_MCU_D+", "USB_MCU_D-")):
        line = next(ln for ln in report.splitlines() if ln.strip().startswith(f"{p}/{n}:"))
        assert "skew tolerance 2.500 mm [protocol:USB2_FS (Renesas" in line
        assert line.rstrip().endswith("; error]")
        got = resolve_pair_thresholds(ncm, p, n)
        assert (got.skew_mm, got.protocol, got.skew_severity) == (2.5, "USB2_FS", "error")
        finding = findings[tuple(sorted((p, n)))]
        assert finding.severity == "info"  # the committed copper passes
        assert "within tolerance 2.500 mm [protocol:USB2_FS (Renesas" in finding.message


def test_board03_bench_rows_carry_the_limit_and_the_stretch_figure():
    """Issue #6443: 2.5 mm is the gate, 0.5 mm is still reported (P5 stretch)."""
    _pcb, ncm = _board03()
    rows = {
        r["pair"]: r for r in bench.measure_pairs(B03 / "usb_joystick_routed.kicad_pcb", ncm, 4)
    }
    assert set(rows) == {"USB_D", "USB_MCU_D"}
    for row in rows.values():
        assert row["skew_tolerance_mm"] == 2.5
        assert row["protocol"] == "USB2_FS"
        assert row["skew_source"].startswith("protocol:USB2_FS (Renesas")
        assert (row["skew_severity"], row["skew_pass"]) == ("error", True)
        assert (row["skew_stretch_mm"], row["skew_stretch_pass"]) == (0.5, True)
        assert row["max_uncoupled_per_terminal_mm"] == 6.35
        assert row["max_vias_per_net"] == 4
    assert rows["USB_D"]["skew_mm"] == pytest.approx(0.166, abs=0.001)
    table = bench.render_pairs(
        [{"board": "03", "engine": "-", "seed": 0, "pairs": list(rows.values())}]
    )
    assert "| 2.5 | protocol:USB2_FS (Renesas" in table
    assert "pass (0.5)" in table


def test_limit_descriptors_do_not_change_the_pair_digest():
    pair = {"pair": "X", "skew_mm": 0.1, "skew_tolerance_mm": 0.5}
    described = dict(pair, protocol="LVDS", skew_source="protocol:LVDS (uncited)", skew_pass=True)
    assert bench.pair_digest([pair]) == bench.pair_digest([described])


def test_resolver_ignores_a_collapsed_class_key():
    """Even with a poisoned class-name key, the net-name key decides."""
    from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds

    _pcb, ncm = _board03()
    poisoned = dict(ncm)
    from dataclasses import replace

    # A different variant under the class name; the net-name key still decides.
    poisoned["HighSpeed"] = replace(ncm["USB_MCU_D+"], skew_tolerance_mm=9.0)
    assert resolve_pair_thresholds(poisoned, "USB_D+", "USB_D-").skew_mm == 2.5


def test_resolver_falls_back_to_negative_net_then_defaults():
    from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds

    _pcb, ncm = _board03()
    only_n = {"USB_D-": ncm["USB_D-"]}
    got = resolve_pair_thresholds(only_n, "USB_D+", "USB_D-")
    assert (got.continuity, got.source_net) == (0.7, "USB_D-")
    bare = resolve_pair_thresholds({}, "X_P", "X_N")
    assert (bare.continuity, bare.skew_mm, bare.source_net) == (0.7, 0.5, None)
    assert (bare.protocol, bare.skew_source, bare.skew_severity) == (
        None,
        "default:generic",
        "error",
    )


def test_determinism_and_merge():
    def row(rep, digest, board="03"):
        return {"board": board, "engine": "grid", "seed": 42, "rep": rep, "pair_digest": digest}

    doc = {"rows": [row(0, "a"), row(1, "a"), row(0, "x", "06b"), row(1, "y", "06b")]}
    assert bench.determinism(doc) == [
        ("03/grid/s42/committed", 2, True),
        ("06b/grid/s42/committed", 2, False),
    ]
    merged = bench.merge([{"rows": [row(0, "a"), row(0, "b", "06b")]}, {"rows": [row(0, "z")]}])
    assert {(r["board"], r["pair_digest"]) for r in merged["rows"]} == {("03", "z"), ("06b", "b")}


# ---------------------------------------------------------------------------
# Issue #6535: 06b lattice keeps coupled copper on BOTH nets of its pairs
# ---------------------------------------------------------------------------

#: The committed fix-branch baseline this guard pins (``diffpair_bench run --boards
#: 03,06b --engines lattice --seeds 42 --label fix-6535``, committed net-class map).
BASELINES_6535 = sorted(
    (REPO_ROOT / "data" / "research" / "diffpair_bench").glob("fix-6535-*.json")
)
COUPLED = ("coupled", "partially-coupled")


def _coupled_pairs_with_copper(pairs: list[dict]) -> list[str]:
    """Pairs counted as coupled whose BOTH nets carry copper and a coupled run."""
    good = []
    for p in pairs:
        if not str(p.get("outcome") or "").startswith(COUPLED):
            continue
        routed = p.get("routed_mm") or [0, 0]
        assert min(routed) > 0, f"{p['pair']} is '{p['outcome']}' with no copper on a net: {routed}"
        assert (p.get("coupled_trunk_mm") or 0) > 0, f"{p['pair']} has no coupled run"
        good.append(p["pair"])
    return good


def test_committed_06b_lattice_row_couples_with_copper_on_both_nets():
    """The committed baseline: >= 3 of 4 LVDS pairs coupled, each with copper on
    both nets (the #6440 baseline's "3 coupled" had 0 mm on every N net)."""
    import json

    assert len(BASELINES_6535) == 1, BASELINES_6535
    doc = json.loads(BASELINES_6535[0].read_text())
    rows = [
        r
        for r in doc["rows"]
        if (r["board"], r["engine"], r["seed"]) == ("06b", "lattice", 42)
        and r.get("net_class_map_mode", "committed") == "committed"
    ]
    assert rows, "no 06b lattice committed-map row"
    for row in rows:
        assert row["valid"] is True
        assert len(_coupled_pairs_with_copper(row["pairs"])) >= 3
        assert row["kicad_drc_unconnected"] <= 4  # main before #6535: 4
        assert (row.get("kct_check_by_rule") or {}).get("connectivity", 0) <= 2


@pytest.mark.slow
def test_06b_lattice_route_couples_with_copper_on_both_nets(tmp_path):
    """Live guard: route 06b with the lattice exactly as the bench does and fail if
    no pair couples, a coupled pair lacks copper on one net, or the post-route
    short check removes an LVDS net (the double-copper signature of #6535)."""
    row = bench.run_cell(ere.BOARDS["06b"], "lattice", 42, 2400, tmp_path)
    assert row["valid"] is True, row.get("invalid_reasons")
    log = next(tmp_path.rglob("route.log")).read_text()
    assert "Post-optimize backstop demoted" not in log
    assert "routed by both the lattice and an earlier pass" not in log
    assert len(_coupled_pairs_with_copper(row["pairs"])) >= 1  # target (and today): 3


# ---------------------------------------------------------------------------
# Issue #6475: harness fixes
# ---------------------------------------------------------------------------


def test_replay_flags_are_real_kct_route_flags():
    from kicad_tools.cli.route_cmd import _route_parser

    known = set(_route_parser()._option_string_actions)
    assert bench.REPLAY_FLAGS
    assert "--resume" in bench.REPLAY_FLAGS
    for flag in bench.REPLAY_FLAGS:
        assert flag in known, f"{flag} is not a kct route flag"


@pytest.mark.parametrize(
    "args",
    [
        ["--resume", "x.kicad_pcb"],
        ["--resume=x.kicad_pcb"],
        ["--resu", "x.kicad_pcb"],
        ["--resu=x.kicad_pcb"],
    ],
)
def test_resume_and_its_abbreviation_are_refused(args):
    with pytest.raises(bench.PairBenchError):
        bench.check_no_replay(args)


def test_unrelated_flags_are_not_refused():
    bench.check_no_replay(["--seed", "42", "--route-engine", "grid", "--rescue-x"])


def test_extra_reaches_kct_route_and_the_row_cmd(tmp_path, monkeypatch):
    seen = {}

    def fake_run_route(inp, out, extra, timeout, log):
        seen["extra"] = list(extra)
        log.write_text("")
        return {"exit_code": 0, "cmd": ["kct", "route", *extra]}

    monkeypatch.setattr(ere, "run_route", fake_run_route)
    monkeypatch.setattr(ere, "pick_artifact", lambda *a, **k: (None, None))
    row = bench.run_cell(
        ere.BOARDS["06b"],
        "grid",
        42,
        10.0,
        tmp_path,
        extra_args=["--checkpoint-interval", "0"],
    )
    assert seen["extra"][-2:] == ["--checkpoint-interval", "0"]
    assert "--checkpoint-interval" in row["cmd"]


def test_extra_resume_is_refused_in_run_cell(tmp_path):
    row = bench.run_cell(
        ere.BOARDS["06b"], "grid", 42, 10.0, tmp_path, extra_args=["--resu", "x.kicad_pcb"]
    )
    assert row["valid"] is False
    assert row["invalid_reasons"] == ["replay flag"]


@pytest.mark.parametrize("code", [1, 5])
def test_crashed_route_row_is_invalid_and_not_final(tmp_path, monkeypatch, code):
    def fake_run_route(inp, out, extra, timeout, log):
        out.write_text("(kicad_pcb)")
        log.write_text("")
        return {"exit_code": code, "cmd": []}

    monkeypatch.setattr(ere, "run_route", fake_run_route)
    monkeypatch.setattr(ere, "grade", lambda *a, **k: {})
    monkeypatch.setattr(ere, "manufacturable", lambda *a, **k: False)
    monkeypatch.setattr(ere, "find_net_class_map", lambda *a, **k: None)
    monkeypatch.setattr(bench, "load_net_class_map", lambda *a, **k: {})
    monkeypatch.setattr(bench, "measure_pairs", lambda *a, **k: [])
    monkeypatch.setattr(bench, "committed_copper_matches", lambda *a, **k: None)
    row = bench.run_cell(ere.BOARDS["06b"], "grid", 42, 10.0, tmp_path)
    assert row["artifact"] == "checkpoint"
    assert row["valid"] is False
    assert any(f"exited {code}" in r for r in row["invalid_reasons"])


def test_pick_artifact_labels_by_exit_code(tmp_path):
    out = tmp_path / "o.kicad_pcb"
    out.write_text("x")
    assert ere.pick_artifact(out, 0)[1] == "final"
    assert ere.pick_artifact(out, 3)[1] == "final"
    assert ere.pick_artifact(out, 1)[1] == "checkpoint"
    assert ere.pick_artifact(out)[1] == "final"
