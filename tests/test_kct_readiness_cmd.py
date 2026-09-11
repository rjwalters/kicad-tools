"""``kct readiness`` must earn Ready, never assert it (issue #4977).

The runner orchestrates engines that need KiCad on the host, so these tests
inject a :class:`~kicad_tools.cli.readiness_cmd.Engines` bundle of fakes and
exercise the *orchestration*: gate ordering, verdict composition, the refusal
conditions from ``.claude/commands/kct/tapeout.md``, and the hash binding the
gallery validator re-checks.
"""

from __future__ import annotations

import csv
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd
from kicad_tools.cli.board_readiness import read_readiness
from kicad_tools.cli.readiness_cmd import EngineRun, Engines

PCB_NAME = "demo_routed.kicad_pcb"
SCH_NAME = "demo.kicad_sch"
PRO_NAME = "demo_routed.kicad_pro"


# ---------------------------------------------------------------------------
# Fixtures: a board tree plus a bundle of fake engines
# ---------------------------------------------------------------------------


def make_board(tmp_path: Path) -> Path:
    """Create a minimal board directory with the canonical sources present."""
    board = tmp_path / "99-demo"
    output = board / "output"
    output.mkdir(parents=True)
    (output / PCB_NAME).write_text("(kicad_pcb original)\n")
    (output / SCH_NAME).write_text("(kicad_sch demo)\n")
    (output / PRO_NAME).write_text('{"board": {}}\n')
    (output / "demo_routed.kicad_dru").write_text("(version 1)\n")
    (board / "project.kct").write_text("[project]\nmanufacturer = jlcpcb\n")
    return board


