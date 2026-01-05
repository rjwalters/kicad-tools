"""Tests for kicad_tools.mcp.tools.session module.

Tests the complete session management workflow:
start_session -> query_move -> apply_move -> commit/rollback
"""

from pathlib import Path

import pytest

from kicad_tools.mcp.tools.session import (
    apply_move,
    commit_session,
    get_session_manager,
    query_move,
    reset_session_manager,
    rollback_session,
    start_session,
    undo_move,
)
from kicad_tools.schema.pcb import PCB

# Simple PCB with movable components for session testing
SESSION_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG1")
  (net 4 "SIG2")

  (gr_line (start 0 0) (end 100 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 100 0) (end 100 80) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 100 80) (end 0 80) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 80) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 20 20)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 40 20)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 4 "SIG2"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 30 40)
    (attr smd)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 4 "SIG2"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 50 40)
    (attr smd)
    (property "Reference" "C2")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (segment (start 20.5 20) (end 39.5 20) (width 0.25) (layer "F.Cu") (net 3))
  (segment (start 40.5 20) (end 29.5 40) (width 0.25) (layer "F.Cu") (net 4))
)
"""


@pytest.fixture(autouse=True)
def reset_sessions():
    """Reset session manager before each test."""
    reset_session_manager()
    yield
    reset_session_manager()


@pytest.fixture
def session_pcb_path(tmp_path: Path) -> str:
    """Create a temporary PCB file for testing."""
    pcb_file = tmp_path / "test_board.kicad_pcb"
    pcb_file.write_text(SESSION_TEST_PCB)
    return str(pcb_file)


class TestStartSession:
    """Tests for start_session function."""

    def test_start_session_success(self, session_pcb_path: str) -> None:
        """Test successful session creation."""
        result = start_session(session_pcb_path)

        assert result.success is True
        assert result.session_id != ""
        assert result.component_count == 4  # R1, R2, C1, C2
        assert result.initial_score > 0
        assert result.error_message is None

    def test_start_session_with_fixed_refs(self, session_pcb_path: str) -> None:
        """Test session with fixed components."""
        result = start_session(session_pcb_path, fixed_refs=["R1", "C1"])

        assert result.success is True
        assert result.fixed_count >= 2

    def test_start_session_file_not_found(self) -> None:
        """Test error handling for missing file."""
        result = start_session("/nonexistent/path/board.kicad_pcb")

        assert result.success is False
        assert "not found" in result.error_message.lower()

    def test_start_session_invalid_extension(self, tmp_path: Path) -> None:
        """Test error handling for invalid file extension."""
        invalid_file = tmp_path / "test.txt"
        invalid_file.write_text("not a pcb")

        result = start_session(str(invalid_file))

        assert result.success is False
        assert "extension" in result.error_message.lower()

    def test_start_session_creates_unique_ids(self, session_pcb_path: str) -> None:
        """Test that each session gets a unique ID."""
        result1 = start_session(session_pcb_path)
        result2 = start_session(session_pcb_path)

        assert result1.session_id != result2.session_id

        # Clean up
        rollback_session(result1.session_id)
        rollback_session(result2.session_id)


class TestQueryMove:
    """Tests for query_move function."""

    def test_query_move_success(self, session_pcb_path: str) -> None:
        """Test successful move query."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        result = query_move(session_id, "R1", 25.0, 25.0)

        assert result.success is True
        assert result.would_succeed is True
        assert isinstance(result.score_delta, float)
        assert result.error_message is None

        rollback_session(session_id)

    def test_query_move_does_not_change_state(self, session_pcb_path: str) -> None:
        """Test that query doesn't modify session state."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id
        manager = get_session_manager()
        metadata = manager.get(session_id)

        # Query a move
        query_move(session_id, "R1", 25.0, 25.0)

        # Verify no pending moves
        assert len(metadata.session.pending_moves) == 0

        rollback_session(session_id)

    def test_query_move_invalid_session(self) -> None:
        """Test error handling for invalid session."""
        result = query_move("invalid-session-id", "R1", 25.0, 25.0)

        assert result.success is False
        assert "not found" in result.error_message.lower()

    def test_query_move_invalid_component(self, session_pcb_path: str) -> None:
        """Test error handling for invalid component."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        result = query_move(session_id, "INVALID", 25.0, 25.0)

        assert result.success is False
        assert "not found" in result.error_message.lower()

        rollback_session(session_id)

    def test_query_move_with_rotation(self, session_pcb_path: str) -> None:
        """Test move query with rotation."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        result = query_move(session_id, "R1", 25.0, 25.0, rotation=90.0)

        assert result.success is True

        rollback_session(session_id)


class TestApplyMove:
    """Tests for apply_move function."""

    def test_apply_move_success(self, session_pcb_path: str) -> None:
        """Test successful move application."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        result = apply_move(session_id, "R1", 25.0, 25.0)

        assert result.success is True
        assert result.move_id > 0
        assert result.pending_moves == 1
        assert result.component is not None
        assert result.component.x == 25.0
        assert result.component.y == 25.0

        rollback_session(session_id)

    def test_apply_move_updates_pending_count(self, session_pcb_path: str) -> None:
        """Test that pending moves count increases."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        result1 = apply_move(session_id, "R1", 25.0, 25.0)
        assert result1.pending_moves == 1

        result2 = apply_move(session_id, "R2", 45.0, 25.0)
        assert result2.pending_moves == 2

        rollback_session(session_id)

    def test_apply_move_with_rotation(self, session_pcb_path: str) -> None:
        """Test move with rotation."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        result = apply_move(session_id, "R1", 25.0, 25.0, rotation=90.0)

        assert result.success is True
        assert result.component.rotation == 90.0

        rollback_session(session_id)

    def test_apply_move_invalid_session(self) -> None:
        """Test error handling for invalid session."""
        result = apply_move("invalid-session-id", "R1", 25.0, 25.0)

        assert result.success is False
        assert "not found" in result.error_message.lower()


