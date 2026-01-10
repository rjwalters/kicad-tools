"""Tests for MCP session manager."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.mcp.errors import (
    SessionNotFoundError,
    SessionOperationError,
)
from kicad_tools.mcp.session_manager import SessionManager
from kicad_tools.mcp.types import SessionInfo

# Test PCB with multiple components for placement testing
PLACEMENT_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 120 120)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 140 120)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
)
"""


@pytest.fixture
def test_pcb(tmp_path: Path) -> Path:
    """Create a test PCB file."""
    pcb_file = tmp_path / "test.kicad_pcb"
    pcb_file.write_text(PLACEMENT_TEST_PCB)
    return pcb_file


@pytest.fixture
def session_manager() -> SessionManager:
    """Create a session manager with short timeout for testing."""
    return SessionManager(timeout_minutes=1)


class TestSessionInfo:
    """Tests for SessionInfo dataclass."""

    def test_creation(self):
        info = SessionInfo(
            id="abc12345",
            pcb_path="/path/to/board.kicad_pcb",
            created_at="2024-01-01T00:00:00+00:00",
            last_accessed="2024-01-01T00:00:00+00:00",
            pending_moves=0,
            components=10,
            current_score=123.456,
        )
        assert info.id == "abc12345"
        assert info.pcb_path == "/path/to/board.kicad_pcb"
        assert info.pending_moves == 0
        assert info.components == 10

    def test_to_dict(self):
        info = SessionInfo(
            id="abc12345",
            pcb_path="/path/to/board.kicad_pcb",
            created_at="2024-01-01T00:00:00+00:00",
            last_accessed="2024-01-01T00:00:00+00:00",
            pending_moves=3,
            components=10,
            current_score=123.456789,
        )
        d = info.to_dict()

        assert d["id"] == "abc12345"
        assert d["pcb_path"] == "/path/to/board.kicad_pcb"
        assert d["pending_moves"] == 3
        assert d["components"] == 10
        assert d["current_score"] == 123.4568  # rounded to 4 decimal places


class TestSessionManagerCreation:
    """Tests for SessionManager initialization and session creation."""

    def test_default_timeout(self):
        manager = SessionManager()
        assert manager.timeout_seconds == 30 * 60  # 30 minutes

    def test_custom_timeout(self):
        manager = SessionManager(timeout_minutes=5)
        assert manager.timeout_seconds == 5 * 60  # 5 minutes

    def test_create_session(self, session_manager: SessionManager, test_pcb: Path):
        info = session_manager.create(str(test_pcb))

        assert info.id is not None
        assert len(info.id) == 8  # UUID prefix length
        assert info.pcb_path == str(test_pcb)
        assert info.pending_moves == 0
        assert info.components == 2  # Two capacitors in test PCB
        assert info.current_score >= 0

    def test_create_multiple_sessions(self, session_manager: SessionManager, test_pcb: Path):
        info1 = session_manager.create(str(test_pcb))
        info2 = session_manager.create(str(test_pcb))

        assert info1.id != info2.id
        assert len(session_manager) == 2

    def test_create_with_fixed_refs(self, session_manager: SessionManager, test_pcb: Path):
        info = session_manager.create(str(test_pcb), fixed_refs=["C1"])

        session = session_manager.get(info.id)
        assert "C1" in session._fixed_refs

    def test_create_nonexistent_file(self, session_manager: SessionManager):
        with pytest.raises(KiCadFileNotFoundError):
            session_manager.create("/nonexistent/board.kicad_pcb")


