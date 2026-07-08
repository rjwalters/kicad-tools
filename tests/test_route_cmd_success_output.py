"""Issue #3942: the router must not print failure-flavored diagnostics on
boards that route 100% successfully.

Three distinct diagnostic bugs were reported and are pinned here:

* **Bug A** -- ``⚠ Oscillation detected`` / ``All N escape strategies
  exhausted`` lines were printed mid-loop and stayed visible even when a
  later iteration resolved the board to full connectivity.  They are now
  buffered and only surfaced when the route ends with stranded nets (a
  genuine partial).  See ``src/kicad_tools/router/core.py`` (deferred
  ``_emit_oscillation_msg`` buffer + post-loop flush gated on
  ``_stranded_nets()``).

* **Bug B** -- the routed/total summary counted pour-served multi-pad
  nets in the denominator even though the router strips them via
  ``_filter_pour_nets``, yielding a spurious ``PARTIAL: Routed 1/2`` on a
  fully-routed board.  ``_routable_multi_pad_nets`` now excludes them so
  the denominator matches what the router was asked to route.

* **Bug C** -- the ``--- Results ---`` segment/via counts are scoped to
  newly-routed multi-pad nets, but the written file also carries
  preserved copper.  The heading is now annotated when
  ``--preserve-existing`` is active so the counts are not mistaken for
  file totals.

The subprocess-based checks route the real demo boards (board 00, board
01) and assert the final output contains no failure-flavored wording;
the unit checks pin the denominator/buffer mechanics directly.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.timeout(900)

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_00 = REPO_ROOT / "boards" / "00-simple-led" / "output" / "simple_led.kicad_pcb"
BOARD_01 = REPO_ROOT / "boards" / "01-voltage-divider" / "output" / "voltage_divider.kicad_pcb"

# Failure-flavored wording that must NOT appear on a 100%-routed board.
_OSCILLATION_RE = re.compile(r"Oscillation detected")
_ESCAPE_EXHAUSTED_RE = re.compile(r"escape strateg(?:y|ies) exhausted", re.IGNORECASE)
_PARTIAL_RE = re.compile(r"^PARTIAL:", re.MULTILINE)


def _route(pcb: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run ``kct route`` as a subprocess and capture combined output."""
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(pcb),
        "--output",
        str(out),
        *extra,
    ]
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )


# ---------------------------------------------------------------------------
# Bug A + Bug B: successful routes must not print failure-flavored wording
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not BOARD_00.exists(), reason="board 00 fixture missing")
def test_board00_success_output_has_no_failure_wording(tmp_path):
    """Board 00 (GND/VCC pour-served) routes 100% and must read as SUCCESS.

    Pins Bug A (no oscillation/escape-exhausted lines) and Bug B (the
    pour-served power nets are excluded from the denominator, so the
    summary is SUCCESS, never PARTIAL).
    """
    out = tmp_path / "board00_routed.kicad_pcb"
    proc = _route(BOARD_00, out, "--layers", "2")
    output = proc.stdout + proc.stderr

    assert out.exists(), f"route did not write output:\n{output}"
    # Bug B: fully-routed board reports SUCCESS, not PARTIAL.
    assert "SUCCESS:" in output, f"expected SUCCESS headline:\n{output}"
    assert not _PARTIAL_RE.search(output), f"unexpected PARTIAL on 100% board:\n{output}"
    # Bug A: no failure-flavored oscillation/escape wording on a success.
    assert not _OSCILLATION_RE.search(output), f"oscillation wording on success:\n{output}"
    assert not _ESCAPE_EXHAUSTED_RE.search(output), (
        f"escape-exhausted wording on success:\n{output}"
    )


@pytest.mark.skipif(not BOARD_01.exists(), reason="board 01 fixture missing")
def test_board01_success_output_has_no_failure_wording(tmp_path):
    """Board 01 (voltage divider) exercises the escape loop yet routes 3/3.

    Board 01 historically ends with residual overflow (overlaps demoted
    post-loop) at full connectivity, so it is the canonical case where
    gating on ``overflow == 0`` alone would still leak the failure
    wording.  Gating on ``_stranded_nets()`` suppresses it correctly.
    """
    out = tmp_path / "board01_routed.kicad_pcb"
    proc = _route(BOARD_01, out, "--layers", "2")
    output = proc.stdout + proc.stderr

    assert out.exists(), f"route did not write output:\n{output}"
    assert not _PARTIAL_RE.search(output), f"unexpected PARTIAL on 100% board:\n{output}"
    assert not _OSCILLATION_RE.search(output), f"oscillation wording on success:\n{output}"
    assert not _ESCAPE_EXHAUSTED_RE.search(output), (
        f"escape-exhausted wording on success:\n{output}"
    )


@pytest.mark.skipif(not BOARD_00.exists(), reason="board 00 fixture missing")
def test_verbose_keeps_status_detail(tmp_path):
    """--verbose still surfaces the normal per-iteration/status detail.

    The Bug A fix suppresses only the failure-flavored oscillation/escape
    lines on success; it must not swallow the ordinary progress/status
    output the router prints (e.g. the SUCCESS headline).
    """
    out = tmp_path / "board00_verbose.kicad_pcb"
    proc = _route(BOARD_00, out, "--layers", "2", "--verbose")
    output = proc.stdout + proc.stderr

    assert out.exists(), f"route did not write output:\n{output}"
    assert "SUCCESS:" in output, f"verbose run lost its SUCCESS headline:\n{output}"
    # Even under --verbose, a clean route stays free of the failure wording.
    assert not _OSCILLATION_RE.search(output), f"oscillation wording under --verbose:\n{output}"


