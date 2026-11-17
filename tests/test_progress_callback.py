"""Tests for progress callback infrastructure."""

from __future__ import annotations

import io
import json

import pytest

from kicad_tools.progress import (
    ProgressContext,
    ProgressEvent,
    SubProgressCallback,
    create_json_callback,
    create_print_callback,
    get_current_callback,
    null_progress,
    report_progress,
)


class TestProgressCallback:
    """Tests for the basic progress callback type."""

    def test_callback_receives_progress_values(self):
        """Test that callbacks receive correct progress, message, and cancelable values."""
        received = []

        def callback(progress: float, message: str, cancelable: bool) -> bool:
            received.append((progress, message, cancelable))
            return True

        # Simulate calling the callback
        callback(0.0, "Starting", True)
        callback(0.5, "Halfway", True)
        callback(1.0, "Complete", False)

        assert len(received) == 3
        assert received[0] == (0.0, "Starting", True)
        assert received[1] == (0.5, "Halfway", True)
        assert received[2] == (1.0, "Complete", False)

    def test_callback_can_cancel_operation(self):
        """Test that returning False from callback signals cancellation."""
        call_count = 0

        def cancel_callback(progress: float, message: str, cancelable: bool) -> bool:
            nonlocal call_count
            call_count += 1
            # Cancel after first call
            return call_count < 2

        # Simulate an operation that respects cancellation
        for i in range(10):
            progress = i / 10
            if not cancel_callback(progress, f"Step {i}", True):
                break

        # Should have stopped after 2 calls
        assert call_count == 2


class TestProgressContext:
    """Tests for the ProgressContext context manager."""

    def test_context_sets_current_callback(self):
        """Test that entering context sets the current callback."""
        received = []

        def callback(progress: float, message: str, cancelable: bool) -> bool:
            received.append((progress, message))
            return True

        # Before context, no callback
        assert get_current_callback() is None

        with ProgressContext(callback=callback) as ctx:
            # Inside context, callback is set
            current = get_current_callback()
            assert current is not None
            ctx.report(0.5, "Test message")

        # After context, callback is cleared
        assert get_current_callback() is None

        # Check callback was invoked
        assert len(received) == 1
        assert received[0] == (0.5, "Test message")

    def test_context_tracks_cancellation(self):
        """Test that context tracks when operation is cancelled."""

        def cancel_callback(progress: float, message: str, cancelable: bool) -> bool:
            return progress < 0.5  # Cancel at 50%

        with ProgressContext(callback=cancel_callback) as ctx:
            assert not ctx.cancelled

            # Report progress up to cancellation point
            ctx.report(0.25, "Quarter done")
            assert not ctx.cancelled

            ctx.report(0.5, "Half done")  # This should trigger cancel
            assert ctx.cancelled

            # Further reports should return False immediately
            result = ctx.report(0.75, "Should not process")
            assert result is False

    def test_null_context(self):
        """Test that null context provides no-op progress."""
        with ProgressContext(callback=None) as ctx:
            # Should not raise, just return True
            result = ctx.report(0.5, "Test")
            assert result is True

    def test_report_progress_uses_current_context(self):
        """Test that report_progress uses the current context callback."""
        received = []

        def callback(progress: float, message: str, cancelable: bool) -> bool:
            received.append((progress, message))
            return True

        # Without context, report_progress returns True (no-op)
        result = report_progress(0.5, "No context")
        assert result is True
        assert len(received) == 0

        # With context, report_progress uses the callback
        with ProgressContext(callback=callback):
            result = report_progress(0.5, "With context")
            assert result is True
            assert len(received) == 1
            assert received[0] == (0.5, "With context")


class TestProgressEvent:
    """Tests for ProgressEvent dataclass."""

    def test_event_to_dict(self):
        """Test conversion to dictionary."""
        event = ProgressEvent(progress=0.5, message="Halfway", cancelable=True)
        d = event.to_dict()

        assert d == {"progress": 0.5, "message": "Halfway", "cancelable": True}

    def test_event_to_json(self):
        """Test conversion to JSON string."""
        event = ProgressEvent(progress=0.75, message="Almost done", cancelable=False)
        j = event.to_json()

        parsed = json.loads(j)
        assert parsed["progress"] == 0.75
        assert parsed["message"] == "Almost done"
        assert parsed["cancelable"] is False


