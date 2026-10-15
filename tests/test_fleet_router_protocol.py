"""Fleet-router protocol semantics (Issue #6334).

``benchmarks/external/fleet_routers/run.py`` drives TraceMaker, fastroute and
``kct route`` on the demo fleet. These tests pin the properties that keep that
comparison honest, using fake router executables and small fixtures (no
upstream build, no network):

* freeze/verify/prepare catch drift (against a throwaway copy of board 00, so
  regenerating a committed board never fails this file), and the committed
  manifest keeps 08's exclusion and 09's no-DRU marker;
* one copper-stripping policy that never touches nested forms;
* outputs are graded beside the FROZEN rules, never tool-emitted ones;
* nonzero DRC / unconnected counts stay visible, a missing referee is
  ``unavailable`` and can never yield a clean pass;
* the DSN audit names omitted project/DRU/keepout/zone constraints, and an SES
  round-trip keeps placement and pad nets;
* a router that writes a partial board, spawns a child and hangs is a
  ``timeout`` with both processes gone; reruns never see stale artifacts;
* moved footprints / rewritten pad nets make a row invalid, including when the
  router also rewrites its own input copy.

The driver is not a package module, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "benchmarks" / "external" / "fleet_routers" / "run.py"
BOARD00_OUT = Path("boards") / "00-simple-led" / "output"


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


def _copy_board00(dest: Path) -> Path:
    """A throwaway repo holding only board 00's frozen inputs.

    Tests freeze and verify THIS tree, never the live ``boards/`` checkout, so
    regenerating a committed board file cannot fail this test file.
    """
    src = REPO_ROOT / BOARD00_OUT
    out = dest / BOARD00_OUT
    out.mkdir(parents=True)
    for name in (
        "simple_led.kicad_pcb",
        "simple_led.kicad_pro",
        "simple_led_routed.kicad_dru",
        "simple_led.kicad_sch",
    ):
        if (src / name).exists():
            shutil.copy2(src / name, out / name)
    return dest


@pytest.fixture
def tmp_repo(tmp_path):
    return _copy_board00(tmp_path / "repo")


@pytest.fixture
def board00(fr, tmp_repo):
    return fr.freeze_board("00", repo=tmp_repo)


@pytest.fixture
def prep(fr, board00, tmp_repo, tmp_path):
    return fr.prepare_board(board00, tmp_path / "input", repo=tmp_repo)


@pytest.fixture
def board00_pcb(tmp_repo):
    return tmp_repo / BOARD00_OUT / "simple_led.kicad_pcb"


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


def _frozen(fr, repo: Path) -> dict:
    return {"boards": fr.freeze_all(repo=repo)}


def test_freeze_then_verify_is_clean(fr, tmp_repo):
    manifest = _frozen(fr, tmp_repo)
    boards = {b["key"]: b for b in manifest["boards"]}
    assert boards["00"]["status"] == "included"
    assert boards["00"]["dru"]["provenance"] == "kct-managed fab floors"
    assert all(boards[k]["status"] == "excluded" for k in boards if k != "00")
    assert fr.verify_manifest(manifest, repo=tmp_repo) == []


@pytest.mark.parametrize(
    "name", ["simple_led.kicad_pcb", "simple_led.kicad_pro", "simple_led_routed.kicad_dru"]
)
def test_verify_detects_drift(fr, tmp_repo, name):
    manifest = _frozen(fr, tmp_repo)
    path = tmp_repo / BOARD00_OUT / name
    path.write_text(path.read_text() + "\n")  # e.g. a board regeneration
    problems = fr.verify_manifest(manifest, repo=tmp_repo)
    assert problems == [f"00: {(BOARD00_OUT / name).as_posix()} hash drifted"]


def test_verify_detects_missing_frozen_file(fr, tmp_repo):
    manifest = _frozen(fr, tmp_repo)
    (tmp_repo / BOARD00_OUT / "simple_led.kicad_sch").unlink()
    assert any("is missing" in p for p in fr.verify_manifest(manifest, repo=tmp_repo))


def test_verify_detects_broken_absence(fr, tmp_repo):
    """A recorded absence (09-style DRU, 08-style excluded board) must still hold."""
    dru = tmp_repo / BOARD00_OUT / "simple_led_routed.kicad_dru"
    dru_text = dru.read_text()
    dru.unlink()
    manifest = _frozen(fr, tmp_repo)
    assert manifest["boards"][0]["dru"] == {
        "path": None,
        "absent": True,
        "provenance": "absent",
        "note": "no .kicad_dru at this revision; graded on .kicad_pro alone",
    }
    assert fr.verify_manifest(manifest, repo=tmp_repo) == []
    dru.write_text(dru_text)
    excluded = tmp_repo / "boards/01-voltage-divider/output/voltage_divider.kicad_pcb"
    excluded.parent.mkdir(parents=True)
    excluded.write_text("(kicad_pcb)\n")
    problems = fr.verify_manifest(manifest, repo=tmp_repo)
    assert any(
        "simple_led_routed.kicad_dru now exists but is recorded absent" in p for p in problems
    )
    assert any(
        "voltage_divider.kicad_pcb now exists but the board is excluded" in p for p in problems
    )


def test_prepare_refuses_drifted_input(fr, board00, tmp_repo, tmp_path):
    pcb = tmp_repo / BOARD00_OUT / "simple_led.kicad_pcb"
    pcb.write_text(pcb.read_text() + "\n")
    with pytest.raises(RuntimeError, match="input hash drifted"):
        fr.prepare_board(board00, tmp_path / "input", repo=tmp_repo)


def test_run_refuses_on_manifest_drift(fr, tmp_repo, tmp_path, monkeypatch, capsys):
    path = tmp_path / "manifest.json"
    manifest = _frozen(fr, tmp_repo)
    manifest["boards"][0]["input"]["sha256"] = "0" * 64
    path.write_text(json.dumps(manifest))
    verify = fr.verify_manifest
    monkeypatch.setattr(fr, "verify_manifest", lambda m, repo=None: verify(m, repo=tmp_repo))
    assert fr.main(["--manifest", str(path), "verify"]) == 1
    rc = fr.main(["--manifest", str(path), "run", "--work-dir", str(tmp_path / "w"), "--tools", ""])
    assert rc == 2
    assert "manifest drift -- refusing to run" in capsys.readouterr().out


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


def test_prepare_keeps_input_bytes_when_nothing_to_strip(fr, prep, board00_pcb):
    if fr.strip_copper(board00_pcb.read_text())[1] != {"segment": 0, "arc": 0, "via": 0}:
        pytest.skip("board 00's committed input is no longer copper-free")
    assert prep.stripped == {"segment": 0, "arc": 0, "via": 0}
    assert prep.pcb.read_bytes() == board00_pcb.read_bytes()
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


def test_missing_scorer_executable_is_unavailable(fr, board00_pcb, tmp_path):
    res = fr.kicad_cli_drc(board00_pcb, tmp_path / "drc.json", kicad_cli=str(tmp_path / "nope"))
    assert res["ran"] is False
    assert fr.kct_sync(board00_pcb, None)["ran"] is False


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

# Issue #6642: since #6594 the exporter writes the KEEPOUT fixture's rule area
# and GND pour, so they are no longer losses.  These two still are: Specctra
# polygons have no arc form, so an arc-outlined rule area is dropped, and a
# copper zone without a net has no (plane ...) form -- yet both are copper a
# router must keep clear of.
OMITTED = """  (zone (net 0) (net_name "") (layers "B.Cu") (uuid "a0000000-0000-0000-0000-000000000003") (name "arcko")
    (hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25)
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed))
    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 140 80) (arc (start 142 80) (mid 143 81) (end 142 82)) (xy 140 82))))
  (zone (net 0) (net_name "") (layer "F.Cu") (uuid "a0000000-0000-0000-0000-000000000004")
    (hatch edge 0.5) (connect_pads (clearance 0.2)) (min_thickness 0.25)
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 154 90) (xy 156 90) (xy 156 92) (xy 154 92))))
"""


@pytest.fixture
def rule_bearing(tmp_path, board00_pcb):
    """Board 00 plus a keepout, a GND pour, copper text, a second net class, a DRU."""
    d = tmp_path / "rb"
    d.mkdir()
    text = board00_pcb.read_text()
    cut = text.rstrip().rfind(")")
    pcb = d / "rb.kicad_pcb"
    pcb.write_text(text[:cut] + KEEPOUT + OMITTED + ")\n")
    pro = json.loads(board00_pcb.with_suffix(".kicad_pro").read_text())
    pro["net_settings"]["classes"].append({"name": "Power", "clearance": 0.3, "track_width": 0.5})
    pro["net_settings"]["netclass_patterns"] = [{"netclass": "Power", "pattern": "VCC"}]
    (d / "rb.kicad_pro").write_text(json.dumps(pro))
    (d / "rb.kicad_dru").write_text(
        '(version 1)\n(rule "Edge" (constraint edge_clearance (min 0.5mm)))\n'
        '(rule "Clear" (constraint clearance (min 0.1mm)))\n'
    )
    return pcb


def test_dsn_audit_detects_omitted_constraints(fr, rule_bearing):
    not_routing: list = []
    lost = fr.dsn_audit(
        rule_bearing,
        rule_bearing.with_suffix(".kicad_pro"),
        rule_bearing.with_suffix(".kicad_dru"),
        rule_bearing.with_suffix(".dsn"),
        not_routing,
    )
    kinds = {e["kind"] for e in lost}
    assert {"keepout", "zone", "copper_text"} <= kinds
    details = " ".join(e["detail"] for e in lost)
    # Issue #6756: a fab-only rule is reported as not-routing, not as a loss.
    assert "dru" not in kinds and "edge_clearance" not in details
    assert [n["type"] for n in not_routing] == ["edge_clearance"]
    # Issue #6642: only the genuinely omitted rule area and zone are reported;
    # the plain keepout ("ko") and the GND pour are exported since #6594.
    keepouts = [e["detail"] for e in lost if e["kind"] == "keepout"]
    assert len(keepouts) == 1 and "'arcko'" in keepouts[0], keepouts
    zones = [e["detail"] for e in lost if e["kind"] == "zone"]
    assert zones == ["zone <no net>@F.Cu not exported"], zones
    # The Power class, its values and its VCC assignment are exported too.
    assert not kinds & {"netclass", "netclass_assignment"}, lost
    assert "'Power'" not in details and "Power." not in details, lost
    # A DRU floor the DSN's global clearance already satisfies is not a loss.
    assert "'Clear'" not in details
    assert rule_bearing.with_suffix(".dsn").exists()


def test_dsn_audit_detects_dropped_class_rules_and_membership(fr, rule_bearing):
    """Issue #6642: class checks read each emitted (class ...) block.

    The exporter now writes the Power class faithfully, so tamper with the DSN
    the audit reads (an existing ``dsn_path`` is not re-exported) to prove the
    netclass checks still fire on a genuinely degraded export.
    """
    from kicad_tools.export.dsn import KiCadToDSNExporter

    dsn = rule_bearing.with_suffix(".dsn")
    text = KiCadToDSNExporter(rule_bearing).export(dsn)
    start = text.index("(class Power")
    block = text[start : text.index("(rule", start)]
    power_rule = text[start:].index("(width 500.0)") + start
    text = text[:power_rule] + "(width 250.0)" + text[power_rule + len("(width 500.0)") :]
    dsn.write_text(text.replace(block, block.replace('"VCC"', "").replace("VCC", "")))

    def audit():
        return fr.dsn_audit(
            rule_bearing,
            rule_bearing.with_suffix(".kicad_pro"),
            rule_bearing.with_suffix(".kicad_dru"),
            dsn,
        )

    lost = audit()
    details = [e["detail"] for e in lost if e["kind"] == "netclass_rule"]
    assert any("Power.track_width 0.5 mm exported as 0.25 mm (relaxed)" in d for d in details)
    assigned = [e["detail"] for e in lost if e["kind"] == "netclass_assignment"]
    assert any("'VCC'" in d and "exported in none" in d for d in assigned), assigned

    # Dropping the class outright is a "netclass" loss: VCC is still assigned.
    stripped = re.sub(r"\n    \(class Power\b.*?\n    \)", "", text, count=1, flags=re.S)
    assert "(class Power" not in stripped
    dsn.write_text(stripped)
    assert {"kind": "netclass", "detail": "net class 'Power' not exported"} in audit()


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


def test_ses_import_nested_session_with_resolution_is_lossless(fr, prep, tmp_path):
    """Standard Freerouting nesting and ``(resolution um 10)`` import cleanly since #6593."""
    ses = tmp_path / "b.ses"
    ses.write_text(NESTED_SES)
    out = tmp_path / "routed.kicad_pcb"
    lost, counts = fr.ses_import(ses, prep.pcb, out)
    assert counts["ses"]["wires"] == 2
    assert counts["imported_wires"] == 2
    assert lost == []
    assert out.exists()


