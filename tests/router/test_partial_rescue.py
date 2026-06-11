"""Unit tests for the reusable partial-net rescue loop (Issues #3471/#3474).

The end-to-end rescue behavior is exercised by the board recipes (board
05 step 6b, chorus R2); these tests pin the pure-Python pieces: net-name
parsing, copper stripping, ``kct check`` output classification, and the
per-stage ``kct route`` command construction.
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.router.partial_rescue import (
    RescueConfig,
    all_net_names,
    build_rescue_command,
    complete_unfinished_nets,
    partially_connected_signal_nets,
    rescue_partial_nets,
    strip_net_copper,
)

# A minimal kicad_pcb skeleton: 3 nets, copper for nets 1 and 2.
# Top-level copper blocks are tab-indented exactly as kicad emits them
# (the stripper keys on ``^\t(segment|via)``).
_PCB_TEXT = (
    "(kicad_pcb\n"
    '\t(net 0 "")\n'
    '\t(net 1 "SDA")\n'
    '\t(net 2 "SCL")\n'
    '\t(net 3 "NRST")\n'
    "\t(segment\n"
    "\t\t(start 1 1)\n"
    "\t\t(end 2 2)\n"
    "\t\t(width 0.2)\n"
    '\t\t(layer "F.Cu")\n'
    "\t\t(net 1)\n"
    "\t)\n"
    "\t(segment\n"
    "\t\t(start 2 2)\n"
    "\t\t(end 3 3)\n"
    "\t\t(width 0.2)\n"
    '\t\t(layer "F.Cu")\n'
    "\t\t(net 2)\n"
    "\t)\n"
    "\t(via\n"
    "\t\t(at 2 2)\n"
    "\t\t(size 0.6)\n"
    "\t\t(net 2)\n"
    "\t)\n"
    "\t(zone\n"
    "\t\t(net 1)\n"
    '\t\t(layer "B.Cu")\n'
    "\t)\n"
    ")\n"
)


def _write_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_PCB_TEXT)
    return pcb


def test_all_net_names_parses_named_nets(tmp_path: Path) -> None:
    pcb = _write_pcb(tmp_path)
    assert all_net_names(pcb) == ["NRST", "SCL", "SDA"]


def test_strip_net_copper_removes_only_target_net(tmp_path: Path) -> None:
    pcb = _write_pcb(tmp_path)
    removed = strip_net_copper(pcb, ["SCL"])
    # SCL had one segment + one via.
    assert removed == 2
    text = pcb.read_text()
    # SDA's segment and the zone survive; net declarations survive.
    assert text.count("(segment") == 1
    assert "(via" not in text
    assert "(zone" in text
    assert '(net 2 "SCL")' in text


def test_strip_net_copper_unknown_net_is_noop(tmp_path: Path) -> None:
    pcb = _write_pcb(tmp_path)
    before = pcb.read_text()
    assert strip_net_copper(pcb, ["DOES_NOT_EXIST"]) == 0
    assert pcb.read_text() == before


def test_strip_net_copper_never_touches_zones(tmp_path: Path) -> None:
    pcb = _write_pcb(tmp_path)
    # SDA has a segment AND a zone; only the segment goes.
    removed = strip_net_copper(pcb, ["SDA"])
    assert removed == 1
    assert "(zone" in pcb.read_text()


def _fake_check_payload() -> str:
    return json.dumps(
        {
            "violations": [
                {
                    "rule": "connectivity",
                    "severity": "error",
                    "message": "Net 'SDA' is partially routed (1/3 pads)",
                },
                {
                    "rule": "connectivity",
                    "severity": "error",
                    "message": "Net 'NRST' is not routed",
                },
                {
                    "rule": "connectivity",
                    "severity": "error",
                    "message": "Net 'GNDD' is partially routed (2/40 pads)",
                },
                {
                    "rule": "connectivity",
                    "severity": "error",
                    "message": ("Net 'unconnected-(U8-PC14-Pad2)' is not routed"),
                },
                {
                    "rule": "clearance_segment_segment",
                    "severity": "error",
                    "message": "clearance violation",
                },
            ]
        }
    )


def test_partially_connected_signal_nets_classification(tmp_path: Path, monkeypatch) -> None:
    pcb = _write_pcb(tmp_path)

    class _Result:
        stdout = _fake_check_payload()
        stderr = ""
        returncode = 1

    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.subprocess.run",
        lambda *a, **k: _Result(),
    )

    # Default: partial only, pour nets and single-pad NC nets excluded.
    partial = partially_connected_signal_nets(pcb, excluded_nets=frozenset({"GNDD"}))
    assert partial == ["SDA"]

    # include_unrouted adds the not-routed class (still excluding NCs).
    both = partially_connected_signal_nets(
        pcb, excluded_nets=frozenset({"GNDD"}), include_unrouted=True
    )
    assert both == ["NRST", "SDA"]


def test_partially_connected_signal_nets_bad_json(tmp_path: Path, monkeypatch) -> None:
    pcb = _write_pcb(tmp_path)

    class _Result:
        stdout = "kct exploded"
        stderr = ""
        returncode = 2

    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.subprocess.run",
        lambda *a, **k: _Result(),
    )
    assert partially_connected_signal_nets(pcb) == []


def test_build_rescue_command_pins_single_net_recipe(tmp_path: Path) -> None:
    pcb = tmp_path / "routed.kicad_pcb"
    out = tmp_path / "routed_rescue.kicad_pcb"
    config = RescueConfig(
        manufacturer="jlcpcb-tier1",
        backend="cpp",
        seed=42,
        stage_timeout_s=300,
        per_net_timeout_s=60,
        micro_via_in_pad_fallback=True,
        extra_args=("--iterations", "50"),
    )
    cmd = build_rescue_command(pcb, out, ["SCL", "NRST"], config)

    # The load-bearing flags of the rescue mechanism (#3471).
    assert "--preserve-existing" in cmd
    assert "--skip-nets" in cmd
    assert cmd[cmd.index("--skip-nets") + 1] == "SCL,NRST"
    assert cmd[cmd.index("--seed") + 1] == "42"
    assert cmd[cmd.index("--timeout") + 1] == "300"
    assert cmd[cmd.index("--per-net-timeout") + 1] == "60"
    assert cmd[cmd.index("--manufacturer") + 1] == "jlcpcb-tier1"
    assert "--micro-via-in-pad-fallback" in cmd
    # Pinned 4L (no escalation ladder inside a rescue stage).
    assert cmd[cmd.index("--starting-layers") + 1] == "4"
    assert cmd[cmd.index("--max-layers") + 1] == "4"
    # extra_args appended verbatim.
    assert cmd[-2:] == ["--iterations", "50"]


def test_build_rescue_command_omits_micro_via_by_default(tmp_path: Path) -> None:
    cmd = build_rescue_command(
        tmp_path / "a.kicad_pcb",
        tmp_path / "b.kicad_pcb",
        ["X"],
        RescueConfig(),
    )
    assert "--micro-via-in-pad-fallback" not in cmd


def test_rescue_failed_stage_strips_stubs(tmp_path: Path, monkeypatch) -> None:
    """A failed rescue must leave the target net with NO copper (#3470)."""
    pcb = _write_pcb(tmp_path)

    def _fake_run(cmd, **kwargs):
        class _Result:
            returncode = 3
            stdout = ""
            stderr = ""

        # Simulate kct route producing a (partial) output file: copy the
        # input (which still contains SCL's stranded copper because the
        # upfront strip only removed the explicit rescue targets).
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text(Path(cmd[4]).read_text())
        return _Result()

    monkeypatch.setattr("kicad_tools.router.partial_rescue.subprocess.run", _fake_run)

    results = rescue_partial_nets(pcb, RescueConfig(), nets=["SDA"], quiet=True)
    assert results == {"SDA": False}
    text = pcb.read_text()
    # SDA's segment stripped (upfront strip), zone untouched, SCL intact.
    assert text.count("(segment") == 1  # SCL's
    assert "(zone" in text
    # No *_rescue side files left behind.
    assert (
        list(tmp_path.glob("*_rescue*"))
        == [
            # the rescue output was promoted onto pcb itself, so no stray
        ]
        or not list(tmp_path.glob("*_rescue*"))
    )


def test_rescue_successful_stage_promotes_output(tmp_path: Path, monkeypatch) -> None:
    pcb = _write_pcb(tmp_path)
    marker = "\t(segment\n\t\t(start 9 9)\n\t\t(end 9 8)\n\t\t(net 1)\n\t)\n"

    def _fake_run(cmd, **kwargs):
        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        src = Path(cmd[4]).read_text()
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text(src.replace("(kicad_pcb\n", "(kicad_pcb\n" + marker, 1))
        return _Result()

    monkeypatch.setattr("kicad_tools.router.partial_rescue.subprocess.run", _fake_run)

    results = rescue_partial_nets(pcb, RescueConfig(), nets=["SDA"], quiet=True)
    assert results == {"SDA": True}
    assert "(start 9 9)" in pcb.read_text()


# ---------------------------------------------------------------------------
# complete_unfinished_nets (batch completion passes, issue #3474 R2)
# ---------------------------------------------------------------------------


def test_completion_no_unfinished_nets_is_noop(tmp_path: Path, monkeypatch) -> None:
    pcb = _write_pcb(tmp_path)
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.subprocess.run",
        lambda cmd, **k: calls.append(cmd),
    )

    history = complete_unfinished_nets(pcb, RescueConfig(), quiet=True)
    assert history == []
    assert calls == []  # no route subprocess launched