# ---------------------------------------------------------------------------
# Bug B: denominator excludes pour-served multi-pad nets
# ---------------------------------------------------------------------------


class _FakeRouter:
    """Minimal stand-in exposing what ``_routable_multi_pad_nets`` reads."""

    def __init__(self, nets, pour_net_ids):
        # nets: {net_id: [pad, pad, ...]}
        self.nets = nets
        self._pour_net_ids = set(pour_net_ids)

    def _is_pour_net(self, net_id):  # noqa: D401 - mirror Autorouter API
        return net_id in self._pour_net_ids


def test_routable_multi_pad_nets_excludes_pour_served():
    """A pour-served multi-pad net (net_num > 0) is dropped from the count."""
    from kicad_tools.cli.route_cmd import _routable_multi_pad_nets

    router = _FakeRouter(
        nets={
            0: ["p"],  # net 0 obstacle -- excluded (net_num > 0 filter)
            1: ["a", "b"],  # routable signal
            2: ["c", "d"],  # pour-served (has zone, is_pour_net) -- excluded
            3: ["e"],  # single-pad -- excluded (needs 2+)
        },
        pour_net_ids={2},
    )

    assert _routable_multi_pad_nets(router) == [1]


def test_routable_multi_pad_nets_keeps_no_zone_pour_nets():
    """A pour net without a zone is routed as a signal, so it stays counted.

    ``Autorouter._is_pour_net`` returns False for nets in
    ``_pour_nets_without_zones`` -- the router DOES route them, so they
    belong in the denominator.  Here net 2 is pour-classified-but-no-zone,
    modeled by ``_is_pour_net`` returning False.
    """
    from kicad_tools.cli.route_cmd import _routable_multi_pad_nets

    router = _FakeRouter(
        nets={1: ["a", "b"], 2: ["c", "d"]},
        pour_net_ids=set(),  # neither is pour-served -> both routable
    )

    assert _routable_multi_pad_nets(router) == [1, 2]


# ---------------------------------------------------------------------------
# Bug A: the buffer-flush gate (genuine failures stay loud, successes quiet)
# ---------------------------------------------------------------------------


def test_flush_gate_suppresses_on_full_success():
    """Zero stranded nets => buffered oscillation messages are dropped."""
    from kicad_tools.router.core import _should_flush_oscillation_msgs

    msgs = ["  ⚠ Oscillation detected: [4, 2, 4, 2]", "    All 4 escape strategies exhausted"]
    assert _should_flush_oscillation_msgs(msgs, stranded_net_count=0) is False


def test_flush_gate_surfaces_on_genuine_partial():
    """Any stranded net => buffered messages ARE surfaced (stay loud)."""
    from kicad_tools.router.core import _should_flush_oscillation_msgs

    msgs = ["  ⚠ Oscillation detected: [4, 2, 4, 2]", "    All 4 escape strategies exhausted"]
    assert _should_flush_oscillation_msgs(msgs, stranded_net_count=1) is True
    assert _should_flush_oscillation_msgs(msgs, stranded_net_count=7) is True


def test_flush_gate_noop_when_no_messages_buffered():
    """No buffered messages => nothing to flush regardless of stranded count."""
    from kicad_tools.router.core import _should_flush_oscillation_msgs

    assert _should_flush_oscillation_msgs([], stranded_net_count=0) is False
    assert _should_flush_oscillation_msgs([], stranded_net_count=5) is False


def test_flush_gate_ignores_residual_overflow_on_success():
    """Full connectivity with residual overflow still suppresses (board 01).

    The gate keys on stranded-net count, NOT overflow -- a board that
    reaches full connectivity while carrying overlap-driven overflow must
    still suppress the failure wording.
    """
    from kicad_tools.router.core import _should_flush_oscillation_msgs

    msgs = ["  ⚠ Oscillation detected: [2, 2, 2, 2]"]
    # stranded=0 models "all nets connected"; overflow is intentionally not
    # a parameter, proving the decision does not depend on it.
    assert _should_flush_oscillation_msgs(msgs, stranded_net_count=0) is False


# ---------------------------------------------------------------------------
# Bug C: --- Results --- heading is scope-labeled under --preserve-existing
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not BOARD_00.exists(), reason="board 00 fixture missing")
def test_results_heading_annotated_when_preserving(tmp_path):
    """With --preserve-existing, the Results heading carries a scope label.

    The counts under ``--- Results ---`` are newly-routed-only; the label
    tells the user the written file also includes the preserved copper.
    """
    routed = REPO_ROOT / "boards" / "00-simple-led" / "output" / "simple_led_routed.kicad_pcb"
    if not routed.exists():
        pytest.skip("board 00 routed fixture missing")
    src = tmp_path / "preserve_in.kicad_pcb"
    shutil.copy(routed, src)
    out = tmp_path / "preserve_out.kicad_pcb"
    proc = _route(src, out, "--layers", "2", "--preserve-existing")
    output = proc.stdout + proc.stderr

    if "--- Results ---" not in output:
        pytest.skip(f"route path did not reach Results block:\n{output}")
    # The heading must carry the newly-routed scope annotation.
    results_line = next(
        line for line in output.splitlines() if line.strip().startswith("--- Results ---")
    )
    assert "newly-routed" in results_line, f"Results heading not scope-labeled: {results_line!r}"