class TestUndoMove:
    """Tests for undo_move function."""

    def test_undo_move_success(self, session_pcb_path: str) -> None:
        """Test successful undo."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        # Apply a move
        apply_move(session_id, "R1", 25.0, 25.0)

        # Undo it
        result = undo_move(session_id)

        assert result.success is True
        assert result.pending_moves == 0
        assert result.restored_component is not None
        assert result.restored_component.x == 20.0  # Original position
        assert result.restored_component.y == 20.0

        rollback_session(session_id)

    def test_undo_move_multiple(self, session_pcb_path: str) -> None:
        """Test undoing multiple moves."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        # Apply two moves
        apply_move(session_id, "R1", 25.0, 25.0)
        apply_move(session_id, "R1", 30.0, 30.0)

        # Undo once
        result1 = undo_move(session_id)
        assert result1.pending_moves == 1
        assert result1.restored_component.x == 25.0

        # Undo again
        result2 = undo_move(session_id)
        assert result2.pending_moves == 0
        assert result2.restored_component.x == 20.0

        rollback_session(session_id)

    def test_undo_move_no_moves(self, session_pcb_path: str) -> None:
        """Test undo when no moves to undo."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        result = undo_move(session_id)

        assert result.success is False
        assert "no moves" in result.error_message.lower()

        rollback_session(session_id)

    def test_undo_move_invalid_session(self) -> None:
        """Test error handling for invalid session."""
        result = undo_move("invalid-session-id")

        assert result.success is False
        assert "not found" in result.error_message.lower()


class TestCommitSession:
    """Tests for commit_session function."""

    def test_commit_session_success(self, session_pcb_path: str, tmp_path: Path) -> None:
        """Test successful commit."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        # Apply a move
        apply_move(session_id, "R1", 25.0, 25.0)

        # Commit to a different file
        output_path = str(tmp_path / "output.kicad_pcb")
        result = commit_session(session_id, output_path)

        assert result.success is True
        assert result.output_path == output_path
        assert result.moves_applied == 1
        assert result.session_closed is True
        assert "R1" in result.components_moved

        # Verify the file was saved
        assert Path(output_path).exists()

        # Verify the position was updated in the file
        pcb = PCB.load(output_path)
        r1 = next(fp for fp in pcb.footprints if fp.reference == "R1")
        assert r1.position[0] == 25.0
        assert r1.position[1] == 25.0

    def test_commit_session_overwrites_original(self, tmp_path: Path) -> None:
        """Test commit without output_path overwrites original."""
        # Create a temp PCB file
        pcb_file = tmp_path / "test_board.kicad_pcb"
        pcb_file.write_text(SESSION_TEST_PCB)
        pcb_path = str(pcb_file)

        session_result = start_session(pcb_path)
        session_id = session_result.session_id

        # Apply a move
        apply_move(session_id, "R1", 25.0, 25.0)

        # Commit without output_path
        result = commit_session(session_id)

        assert result.success is True
        assert result.output_path == pcb_path

        # Verify the original file was updated
        pcb = PCB.load(pcb_path)
        r1 = next(fp for fp in pcb.footprints if fp.reference == "R1")
        assert r1.position[0] == 25.0

    def test_commit_session_closes_session(self, session_pcb_path: str, tmp_path: Path) -> None:
        """Test that commit closes the session."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        apply_move(session_id, "R1", 25.0, 25.0)

        output_path = str(tmp_path / "output.kicad_pcb")
        commit_session(session_id, output_path)

        # Session should no longer exist
        manager = get_session_manager()
        assert manager.get(session_id) is None

    def test_commit_session_reports_score_improvement(
        self, session_pcb_path: str, tmp_path: Path
    ) -> None:
        """Test that commit reports score improvement."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id
        initial_score = session_result.initial_score

        apply_move(session_id, "R1", 25.0, 25.0)

        output_path = str(tmp_path / "output.kicad_pcb")
        result = commit_session(session_id, output_path)

        assert result.initial_score == pytest.approx(initial_score, rel=0.01)
        # Score improvement is initial - final (positive = improvement)
        assert isinstance(result.score_improvement, float)

    def test_commit_session_invalid_session(self) -> None:
        """Test error handling for invalid session."""
        result = commit_session("invalid-session-id")

        assert result.success is False
        assert "not found" in result.error_message.lower()


