"""``kct route-link`` per-link DRC + creepage gate and rollback (Issue #8314)."""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli import kicad_toolchain, main, route_link_cmd  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402

# Two independent links, each pad pair 10 mm apart on its own row.
BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIGA")
  (net 2 "SIGB")
  (gr_rect (start 100 100) (end 130 130) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000001") (at 110 105)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIGA")))
  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000002") (at 120 105)
    (fp_text reference "R2" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIGA")))
  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000003") (at 110 120)
    (fp_text reference "R3" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIGB")))
  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000004") (at 120 120)
    (fp_text reference "R4" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIGB")))
)
"""
LINK_A = "SIGA R1.1 R2.1"
LINK_B = "SIGB R3.1 R4.1"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(BOARD)
    return p


@pytest.fixture
def toolchain_ok(monkeypatch):
    monkeypatch.setattr(
        kicad_toolchain, "evidence_toolchain", lambda *a, **k: contextlib.nullcontext()
    )


def _fake_gate(monkeypatch, *, bad_nets=(), census_bad_nets=()):
    """DRC/census stand-ins keyed on which nets have copper in the board file."""

    def state(path, args):
        pcb = PCB.load(str(path))
        routed = {pcb.nets[s.net_number].name for s in pcb.segments if s.net_number in pcb.nets}
        drc = Counter()
        for net in bad_nets:
            if net in routed:
                drc[("clearance", "error", (net,))] += 1
        census = None
        if census_bad_nets:
            census = {
                ("HV", net, "conductor", "board"): 1.0 for net in census_bad_nets if net in routed
            }
        return drc, census

    monkeypatch.setattr(route_link_cmd, "_gate_state", state)


def _run(capsys, *argv: str):
    rc = main(["route-link", "--format", "json", *argv])
    return rc, json.loads(capsys.readouterr().out)


def test_clean_links_all_commit(board, capsys, toolchain_ok, monkeypatch):
    _fake_gate(monkeypatch)
    rc, doc = _run(capsys, str(board), "--link", LINK_A, "--link", LINK_B)
    assert rc == 0 and doc["ok"]
    assert [e["status"] for e in doc["links"]] == ["committed", "committed"]
    assert all(e["failed_check"] is None for e in doc["links"])


def test_creepage_violating_link_rolls_back_byte_exactly(board, capsys, toolchain_ok, monkeypatch):
    # Reference: board bytes after committing only link A.
    ref = board.parent / "ref.kicad_pcb"
    ref.write_text(BOARD)
    _fake_gate(monkeypatch)
    assert main(["route-link", "--format", "json", str(ref), "--link", LINK_A]) == 0
    capsys.readouterr()

    _fake_gate(monkeypatch, census_bad_nets=["SIGB"])
    rc, doc = _run(capsys, str(board), "--link", LINK_A, "--link", LINK_B, "--min", "5")
    assert rc == 1 and not doc["ok"]
    first, second = doc["links"]
    assert first["status"] == "committed"
    assert second["status"] == "rolled-back" and second["failed_check"] == "creepage"
    assert board.read_bytes() == ref.read_bytes()


def test_drc_violating_link_rolled_back_later_link_still_runs(
    board, capsys, toolchain_ok, monkeypatch
):
    _fake_gate(monkeypatch, bad_nets=["SIGA"])
    rc, doc = _run(capsys, str(board), "--link", LINK_A, "--link", LINK_B)
    assert rc == 1
    a, b = doc["links"]
    assert a["status"] == "rolled-back" and a["failed_check"] == "drc"
    assert b["status"] == "committed"
    pcb = PCB.load(str(board))
    assert not [s for s in pcb.segments if s.net_number == 1]
    assert [s for s in pcb.segments if s.net_number == 2]


def test_unroutable_link_exits_nonzero(board, capsys, toolchain_ok, monkeypatch):
    _fake_gate(monkeypatch)
    rc, doc = _run(capsys, str(board), "--link", "SIGA R1.1 R9.9", "--link", LINK_B)
    assert rc == 1
    assert [e["status"] for e in doc["links"]] == ["unroutable", "committed"]


def test_kicad_cli_unavailable_refuses_and_writes_nothing(board, capsys, monkeypatch):
    def refuse(*a, **k):
        raise kicad_toolchain.KiCadEnvironmentError("kicad-cli not found")

    monkeypatch.setattr(kicad_toolchain, "evidence_toolchain", refuse)
    before = board.read_bytes()
    out = board.parent / "out.kicad_pcb"
    rc = main(["route-link", "--format", "json", str(board), "--link", LINK_A, "-o", str(out)])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 2 and doc["refused"]
    assert board.read_bytes() == before and not out.exists()


def test_baseline_gate_failure_refuses(board, capsys, toolchain_ok, monkeypatch):
    def boom(path, args):
        raise RuntimeError("drc exploded")

    monkeypatch.setattr(route_link_cmd, "_gate_state", boom)
    before = board.read_bytes()
    rc, doc = _run(capsys, str(board), "--link", LINK_A)
    assert rc == 2 and doc["refused"] and board.read_bytes() == before


def test_probe_is_ungated(board, capsys, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("probe must not touch the toolchain")

    monkeypatch.setattr(kicad_toolchain, "evidence_toolchain", refuse)
    before = board.read_bytes()
    rc, doc = _run(capsys, str(board), "--link", LINK_A, "--probe")
    assert rc == 0 and board.read_bytes() == before


def test_new_board_failure_not_masked_by_failing_same_footprint_pair(
    board, capsys, toolchain_ok, monkeypatch
):
    """Baseline already fails (HV, GND) via same_footprint; a new board-level one must roll back."""

    def state(path, args):
        pcb = PCB.load(str(path))
        routed = any(s.net_number == 1 for s in pcb.segments)
        census = {("HV", "GND", "conductor", "same_footprint"): 0.84}
        if routed:
            census[("HV", "GND", "conductor", "board")] = 1.87
        return Counter(), census

    monkeypatch.setattr(route_link_cmd, "_gate_state", state)
    rc, doc = _run(capsys, str(board), "--link", LINK_A, "--min", "3")
    assert rc == 1
    assert doc["links"][0]["status"] == "rolled-back"
    assert doc["links"][0]["failed_check"] == "creepage"


def test_already_failing_pair_made_worse_rolls_back():
    base = {("HV", "GND", "conductor", "board"): 2.0}
    assert route_link_cmd._new_census_failures({("HV", "GND", "conductor", "board"): 1.5}, base)
    assert not route_link_cmd._new_census_failures({("HV", "GND", "conductor", "board"): 2.0}, base)


def test_gate_crash_mid_run_reported_as_gate_error(board, capsys, toolchain_ok, monkeypatch):
    calls = {"n": 0}

    def state(path, args):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("kicad-cli crashed")
        return Counter(), None

    monkeypatch.setattr(route_link_cmd, "_gate_state", state)
    rc, doc = _run(capsys, str(board), "--link", LINK_A)
    assert rc == 1 and doc["links"][0]["failed_check"] == "gate-error"


def test_census_mode_reported_and_no_unchanged_copy(board, capsys, toolchain_ok, monkeypatch):
    _fake_gate(monkeypatch, bad_nets=["SIGA"])
    out = board.parent / "out.kicad_pcb"
    rc, doc = _run(capsys, str(board), "--link", LINK_A, "-o", str(out))
    assert rc == 1 and doc["census"] == "skipped" and doc["written"] is None
    assert not out.exists()


def test_census_failures_keys_on_relationship(board, monkeypatch):
    """The real ``_census_failures`` must keep same_footprint and board rows apart."""
    from types import SimpleNamespace

    from kicad_tools.creepage import engine

    def pair(rel, mm, passed=False):
        return SimpleNamespace(
            net_a="HV",
            net_b="GND",
            kind="conductor",
            relationship=rel,
            creepage_mm=mm,
            passed=passed,
        )

    report = SimpleNamespace(
        pairs=[pair("same_footprint", 0.84), pair("board", 1.87), pair("board", 5.0, True)]
    )
    monkeypatch.setattr(engine, "resolve_hv_nets", lambda *a, **k: {"HV"})
    monkeypatch.setattr(engine, "compute_creepage_census", lambda *a, **k: report)
    args = SimpleNamespace(min=3.0, net_class="HV", net_class_map=None)

    failing = route_link_cmd._census_failures(board, args)
    assert failing == {
        ("HV", "GND", "conductor", "same_footprint"): 0.84,
        ("HV", "GND", "conductor", "board"): 1.87,
    }
    # Baseline had only the same-footprint failure: the board one is new.
    base = {("HV", "GND", "conductor", "same_footprint"): 0.84}
    assert route_link_cmd._new_census_failures(failing, base) == [
        ("HV", "GND", "conductor", "board")
    ]
