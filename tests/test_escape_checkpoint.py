"""Committed escape/two-phase copper survives an interrupted later search (#5478)."""

from types import SimpleNamespace

import pytest

from kicad_tools.cli.route_cmd import _make_checkpoint_callback
from kicad_tools.cli.route_deadline import RouteDeadlineExpired
from kicad_tools.router.core import Autorouter, _emit_route_checkpoint
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Layer, Route, Segment
from kicad_tools.router.rules import DesignRules

PCB = """(kicad_pcb (version 20240108) (generator pcbnew)
(layers
  (0 "F.Cu" signal)
  (31 "B.Cu" signal)
)
(segment (start 1 0.5) (end 2 0.5) (width 0.2) (layer "F.Cu") (net 9)))"""


def make_router():
    router = Autorouter(
        width=10,
        height=10,
        rules=DesignRules(grid_resolution=0.25),
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    for net, y in [(1, 3), (2, 7)]:
        router.add_component(
            f"R{net}",
            [
                {"number": "1", "x": 2, "y": y, "net": net, "net_name": f"N{net}"},
                {"number": "2", "x": 8, "y": y, "net": net, "net_name": f"N{net}"},
            ],
        )
    fixed = Route(
        net=3,
        net_name="ESCAPE",
        segments=[
            Segment(x1=1, y1=1, x2=2, y2=1, width=0.2, layer=Layer.IN1_CU, net=3),
        ],
    )
    router._mark_route(fixed)
    router.routes.append(fixed)
    return router


@pytest.mark.parametrize("strategy", ["two_phase", "escape", "escape_diffpairs"])
@pytest.mark.parametrize("negotiated", [True, False])
@pytest.mark.parametrize("interval", [0, 30])
def test_checkpoint_survives_later_search_interrupt(
    tmp_path, monkeypatch, strategy, negotiated, interval
):
    router = make_router()
    source, output = tmp_path / "input.kicad_pcb", tmp_path / "partial.kicad_pcb"
    source.write_text(PCB)
    source.with_suffix(".kicad_pro").write_text('{"custom": {"retained": true}}')
    source.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "authored" (constraint clearance (min 0.2)))'
    )
    callback = _make_checkpoint_callback(
        source,
        output,
        interval,
        quiet=True,
        router_provider=lambda: router,
        preserved_sexp='(segment (start 1 0.5) (end 2 0.5) (width 0.2) (layer "F.Cu") (net 9))',
    )
    method = "_route_net_with_corridor" if negotiated else "route_net"
    original = getattr(router, method)
    completed = []

    def interrupt_later(net, *args, **kwargs):
        if completed:
            # Simulate a later search unwinding with mutable state discarded.
            # The previously serialized snapshot must remain independent.
            router.routes.clear()
            raise RouteDeadlineExpired()
        result = original(net, *args, **kwargs)
        assert result, "fixture must route real copper before interruption"
        completed.extend(result)
        return result

    monkeypatch.setattr(router, method, interrupt_later)
    run = router.route_all_two_phase if strategy == "two_phase" else router.route_with_escape
    extra = {}
    if strategy == "escape_diffpairs":
        from kicad_tools.router.diffpair import DifferentialPairConfig

        run = router.route_with_escape_and_diffpairs
        extra["diffpair_config"] = DifferentialPairConfig()
    with pytest.raises(RouteDeadlineExpired):
        run(
            use_negotiated=negotiated,
            timeout=10,
            per_net_timeout=2,
            checkpoint_callback=callback,
            **extra,
        )
    assert completed
    if interval == 0:
        assert not output.exists()
        return
    content = output.read_text()
    assert "(start 1 0.5)" in content  # existing source copper
    assert '"In1.Cu"' in content and '"In2.Cu"' in content
    assert "(net 3)" in content  # fixed escape copper
    assert f"(net {completed[0].net})" in content
    assert content.count("(segment") >= 3
    for suffix in (".kicad_pro", ".kicad_dru"):
        assert output.with_suffix(suffix).read_bytes() == source.with_suffix(suffix).read_bytes()


def test_throttle_prevents_geometry_copy(tmp_path, monkeypatch):
    source, output = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_text(PCB)
    callback = _make_checkpoint_callback(source, output, 30, quiet=True)
    _emit_route_checkpoint(callback, [], 0)

    def forbidden(*args):
        pytest.fail("throttled checkpoint must not copy geometry")

    monkeypatch.setattr("kicad_tools.router.core.copy.deepcopy", forbidden)
    _emit_route_checkpoint(callback, [], 0)


def test_snapshot_owns_geometry_and_excludes_grid_only_stale_routes():
    from kicad_tools.router.algorithms.two_phase import TwoPhaseRouter

    committed = Route(
        net=1, net_name="N1", segments=[Segment(x1=1, y1=1, x2=2, y2=1, width=0.2, layer=0, net=1)]
    )
    router = object.__new__(TwoPhaseRouter)
    router.routes = [committed]
    router.grid = SimpleNamespace(
        routes=[Route(net=99, net_name="STALE")], get_total_overflow=lambda: 0
    )
    seen = []
    router._emit_checkpoint(lambda routes, metrics: seen.append(routes))
    committed.segments.clear()
    assert [r.net for r in seen[0]] == [1]
    assert len(seen[0][0].segments) == 1


