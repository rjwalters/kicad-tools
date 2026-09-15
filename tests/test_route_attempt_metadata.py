"""Current-attempt metadata survives finite workers and early/no-output exits."""

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.router.reporting import RouteAttemptResult
from tests.test_routing_placement_disposition import board_text


@pytest.mark.parametrize("finite", [False, True])
def test_all_invalid_attempt_returns_typed_metadata_without_output(tmp_path, finite):
    source = tmp_path / "mixed.kicad_pcb"
    source.write_text(board_text())
    original = source.read_bytes()
    output = tmp_path / "out.kicad_pcb"
    output.write_text("stale unrelated output")
    args = [str(source), "-o", str(output), "--nets", "BAD"]
    if finite:
        args += ["--timeout", "10"]
    result = route_cmd.main_with_result(args)
    assert isinstance(result, RouteAttemptResult)
    assert result.exit_code == 2
    d = result.placement_disposition
    assert d.requested_nets == d.requested_invalid_nets == frozenset({"BAD"})
    assert not d.eligible_nets
    assert d.invalid_references == frozenset({"X1"})
    assert d.pad_net_identities
    assert output.read_text() == "stale unrelated output"
    assert source.read_bytes() == original
    # A later invocation must not inherit metadata from this failed one.
    later = route_cmd.main_with_result([str(tmp_path / "missing.kicad_pcb")])
    assert later.exit_code == 1
    assert later.placement_disposition is None


def test_startup_timeout_does_not_invent_placement_analysis(tmp_path):
    source = tmp_path / "mixed.kicad_pcb"
    source.write_text(board_text())
    result = route_cmd.main_with_result([str(source), "--timeout", "0.001"])
    assert result.exit_code == 124
    assert result.placement_disposition is None
    assert isinstance(route_cmd.main([str(tmp_path / "missing.kicad_pcb")]), int)
