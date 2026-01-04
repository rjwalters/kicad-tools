"""Tests for the reasoning/agent module."""

from unittest.mock import MagicMock, patch

from kicad_tools.reasoning.agent import (
    AgentProgress,
    PCBReasoningAgent,
    ReasoningStep,
)
from kicad_tools.reasoning.commands import (
    Command,
    CommandResult,
    CommandType,
    DeleteTraceCommand,
    PlaceComponentCommand,
    RouteNetCommand,
)
from kicad_tools.reasoning.state import (
    PCBState,
    ViolationState,
)

# =============================================================================
# Helper Functions
# =============================================================================


def create_mock_pcb_state(
    unrouted_nets: list | None = None,
    routed_nets: list | None = None,
    violations: list | None = None,
    shorts: list | None = None,
    clearance_violations: list | None = None,
    components: dict | None = None,
    nets: dict | None = None,
) -> MagicMock:
    """Helper to create a mock PCBState for testing.

    Uses MagicMock because PCBState has computed properties that can't be set directly.
    """
    mock_state = MagicMock(spec=PCBState)

    # Set up outline
    mock_outline = MagicMock()
    mock_outline.width = 100
    mock_outline.height = 100
    mock_state.outline = mock_outline

    # Set up layers
    mock_state.layers = ["F.Cu", "B.Cu"]

    # Set up computed properties
    mock_state.unrouted_nets = unrouted_nets or []
    mock_state.routed_nets = routed_nets or []
    mock_state.violations = violations or []
    mock_state.shorts = shorts or []
    mock_state.clearance_violations = clearance_violations or []
    mock_state.components = components or {}
    mock_state.nets = nets or {}
    mock_state.traces = []
    mock_state.vias = []
    mock_state.zones = []

    # Set up methods
    mock_state.to_prompt = MagicMock(return_value="## Board State\nMock state")
    mock_state.summary = MagicMock(return_value={"components": 0, "nets": 0})

    return mock_state


def create_mock_command() -> Command:
    """Create a simple mock command for testing."""
    return RouteNetCommand(net="TEST_NET")


def create_mock_command_result(success: bool = True, message: str = "Test") -> CommandResult:
    """Create a mock command result for testing."""
    return CommandResult(
        success=success,
        command_type=CommandType.ROUTE_NET,
        message=message,
    )


# =============================================================================
# Tests for ReasoningStep
# =============================================================================


class TestReasoningStep:
    """Tests for ReasoningStep dataclass."""

    def test_create_reasoning_step(self):
        """Test creating a reasoning step."""
        command = create_mock_command()
        result = create_mock_command_result()

        step = ReasoningStep(
            step_number=1,
            timestamp="2024-01-01T12:00:00",
            command=command,
            result=result,
            diagnosis=None,
        )

        assert step.step_number == 1
        assert step.timestamp == "2024-01-01T12:00:00"
        assert step.command == command
        assert step.result == result
        assert step.diagnosis is None

    def test_reasoning_step_with_diagnosis(self):
        """Test reasoning step with a diagnosis."""
        command = create_mock_command()
        result = create_mock_command_result(success=False, message="Failed")

        step = ReasoningStep(
            step_number=2,
            timestamp="2024-01-01T12:05:00",
            command=command,
            result=result,
            diagnosis="Path blocked by component U1",
        )

        assert step.step_number == 2
        assert step.diagnosis == "Path blocked by component U1"

    def test_to_dict_success(self):
        """Test to_dict for successful step."""
        command = RouteNetCommand(net="VCC", minimize_vias=True)
        result = CommandResult(
            success=True,
            command_type=CommandType.ROUTE_NET,
            message="Routed successfully",
        )

        step = ReasoningStep(
            step_number=1,
            timestamp="2024-01-01T12:00:00",
            command=command,
            result=result,
        )

        data = step.to_dict()

        assert data["step"] == 1
        assert data["timestamp"] == "2024-01-01T12:00:00"
        assert data["command"]["type"] == "route_net"
        assert data["command"]["net"] == "VCC"
        assert data["result"]["success"] is True
        assert data["result"]["message"] == "Routed successfully"
        assert data["diagnosis"] is None

    def test_to_dict_with_diagnosis(self):
        """Test to_dict for failed step with diagnosis."""
        command = PlaceComponentCommand(ref="U1", at=(50, 50))
        result = CommandResult(
            success=False,
            command_type=CommandType.PLACE_COMPONENT,
            message="Position occupied",
        )

        step = ReasoningStep(
            step_number=3,
            timestamp="2024-01-01T12:15:00",
            command=command,
            result=result,
            diagnosis="Position (50, 50) is occupied by C1",
        )

        data = step.to_dict()

        assert data["step"] == 3
        assert data["result"]["success"] is False
        assert data["diagnosis"] == "Position (50, 50) is occupied by C1"