def test_supervisor_quarantines_routed_checkpoint_with_context(tmp_path):
    import json
    import sys
    from pathlib import Path

    from kicad_tools.cli import route_deadline

    source, output = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_text(PCB)
    source.with_suffix(".kicad_pro").write_text('{"authored": true}')
    source.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "source" (constraint clearance (min 0.2)))'
    )
    script = f"""
import os, runpy, signal
from pathlib import Path
from types import SimpleNamespace
from kicad_tools.cli import route_cmd, route_deadline
router = runpy.run_path({__file__!r})["make_router"]()
source, output = Path({str(source)!r}), Path({str(output)!r})
route_deadline.configure_output(SimpleNamespace(pcb=str(source), output=str(output)))
route_deadline.record_stage("routing")
signal.signal(signal.SIGTERM, route_deadline._deadline_signal)
callback = route_cmd._make_checkpoint_callback(source, output, 30, quiet=True, router_provider=lambda: router)
original = router._route_net_with_corridor
completed = []
def route(net, *args, **kwargs):
    if completed:
        assert output.exists()
        os.kill(os.getpid(), signal.SIGTERM)
    result = original(net, *args, **kwargs)
    assert result
    completed.extend(result)
    return result
router._route_net_with_corridor = route
try:
    router.route_all_two_phase(timeout=10, per_net_timeout=2, checkpoint_callback=callback)
except route_deadline.RouteDeadlineExpired:
    raise SystemExit(124)
raise AssertionError("second search must be interrupted")
"""
    assert (
        route_deadline._supervise(
            [sys.executable, "-c", script], 20, tmp_path / "control.json", save_seconds=1
        )
        == 124
    )
    report = json.loads(output.with_suffix(".timeout.json").read_text())
    assert report["manufacturing_ready"] is False
    assert report["stage"] == "routing"
    assert not output.exists()
    quarantined = Path(report["unverified_output"])
    content = quarantined.read_text()
    assert '"In1.Cu"' in content and "(net 1)" in content and "(net 3)" in content
    for suffix in (".kicad_pro", ".kicad_dru"):
        assert (
            quarantined.with_suffix(suffix).read_bytes() == source.with_suffix(suffix).read_bytes()
        )


def test_new_attempt_retains_previous_checkpoint_and_context(tmp_path, monkeypatch):
    import json

    from kicad_tools.cli import route_deadline
    from kicad_tools.router.core import IterationMetrics

    control = tmp_path / "control.json"
    monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
    route_deadline.record_stage("layer-escalation")
    source, output = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_text(PCB)
    source.with_suffix(".kicad_pro").write_text('{"authored": true}')
    source.with_suffix(".kicad_dru").write_text("(version 1)")
    first_router = make_router()
    active = [first_router]
    now = [100.0]
    monkeypatch.setattr("kicad_tools.cli.route_cmd.time.monotonic", lambda: now[0])
    callback = _make_checkpoint_callback(
        source, output, 30, quiet=True, router_provider=lambda: active[0]
    )
    callback(first_router.routes, IterationMetrics(0, 1, 0))
    first_bytes = output.read_bytes()
    active[0] = make_router()
    active[0].routes.clear()
    now[0] += 31
    callback([], IterationMetrics(0, 0, 0))
    report = json.loads(control.read_text())
    from pathlib import Path

    archived = Path(report["checkpoint_history"][0])
    assert "unverified" in archived.name
    assert archived.read_bytes() == first_bytes
    for suffix in (".kicad_pro", ".kicad_dru"):
        assert archived.with_suffix(suffix).read_bytes() == source.with_suffix(suffix).read_bytes()
    assert report["stage"] == "layer-escalation"
    assert report["checkpoint_saved"] is True


def test_checkpoint_context_conflict_preserves_previous_output(tmp_path):
    from kicad_tools.cli.route_cmd import DRCConstraintPropagationError
    from kicad_tools.router.core import IterationMetrics

    source, output = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_text(PCB)
    output.write_text("previous copper")
    source.with_suffix(".kicad_pro").write_text('{"authored": true}')
    output.with_suffix(".kicad_pro").write_text('{"unrelated": true}')
    callback = _make_checkpoint_callback(source, output, 30, quiet=True)
    with pytest.raises(DRCConstraintPropagationError):
        callback([], IterationMetrics(0, 0, 0))
    assert output.read_text() == "previous copper"
    assert output.with_suffix(".kicad_pro").read_text() == '{"unrelated": true}'


@pytest.mark.parametrize("interval", [0, 999])
def test_completed_attempt_survives_later_attempt_interrupt(tmp_path, monkeypatch, interval):
    import json
    from pathlib import Path

    from kicad_tools.cli import route_deadline

    control = tmp_path / "control.json"
    monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
    route_deadline.record_stage("layer-escalation")
    source, output = tmp_path / "source.kicad_pcb", tmp_path / "out.kicad_pcb"
    source.write_text(PCB)
    active = [make_router()]
    callback = _make_checkpoint_callback(
        source, output, interval, quiet=True, router_provider=lambda: active[0]
    )
    active[0].route_all_two_phase(
        timeout=10, per_net_timeout=2, max_iterations=1, checkpoint_callback=callback
    )
    report = json.loads(control.read_text())
    if interval:
        completed = [
            Path(path) for path in report["checkpoint_history"] if "completed_unverified" in path
        ]
        assert len(completed) == 1
        first_bytes = completed[0].read_bytes()
        assert all(f"(net {net})".encode() in first_bytes for net in (1, 2, 3))
        # Periodic cadence remains throttled: its initial checkpoint has N1,
        # while the separate completion artifact has both completed nets.
        assert "(net 2)" not in output.read_text()
    active[0] = make_router()

    def interrupted(*args, **kwargs):
        raise RouteDeadlineExpired()

    monkeypatch.setattr(active[0], "_route_net_with_corridor", interrupted)
    with pytest.raises(RouteDeadlineExpired):
        active[0].route_all_two_phase(timeout=10, per_net_timeout=2, checkpoint_callback=callback)
    if interval:
        assert completed[0].read_bytes() == first_bytes
    else:
        assert not list(tmp_path.glob("*unverified*.kicad_pcb"))
        assert not output.exists()
