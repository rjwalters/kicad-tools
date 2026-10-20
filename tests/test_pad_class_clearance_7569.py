"""Issue #7569: the native exact pad test applies the pair rule per pad.

``Grid3D.pad_geometry_clear`` used to be held to one board-wide clearance (the
largest net class) so a tighter-class pad beside a wider-class net on the board
refused steps that DRC and the Python search (#6389) accept.  With each pad's
own class pushed via ``set_pad_class_clearances`` the required clearance is
``max(own class, pad's class)``.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available

pytestmark = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available; run `kct build-native`",
)

HALF = 0.1  # 0.2 mm trace
PAD = 0.4  # square 0.4 mm pad at (5, 5)
TRACE_Y = 5.0 + PAD / 2 + 0.18 + HALF


def _grid():
    from kicad_tools.router import router_cpp

    impl = router_cpp.Grid3D(200, 200, 2, 0.05, 0.0, 0.0)
    impl.add_pad(5.0, 5.0, PAD, PAD, 1, 0, 0, 0.0, False, 0.0, False)
    return impl


def _clear(impl, clearance=0.0):
    return impl.pad_geometry_clear(4.0, TRACE_Y, 6.0, TRACE_Y, 0, 2, HALF, clearance)


def test_pad_class_accepts_tighter_class_pad() -> None:
    impl = _grid()
    impl.set_pad_class_clearances([0.15])
    assert _clear(impl)  # 0.18 mm gap >= 0.15 mm class
    # A board-wide 0.2 mm floor (the pre-#7569 behaviour) refuses the step.
    assert not _clear(impl, clearance=0.2)


def test_pad_class_rejects_below_pad_class() -> None:
    impl = _grid()
    impl.set_pad_class_clearances([0.2])
    assert not _clear(impl)  # 0.18 mm gap < 0.2 mm class


def test_no_class_keeps_caller_clearance() -> None:
    impl = _grid()
    assert _clear(impl, clearance=0.15)
    assert not _clear(impl, clearance=0.2)
    impl.set_pad_class_clearances([])  # shorter list -> 0 for missing pads
    assert not _clear(impl, clearance=0.2)


def test_wide_own_class_refused_next_to_narrow_class_pad() -> None:
    """The MAX side: a 0.2 mm-class track (the caller's clearance) next to a
    0.15 mm-class pad at a 0.18 mm gap is refused -- required is
    max(own class, pad's class) = 0.2, not the pad's class alone."""
    impl = _grid()
    impl.set_pad_class_clearances([0.15])
    assert not _clear(impl, clearance=0.2)
    assert _clear(impl, clearance=0.15)