class FakeEngines:
    """Configurable stand-ins for kct check / kicad-cli / kct export."""

    def __init__(
        self,
        board: Path,
        *,
        check_errors: int = 0,
        meta_overall: str = "PASSED",
        drc_status: str = "PASSED",
        erc_status: str = "PASSED",
        manifest_status: str = "NOT RUN",
        warnings: dict[str, int] | None = None,
        native_ran: bool = True,
        native_errors: int = 0,
        lvs_status: str = "PASSED",
        lvs_bound_pads: int = 24,
        lvs_mismatches: int = 0,
        part_numbers: dict[str, str] | None = None,
        tht_refs: tuple[str, ...] = ("D1",),
        cpl_refs: tuple[str, ...] = ("R1", "U1"),
        refill_ok: bool = True,
        refill_changes_fill: bool = False,
        zip_pcb_stale: bool = False,
        drop_gerbers: bool = False,
        drawings_ok: bool = True,
    ) -> None:
        self.board = board
        self.check_errors = check_errors
        self.meta_overall = meta_overall
        self.drc_status = drc_status
        self.erc_status = erc_status
        self.manifest_status = manifest_status
        self.warnings = warnings or {}
        self.native_ran = native_ran
        self.native_errors = native_errors
        self.lvs_status = lvs_status
        self.lvs_bound_pads = lvs_bound_pads
        self.lvs_mismatches = lvs_mismatches
        self.part_numbers = (
            part_numbers
            if part_numbers is not None
            else {"R1": "C17630", "U1": "C8734", "D1": "C87271"}
        )
        self.tht_refs = set(tht_refs)
        self.cpl_refs = cpl_refs
        self.refill_ok = refill_ok
        self.refill_changes_fill = refill_changes_fill
        self.zip_pcb_stale = zip_pcb_stale
        self.drop_gerbers = drop_gerbers
        self.drawings_ok = drawings_ok
        self.calls: list[str] = []
        self.pcb_bytes_at_export: bytes | None = None

    # -- engine implementations -------------------------------------------

    def refill(self, pcb: Path) -> EngineRun:
        self.calls.append("refill")
        if not self.refill_ok:
            return EngineRun(ok=False, detail="kicad-cli not found on PATH")
        pcb.write_text("(kicad_pcb refilled)\n")
        return EngineRun(ok=True)

    def fill_areas(self, pcb: Path) -> dict[str, float]:
        if self.refill_changes_fill and "refilled" not in pcb.read_text():
            return {"F.Cu": 100.0, "B.Cu": 100.0}
        if self.refill_changes_fill:
            # The staged refill measures one area; the saved board another.
            self.refill_changes_fill = False
            return {"F.Cu": 100.0, "B.Cu": 100.0}
        return {"F.Cu": 100.0, "B.Cu": 100.0}

    def kct_check(self, pcb: Path, mfr: str, report_path: Path, extra) -> EngineRun:
        self.calls.append("kct_check")
        violations = [
            {"rule_id": rule, "severity": "warning", "message": rule}
            for rule, count in self.warnings.items()
            for _ in range(count)
        ]
        violations.extend(
            {"rule_id": "clearance", "severity": "error", "message": "too close"}
            for _ in range(self.check_errors)
        )
        lvs: dict = {
            "status": self.lvs_status,
            "detail": "layout vs schematic",
            "clean": self.lvs_mismatches == 0 and self.lvs_status == "PASSED",
            "mismatches": [],
            "copper_mismatches": [{"ref": "R1"} for _ in range(self.lvs_mismatches)],
            "copper_bound_pad_count": self.lvs_bound_pads,
            "copper_vacuous": self.lvs_bound_pads == 0,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "file": str(pcb),
                    "manufacturer": mfr,
                    "summary": {
                        "errors": self.check_errors,
                        "warnings": sum(self.warnings.values()),
                        "passed": self.check_errors == 0,
                    },
                    "violations": violations,
                    "meta_checks": {
                        "drc": {"status": self.drc_status, "detail": "ok"},
                        "erc": {"status": self.erc_status, "detail": "ok"},
                        "lvs": lvs,
                        "manifest": {"status": self.manifest_status, "detail": "no bundle yet"},
                        "overall": self.meta_overall,
                        "schematic_missing": False,
                    },
                },
                indent=2,
            )
        )
        return EngineRun(ok=True)

    def native_drc(self, pcb: Path, report_path: Path) -> EngineRun:
        self.calls.append("native_drc")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "ran": self.native_ran,
                    "error_count": self.native_errors if self.native_ran else None,
                    "by_type": {"shorting_items": self.native_errors} if self.native_errors else {},
                    "all_by_type": {},
                    "reason": "ok" if self.native_ran else "kicad_cli_absent",
                    "note": None
                    if self.native_ran
                    else "kicad-cli not found; geometric DRC skipped",
                },
                indent=2,
            )
        )
        if not self.native_ran:
            return EngineRun(ok=False, detail="kicad-cli not found; geometric DRC skipped")
        return EngineRun(ok=True)

    def export(self, pcb: Path, mfr: str, output_dir: Path, assembly: bool) -> EngineRun:
        self.calls.append("export")
        self.pcb_bytes_at_export = pcb.read_bytes()
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "report.md").write_text("# report\n")
        if not self.drop_gerbers:
            gerbers = output_dir / "gerbers"
            gerbers.mkdir(exist_ok=True)
            with zipfile.ZipFile(gerbers / "gerbers.zip", "w") as zf:
                zf.writestr("demo-F_Cu.gbr", "G04*\n")

        if assembly:
            with (output_dir / f"bom_{mfr}.csv").open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
                for ref, part in self.part_numbers.items():
                    writer.writerow([ref, ref, "Package", part])
            with (output_dir / f"cpl_{mfr}.csv").open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
                )
                for ref in self.cpl_refs:
                    writer.writerow([ref, "1k", "Package", "1mm", "1mm", "0.0", "Top"])

        zip_pcb = b"(kicad_pcb stale)\n" if self.zip_pcb_stale else pcb.read_bytes()
        with zipfile.ZipFile(output_dir / "kicad_project.zip", "w") as zf:
            zf.writestr(pcb.name, zip_pcb)
            zf.writestr(SCH_NAME, (self.board / "output" / SCH_NAME).read_bytes())
        (output_dir / "manifest.json").write_text(
            json.dumps({"version": "1.0", "manufacturer": mfr, "files": {}}, indent=2)
        )
        return EngineRun(ok=True)

    def schematic_pdf(self, schematic: Path, output: Path) -> EngineRun:
        self.calls.append("schematic_pdf")
        if not self.drawings_ok:
            return EngineRun(ok=False, detail="kicad-cli not found on PATH")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF-1.4 schematic\n")
        return EngineRun(ok=True)

    def assembly_pdf(self, pcb: Path, layers, output: Path) -> EngineRun:
        self.calls.append(f"assembly_pdf:{output.name}")
        if not self.drawings_ok:
            return EngineRun(ok=False, detail="kicad-cli not found on PATH")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF-1.4 assembly\n")
        return EngineRun(ok=True)

    def through_hole_refs(self, pcb: Path) -> set[str]:
        return set(self.tht_refs)

    def net_metrics(self, pcb: Path) -> dict:
        return {"nets_routed_pct": 100.0}

    def kicad_cli_version(self) -> str | None:
        return "kicad-cli 9.0.1"

    # -- adapter -----------------------------------------------------------

    def bundle(self) -> Engines:
        return Engines(
            kct_check=self.kct_check,
            native_drc=self.native_drc,
            export=self.export,
            refill=self.refill,
            schematic_pdf=self.schematic_pdf,
            assembly_pdf=self.assembly_pdf,
            fill_areas=self.fill_areas,
            through_hole_refs=self.through_hole_refs,
            net_metrics=self.net_metrics,
            kicad_cli_version=self.kicad_cli_version,
        )