def test_ses_import_reports_dropped_wires_and_unapplied_resolution(fr, prep, tmp_path, monkeypatch):
    """A regression in the importer must still surface as a loss, not be hidden."""
    from kicad_tools.export.ses import SESToKiCadImporter

    real_parse = SESToKiCadImporter.parse

    def lossy_parse(self):
        real_parse(self)
        self._wires = self._wires[:1]
        self._resolution_unit, self._resolution = "um", 1.0

    monkeypatch.setattr(SESToKiCadImporter, "parse", lossy_parse)
    ses = tmp_path / "b.ses"
    ses.write_text(NESTED_SES)
    lost, _ = fr.ses_import(ses, prep.pcb, tmp_path / "routed.kicad_pcb")
    details = [e["detail"] for e in lost]
    assert "1/2 SES wires imported" in details
    assert any("resolution um 10" in d and "applied um 1" in d for d in details)


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


#: Fake-router helper: nudge the first footprint 1 mm in x and rewrite the net
#: of its first pad. Content-agnostic, so it survives a board 00 regeneration.
MOVE_SRC = """
import re, sys
def move(t):
    i = t.index("(footprint ")
    m = re.compile(r"\\(at ([-\\d.]+) ").search(t, i)
    t = t[: m.start(1)] + str(float(m.group(1)) + 1) + t[m.end(1) :]
    m = re.compile(r'\\(net \\d+ "[^"]+"\\)').search(t, t.index("(pad ", i))
    return t[: m.start()] + '(net 0 "")' + t[m.end() :]
args = sys.argv[1:]
src, out = args[1], args[args.index("-o") + 1]
"""