# =============================================================================
# Tests for AgentProgress
# =============================================================================


class TestAgentProgress:
    """Tests for AgentProgress dataclass."""

    def test_create_progress(self):
        """Test creating agent progress."""
        progress = AgentProgress(
            nets_total=10,
            nets_routed=5,
            violations_initial=3,
            violations_current=1,
            steps_taken=8,
            commands_successful=6,
            commands_failed=2,
        )

        assert progress.nets_total == 10
        assert progress.nets_routed == 5
        assert progress.violations_initial == 3
        assert progress.violations_current == 1
        assert progress.steps_taken == 8
        assert progress.commands_successful == 6
        assert progress.commands_failed == 2

    def test_routing_progress_partial(self):
        """Test routing progress calculation with partial completion."""
        progress = AgentProgress(
            nets_total=10,
            nets_routed=5,
            violations_initial=0,
            violations_current=0,
            steps_taken=0,
            commands_successful=0,
            commands_failed=0,
        )

        assert progress.routing_progress == 0.5

    def test_routing_progress_complete(self):
        """Test routing progress when all nets routed."""
        progress = AgentProgress(
            nets_total=10,
            nets_routed=10,
            violations_initial=0,
            violations_current=0,
            steps_taken=0,
            commands_successful=0,
            commands_failed=0,
        )

        assert progress.routing_progress == 1.0

    def test_routing_progress_none_routed(self):
        """Test routing progress when no nets routed."""
        progress = AgentProgress(
            nets_total=10,
            nets_routed=0,
            violations_initial=0,
            violations_current=0,
            steps_taken=0,
            commands_successful=0,
            commands_failed=0,
        )

        assert progress.routing_progress == 0.0

    def test_routing_progress_zero_total(self):
        """Test routing progress when no nets to route."""
        progress = AgentProgress(
            nets_total=0,
            nets_routed=0,
            violations_initial=0,
            violations_current=0,
            steps_taken=0,
            commands_successful=0,
            commands_failed=0,
        )

        # Should return 1.0 (complete) when nothing to route
        assert progress.routing_progress == 1.0

    def test_violation_improvement_all_resolved(self):
        """Test violation improvement when all resolved."""
        progress = AgentProgress(
            nets_total=0,
            nets_routed=0,
            violations_initial=10,
            violations_current=0,
            steps_taken=0,
            commands_successful=0,
            commands_failed=0,
        )

        assert progress.violation_improvement == 1.0

    def test_violation_improvement_partial(self):
        """Test violation improvement with partial resolution."""
        progress = AgentProgress(
            nets_total=0,
            nets_routed=0,
            violations_initial=10,
            violations_current=4,
            steps_taken=0,
            commands_successful=0,
            commands_failed=0,
        )

        # 6/10 = 0.6 resolved
        assert progress.violation_improvement == 0.6

    def test_violation_improvement_none_resolved(self):
        """Test violation improvement when none resolved."""
        progress = AgentProgress(
            nets_total=0,
            nets_routed=0,
            violations_initial=10,
            violations_current=10,
            steps_taken=0,
            commands_successful=0,
            commands_failed=0,
        )

        assert progress.violation_improvement == 0.0

    def test_violation_improvement_no_initial(self):
        """Test violation improvement when no initial violations."""
        progress = AgentProgress(
            nets_total=0,
            nets_routed=0,
            violations_initial=0,
            violations_current=0,
            steps_taken=0,
            commands_successful=0,
            commands_failed=0,
        )

        # Should return 1.0 (complete) when nothing to fix
        assert progress.violation_improvement == 1.0

    def test_to_prompt(self):
        """Test generating progress prompt."""
        progress = AgentProgress(
            nets_total=10,
            nets_routed=7,
            violations_initial=5,
            violations_current=2,
            steps_taken=15,
            commands_successful=12,
            commands_failed=3,
        )

        prompt = progress.to_prompt()

        assert "## Progress" in prompt
        assert "Nets routed: 7/10 (70%)" in prompt
        assert "Violations: 2 (started at 5)" in prompt
        assert "Steps taken: 15" in prompt
        assert "Success rate: 12/15" in prompt

    def test_to_prompt_full_progress(self):
        """Test prompt with full progress."""
        progress = AgentProgress(
            nets_total=5,
            nets_routed=5,
            violations_initial=3,
            violations_current=0,
            steps_taken=8,
            commands_successful=8,
            commands_failed=0,
        )

        prompt = progress.to_prompt()

        assert "100%" in prompt
        assert "Violations: 0 (started at 3)" in prompt

    def test_to_prompt_zero_progress(self):
        """Test prompt with zero progress."""
        progress = AgentProgress(
            nets_total=10,
            nets_routed=0,
            violations_initial=0,
            violations_current=5,
            steps_taken=0,
            commands_successful=0,
            commands_failed=0,
        )

        prompt = progress.to_prompt()

        assert "0%" in prompt


