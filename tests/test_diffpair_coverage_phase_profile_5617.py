"""Tests for the re-route phase profiler in the diff-pair CI gate (Issue #5617).

``scripts/ci/check_diffpair_coverage.py`` streams the ``generate_design.py
--step route`` child's output and attributes wall time to the board recipe's
numbered pipeline phases, so the ``Re-route board + check diff-pair coverage``
step (96 % of its job) carries its own breakdown instead of requiring a
by-hand diff of Actions log timestamps.

The instrumentation is pure observation: **every** line of the child's output
must still reach the CI log unchanged apart from the phase-header prefix, or
downstream ``::error::`` annotations and log greps would break.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_diffpair_coverage.py"


def _load_helper_module():
    spec = importlib.util.spec_from_file_location(
        "check_diffpair_coverage_profile_test_module", HELPER_SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_diffpair_coverage_profile_test_module"] = module
    spec.loader.exec_module(module)
    return module


def test_module_execs_without_sys_modules_registration():
    """The script must import when exec'd *without* a ``sys.modules`` entry.

    ``tests/test_recipe_artifact_isolation.py`` loads every ``scripts/ci``
    helper with ``module_from_spec`` + ``exec_module`` and never registers the
    result.  Under ``from __future__ import annotations`` the ``@dataclass``
    machinery resolves each string annotation via
    ``sys.modules.get(cls.__module__).__dict__``, which is ``None`` for such a
    module -- so a single dataclass in this file raises ``AttributeError`` at
    *definition* time and takes that whole suite down (which is exactly what
    happened on the first #5617 push: four failures, three of them in tests
    unrelated to this change).  ``PhaseTiming`` is a ``NamedTuple`` for that
    reason; this test is the guard that keeps it one.

    ``_load_helper_module`` above *does* register the module, so it cannot
    catch this -- hence the deliberate duplication here.
    """
    spec = importlib.util.spec_from_file_location(
        "check_diffpair_coverage_unregistered", HELPER_SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert "check_diffpair_coverage_unregistered" not in sys.modules
    spec.loader.exec_module(module)

    timing = module.PhaseTiming(label="4", title="Routing nets...", start=1.0, duration=2.0)
    assert timing == module.PhaseTiming("4", "Routing nets...", 1.0, 2.0)


class _FakeClock:
    """A monotonic clock driven by an explicit list of tick values."""

    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)
        self._last = self._ticks[-1] if self._ticks else 0.0

    def __call__(self) -> float:
        if self._ticks:
            self._last = self._ticks.pop(0)
        return self._last


def _timeline(ticks: list[float]):
    mod = _load_helper_module()
    return mod, mod.PhaseTimeline(clock=_FakeClock(ticks))


def test_phase_headers_are_annotated_with_their_start_offset():
    _mod, tl = _timeline([0.0, 10.0, 25.0])
    assert tl.feed("4. Routing nets...") == "[phase 4 @ 10.0s] 4. Routing nets..."


def test_non_header_lines_pass_through_byte_for_byte():
    _mod, tl = _timeline([0.0] + [1.0] * 10)
    for line in (
        "  [ 14.3%] Routing net 4/21: USB3_TX2-... (57.0s)",
        "::error::Re-route failed",
        "",
        "   Placed 12 repair via(s) + 0 bridge trace(s)",
        "1.2.3 not a phase header",
        "Net USB3_RX1-: C++ pathfinder gave up",
    ):
        assert tl.feed(line) == line


def test_interstitial_sub_phase_labels_are_recognised():
    """Recipes number interstitial passes ``9b.`` / ``10c.`` / ``13b.``."""
    _mod, tl = _timeline([0.0, 1.0, 2.0])
    assert tl.feed("10c. Re-filling zones (round 1)...").startswith("[phase 10c @ ")


def test_leading_whitespace_and_blank_prefix_still_match():
    _mod, tl = _timeline([0.0, 3.0])
    out = tl.feed("\n5. Optimizing traces...")
    assert out.startswith("[phase 5 @ 3.0s] ")
    assert out.endswith("5. Optimizing traces...")


def test_durations_are_attributed_to_the_preceding_header():
    mod = _load_helper_module()
    # clock():  init=0, then one call per feed(), then one for finish().
    clock = _FakeClock([0.0, 1.0, 5.0, 105.0, 130.0, 200.0])
    tl = mod.PhaseTimeline(clock=clock)
    tl.feed("noise before phase 1")  # t=1.0
    tl.feed("4. Routing nets...")  # t=5.0   -> phase 4 opens
    tl.feed("10. Pad-aware post-route stitching...")  # t=105.0 -> 4 closes (100s)
    tl.feed("12. 45-degree quantization...")  # t=130.0 -> 10 closes (25s)
    phases = tl.finish()  # t=200.0 -> 12 closes (70s)

    by_label = {p.label: p for p in phases}
    assert set(by_label) == {"4", "10", "12"}
    assert by_label["4"].start == 5.0
    assert by_label["4"].duration == 100.0
    assert by_label["10"].duration == 25.0
    assert by_label["12"].duration == 70.0
    assert by_label["4"].title == "Routing nets..."


def test_finish_is_idempotent():
    mod = _load_helper_module()
    tl = mod.PhaseTimeline(clock=_FakeClock([0.0, 1.0, 9.0, 9.0]))
    tl.feed("4. Routing nets...")
    first = tl.finish()
    assert tl.finish() == first


def test_summary_is_sorted_slowest_first_and_reports_shares():
    mod = _load_helper_module()
    clock = _FakeClock([0.0, 1.0, 11.0, 91.0, 100.0])
    tl = mod.PhaseTimeline(clock=clock)
    tl.feed("5. Optimizing traces...")  # opens at 1.0
    tl.feed("4. Routing nets...")  # 5 closes with 10s
    tl.feed("9. Generating copper-pour zones...")  # 4 closes with 80s
    lines = tl.summary_lines(total=100.0)

    body = [ln for ln in lines if ln.startswith("[phase-profile]   ")]
    labels = [ln.split(".")[0].split()[-1] for ln in body[:3]]
    assert labels == ["4", "5", "9"]
    assert "(80.0%)" in body[0]
    assert "(unattributed" in body[-1]


def test_summary_is_empty_when_no_phase_header_was_seen():
    mod = _load_helper_module()
    tl = mod.PhaseTimeline(clock=_FakeClock([0.0, 1.0, 2.0]))
    tl.feed("no numbered headers here")
    assert tl.summary_lines(total=5.0) == []


def test_re_route_helper_streams_and_profiles(tmp_path, capsys):
    """End-to-end: a stub ``generate_design.py`` is streamed and profiled."""
    mod = _load_helper_module()
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    (board_dir / "generate_design.py").write_text(
        "import sys\n"
        "print('4. Routing nets...')\n"
        "print('  [  0.0%] Routing net 1/2: A... (0.5s)')\n"
        "print('10. Pad-aware post-route stitching...')\n"
        "sys.exit(0)\n"
    )

    assert mod.re_route_board(board_dir, 42) is True
    out = capsys.readouterr().out
    assert "[phase 4 @ " in out
    assert "[phase 10 @ " in out
    # The child's ordinary output is preserved verbatim.
    assert "  [  0.0%] Routing net 1/2: A... (0.5s)" in out
    assert "[phase-profile] re-route phase breakdown" in out
    assert "[phase-profile] re-route subprocess wall time:" in out


def test_re_route_helper_reports_a_failing_child(tmp_path, capsys):
    mod = _load_helper_module()
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    (board_dir / "generate_design.py").write_text(
        "import sys\nprint('4. Routing nets...')\nsys.exit(3)\n"
    )

    assert mod.re_route_board(board_dir, 42) is False
    out = capsys.readouterr().out
    assert "::error::Re-route failed" in out
    assert "exit code 3" in out
