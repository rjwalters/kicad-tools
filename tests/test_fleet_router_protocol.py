"""Fleet-router protocol semantics (Issue #6334).

``benchmarks/external/fleet_routers/run.py`` drives TraceMaker, fastroute and
``kct route`` on the demo fleet. These tests pin the properties that keep that
comparison honest, using fake router executables and small fixtures (no
upstream build, no network):

* the manifest matches the tree, with 08's exclusion and 09's no-DRU marker;
* one copper-stripping policy that never touches nested forms;
* outputs are graded beside the FROZEN rules, never tool-emitted ones;
* nonzero DRC / unconnected counts stay visible, a missing referee is
  ``unavailable`` and can never yield a clean pass;
* the DSN audit names omitted project/DRU/keepout/zone constraints, and an SES
  round-trip keeps placement and pad nets;
* a router that writes a partial board, spawns a child and hangs is a
  ``timeout`` with both processes gone; reruns never see stale artifacts;
* moved footprints / rewritten pad nets make a row invalid.

The driver is not a package module, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "benchmarks" / "external" / "fleet_routers" / "run.py"
BOARD00 = REPO_ROOT / "boards" / "00-simple-led" / "output" / "simple_led.kicad_pcb"


def _load():
    name = "fleet_routers_run_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # @dataclass resolves annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fr():
    return _load()


@pytest.fixture(scope="module")
def manifest(fr):
    return fr.load_manifest()


@pytest.fixture
def prep(fr, manifest, tmp_path):
    board = next(b for b in manifest["boards"] if b["key"] == "00")
    return fr.prepare_board(board, tmp_path / "input")


@pytest.fixture
def board00(manifest):
    return next(b for b in manifest["boards"] if b["key"] == "00")


def _fake_exe(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return path


def _fake_kicad_cli(
    tmp_path: Path, violations: list[dict], unconnected: int, write: bool = True
) -> Path:
    """A kicad-cli stand-in: records the rules it was graded against."""
    payload = {"violations": violations, "unconnected_items": [{}] * unconnected}
    return _fake_exe(
        tmp_path / "fake-kicad-cli",
        f"""
        import json, pathlib, shutil, sys
        args = sys.argv[1:]
        pcb = pathlib.Path(args[-1])
        out = pathlib.Path(args[args.index("--output") + 1])
        seen = {{s: (pcb.with_suffix(s).read_text() if pcb.with_suffix(s).exists() else None)
                for s in (".kicad_pro", ".kicad_dru")}}
        (out.parent / "seen_rules.json").write_text(json.dumps(seen))
        if {write!r}:
            out.write_text({json.dumps(payload)!r})
        """,
    )


@pytest.fixture
def stub_referees(fr, monkeypatch):
    """kct check / sync stand-ins so scoring tests do not need the full engine."""
    monkeypatch.setattr(
        fr, "kct_check", lambda pcb, layers: {"ran": True, "passed": True, "error_count": 0}
    )
    monkeypatch.setattr(fr, "kct_sync", lambda pcb, sch: {"ran": True, "in_sync": True})


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


REFREEZE = (
    "a fleet input changed: re-freeze deliberately with "
    "`uv run python benchmarks/external/fleet_routers/run.py freeze` and commit manifest.json"
)


def test_manifest_hashes_match_tree(fr, manifest):
    assert fr.verify_manifest(manifest) == [], REFREEZE


def test_manifest_is_exactly_what_freeze_produces(fr, manifest):
    """Hand edits or a forgotten re-freeze cannot slip through."""
    assert manifest["boards"] == fr.freeze_all(), REFREEZE


def test_manifest_covers_fleet_with_explicit_markers(manifest):
    boards = {b["key"]: b for b in manifest["boards"]}
    assert sorted(boards) == [f"{i:02d}" for i in range(10)]
    assert boards["08"]["status"] == "excluded"
    assert "no committed .kicad_pcb" in boards["08"]["exclusion_reason"]
    nine = boards["09"]
    assert nine["status"] == "included"
    assert nine["dru"]["path"] is None and nine["dru"]["provenance"] == "absent"
    assert nine["input_copper"]["segments"] > 0  # pre-routed: needs the strip policy
    for key in ("00", "01", "02", "03", "04", "05", "06", "07"):
        assert boards[key]["dru"]["provenance"] == "kct-managed fab floors", key
        assert boards[key]["dru"]["path"].endswith("_routed.kicad_dru")


def test_manifest_pins_match_ecosystem_registry(manifest):
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    reg = tomllib.loads((REPO_ROOT / "src/kicad_tools/ecosystem/data/projects.toml").read_text())[
        "projects"
    ]
    for tool in ("tracemaker", "fastroute"):
        assert manifest["pins"][tool]["commit"] == reg[tool]["pinned_commit"]


def test_verify_detects_drift(fr, manifest, tmp_path):
    drifted = json.loads(json.dumps(manifest))
    drifted["boards"][0]["dru"]["sha256"] = "0" * 64
    assert any("hash drifted" in p for p in fr.verify_manifest(drifted))


# ---------------------------------------------------------------------------
# Stripping policy
# ---------------------------------------------------------------------------

PCB_SNIPPET = """(kicad_pcb
  (net 0 "")
  (gr_text "has (segment) in a \\"string\\"" (at 1 1) (layer "F.SilkS"))
  (segment (start 0 0) (end 1 1) (width 0.25) (layer "F.Cu") (net 1))
  (arc (start 0 0) (mid 1 1) (end 2 0) (width 0.25) (layer "F.Cu") (net 1))
  (via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (zone (net 1) (polygon (pts (arc (start 0 0) (mid 1 1) (end 2 0)) (xy 0 0))))
)
"""


def test_strip_removes_only_top_level_copper(fr):
    out, counts = fr.strip_copper(PCB_SNIPPET)
    assert counts == {"segment": 1, "arc": 1, "via": 1}
    assert "(segment (start" not in out and "(via (at" not in out
    assert "(pts (arc (start 0 0)" in out  # nested zone arc untouched
    assert 'has (segment) in a \\"string\\"' in out  # string content untouched
    assert out.count("\n") == PCB_SNIPPET.count("\n") - 3


def test_prepare_keeps_input_bytes_when_nothing_to_strip(fr, prep):
    assert prep.stripped == {"segment": 0, "arc": 0, "via": 0}
    assert prep.pcb.read_bytes() == BOARD00.read_bytes()
    assert (prep.frozen_dir / "simple_led.kicad_dru").exists()


# ---------------------------------------------------------------------------
# Frozen referee
# ---------------------------------------------------------------------------


def test_tool_emitted_rules_cannot_relax_referee(fr, prep, tmp_path, stub_referees):
    out_dir = tmp_path / "tool"
    out_dir.mkdir()
    out = out_dir / "simple_led_routed.kicad_pcb"
    out.write_text(prep.pcb.read_text())
    # The tool emits a relaxed rule set beside its output.
    out.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "x" (constraint clearance (min 0.01mm)))\n'
    )
    out.with_suffix(".kicad_pro").write_text("{}")
    cli = _fake_kicad_cli(tmp_path, [], 0)
    res = fr.score(out, prep, 2, tmp_path / "score", kicad_cli=str(cli))
    seen = json.loads((tmp_path / "score" / "seen_rules.json").read_text())
    assert seen[".kicad_dru"] == (prep.frozen_dir / "simple_led.kicad_dru").read_text()
    assert seen[".kicad_pro"] == (prep.frozen_dir / "simple_led.kicad_pro").read_text()
    assert res["graded_rules_sha256"][".kicad_dru"] == fr.sha256_file(
        prep.frozen_dir / "simple_led.kicad_dru"
    )


def test_nonzero_drc_and_unconnected_stay_visible(fr, prep, tmp_path, stub_referees):
    viol = [
        {"severity": "error", "type": "clearance"},
        {"severity": "error", "type": "clearance"},
        {"severity": "warning", "type": "silk_overlap"},
    ]
    cli = _fake_kicad_cli(tmp_path, viol, 2)
    res = fr.score(prep.pcb, prep, 2, tmp_path / "score", kicad_cli=str(cli))
    assert res["drc"]["errors_total"] == 2
    assert res["drc"]["warnings"] == 1
    assert res["drc"]["unconnected"] == 2
    assert res["clean_pass"] is False


def test_missing_drc_json_is_unavailable_not_zero(fr, prep, tmp_path, stub_referees):
    cli = _fake_kicad_cli(tmp_path, [], 0, write=False)
    res = fr.score(prep.pcb, prep, 2, tmp_path / "score", kicad_cli=str(cli))
    assert res["drc"]["ran"] is False
    assert "errors_total" not in res["drc"]
    assert res["clean_pass"] is not True


def test_missing_scorer_executable_is_unavailable(fr, tmp_path):
    res = fr.kicad_cli_drc(BOARD00, tmp_path / "drc.json", kicad_cli=str(tmp_path / "nope"))
    assert res["ran"] is False
    assert fr.kct_sync(BOARD00, None)["ran"] is False


@pytest.mark.parametrize(
    ("drc", "chk", "sync", "expected"),
    [
        ({"ran": True, "errors_total": 0, "unconnected": 0}, {"ran": True, "passed": True}, {"ran": True, "in_sync": True}, True),
        ({"ran": True, "errors_total": 0, "unconnected": 0}, {"ran": True, "passed": True}, {"ran": False}, None),
        ({"ran": False}, {"ran": True, "passed": True}, {"ran": True, "in_sync": True}, None),
        ({"ran": False}, {"ran": True, "passed": False}, {"ran": False}, False),
        ({"ran": True, "errors_total": 0, "unconnected": 0}, {"ran": True, "passed": True}, {"ran": True, "in_sync": False}, False),
    ],
)  # fmt: skip
def test_clean_pass_requires_every_referee(fr, drc, chk, sync, expected):
    comp = {"connections_routed": 3, "connections_total": 3}
    assert (
        fr.clean_pass({"drc": drc, "kct_check": chk, "sync": sync, "completion": comp}) is expected
    )


# ---------------------------------------------------------------------------
# DSN / SES boundary
# ---------------------------------------------------------------------------

KEEPOUT = """  (zone (net 0) (net_name "") (layers "F.Cu") (uuid "a0000000-0000-0000-0000-000000000001") (name "ko")
    (hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25)
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 150 80) (xy 152 80) (xy 152 82) (xy 150 82))))
  (zone (net 3) (net_name "GND") (layer "B.Cu") (uuid "a0000000-0000-0000-0000-000000000002")
    (hatch edge 0.5) (connect_pads (clearance 0.2)) (min_thickness 0.25)
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 137 78) (xy 160 78) (xy 160 97) (xy 137 97))))
  (gr_text "CU" (at 158 95) (layer "F.Cu") (effects (font (size 1 1) (thickness 0.15))))