# =============================================================================
# Tests for PCBReasoningAgent
# =============================================================================


class TestPCBReasoningAgentInit:
    """Tests for PCBReasoningAgent initialization."""

    @patch("kicad_tools.reasoning.agent.PCBState.from_pcb")
    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_init_with_state(self, mock_diag, mock_interp, mock_from_pcb):
        """Test initialization with provided state."""
        state = create_mock_pcb_state()

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        assert agent.state == state
        assert agent.pcb_path.name == "board.kicad_pcb"
        assert agent.history == []
        assert agent.step_count == 0
        # from_pcb should not be called when state is provided
        mock_from_pcb.assert_not_called()

    @patch("kicad_tools.reasoning.agent.PCBState.from_pcb")
    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_init_loads_state(self, mock_diag, mock_interp, mock_from_pcb):
        """Test initialization loads state from file."""
        mock_state = create_mock_pcb_state()
        mock_from_pcb.return_value = mock_state

        agent = PCBReasoningAgent(pcb_path="/test/board.kicad_pcb")

        mock_from_pcb.assert_called_once()

    @patch("kicad_tools.reasoning.agent.PCBState.from_pcb")
    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_init_tracks_initial_state(self, mock_diag, mock_interp, mock_from_pcb):
        """Test initialization tracks initial unrouted nets and violations."""
        # Create mock unrouted nets
        unrouted1 = MagicMock()
        unrouted1.name = "NET1"
        unrouted2 = MagicMock()
        unrouted2.name = "NET2"

        # Create violations
        violations = [
            ViolationState(
                type="clearance",
                severity="error",
                message="Clearance violation",
                x=10,
                y=20,
                layer="F.Cu",
                nets=["NET1"],
                items=[],
            ),
        ]

        state = create_mock_pcb_state(
            unrouted_nets=[unrouted1, unrouted2],
            violations=violations,
        )

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        assert agent.initial_unrouted == 2
        assert agent.initial_violations == 1


class TestPCBReasoningAgentProgress:
    """Tests for PCBReasoningAgent progress tracking."""

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_get_progress(self, mock_diag, mock_interp):
        """Test get_progress returns correct AgentProgress."""
        # Create mock routed and unrouted nets
        routed1 = MagicMock()
        routed1.name = "VCC"
        unrouted1 = MagicMock()
        unrouted1.name = "SDA"

        state = create_mock_pcb_state(
            routed_nets=[routed1],
            unrouted_nets=[unrouted1],
        )

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        progress = agent.get_progress()

        assert progress.nets_total == 2
        assert progress.nets_routed == 1
        assert progress.steps_taken == 0
        assert progress.commands_successful == 0
        assert progress.commands_failed == 0

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_get_progress_with_history(self, mock_diag, mock_interp):
        """Test get_progress tracks command success/failure from history."""
        state = create_mock_pcb_state()

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        # Add history entries
        success_result = CommandResult(
            success=True, command_type=CommandType.ROUTE_NET, message="OK"
        )
        fail_result = CommandResult(
            success=False, command_type=CommandType.ROUTE_NET, message="Failed"
        )

        agent.history = [
            ReasoningStep(1, "t1", RouteNetCommand(net="N1"), success_result),
            ReasoningStep(2, "t2", RouteNetCommand(net="N2"), fail_result),
            ReasoningStep(3, "t3", RouteNetCommand(net="N3"), success_result),
        ]

        progress = agent.get_progress()

        assert progress.steps_taken == 3
        assert progress.commands_successful == 2
        assert progress.commands_failed == 1