class TestRollbackSession:
    """Tests for rollback_session function."""

    def test_rollback_session_success(self, session_pcb_path: str) -> None:
        """Test successful rollback."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        # Apply some moves
        apply_move(session_id, "R1", 25.0, 25.0)
        apply_move(session_id, "R2", 45.0, 25.0)

        result = rollback_session(session_id)

        assert result.success is True
        assert result.moves_discarded == 2
        assert result.session_closed is True

    def test_rollback_session_closes_session(self, session_pcb_path: str) -> None:
        """Test that rollback closes the session."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        rollback_session(session_id)

        manager = get_session_manager()
        assert manager.get(session_id) is None

    def test_rollback_session_no_moves(self, session_pcb_path: str) -> None:
        """Test rollback with no pending moves."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        result = rollback_session(session_id)

        assert result.success is True
        assert result.moves_discarded == 0
        assert result.session_closed is True

    def test_rollback_session_invalid_session(self) -> None:
        """Test error handling for invalid session."""
        result = rollback_session("invalid-session-id")

        assert result.success is False
        assert "not found" in result.error_message.lower()


class TestFullWorkflow:
    """End-to-end tests for the complete session workflow."""

    def test_full_workflow_query_apply_commit(self, session_pcb_path: str, tmp_path: Path) -> None:
        """Test: start -> query -> apply -> commit."""
        # Start session
        start_result = start_session(session_pcb_path)
        assert start_result.success
        session_id = start_result.session_id

        # Query a move first
        query_result = query_move(session_id, "C1", 35.0, 35.0)
        assert query_result.success
        assert query_result.would_succeed

        # Apply the move
        apply_result = apply_move(session_id, "C1", 35.0, 35.0)
        assert apply_result.success
        assert apply_result.pending_moves == 1

        # Commit changes
        output_path = str(tmp_path / "output.kicad_pcb")
        commit_result = commit_session(session_id, output_path)
        assert commit_result.success
        assert commit_result.moves_applied == 1
        assert "C1" in commit_result.components_moved

        # Verify file was updated
        pcb = PCB.load(output_path)
        c1 = next(fp for fp in pcb.footprints if fp.reference == "C1")
        assert c1.position[0] == 35.0
        assert c1.position[1] == 35.0

    def test_full_workflow_apply_undo_commit(self, session_pcb_path: str, tmp_path: Path) -> None:
        """Test: start -> apply -> undo -> apply -> commit."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        # Apply move
        apply_move(session_id, "R1", 25.0, 25.0)
        assert get_session_manager().get(session_id).session.pending_moves

        # Undo
        undo_result = undo_move(session_id)
        assert undo_result.success
        assert undo_result.pending_moves == 0

        # Apply different move
        apply_move(session_id, "R2", 50.0, 25.0)

        # Commit
        output_path = str(tmp_path / "output.kicad_pcb")
        commit_result = commit_session(session_id, output_path)
        assert commit_result.moves_applied == 1
        assert "R2" in commit_result.components_moved

        # Verify only R2 was moved
        pcb = PCB.load(output_path)
        r1 = next(fp for fp in pcb.footprints if fp.reference == "R1")
        r2 = next(fp for fp in pcb.footprints if fp.reference == "R2")
        assert r1.position[0] == 20.0  # Original position
        assert r2.position[0] == 50.0  # New position

    def test_full_workflow_apply_rollback(self, session_pcb_path: str) -> None:
        """Test: start -> apply -> rollback (no file changes)."""
        # Get original positions
        original_pcb = PCB.load(session_pcb_path)
        original_r1 = next(fp for fp in original_pcb.footprints if fp.reference == "R1")
        original_pos = original_r1.position

        # Start session and apply moves
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        apply_move(session_id, "R1", 50.0, 50.0)
        apply_move(session_id, "R2", 60.0, 60.0)

        # Rollback
        rollback_result = rollback_session(session_id)
        assert rollback_result.success
        assert rollback_result.moves_discarded == 2

        # Verify original file was not modified
        reloaded_pcb = PCB.load(session_pcb_path)
        reloaded_r1 = next(fp for fp in reloaded_pcb.footprints if fp.reference == "R1")
        assert reloaded_r1.position[0] == original_pos[0]
        assert reloaded_r1.position[1] == original_pos[1]

    def test_multiple_components_workflow(self, session_pcb_path: str, tmp_path: Path) -> None:
        """Test moving multiple components in one session."""
        session_result = start_session(session_pcb_path)
        session_id = session_result.session_id

        # Move multiple components
        apply_move(session_id, "R1", 25.0, 25.0)
        apply_move(session_id, "R2", 45.0, 25.0)
        apply_move(session_id, "C1", 35.0, 45.0)

        # Query a fourth component
        query_result = query_move(session_id, "C2", 55.0, 45.0)
        assert query_result.success

        # Apply it
        apply_move(session_id, "C2", 55.0, 45.0)

        # Commit
        output_path = str(tmp_path / "output.kicad_pcb")
        commit_result = commit_session(session_id, output_path)

        assert commit_result.moves_applied == 4
        assert len(commit_result.components_moved) == 4


