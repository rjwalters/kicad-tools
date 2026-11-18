"""Regression guard for the #9089 RSS-growth teardown hook (Issue #9154).

``tests/conftest.py::pytest_runtest_teardown`` is the only thing standing
between the bulk ``Test`` job and the OOM-kill flake of #9089, yet nothing
failed if a future edit deleted or weakened it. This exercises the hook
directly (its ``item``/``nextitem`` arguments are unused) so the check is
independent of xdist worker distribution and test ordering: one test
function, three rounds of cyclic garbage, and the floor must not ratchet.
"""

import sys

import pytest

from tests import conftest

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "linux",
        reason="hook reads /proc/self/statm (Linux only)",
    ),
    pytest.mark.skipif(
        conftest._malloc_trim is None,
        reason="glibc malloc_trim unavailable",
    ),
]

# Per round: enough cyclic garbage to clear the 32 MB trigger with margin
# for allocator reuse, while keeping the transient footprint of a bulk-job
# worker modest (Issue #9154: the probe that inspired this used ~120 MB).
_GARBAGE_MB = 64
# 64 KB blocks sit under glibc's 128 KB mmap threshold, so they are served
# from the brk heap; mmap'd blocks are munmap'd on free and never need a
# trim. 8 consecutive blocks form a 512 KB free run -- large enough for
# malloc_trim(0) to madvise away on any real page size (4K/16K/64K).
_TRIM_PIECE_BYTES = 64 * 1024
_TRIM_RUN_PIECES = 8
_ROUNDS = 3
# The post-trim floor may creep between rounds (allocator fragmentation)
# but must not ratchet toward the worker's OOM limit -- #9089 measured a
# 4.4 GB ratchet with the hook absent, so 64 MB of total creep is a far
# cry from the failure mode this guards against.
_RATCHET_TOLERANCE_MB = 64.0
# Issue #9184: after the hook, absolute RSS must be back near the
# pre-probe baseline plus the deliberately-live separator blocks. With the
# interleaved probe only malloc_trim(0) can return the interior free runs
# (gc.collect() merely frees them into the heap), so this is the direct
# observation of the pages-returned-to-OS half of the hook: deleting the
# _malloc_trim(0) call leaves the whole round's garbage resident and this
# goes red. The ceiling's slack is this page-independent noise margin plus
# the page-size-derived straddle term from _straddle_slack_mb() below
# (Issue #9201) -- a fixed MB constant would mis-measure a healthy hook on
# a 16K/64K-page runner.
_TRIM_SLACK_MB = 24.0


def _straddle_slack_mb(rounds_done: int, live_count: int) -> float:
    """Ceiling slack for malloc_trim straddle waste, scaled by page size.

    madvise returns whole pages only, so each interior free run loses up to
    two partial pages (its start and end edges) to alignment, and each live
    separator pins a 64 KB block whose tail page is shared with the
    neighbouring run. The term is 4.5 MB at 4K pages after round 3 (1.5 MB
    in round 1) and scales linearly with the page size (~72 MB at 64K pages
    after round 3), so the ceiling charges it explicitly instead of hiding
    it in the fixed slack (Issue #9201). It is precautionary margin, not
    coverage of an observed residual: on a 4K host a healthy hook's
    post-teardown RSS lands *below* ``baseline + live`` (measured -3.1 MB in
    round 1, -2.5 MB in round 3, Issue #9214), so no positive straddle
    waste has been seen to cover. Analytical, not measured on a large-page
    host: the term bounds the heap shape (3 rounds x (128 runs x 2 edges +
    separators), x page size).

    Detection margin this trades away (Issue #9214): a hook with
    ``malloc_trim(0)`` deleted retains ~68.4 MB over baseline (measured at
    4K) and the assertion fires at round 1, where the straddle term is
    smallest. Round-1 margin between that retention and the ceiling's
    excess over baseline (8 MB live + 24 MB slack + straddle):
    ~34.9 MB at 4K (measured), ~30.4 MB at 16K and ~12.4 MB at 64K
    (both analysis only -- they assume retention stays at the 4K figure;
    Issue #9202's large-page run should record the real number). The guard
    is not defeated on 64K pages, but its margin narrows ~3x.
    """
    page_mb = conftest._PAGE_SIZE / 1048576
    runs_per_round = (_GARBAGE_MB * 1048576) // (_TRIM_RUN_PIECES * _TRIM_PIECE_BYTES)
    straddle_pages = 2 * rounds_done * runs_per_round + live_count
    return straddle_pages * page_mb


class _Cyclic:
    __slots__ = ("data", "cycle")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.cycle = self  # refcounting can never free this