def test_moved_footprint_is_invalid_fixed_placement(fr, prep, board00, tmp_path):
    spec = _tm(tmp_path, MOVE_SRC + "open(out, 'w').write(move(open(src).read()))\n")
    rec = fr.run_tool("tracemaker", spec, prep, board00, tmp_path / "tm", BUDGETS)
    assert rec.outcome == "invalid-fixed-placement"
    joined = " ".join(rec.placement_violations)
    assert "footprints: " in joined
    assert "pad_nets: " in joined
    assert rec.score is None


def test_router_rewriting_its_input_is_invalid_fixed_placement(fr, prep, board00, tmp_path):
    """Moving parts in the input copy AND the output must not compare clean."""
    spec = _tm(
        tmp_path,
        MOVE_SRC
        + "moved = move(open(src).read())\n"
        + "open(src, 'w').write(moved)\n"
        + "open(out, 'w').write(moved)\n",
    )
    rec = fr.run_tool("tracemaker", spec, prep, board00, tmp_path / "tm", BUDGETS)
    assert rec.outcome == "invalid-fixed-placement"
    assert rec.reason == "router modified its input"
    assert rec.score is None
    # The rewritten working copy and the output agree, so a baseline taken from
    # the router's copy (the old behaviour) would have passed this row.
    work_in = tmp_path / "tm" / "run" / "simple_led.kicad_pcb"
    out = tmp_path / "tm" / "run" / "simple_led_routed.kicad_pcb"
    assert fr.compare_fingerprints(fr.fingerprint(work_in), fr.fingerprint(out)) == []
    assert fr.compare_fingerprints(fr.fingerprint(prep.pcb), fr.fingerprint(out)) != []


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


