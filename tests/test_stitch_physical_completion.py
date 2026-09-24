"""Physical stitch acceptance is distinct from counting inserted vias (#5388)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import stitch_cmd
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.stitching import (
    StitchRejected,
    _native_refill,
    _snapshot,
    complete_power_connections,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stitch-5388"


@pytest.fixture
def native_cli():
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip(
            "find_kicad_cli() found no kicad-cli install (checked PATH and common install "
            "locations) -- KiCad 10 is needed for native refill acceptance"
        )
    # complete_power_connections()'s kicad_cli param (and _native_refill's
    # command-list json.dumps() log) expect a str, matching the prior
    # shutil.which() return type -- keep the resolved Path a str here.
    cli = str(cli)
    version = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=20)
    if version.returncode or not version.stdout.startswith("10."):
        pytest.skip("This acceptance contract requires native KiCad 10")
    return cli


def fixture_board(tmp_path, name="continuous"):
    board = tmp_path / "board.kicad_pcb"
    shutil.copy2(FIXTURES / f"{name}.kicad_pcb", board)
    return board


@pytest.mark.parametrize("inner_plane", [False, True])
def test_real_refill_completes_power_pads_and_preserves_bonds(tmp_path, native_cli, inner_plane):
    board = fixture_board(tmp_path)
    if inner_plane:
        text = board.read_text().replace(
            '(2 "B.Cu" signal)', '(4 "In1.Cu" power) (6 "In2.Cu" power) (2 "B.Cu" signal)'
        )
        # Vias still span the whole board; only the receiving pour moves.
        zone_start = text.index("\n\t(zone")
        text = text[:zone_start] + text[zone_start:].replace('(layer "B.Cu")', '(layer "In1.Cu")')
        board.write_text(text)
    evidence = tmp_path / "evidence"
    result = complete_power_connections(
        board, ["GNDA"], evidence_dir=evidence, kicad_cli=native_cli
    )
    assert result["status"] == "complete"
    assert result["vias_added"] == 1
    assert len(result["before"]["native"]["unconnected_items"]) == 1
    assert result["candidate"]["native"]["unconnected_items"] == []
    assert result["candidate"]["components"] == [["C1.1", "C2.1", "C3.1"]]
    assert board.read_bytes() == (evidence / "candidate" / board.name).read_bytes()
    for stage in ["saved", "before", "proposed", "candidate"]:
        assert (evidence / stage / board.name).is_file()


def test_refilled_split_plane_rejects_false_completion_without_touching_input(tmp_path, native_cli):
    board = fixture_board(tmp_path, "split")
    original = board.read_bytes()
    evidence = tmp_path / "evidence"
    with pytest.raises(StitchRejected, match="remains physically disconnected"):
        complete_power_connections(board, ["GNDA"], evidence_dir=evidence, kicad_cli=native_cli)
    assert board.read_bytes() == original
    record = json.loads((evidence / "evidence.json").read_text())
    assert record["status"] == "rejected"
    assert len(record["before"]["native"]["unconnected_items"]) == 2
    assert len(record["candidate"]["native"]["unconnected_items"]) == 1
    assert _snapshot(board)["components"] == record["saved"]["components"]


def test_already_complete_keeps_exact_bytes_and_still_checks_refill(tmp_path, native_cli):
    board = fixture_board(tmp_path)
    complete_power_connections(
        board, ["GNDA"], evidence_dir=tmp_path / "first", kicad_cli=native_cli
    )
    original = board.read_bytes()
    result = complete_power_connections(
        board, ["GNDA"], evidence_dir=tmp_path / "second", kicad_cli=native_cli
    )
    assert result["vias_added"] == 0
    assert result["candidate"]["native"]["unconnected_items"] == []
    assert board.read_bytes() == original


@pytest.mark.parametrize("kind", ["segment", "via", "pad", "zone"])
def test_candidate_touching_foreign_copper_rolls_back(tmp_path, native_cli, monkeypatch, kind):
    board = fixture_board(tmp_path)
    pad_x = 114 if kind == "pad" else 118
    foreign = f"""(footprint "Test:Foreign" (layer "F.Cu") (at {pad_x} 110)
      (property "Reference" "R9")
      (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net "SIG")))"""
    if kind == "segment":
        foreign += '(segment (start 114 110) (end 118 110) (width 0.3) (layer "F.Cu") (net "SIG"))'
    elif kind == "via":
        foreign += '(via (at 114 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "SIG"))'
        foreign += '(segment (start 114 110) (end 118 110) (width 0.3) (layer "F.Cu") (net "SIG"))'
    elif kind == "zone":
        foreign += """(zone (net "SIG") (layer "F.Cu") (hatch edge 0.5)
          (connect_pads yes (clearance 0.2)) (min_thickness 0.25)
          (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
          (polygon (pts (xy 113.5 109) (xy 119 109) (xy 119 111) (xy 113.5 111))))"""
    board.write_text(board.read_text().rstrip()[:-1] + foreign + ")")
    # Establish actual foreign-pour clearance before proposing the bad copper.
    _native_refill(board, native_cli)
    original = board.read_bytes()
    before = _snapshot(board)

    def unsafe_candidate(path, *args, **kwargs):
        text = path.read_text().rstrip()[:-1]
        text += '(segment (start 110 110) (end 114 110) (width 0.3) (layer "F.Cu") (net "GNDA")))'
        path.write_text(text)
        return SimpleNamespace(vias_added=[object()])

    # The proposal is real copper, not a mocked validation verdict. Exercise
    # the shared transaction's protection even when a generator is defective.
    monkeypatch.setattr(stitch_cmd, "run_stitch", unsafe_candidate)
    with pytest.raises(StitchRejected, match="Different-net copper contact"):
        complete_power_connections(
            board, ["GNDA"], evidence_dir=tmp_path / "evidence", kicad_cli=native_cli
        )
    assert board.read_bytes() == original
    assert _snapshot(board)["components"] == before["components"]


def test_failed_native_invocation_does_not_promote(tmp_path):
    board = fixture_board(tmp_path)
    original = board.read_bytes()
    with pytest.raises(StitchRejected):
        complete_power_connections(
            board, ["GNDA"], evidence_dir=tmp_path / "evidence", kicad_cli="/no/such/kicad-cli"
        )
    assert board.read_bytes() == original
    assert json.loads((tmp_path / "evidence/evidence.json").read_text())["status"] == "rejected"


def test_generator_disconnects_existing_pad_bond_is_rejected(tmp_path, native_cli, monkeypatch):
    board = fixture_board(tmp_path)
    original = board.read_bytes()

    def disconnect(path, *args, **kwargs):
        from kicad_tools.core.sexp_file import load_pcb, save_pcb

        document = load_pcb(path)
        # Remove the actual C2 barrel; its trace no longer reaches the plane.
        via = next(v for v in document.find_children("via") if v.find("at").get_float(0) == 120.8)
        document.children.remove(via)
        save_pcb(document, path)
        return SimpleNamespace(vias_added=[object()])

    monkeypatch.setattr(stitch_cmd, "run_stitch", disconnect)
    with pytest.raises(
        StitchRejected, match="Previously connected physical pads|Existing copper identities"
    ):
        complete_power_connections(
            board, ["GNDA"], evidence_dir=tmp_path / "evidence", kicad_cli=native_cli
        )
    assert board.read_bytes() == original


def test_existing_evidence_is_never_reused(tmp_path):
    board = fixture_board(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "evidence.json").write_text('{"status": "complete"}')
    with pytest.raises(FileExistsError):
        complete_power_connections(board, ["GNDA"], evidence_dir=evidence)
    assert (evidence / "evidence.json").read_text() == '{"status": "complete"}'


def test_cli_physical_result_uses_completion_scope(tmp_path, monkeypatch, capsys):
    import kicad_tools.stitching as stitching

    board = fixture_board(tmp_path)
    calls = []

    def complete(path, nets, **kwargs):
        calls.append((path, nets, kwargs))
        return {
            "target_nets": nets,
            "vias_added": 1,
            "output_sha256": "measured",
            "evidence_dir": "evidence",
        }

    monkeypatch.setattr(stitching, "complete_power_connections", complete)
    assert stitch_cmd.main([str(board), "--net", "GNDA", "--complete", "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["success_scope"] == "physical_power_connections"
    assert result["success"] is True
    assert len(calls) == 1 and calls[0][1] == ["GNDA"]
    assert calls[0][2]["avoid_pad_overlap"] is True


def test_cli_physical_rejection_is_non_success(tmp_path, monkeypatch, capsys):
    import kicad_tools.stitching as stitching

    board = fixture_board(tmp_path)
    original = board.read_bytes()

    def reject(*args, **kwargs):
        raise StitchRejected("Power net remains physically disconnected")

    monkeypatch.setattr(stitching, "complete_power_connections", reject)
    assert stitch_cmd.main([str(board), "--net", "GNDA", "--complete", "--format", "json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["success"] is False
    assert "physically disconnected" in result["error"]
    assert board.read_bytes() == original


def test_authored_hole_rule_rejects_undersized_candidate_and_preserves_sidecar(
    tmp_path, native_cli
):
    board = fixture_board(tmp_path)
    rules = board.with_suffix(".kicad_dru")
    rules.write_text(
        '(version 1)\n(rule "selected hole floor" (constraint hole_size (min 0.3mm)))\n'
    )
    original, authored = board.read_bytes(), rules.read_bytes()
    with pytest.raises(StitchRejected, match="native physical or manufacturing findings"):
        complete_power_connections(
            board,
            ["GNDA"],
            via_size=0.3,
            drill=0.1,
            evidence_dir=tmp_path / "evidence",
            kicad_cli=native_cli,
        )
    assert board.read_bytes() == original
    assert rules.read_bytes() == authored
    record = json.loads((tmp_path / "evidence/evidence.json").read_text())
    assert record["candidate"]["native"]["unconnected_items"] == []
    assert any(
        item["type"] == "drill_out_of_range" for item in record["candidate"]["native"]["violations"]
    )


def test_refill_only_completion_promotes_the_measured_filled_bytes(tmp_path, native_cli):
    from kicad_tools.core.sexp_file import load_pcb, save_pcb
    from kicad_tools.sexp import parse_string

    board = fixture_board(tmp_path)
    document = load_pcb(board)
    for zone in document.find_children("zone"):
        for fill in list(zone.find_children("filled_polygon")):
            zone.children.remove(fill)
    document.children.append(
        parse_string(
            '(via (at 110.8 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GNDA"))'
        )
    )
    save_pcb(document, board)
    original = board.read_bytes()
    result = complete_power_connections(
        board, ["GNDA"], evidence_dir=tmp_path / "evidence", kicad_cli=native_cli
    )
    assert result["vias_added"] == 0
    assert result["candidate"]["native"]["unconnected_items"] == []
    assert board.read_bytes() != original
    assert board.read_bytes() == (tmp_path / "evidence/candidate/board.kicad_pcb").read_bytes()
    assert _snapshot(board)["components"] == [["C1.1", "C2.1", "C3.1"]]


def test_first_refill_cannot_hide_new_orphan_copper_findings(tmp_path, native_cli):
    from kicad_tools.core.sexp_file import load_pcb, save_pcb
    from kicad_tools.sexp import parse_string

    board = fixture_board(tmp_path)
    document = load_pcb(board)
    document.children.append(
        parse_string(
            '(via (at 110.8 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GNDA"))'
        )
    )
    document.children.append(
        parse_string('(via (at 106 106) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "SIG"))')
    )
    document.children.append(
        parse_string(
            '(via (at 107.8 106) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "SIG"))'
        )
    )
    document.children.append(
        parse_string(
            '(segment (start 106 106) (end 107.8 106) (width 0.3) (layer "B.Cu") (net "SIG"))'
        )
    )
    document.children.append(
        parse_string(
            '(zone (net "SIG") (layer "F.Cu") (hatch edge 0.5)'
            " (connect_pads yes (clearance 0.2)) (min_thickness 0.25)"
            " (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5) (island_removal_mode 0))"
            " (polygon (pts (xy 105 105) (xy 108 105) (xy 108 108) (xy 105 108))))"
        )
    )
    save_pcb(document, board)
    _native_refill(board, native_cli)
    # Retain the connected fill cache, then add a pour keepout around one
    # untargeted barrel. Refill removes that barrel's F.Cu contact without
    # changing any component pad bond or the completed GNDA target.
    document = load_pcb(board)
    document.children.append(
        parse_string(
            '(zone (net "") (layer "F.Cu") (hatch edge 0.5)'
            " (connect_pads (clearance 0)) (min_thickness 0.25)"
            " (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))"
            " (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))"
            " (polygon (pts (xy 105.5 105.5) (xy 106.5 105.5) (xy 106.5 106.5) (xy 105.5 106.5))))"
        )
    )
    save_pcb(document, board)
    original = board.read_bytes()
    before = _snapshot(board)
    with pytest.raises(StitchRejected, match="native physical or manufacturing findings"):
        complete_power_connections(
            board, ["GNDA"], evidence_dir=tmp_path / "evidence", kicad_cli=native_cli
        )
    assert board.read_bytes() == original
    record = json.loads((tmp_path / "evidence/evidence.json").read_text())
    assert record["saved"]["components"] == record["before"]["components"] == before["components"]
    assert record["saved"]["native"]["unconnected_items"] == []
    assert record["before"]["native"]["unconnected_items"] or any(
        finding["type"] == "via_dangling" for finding in record["before"]["native"]["violations"]
    )


def test_project_local_library_context_is_retained(tmp_path, native_cli):
    board = fixture_board(tmp_path)
    library = tmp_path / "local.pretty"
    library.mkdir()
    footprint = library / "Pad.kicad_mod"
    footprint.write_text('(footprint "Pad" (version 20260101) (generator "test") (layer "F.Cu"))')
    table = tmp_path / "fp-lib-table"
    table.write_text(
        '(fp_lib_table (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/local.pretty") (options "") (descr "")))'
    )
    constraints = board.with_suffix(".kicad_pro")
    constraints.write_text(json.dumps({"text_variables": {"PROCESS": "selected"}}))
    result = complete_power_connections(
        board, ["GNDA"], evidence_dir=tmp_path / "evidence", kicad_cli=native_cli
    )
    assert result["status"] == "complete"
    for stage in ("saved", "before", "proposed", "candidate"):
        folder = tmp_path / "evidence" / stage
        assert (folder / "fp-lib-table").read_bytes() == table.read_bytes()
        assert (folder / "local.pretty/Pad.kicad_mod").read_bytes() == footprint.read_bytes()
        assert (folder / constraints.name).read_bytes() == constraints.read_bytes()


@pytest.mark.parametrize("variable", [False, True])
def test_unpreservable_project_relative_library_fails_with_evidence(tmp_path, variable):
    board = fixture_board(tmp_path)
    original = board.read_bytes()
    uri = "${SHARED}" if variable else "../shared.pretty"
    if variable:
        board.with_suffix(".kicad_pro").write_text(
            json.dumps({"text_variables": {"SHARED": "../shared.pretty"}})
        )
    (tmp_path / "fp-lib-table").write_text(
        f'(fp_lib_table (lib (name "Local") (type "KiCad") (uri "{uri}") (options "") (descr "")))'
    )
    with pytest.raises(StitchRejected, match="Cannot preserve relative footprint library"):
        complete_power_connections(board, ["GNDA"], evidence_dir=tmp_path / "evidence")
    assert board.read_bytes() == original
    assert json.loads((tmp_path / "evidence/evidence.json").read_text())["status"] == "rejected"


@pytest.mark.parametrize("fail_write", [1, 2])
def test_evidence_disk_failure_preserves_an_honest_commit_outcome(
    tmp_path, native_cli, monkeypatch, fail_write
):
    import kicad_tools.stitching as stitching

    board = fixture_board(tmp_path)
    original = board.read_bytes()
    evidence = tmp_path / "evidence"
    actual_write = stitching.atomic_write_text
    calls = 0

    def fail_evidence(path, text, *args, **kwargs):
        nonlocal calls
        if Path(path) == evidence / "evidence.json":
            calls += 1
            if calls >= fail_write:
                raise OSError("injected evidence disk failure")
        return actual_write(path, text, *args, **kwargs)

    monkeypatch.setattr(stitching, "atomic_write_text", fail_evidence)
    if fail_write == 1:
        with pytest.raises(StitchRejected, match="injected evidence disk failure"):
            complete_power_connections(board, ["GNDA"], evidence_dir=evidence, kicad_cli=native_cli)
        assert board.read_bytes() == original
    else:
        result = complete_power_connections(
            board, ["GNDA"], evidence_dir=evidence, kicad_cli=native_cli
        )
        assert result["status"] == "complete"
        assert result["evidence_finalization_error"] == "injected evidence disk failure"
        accepted = json.loads((evidence / "evidence.json").read_text())
        assert accepted["status"] == "accepted"
        assert accepted["output_sha256"] == result["output_sha256"]
        assert accepted["candidate"]["native"]["unconnected_items"] == []
        assert board.read_bytes() == (evidence / "candidate" / board.name).read_bytes()
        assert board.read_bytes() != original


def test_completion_refuses_output_copy_before_touching_either_file(tmp_path, capsys):
    board = fixture_board(tmp_path)
    output = tmp_path / "other.kicad_pcb"
    output.write_bytes(b"previous output")
    original = board.read_bytes()
    assert (
        stitch_cmd.main([str(board), "--complete", "--output", str(output), "--format", "json"])
        == 1
    )
    assert json.loads(capsys.readouterr().out)["success"] is False
    assert board.read_bytes() == original
    assert output.read_bytes() == b"previous output"


@pytest.mark.parametrize("case", ["complete", "split", "strict", "missing_cli", "output"])
def test_public_cli_physical_completion(tmp_path, native_cli, case):
    import os
    import sys

    board = fixture_board(tmp_path, "split" if case == "split" else "continuous")
    original = board.read_bytes()
    evidence = tmp_path / "evidence"
    command = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "stitch",
        str(board),
        "--net",
        "GNDA",
        "--complete",
        "--evidence-dir",
        str(evidence),
        "--kicad-cli",
        "/no/such/kicad-cli" if case == "missing_cli" else native_cli,
        "--via-size",
        "0.6",
        "--drill",
        "0.3",
        "--format",
        "json",
    ]
    if case == "strict":
        command.append("--drc-strict")
    output = tmp_path / "existing.kicad_pcb"
    if case == "output":
        output.write_bytes(b"prior output")
        command.extend(["--output", str(output)])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=90,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    document = json.loads(result.stdout)
    assert result.returncode == document["exit_code"] == (0 if case == "complete" else 1)
    assert document["success"] is (case == "complete")
    if case == "complete":
        assert document["success_scope"] == "physical_power_connections"
        assert _snapshot(board)["components"] == [["C1.1", "C2.1", "C3.1"]]
        assert (evidence / "evidence.json").is_file()
    else:
        assert board.read_bytes() == original
    if case == "strict":
        assert "Strict native DRC" in document["error"]
    if case == "output":
        assert output.read_bytes() == b"prior output"


@pytest.mark.parametrize("changed", ["pcb", "new_rules"])
def test_edit_after_accepted_evidence_is_preserved(tmp_path, native_cli, monkeypatch, changed):
    import kicad_tools.stitching as stitching

    board = fixture_board(tmp_path)
    original = board.read_bytes()
    evidence = tmp_path / "evidence"
    actual_write = stitching.atomic_write_text
    edited = original + b"\n; concurrent edit\n"
    rules = board.with_suffix(".kicad_dru")

    def interleave(path, text, *args, **kwargs):
        result = actual_write(path, text, *args, **kwargs)
        if Path(path) == evidence / "evidence.json" and json.loads(text)["status"] == "accepted":
            if changed == "pcb":
                board.write_bytes(edited)
            else:
                rules.write_text(
                    '(version 1) (rule "new process" (constraint hole_size (min 1mm)))'
                )
        return result

    monkeypatch.setattr(stitching, "atomic_write_text", interleave)
    with pytest.raises(StitchRejected, match="changed concurrently"):
        complete_power_connections(board, ["GNDA"], evidence_dir=evidence, kicad_cli=native_cli)
    assert board.read_bytes() == (edited if changed == "pcb" else original)
    if changed == "new_rules":
        assert "new process" in rules.read_text()
    assert json.loads((evidence / "evidence.json").read_text())["status"] == "rejected"
    assert not list(tmp_path.glob("*.stitch-tmp"))
    assert not list(tmp_path.glob("*.stitch.lock"))


def test_cooperating_publisher_lock_and_unique_staging(tmp_path, native_cli):
    from kicad_tools.stitching import _publisher_lock

    board = fixture_board(tmp_path)
    original = board.read_bytes()
    with _publisher_lock(board):
        with pytest.raises(StitchRejected, match="publisher lock exists"):
            complete_power_connections(
                board, ["GNDA"], evidence_dir=tmp_path / "busy", kicad_cli=native_cli
            )
    assert board.read_bytes() == original
    other_temporary = board.with_suffix(".kicad_pcb.tmp")
    other_temporary.write_bytes(b"another writer's staging file")
    result = complete_power_connections(
        board, ["GNDA"], evidence_dir=tmp_path / "free", kicad_cli=native_cli
    )
    assert result["status"] == "complete"
    assert other_temporary.read_bytes() == b"another writer's staging file"


def test_failed_atomic_promotion_preserves_input_and_records_rejection(
    tmp_path, native_cli, monkeypatch
):
    import kicad_tools.stitching as stitching

    board = fixture_board(tmp_path)
    original = board.read_bytes()
    evidence = tmp_path / "evidence"
    replace = stitching.os.replace

    def fail_commit(source, destination, *args, **kwargs):
        if Path(destination) == board.resolve():
            raise OSError("injected PCB promotion failure")
        return replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(stitching.os, "replace", fail_commit)
    with pytest.raises(StitchRejected, match="PCB promotion failure"):
        complete_power_connections(board, ["GNDA"], evidence_dir=evidence, kicad_cli=native_cli)
    assert board.read_bytes() == original
    assert json.loads((evidence / "evidence.json").read_text())["status"] == "rejected"
    assert not list(tmp_path.glob("*.stitch-tmp"))
    assert not list(tmp_path.glob("*.stitch.lock"))


def test_edit_during_initial_context_capture_cannot_rebind_original_bytes(tmp_path, monkeypatch):
    import hashlib

    import kicad_tools.stitching as stitching

    board = fixture_board(tmp_path)
    original = board.read_bytes()
    evidence = tmp_path / "evidence"
    actual_context = stitching._context_files
    edited = (
        original.rstrip()[:-1]
        + b'(gr_text "KEEP MY EDIT" (at 111 111) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15)))))\n'
    )
    calls = 0

    def edit_during_census(source):
        nonlocal calls
        result = actual_context(source)
        calls += 1
        if calls == 1:
            source.write_bytes(edited)
        return result

    monkeypatch.setattr(stitching, "_context_files", edit_during_census)
    with pytest.raises(StitchRejected, match="changed concurrently during input capture"):
        complete_power_connections(board, ["GNDA"], evidence_dir=evidence, kicad_cli="/not-reached")
    assert board.read_bytes() == edited
    record = json.loads((evidence / "evidence.json").read_text())
    original_hash = hashlib.sha256(original).hexdigest()
    assert record["inputs"][str(board.resolve())] == record["saved"]["sha256"] == original_hash
    assert (evidence / "saved" / board.name).read_bytes() == original
    assert record["status"] == "rejected"
