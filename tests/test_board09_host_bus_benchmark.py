"""Fast gates for the real Board09 routing diagnostic."""

import importlib.util
import json
from pathlib import Path

import pytest

PATH = (
    Path(__file__).parents[1]
    / "boards/09-usbc-pd-power/engineering/routing-investigation/host-bus/benchmark.py"
)
spec = importlib.util.spec_from_file_location("host_bus_benchmark", PATH)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_partial_branch_does_not_complete_multiterminal_net():
    pads = {name: {f"{name}.1", f"{name}.2", f"{name}.3"} for name in benchmark.TARGETS}
    groups = [{"SCL.1", "SCL.2"}, {"SCL.3"}, pads["SDA"]]
    result = benchmark.classify(pads, groups)
    assert result["SCL"]["status"] == "partial"
    assert result["SDA"]["status"] == "complete"
    assert result["MON_ALERT"]["status"] == "unrouted"
    assert json.loads(json.dumps(result)) == result


def test_missing_targets_fail_closed():
    result = benchmark.classify({}, [])
    assert len(result) == 4
    assert all(info["status"] == "missing" for info in result.values())


def test_scratch_commands_keep_strict_settings_and_both_project_names(tmp_path):
    for command_file in ("numbered-command.json", "fine-command.json"):
        directory = tmp_path / command_file
        command = benchmark.prepare_case(directory, command_file)
        assert "--preserve-existing" in command
        assert "--strict-layers" in command
        assert command[command.index("--nets") + 1] == ",".join(benchmark.TARGETS)
        assert command[command.index("--seed") + 1] == "42"
        assert command[command.index("--iterations") + 1] == "8"
        assert (directory / "support.kicad_pro").read_bytes() == (
            directory / "usbc_pd_power.kicad_pro"
        ).read_bytes()
        assert Path(command[4]).parent == directory
        assert Path(command[command.index("-o") + 1]).parent == directory


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"violations": [], "unconnected_items": [{}]},
        {"violations": [{}], "unconnected_items": []},
    ],
)
def test_native_invalid_reports_do_not_pass(report):
    assert benchmark.native_status(0, report)["status"] != "passed"


def test_native_failure_and_missing_tool(tmp_path, monkeypatch):
    assert (
        benchmark.native_status(1, {"violations": [], "unconnected_items": []})["status"]
        == "failed"
    )
    monkeypatch.setattr(benchmark.shutil, "which", lambda _: None)
    assert benchmark.validate_native(tmp_path / "support.kicad_pcb")["status"] == "unavailable"


def test_native_clean_report():
    assert (
        benchmark.native_status(0, {"violations": [], "unconnected_items": []})["status"]
        == "passed"
    )


def test_native_refills_and_rejects_missing_report(tmp_path, monkeypatch):
    from types import SimpleNamespace

    seen = []
    monkeypatch.setattr(benchmark.shutil, "which", lambda _: "/fake/kicad-cli")

    def run(command, **kwargs):
        seen.extend(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    result = benchmark.validate_native(tmp_path / "support.kicad_pcb")
    assert "--refill-zones" in seen and "--save-board" in seen
    assert result["status"] == "failed"


@pytest.mark.parametrize(
    "failure",
    [None, "native", "partial", "missing", "inner", "copper", "placement", "targets", "router"],
)
def test_success_requires_all_independent_gates(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace

    def run(command, **kwargs):
        (tmp_path / "case/support.kicad_pcb").write_text("mock routed output")
        return SimpleNamespace(returncode=1 if failure == "router" else 0)

    monkeypatch.setattr(benchmark.subprocess, "run", run)
    monkeypatch.setattr(
        benchmark,
        "validate_native",
        lambda _: {"status": "failed" if failure == "native" else "passed"},
    )
    info = {
        "nets": {name: {"status": "complete"} for name in benchmark.TARGETS},
        "targets_preserved": failure != "targets",
        "existing_copper_preserved": failure != "copper",
        "placement_preserved": failure != "placement",
        "target_copper_outer_only": failure != "inner",
    }
    if failure == "partial":
        info["nets"]["SCL"]["status"] = "partial"
    if failure == "missing":
        info["nets"] = {}
    monkeypatch.setattr(benchmark, "inspect_output", lambda *args: info)
    result = benchmark.run_case(tmp_path / "case", "numbered-command.json")
    assert result["success"] is (failure is None)
    assert json.loads((tmp_path / "case/result.json").read_text()) == result


def test_copper_preservation_includes_zone_boundary_but_allows_refill():
    from types import SimpleNamespace

    from kicad_tools.schema.pcb import Zone

    zone = Zone(net_number=1, net_name="GND", layer="In1.Cu", polygon=[(0, 0), (1, 0), (1, 1)])
    board = SimpleNamespace(segments=[], vias=[], zones=[zone])
    before = benchmark.copper(board)
    zone.filled_polygons = [[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]]
    zone.is_filled = True
    assert benchmark.copper(board) == before
    zone.polygon[0] = (0.2, 0.2)
    assert before - benchmark.copper(board)


def test_no_router_output_is_unknown_not_zero_net_success(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        benchmark.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    result = benchmark.run_case(tmp_path / "case", "numbered-command.json")
    assert result["success"] is False
    assert set(result["nets"]) == set(benchmark.TARGETS)
    assert all(n["status"] == "unknown" for n in result["nets"].values())
    assert result["native_validation"]["status"] == "unavailable"