class TestSessionManagerAccess:
    """Tests for session access and retrieval."""

    def test_get_session(self, session_manager: SessionManager, test_pcb: Path):
        info = session_manager.create(str(test_pcb))
        session = session_manager.get(info.id)

        assert session is not None
        assert len(session._optimizer.components) == 2

    def test_get_updates_last_accessed(self, session_manager: SessionManager, test_pcb: Path):
        info = session_manager.create(str(test_pcb))
        original_accessed = info.last_accessed

        time.sleep(0.01)  # Small delay
        session_manager.get(info.id)

        updated_info = session_manager.get_info(info.id)
        assert updated_info.last_accessed > original_accessed

    def test_get_nonexistent_session(self, session_manager: SessionManager):
        with pytest.raises(SessionNotFoundError) as exc_info:
            session_manager.get("nonexistent")

        assert "nonexistent" in str(exc_info.value)
        assert exc_info.value.session_id == "nonexistent"

    def test_get_info(self, session_manager: SessionManager, test_pcb: Path):
        info = session_manager.create(str(test_pcb))
        retrieved_info = session_manager.get_info(info.id)

        assert retrieved_info.id == info.id
        assert retrieved_info.pcb_path == info.pcb_path
        assert retrieved_info.components == info.components

    def test_get_info_nonexistent(self, session_manager: SessionManager):
        with pytest.raises(SessionNotFoundError):
            session_manager.get_info("nonexistent")

    def test_list_sessions(self, session_manager: SessionManager, test_pcb: Path):
        info1 = session_manager.create(str(test_pcb))
        info2 = session_manager.create(str(test_pcb))

        sessions = session_manager.list_sessions()

        assert len(sessions) == 2
        session_ids = {s.id for s in sessions}
        assert info1.id in session_ids
        assert info2.id in session_ids

    def test_list_sessions_empty(self, session_manager: SessionManager):
        sessions = session_manager.list_sessions()
        assert sessions == []

    def test_contains(self, session_manager: SessionManager, test_pcb: Path):
        info = session_manager.create(str(test_pcb))

        assert info.id in session_manager
        assert "nonexistent" not in session_manager

    def test_len(self, session_manager: SessionManager, test_pcb: Path):
        assert len(session_manager) == 0

        session_manager.create(str(test_pcb))
        assert len(session_manager) == 1

        session_manager.create(str(test_pcb))
        assert len(session_manager) == 2


class TestSessionManagerOperations:
    """Tests for session operations (commit, rollback, close)."""

    def test_commit(self, session_manager: SessionManager, test_pcb: Path, tmp_path):
        info = session_manager.create(str(test_pcb))

        # Make a change
        session = session_manager.get(info.id)
        session.apply_move("C1", 130.0, 130.0)

        # Commit to a new file
        output_path = tmp_path / "output.kicad_pcb"
        saved_path = session_manager.commit(info.id, str(output_path))

        assert saved_path == str(output_path)
        assert output_path.exists()

        # Session should still be active with no pending moves
        updated_info = session_manager.get_info(info.id)
        assert updated_info.pending_moves == 0

    def test_commit_overwrites_original(self, session_manager: SessionManager, test_pcb: Path):
        info = session_manager.create(str(test_pcb))

        # Make a change
        session = session_manager.get(info.id)
        session.apply_move("C1", 130.0, 130.0)

        # Commit without output path (should overwrite original)
        saved_path = session_manager.commit(info.id)

        assert saved_path == str(test_pcb)

    def test_commit_nonexistent_session(self, session_manager: SessionManager):
        with pytest.raises(SessionNotFoundError):
            session_manager.commit("nonexistent")

    def test_rollback(self, session_manager: SessionManager, test_pcb: Path):
        info = session_manager.create(str(test_pcb))

        # Get initial score
        initial_score = info.current_score

        # Make changes
        session = session_manager.get(info.id)
        session.apply_move("C1", 130.0, 130.0)

        # Verify change was made
        moved_info = session_manager.get_info(info.id)
        assert moved_info.pending_moves == 1

        # Rollback
        restored_info = session_manager.rollback(info.id)

        assert restored_info.pending_moves == 0
        # Score should be back to initial
        assert abs(restored_info.current_score - initial_score) < 0.01

    def test_rollback_nonexistent_session(self, session_manager: SessionManager):
        with pytest.raises(SessionNotFoundError):
            session_manager.rollback("nonexistent")

    def test_close(self, session_manager: SessionManager, test_pcb: Path):
        info = session_manager.create(str(test_pcb))
        assert info.id in session_manager

        result = session_manager.close(info.id)

        assert result is True
        assert info.id not in session_manager
        assert len(session_manager) == 0

    def test_close_nonexistent_session(self, session_manager: SessionManager):
        result = session_manager.close("nonexistent")
        assert result is False


