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
_PROBE_MB = 96
_ROUNDS = 3
# The post-trim floor may creep between rounds (allocator fragmentation)
# but must not ratchet toward the worker's OOM limit -- #9089 measured a
# 4.4 GB ratchet with the hook absent, so 64 MB of total creep is a far
# cry from the failure mode this guards against.
_RATCHET_TOLERANCE_MB = 64.0


class _Cyclic:
    __slots__ = ("data", "cycle")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.cycle = self  # refcounting can never free this


def _drop_cyclic_garbage(total_mb: int) -> None:
    """Leave ``total_mb`` of unreachable cyclic garbage for the GC.

    The nodes stay reachable while being created (otherwise the automatic
    generational GC reclaims them mid-loop and RSS never grows), then the
    only external reference is dropped -- refcounting cannot free a
    self-cycle, exactly like the nanobind wrapper leak of #9089. 16 KB
    blocks sit under glibc's mmap threshold, so they are served from
    malloc arenas and only returned to the OS by the hook's
    ``gc.collect()`` + ``malloc_trim(0)`` pair.
    """
    keep = []
    for _ in range(total_mb * 64):  # 64 x 16 KB = 1 MB
        keep.append(_Cyclic(bytearray(16 * 1024)))
    del keep


def test_teardown_hook_holds_rss_floor():
    floors = []
    for _ in range(_ROUNDS):
        _drop_cyclic_garbage(_PROBE_MB)
        rss_mb = conftest._statm_rss_mb()
        assert rss_mb is not None
        # Sanity: this round really did push RSS past the trigger, so the
        # assertions below cannot pass vacuously.
        assert rss_mb - conftest._rss_floor_mb >= conftest._RSS_GROWTH_TRIGGER_MB
        conftest.pytest_runtest_teardown(None, None)
        floors.append(conftest._rss_floor_mb)

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
