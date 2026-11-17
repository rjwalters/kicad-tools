"""Opt-in companion plugin. Does not wrap Popen, mutate argv or set timeouts."""

import os
import sys
from contextlib import suppress
from pathlib import Path

import pytest
from native_observer import OUTPUT, TOKEN, append, category, owner_id


def pytest_configure(config):
    output = os.environ.get(OUTPUT)
    token = os.environ.get(TOKEN)
    if not output or not token:
        return
    path = Path(output) / f"pytest-{os.getpid()}.jsonl"
    config._native_observer_path = path
    state = {"active": True}
    config._native_observer_state = state

    def audit(event, args):
        if not state["active"] or event != "subprocess.Popen":
            return
        _executable, argv, _cwd, environment = args
        child_env = os.environ if environment is None else environment
        # Audit runs before exec. Preserve attempted fast launches without
        # claiming child PID, execution success, RSS, or native exit status.
        with suppress(OSError):
            append(
                path,
                {
                    "event": "launch_attempt",
                    "parent_pid": os.getpid(),
                    "owner": owner_id(os.environ.get("PYTEST_CURRENT_TEST")),
                    "worker": os.environ.get("PYTEST_XDIST_WORKER"),
                    "category": category(argv),
                    "child_tracking_inherited": child_env.get(TOKEN) == token,
                },
            )

    sys.addaudithook(audit)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    path = getattr(item.config, "_native_observer_path", None)
    if path:
        with suppress(OSError):
            append(
                path,
                {
                    "event": "test_report",
                    "pid": os.getpid(),
                    "worker": os.environ.get("PYTEST_XDIST_WORKER"),
                    "nodeid": owner_id(report.nodeid),
                    "phase": report.when,
                    "outcome": report.outcome,
                    "duration": report.duration,
                },
            )


def _phase(item, phase):
    path = getattr(item.config, "_native_observer_path", None)
    if path:
        with suppress(OSError):
            append(
                path,
                {
                    "event": "phase_start",
                    "pid": os.getpid(),
                    "worker": os.environ.get("PYTEST_XDIST_WORKER"),
                    "nodeid": owner_id(item.nodeid),
                    "phase": phase,
                },
            )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _phase(item, "setup")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    _phase(item, "call")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item):
    _phase(item, "teardown")


def pytest_unconfigure(config):
    # CPython audit hooks cannot be removed. Disable this session's closure
    # so a later pytest.main invocation does not duplicate events or leak
    # ownership into unrelated work in the embedding process.
    state = getattr(config, "_native_observer_state", None)
    if state:
        state["active"] = False
