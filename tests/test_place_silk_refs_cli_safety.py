"""Public input, output, and verification contracts for reference placement."""

from pathlib import Path

import pytest

from kicad_tools.cli import place_silk_refs_cmd as cli
from kicad_tools.silkscreen.place_refs import SilkRefPlacer


@pytest.fixture
def board(tmp_path: Path) -> Path:
    path = tmp_path / "input.kicad_pcb"
    path.write_text(
        "(kicad_pcb (version 20240108) (generator test) "
        '(general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal)))'
    )
    return path


@pytest.mark.parametrize(
    "parameter,value",
    [
        ("step_mm", 0),
        ("step_mm", -1),
        ("step_mm", float("nan")),
        ("step_mm", float("inf")),
        ("step_mm", 1e-300),
        ("max_offset_mm", -1),
        ("max_offset_mm", float("inf")),
        ("clearance_mm", -1),
        ("clearance_mm", float("nan")),
        ("edge_clearance_mm", -1),
        ("mask_clearance_mm", float("inf")),
    ],
)
def test_invalid_search_inputs_fail_before_mutation(board: Path, parameter: str, value: float):
    original = board.read_bytes()
    placer = SilkRefPlacer(board)
    with pytest.raises(ValueError):
        placer.plan(**{parameter: value})
    assert board.read_bytes() == original


def test_cli_rejects_zero_step_without_traceback(board: Path, capsys):
    original = board.read_bytes()
    assert cli.main([str(board), "--step", "0"]) == 1
    assert "step_mm must be finite and positive" in capsys.readouterr().err
    assert board.read_bytes() == original


def test_noop_creates_explicit_output_before_verification(board: Path, tmp_path: Path, monkeypatch):
    output = tmp_path / "output.kicad_pcb"
    original = board.read_bytes()
    verified = []

    def verify(path):
        assert path == output
        assert path.is_file()
        SilkRefPlacer(path)
        verified.append(path)
        return {"available": True, "silk_violations": 0}

    monkeypatch.setattr(cli, "_run_verify_drc", verify)
    assert cli.main([str(board), "-o", str(output), "--verify-drc"]) == 0
    assert verified == [output]
    assert board.read_bytes() == original


def test_dry_run_does_not_create_explicit_output(board: Path, tmp_path: Path):
    output = tmp_path / "output.kicad_pcb"
    assert cli.main([str(board), "-o", str(output), "--dry-run"]) == 0
    assert not output.exists()


@pytest.mark.parametrize("output_format", ["text", "summary", "json"])
@pytest.mark.parametrize(
    "result,expected_code,expected_text",
    [
        ({"available": False, "message": "tool unavailable"}, 1, "tool unavailable"),
        ({"available": True, "error": "native failure"}, 1, "native failure"),
        ({"available": True, "silk_violations": 2}, 1, "2"),
        ({"available": True, "silk_violations": 0}, 0, "0"),
    ],
)
def test_verification_is_visible_and_controls_exit(
    board: Path, monkeypatch, capsys, output_format, result, expected_code, expected_text
):
    monkeypatch.setattr(cli, "_run_verify_drc", lambda path: result)
    assert cli.main([str(board), "--verify-drc", "--format", output_format]) == expected_code
    assert expected_text in capsys.readouterr().out


def test_zero_radius_with_tiny_step_has_one_ring():
    from kicad_tools.silkscreen.place_refs import _candidate_points

    points = _candidate_points((0, 0, 1, 1), 1, 1, 0, 1e-300)
    assert len(points) == 8


def test_unplaceable_text_does_not_claim_all_references_clear(capsys):
    from kicad_tools.silkscreen.place_refs import PlaceSilkRefsResult, RefPlacement

    result = PlaceSilkRefsResult(clearance_mm=0.15)
    result.placements.append(
        RefPlacement(
            footprint_ref="R1",
            layer="F.SilkS",
            old_position=(0, 0),
            new_position=(0, 0),
            old_rotation=0,
            new_rotation=0,
            status="unplaceable",
            reason="board outline unavailable",
        )
    )
    cli._print_text(result, False)
    output = capsys.readouterr().out
    assert "every visible reference is already clear" not in output
    assert "board outline unavailable" in output


@pytest.mark.parametrize(
    "report_text,return_code", [("", 0), ("{}", 0), ("bad", 0), ('{"violations": []}', 2)]
)
def test_native_failure_or_malformed_report_is_explicit(
    board, tmp_path, monkeypatch, report_text, return_code
):
    from kicad_tools.cli import runner

    report_path = tmp_path / "native.json"
    report_path.write_text(report_text)
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("kicad-cli"))
    monkeypatch.setattr(
        runner,
        "run_drc",
        lambda *args: runner.KiCadCLIResult(
            success=True, output_path=report_path, return_code=return_code
        ),
    )
    result = cli._run_verify_drc(board)
    assert result["available"] is True
    assert result["error"]
    assert not report_path.exists()


def test_native_invocation_exception_is_explicit(board, monkeypatch):
    from kicad_tools.cli import runner

    def fail(*args):
        raise OSError("native execution unavailable")

    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("kicad-cli"))
    monkeypatch.setattr(runner, "run_drc", fail)
    assert "native execution unavailable" in cli._run_verify_drc(board)["error"]