class TestPCBReasoningAgentCompletion:
    """Tests for PCBReasoningAgent completion checks."""

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_is_complete_true(self, mock_diag, mock_interp):
        """Test is_complete returns True when done."""
        state = create_mock_pcb_state()

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        assert agent.is_complete() is True

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_is_complete_false_unrouted(self, mock_diag, mock_interp):
        """Test is_complete returns False with unrouted nets."""
        unrouted = MagicMock()
        unrouted.name = "NET1"

        state = create_mock_pcb_state(unrouted_nets=[unrouted])

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        assert agent.is_complete() is False

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_is_complete_false_violations(self, mock_diag, mock_interp):
        """Test is_complete returns False with violations."""
        violations = [
            ViolationState(
                type="clearance",
                severity="error",
                message="Violation",
                x=10,
                y=20,
                layer="F.Cu",
                nets=[],
                items=[],
            )
        ]

        state = create_mock_pcb_state(violations=violations)

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        assert agent.is_complete() is False

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_is_complete_with_max_violations(self, mock_diag, mock_interp):
        """Test is_complete with max_violations threshold."""
        violations = [
            ViolationState(
                type="clearance",
                severity="error",
                message="V1",
                x=10,
                y=20,
                layer="F.Cu",
                nets=[],
                items=[],
            ),
            ViolationState(
                type="clearance",
                severity="error",
                message="V2",
                x=30,
                y=40,
                layer="F.Cu",
                nets=[],
                items=[],
            ),
        ]

        state = create_mock_pcb_state(violations=violations)

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        # 2 violations, max 2 -> complete
        assert agent.is_complete(max_violations=2) is True
        # 2 violations, max 1 -> not complete
        assert agent.is_complete(max_violations=1) is False

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_is_drc_clean_true(self, mock_diag, mock_interp):
        """Test is_drc_clean returns True when no violations."""
        state = create_mock_pcb_state()

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        assert agent.is_drc_clean() is True

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_is_drc_clean_false(self, mock_diag, mock_interp):
        """Test is_drc_clean returns False with violations."""
        violations = [
            ViolationState(
                type="short",
                severity="error",
                message="Short",
                x=10,
                y=20,
                layer="F.Cu",
                nets=[],
                items=[],
            )
        ]

        state = create_mock_pcb_state(violations=violations)

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        assert agent.is_drc_clean() is False


class TestPCBReasoningAgentExecution:
    """Tests for PCBReasoningAgent command execution."""

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_execute_success(self, mock_diag_cls, mock_interp_cls):
        """Test successful command execution."""
        state = create_mock_pcb_state()

        # Set up mock interpreter
        mock_interp = MagicMock()
        mock_interp.execute.return_value = CommandResult(
            success=True,
            command_type=CommandType.ROUTE_NET,
            message="Routed successfully",
        )
        mock_interp_cls.return_value = mock_interp

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        command = RouteNetCommand(net="VCC")
        result, diagnosis = agent.execute(command)

        assert result.success is True
        assert diagnosis is None
        assert agent.step_count == 1
        assert len(agent.history) == 1
        assert agent.history[0].command == command
        assert agent.history[0].result == result

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_execute_failure_generates_diagnosis(self, mock_diag_cls, mock_interp_cls):
        """Test failed command execution generates diagnosis."""
        state = create_mock_pcb_state()

        # Set up mock interpreter
        mock_interp = MagicMock()
        mock_interp.execute.return_value = CommandResult(
            success=False,
            command_type=CommandType.PLACE_COMPONENT,
            message="Position occupied",
        )
        mock_interp_cls.return_value = mock_interp

        # Set up mock diagnosis engine
        mock_diag = MagicMock()
        mock_diagnosis_result = MagicMock()
        mock_diagnosis_result.to_prompt.return_value = "Position (50, 50) blocked by C1"
        mock_diag.diagnose_placement.return_value = mock_diagnosis_result
        mock_diag_cls.return_value = mock_diag

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        command = PlaceComponentCommand(ref="U1", at=(50, 50))
        result, diagnosis = agent.execute(command)

        assert result.success is False
        assert diagnosis == "Position (50, 50) blocked by C1"
        assert agent.history[0].diagnosis == diagnosis

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_execute_dict(self, mock_diag_cls, mock_interp_cls):
        """Test executing command from dictionary."""
        state = create_mock_pcb_state()

        # Set up mock interpreter
        mock_interp = MagicMock()
        mock_interp.execute.return_value = CommandResult(
            success=True,
            command_type=CommandType.ROUTE_NET,
            message="Routed",
        )
        mock_interp_cls.return_value = mock_interp

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        command_dict = {"type": "route_net", "net": "VCC"}
        result, diagnosis = agent.execute_dict(command_dict)

        assert result.success is True
        assert agent.step_count == 1