class TestSessionIsolation:
    """Tests for session isolation (changes in one don't affect others)."""

    def test_sessions_are_isolated(self, session_manager: SessionManager, test_pcb: Path):
        info1 = session_manager.create(str(test_pcb))
        info2 = session_manager.create(str(test_pcb))

        # Make change in session 1
        session1 = session_manager.get(info1.id)
        session1.apply_move("C1", 150.0, 150.0)

        # Session 2 should be unaffected
        session2 = session_manager.get(info2.id)
        comp_pos = session2.get_component_position("C1")

        assert comp_pos["x"] == 120.0  # Original position
        assert comp_pos["y"] == 120.0


class TestSessionExpiration:
    """Tests for session timeout and cleanup."""

    def test_cleanup_expired_sessions(self, test_pcb: Path):
        # Create manager with very short timeout (1 second)
        manager = SessionManager(timeout_minutes=0)  # 0 minutes = immediate timeout
        manager.timeout_seconds = 0.1  # Override to 100ms for testing

        info = manager.create(str(test_pcb))
        assert len(manager) == 1

        # Wait for session to expire
        time.sleep(0.2)

        # Cleanup should remove the session
        removed = manager.cleanup_expired()

        assert removed == 1
        assert len(manager) == 0
        assert info.id not in manager

    def test_cleanup_preserves_active_sessions(
        self, session_manager: SessionManager, test_pcb: Path
    ):
        info = session_manager.create(str(test_pcb))

        # Cleanup should not remove active session (not expired yet)
        removed = session_manager.cleanup_expired()

        assert removed == 0
        assert len(session_manager) == 1
        assert info.id in session_manager

    def test_cleanup_partial(self, test_pcb: Path):
        manager = SessionManager(timeout_minutes=0)
        manager.timeout_seconds = 0.1

        # Create two sessions
        info1 = manager.create(str(test_pcb))

        # Wait for first to expire
        time.sleep(0.15)

        # Create second session (should be active)
        info2 = manager.create(str(test_pcb))

        # Cleanup should only remove expired session
        removed = manager.cleanup_expired()

        assert removed == 1
        assert info1.id not in manager
        assert info2.id in manager


class TestThreadSafety:
    """Tests for thread-safe concurrent access."""

    def test_concurrent_create(self, test_pcb: Path):
        manager = SessionManager()
        num_threads = 10
        session_ids: list[str] = []

        def create_session():
            info = manager.create(str(test_pcb))
            return info.id

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(create_session) for _ in range(num_threads)]
            for future in as_completed(futures):
                session_ids.append(future.result())

        # All sessions should be created with unique IDs
        assert len(session_ids) == num_threads
        assert len(set(session_ids)) == num_threads
        assert len(manager) == num_threads

    def test_concurrent_get(self, session_manager: SessionManager, test_pcb: Path):
        info = session_manager.create(str(test_pcb))
        num_threads = 10
        results: list[bool] = []

        def get_session():
            session = session_manager.get(info.id)
            return session is not None

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(get_session) for _ in range(num_threads)]
            for future in as_completed(futures):
                results.append(future.result())

        # All gets should succeed
        assert all(results)
        assert len(results) == num_threads

    def test_concurrent_access_with_modifications(
        self, session_manager: SessionManager, test_pcb: Path
    ):
        info = session_manager.create(str(test_pcb))
        num_threads = 5
        errors: list[Exception] = []

        def modify_session(x_offset: float):
            try:
                session = session_manager.get(info.id)
                # Each thread makes a small modification
                session.query_move("C1", 120.0 + x_offset, 120.0)
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(modify_session, float(i)) for i in range(num_threads)]
            for future in as_completed(futures):
                future.result()  # Wait for completion

        # Should complete without errors
        assert len(errors) == 0


class TestErrorTypes:
    """Tests for custom error types."""

    def test_session_not_found_error(self):
        error = SessionNotFoundError("abc123")
        assert error.session_id == "abc123"
        assert "abc123" in str(error)
        assert "not found" in str(error).lower()

    def test_session_not_found_error_custom_message(self):
        error = SessionNotFoundError("abc123", "Custom message")
        assert error.session_id == "abc123"
        assert str(error) == "Custom message"

    def test_session_operation_error(self):
        error = SessionOperationError("abc123", "commit")
        assert error.session_id == "abc123"
        assert error.operation == "commit"
        assert "commit" in str(error).lower()
        assert "abc123" in str(error)

    def test_session_operation_error_custom_message(self):
        error = SessionOperationError("abc123", "rollback", "Custom failure")
        assert str(error) == "Custom failure"
