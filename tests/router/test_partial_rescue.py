"""Unit tests for the reusable partial-net rescue loop (Issues #3471/#3474).

The end-to-end rescue behavior is exercised by the board recipes (board
05 step 6b, chorus R2); these tests pin the pure-Python pieces: net-name
parsing, copper stripping, ``kct check`` output classification, and the
per-stage ``kct route`` command construction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from kicad_tools.router.partial_rescue import (
    CompletionResult,
    RescueConfig,
    UnroutableLink,
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


def test_build_rescue_command_uses_wall_clock_per_net_by_default(tmp_path: Path) -> None:
    """Without deterministic-budget the legacy wall-clock cutoff is emitted (#3877)."""
    cmd = build_rescue_command(
        tmp_path / "a.kicad_pcb",
        tmp_path / "b.kicad_pcb",
        ["X"],
        RescueConfig(per_net_timeout_s=60),
    )
    # Legacy behaviour preserved bit-for-bit when the flag is off.
    assert "--per-net-timeout" in cmd
    assert cmd[cmd.index("--per-net-timeout") + 1] == "60"
    assert "--deterministic-budget" not in cmd


def test_build_rescue_command_deterministic_budget_replaces_per_net_timeout(
    tmp_path: Path,
) -> None:
    """Issue #3877: deterministic-budget drops the wall-clock per-net cutoff.

    A rescue/completion subprocess routed under deterministic-budget must NOT
    carry ``--per-net-timeout`` (the load-dependent wall-clock cutoff) -- it is
    replaced by ``--deterministic-budget`` so the rescued copper is
    reproducible regardless of machine load.  The outer ``--timeout`` (stage
    budget) is retained as a safety backstop.
    """
    cmd = build_rescue_command(
        tmp_path / "a.kicad_pcb",
        tmp_path / "b.kicad_pcb",
        ["SCL", "NRST"],
        RescueConfig(
            stage_timeout_s=300,
            per_net_timeout_s=60,
            deterministic_budget=True,
        ),
    )
    assert "--deterministic-budget" in cmd
    # The wall-clock per-net cutoff is GONE under deterministic-budget.
    assert "--per-net-timeout" not in cmd
    # The outer stage timeout is retained as a safety backstop.
    assert cmd[cmd.index("--timeout") + 1] == "300"
    # skip-nets/seed/preserve still present (recipe otherwise unchanged).
    assert cmd[cmd.index("--skip-nets") + 1] == "SCL,NRST"
    assert "--preserve-existing" in cmd


def test_build_rescue_command_emits_allow_unsafe_grid_when_set(tmp_path: Path) -> None:
    """Issue #4528: a board whose main pass opts into the coarse grid must
    forward ``--allow-unsafe-grid`` to the rescue subprocess, or the #3911
    auto-grid gate refuses the grid and exits 1 before routing anything."""
    cmd = build_rescue_command(
        tmp_path / "a.kicad_pcb",
        tmp_path / "b.kicad_pcb",
        ["SCL", "NRST"],
        RescueConfig(allow_unsafe_grid=True),
    )
    assert "--allow-unsafe-grid" in cmd


def test_build_rescue_command_emits_allow_unsafe_grid_in_complete_shape(
    tmp_path: Path,
) -> None:
    """Issue #4528: the flag propagates to the completion (lattice) shape too --
    ``_COMPLETION_CONFIG`` inherits it via ``dataclasses.replace(_RESCUE_CONFIG)``."""
    cmd = build_rescue_command(
        tmp_path / "a.kicad_pcb",
        tmp_path / "b.kicad_pcb",
        [],
        RescueConfig(allow_unsafe_grid=True),
        complete=True,
    )
    assert "--allow-unsafe-grid" in cmd
    assert "--complete" in cmd


def test_build_rescue_command_omits_allow_unsafe_grid_by_default(tmp_path: Path) -> None:
    """Issue #4528: a board that does NOT opt into the unsafe grid emits
    byte-identical rescue argv (no flag) -- the knob is off by default."""
    cmd = build_rescue_command(
        tmp_path / "a.kicad_pcb",
        tmp_path / "b.kicad_pcb",
        ["X"],
        RescueConfig(),
    )
    assert "--allow-unsafe-grid" not in cmd


# ---------------------------------------------------------------------------
# build_rescue_command completion shape (issue #4478, epic #4465 Phase 5)
# ---------------------------------------------------------------------------


def test_build_rescue_command_complete_shape(tmp_path: Path) -> None:
    """complete=True shells ``kct route --complete`` -- NOT the grid-engine shape.

    The completion shape must (a) emit ``--complete``, (b) NOT emit
    ``--skip-nets``/``--nets`` (which trip the mutual-exclusivity guard,
    route_cmd.py:8805), and (c) NOT emit ``--auto-layers`` (completion routes
    within the committed layer stack -- issue #4477).
    """
    report = tmp_path / "report.json"
    cmd = build_rescue_command(
        tmp_path / "routed.kicad_pcb",
        tmp_path / "out.kicad_pcb",
        # A skip list is passed but MUST be ignored in complete mode.
        ["SCL", "NRST"],
        RescueConfig(manufacturer="jlcpcb-tier1", seed=7, stage_timeout_s=600),
        complete=True,
        complete_report=report,
    )
    assert "--complete" in cmd
    # Mutual-exclusivity guard (route_cmd.py:8805): never combine --complete
    # with a hand-enumerated net set.
    assert "--skip-nets" not in cmd
    assert "--nets" not in cmd
    # Completion works within the committed layer stack (#4477): no whole-board
    # layer escalation.
    assert "--auto-layers" not in cmd
    # --preserve-existing is still present (harmless: --complete implies it).
    assert "--preserve-existing" in cmd
    # The Phase 4 structured report is requested at the given path.
    assert cmd[cmd.index("--complete-report") + 1] == str(report)
    # Recipe knobs still flow through.
    assert cmd[cmd.index("--manufacturer") + 1] == "jlcpcb-tier1"
    assert cmd[cmd.index("--seed") + 1] == "7"
    assert cmd[cmd.index("--timeout") + 1] == "600"


def test_build_rescue_command_complete_omits_report_when_none(tmp_path: Path) -> None:
    cmd = build_rescue_command(
        tmp_path / "a.kicad_pcb",
        tmp_path / "b.kicad_pcb",
        [],
        RescueConfig(),
        complete=True,
    )
    assert "--complete" in cmd
    assert "--complete-report" not in cmd


def test_build_rescue_command_default_is_grid_rescue_shape(tmp_path: Path) -> None:
    """complete defaults False: the single-net rescue shape is byte-unchanged."""
    cmd = build_rescue_command(
        tmp_path / "a.kicad_pcb",
        tmp_path / "b.kicad_pcb",
        ["X", "Y"],
        RescueConfig(),
    )
    assert "--complete" not in cmd
    assert "--complete-report" not in cmd
    assert "--auto-layers" in cmd
    assert cmd[cmd.index("--skip-nets") + 1] == "X,Y"


def test_build_rescue_command_golden_argv_unchanged(tmp_path: Path) -> None:
    """AC item 4: the default (single-net rescue) argv is byte-identical to the
    pre-#4478 shape -- boards that never hit the completion path are unaffected."""
    routed = tmp_path / "routed.kicad_pcb"
    out = tmp_path / "routed_rescue.kicad_pcb"
    cmd = build_rescue_command(
        routed,
        out,
        ["SCL", "NRST"],
        RescueConfig(
            manufacturer="jlcpcb-tier1",
            backend="cpp",
            seed=42,
            stage_timeout_s=300,
            per_net_timeout_s=60,
            starting_layers=4,
            max_layers=4,
            micro_via_in_pad_fallback=True,
        ),
    )
    assert cmd == [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(routed),
        "--output",
        str(out),
        "--preserve-existing",
        "--auto-layers",
        "--starting-layers",
        "4",
        "--max-layers",
        "4",
        "--manufacturer",
        "jlcpcb-tier1",
        "--micro-via-in-pad-fallback",
        "--backend",
        "cpp",
        "--seed",
        "42",
        "--timeout",
        "300",
        "--per-net-timeout",
        "60",
        "--skip-nets",
        "SCL,NRST",
    ]


def test_unroutable_link_from_report_entry() -> None:
    entry = {
        "net": "/OC_TRIP_N",
        "reason": "no-path",
        "link": {"start": "U1-3", "end": "R4-1"},
        "deadline_hit": False,
        "blocking_copper": ["/V_BUS", "/GND"],
        "nearest_blocker_mm": 0.1234,
        "stuck_classification": "walled",
        "tier_limit_note": "manufacturer 'x' does not support via-in-pad",
    }
    lk = UnroutableLink.from_report_entry(entry)
    assert lk.net == "/OC_TRIP_N"
    assert lk.reason == "no-path"
    assert lk.start == "U1-3" and lk.end == "R4-1"
    assert lk.blocking_copper == ("/V_BUS", "/GND")
    assert lk.nearest_blocker_mm == 0.1234
    assert lk.stuck_classification == "walled"


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


# ---------------------------------------------------------------------------
# complete_unfinished_nets: --complete rewiring + report propagation (#4478)
# ---------------------------------------------------------------------------


def test_completion_shells_complete_not_skip_nets(tmp_path: Path, monkeypatch) -> None:
    """Every completion subprocess must use ``--complete`` and NO skip list (#4478).

    This is the core of the rewiring: the completion driver no longer shells
    the coarse grid engine via ``--skip-nets``; it shells ``kct route
    --complete`` (lattice engine).  The mutual-exclusivity guard
    (route_cmd.py:8805) means the argv must never combine ``--complete`` with
    ``--skip-nets``/``--nets``.
    """
    pcb = _write_pcb(tmp_path)
    captured: list[list[str]] = []

    detections = iter([["SDA"], []])
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: next(detections),
    )

    def _fake_run(cmd, **kwargs):
        captured.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text(Path(cmd[4]).read_text())
        return _Result()

    monkeypatch.setattr("kicad_tools.router.partial_rescue.subprocess.run", _fake_run)

    complete_unfinished_nets(pcb, RescueConfig(), max_passes=3, quiet=True)

    assert captured, "a completion subprocess should have been launched"
    for cmd in captured:
        assert "--complete" in cmd
        # The mutual-exclusivity guard must never be tripped from this path.
        assert "--skip-nets" not in cmd
        assert "--nets" not in cmd
        assert "--complete-report" in cmd


