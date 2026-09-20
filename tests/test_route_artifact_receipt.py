"""Route receipts bind the effective final artifacts, not just selected rules."""

import json
import shutil

import pytest

from kicad_tools.cli import route_cmd, route_receipt
from kicad_tools.native_concurrency import configured_limit, slot_wait_ceiling
from kicad_tools.schema.pcb import PCB
from tests.test_factory_object_clearance import board_fixture

# Wall-clock budget for the ``finite`` parametrization (Issue #5579/#5584).
# --------------------------------------------------------------------------
# Composed rather than a flat literal: the invocation's own work, plus the
# native-permit gate's own fail-open queue bound when (and only when) the gate
# is configured. See tests/test_route_partial_placement_cli.py's
# ``_queue_ceiling_seconds()`` for the full rationale -- the gate can charge
# ordinary queue-wait time against this test's route budget, so the budget
# must absorb that worst case instead of hardcoding the pre-gate 30 s.
_ROUTE_WORK_SECONDS = 30.0


def _queue_ceiling_seconds() -> float:
    """Worst-case native-permit queue time this invocation can be charged."""
    return slot_wait_ceiling() if configured_limit() is not None else 0.0


def source_board(tmp_path):
    board = board_fixture(tmp_path / "input.kicad_pcb", "smd", 2, same_net=True)
    board.write_text(
        board.read_text()
        .replace('(footprint "T"', '(footprint "T" (property "Reference" "R1")', 1)
        .replace('(footprint "T" (layer', '(footprint "T" (property "Reference" "R2") (layer', 1)
    )
    board.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "Authored width" (constraint track_width (min 0.1mm)))\n'
    )
    return board


@pytest.mark.parametrize("mode", ["default", "finite", "complete", "complete_noop"])
def test_real_route_artifacts_relocate_and_detect_each_tamper(tmp_path, mode):
    source = source_board(tmp_path)
    if mode == "complete_noop":
        source.write_text(
            source.read_text()[:-1]
            + '(segment (start 10 10) (end 13 10) (width .2) (layer "F.Cu") (net 1)))'
        )
    originals = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / "renamed.kicad_pcb"
    options = (
        ["--timeout", str(_ROUTE_WORK_SECONDS + _queue_ceiling_seconds())]
        if mode == "finite"
        else []
    )
    if mode.startswith("complete"):
        options += ["--complete", "--route-engine", "grid"]
    code = route_cmd.main(
        [
            str(source),
            "-o",
            str(output),
            "--backend",
            "python",
            "--no-auto-layers",
            "--strategy",
            "basic",
            "--no-optimize",
            *options,
        ]
    )
    assert code == 0
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert PCB.load(output).segments
    receipt = output.with_suffix(".route.json")
    assert route_receipt.verify_route_receipt(receipt) == []
    data = json.loads(receipt.read_text())
    assert data["route_exit_code"] == code
    assert b'"Authored width"' in output.with_suffix(".kicad_dru").read_bytes()
    assert data["files"]["project"]["state"] == ("absent" if mode == "complete_noop" else "present")
    relocated = tmp_path / "away"
    relocated.mkdir()
    shutil.copyfile(receipt, relocated / receipt.name)
    for record in data["files"].values():
        p = tmp_path / record["path"]
        if p.exists():
            shutil.move(p, relocated / p.name)
    moved = relocated / receipt.name
    assert route_receipt.verify_route_receipt(moved) == []
    for record in data["files"].values():
        p = relocated / record["path"]
        before = p.read_bytes() if p.exists() else None
        p.write_bytes((before or b"") + b"\n# changed\n")
        assert route_receipt.verify_route_receipt(moved), record
        if before is None:
            p.unlink()
        else:
            p.write_bytes(before)
    assert route_receipt.verify_route_receipt(moved) == []


@pytest.mark.parametrize("exit_code", [0, 2, 3, 8, 124])
def test_final_postprocessing_and_partial_status_bound(tmp_path, exit_code):
    source = source_board(tmp_path)
    output = tmp_path / "partial.kicad_pcb"
    args = route_cmd._route_parser().parse_args([str(source), "-o", str(output)])

    def action():
        route_receipt.configure(args)
        route_cmd._write_routed_pcb(source, output, "")
        # Model final native fill/repair following the atomic routing save.
        output.write_text(output.read_text() + "\n# postprocessed\n")
        return exit_code

    assert route_receipt.run(action) == exit_code
    receipt = output.with_suffix(".route.json")
    assert route_receipt.verify_route_receipt(receipt) == []
    assert json.loads(receipt.read_text())["route_exit_code"] == exit_code


