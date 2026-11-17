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
# goes red. 24 MB of slack absorbs madvise alignment waste and interpreter
# noise (~3 MB measured on 4K pages) while staying ~40 MB below the
# retention level of a trim-less hook.
_TRIM_SLACK_MB = 24.0


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
    for _ in range(_ROUNDS):
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
        ceiling_mb = baseline_mb + len(live) * _TRIM_PIECE_BYTES / 1048576 + _TRIM_SLACK_MB
        assert post_mb <= ceiling_mb, (
            f"post-teardown RSS {post_mb:.1f} MB stayed above {ceiling_mb:.1f} MB "
            f"(baseline {baseline_mb:.1f} MB + live separators "
            f"{len(live) * _TRIM_PIECE_BYTES / 1048576:.1f} MB + {_TRIM_SLACK_MB} MB "
            f"slack): the freed pages were not returned to the OS -- is "
            f"malloc_trim(0) still called by the teardown hook? (floors: {floors})"
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
