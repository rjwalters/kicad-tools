"""Regression coverage for kicad-cli capability-probe caching (Issue #5566).

``_kicad_cli_has_fill_zones`` and ``_kicad_drc_supports_refill`` each launch a
gated ``kicad-cli ... --help`` subprocess to detect a fixed property of the
installed binary. Before this fix, a single logical zone-fill operation
invoked ``_kicad_drc_supports_refill`` twice -- once inside
``_run_fill_zones_via_drc`` (the KiCad-10 DRC-refill fallback path) and again
inside ``_remediate_starved_thermal``'s first refill pass (Issue #3729) --
each an independent, *gated* ``kicad-cli`` launch under CI's
``KCT_NATIVE_MAX_CONCURRENCY=1`` (Issue #5501). Traced against the two
``test_post_pass_gating.py`` CLI tests that #5566 reports timing out, a single
call performed **six** sequential gated ``kicad-cli`` launches for one board
with a synthesized pour zone -- three of them (two ``--help`` probes plus one
avoidable repeat) pure repetition of a fact that cannot change within a
process. That repetition amplifies one logical operation's wait-tail against
the shared native-permit semaphore for no benefit.

These tests assert the *shape* of the fix -- at most one real subprocess
launch per distinct ``kicad_cli`` path per process -- rather than only timing
wall clock, per this issue's acceptance criteria ("no per-test timeout is
simply raised to paper over" a defect).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kicad_tools.cli import runner


@pytest.fixture(autouse=True)
def _clear_capability_caches():
    """Isolate each test from cache state left by any earlier test/import.

    Not strictly required by the tests below (each uses a ``tmp_path``-unique
    fake binary, so cache keys never collide across tests), but it keeps the
    module-level ``lru_cache`` state from silently depending on test order.
    """
    runner._kicad_cli_has_fill_zones.cache_clear()
    runner._kicad_drc_supports_refill.cache_clear()
    yield
    runner._kicad_cli_has_fill_zones.cache_clear()
    runner._kicad_drc_supports_refill.cache_clear()


def _fake_completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = ""
    return completed


def test_has_fill_zones_probes_once_per_path(monkeypatch, tmp_path):
    cli = tmp_path / "kicad-cli"
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True, **kwargs):
        calls.append(cmd)
        return _fake_completed(stdout="fill-zones", returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner._kicad_cli_has_fill_zones(cli) is True
    assert runner._kicad_cli_has_fill_zones(cli) is True
    assert len(calls) == 1, "repeated probes of the same binary must not re-launch kicad-cli"


def test_drc_supports_refill_probes_once_per_path(monkeypatch, tmp_path):
    cli = tmp_path / "kicad-cli"
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=True, text=True, **kwargs):
        calls.append(cmd)
        return _fake_completed(stdout="--refill-zones")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner._kicad_drc_supports_refill(cli) is True
    assert runner._kicad_drc_supports_refill(cli) is True
    assert len(calls) == 1


def test_capability_cache_is_keyed_by_path_not_shared_globally(monkeypatch, tmp_path):
    """Two distinct binaries must each be probed once; the cache never mixes them."""
    cli_a = tmp_path / "a" / "kicad-cli"
    cli_b = tmp_path / "b" / "kicad-cli"
    calls: list[str] = []

    def fake_run(cmd, capture_output=True, text=True, **kwargs):
        calls.append(cmd[0])
        supports = cmd[0] == str(cli_b)
        return _fake_completed(stdout="--refill-zones" if supports else "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner._kicad_drc_supports_refill(cli_a) is False
    assert runner._kicad_drc_supports_refill(cli_b) is True
    # Re-querying both is served from cache -- no further launches.
    assert runner._kicad_drc_supports_refill(cli_a) is False
    assert runner._kicad_drc_supports_refill(cli_b) is True
    assert len(calls) == 2


def test_remediation_pass_reuses_the_fill_paths_refill_probe(monkeypatch, tmp_path):
    """Models the two real call sites for the same binary (Issue #5566):
    ``_run_fill_zones_via_drc`` probes ``_kicad_drc_supports_refill`` once for
    the fallback fill command, and ``_remediate_starved_thermal``'s first
    refill pass probes it again immediately afterwards. Caching must collapse
    these into a single real ``kicad-cli pcb drc --help`` launch.
    """
    cli = tmp_path / "kicad-cli"
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20260206))")

    help_calls = 0
    drc_calls = 0

    def fake_run(cmd, capture_output=True, text=True, **kwargs):
        nonlocal help_calls, drc_calls
        if "--help" in cmd:
            help_calls += 1
            return _fake_completed(stdout="--refill-zones")
        drc_calls += 1
        out_idx = cmd.index("--output") + 1
        Path(cmd[out_idx]).write_text("{}")
        return _fake_completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "_snapshot_net_declarations", lambda _p: [])
    monkeypatch.setattr(runner, "_snapshot_element_nets", lambda _p: {})
    monkeypatch.setattr(runner, "_restore_net_declarations", lambda *a, **k: None)

    # Call site 1: the fallback zone-fill path (mirrors _run_fill_zones_via_drc).
    assert runner._kicad_drc_supports_refill(cli) is True
    # Call site 2: the #3729 thermal-remediation pass, same binary.
    runner._remediate_starved_thermal(pcb, cli, settle=None)

    assert help_calls == 1, "both call sites probe the same fixed fact about the same binary"
    # refill=True pass + the read-only refill=False report; the loop exits
    # after one iteration because the {} report has no violations to fix.
    assert drc_calls == 2