@pytest.mark.parametrize("failure", ["unchanged", "staged", "fatal"])
def test_no_receipt_for_stale_staging_or_constraint_failure(tmp_path, failure):
    source = source_board(tmp_path)
    output = tmp_path / "old.kicad_pcb"
    shutil.copyfile(source, output)
    receipt = output.with_suffix(".route.json")
    receipt.write_text('{"old":true}')
    args = route_cmd._route_parser().parse_args([str(source), "-o", str(output)])

    def action():
        route_receipt.configure(args)
        if failure == "staged":
            shutil.copyfile(source, output)
        elif failure == "fatal":
            route_cmd._write_routed_pcb(source, output, "")
        return 1 if failure == "fatal" else 2

    route_receipt.run(action)
    assert not receipt.exists()


def test_receipt_alias_does_not_modify_source(tmp_path):
    source = source_board(tmp_path)
    output = tmp_path / "out.kicad_pcb"
    receipt = output.with_suffix(".route.json")
    receipt.symlink_to(source)
    original = source.read_bytes()
    args = route_cmd._route_parser().parse_args([str(source), "-o", str(output)])
    with pytest.raises(ValueError, match="aliases protected"):
        route_receipt.configure(args)
    assert source.read_bytes() == original


def test_real_partial_route_binds_saved_copper(tmp_path, monkeypatch):
    from kicad_tools.router import Autorouter

    source = source_board(tmp_path)
    source.write_text(
        source.read_text()[:-1]
        + """(footprint "T" (property "Reference" "R3") (layer "F.Cu") (at 5 5)
          (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "B")))
        (footprint "T" (property "Reference" "R4") (layer "F.Cu") (at 8 5)
          (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "B"))))"""
    )
    route = Autorouter.route_all

    def only_first_net(self, *args, **kwargs):
        all_nets = self.nets
        self.nets = {1: all_nets[1]}
        try:
            return route(self, *args, **kwargs)
        finally:
            self.nets = all_nets

    monkeypatch.setattr(Autorouter, "route_all", only_first_net)
    output = tmp_path / "partial.kicad_pcb"
    rc = route_cmd.main(
        [
            str(source),
            "-o",
            str(output),
            "--backend",
            "python",
            "--no-auto-layers",
            "--strategy",
            "basic",
            "--no-optimize",
            "--skip-drc",
        ]
    )
    assert rc == 2
    assert PCB.load(output).segments
    receipt = output.with_suffix(".route.json")
    assert route_receipt.verify_route_receipt(receipt) == []
    assert json.loads(receipt.read_text())["route_exit_code"] == 2
    assert (
        output.with_suffix(".kicad_dru").read_bytes()
        == source.with_suffix(".kicad_dru").read_bytes()
    )


@pytest.mark.parametrize("mode", ["skip_drc", "complete_noop"])
@pytest.mark.parametrize("suffix", [".kicad_pro", ".kicad_dru"])
def test_actual_route_rejects_stale_destination_constraints(tmp_path, mode, suffix):
    source = source_board(tmp_path)
    source.with_suffix(".kicad_pro").write_text('{"text_variables":{"AUTHORED":"keep"}}')
    if mode == "complete_noop":
        source.write_text(
            source.read_text()[:-1]
            + '(segment (start 10 10) (end 13 10) (width .2) (layer "F.Cu") (net 1)))'
        )
    originals = {p: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / "renamed.kicad_pcb"
    stale = output.with_suffix(suffix)
    stale.write_text(
        '{"text_variables":{"STALE":"other board"}}'
        if suffix == ".kicad_pro"
        else '(version 1)\n(rule "STALE OTHER BOARD" (constraint clearance (min .2mm)))\n'
    )
    before = stale.read_bytes()
    options = ["--skip-drc"] if mode == "skip_drc" else ["--complete", "--route-engine", "grid"]
    rc = route_cmd.main(
        [
            str(source),
            "-o",
            str(output),
            "--backend",
            "python",
            "--no-auto-layers",
            "--strategy",
            "basic",
            "--no-optimize",
            *options,
        ]
    )
    assert rc == 1
    assert PCB.load(output).segments
    assert not output.with_suffix(".route.json").exists()
    assert stale.read_bytes() == before
    assert all(p.read_bytes() == data for p, data in originals.items())
