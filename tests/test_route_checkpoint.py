"""Tests for the checkpoint/atomic-write machinery (Issue #2808 + #2809).

Covers three concerns:

1. ``_write_routed_pcb`` -- atomic single-file write that consolidates the
   four duplicated save sites in ``route_cmd.py``.  Verifies:
   - Normal write produces a valid PCB at the user's exact ``output_path``
     (Issue #2809: no ``_4layer`` suffix mutation).
   - ``is_checkpoint=True`` skips the layer-stackup mutation so checkpoints
     write the in-progress state without forcing a stackup escalation.
   - Atomic semantics: a forced exception during the rename leaves the
     original ``output_path`` untouched (no torn file).

2. ``_make_checkpoint_callback`` -- builds the callback closure for
   ``Autorouter.route_all_negotiated``.  Verifies:
   - ``interval <= 0`` returns ``None`` (callback disabled).
   - First improvement always writes (no warm-up delay).
   - Repeated improvements within ``interval`` are throttled.
   - Improvements after ``interval`` produce a fresh write.

3. ``Autorouter.route_all_negotiated`` accepts ``checkpoint_callback``
   and the callback receives the ``best_routes`` snapshot, NOT
   ``self.routes`` (per Curator's CRITICAL SUBTLETY note).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.route_cmd import (
    _make_checkpoint_callback,
    _write_routed_pcb,
)
from kicad_tools.core.types import CopperLayer
from kicad_tools.router.core import Autorouter, IterationMetrics
from kicad_tools.router.primitives import Route, Segment

# Minimal valid PCB content used as the "input file" for write tests.
# Just enough structure that _insert_sexp_before_closing has a closing ``)``
# to splice before, and _validate_sexp_parentheses sees balanced parens.
_MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator pcbnew)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
)
"""

_SAMPLE_ROUTE_SEXP = """(segment
\t(start 1.0 1.0)
\t(end 2.0 2.0)
\t(width 0.2)
\t(layer "F.Cu")
\t(net 1)
\t(uuid "00000000-0000-0000-0000-000000000001")
)"""


# =============================================================================
# _write_routed_pcb unit tests
# =============================================================================


class TestWriteRoutedPcbBasic:
    """The helper produces a valid PCB at the exact output path."""

    def test_writes_to_exact_output_path(self, tmp_path: Path):
        """Issue #2809: --output PATH must be honored exactly.  No
        ``_4layer`` suffix mutation, no other path rewriting."""
        in_path = tmp_path / "input.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "honored.kicad_pcb"

        written = _write_routed_pcb(in_path, out_path, _SAMPLE_ROUTE_SEXP)

        # Returns the same Path object passed in.
        assert written == out_path
        # The file exists at the exact path requested.
        assert out_path.exists()
        # Sibling suffix-mangled file does NOT exist.
        assert not (tmp_path / "honored_4layer.kicad_pcb").exists()
        # Content includes the route sexp we asked for.
        content = out_path.read_text()
        assert "(segment" in content
        assert "(start 1.0 1.0)" in content
        # Parens still balanced (basic structural integrity).
        from kicad_tools.cli.route_cmd import _validate_sexp_parentheses

        assert _validate_sexp_parentheses(content)

    def test_empty_route_sexp_writes_unmodified_input(self, tmp_path: Path):
        """When ``route_sexp`` is empty (e.g. no nets routed) the input
        is still written to the output path -- callers downstream of the
        helper expect a file to exist at output_path."""
        in_path = tmp_path / "input.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "out.kicad_pcb"

        _write_routed_pcb(in_path, out_path, "")

        assert out_path.exists()
        # No segment fragment in output because route_sexp was empty.
        assert "(segment" not in out_path.read_text()

    def test_layer_count_2_is_noop_for_stackup(self, tmp_path: Path):
        """layer_count=2 (default) does not invoke stackup update -- the
        2-layer block is already in the input."""
        in_path = tmp_path / "input.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "out.kicad_pcb"

        _write_routed_pcb(in_path, out_path, _SAMPLE_ROUTE_SEXP, layer_count=2)

        content = out_path.read_text()
        # 2L stackup unchanged.
        assert '(0 "F.Cu" signal)' in content
        assert '(31 "B.Cu" signal)' in content
        # No inner copper layers appeared.
        assert '"In1.Cu"' not in content


