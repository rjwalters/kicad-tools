"""Issue #8414 AC 3: a deadline inside the post-negotiation sweep saves a partial."""

import json
import os
import sys
from pathlib import Path

import pytest

from kicad_tools.cli import route_deadline


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal/process-group regression")
def test_deadline_during_sweep_route_net_writes_partial_save(tmp_path):
    source = tmp_path / "input.kicad_pcb"
    source.write_text("(kicad_pcb (version 20240108))")
    output = tmp_path / "output.kicad_pcb"
    script = f"""
from pathlib import Path
import os, signal, threading, time
from types import SimpleNamespace
from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.router.core import Autorouter, DesignRules

signal.signal(signal.SIGTERM, route_deadline._deadline_signal)

router = Autorouter(width=20.0, height=20.0, rules=DesignRules(grid_resolution=0.1))
router.add_component("R1", [
    {{"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "N1"}},
    {{"number": "2", "x": 10.0, "y": 5.0, "net": 1, "net_name": "N1"}},
])

def spinning_route_net(net, **kw):
    # Stands in for a stranded net whose retry burns Python-level time.
    timer = threading.Timer(1.5, os.kill, args=(os.getpid(), signal.SIGTERM))
    timer.daemon = True
    timer.start()
    while True:
        sum(range(10000))

router.route_net = spinning_route_net

def work(argv):
    route_deadline.configure_output(SimpleNamespace(pcb={str(source)!r}, output={str(output)!r}))
    route_cmd._interrupt_state.update(
        router=SimpleNamespace(routes=[1], to_sexp=lambda **kw: '(segment (start 0 0) (end 1 1) (width 0.2) (layer "F.Cu") (net 1))'),
        output_path=Path({str(output)!r}), pcb_path=Path({str(source)!r}),
        best_completed_attempt=True)
    route_deadline.record_stage("routing")
    pads = {{1: list(router.pads.values())}}
    router._post_negotiation_sweep(
        stranded_nets=[1], net_routes={{1: []}}, pads_by_net=pads,
        per_net_timeout=10.0, deadline=time.time() + 600)
    raise AssertionError("sweep must not complete")

route_cmd._main_impl = work
raise SystemExit(route_cmd._in_process_main([]))
"""
    rc = route_deadline._supervise(
        [sys.executable, "-c", script], 30, tmp_path / "control.json", save_seconds=3
    )
    assert rc == 124
    report = json.loads(output.with_suffix(".timeout.json").read_text())
    # The receipt keeps the interrupted stage; the contract is the saved snapshot.
    assert report["interrupted_function"] == "spinning_route_net"
    assert report["status"] == "partial"
    assert report["snapshot_saved"] is True
    assert "(segment" in Path(report["snapshot"]).read_text()