def run(board: Path, fake: FakeEngines, *extra: str) -> tuple[int, dict]:
    """Invoke the command and return ``(exit_code, report)``."""
    code = readiness_cmd.main([str(board), "--mfr", "jlcpcb", *extra], engines=fake.bundle())
    report_path = board / "output" / "readiness.json"
    report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    return code, report


def check_status(report: dict, name: str) -> str | None:
    for check in report.get("checks", []):
        if check["name"] == name:
            return check["status"]
    return None


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_clean_assembly_board_is_ready_and_round_trips(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, tht_refs=("D1",), cpl_refs=("R1", "U1"))

    code, report = run(board, fake)

    assert code == 0
    assert report["status"] == "ready"
    assert report["blockers"] == []
    assert report["mode"] == "assembly"
    # The four checks read_readiness() demands by name are all present + passed.
    for name in ("kct_check", "native_drc", "artifacts", "bom"):
        assert check_status(report, name) == "passed", name
    # The consumer-side validator accepts what the producer wrote.
    assert read_readiness(board)["status"] == "ready"


def test_pcb_only_mode_makes_no_assembly_claim(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    code, report = run(board, fake, "--pcb-only")

    assert code == 0
    assert report["mode"] == "pcb_only"
    assert report["status"] == "ready"
    bom = next(c for c in report["checks"] if c["name"] == "bom")
    assert "assembly not included" in bom["detail"]
    # No BOM/CPL were produced, and none were demanded.
    assert not list((board / "output" / "manufacturing").glob("bom*.csv"))
    assert read_readiness(board)["status"] == "ready"


def test_report_hashes_sources_rules_and_delivered_files(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    _, report = run(board, fake)

    inputs = report["inputs"]
    for required in (
        f"output/{PCB_NAME}",
        f"output/{SCH_NAME}",
        f"output/{PRO_NAME}",
        "output/demo_routed.kicad_dru",
        "output/manufacturing/manifest.json",
        "output/manufacturing/kicad_project.zip",
        "output/manufacturing/README.txt",
        "output/readiness/kct-check.json",
        "output/readiness/native-drc.json",
        "output/manufacturing.zip",
        "project.kct",
    ):
        assert required in inputs, required
        digest = hashlib.sha256((board / required).read_bytes()).hexdigest()
        assert inputs[required] == digest, required
    # The report never hashes itself.
    assert "output/readiness.json" not in inputs


def test_full_archive_lives_outside_the_checksummed_directory(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    run(board, fake)

    archive = board / "output" / "manufacturing.zip"
    manifest = json.loads((board / "output" / "manufacturing" / "manifest.json").read_text())
    assert archive.is_file()
    # Placing it inside the bundle would make the manifest hash itself.
    assert "manufacturing.zip" not in manifest["files"]
    with zipfile.ZipFile(archive) as zf:
        assert "manufacturing/manifest.json" in zf.namelist()


def test_manifest_covers_every_bundle_file_including_late_drawings(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    run(board, fake)

    bundle = board / "output" / "manufacturing"
    manifest = json.loads((bundle / "manifest.json").read_text())
    on_disk = {
        p.relative_to(bundle).as_posix()
        for p in bundle.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    assert on_disk == set(manifest["files"])
    # The drawings and README written after `kct export` are checksummed too.
    assert "schematic.pdf" in manifest["files"]
    assert "assembly-front.pdf" in manifest["files"]
    assert "README.txt" in manifest["files"]
    assert manifest["warning_counts_by_rule"] == {}


# ---------------------------------------------------------------------------
# Ordering: refill+save happens before BOTH the native check and the export
# ---------------------------------------------------------------------------


def test_export_reads_the_refilled_and_saved_canonical_pcb(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    run(board, fake)

    # The refill was committed to the canonical source ...
    assert (board / "output" / PCB_NAME).read_text() == "(kicad_pcb refilled)\n"
    # ... before the native cross-gate and before the export read it.
    assert fake.calls.index("refill") < fake.calls.index("native_drc")
    assert fake.calls.index("refill") < fake.calls.index("export")
    assert fake.pcb_bytes_at_export == b"(kicad_pcb refilled)\n"


def test_saved_fill_divergence_blocks_sign_off(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    saved_areas = iter([{"F.Cu": 100.0}, {"F.Cu": 91.25}])
    fake.fill_areas = lambda pcb: next(saved_areas)  # type: ignore[method-assign]

    code, report = run(board, fake)

    assert code != 0
    assert report["status"] == "blocked"
    assert check_status(report, "zone_fill") == "failed"
    assert any("diverges" in blocker for blocker in report["blockers"])
    evidence = json.loads((board / "output" / "readiness" / "fill-consistency.json").read_text())
    assert evidence["F.Cu"] == pytest.approx(8.75)


def test_source_zip_provenance_is_verified_not_just_manifest_integrity(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, zip_pcb_stale=True)

    code, report = run(board, fake)

    assert code != 0
    assert report["status"] == "blocked"
    assert check_status(report, "artifacts") == "failed"
    assert any("kicad_project.zip differs" in blocker for blocker in report["blockers"])


# ---------------------------------------------------------------------------
# Refusal conditions
# ---------------------------------------------------------------------------


def test_missing_native_checker_is_unverified_not_a_pass(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, native_ran=False)

    code, report = run(board, fake)

    assert code != 0
    assert report["status"] == "unverified"
    assert check_status(report, "native_drc") == "not_run"
    assert any("hard blocker" in blocker for blocker in report["blockers"])
    # An unverified report must not claim current metrics.
    assert "metrics" not in report
    assert read_readiness(board)["status"] == "unverified"


def test_native_drc_findings_block_even_though_kicad_cli_exits_zero(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, native_errors=13)

    code, report = run(board, fake)

    assert code != 0
    assert report["status"] == "blocked"
    assert check_status(report, "native_drc") == "failed"
    assert any("13 error-level" in blocker for blocker in report["blockers"])
    # The DRC aggregate is the max of the two engines, never their sum.
    assert report["metrics"]["drc_violations"] == 13


def test_missing_refill_engine_blocks_and_leaves_sources_untouched(tmp_path):
    board = make_board(tmp_path)
    original = (board / "output" / PCB_NAME).read_bytes()
    fake = FakeEngines(board, refill_ok=False)

    code, report = run(board, fake)

    assert code != 0
    assert check_status(report, "zone_fill") == "not_run"
    # A failed refill must never half-write the canonical source.
    assert (board / "output" / PCB_NAME).read_bytes() == original


def test_vacuous_lvs_is_not_evidence(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, lvs_bound_pads=0)

    code, report = run(board, fake)

    assert code != 0
    assert check_status(report, "lvs") == "not_run"
    assert any("vacuous" in blocker for blocker in report["blockers"])


def test_lvs_mismatches_block(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, lvs_mismatches=2)

    code, report = run(board, fake)

    assert code != 0
    assert report["status"] == "blocked"
    assert check_status(report, "lvs") == "failed"
    assert report["metrics"]["lvs_mismatches"] == 2
    assert report["metrics"]["lvs_clean"] is False


def test_unacknowledged_assembly_affecting_warnings_refuse(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, warnings={"silk_over_copper": 31, "trace_width_advisory": 4})

    code, report = run(board, fake)

    assert code != 0
    assert check_status(report, "warning_review") == "failed"
    assert any("silk_over_copper=31" in blocker for blocker in report["blockers"])


def test_acknowledged_warnings_pass_and_become_accepted_risk_lines(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, warnings={"silk_over_copper": 31, "trace_width_advisory": 4})

    code, report = run(board, fake, "--ack-warnings", "silk_over_copper")

    assert code == 0
    assert check_status(report, "warning_review") == "passed"
    readme = (board / "output" / "manufacturing" / "README.txt").read_text()
    assert "ACCEPTED RISK: silk_over_copper (31 warning(s))" in readme
    # Per-rule counts, never an aggregate, are what a later re-verification diffs.
    manifest = json.loads((board / "output" / "manufacturing" / "manifest.json").read_text())
    assert manifest["warning_counts_by_rule"] == {
        "silk_over_copper": 31,
        "trace_width_advisory": 4,
    }


def test_first_signoff_is_not_blocked_by_the_not_yet_existing_bundle(tmp_path):
    """``meta_checks.manifest`` is NOT RUN before the bundle this run creates.

    Rolling that up as INCOMPLETE would make a first sign-off structurally
    impossible.  The artifacts gate re-verifies the manifest *and* the archived
    source provenance, so deferring is strictly stronger, not weaker.
    """
    board = make_board(tmp_path)
    fake = FakeEngines(board, manifest_status="NOT RUN", meta_overall="INCOMPLETE")

    code, report = run(board, fake)

    assert code == 0
    assert check_status(report, "kct_check") == "passed"
    detail = next(c for c in report["checks"] if c["name"] == "kct_check")["detail"]
    assert "deferred to the artifacts gate" in detail
    assert check_status(report, "artifacts") == "passed"


def test_erc_sub_check_that_did_not_run_is_not_a_pass(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, erc_status="NOT RUN")

    code, report = run(board, fake)

    assert code != 0
    assert report["status"] == "unverified"
    assert check_status(report, "kct_check") == "not_run"
    assert any("erc" in blocker for blocker in report["blockers"])


def test_failed_drc_sub_check_blocks(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, drc_status="FAILED")

    code, report = run(board, fake)

    assert code != 0
    assert report["status"] == "blocked"
    assert check_status(report, "kct_check") == "failed"


def test_kct_check_errors_block(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, check_errors=4, meta_overall="FAILED")

    code, report = run(board, fake)

    assert code != 0
    assert check_status(report, "kct_check") == "failed"
    assert report["metrics"]["drc_violations"] == 4


def test_missing_drawings_block_the_bundle(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, drawings_ok=False)

    code, report = run(board, fake)

    assert code != 0
    assert check_status(report, "artifacts") == "failed"
    joined = " ".join(report["blockers"])
    assert "schematic PDF not produced" in joined
    assert "assembly-front.pdf not produced" in joined


def test_partial_package_is_detected_by_the_manifest_gate(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    original_export = fake.export

    def export_then_delete(pcb, mfr, output_dir, assembly):
        result = original_export(pcb, mfr, output_dir, assembly)
        # A file listed by the export that vanishes before packaging.
        (output_dir / "report.md").unlink()
        return result

    fake.export = export_then_delete  # type: ignore[method-assign]
    code, report = run(board, fake)
    assert code == 0  # nothing is missing once the manifest is regenerated

    # But a file that appears AFTER the manifest is written breaks the contract.
    bundle = board / "output" / "manufacturing"
    (bundle / "stray.txt").write_text("late arrival\n")
    from kicad_tools.export.manufacturing import verify_manifest

    manifest = bundle / "manifest.json"
    assert verify_manifest(manifest) == []
    listed = set(json.loads(manifest.read_text())["files"])
    on_disk = {
        p.relative_to(bundle).as_posix()
        for p in bundle.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    assert on_disk - listed == {"stray.txt"}


# ---------------------------------------------------------------------------
# BOM / CPL procurement identity
# ---------------------------------------------------------------------------


def test_placeholder_bom_ids_are_rejected_in_assembly_mode(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, part_numbers={"R1": "C17630", "U1": "TBD", "D1": ""})

    code, report = run(board, fake)

    assert code != 0
    assert check_status(report, "bom") == "failed"
    blocker = " ".join(report["blockers"])
    assert "require human part selection" in blocker
    assert "U1" in blocker and "D1" in blocker


def test_entirely_empty_part_column_is_matcher_broken_not_exotic_parts(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, part_numbers={"R1": "", "U1": "", "D1": ""})

    code, report = run(board, fake)

    assert code != 0
    assert check_status(report, "bom") == "failed"
    assert any("matcher is broken" in check["detail"] for check in report["checks"])
    assert any("#4104" in blocker for blocker in report["blockers"])


def test_placeholder_ids_are_ignored_for_a_pcb_only_order(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, part_numbers={"R1": "TBD"})

    code, report = run(board, fake, "--pcb-only")

    assert code == 0
    assert check_status(report, "bom") == "passed"


def test_through_hole_parts_must_not_ride_in_the_smt_cpl(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, tht_refs=("D1", "J1"), cpl_refs=("R1", "U1", "D1"))

    code, report = run(board, fake)

    assert code != 0
    assert check_status(report, "bom") == "failed"
    assert any("hand-soldered" in blocker for blocker in report["blockers"])


def test_include_tht_opts_into_a_cpl_carrying_through_hole_parts(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, tht_refs=("D1",), cpl_refs=("R1", "U1", "D1"))

    code, report = run(board, fake, "--include-tht")

    assert code == 0
    assert check_status(report, "bom") == "passed"


def test_excluded_tht_parts_are_named_as_hand_solder_items(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board, tht_refs=("D1", "J1"), cpl_refs=("R1", "U1"))

    code, report = run(board, fake)

    assert code == 0
    readme = (board / "output" / "manufacturing" / "README.txt").read_text()
    assert "hand-soldered" in readme
    assert "D1" in readme and "J1" in readme
    assert check_status(report, "bom") == "passed"


# ---------------------------------------------------------------------------
# HV / isolation gate
# ---------------------------------------------------------------------------


def _write_hv_map(board: Path) -> None:
    (board / "output" / "net_class_map.json").write_text(
        json.dumps({"L_IN": {"name": "HV"}, "GND": {"name": "Power"}})
    )


def test_hv_board_without_an_isolation_requirement_is_not_signed_off(tmp_path):
    board = make_board(tmp_path)
    _write_hv_map(board)
    fake = FakeEngines(board)

    code, report = run(board, fake)

    assert code != 0
    assert check_status(report, "hv_isolation") == "not_run"
    assert any("HV nets present" in blocker for blocker in report["blockers"])


def test_hv_board_with_a_recorded_requirement_passes(tmp_path):
    board = make_board(tmp_path)
    _write_hv_map(board)
    fake = FakeEngines(board)

    code, report = run(board, fake, "--hv-requirement", "iec60664 250Vrms PD2 MGII")

    assert code == 0
    assert check_status(report, "hv_isolation") == "passed"


def test_board_without_hv_nets_omits_the_gate_entirely(tmp_path):
    board = make_board(tmp_path)
    (board / "output" / "net_class_map.json").write_text(json.dumps({"GND": {"name": "Power"}}))
    fake = FakeEngines(board)

    code, report = run(board, fake)

    assert code == 0
    assert check_status(report, "hv_isolation") is None


# ---------------------------------------------------------------------------
# Engine fingerprint (additive readiness-v1 field)
# ---------------------------------------------------------------------------


def test_report_records_a_reproducible_engine_fingerprint(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    _, report = run(board, fake)

    engine = report["engine"]
    assert engine["manufacturer"] == "jlcpcb"
    assert engine["kicad_cli_version"] == "kicad-cli 9.0.1"
    assert isinstance(engine["kicad_tools_version"], str)
    assert isinstance(engine["dirty"], bool)
    assert len(engine["source_digest"]) == 64
    # The resolved rule inputs are bound too: a rule change without a board
    # edit must be visible (#5012, #5016-#5018).
    assert len(engine["rules_digest"]) == 64
    first = engine["rules_digest"]

    (board / "output" / "demo_routed.kicad_dru").write_text("(version 1)\n(rule new)\n")
    _, second_report = run(board, fake)
    assert second_report["engine"]["rules_digest"] != first


def test_engine_fingerprint_survives_the_consumer_validator(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    run(board, fake)

    validated = read_readiness(board)
    assert validated["status"] == "ready"
    assert validated["engine"]["manufacturer"] == "jlcpcb"


# ---------------------------------------------------------------------------
# Resolution / refusals before any gate runs
# ---------------------------------------------------------------------------


def test_board_without_paired_project_rules_is_refused(tmp_path):
    board = make_board(tmp_path)
    (board / "output" / PRO_NAME).unlink()
    fake = FakeEngines(board)

    code = readiness_cmd.main([str(board), "--mfr", "jlcpcb"], engines=fake.bundle())

    assert code == 2
    assert not (board / "output" / "readiness.json").exists()


def test_unknown_fab_tier_is_refused_rather_than_defaulted(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    code = readiness_cmd.main([str(board), "--mfr", "not-a-fab"], engines=fake.bundle())

    assert code == 2


def test_tier_is_discovered_from_the_board_recipe_when_not_given(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    code = readiness_cmd.main([str(board)], engines=fake.bundle())

    assert code == 0
    report = json.loads((board / "output" / "readiness.json").read_text())
    assert report["engine"]["manufacturer"] == "jlcpcb"


def test_json_output_emits_a_single_envelope(tmp_path, capsys):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    code = readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--format", "json"], engines=fake.bundle()
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["command"] == "readiness"
    assert payload["success"] is True
    assert payload["readiness"]["status"] == "ready"


def test_no_archive_skips_the_full_package_zip(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)

    code, report = run(board, fake, "--no-archive")

    assert code == 0
    assert not (board / "output" / "manufacturing.zip").exists()
    assert "output/manufacturing.zip" not in report["inputs"]


def test_ready_is_unreachable_when_a_required_check_is_absent():
    from kicad_tools.cli.readiness_cmd import (
        CheckOutcome,
        EngineFingerprint,
        ReadinessOptions,
        build_report,
    )

    options = ReadinessOptions(
        board_dir=Path("/nonexistent"),
        pcb=Path("/nonexistent/board.kicad_pcb"),
        schematic=None,
        project=None,
        manufacturer="jlcpcb",
        mode="pcb_only",
        output_dir=Path("/nonexistent/manufacturing"),
        evidence_dir=Path("/nonexistent/readiness"),
    )
    checks = [CheckOutcome(name="kct_check", status="passed", detail="ok")]
    report = build_report(options, checks, {}, EngineFingerprint(kicad_tools_version="test"))

    assert report["status"] == "blocked"
    assert any("Required checks did not pass" in b for b in report["blockers"])


def test_unified_cli_dispatches_the_readiness_subcommand(tmp_path, monkeypatch):
    board = make_board(tmp_path)
    seen: dict = {}

    def fake_main(argv, engines=None):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(readiness_cmd, "main", fake_main)
    from kicad_tools.cli import main as cli_main

    assert cli_main(["readiness", str(board), "--mfr", "jlcpcb", "--pcb-only"]) == 0
    assert seen["argv"] == [str(board), "--mfr", "jlcpcb", "--pcb-only"]
