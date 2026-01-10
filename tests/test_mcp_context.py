"""Tests for MCP context persistence and decision tracking.

Tests the SessionContext, Decision, StateSnapshot, PreferenceLearner,
and related MCP tools for context persistence.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from kicad_tools.mcp.context import (
    AgentPreferences,
    Decision,
    SessionContext,
    StateSnapshot,
)
from kicad_tools.mcp.preference_learner import PreferenceLearner
from kicad_tools.mcp.tools.context import (
    annotate_decision,
    create_checkpoint,
    get_context_manager,
    get_decision_history,
    get_session_context,
    get_session_summary,
    record_decision,
    reset_context_manager,
    restore_checkpoint,
)


@pytest.fixture(autouse=True)
def reset_context():
    """Reset the context manager before each test."""
    reset_context_manager()
    yield
    reset_context_manager()


class TestDecision:
    """Tests for Decision dataclass."""

    def test_create_decision(self):
        """Test creating a decision with factory method."""
        decision = Decision.create(
            action="move",
            target="C3",
            rationale="Moving bypass cap closer to U1",
            confidence=0.9,
        )

        assert decision.id.startswith("dec_")
        assert decision.action == "move"
        assert decision.target == "C3"
        assert decision.rationale == "Moving bypass cap closer to U1"
        assert decision.confidence == 0.9
        assert decision.outcome == "pending"
        assert decision.timestamp is not None

    def test_decision_to_dict(self):
        """Test converting decision to dictionary."""
        decision = Decision.create(
            action="route",
            target="VDD",
            confidence=0.8,
        )

        d = decision.to_dict()
        assert d["action"] == "route"
        assert d["target"] == "VDD"
        assert d["confidence"] == 0.8
        assert "id" in d
        assert "timestamp" in d

    def test_decision_to_compact_dict(self):
        """Test converting decision to compact dictionary."""
        decision = Decision.create(
            action="move",
            target="R1",
            rationale="This should not appear in compact",
        )

        d = decision.to_compact_dict()
        assert "action" in d
        assert "target" in d
        assert "rationale" not in d

    def test_decision_with_alternatives(self):
        """Test decision with alternatives considered."""
        decision = Decision.create(
            action="move",
            target="C1",
            alternatives=[
                {"target": "C2", "reason": "Too far from power"},
                {"action": "add_cap", "reason": "Would increase BOM"},
            ],
            confidence=0.85,
        )

        assert len(decision.alternatives_considered) == 2
        assert decision.alternatives_considered[0]["target"] == "C2"


class TestStateSnapshot:
    """Tests for StateSnapshot dataclass."""

    def test_create_snapshot(self):
        """Test creating a snapshot with factory method."""
        snapshot = StateSnapshot.create(name="before_routing")

        assert snapshot.snapshot_id.startswith("snap_")
        assert snapshot.name == "before_routing"
        assert snapshot.timestamp is not None
        assert snapshot.drc_violation_count == 0

    def test_snapshot_with_positions(self):
        """Test snapshot with component positions."""
        snapshot = StateSnapshot.create()
        snapshot.component_positions = {
            "C1": (10.0, 20.0, 0.0),
            "R1": (15.0, 25.0, 90.0),
        }
        snapshot.drc_violation_count = 3
        snapshot.score = 125.5

        d = snapshot.to_dict()
        assert "C1" in d["component_positions"]
        assert d["component_positions"]["C1"]["x"] == 10.0
        assert d["component_positions"]["C1"]["rotation"] == 0.0
        assert d["drc_violation_count"] == 3
        assert d["score"] == 125.5


class TestAgentPreferences:
    """Tests for AgentPreferences dataclass."""

    def test_default_preferences(self):
        """Test default preference values."""
        prefs = AgentPreferences()

        assert prefs.preferred_spacing == 2.5
        assert prefs.alignment_preference == "grid"
        assert prefs.via_tolerance == "moderate"
        assert prefs.density_vs_routability == 0.5
        assert prefs.cost_vs_performance == 0.5

    def test_preferences_to_dict(self):
        """Test converting preferences to dictionary."""
        prefs = AgentPreferences(
            preferred_spacing=3.0,
            alignment_preference="functional",
            common_patterns=["bypass_cap_optimization"],
        )

        d = prefs.to_dict()
        assert d["preferred_spacing"] == 3.0
        assert d["alignment_preference"] == "functional"
        assert "bypass_cap_optimization" in d["common_patterns"]


class TestSessionContext:
    """Tests for SessionContext dataclass."""

    def test_create_context(self):
        """Test creating a session context."""
        context = SessionContext(
            session_id="test_123",
            pcb_path="/path/to/board.kicad_pcb",
        )

        assert context.session_id == "test_123"
        assert context.pcb_path == "/path/to/board.kicad_pcb"
        assert context.created_at is not None
        assert len(context.decisions) == 0
        assert len(context.snapshots) == 0

    def test_add_decision(self):
        """Test adding decisions to context."""
        context = SessionContext(session_id="test", pcb_path="/test.kicad_pcb")

        decision = Decision.create(action="move", target="C1")
        context.add_decision(decision)

        assert len(context.decisions) == 1
        assert context.decisions[0].target == "C1"

    def test_get_recent_decisions(self):
        """Test getting recent decisions."""
        context = SessionContext(session_id="test", pcb_path="/test.kicad_pcb")

        for i in range(15):
            decision = Decision.create(action="move", target=f"C{i}")
            context.add_decision(decision)

        recent = context.get_recent_decisions(5)
        assert len(recent) == 5
        assert recent[-1].target == "C14"

    def test_get_decisions_by_action(self):
        """Test filtering decisions by action type."""
        context = SessionContext(session_id="test", pcb_path="/test.kicad_pcb")

        context.add_decision(Decision.create(action="move", target="C1"))
        context.add_decision(Decision.create(action="route", target="VDD"))
        context.add_decision(Decision.create(action="move", target="C2"))

        moves = context.get_decisions_by_action("move")
        assert len(moves) == 2

        routes = context.get_decisions_by_action("route")
        assert len(routes) == 1

    def test_create_checkpoint(self):
        """Test creating a named checkpoint."""
        context = SessionContext(session_id="test", pcb_path="/test.kicad_pcb")

        snapshot = StateSnapshot.create()
        snapshot.drc_violation_count = 5
        snapshot.score = 100.0

        checkpoint_id = context.create_checkpoint("before_power", snapshot)

        assert checkpoint_id.startswith("cp_")
        assert checkpoint_id in context.checkpoints
        assert len(context.snapshots) == 1
        assert context.snapshots[0].name == "before_power"

    def test_get_checkpoint_snapshot(self):
        """Test retrieving checkpoint snapshot."""
        context = SessionContext(session_id="test", pcb_path="/test.kicad_pcb")

        snapshot = StateSnapshot.create()
        snapshot.score = 150.0

        checkpoint_id = context.create_checkpoint("test_cp", snapshot)

        retrieved = context.get_checkpoint_snapshot(checkpoint_id)
        assert retrieved is not None
        assert retrieved.score == 150.0

    def test_get_context_summary(self):
        """Test getting context at summary level."""
        context = SessionContext(session_id="test", pcb_path="/test.kicad_pcb")

        context.add_decision(Decision.create(action="move", target="C1"))
        context.add_decision(Decision.create(action="move", target="C2"))

        ctx = context.get_context("summary")
        assert ctx["session_id"] == "test"
        assert ctx["decision_count"] == 2

    def test_get_context_detailed(self):
        """Test getting context at detailed level."""
        context = SessionContext(session_id="test", pcb_path="/test.kicad_pcb")

        context.add_decision(Decision.create(action="move", target="C1"))

        ctx = context.get_context("detailed")
        assert "recent_decisions" in ctx
        assert len(ctx["recent_decisions"]) == 1

    def test_get_summary(self):
        """Test getting token-efficient summary."""
        context = SessionContext(
            session_id="test",
            pcb_path="/path/to/board.kicad_pcb",
        )

        context.add_decision(Decision.create(action="move", target="C1"))

        summary = context.get_summary()
        assert "test" in summary
        assert "Decisions: 1 total" in summary


class TestPreferenceLearner:
    """Tests for PreferenceLearner."""

    def test_analyze_empty_decisions(self):
        """Test analyzing empty decision list."""
        learner = PreferenceLearner()
        prefs = learner.analyze_decisions([])

        assert prefs.preferred_spacing == 2.5  # Default
        assert prefs.alignment_preference == "grid"

    def test_analyze_move_decisions(self):
        """Test analyzing move decisions for spacing."""
        learner = PreferenceLearner()

        decisions = [
            Decision.create(
                action="move",
                target="C1",
                params={"spacing": 3.5},
            ),
            Decision.create(
                action="move",
                target="C2",
                params={"spacing": 3.0},
            ),
            Decision.create(
                action="move",
                target="C3",
                params={"spacing": 4.0},
            ),
        ]

        prefs = learner.analyze_decisions(decisions)
        # Median of [3.0, 3.5, 4.0] = 3.5
        assert prefs.preferred_spacing == 3.5

    def test_detect_bypass_cap_pattern(self):
        """Test detecting bypass cap optimization pattern."""
        learner = PreferenceLearner()

        decisions = [
            Decision.create(
                action="move",
                target="C1",
                rationale="Moving bypass cap closer to VDD pin",
            ),
            Decision.create(
                action="move",
                target="C2",
                rationale="Decoupling cap for U2 power",
            ),
        ]

        prefs = learner.analyze_decisions(decisions)
        assert "bypass_cap_optimization" in prefs.common_patterns

    def test_detect_avoided_patterns(self):
        """Test detecting avoided patterns from reverted decisions."""
        learner = PreferenceLearner()

        decision = Decision.create(
            action="move",
            target="C1",
            rationale="Moving bypass cap",
        )
        decision.outcome = "reverted"

        prefs = learner.analyze_decisions([decision])
        assert "bypass_cap_optimization" in prefs.avoided_patterns

    def test_suggest_based_on_preferences(self):
        """Test generating suggestions from preferences."""
        learner = PreferenceLearner()

        context = SessionContext(session_id="test", pcb_path="/test.kicad_pcb")
        context.preferences.preferred_spacing = 1.5  # Tight spacing
        context.preferences.common_patterns = ["bypass_cap_optimization"]

        suggestions = learner.suggest_based_on_preferences(context)
        assert len(suggestions) > 0
        assert any("tight spacing" in s for s in suggestions)
        assert any("bypass" in s.lower() for s in suggestions)


class TestContextTools:
    """Tests for MCP context tools."""

    def test_record_decision(self):
        """Test recording a decision."""
        # First create a context
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        result = record_decision(
            session_id="test_session",
            action="move",
            target="C1",
            rationale="Test decision",
            confidence=0.9,
        )

        assert result.success
        assert result.decision_id.startswith("dec_")

    def test_record_decision_creates_context(self):
        """Test that record_decision creates context if needed."""
        result = record_decision(
            session_id="new_session",
            action="move",
            target="C1",
        )

        assert result.success
        context = get_context_manager().get("new_session")
        assert context is not None
        assert len(context.decisions) == 1

    def test_get_decision_history(self):
        """Test getting decision history."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        # Record some decisions
        for i in range(5):
            record_decision(
                session_id="test_session",
                action="move",
                target=f"C{i}",
            )

        result = get_decision_history(
            session_id="test_session",
            limit=3,
        )

        assert result.success
        assert len(result.decisions) == 3
        assert result.total == 5

    def test_get_decision_history_with_filter(self):
        """Test filtering decision history by action."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        record_decision(session_id="test_session", action="move", target="C1")
        record_decision(session_id="test_session", action="route", target="VDD")
        record_decision(session_id="test_session", action="move", target="C2")

        result = get_decision_history(
            session_id="test_session",
            filter_action="move",
        )

        assert result.success
        assert len(result.decisions) == 2

    def test_get_decision_history_session_not_found(self):
        """Test getting history for non-existent session."""
        result = get_decision_history(session_id="nonexistent")

        assert not result.success
        assert "not found" in result.error_message.lower()

    def test_annotate_decision(self):
        """Test annotating a decision."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        # Record a decision
        record_result = record_decision(
            session_id="test_session",
            action="move",
            target="C1",
        )

        # Annotate it
        result = annotate_decision(
            session_id="test_session",
            decision_id=record_result.decision_id,
            feedback="This improved signal integrity",
            outcome="success",
        )

        assert result.success

        # Verify the annotation
        context = get_context_manager().get("test_session")
        decision = context.decisions[0]
        assert decision.feedback == "This improved signal integrity"
        assert decision.outcome == "success"

    def test_annotate_decision_not_found(self):
        """Test annotating non-existent decision."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        result = annotate_decision(
            session_id="test_session",
            decision_id="nonexistent",
            feedback="test",
        )

        assert not result.success
        assert "not found" in result.error_message.lower()

    def test_annotate_decision_invalid_outcome(self):
        """Test annotating with invalid outcome."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        record_result = record_decision(
            session_id="test_session",
            action="move",
            target="C1",
        )

        result = annotate_decision(
            session_id="test_session",
            decision_id=record_result.decision_id,
            feedback="test",
            outcome="invalid_outcome",
        )

        assert not result.success
        assert "invalid outcome" in result.error_message.lower()

    def test_get_session_context_summary(self):
        """Test getting session context at summary level."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        record_decision(session_id="test_session", action="move", target="C1")

        result = get_session_context(
            session_id="test_session",
            detail_level="summary",
        )

        assert result.success
        assert result.context["session_id"] == "test_session"
        assert result.context["decision_count"] == 1

    def test_get_session_context_detailed(self):
        """Test getting session context at detailed level."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        record_decision(session_id="test_session", action="move", target="C1")

        result = get_session_context(
            session_id="test_session",
            detail_level="detailed",
        )

        assert result.success
        assert "recent_decisions" in result.context

    def test_get_session_context_invalid_level(self):
        """Test getting context with invalid detail level."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        result = get_session_context(
            session_id="test_session",
            detail_level="invalid",
        )

        assert not result.success
        assert "invalid" in result.error_message.lower()

    def test_create_checkpoint(self):
        """Test creating a checkpoint."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        result = create_checkpoint(
            session_id="test_session",
            name="before_routing",
            drc_violation_count=5,
            score=100.0,
        )

        assert result.success
        assert result.checkpoint_id.startswith("cp_")
        assert result.name == "before_routing"

    def test_create_checkpoint_without_name(self):
        """Test creating an unnamed checkpoint."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        result = create_checkpoint(session_id="test_session")

        assert result.success
        assert result.checkpoint_id.startswith("cp_")

    def test_restore_checkpoint(self):
        """Test restoring a checkpoint."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        # Create checkpoint
        create_result = create_checkpoint(
            session_id="test_session",
            name="test_checkpoint",
        )

        # Restore checkpoint
        result = restore_checkpoint(
            session_id="test_session",
            checkpoint_id=create_result.checkpoint_id,
        )

        assert result.success
        assert result.checkpoint_id == create_result.checkpoint_id
        assert result.name == "test_checkpoint"

    def test_restore_checkpoint_not_found(self):
        """Test restoring non-existent checkpoint."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        result = restore_checkpoint(
            session_id="test_session",
            checkpoint_id="nonexistent",
        )

        assert not result.success
        assert "not found" in result.error_message.lower()

    def test_get_session_summary(self):
        """Test getting token-efficient session summary."""
        get_context_manager().get_or_create("test_session", "/test.kicad_pcb")

        record_decision(session_id="test_session", action="move", target="C1")

        result = get_session_summary(session_id="test_session")

        assert result.success
        assert "test_session" in result.summary
        assert result.token_estimate > 0

    def test_get_session_summary_session_not_found(self):
        """Test getting summary for non-existent session."""
        result = get_session_summary(session_id="nonexistent")

        assert not result.success
        assert "not found" in result.error_message.lower()


class TestContextManager:
    """Tests for ContextManager."""

    def test_get_or_create(self):
        """Test get_or_create method."""
        manager = get_context_manager()

        context = manager.get_or_create("test", "/test.kicad_pcb")
        assert context is not None
        assert context.session_id == "test"

        # Getting again should return same context
        context2 = manager.get_or_create("test", "/other.kicad_pcb")
        assert context2 is context

    def test_close_session(self):
        """Test closing a session."""
        manager = get_context_manager()

        manager.get_or_create("test", "/test.kicad_pcb")
        assert manager.close("test")
        assert manager.get("test") is None

    def test_list_sessions(self):
        """Test listing sessions."""
        manager = get_context_manager()

        manager.get_or_create("session1", "/test1.kicad_pcb")
        manager.get_or_create("session2", "/test2.kicad_pcb")

        sessions = manager.list_sessions()
        assert "session1" in sessions
        assert "session2" in sessions
