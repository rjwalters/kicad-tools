"""Copper-rule flags reach the chained ``--complete`` pass (Issue #8694)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli import route_cmd

OUT = Path("out.kicad_pcb")
STAGED = Path("out.chain.kicad_pcb")
BOARD = "b.kicad_pcb"


def _chain(outer: list[str]) -> list[str]:
    argv = [BOARD, *outer]
    args = route_cmd._route_parser().parse_args(argv)
    return route_cmd._chain_complete_argv(args, argv, OUT, STAGED)


NEW_FLAGS = (
    "--strict-layers",
    "--strict-pad-clearance",
    "--strict-in-pad-clearance",
    "--micro-via-in-pad-fallback",
    "--micro-via-size",
    "--micro-via-drill",
    "--via-clearance",
    "--fine-pitch-clearance",
    "--no-reserve-plane-layers",
    "--no-auto-pour",
    "--no-optimize",
)


def test_default_chain_argv_gains_none_of_the_new_flags():
    assert not set(NEW_FLAGS) & set(_chain([]))


@pytest.mark.parametrize(
    "flag",
    [
        "--strict-layers",
        "--strict-pad-clearance",
        "--strict-in-pad-clearance",
        "--micro-via-in-pad-fallback",
        "--no-reserve-plane-layers",
        "--no-auto-pour",
        "--no-optimize",
    ],
)
def test_boolean_flag_is_forwarded_when_set(flag):
    assert flag in _chain([flag])


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--via-clearance", "0.31"),
        ("--fine-pitch-clearance", "0.12"),
        ("--micro-via-size", "0.35"),
        ("--micro-via-drill", "0.2"),
    ],
)
def test_value_flag_is_forwarded_only_when_passed(flag, value):
    out = _chain([flag, value])
    assert out[out.index(flag) + 1] == value
    assert flag not in _chain([])


def test_default_valued_negations_are_not_forwarded():
    out = _chain(["--reserve-plane-layers", "--auto-pour"])
    assert out == _chain([])


@pytest.mark.parametrize(
    "outer,open_nets,expected",
    [
        (["--differential-pairs"], ["USB_DP", "VCC_SENSE"], "USB_DP"),
        (["--analog-nets", "AUDIO_L,AUDIO_R"], ["AUDIO_R", "SDA"], "AUDIO_R"),
    ],
)
def test_coupled_flag_refuses_when_it_governs_an_open_net(outer, open_nets, expected):
    args = route_cmd._route_parser().parse_args([BOARD, *outer])
    reason = route_cmd._chain_coupled_routing_refusal(args, open_nets)
    assert reason is not None and expected in reason


@pytest.mark.parametrize(
    "outer,open_nets",
    [
        (["--differential-pairs"], ["SDA", "VCC_SENSE"]),
        (["--analog-nets", "AUDIO_L"], ["SDA"]),
        ([], ["USB_DP", "AUDIO_L"]),
    ],
)
def test_coupled_flag_does_not_refuse_ungoverned_nets(outer, open_nets):
    args = route_cmd._route_parser().parse_args([BOARD, *outer])
    assert route_cmd._chain_coupled_routing_refusal(args, open_nets) is None


def test_chain_pass_is_skipped_with_reason(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out.kicad_pcb"
    out.write_text("negotiated")
    outer = [str(tmp_path / "in.kicad_pcb"), "-o", str(out), "--analog-nets", "AUDIO_L"]
    args = route_cmd._route_parser().parse_args(outer)
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: ["AUDIO_L"],
    )
    monkeypatch.setattr(route_cmd, "_main_impl", lambda argv: pytest.fail("pass must not run"))
    assert route_cmd._chain_complete_pass(args, outer, 2) == 2
    assert "--analog-nets governs open net(s) AUDIO_L" in capsys.readouterr().out
