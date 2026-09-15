"""Readiness operates on snapshots and preserves already shipped releases."""

import json

from kicad_tools.cli import readiness_cmd as cmd
from tests.test_kct_readiness_cmd import FakeEngines, make_board


def inventory(board):
    return {
        p.relative_to(board).as_posix(): p.read_bytes() for p in board.rglob("*") if p.is_file()
    }


def test_default_verification_never_exports_or_rewrites_sources(tmp_path):
    board = make_board(tmp_path)
    before = inventory(board)
    fake = FakeEngines(board)
    rc = cmd.main([str(board), "--mfr", "jlcpcb"], engines=fake.bundle())
    assert rc != 0  # A missing finished package cannot earn READY.
    assert "export" not in fake.calls
    for name, data in before.items():
        assert (board / name).read_bytes() == data
    assert not (board / "output/manufacturing.zip").exists()
    assert not (board / "output/readiness.json").exists()


def test_failed_generation_preserves_previous_release(tmp_path):
    board = make_board(tmp_path)
    old = board / "output/manufacturing"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps({"producer": "kct readiness"}))
    (old / "README.txt").write_text("prior release")
    (board / "output/manufacturing.zip").write_bytes(b"old archive")
    (board / "output/readiness.json").write_text("prior evidence")
    before = inventory(board)
    fake = FakeEngines(board, native_errors=1)
    rc = cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=fake.bundle())
    assert rc != 0
    for name, data in before.items():
        assert (board / name).read_bytes() == data


def test_generation_refuses_to_replace_recipe_package(tmp_path):
    board = make_board(tmp_path)
    old = board / "output/manufacturing"
    old.mkdir()
    (old / "README.txt").write_text("Epoxy-filled & Capped (POFV); J1 manual assembly")
    before = inventory(board)
    fake = FakeEngines(board)
    assert cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=fake.bundle()) != 0
    assert "export" not in fake.calls
    for name, data in before.items():
        assert (board / name).read_bytes() == data


def test_refill_divergence_is_real_and_preserved(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    fake.fill_areas = lambda p: {"F.Cu": 2.0 if "refilled" in p.read_text() else 1.0}
    before = (board / "output/demo_routed.kicad_pcb").read_bytes()
    assert cmd.main([str(board), "--mfr", "jlcpcb"], engines=fake.bundle()) != 0
    evidence = json.loads(
        (board / "output/readiness-verification/fill-consistency.json").read_text()
    )
    assert evidence["deltas_mm2"] == {"F.Cu": 1.0}
    assert (board / "output/demo_routed.kicad_pcb").read_bytes() == before


def test_generated_package_can_be_verified_without_changing_shipped_bytes(tmp_path):
    board = make_board(tmp_path)
    assert (
        cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=FakeEngines(board).bundle())
        == 0
    )
    before = inventory(board)
    fake = FakeEngines(board)
    assert cmd.main([str(board), "--mfr", "jlcpcb"], engines=fake.bundle()) == 0
    assert "export" not in fake.calls
    for name, data in before.items():
        assert (board / name).read_bytes() == data


def test_consistent_manifest_cannot_hide_missing_project_rules(tmp_path):
    import zipfile

    board = make_board(tmp_path)
    assert (
        cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=FakeEngines(board).bundle())
        == 0
    )
    args = cmd.build_parser().parse_args([str(board), "--mfr", "jlcpcb"])
    opts, _ = cmd.resolve_options(args)
    archive = opts.output_dir / "kicad_project.zip"
    with zipfile.ZipFile(archive) as zf:
        files = {name: zf.read(name) for name in zf.namelist() if not name.endswith(".kicad_dru")}
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    cmd._write_full_manifest(opts, {})
    cmd._build_archive(opts)
    before = inventory(board)
    assert cmd.main([str(board), "--mfr", "jlcpcb"], engines=FakeEngines(board).bundle()) != 0
    report = json.loads((board / "output/readiness-verification/readiness.json").read_text())
    assert any(".kicad_dru is absent" in problem for problem in report["blockers"])
    for name, data in before.items():
        assert (board / name).read_bytes() == data


def test_publication_failure_rolls_back_all_changed_files(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    for name in ("a", "b", "c"):
        (old / name).write_text("old " + name)
        (new / name).write_text("new " + name)
    before = inventory(old)
    real_replace = cmd.os.replace
    calls = []

    def fail_second(source, destination):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError("injected publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(cmd.os, "replace", fail_second)
    import pytest

    with pytest.raises(OSError, match="injected"):
        cmd._publish_candidate(new, old, cmd._snapshot_inventory(old))
    assert inventory(old) == before


def test_export_cannot_change_checked_project_rules(tmp_path):
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    original_export = fake.export

    def mutate_rules(pcb, mfr, output, assembly):
        pcb.with_suffix(".kicad_pro").write_text('{"changed":true}')
        return original_export(pcb, mfr, output, assembly)

    fake.export = mutate_rules
    before = inventory(board)
    assert cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=fake.bundle()) != 0
    for name, data in before.items():
        assert (board / name).read_bytes() == data


def test_flat_recipe_archive_is_verified_with_exact_bytes(tmp_path):
    import zipfile

    board = make_board(tmp_path)
    assert (
        cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=FakeEngines(board).bundle())
        == 0
    )
    bundle = board / "output/manufacturing"
    with zipfile.ZipFile(board / "output/manufacturing.zip", "w") as zf:
        for path in bundle.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(bundle).as_posix())
    assert cmd.main([str(board), "--mfr", "jlcpcb"], engines=FakeEngines(board).bundle()) == 0