def _audit_with_dru(fr, rule_bearing, dru_text, *, dsn_edit=None):
    dsn = rule_bearing.with_suffix(".dsn")
    rule_bearing.with_suffix(".kicad_dru").write_text(dru_text)
    if dsn_edit is not None:
        from kicad_tools.export.dsn import KiCadToDSNExporter

        dsn.write_text(dsn_edit(KiCadToDSNExporter(rule_bearing).export(dsn)))
    not_routing: list = []
    lost = fr.dsn_audit(
        rule_bearing,
        rule_bearing.with_suffix(".kicad_pro"),
        rule_bearing.with_suffix(".kicad_dru"),
        dsn,
        not_routing,
    )
    return [e for e in lost if e["kind"] == "dru"], not_routing


def test_dsn_audit_fab_only_rules_are_not_routing_not_lost(fr, rule_bearing):
    dru, not_routing = _audit_with_dru(
        fr,
        rule_bearing,
        '(version 1)\n(rule "Silk" (constraint silk_clearance (min 0.15mm)))\n'
        '(rule "Ann" (constraint annular_width (min 0.13mm)))\n'
        '(rule "Edge" (constraint edge_clearance (min 0.5mm)))\n',
    )
    assert dru == []
    assert {n["type"] for n in not_routing} == {
        "silk_clearance",
        "annular_width",
        "edge_clearance",
    }


