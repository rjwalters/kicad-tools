"""Issue #8413: fallback diagnostics reach the timeout receipt even unsaved."""

from __future__ import annotations

import json

import pytest

from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.cli.route_deadline import RouteDeadlineExpired


class _StubRouter:
    routes: list = []  # empty -> _save_partial_results returns False

    def __init__(self, backend_info):
        self._info = backend_info

    @property
    def backend_info(self):
        if isinstance(self._info, Exception):
            raise self._info
        return self._info

    def __getattr__(self, name):  # _restore_best_trial etc. find nothing
        raise AttributeError(name)


@pytest.fixture
def run_deadline(tmp_path, monkeypatch):
    control = tmp_path / "control.json"
    control.write_text(json.dumps({"stage": "routing"}))
    monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
    saved = dict(route_cmd._interrupt_state)

    def raiser(argv=None):
        raise RouteDeadlineExpired

    monkeypatch.setattr(route_cmd, "_main_impl", raiser)

    def run(router):
        route_cmd._interrupt_state.update(
            router=router,
            output_path=tmp_path / "out.kicad_pcb",
            pcb_path=tmp_path / "in.kicad_pcb",
            quiet=True,
            dry_run=False,
            pre_save_hook=None,
            placement_text=None,
        )
        rc = route_cmd._in_process_main_impl([])
        return rc, json.loads(control.read_text())

    yield run
    route_cmd._interrupt_state.clear()
    route_cmd._interrupt_state.update(saved)


def test_unsaved_snapshot_keeps_fallback_stats(run_deadline):
    router = _StubRouter(
        {
            "fallback_stats": {
                "python_fallback_seconds_by_net": {"N1": 2.0, "N2": 0.5},
                "python_fallback_repeat_skips": 3,
                "python_fallback_proven_skips": 1,
                "python_fallback_seconds_saved": 4.5,
                "fallback_count": 7,
            }
        }
    )
    rc, receipt = run_deadline(router)
    assert rc == route_deadline.TIMEOUT_EXIT
    assert receipt["snapshot_saved"] is False
    assert receipt["python_fallback_repeat_skips"] == 3
    assert receipt["python_fallback_top_nets"][0] == {"net": "N1", "seconds": 2.0}
    assert receipt["fallback_count"] == 7


def test_missing_stats_are_null(run_deadline):
    rc, receipt = run_deadline(_StubRouter({}))
    assert rc == route_deadline.TIMEOUT_EXIT
    assert receipt["snapshot_saved"] is False
    for key in route_cmd._FALLBACK_RECEIPT_KEYS:
        assert key in receipt and receipt[key] is None


def test_no_router_is_null(run_deadline):
    rc, receipt = run_deadline(None)
    assert rc == route_deadline.TIMEOUT_EXIT
    assert receipt["python_fallback_top_nets"] is None


def test_helper_failure_does_not_block_handler(run_deadline):
    rc, receipt = run_deadline(_StubRouter(RuntimeError("boom")))
    assert rc == route_deadline.TIMEOUT_EXIT
    assert receipt["snapshot_saved"] is False
    assert receipt["python_fallback_repeat_skips"] is None