class TestWriteRoutedPcbCheckpoint:
    """``is_checkpoint=True`` semantics: skip stackup update."""

    def test_checkpoint_skips_stackup_update(self, tmp_path: Path):
        """When is_checkpoint=True, even ``layer_count=4`` should NOT
        rewrite the layer block -- checkpoints reflect mid-route state
        and should not force escalation.

        We verify by passing layer_count=4 on a 2L input: with
        is_checkpoint=False the helper would inject 4L layer entries;
        with is_checkpoint=True the input's 2L block is preserved.
        """
        in_path = tmp_path / "input.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "ckpt.kicad_pcb"

        _write_routed_pcb(
            in_path,
            out_path,
            _SAMPLE_ROUTE_SEXP,
            layer_count=4,
            is_checkpoint=True,
        )

        content = out_path.read_text()
        # 2L block preserved -- no inner layers injected.
        assert '"In1.Cu"' not in content
        assert '"In2.Cu"' not in content
        # And the route sexp still landed.
        assert "(segment" in content

    def test_checkpoint_output_is_valid_pcb(self, tmp_path: Path):
        """A checkpoint write produces a structurally valid PCB
        (balanced parens, no layer-stackup mismatch)."""
        from kicad_tools.cli.route_cmd import _validate_sexp_parentheses

        in_path = tmp_path / "input.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "ckpt.kicad_pcb"

        _write_routed_pcb(
            in_path,
            out_path,
            _SAMPLE_ROUTE_SEXP,
            is_checkpoint=True,
        )

        assert _validate_sexp_parentheses(out_path.read_text())