class TestSubProgressCallback:
    """Tests for SubProgressCallback scaling wrapper."""

    def test_sub_progress_scales_values(self):
        """Test that sub-progress correctly scales progress values."""
        received = []

        def parent(progress: float, message: str, cancelable: bool) -> bool:
            received.append(progress)
            return True

        # Phase 1: 0-50%
        phase1 = SubProgressCallback(parent, start=0.0, end=0.5)
        phase1(0.0, "Start phase 1", True)  # 0.0 -> 0.0
        phase1(0.5, "Mid phase 1", True)  # 0.5 -> 0.25
        phase1(1.0, "End phase 1", True)  # 1.0 -> 0.5

        # Phase 2: 50-100%
        phase2 = SubProgressCallback(parent, start=0.5, end=1.0)
        phase2(0.0, "Start phase 2", True)  # 0.0 -> 0.5
        phase2(0.5, "Mid phase 2", True)  # 0.5 -> 0.75
        phase2(1.0, "End phase 2", True)  # 1.0 -> 1.0

        assert len(received) == 6
        assert received[0] == pytest.approx(0.0)
        assert received[1] == pytest.approx(0.25)
        assert received[2] == pytest.approx(0.5)
        assert received[3] == pytest.approx(0.5)
        assert received[4] == pytest.approx(0.75)
        assert received[5] == pytest.approx(1.0)

    def test_sub_progress_with_prefix(self):
        """Test that sub-progress can add message prefix."""
        received = []

        def parent(progress: float, message: str, cancelable: bool) -> bool:
            received.append(message)
            return True

        sub = SubProgressCallback(parent, start=0.0, end=1.0, prefix="Phase 1: ")
        sub(0.5, "Working", True)

        assert received[0] == "Phase 1: Working"

    def test_sub_progress_passes_through_indeterminate(self):
        """Test that indeterminate progress (-1) is passed through."""
        received = []

        def parent(progress: float, message: str, cancelable: bool) -> bool:
            received.append(progress)
            return True

        sub = SubProgressCallback(parent, start=0.0, end=0.5)
        sub(-1, "Indeterminate", True)

        assert received[0] == -1

    def test_sub_progress_propagates_cancel(self):
        """Test that cancellation from parent is propagated."""

        def cancel_parent(progress: float, message: str, cancelable: bool) -> bool:
            return False  # Always cancel

        sub = SubProgressCallback(cancel_parent, start=0.0, end=1.0)
        result = sub(0.5, "Test", True)

        assert result is False


class TestFactoryFunctions:
    """Tests for callback factory functions."""

    def test_create_json_callback(self):
        """Test JSON callback outputs properly formatted events."""
        output = io.StringIO()
        callback = create_json_callback(file=output)

        callback(0.5, "Test message", True)

        output.seek(0)
        line = output.readline().strip()
        parsed = json.loads(line)

        assert parsed["progress"] == 0.5
        assert parsed["message"] == "Test message"
        assert parsed["cancelable"] is True

    def test_json_callback_never_cancels(self):
        """Test that JSON callback always returns True (never cancels)."""
        output = io.StringIO()
        callback = create_json_callback(file=output)

        # Should always return True
        assert callback(0.0, "Start", True) is True
        assert callback(0.5, "Mid", True) is True
        assert callback(1.0, "End", False) is True

    def test_create_print_callback(self):
        """Test print callback outputs progress messages."""
        output = io.StringIO()
        callback = create_print_callback(file=output, show_percent=True)

        callback(0.5, "Halfway", True)

        output.seek(0)
        line = output.readline().strip()
        assert "50%" in line
        assert "Halfway" in line

    def test_print_callback_without_percent(self):
        """Test print callback can hide percentage."""
        output = io.StringIO()
        callback = create_print_callback(file=output, show_percent=False)

        callback(0.5, "Halfway", True)

        output.seek(0)
        line = output.readline().strip()
        assert "%" not in line
        assert "Halfway" in line

    def test_null_progress_context(self):
        """Test null_progress provides no-op context."""
        with null_progress() as ctx:
            # Should not have a callback
            assert ctx._callback is None

            # Report should succeed without error
            result = ctx.report(0.5, "Test")
            assert result is True


class TestNestedContexts:
    """Tests for nested progress contexts."""

    def test_nested_contexts_restore_correctly(self):
        """Test that nested contexts properly restore previous callback."""
        outer_received = []
        inner_received = []

        def outer_callback(progress: float, message: str, cancelable: bool) -> bool:
            outer_received.append(message)
            return True

        def inner_callback(progress: float, message: str, cancelable: bool) -> bool:
            inner_received.append(message)
            return True

        with ProgressContext(callback=outer_callback):
            report_progress(0.5, "Outer 1")

            with ProgressContext(callback=inner_callback):
                report_progress(0.5, "Inner")

            # After inner context, outer should be restored
            report_progress(0.5, "Outer 2")

        assert outer_received == ["Outer 1", "Outer 2"]
        assert inner_received == ["Inner"]