_POWER_RULE = (
    "(version 1)\n"
    '(rule "Reviewed clearance - Power" (condition "A.NetClass == \'Power\' || '
    "B.NetClass == 'Power'\") (constraint clearance (min 0.2mm)))\n"
)


def test_dsn_audit_class_clearance_covered_by_class_block(fr, rule_bearing):
    dru, _ = _audit_with_dru(fr, rule_bearing, _POWER_RULE)  # Power clearance 0.3 >= 0.2
    assert dru == []


def test_dsn_audit_class_clearance_lost_when_class_block_weaker_or_absent(fr, rule_bearing):
    def weaken(text):
        start = text.index("(class Power")
        head, tail = text[:start], text[start:]
        return head + re.sub(r"\(clearance [\d.]+\)", "(clearance 100.0)", tail, count=1)

    dru, _ = _audit_with_dru(fr, rule_bearing, _POWER_RULE, dsn_edit=weaken)
    assert len(dru) == 1 and "Reviewed clearance - Power" in dru[0]["detail"]

    def drop(text):
        return re.sub(r"\n    \(class Power\b.*?\n    \)", "", text, count=1, flags=re.S)

    dru, _ = _audit_with_dru(fr, rule_bearing, _POWER_RULE, dsn_edit=drop)
    assert len(dru) == 1


def test_dsn_audit_unexpressible_routing_rules_stay_lost(fr, rule_bearing):
    dru, not_routing = _audit_with_dru(
        fr,
        rule_bearing,
        "(version 1)\n"
        '(rule "Layered" (layer "F.Cu") (constraint clearance (min 0.5mm)))\n'
        '(rule "NoClass" (condition "A.Type == \'Via\'") (constraint clearance (min 0.5mm)))\n'
        '(rule "Ghost" (condition "A.NetClass == \'Nope\'") (constraint clearance (min 0.5mm)))\n',
    )
    named = " ".join(e["detail"] for e in dru)
    assert "'Layered'" in named and "'NoClass'" in named and "'Ghost'" in named
    assert not_routing == []


def _audit_pro(fr, rule_bearing, edit_pro, dru_text=None):
    pro_path = rule_bearing.with_suffix(".kicad_pro")
    pro = json.loads(pro_path.read_text())
    edit_pro(pro)
    pro_path.write_text(json.dumps(pro))
    dru = rule_bearing.with_suffix(".kicad_dru")
    if dru_text is not None:
        dru.write_text(dru_text)
    not_routing: list = []
    lost = fr.dsn_audit(rule_bearing, pro_path, dru, rule_bearing.with_suffix(".dsn"), not_routing)
    return lost, not_routing


def _set_edge(value):
    def edit(pro):
        pro.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})[
            "min_copper_edge_clearance"
        ] = value

    return edit


def test_dsn_audit_edge_clearance_carried_by_inset_boundary(fr, rule_bearing):
    lost, _ = _audit_pro(fr, rule_bearing, _set_edge(0.3))
    assert [e for e in lost if e["kind"] == "board_rule"] == []