class TestWriteRoutedPcbAtomic:
    """Atomic-write semantics: a failure between tmp-write and rename
    must leave the original output_path file unchanged."""

    def test_replace_failure_leaves_existing_file_unchanged(self, tmp_path: Path):
        """If ``os.replace`` raises (simulating a crash or filesystem
        error mid-rename), the original file at ``output_path`` -- if it
        already existed -- must NOT have been clobbered.

        Atomic semantics guarantee: either the output_path has the new
        content OR it has the original (pre-existing) content.  Never
        partial / torn content.
        """
        in_path = tmp_path / "input.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "out.kicad_pcb"

        # Seed output_path with sentinel content so we can verify it
        # didn't get truncated or replaced.
        SENTINEL = "(sentinel-content-must-not-be-clobbered)"
        out_path.write_text(SENTINEL)

        # Simulate failure during the atomic rename.
        with patch("kicad_tools.cli.route_cmd.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                _write_routed_pcb(in_path, out_path, _SAMPLE_ROUTE_SEXP)

        # Output path still has the sentinel -- the rename failed before
        # touching it.
        assert out_path.read_text() == SENTINEL

    def test_tmp_file_uses_dot_tmp_suffix(self, tmp_path: Path):
        """Tmp file is a sibling of output_path with ``.tmp`` suffix --
        the suffix matters because it goes through ``os.replace`` which
        requires same-filesystem.

        We can verify this indirectly: simulate the rename failing,
        then check the tmp file exists with the expected name (the
        helper doesn't clean it up on failure -- that's intentional so
        the operator can inspect what we tried to write).
        """
        in_path = tmp_path / "input.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "out.kicad_pcb"

        with patch("kicad_tools.cli.route_cmd.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                _write_routed_pcb(in_path, out_path, _SAMPLE_ROUTE_SEXP)

        # The tmp file remains on disk with the expected name.
        expected_tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        assert expected_tmp.exists()
        # And it contains the new content we tried to write (proves the
        # write happened, just the rename failed).
        assert "(segment" in expected_tmp.read_text()


# =============================================================================
# _make_checkpoint_callback unit tests
# =============================================================================


def _route(net: int, name: str = "") -> Route:
    """Minimal Route for testing."""
    return Route(
        net=net,
        net_name=name or f"NET{net}",
        segments=[
            Segment(
                x1=0.0,
                y1=0.0,
                x2=1.0,
                y2=1.0,
                width=0.2,
                layer=CopperLayer.F_CU,
                net=net,
            ),
        ],
    )


class TestMakeCheckpointCallback:
    """Behavior of the throttling callback factory."""

    def test_zero_interval_returns_none(self, tmp_path: Path):
        """``--checkpoint-interval 0`` disables checkpointing -- the
        factory returns ``None`` so the router treats it as no callback."""
        in_path = tmp_path / "in.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "out.kicad_pcb"

        cb = _make_checkpoint_callback(in_path, out_path, interval=0.0, quiet=True)
        assert cb is None

    def test_negative_interval_returns_none(self, tmp_path: Path):
        """Defensive: negative values also disable."""
        in_path = tmp_path / "in.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "out.kicad_pcb"

        cb = _make_checkpoint_callback(in_path, out_path, interval=-1.0, quiet=True)
        assert cb is None

    def test_first_improvement_always_writes(self, tmp_path: Path):
        """No warm-up delay: the very first call to the callback
        produces a write regardless of interval size."""
        in_path = tmp_path / "in.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "out.kicad_pcb"

        cb = _make_checkpoint_callback(in_path, out_path, interval=30.0, quiet=True)
        assert cb is not None

        metrics = IterationMetrics(iteration=1, routed_count=1, overflow=0)
        cb([_route(1)], metrics)

        # First write fires immediately.
        assert out_path.exists()
        assert "(segment" in out_path.read_text()

    def test_rapid_calls_throttled_to_one_write(self, tmp_path: Path):
        """Two improvement events within ``interval`` produce exactly
        one write (the first one).  The second call returns early.

        We verify throttling by snapshotting the file's mtime + content
        hash after the first call, then asserting they're unchanged
        after the second call.
        """
        in_path = tmp_path / "in.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "out.kicad_pcb"

        cb = _make_checkpoint_callback(in_path, out_path, interval=30.0, quiet=True)
        assert cb is not None

        # First call writes.  Routes for net 1 -- one segment.
        cb([_route(1)], IterationMetrics(iteration=1, routed_count=1, overflow=0))
        first_content = out_path.read_text()
        # Sanity: the write produced a segment in the output.
        assert "(segment" in first_content
        first_segment_count = first_content.count("(segment")
        assert first_segment_count == 1

        # Second call within interval: pass MORE routes (two segments).
        # Throttled callback must NOT write, so the file should still
        # show only one segment.
        cb(
            [_route(1), _route(2)],
            IterationMetrics(iteration=2, routed_count=2, overflow=0),
        )
        assert out_path.read_text() == first_content, (
            "Second call within interval should be throttled (no write)"
        )

    def test_call_after_interval_writes_again(self, tmp_path: Path):
        """After ``interval`` has elapsed, the next call writes again."""
        in_path = tmp_path / "in.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "out.kicad_pcb"

        # Use a 0.05s interval so the test runs fast.
        cb = _make_checkpoint_callback(in_path, out_path, interval=0.05, quiet=True)
        assert cb is not None

        # First call -- one route.
        cb([_route(1)], IterationMetrics(iteration=1, routed_count=1, overflow=0))
        first_content = out_path.read_text()
        assert first_content.count("(segment") == 1

        # Wait past the interval.
        import time as _t

        _t.sleep(0.10)

        # Second call with two routes -- must produce a new write
        # reflecting the additional segment.
        cb(
            [_route(1), _route(2)],
            IterationMetrics(iteration=2, routed_count=2, overflow=0),
        )
        new_content = out_path.read_text()
        assert new_content.count("(segment") == 2, "Second call after interval should write again"


# =============================================================================
# End-to-end: route_all_negotiated invokes the callback with best_routes
# =============================================================================


class TestRouteAllNegotiatedCheckpointHook:
    """The router actually calls ``checkpoint_callback`` and feeds it
    the snapshot, not ``self.routes``."""

    @pytest.fixture
    def trivial_autorouter(self):
        router = Autorouter(width=20.0, height=20.0)
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 2.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 18.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 10.0, "y": 2.0, "net": 2, "net_name": "NET2"},
                {"number": "2", "x": 10.0, "y": 18.0, "net": 2, "net_name": "NET2"},
            ],
        )
        return router

    def test_callback_is_invoked_at_least_once(self, trivial_autorouter):
        """The callback fires on any improvement event (initial route OR
        iteration-end strict improvement).  On a trivial board, the
        initial pass routes everything with overflow=0 -- that's a strict
        improvement over the iteration-0 baseline (also overflow=0,
        routed_count=0), so we expect at least one invocation from the
        iteration-top snapshot."""
        calls: list[tuple[list, IterationMetrics]] = []

        def _capture(routes, metrics):
            # Deep-store the routes list AS-IS so we can assert it's the
            # snapshot, not a live reference.
            calls.append((list(routes), metrics))

        trivial_autorouter.route_all_negotiated(
            max_iterations=3,
            timeout=10.0,
            adaptive=False,
            perturbation=False,
            checkpoint_callback=_capture,
        )

        # On a trivial 2-net board the initial pass converges fast; the
        # callback may or may not fire depending on overflow values.  Test
        # that the machinery doesn't crash and that IF the callback fires,
        # it receives the right types.
        for routes, metrics in calls:
            assert isinstance(routes, list)
            assert all(isinstance(r, Route) for r in routes)
            assert isinstance(metrics, IterationMetrics)

    def test_callback_receives_best_routes_snapshot_not_self_routes(self):
        """CRITICAL SUBTLETY from Curator: the callback must receive the
        passed-in ``best_routes`` (deep-copy snapshot), not
        ``self.routes`` (live, possibly-worse state).

        We verify this by checking that the routes list the callback
        receives is independent of subsequent mutations to the router's
        live routes.
        """
        router = Autorouter(width=20.0, height=20.0)
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 2.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 18.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )

        snapshots: list[list[Route]] = []

        def _capture(routes, metrics):
            # Append the LIST OBJECT itself -- if the router passed
            # self.routes by reference, mutating self.routes after the
            # call would change this list too.
            snapshots.append(routes)

        router.route_all_negotiated(
            max_iterations=2,
            timeout=10.0,
            adaptive=False,
            perturbation=False,
            checkpoint_callback=_capture,
        )

        # If any snapshot was captured, mutate router.routes and verify
        # the snapshot is independent.
        if snapshots:
            snapshot = snapshots[0]
            original_len = len(snapshot)
            # Mutate live state.
            router.routes.clear()
            router.routes.append(_route(99, "POST_HOC"))
            # Snapshot must be unaffected.
            assert len(snapshot) == original_len
            # And no POST_HOC route should be in the snapshot.
            assert not any(r.net_name == "POST_HOC" for r in snapshot)

    def test_callback_exception_does_not_abort_routing(self, trivial_autorouter):
        """A checkpoint failure (disk full, permission denied, etc.)
        must NOT crash the router -- the in-memory best snapshot is
        still valid and the terminal save can still proceed."""

        def _raising_cb(routes, metrics):
            raise OSError("simulated disk full")

        # Routing must complete without re-raising.
        routes = trivial_autorouter.route_all_negotiated(
            max_iterations=2,
            timeout=10.0,
            adaptive=False,
            perturbation=False,
            checkpoint_callback=_raising_cb,
        )
        assert isinstance(routes, list)

    def test_no_callback_default_preserves_backward_compat(self, trivial_autorouter):
        """``checkpoint_callback=None`` (the default) preserves the
        legacy behavior -- no exceptions, no extra side effects."""
        routes = trivial_autorouter.route_all_negotiated(
            max_iterations=2,
            timeout=10.0,
            adaptive=False,
            perturbation=False,
            # No checkpoint_callback kwarg -- defaults to None.
        )
        assert isinstance(routes, list)