def test_completion_report_propagates_unroutable_links(tmp_path: Path, monkeypatch) -> None:
    """A pass that leaves a link open surfaces the per-link report to the caller.

    The fake ``kct route --complete`` writes a Phase-4 ``--complete-report``
    naming one still-unroutable link with blocking copper.  The returned
    :class:`CompletionResult` must expose it (closed vs. unroutable-with-
    blockers propagation, AC item 3).
    """
    pcb = _write_pcb(tmp_path)

    # Pass 1: two unfinished -> one remains (progress, kept).  Pass 2 detects
    # the single remainder is unchanged from a prior detection so the loop
    # would continue, but we stop it by reporting no further progress.
    detections = iter([["SCL", "SDA"], ["SDA"], ["SDA"], ["SDA"]])
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: next(detections),
    )

    def _fake_run(cmd, **kwargs):
        class _Result:
            returncode = 8  # --complete: one or more links remain unroutable
            stdout = ""
            stderr = ""

        # Land SCL's copper (net 2) so the unfinished count drops 2 -> 1.
        src = Path(cmd[4]).read_text()
        out = Path(cmd[cmd.index("--output") + 1])
        marker = "\t(segment\n\t\t(start 7 7)\n\t\t(net 2)\n\t)\n"
        out.write_text(src.replace("(kicad_pcb\n", "(kicad_pcb\n" + marker, 1))
        # Emit the structured Phase-4 report for the still-open SDA link.
        report = Path(cmd[cmd.index("--complete-report") + 1])
        report.write_text(
            json.dumps(
                {
                    "unroutable_links": [
                        {
                            "net": "SDA",
                            "reason": "no-path",
                            "link": {"start": "U1-1", "end": "U2-1"},
                            "deadline_hit": False,
                            "blocking_copper": ["SCL"],
                            "nearest_blocker_mm": 0.15,
                            "stuck_classification": "walled",
                        }
                    ]
                }
            )
        )
        return _Result()

    monkeypatch.setattr("kicad_tools.router.partial_rescue.subprocess.run", _fake_run)

    result = complete_unfinished_nets(pcb, RescueConfig(), max_passes=1, quiet=True)

    assert isinstance(result, CompletionResult)
    assert result.history == [(2, 1)]
    assert len(result.unroutable_links) == 1
    link = result.unroutable_links[0]
    assert link.net == "SDA"
    assert link.blocking_copper == ("SCL",)  # non-empty -> "blocked by copper"
    assert link.stuck_classification == "walled"
    # No report side-file left behind.
    assert not list(tmp_path.glob("*_complete_report*"))


def test_completion_result_is_list_compatible() -> None:
    """CompletionResult forwards list ops so legacy ``== [...]`` callers work."""
    r = CompletionResult(history=[(2, 1), (1, 0)])
    assert r == [(2, 1), (1, 0)]
    assert len(r) == 2
    assert list(r) == [(2, 1), (1, 0)]
    assert r[0] == (2, 1)
    assert bool(r) is True
    assert bool(CompletionResult()) is False