class TestPCBReasoningAgentPrompt:
    """Tests for PCBReasoningAgent prompt generation."""

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_get_prompt_basic(self, mock_diag_cls, mock_interp_cls):
        """Test basic prompt generation."""
        state = create_mock_pcb_state()

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        prompt = agent.get_prompt()

        assert "## Progress" in prompt
        assert "## Board State" in prompt or "Mock state" in prompt

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_get_prompt_with_history(self, mock_diag_cls, mock_interp_cls):
        """Test prompt includes recent history."""
        state = create_mock_pcb_state()

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        # Add history
        command = RouteNetCommand(net="VCC")
        result = CommandResult(success=True, command_type=CommandType.ROUTE_NET, message="Done")
        agent.history.append(ReasoningStep(1, "t1", command, result))

        prompt = agent.get_prompt(include_history=True)

        assert "Recent Actions" in prompt
        assert "Step 1" in prompt

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_get_prompt_without_progress(self, mock_diag_cls, mock_interp_cls):
        """Test prompt can exclude progress."""
        state = create_mock_pcb_state()

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        prompt = agent.get_prompt(include_progress=False)

        # Should not include progress section header
        # (though _suggest_next_action will still be included)
        assert "Nets routed:" not in prompt


class TestPCBReasoningAgentSuggestions:
    """Tests for PCBReasoningAgent next action suggestions."""

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_suggest_fix_shorts_priority(self, mock_diag_cls, mock_interp_cls):
        """Test suggestions prioritize fixing shorts."""
        # Create short violation
        short = ViolationState(
            type="short",
            severity="error",
            message="Short circuit",
            x=50,
            y=50,
            layer="F.Cu",
            nets=["VCC", "GND"],
            items=[],
        )

        state = create_mock_pcb_state(shorts=[short])

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        suggestion = agent._suggest_next_action()

        assert "PRIORITY" in suggestion
        assert "short" in suggestion.lower()

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_suggest_route_unrouted_net(self, mock_diag_cls, mock_interp_cls):
        """Test suggestions include unrouted nets."""
        # Create unrouted net
        unrouted = MagicMock()
        unrouted.name = "SPI_CLK"
        unrouted.priority = 1
        unrouted.pad_count = 2

        state = create_mock_pcb_state(unrouted_nets=[unrouted])

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        suggestion = agent._suggest_next_action()

        assert "SPI_CLK" in suggestion
        assert "Route" in suggestion

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_suggest_complete_message(self, mock_diag_cls, mock_interp_cls):
        """Test suggestion when all work is done."""
        state = create_mock_pcb_state()

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        suggestion = agent._suggest_next_action()

        assert "All nets routed" in suggestion
        assert "no critical violations" in suggestion


