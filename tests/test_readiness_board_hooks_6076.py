"""Board-recipe hooks on the generic readiness producer (Issue #6076).

``boards/*/package_release.py`` scripts extend ``kct readiness --generate``
through three ``Engines`` hooks -- extra package files, extra checks and
extra hashed inputs -- and rely on ``manufacturing.zip`` being written
byte-reproducibly.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import zipfile

from kicad_tools.cli import readiness_cmd as cmd
from kicad_tools.cli.board_readiness import read_readiness
from tests.test_kct_readiness_cmd import FakeEngines, make_board


def _generate(board, engines) -> tuple[int, dict]:
    rc = cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=engines)
    published = board / "output/readiness.json"
    attempt = board / "output/readiness-attempt/readiness.json"
    return rc, json.loads((published if published.exists() else attempt).read_text())


def test_hooks_ship_files_add_checks_and_hash_inputs(tmp_path):
    board = make_board(tmp_path)
    (board / "firmware.hex").write_text(":00000001FF\n")

    def extras(options):
        (options.output_dir / "firmware").mkdir()
        (options.output_dir / "firmware/firmware.hex").write_bytes(
            (options.board_dir / "firmware.hex").read_bytes()
        )
        return ["Program firmware/firmware.hex."]

    def checks(options):
        return [cmd.CheckOutcome("firmware", cmd.PASSED, "image verified")]

    engines = dataclasses.replace(
        FakeEngines(board).bundle(),
        package_extras=extras,
        extra_checks=checks,
        extra_inputs=lambda options: [options.board_dir / "firmware.hex"],
    )
    rc, report = _generate(board, engines)
    assert rc == 0, report["blockers"]
    assert {"name": "firmware", "status": "passed", "detail": "image verified"} in report["checks"]
    package = board / "output/manufacturing"
    manifest = json.loads((package / "manifest.json").read_text())
    assert "firmware/firmware.hex" in manifest["files"]
    assert "Program firmware/firmware.hex." in (package / "README.txt").read_text()
    digest = hashlib.sha256((board / "firmware.hex").read_bytes()).hexdigest()
    assert report["inputs"]["firmware.hex"] == digest
    assert read_readiness(board)["status"] == "ready"

    # The extra input binds the verdict: editing it makes the evidence stale.
    (board / "firmware.hex").write_text(":00000001FF\n\n")
    assert read_readiness(board)["status"] == "unverified"


def test_failed_extra_check_blocks_and_publishes_nothing(tmp_path):
    board = make_board(tmp_path)
    engines = dataclasses.replace(
        FakeEngines(board).bundle(),
        extra_checks=lambda options: [
            cmd.CheckOutcome(
                "firmware", cmd.FAILED, "image changed", blockers=["Firmware changed."]
            )
        ],
    )
    rc, report = _generate(board, engines)
    assert rc != 0
    assert report["status"] == "blocked"
    assert "Firmware changed." in report["blockers"]
    assert not (board / "output/readiness.json").exists()


def test_package_extras_refusal_fails_the_artifacts_gate(tmp_path):
    board = make_board(tmp_path)

    def refuse(options):
        raise RuntimeError("refusing to drop files the current package ships: renders/a.png")

    engines = dataclasses.replace(FakeEngines(board).bundle(), package_extras=refuse)
    rc, report = _generate(board, engines)
    assert rc != 0
    artifacts = next(c for c in report["checks"] if c["name"] == "artifacts")
    assert artifacts["status"] == "failed"
    assert "renders/a.png" in artifacts["detail"]
    assert not (board / "output/manufacturing").exists()


def test_manufacturing_zip_is_byte_reproducible(tmp_path):
    board = make_board(tmp_path)
    assert _generate(board, FakeEngines(board).bundle())[0] == 0
    output = board / "output"
    options, error = cmd.resolve_options(
        cmd.build_parser().parse_args([str(board), "--mfr", "jlcpcb"])
    )
    assert error is None
    first = (output / "manufacturing.zip").read_bytes()
    # Touch every bundle file: mtimes must not leak into the archive.
    for path in (output / "manufacturing").rglob("*"):
        if path.is_file():
            path.touch()
    cmd._build_archive(options)
    assert (output / "manufacturing.zip").read_bytes() == first
    with zipfile.ZipFile(output / "manufacturing.zip") as zf:
        names = zf.namelist()
        assert names == sorted(names)
        assert {info.date_time for info in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_kct_check_evidence_records_the_board_relative_pcb(tmp_path):
    """The throw-away candidate directory must not leak into evidence bytes."""
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    inner = fake.kct_check

    def kct_check(pcb, mfr, report_path, extra):
        run = inner(pcb, mfr, report_path, extra)
        data = json.loads(report_path.read_text())
        data["file"] = str(pcb.resolve())
        report_path.write_text(json.dumps(data, indent=2) + "\n")
        return run

    fake.kct_check = kct_check
    rc, report = _generate(board, fake.bundle())
    assert rc == 0, report["blockers"]
    evidence = json.loads((board / "output/readiness/kct-check.json").read_text())
    assert evidence["file"] == "output/demo_routed.kicad_pcb"
