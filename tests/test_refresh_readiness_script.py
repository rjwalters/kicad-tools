"""``scripts/boards/refresh_readiness.py`` refuses to re-pin on a failed gate (Issue #6076)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "boards" / "refresh_readiness.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("refresh_readiness_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_deterministic_zip_is_order_independent(mod, tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    mod.write_deterministic_zip(a, {"z.txt": b"z", "a/b.txt": b"ab"})
    mod.write_deterministic_zip(b, {"a/b.txt": b"ab", "z.txt": b"z"})
    assert a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as z:
        assert z.namelist() == ["a/b.txt", "z.txt"]
        assert {i.date_time for i in z.infolist()} == {mod.ZIP_DATE_TIME}


def test_compare_metrics_flags_only_recorded_differences(mod):
    recorded = {"drc_violations": 0, "nets_routed_pct": 100.0, "lvs_clean": True}
    assert mod.compare_metrics(recorded, {**recorded, "lvs_mismatches": 3}) == []
    assert mod.compare_metrics(recorded, {**recorded, "nets_routed_pct": 97.3}) == [
        "nets_routed_pct"
    ]


def _fake_board(root: Path) -> Path:
    board = root / "99-fake"
    mfg = board / "output" / "manufacturing"
    mfg.mkdir(parents=True)
    (board / "output" / "board.kicad_sch").write_text("sch")
    (board / "output" / "board_routed.kicad_pcb").write_text("pcb")
    (board / "output" / "erc_report.json").write_text("{}")
    for name in ("kicad_project.zip", "design-source.zip"):
        with zipfile.ZipFile(mfg / name, "w") as z:
            z.writestr("output/board.kicad_sch", "old")
    (mfg / "manifest.json").write_text(json.dumps({"version": "1.0", "files": {}}) + "\n")
    with zipfile.ZipFile(board / "output" / "manufacturing.zip", "w") as z:
        z.writestr("manifest.json", "old")
    report = {
        "status": "ready",
        "blockers": [],
        "metrics": {"drc_violations": 0, "nets_routed_pct": 100.0, "lvs_clean": True},
        "inputs": {"output/board.kicad_sch": "0" * 64},
        "checked_at": "2026-01-01T00:00:00+00:00",
    }
    (board / "output" / "readiness.json").write_text(json.dumps(report, indent=2) + "\n")
    return board


def test_metric_regression_publishes_nothing(mod, tmp_path, monkeypatch):
    board = _fake_board(tmp_path)
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    monkeypatch.setitem(
        mod.BOARDS,
        board.name,
        mod.BoardConfig(
            routed_pcb="output/board_routed.kicad_pcb",
            schematic="output/board.kicad_sch",
            tier="jlcpcb",
            erc=(mod.ErcJob("x.json", "output", "board.kicad_sch", ("output/erc_report.json",)),),
            archives=(
                ("output/manufacturing/kicad_project.zip", (".",)),
                ("output/manufacturing/design-source.zip", (".",)),
            ),
        ),
    )
    clean_erc = json.dumps({"sheets": [{"violations": []}], "date": "now"}).encode()
    monkeypatch.setattr(mod, "native_erc", lambda *a, **k: clean_erc)
    monkeypatch.setattr(mod, "gate_native_drc", lambda *a: (0, b"{}"))
    monkeypatch.setattr(mod, "gate_kct_check", lambda *a: (True, 0))
    monkeypatch.setattr(mod, "gate_net_status", lambda *a: 97.3)  # one net lost
    monkeypatch.setattr(mod, "gate_sync", lambda *a: None)

    with pytest.raises(mod.GateFailure, match="nets_routed_pct"):
        mod.refresh(board, "2026-10-09T00:00:00+00:00")
    after = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    assert after == before


def test_failed_gate_publishes_nothing(mod, tmp_path, monkeypatch):
    board = _fake_board(tmp_path)
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    monkeypatch.setitem(
        mod.BOARDS,
        board.name,
        mod.BoardConfig(
            routed_pcb="output/board_routed.kicad_pcb",
            schematic="output/board.kicad_sch",
            tier="jlcpcb",
            erc=(),
            archives=(),
        ),
    )

    def drc_fails(*_):
        raise mod.GateFailure("native DRC has errors or unconnected items")

    monkeypatch.setattr(mod, "gate_native_drc", drc_fails)
    with pytest.raises(mod.GateFailure, match="native DRC"):
        mod.refresh(board, "2026-10-09T00:00:00+00:00")
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file()} == before


# --------------------------------------------------------------------------- Issue #6269
_CHECK_SCRIPT = """\
import json, sys
from pathlib import Path