def test_dsn_audit_edge_clearance_lost_when_boundary_not_inset_enough(fr, rule_bearing):
    _audit_pro(fr, rule_bearing, _set_edge(0.3))  # export once with the inset
    dsn = rule_bearing.with_suffix(".dsn")
    # Restore the raw outline: the DSN on disk is what the audit trusts.
    from kicad_tools.export.dsn import KiCadToDSNExporter

    exp = KiCadToDSNExporter(rule_bearing)
    exp._load()
    exp._edge_clearance = 0.0
    dsn.write_text(exp.export())
    lost, _ = _audit_pro(fr, rule_bearing, _set_edge(0.3))
    assert any(e["kind"] == "board_rule" and "min_copper_edge" in e["detail"] for e in lost)


def test_dsn_audit_zero_edge_clearance_is_not_a_loss(fr, rule_bearing):
    lost, _ = _audit_pro(fr, rule_bearing, _set_edge(0))
    assert [e for e in lost if e["kind"] == "board_rule"] == []


def test_dsn_audit_diff_pair_gap_implied_by_class_clearance(fr, rule_bearing):
    rule = (
        '(version 1)\n(rule "Gap" (condition "A.NetClass == \'Power\'") '
        "(constraint diff_pair_gap (min {gap}mm)))\n"
    )
    dru, _ = _audit_with_dru(fr, rule_bearing, rule.format(gap=0.3))  # == Power clearance
    assert dru == []
    dru, _ = _audit_with_dru(fr, rule_bearing, rule.format(gap=0.2))
    assert dru == []


def test_dsn_audit_diff_pair_gap_stricter_than_class_clearance_is_lost(fr, rule_bearing):
    rule = (
        '(version 1)\n(rule "Gap" (condition "A.NetClass == \'Power\'") '
        "(constraint diff_pair_gap (min 0.4mm)))\n"
    )
    dru, _ = _audit_with_dru(fr, rule_bearing, rule)
    assert len(dru) == 1 and "diff_pair_gap" in dru[0]["detail"]


def test_dsn_audit_zone_and_smd_pad_clearance_are_not_representable(fr, rule_bearing):
    dru, not_routing = _audit_with_dru(
        fr,
        rule_bearing,
        "(version 1)\n"
        '(rule "Native zone clearance" (layer "F.Cu")\n'
        "  (condition \"(A.Type == 'Zone' && A.NetName == 'GND') || "
        "(B.Type == 'Zone' && B.NetName == 'GND')\")\n"
        "  (constraint clearance (min 0.3mm)))\n"
        '(rule "SMD Pad Clearance - jlcpcb"\n'
        "  (condition \"A.Type == 'Pad' && B.Type == 'Pad' && A.Pad_Type == 'SMD' && "
        "B.Pad_Type == 'SMD' && A.Net != B.Net\")\n"
        "  (constraint clearance (min 0.5mm)))\n",
    )
    assert dru == []
    assert {n["rule"] for n in not_routing} == {
        "Native zone clearance",
        "SMD Pad Clearance - jlcpcb",
    }


def test_dsn_audit_name_only_zone_or_smd_rule_is_still_a_loss(fr, rule_bearing):
    """The name alone is not enough: a rule whose condition also selects
    tracks (or has none) is still something the router must honour."""
    dru, not_routing = _audit_with_dru(
        fr,
        rule_bearing,
        "(version 1)\n"
        '(rule "Native zone clearance" (layer "F.Cu") (constraint clearance (min 0.3mm)))\n'
        '(rule "SMD Pad Clearance - jlcpcb"\n'
        "  (condition \"A.Type == 'Pad' && B.Type == 'Track'\")\n"
        "  (constraint clearance (min 0.5mm)))\n",
    )
    assert not_routing == []
    assert len(dru) == 2


def test_dsn_audit_diff_pair_length_rules_stay_lost(fr, rule_bearing):
    dru, not_routing = _audit_with_dru(
        fr,
        rule_bearing,
        "(version 1)\n"
        '(rule "Skew" (condition "A.NetClass == \'Power\'") (constraint skew (max 0.1mm)))\n'
        '(rule "Unc" (condition "A.NetClass == \'Power\'") '
        "(constraint diff_pair_uncoupled (max 5mm)))\n"
        '(rule "Vias" (condition "A.NetClass == \'Power\'") (constraint via_count (max 2)))\n',
    )
    assert len(dru) == 3 and not_routing == []