def test_completion_progress_promotes_and_iterates(tmp_path: Path, monkeypatch) -> None:
    """Targets shrink 2 -> 1 -> 0 across two passes; both kept."""
    pcb = _write_pcb(tmp_path)

    # Scripted unfinished-net detection: before pass 1 -> [SCL, SDA];
    # after pass 1 -> [SDA]; after pass 2 -> [].
    detections = iter([["SCL", "SDA"], ["SDA"], ["SDA"], []])
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: next(detections),
    )

    # Pass 1 lands SCL's copper (net 2) -- SCL leaves the target set, so
    # its marker must survive pass 2's strip.  Pass 2 lands SDA (net 1).
    marker_iter = iter(["(start 7 7)\n\t\t(net 2)", "(start 8 8)\n\t\t(net 1)"])

    def _fake_run(cmd, **kwargs):
        class _Result:
            returncode = 1
            stdout = ""
            stderr = ""

        src = Path(cmd[4]).read_text()
        out = Path(cmd[cmd.index("--output") + 1])
        marker = f"\t(segment\n\t\t{next(marker_iter)}\n\t)\n"
        out.write_text(src.replace("(kicad_pcb\n", "(kicad_pcb\n" + marker, 1))
        return _Result()

    monkeypatch.setattr("kicad_tools.router.partial_rescue.subprocess.run", _fake_run)

    history = complete_unfinished_nets(pcb, RescueConfig(), max_passes=3, quiet=True)
    assert history == [(2, 1), (1, 0)]
    text = pcb.read_text()
    # Both passes' output survived.
    assert "(start 7 7)" in text and "(start 8 8)" in text
    # No side files left behind.
    assert not list(tmp_path.glob("*_completion*"))
    assert not list(tmp_path.glob("*_prepass*"))