warnings = int(Path(__file__).with_name("warnings.txt").read_text())
meta = {k: {"status": "PASSED"} for k in ("drc", "erc", "manifest")}
meta["lvs"] = {"status": "PASSED", "mismatches": [], "copper_mismatches": []}
report = {"summary": {"errors": 0, "warnings": warnings}, "meta_checks": meta}
Path(sys.argv[2]).write_text(json.dumps(report))
sys.exit(2 if warnings else 0)
"""


def _hooked_board(mod, tmp_path, monkeypatch, warnings: int) -> Path:
    board = _fake_board(tmp_path)
    (board / "check_board.py").write_text(_CHECK_SCRIPT)
    (board / "warnings.txt").write_text(str(warnings))
    import kicad_tools.cli.board_readiness as board_readiness

    # read_readiness() itself is covered elsewhere; this fake report is minimal.
    monkeypatch.setattr(
        board_readiness, "read_readiness", lambda _b: {"status": "ready", "blockers": []}
    )
    monkeypatch.setitem(
        mod.BOARDS,
        board.name,
        mod.BoardConfig(
            routed_pcb="output/board_routed.kicad_pcb",
            schematic="output/board.kicad_sch",
            tier="jlcpcb-tier1",
            erc=(),
            check_script="check_board.py",
            archives=(),
        ),
    )

    def generic_check_must_not_run(*_):
        raise AssertionError("kct check ran although the board has a check hook")

    monkeypatch.setattr(mod, "gate_kct_check", generic_check_must_not_run)
    monkeypatch.setattr(mod, "gate_native_drc", lambda *a: (0, b"{}"))
    monkeypatch.setattr(mod, "gate_net_status", lambda *a: 100.0)
    monkeypatch.setattr(mod, "gate_sync", lambda *a: None)
    return board


def test_board_check_hook_replaces_kct_check(mod, tmp_path, monkeypatch):
    board = _hooked_board(mod, tmp_path, monkeypatch, warnings=0)
    published = mod.refresh(board, "2026-10-14T00:00:00+00:00", dry_run=True)
    assert "output/readiness.json" in published


def test_refusing_board_check_hook_publishes_nothing(mod, tmp_path, monkeypatch):
    board = _hooked_board(mod, tmp_path, monkeypatch, warnings=1)
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    with pytest.raises(mod.GateFailure, match="check_board.py failed"):
        mod.refresh(board, "2026-10-14T00:00:00+00:00")
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    ("data", "ok"),
    [
        ({"summary": {"errors": 0, "warnings": 0}}, True),
        ({"summary": {"errors": 1, "warnings": 0}}, False),
        ({"summary": {"errors": 0, "warnings": 2}}, False),
        ({"summary": {}}, False),  # a report without counts never passes
    ],
)
def test_check_report_verdict_requires_clean_summary(mod, data, ok):
    meta = {k: {"status": "PASSED"} for k in ("drc", "erc", "lvs", "manifest")}
    report = {**data, "meta_checks": meta}
    if ok:
        assert mod.check_report_verdict(report, "x") == (True, 0)
    else:
        with pytest.raises(mod.GateFailure):
            mod.check_report_verdict(report, "x")
    meta["manifest"] = {"status": "FAILED"}
    with pytest.raises(mod.GateFailure):
        mod.check_report_verdict(report, "x")


def test_board04_uses_its_own_manufacturing_check(mod):
    cfg = mod.BOARDS["04-stm32-devboard"]
    assert cfg.check_script == "check_manufacturing.py"
    board = mod.REPO_ROOT / "boards" / "04-stm32-devboard"
    assert (board / cfg.check_script).is_file()
    assert (board / cfg.routed_pcb).is_file() and (board / cfg.schematic).is_file()


# --------------------------------------------------------------------------- Issue #7261
def _warned_report(**counts: int) -> dict:
    meta = {k: {"status": "PASSED"} for k in ("drc", "erc", "lvs", "manifest")}
    violations = [
        {"rule_id": rule, "severity": "warning"} for rule, n in counts.items() for _ in range(n)
    ]
    summary = {"errors": 0, "warnings": len(violations)}
    return {"summary": summary, "meta_checks": meta, "violations": violations}


def test_known_issue_allowance_tolerates_exactly_the_recorded_warnings(mod):
    allowance = {"thermal_asymmetry": 9}
    assert mod.check_report_verdict(_warned_report(thermal_asymmetry=9), "x", allowance)
    for report in (
        _warned_report(thermal_asymmetry=10),  # more than recorded
        _warned_report(thermal_asymmetry=1, via_at_pad_edge=1),  # unlisted rule
    ):
        with pytest.raises(mod.GateFailure):
            mod.check_report_verdict(report, "x", allowance)
    mismatched = _warned_report(thermal_asymmetry=1)
    mismatched["summary"]["warnings"] = 2  # summary disagrees with findings
    with pytest.raises(mod.GateFailure):
        mod.check_report_verdict(mismatched, "x", {"thermal_asymmetry": 5})


def test_allowance_comes_from_the_boards_recorded_known_issues(mod, tmp_path):
    board = tmp_path / "board"
    (board / "output").mkdir(parents=True)
    entry = {
        "issue": 7220,
        "kind": "advisory",
        "summary": "s",
        "recorded": "2026-10-18",
        "warnings": {"thermal_asymmetry": 9},
    }
    (board / "output" / "readiness.json").write_text(json.dumps({"known_issues": [entry]}))
    assert mod.known_issue_allowance(board) == {"thermal_asymmetry": 9}
    (board / "output" / "readiness.json").write_text(json.dumps({}))
    assert mod.known_issue_allowance(board) == {}


_NO_REFRESH_CONFIG = {"03-usb-joystick"}


def test_every_demo_board_has_a_refresh_config(mod):
    boards = sorted(p.name for p in (mod.REPO_ROOT / "boards").glob("0[0-7]-*") if p.is_dir())
    assert len(boards) == 8
    assert not _NO_REFRESH_CONFIG & set(mod.BOARDS)  # stale exemption
    boards = [b for b in boards if b not in _NO_REFRESH_CONFIG]
    for name in boards:
        cfg = mod.BOARDS[name]
        board = mod.REPO_ROOT / "boards" / name
        assert (board / cfg.routed_pcb).is_file(), name
        assert (board / cfg.schematic).is_file(), name
        for archive, _ in cfg.archives:
            assert (board / archive).is_file(), (name, archive)
        for job in cfg.erc:
            assert (board / job.directory / job.schematic).is_file(), (name, job.key)


def test_outer_archive_members_resolve_against_committed_tree(mod, tmp_path):
    """Rebuilding ``output/manufacturing.zip`` finds every member (needs no KiCad).

    Boards 00-02 prefix members with ``manufacturing/``; a search root that
    ignores that refused them all (Issue #7273 review).
    """

    for name, cfg in mod.BOARDS.items():
        board = mod.REPO_ROOT / "boards" / name
        if not (board / "output" / "manufacturing.zip").is_file():
            continue
        staged = tmp_path / name
        shutil.copytree(board / "output", staged / "output")
        mod.rebuild_archive(
            staged / "output" / "manufacturing.zip", [staged / s for s in cfg.bundle_search]
        )


# ---------------------------------------------------------------- known issues (#7323)
CHECKED_AT = "2026-10-18T00:00:00+00:00"
ADVISORY = "7220:one-sided thermal:advisory:thermal_asymmetry=1"


def _evidence(board: Path) -> Path:
    return board / "output" / "readiness" / "known-issues.json"


def test_known_issue_flag_binds_the_evidence_file_into_inputs(mod, tmp_path, monkeypatch):
    board = _hooked_board(mod, tmp_path, monkeypatch, warnings=0)
    published = mod.refresh(board, CHECKED_AT, known_issue=[ADVISORY])

    assert "output/readiness/known-issues.json" in published
    report = json.loads((board / "output" / "readiness.json").read_text())
    assert [e["issue"] for e in report["known_issues"]] == [7220]
    assert json.loads(_evidence(board).read_text()) == {"known_issues": report["known_issues"]}
    assert report["inputs"]["output/readiness/known-issues.json"] == mod.sha256(_evidence(board))


def test_resolve_known_issue_flag_drops_entry_file_and_binding(mod, tmp_path, monkeypatch):
    board = _hooked_board(mod, tmp_path, monkeypatch, warnings=0)
    mod.refresh(board, CHECKED_AT, known_issue=["7220:thermal:advisory"])
    assert _evidence(board).is_file()

    published = mod.refresh(board, CHECKED_AT, resolve_known_issue=["#7220"])

    assert "output/readiness/known-issues.json (removed)" in published
    report = json.loads((board / "output" / "readiness.json").read_text())
    assert "known_issues" not in report
    assert "output/readiness/known-issues.json" not in report["inputs"]
    assert not _evidence(board).exists()


def test_resolving_an_unrecorded_issue_publishes_nothing(mod, tmp_path, monkeypatch):
    board = _hooked_board(mod, tmp_path, monkeypatch, warnings=0)
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    with pytest.raises(mod.GateFailure, match="not recorded on this board"):
        mod.refresh(board, CHECKED_AT, resolve_known_issue=["7220"])
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file()} == before


def test_dry_run_with_known_issue_leaves_the_board_untouched(mod, tmp_path, monkeypatch):
    board = _hooked_board(mod, tmp_path, monkeypatch, warnings=0)
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    mod.refresh(board, CHECKED_AT, dry_run=True, known_issue=["7220:thermal:advisory"])
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file()} == before


# ---------------------------------------------------------------------------
# Issue #7337: saved copper must equal an independent refill.
# ---------------------------------------------------------------------------


def test_fill_divergence_uses_the_readiness_tolerance(mod):
    saved = {"B.Cu": 2258.871182, "F.Cu": 1438.71334}
    assert mod.fill_divergence(saved, dict(saved), 0.01) == {}
    assert mod.fill_divergence(saved, {**saved, "F.Cu": 1438.72334}, 0.01) == {}
    assert mod.fill_divergence(saved, {"B.Cu": 2268.243206, "F.Cu": 1457.428272}, 0.01) == {
        "B.Cu": 9.372024,
        "F.Cu": 18.714932,
    }
    assert mod.fill_divergence(saved, {"B.Cu": 2258.871182}, 0.01) == {"F.Cu": 1438.71334}


def _fill_board(tmp_path: Path):
    board = tmp_path / "board"
    (board / "output").mkdir(parents=True)
    (board / "output" / "x_routed.kicad_pcb").write_text("(kicad_pcb)")
    return board


@pytest.mark.parametrize("refilled_f", [1438.71334, 1457.428272])
def test_gate_fill_identity_refuses_a_divergent_saved_fill(mod, tmp_path, monkeypatch, refilled_f):
    from kicad_tools.cli import readiness_cmd

    board = _fill_board(tmp_path)
    cfg = mod.BoardConfig(
        routed_pcb="output/x_routed.kicad_pcb",
        schematic="output/x.kicad_sch",
        tier="jlcpcb",
        erc=(),
    )
    saved_pcb = board / cfg.routed_pcb
    monkeypatch.setattr(
        readiness_cmd, "_refill_and_save", lambda pcb: readiness_cmd.EngineRun(ok=True)
    )
    monkeypatch.setattr(
        readiness_cmd,
        "_fill_areas",
        lambda pcb: {"F.Cu": 1438.71334} if pcb == saved_pcb else {"F.Cu": refilled_f},
    )
    if refilled_f == 1438.71334:
        assert mod.gate_fill_identity(board, cfg) == {}
    else:
        with pytest.raises(mod.GateFailure, match=r"F\.Cu 18\.714932 mm\^2"):
            mod.gate_fill_identity(board, cfg)


def test_native_drc_gate_runs_the_fill_identity_gate(mod, tmp_path, monkeypatch):
    """A clean refilled DRC alone no longer certifies the saved fill."""
    board = _fill_board(tmp_path)
    cfg = mod.BoardConfig(
        routed_pcb="output/x_routed.kicad_pcb",
        schematic="output/x.kicad_sch",
        tier="jlcpcb",
        erc=(),
    )

    def fake_drc(cmd, **_):
        Path(cmd[cmd.index("--output") + 1]).write_text(
            json.dumps({"violations": [], "unconnected_items": []})
        )

    def divergent(*_):
        raise mod.GateFailure("saved copper differs from an independent refill: F.Cu 1 mm^2")

    monkeypatch.setattr(mod, "run", fake_drc)
    monkeypatch.setattr(mod, "gate_fill_identity", divergent)
    with pytest.raises(mod.GateFailure, match="independent refill"):
        mod.gate_native_drc(board, cfg)


# --------------------------------------------------------------------------- Issue #7283
def test_drc_report_copies_are_written_and_recorded_in_report_sources(mod, tmp_path, monkeypatch):
    board = _fake_board(tmp_path)
    (board / "output" / "drc_report.json").write_text("{}")
    (board / "output" / "manufacturing" / "native-drc.json").write_text("{}")
    import kicad_tools.cli.board_readiness as board_readiness

    monkeypatch.setattr(
        board_readiness, "read_readiness", lambda _b: {"status": "ready", "blockers": []}
    )
    targets = ("output/drc_report.json", "output/manufacturing/native-drc.json")
    monkeypatch.setitem(
        mod.BOARDS,
        board.name,
        mod.BoardConfig(
            routed_pcb="output/board_routed.kicad_pcb",
            schematic="output/board.kicad_sch",
            tier="jlcpcb",
            erc=(),
            archives=(),
            drc_targets=targets,
        ),
    )
    staged_report = b'{"violations": [], "unconnected_items": []}'
    monkeypatch.setattr(mod, "gate_native_drc", lambda *a: (0, staged_report))
    monkeypatch.setattr(mod, "gate_kct_check", lambda *a: (True, 0))
    monkeypatch.setattr(mod, "gate_net_status", lambda *a: 100.0)
    monkeypatch.setattr(mod, "gate_sync", lambda *a: None)

    mod.refresh(board, "2026-10-18T00:00:00+00:00")

    pcb_sha = mod.sha256(board / "output" / "board_routed.kicad_pcb")
    report = json.loads((board / "output" / "readiness.json").read_text())
    assert report["report_sources"] == dict.fromkeys(targets, pcb_sha)
    for target in targets:
        assert (board / target).read_bytes() == staged_report


def test_demo_boards_with_drc_provenance_target_the_report_binding_reads(mod):
    for name in ("05-bldc-motor-controller", "06-diffpair-test", "07-matchgroup-test"):
        assert "output/drc_report.json" in mod.BOARDS[name].drc_targets


# ------------------------------------------------------------ repin_staged (#7353)
def test_repin_staged_gates_and_publishes_a_staged_copy(mod, tmp_path, monkeypatch):
    """``repin_staged`` re-pins a caller-staged copy; ``after_manifest`` runs between re-hashes."""
    board = _hooked_board(mod, tmp_path, monkeypatch, warnings=0)
    cfg = mod.BOARDS[board.name]
    staged = tmp_path / "staged" / board.name
    shutil.copytree(board, staged)
    rehashes: list[str] = []
    monkeypatch.setattr(mod, "load_refresher", lambda *a: lambda mfg: rehashes.append("hash"))

    def after_manifest() -> None:
        rehashes.append("after")

    published = mod.repin_staged(
        board, staged, cfg, CHECKED_AT, dry_run=True, after_manifest=after_manifest
    )

    assert rehashes == ["hash", "after", "hash"]
    assert "output/readiness.json" in published
    assert (
        json.loads((staged / "output" / "readiness.json").read_text())["checked_at"] == CHECKED_AT
    )
    # dry_run: the committed board is untouched
    assert json.loads((board / "output" / "readiness.json").read_text())["checked_at"] != CHECKED_AT