def _grow_interleaved_garbage(live: list[bytearray]) -> None:
    """Leave one round's worth of unreachable cyclic garbage for the hook.

    The nodes stay reachable while being created (otherwise the automatic
    generational GC reclaims them mid-loop and RSS never grows), then the
    only external reference is dropped -- refcounting cannot free a
    self-cycle, exactly like the nanobind wrapper leak of #9089.

    Issue #9184: the garbage is laid out as contiguous 512 KB runs
    separated by 64 KB blocks appended to ``live`` (the caller keeps them
    referenced across the hook call). A plain sequential dump is not
    enough: its freed blocks coalesce into one run at the heap top and
    glibc's free() shrinks that run on its own (auto-systrim), so a hook
    with ``malloc_trim(0)`` deleted is invisible -- RSS falls back to the
    baseline and every assertion passes. Separators pin every free run in
    the heap interior, where nothing but the hook's ``gc.collect()`` +
    ``malloc_trim(0)`` pair can hand the pages back to the OS.
    """
    keep = []
    garbage_pieces = (_GARBAGE_MB * 1048576) // _TRIM_PIECE_BYTES
    for i in range(garbage_pieces):
        keep.append(_Cyclic(bytearray(_TRIM_PIECE_BYTES)))
        if i % _TRIM_RUN_PIECES == _TRIM_RUN_PIECES - 1:
            live.append(bytearray(_TRIM_PIECE_BYTES))
    del keep


def test_teardown_hook_holds_rss_floor(monkeypatch):
    # Issue #9164: pin the floor so the sanity assertion below measures
    # this test's own probe rather than inherited session state -- a
    # stale-high ``_rss_floor_mb`` fails a healthy hook. monkeypatch
    # restores whatever the session held once the test ends.
    monkeypatch.setattr(conftest, "_rss_floor_mb", 0.0)
    baseline_mb = conftest._statm_rss_mb()
    assert baseline_mb is not None
    live: list[bytearray] = []
    floors = []
    for round_no in range(_ROUNDS):
        _grow_interleaved_garbage(live)
        rss_mb = conftest._statm_rss_mb()
        assert rss_mb is not None
        # Sanity: this round really did push RSS past the trigger, so the
        # assertions below cannot pass vacuously.
        assert rss_mb - conftest._rss_floor_mb >= conftest._RSS_GROWTH_TRIGGER_MB
        conftest.pytest_runtest_teardown(None, None)
        floors.append(conftest._rss_floor_mb)
        # Issue #9184: observe the pages-returned-to-OS half of the hook
        # directly. Post-teardown RSS must sit near the baseline plus the
        # live separators; a hook that no longer calls malloc_trim(0)
        # leaves the round's garbage resident and holds RSS near the peak,
        # which the floor-to-floor ratchet check below never sees.
        post_mb = conftest._statm_rss_mb()
        assert post_mb is not None
        straddle_mb = _straddle_slack_mb(round_no + 1, len(live))
        ceiling_mb = (
            baseline_mb + len(live) * _TRIM_PIECE_BYTES / 1048576 + _TRIM_SLACK_MB + straddle_mb
        )
        assert post_mb <= ceiling_mb, (
            f"post-teardown RSS {post_mb:.1f} MB stayed above {ceiling_mb:.1f} MB "
            f"(baseline {baseline_mb:.1f} MB + live separators "
            f"{len(live) * _TRIM_PIECE_BYTES / 1048576:.1f} MB + "
            f"{_TRIM_SLACK_MB} MB slack + {straddle_mb:.1f} MB page-straddle "
            f"slack @ {conftest._PAGE_SIZE} B pages): the freed pages were not "
            f"returned to the OS -- is malloc_trim(0) still called by the "
            f"teardown hook? (floors: {floors})"
        )

    assert floors[0] > 0.0, "hook never fired: _rss_floor_mb still 0.0"
    assert max(floors) - floors[0] <= _RATCHET_TOLERANCE_MB, (
        f"RSS floor ratcheted across rounds: {floors}"
    )


def test_teardown_hook_inert_when_statm_unreadable(monkeypatch):
    """Issue #9154: an unreadable /proc must no-op, not error every test."""

    def _unmounted_proc(*args, **kwargs):
        raise OSError("no /proc mounted")

    monkeypatch.setattr("builtins.open", _unmounted_proc)
    floor_before = conftest._rss_floor_mb
    conftest.pytest_runtest_teardown(None, None)  # must not raise
    assert conftest._rss_floor_mb == floor_before


def test_straddle_slack_scales_with_page_size(monkeypatch):
    """Issue #9201: the ceiling slack must grow with the host's page size.

    Pins the analytical term against constant drift: 128 free runs per
    round, each with 2 straddling edges, plus 128 live separators per
    round = 384 pages per round, i.e. 1152 pages after 3 rounds -- ~4.5 MB
    at 4K pages (precautionary: a healthy hook's measured residual at 4K is
    negative, ~-3 MB below ``baseline + live``, not a positive waste) and
    ~72 MB at 64K pages, the estimate a fixed
    ``_TRIM_SLACK_MB`` could not cover.
    """
    assert (_GARBAGE_MB * 1048576) // (_TRIM_RUN_PIECES * _TRIM_PIECE_BYTES) == 128
    monkeypatch.setattr(conftest, "_PAGE_SIZE", 4096)
    assert _straddle_slack_mb(3, 384) == pytest.approx(1152 * 4096 / 1048576)
    monkeypatch.setattr(conftest, "_PAGE_SIZE", 16384)
    assert _straddle_slack_mb(3, 384) == pytest.approx(1152 * 16384 / 1048576)
    monkeypatch.setattr(conftest, "_PAGE_SIZE", 65536)
    assert _straddle_slack_mb(3, 384) == pytest.approx(1152 * 65536 / 1048576)