class TestPCBReasoningAgentConvenience:
    """Tests for PCBReasoningAgent convenience methods."""

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_get_state(self, mock_diag_cls, mock_interp_cls):
        """Test get_state returns the state."""
        state = create_mock_pcb_state()

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        assert agent.get_state() == state

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_route_priority_nets(self, mock_diag_cls, mock_interp_cls):
        """Test route_priority_nets routes highest priority nets."""
        # Create unrouted nets with priority
        net1 = MagicMock()
        net1.name = "VCC"
        net1.priority = 1

        net2 = MagicMock()
        net2.name = "GND"
        net2.priority = 2

        state = create_mock_pcb_state(unrouted_nets=[net2, net1])  # Unsorted

        # Set up mock interpreter
        mock_interp = MagicMock()
        mock_interp.execute.return_value = CommandResult(
            success=True,
            command_type=CommandType.ROUTE_NET,
            message="Routed",
        )
        mock_interp_cls.return_value = mock_interp

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        results = agent.route_priority_nets(max_nets=2)

        assert len(results) == 2
        assert all(r.success for r in results)

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_fix_shorts(self, mock_diag_cls, mock_interp_cls):
        """Test fix_shorts creates delete commands for shorts."""
        # Create short violation
        short = ViolationState(
            type="short",
            severity="error",
            message="Short",
            x=50,
            y=50,
            layer="F.Cu",
            nets=["VCC"],
            items=[],
        )

        state = create_mock_pcb_state(shorts=[short])

        # Set up mock interpreter
        mock_interp = MagicMock()
        mock_interp.execute.return_value = CommandResult(
            success=True,
            command_type=CommandType.DELETE_TRACE,
            message="Deleted",
        )
        mock_interp_cls.return_value = mock_interp

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        results = agent.fix_shorts()

        assert len(results) == 1
        # Verify a delete command was executed
        call_args = mock_interp.execute.call_args[0]
        assert isinstance(call_args[0], DeleteTraceCommand)
        assert call_args[0].net == "VCC"


class TestPCBReasoningAgentSaveExport:
    """Tests for PCBReasoningAgent save and export."""

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_save(self, mock_diag_cls, mock_interp_cls):
        """Test save calls interpreter save."""
        state = create_mock_pcb_state()

        mock_interp = MagicMock()
        mock_interp_cls.return_value = mock_interp

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        agent.save("/output/board.kicad_pcb")

        mock_interp.save.assert_called_once_with("/output/board.kicad_pcb")

    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    @patch("pathlib.Path.write_text")
    def test_export_history(self, mock_write, mock_diag_cls, mock_interp_cls):
        """Test export_history writes JSON."""
        state = create_mock_pcb_state()

        agent = PCBReasoningAgent(
            pcb_path="/test/board.kicad_pcb",
            state=state,
        )

        # Add history
        command = RouteNetCommand(net="VCC")
        result = CommandResult(success=True, command_type=CommandType.ROUTE_NET, message="Done")
        agent.history.append(ReasoningStep(1, "t1", command, result))

        agent.export_history("/output/history.json")

        mock_write.assert_called_once()
        written_json = mock_write.call_args[0][0]

        import json

        data = json.loads(written_json)

        assert data["pcb_file"] == "/test/board.kicad_pcb"
        assert len(data["steps"]) == 1
        assert data["steps"][0]["step"] == 1


class TestPCBReasoningAgentFromPCB:
    """Tests for PCBReasoningAgent.from_pcb class method."""

    @patch("kicad_tools.reasoning.agent.DRCReport.load")
    @patch("kicad_tools.reasoning.agent.PCBState.from_pcb")
    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_from_pcb_no_drc(self, mock_diag, mock_interp, mock_from_pcb, mock_drc_load):
        """Test from_pcb without DRC report."""
        mock_state = create_mock_pcb_state()
        mock_from_pcb.return_value = mock_state

        agent = PCBReasoningAgent.from_pcb("/test/board.kicad_pcb")

        mock_from_pcb.assert_called_once()
        mock_drc_load.assert_not_called()

    @patch("kicad_tools.reasoning.agent.DRCReport.load")
    @patch("kicad_tools.reasoning.agent.PCBState.from_pcb")
    @patch("kicad_tools.reasoning.agent.CommandInterpreter")
    @patch("kicad_tools.reasoning.agent.DiagnosisEngine")
    def test_from_pcb_with_drc(self, mock_diag, mock_interp, mock_from_pcb, mock_drc_load):
        """Test from_pcb with DRC report."""
        mock_state = create_mock_pcb_state()
        mock_from_pcb.return_value = mock_state

        mock_drc = MagicMock()
        mock_drc_load.return_value = mock_drc

        agent = PCBReasoningAgent.from_pcb(
            "/test/board.kicad_pcb",
            drc_path="/test/board.rpt",
        )

        mock_drc_load.assert_called_once_with("/test/board.rpt")