class TestResultSerialization:
    """Tests for result type serialization."""

    def test_start_session_result_to_dict(self, session_pcb_path: str) -> None:
        """Test StartSessionResult serialization."""
        result = start_session(session_pcb_path)
        d = result.to_dict()

        assert "success" in d
        assert "session_id" in d
        assert "component_count" in d
        assert "initial_score" in d

        rollback_session(result.session_id)

    def test_query_move_result_to_dict(self, session_pcb_path: str) -> None:
        """Test QueryMoveResult serialization."""
        session_result = start_session(session_pcb_path)
        result = query_move(session_result.session_id, "R1", 25.0, 25.0)
        d = result.to_dict()

        assert "success" in d
        assert "would_succeed" in d
        assert "score_delta" in d
        assert "new_violations" in d
        assert "routing_impact" in d

        rollback_session(session_result.session_id)

    def test_apply_move_result_to_dict(self, session_pcb_path: str) -> None:
        """Test ApplyMoveResult serialization."""
        session_result = start_session(session_pcb_path)
        result = apply_move(session_result.session_id, "R1", 25.0, 25.0)
        d = result.to_dict()

        assert "success" in d
        assert "move_id" in d
        assert "component" in d
        assert "pending_moves" in d

        rollback_session(session_result.session_id)

    def test_commit_result_to_dict(self, session_pcb_path: str, tmp_path: Path) -> None:
        """Test CommitResult serialization."""
        session_result = start_session(session_pcb_path)
        apply_move(session_result.session_id, "R1", 25.0, 25.0)

        output_path = str(tmp_path / "output.kicad_pcb")
        result = commit_session(session_result.session_id, output_path)
        d = result.to_dict()

        assert "success" in d
        assert "output_path" in d
        assert "moves_applied" in d
        assert "score_improvement" in d
        assert "components_moved" in d

    def test_rollback_result_to_dict(self, session_pcb_path: str) -> None:
        """Test RollbackResult serialization."""
        session_result = start_session(session_pcb_path)
        result = rollback_session(session_result.session_id)
        d = result.to_dict()

        assert "success" in d
        assert "moves_discarded" in d
        assert "session_closed" in d

    def test_undo_result_to_dict(self, session_pcb_path: str) -> None:
        """Test UndoResult serialization."""
        session_result = start_session(session_pcb_path)
        apply_move(session_result.session_id, "R1", 25.0, 25.0)
        result = undo_move(session_result.session_id)
        d = result.to_dict()

        assert "success" in d
        assert "restored_component" in d
        assert "pending_moves" in d
        assert "current_score" in d

        rollback_session(session_result.session_id)