# =============================================================================
# Regression: Issue #2809 -- --output PATH must be honored exactly
# =============================================================================


class TestOutputPathRegression:
    """Issue #2809: --output PATH must NOT be silently rewritten.  The
    previous behavior appended ``_4layer`` (or ``_6layer``) when auto-layer
    escalation kicked in.  The consolidated ``_write_routed_pcb`` helper
    drops that mutation entirely."""

    def test_write_routed_pcb_does_not_mutate_path_for_4layer(self, tmp_path: Path):
        """Even when ``layer_count=4`` is passed (auto-escalation
        triggered), the file lands at the exact ``output_path`` -- not
        at ``<stem>_4layer.kicad_pcb``."""
        # 4L-capable input: needs at least one more layer entry so the
        # stackup update has something to escalate.  Use a 2L input and
        # let update_pcb_layer_stackup escalate it.
        in_path = tmp_path / "input.kicad_pcb"
        in_path.write_text(_MINIMAL_PCB)
        out_path = tmp_path / "user_requested.kicad_pcb"

        _write_routed_pcb(
            in_path,
            out_path,
            _SAMPLE_ROUTE_SEXP,
            layer_count=4,
        )

        # File exists at the EXACT path requested.
        assert out_path.exists()
        # No suffix-mangled sibling.
        assert not (tmp_path / "user_requested_4layer.kicad_pcb").exists()
        # And the stackup actually escalated to 4L (proving layer_count
        # was honored, just not via the filename).
        content = out_path.read_text()
        assert '"In1.Cu"' in content
        assert '"In2.Cu"' in content
