"""Issue #8299: every optimize-traces pass is switchable; ``--net`` is exact."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from kicad_tools.cli.optimize_cmd import main
from kicad_tools.router.optimizer.pcb import parse_segments

HEADER = """\
(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(general (thickness 1.6))
\t(paper "A4")
\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "PGND")
\t(net 3 "+3.3V")
	(gr_rect (start 0 0) (end 60 60) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))
"""

# GND / PGND: two collinear segments each (merge candidates).
# +3.3V: a staircase of alternating H / diagonal steps plus a detour chain.
SEGS = """\
\t(segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net 1) (uuid "g1"))
\t(segment (start 20 10) (end 30 10) (width 0.25) (layer "F.Cu") (net 1) (uuid "g2"))
\t(segment (start 10 20) (end 20 20) (width 0.25) (layer "F.Cu") (net 2) (uuid "p1"))
\t(segment (start 20 20) (end 30 20) (width 0.25) (layer "F.Cu") (net 2) (uuid "p2"))
\t(segment (start 10 40) (end 12 40) (width 0.25) (layer "F.Cu") (net 3) (uuid "v1"))
\t(segment (start 12 40) (end 13 41) (width 0.25) (layer "F.Cu") (net 3) (uuid "v2"))
\t(segment (start 13 41) (end 15 41) (width 0.25) (layer "F.Cu") (net 3) (uuid "v3"))
\t(segment (start 15 41) (end 16 42) (width 0.25) (layer "F.Cu") (net 3) (uuid "v4"))
\t(segment (start 16 42) (end 18 42) (width 0.25) (layer "F.Cu") (net 3) (uuid "v5"))
\t(segment (start 18 42) (end 18 45) (width 0.25) (layer "F.Cu") (net 3) (uuid "v6"))
\t(segment (start 18 45) (end 22 45) (width 0.25) (layer "F.Cu") (net 3) (uuid "v7"))
)
"""

NO_FLAGS = [
    "--no-merge",
    "--no-zigzag",
    "--no-45",
    "--no-jogs",
    "--no-staircase",
    "--no-pull-tight",
    "--no-detour",
]


@pytest.fixture
def pcb(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(HEADER + SEGS)
    return p


def _geometry(path: Path) -> dict[str, Counter]:
    out = {}
    for net, segs in parse_segments(path.read_text()).items():
        out[net] = Counter(
            (
                *sorted([(round(s.x1, 4), round(s.y1, 4)), (round(s.x2, 4), round(s.y2, 4))]),
                s.layer,
                s.width,
            )
            for s in segs
        )
    return out


def test_all_passes_disabled_preserves_geometry(pcb: Path, tmp_path: Path):
    out = tmp_path / "out.kicad_pcb"
    assert main([str(pcb), "-o", str(out), "--quiet", *NO_FLAGS]) == 0
    assert _geometry(out) == _geometry(pcb)


def test_all_passes_disabled_touches_no_nets(pcb: Path, tmp_path: Path, capsys):
    out = tmp_path / "out.kicad_pcb"
    args = [str(pcb), "-o", str(out), *NO_FLAGS, "--format", "json"]
    assert main(args) == 0
    stats = json.loads(capsys.readouterr().out)["stats"]
    assert stats["nets_touched"] == []
    assert _geometry(out) == _geometry(pcb)


def test_default_run_does_change_something(pcb: Path, tmp_path: Path):
    out = tmp_path / "out.kicad_pcb"
    assert main([str(pcb), "-o", str(out), "--quiet"]) == 0
    assert _geometry(out) != _geometry(pcb)


@pytest.mark.parametrize(
    "flag,field",
    [
        ("--no-staircase", "compress_staircase"),
        ("--no-pull-tight", "pull_tight"),
        ("--no-detour", "shorten_detours"),
    ],
)
def test_new_flag_disables_only_its_own_pass(pcb: Path, capsys, flag: str, field: str):
    assert main([str(pcb), "--dry-run", "--format", "json", flag]) == 0
    opts = json.loads(capsys.readouterr().out)["optimizations"]
    for key in ("compress_staircase", "pull_tight", "shorten_detours"):
        assert opts[key] is (key != field)
    for key in ("merge_collinear", "eliminate_zigzags", "convert_45_corners", "remove_jogs"):
        assert opts[key] is True


def test_text_output_lists_all_passes(pcb: Path, capsys):
    assert main([str(pcb), "--dry-run"]) == 0
    out = capsys.readouterr().out
    for label in ("Staircase compression", "PullTight", "Detour shortening", "jog removal"):
        assert label in out


def _touched(capsys) -> list[str]:
    return json.loads(capsys.readouterr().out)["stats"]["nets_touched"]


def test_net_is_exact_and_case_sensitive(pcb: Path, tmp_path: Path, capsys):
    out = tmp_path / "out.kicad_pcb"
    assert main([str(pcb), "-o", str(out), "--net", "GND", "--format", "json"]) == 0
    assert _touched(capsys) == ["GND"]
    before, after = _geometry(pcb), _geometry(out)
    assert after["PGND"] == before["PGND"]
    assert after["GND"] != before["GND"]
    assert main([str(pcb), "--dry-run", "--net", "gnd", "--format", "json"]) == 0
    assert _touched(capsys) == []


def test_net_pattern_matches_both(pcb: Path, capsys):
    assert main([str(pcb), "--dry-run", "--net-pattern", "*GND*", "--format", "json"]) == 0
    assert sorted(_touched(capsys)) == ["GND", "PGND"]


def test_net_and_pattern_are_mutually_exclusive(pcb: Path):
    with pytest.raises(SystemExit) as exc:
        main([str(pcb), "--net", "GND", "--net-pattern", "*GND*"])
    assert exc.value.code != 0


def test_net_with_glob_characters_is_literal(pcb: Path, capsys):
    assert main([str(pcb), "--dry-run", "--net", "+3.3V", "--format", "json"]) == 0
    assert _touched(capsys) in (["+3.3V"], [])
    payload_nets = main([str(pcb), "--dry-run", "--net", "*", "--format", "json"])
    assert payload_nets == 0
    assert _touched(capsys) == []


def test_nonexistent_net_reports_no_match(pcb: Path, capsys):
    assert main([str(pcb), "--dry-run", "--net", "NOPE"]) == 0
    assert re.search(r"Nets touched: none \(no net matched", capsys.readouterr().out)


def test_text_summary_lists_touched_nets(pcb: Path, capsys):
    assert main([str(pcb), "--dry-run", "--net", "GND"]) == 0
    assert "Nets touched (1): GND" in capsys.readouterr().out


def test_flags_reachable_through_kct_entry_point(pcb: Path, capsys):
    """The outer parser and forwarding shim must accept every new flag (#8299)."""
    from kicad_tools.cli import main as kct_main

    argv = ["optimize-traces", str(pcb), "--dry-run", "--format", "json", *NO_FLAGS]
    assert kct_main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    opts = payload["optimizations"]
    assert not any(opts[k] for k in opts if k != "chamfer_size_mm")
    assert payload["stats"]["nets_touched"] == []

    argv = ["optimize-traces", str(pcb), "--dry-run", "--format", "json", "--net-pattern", "*GND*"]
    assert kct_main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["net_pattern"] == "*GND*"
    assert sorted(payload["stats"]["nets_touched"]) == ["GND", "PGND"]