"""


@pytest.fixture
def rule_bearing(tmp_path):
    """Board 00 plus a keepout, a GND pour, copper text, a second net class, a DRU."""
    d = tmp_path / "rb"
    d.mkdir()
    text = BOARD00.read_text()
    cut = text.rstrip().rfind(")")
    pcb = d / "rb.kicad_pcb"
    pcb.write_text(text[:cut] + KEEPOUT + ")\n")
    pro = json.loads(BOARD00.with_suffix(".kicad_pro").read_text())
    pro["net_settings"]["classes"].append({"name": "Power", "clearance": 0.3, "track_width": 0.5})
    pro["net_settings"]["netclass_patterns"] = [{"netclass": "Power", "pattern": "VCC"}]
    (d / "rb.kicad_pro").write_text(json.dumps(pro))
    (d / "rb.kicad_dru").write_text(
        '(version 1)\n(rule "Edge" (constraint edge_clearance (min 0.5mm)))\n'
        '(rule "Clear" (constraint clearance (min 0.1mm)))\n'
    )
    return pcb


def test_dsn_audit_detects_omitted_constraints(fr, rule_bearing):
    lost = fr.dsn_audit(
        rule_bearing,
        rule_bearing.with_suffix(".kicad_pro"),
        rule_bearing.with_suffix(".kicad_dru"),
        rule_bearing.with_suffix(".dsn"),
    )
    kinds = {e["kind"] for e in lost}
    assert {
        "netclass",
        "netclass_assignment",
        "netclass_rule",
        "dru",
        "keepout",
        "zone",
        "copper_text",
    } <= kinds
    details = " ".join(e["detail"] for e in lost)
    assert "'Power'" in details
    assert "edge_clearance" in details
    # A DRU floor the DSN's global clearance already satisfies is not a loss.
    assert "'Clear'" not in details
    assert rule_bearing.with_suffix(".dsn").exists()


def test_dsn_audit_without_project_is_a_loss(fr, prep, tmp_path):
    lost = fr.dsn_audit(prep.pcb, None, None, tmp_path / "x.dsn")
    assert {"kind": "project", "detail": "no frozen .kicad_pro: project rules unknown"} in lost


INLINE_SES = """(session simple_led
  (routes
    (resolution um 1)
    (network_out
      (wire (path F.Cu 250 148500 85500 150000 85500 156000 87500) (net "VCC"))
      (via "Via[0-1]_Pad600_um" 150000 85500 (net "VCC"))
    )
  )
)
"""

NESTED_SES = """(session simple_led
  (routes
    (resolution um 10)
    (network_out
      (net VCC
        (wire (path F.Cu 2500 1485000 855000 1500000 855000))
        (wire (path F.Cu 2500 1500000 855000 1410000 862300))
      )
    )
  )
)
"""


def test_ses_round_trip_preserves_placement_and_pad_nets(fr, prep, tmp_path):
    ses = tmp_path / "b.ses"
    ses.write_text(INLINE_SES)
    out = tmp_path / "routed.kicad_pcb"
    lost, counts = fr.ses_import(ses, prep.pcb, out)
    assert lost == []
    assert counts["imported_wires"] == 1 and counts["imported_vias"] == 1
    before, after = fr.fingerprint(prep.pcb), fr.fingerprint(out)
    assert fr.compare_fingerprints(before, after) == []
    assert after["pad_nets"] == before["pad_nets"]
    assert out.read_text().count("(segment ") == 2


def test_ses_import_loss_is_reported_not_hidden(fr, prep, tmp_path):
    """Standard Freerouting nesting: our importer drops it; the audit must say so."""
    ses = tmp_path / "b.ses"
    ses.write_text(NESTED_SES)
    out = tmp_path / "routed.kicad_pcb"
    lost, counts = fr.ses_import(ses, prep.pcb, out)
    assert counts["ses"]["wires"] == 2
    details = [e["detail"] for e in lost]
    if counts["imported_wires"] < 2:  # today: 0 (issue #6593)
        assert f"{counts['imported_wires']}/2 SES wires imported" in details
    assert any("resolution um 10" in d for d in details)
    assert out.exists()  # the merge base is always written, even with 0 routes


# ---------------------------------------------------------------------------
# Adapters: outcomes, timeouts, stale artifacts, placement
# ---------------------------------------------------------------------------

BUDGETS = {"wall_cap_s": 30, "threads": 1, "seed": 1, "tracemaker_work": 1000}


def _tm(tmp_path: Path, body: str):
    fr = _load()
    exe = _fake_exe(tmp_path / "fake-tracemaker", body)
    return fr.ToolSpec(name="tracemaker", executable=exe)


def _running(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return False
    try:
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc to check for survivors")
def test_partial_board_then_hang_is_timeout_and_reaped(fr, prep, board00, tmp_path):
    pids = tmp_path / "pids.txt"
    spec = _tm(
        tmp_path,
        f"""
        import os, subprocess, sys, time, shutil
        args = sys.argv[1:]
        out = args[args.index("-o") + 1]
        shutil.copy(args[1], out)  # a partial board on disk
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{child.pid}}")
        time.sleep(600)
        """,
    )
    budgets = dict(BUDGETS, wall_cap_s=3)
    rec = fr.run_tool("tracemaker", spec, prep, board00, tmp_path / "tm", budgets)
    assert rec.outcome == "timeout"
    assert rec.timed_out is True
    assert rec.score is None and rec.output is None
    assert rec.partial_artifact and Path(rec.partial_artifact).exists()
    parent, child = (int(p) for p in pids.read_text().split())
    deadline = time.monotonic() + 5
    while (_running(parent) or _running(child)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _running(parent) and not _running(child)


def test_fastroute_partial_ses_then_hang_is_timeout(fr, prep, board00, tmp_path):
    exe = _fake_exe(
        tmp_path / "fake-fastroute",
        """
        import sys, time
        args = sys.argv[1:]
        open(args[args.index("-do") + 1], "w").write("(session x (routes (network_out)))")
        time.sleep(600)
        """,
    )
    spec = fr.ToolSpec(name="fastroute", executable=exe)
    rec = fr.run_tool(
        "fastroute", spec, prep, board00, tmp_path / "fr", dict(BUDGETS, wall_cap_s=2)
    )
    assert rec.outcome == "timeout"
    assert rec.partial_artifact and rec.partial_artifact.endswith(".ses")
    assert rec.score is None
    assert rec.lost_constraints  # the DSN audit ran before routing


def test_rerun_cannot_reuse_stale_artifacts(fr, prep, board00, tmp_path):
    base = tmp_path / "tm"
    stale = base / "run" / "simple_led_routed.kicad_pcb"
    stale.parent.mkdir(parents=True)
    stale.write_text(prep.pcb.read_text())
    spec = _tm(tmp_path, "import sys; sys.exit(0)\n")
    rec = fr.run_tool("tracemaker", spec, prep, board00, base, BUDGETS)
    assert rec.outcome == "missing-output"
    assert not stale.exists()


def test_nonzero_exit_without_output_is_tool_failure(fr, prep, board00, tmp_path):
    spec = _tm(tmp_path, "import sys; sys.exit(3)\n")
    rec = fr.run_tool("tracemaker", spec, prep, board00, tmp_path / "tm", BUDGETS)
    assert (rec.outcome, rec.stage, rec.exit_code) == ("tool-failure", "exec", 3)


def test_failed_build_is_recorded_with_log(fr, prep, board00, tmp_path):
    log = tmp_path / "build.log"
    log.write_text("CMake Error: nlohmann_json not found\n")
    spec = fr.ToolSpec(name="tracemaker", executable=tmp_path / "missing", build_log=log)
    rec = fr.run_tool("tracemaker", spec, prep, board00, tmp_path / "tm", BUDGETS)
    assert (rec.outcome, rec.stage) == ("tool-failure", "build")
    assert Path(rec.log).read_text() == log.read_text()


def test_pin_mismatch_refuses_to_run(fr, prep, board00, tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    spec = _tm(tmp_path, "import sys; sys.exit(0)\n")
    spec.checkout, spec.pin = repo, "89ff58ca86b2104ac6c55ae8a5dc77e4ab3e8d00"
    rec = fr.run_tool("tracemaker", spec, prep, board00, tmp_path / "tm", BUDGETS)
    assert (rec.outcome, rec.stage) == ("tool-failure", "pin")


def test_moved_footprint_is_invalid_fixed_placement(fr, prep, board00, tmp_path):
    spec = _tm(
        tmp_path,
        """
        import sys
        args = sys.argv[1:]
        text = open(args[1]).read().replace("(at 148.5 85.5 0)", "(at 149.5 85.5 0)", 1)
        text = text.replace('(net 2 "LED_ANODE") (pintype', '(net 1 "VCC") (pintype', 1)
        open(args[args.index("-o") + 1], "w").write(text)
        """,
    )
    rec = fr.run_tool("tracemaker", spec, prep, board00, tmp_path / "tm", BUDGETS)
    assert rec.outcome == "invalid-fixed-placement"
    joined = " ".join(rec.placement_violations)
    assert "footprints: R1" in joined
    assert "pad_nets: R1:2" in joined
    assert rec.score is None


def test_kct_refuses_without_worktree_native_backend(fr, prep, board00, tmp_path, monkeypatch):
    from kicad_tools.benchmark.external import metrics

    monkeypatch.setattr(
        metrics,
        "probe_backend",
        lambda: metrics.BackendInfo(backend="python", available=False, unavailable_reason="test"),
    )
    rec = fr.run_tool("kct", None, prep, board00, tmp_path / "kct", BUDGETS)
    assert (rec.outcome, rec.stage) == ("tool-failure", "native-backend")


def test_outcome_vocabulary_is_closed(fr):
    assert len(set(fr.OUTCOMES)) == len(fr.OUTCOMES)
    rec = fr.RunRecord(tool="x", board="00", timed_out=True, exit_code=-1)
    assert fr.classify(rec, output_exists=True) == "timeout"
    assert fr.classify(fr.RunRecord(tool="x", board="00", exit_code=0), False) == "missing-output"
    assert fr.classify(fr.RunRecord(tool="x", board="00", exit_code=2), False) == "tool-failure"
    assert fr.classify(fr.RunRecord(tool="x", board="00", exit_code=0), True) is None


def test_work_dir_inside_repo_is_refused(fr, tmp_path):
    assert fr.main(["run", "--work-dir", str(REPO_ROOT / "tmp-fleet"), "--tools", ""]) == 2
    assert not (REPO_ROOT / "tmp-fleet").exists()
