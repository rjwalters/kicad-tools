"""Real CLI acceptance for useful routing around invalid placement (#5348)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from tests.test_routing_placement_disposition import board_text


@pytest.mark.parametrize("entry", ["inner", "outer"])
@pytest.mark.parametrize("finite", [False, True])
@pytest.mark.parametrize(
    "selection",
    [
        "mixed",
        "coupled",
        "valid_only",
        "skip_invalid",
        "complete_noop",
        "complete_partial",
        "complete_excluded_noop",
    ],
)
def test_partial_placement_cli_preserves_and_reports(
    tmp_path, entry, finite, selection, extra_options=()
):
    board = tmp_path / "mixed.kicad_pcb"
    text = board_text()
    if selection.startswith("complete_"):
        additions = """
        (segment (start 106 103) (end 108 103) (width 0.2) (layer "F.Cu") (net 1))
        (segment (start 108 103) (end 125 105) (width 0.2) (layer "F.Cu") (net 1))
        (segment (start 105 107) (end 110 107) (width 0.2) (layer "F.Cu") (net 2))
        (segment (start 105 109) (end 110 109) (width 0.2) (layer "F.Cu") (net 3))
        """
        if selection == "complete_partial":
            additions = additions.replace(
                '(segment (start 105 107) (end 110 107) (width 0.2) (layer "F.Cu") (net 2))',
                "",
            )
        text = text.rstrip()[:-1] + additions + ")"
    board.write_text(text)
    original = board.read_bytes()
    output = tmp_path / "routed.kicad_pcb"
    report = tmp_path / "complete.json"
    failed = tmp_path / "failed.json"
    sidecar = tmp_path / "classes.json"
    sidecar.write_text(
        json.dumps(
            {"BAD": {"name": "Pair", "coupled_routing": True, "diffpair_partner": "PARTNER"}}
        )
    )
    options = {
        "mixed": [],
        "coupled": ["--net-class-map", str(sidecar)],
        "valid_only": ["--nets", "GOOD"],
        "skip_invalid": ["--skip-nets", "BAD"],
        "complete_noop": ["--complete"],
        "complete_partial": ["--complete"],
        "complete_excluded_noop": ["--complete", "--complete-exclude-nets", "BAD,PLANE"],
    }[selection]
    argv = [
        str(board),
        "-o",
        str(output),
        "--complete-report",
        str(report),
        "--export-failed-nets",
        str(failed),
        *options,
        *extra_options,
    ]
    if finite:
        argv += ["--timeout", "30"]
    code = (
        "from kicad_tools.cli.route_cmd import main; import sys; sys.exit(main(sys.argv[1:]))"
        if entry == "inner"
        else 'from kicad_tools.cli import main; import sys; sys.exit(main(["route", *sys.argv[1:]]))'
    )
    result = subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert board.read_bytes() == original
    assert output.is_file(), result.stdout + result.stderr
    if selection == "complete_partial" or (extra_options and selection == "mixed"):
        # The retained off-board trace can also trigger the DRC exit (3).
        # Placement must preserve that stronger failure, never turn it into success.
        assert result.returncode in {2, 3}
    else:
        assert result.returncode == (2 if selection in {"mixed", "coupled", "complete_noop"} else 0)
    if selection in {"mixed", "coupled", "complete_noop", "complete_partial"}:
        assert "SUCCESS:" not in result.stdout
        assert "Design routed successfully" not in result.stdout
        assert "Minimum viable configuration found" not in result.stdout
    parsed = PCB.load(output)
    assert any(segment.net_name == "GOOD" for segment in parsed.segments)
    before = PCB.load(board)

    def identities(pcb):
        return sorted((f.reference, p.number, p.net_name) for f in pcb.footprints for p in f.pads)

    def bad_copper(pcb):
        return sorted(
            (s.start, s.end, s.width, s.layer) for s in pcb.segments if s.net_name == "BAD"
        )

    assert identities(parsed) == identities(before)
    assert bad_copper(parsed) == bad_copper(before)
    assert len([v for v in parsed.vias if v.net_name == "BAD"]) == 1
    disposition = json.loads(report.read_text())["placement_disposition"]
    assert disposition["direct_invalid_nets"] == ["BAD"]
    assert disposition["requested_blocked_nets"] == (
        ["BAD", "PARTNER"]
        if selection == "coupled"
        else ["BAD"]
        if selection in {"mixed", "complete_noop", "complete_partial"}
        else []
    )
    assert "GOOD" in disposition["completed_nets"]

    if selection == "complete_partial":
        assert disposition["plane_excluded_nets"] == []
        assert "PARTNER" in disposition["completed_nets"]
    if selection == "complete_excluded_noop":
        assert disposition["plane_excluded_nets"] == ["BAD", "PLANE"]

    if selection == "coupled":
        assert disposition["coupled_invalid_nets"] == ["PARTNER"]
        assert not any(s.net_name == "PARTNER" for s in parsed.segments)

    if disposition["requested_blocked_nets"]:
        entries = json.loads(failed.read_text())
        blocked = [
            entry for entry in entries if entry["status"] == "placement-invalid, not attempted"
        ]
        assert sorted(entry["net"] for entry in blocked) == disposition["requested_blocked_nets"]
        assert all(entry["attempted"] is False for entry in blocked)
        assert all(entry["status"] != "unrouted" for entry in entries if entry["net"] == "BAD")


@pytest.mark.parametrize("suffix", [".json", ".txt"])
def test_all_invalid_export_replaces_stale_attempt(tmp_path, suffix):
    from kicad_tools.cli.route_cmd import main

    source = tmp_path / "mixed.kicad_pcb"
    source.write_text(board_text())
    target = tmp_path / ("failed" + suffix)
    target.write_text(
        '[{"net": "STALE", "status": "unrouted"}]' if suffix == ".json" else "STALE\n"
    )
    assert main([str(source), "--nets", "BAD", "--export-failed-nets", str(target)]) == 2
    assert "STALE" not in target.read_text()
    if suffix == ".json":
        entries = json.loads(target.read_text())
        assert entries == [
            {
                "net": "BAD",
                "status": "placement-invalid, not attempted",
                "attempted": False,
                "pads": ["X1.1", "X2.1", "X3.1"],
            }
        ]
    else:
        assert target.read_text() == "# placement-invalid, not attempted\nBAD\n"


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(["--no-auto-layers", "--strategy", "basic"], id="basic"),
        pytest.param(["--no-auto-layers", "--strategy", "monte-carlo"], id="monte-carlo"),
        pytest.param(["--no-auto-layers", "--strategy", "evolutionary"], id="evolutionary"),
        pytest.param(
            ["--no-auto-layers", "--strategy", "basic", "--route-engine", "mesh"], id="mesh"
        ),
        pytest.param(
            ["--no-auto-layers", "--strategy", "basic", "--route-engine", "lattice"], id="lattice"
        ),
        pytest.param(["--no-auto-layers", "--adaptive-rules"], id="adaptive-rules"),
        pytest.param(["--adaptive-rules"], id="combined"),
        pytest.param(["--region", "0,0,20,12"], id="region"),
    ],
)
def test_partial_placement_route_modes(tmp_path, options):
    test_partial_placement_cli_preserves_and_reports(
        tmp_path, "outer", True, "mixed", extra_options=options
    )