def test_completion_no_progress_restores_backup(tmp_path: Path, monkeypatch) -> None:
    """A pass that does not reduce the unfinished count is discarded."""
    pcb = _write_pcb(tmp_path)
    original = pcb.read_text()

    detections = iter([["SCL", "SDA"], ["SCL", "SDA"]])
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: next(detections),
    )

    def _fake_run(cmd, **kwargs):
        class _Result:
            returncode = 1
            stdout = ""
            stderr = ""

        src = Path(cmd[4]).read_text()
        out = Path(cmd[cmd.index("--output") + 1])
        # Output adds junk stub copper but completes nothing.
        marker = "\t(segment\n\t\t(start 6 6)\n\t\t(net 2)\n\t)\n"
        out.write_text(src.replace("(kicad_pcb\n", "(kicad_pcb\n" + marker, 1))
        return _Result()

    monkeypatch.setattr("kicad_tools.router.partial_rescue.subprocess.run", _fake_run)

    history = complete_unfinished_nets(pcb, RescueConfig(), max_passes=3, quiet=True)
    assert history == [(2, 2)]
    # Pre-pass board restored byte-for-byte (junk stub gone, stripped
    # copper back).
    assert pcb.read_text() == original
    assert not list(tmp_path.glob("*_prepass*"))
