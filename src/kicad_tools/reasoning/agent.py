"""
PCB Reasoning Agent - Main orchestrator for LLM-driven layout.

The agent coordinates:
1. State extraction and presentation
2. Command interpretation and execution
3. Failure diagnosis and feedback
4. Progress tracking

It provides the main interface for integrating with an LLM.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from ..drc.report import DRCReport
from ..pcb.editor import PCBEditor

from .state import PCBState
from .vocabulary import SpatialRegion, NetType, create_hat_regions
from .commands import (
    Command,
    CommandResult,
    RouteNetCommand,
    DeleteTraceCommand,
    PlaceComponentCommand,
    parse_command,
)
from .interpreter import CommandInterpreter, InterpreterConfig
from .diagnosis import DiagnosisEngine, RoutingDiagnosis


@dataclass
class ReasoningStep:
    """A single step in the reasoning process."""

    step_number: int
    timestamp: str
    command: Command
    result: CommandResult
    diagnosis: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "step": self.step_number,
            "timestamp": self.timestamp,
            "command": self.command.to_dict(),
            "result": {
                "success": self.result.success,
                "message": self.result.message,
            },
            "diagnosis": self.diagnosis,
        }


@dataclass
class AgentProgress:
    """Tracks progress toward completion."""

    nets_total: int
    nets_routed: int
    violations_initial: int
    violations_current: int
    steps_taken: int
    commands_successful: int
    commands_failed: int

    @property
    def routing_progress(self) -> float:
        """Fraction of nets routed (0-1)."""
        if self.nets_total == 0:
            return 1.0
        return self.nets_routed / self.nets_total

    @property
    def violation_improvement(self) -> float:
        """Fraction of violations resolved (0-1)."""
        if self.violations_initial == 0:
            return 1.0
        return 1.0 - (self.violations_current / self.violations_initial)

    def to_prompt(self) -> str:
        """Generate progress summary for LLM."""
        lines = [
            "## Progress",
            f"Nets routed: {self.nets_routed}/{self.nets_total} ({self.routing_progress:.0%})",
            f"Violations: {self.violations_current} (started at {self.violations_initial})",
            f"Steps taken: {self.steps_taken}",
            f"Success rate: {self.commands_successful}/{self.steps_taken}",
        ]
        return "\n".join(lines)


class PCBReasoningAgent:
    """Main agent for LLM-driven PCB layout.

    Usage:
        agent = PCBReasoningAgent.from_pcb("board.kicad_pcb")

        # Reasoning loop
        while not agent.is_complete():
            # Get current state for LLM
            prompt = agent.get_prompt()

            # LLM generates command (external call)
            command = llm_generate_command(prompt)

            # Execute and get feedback
            result, diagnosis = agent.execute(command)

            if not result.success:
                # Show diagnosis to LLM for next iteration
                print(diagnosis)

        agent.save("board-routed.kicad_pcb")
    """

    def __init__(
        self,
        pcb_path: str,
        state: Optional[PCBState] = None,
        drc_report: Optional[DRCReport] = None,
        config: Optional[InterpreterConfig] = None,
        regions: Optional[list[SpatialRegion]] = None,
    ):
        self.pcb_path = Path(pcb_path)
        self.config = config or InterpreterConfig()

        # Load state
        if state:
            self.state = state
        else:
            self.state = PCBState.from_pcb(pcb_path, drc_report)

        # Set up regions (default to HAT regions)
        if regions:
            self.regions = regions
        else:
            self.regions = create_hat_regions(
                self.state.outline.width,
                self.state.outline.height,
            )

        # Create interpreter and diagnosis engine
        self.interpreter = CommandInterpreter(
            str(pcb_path),
            state=self.state,
            config=self.config,
            regions=self.regions,
        )
        self.diagnosis_engine = DiagnosisEngine(self.state, self.regions)

        # Track history
        self.history: list[ReasoningStep] = []
        self.step_count = 0

        # Track initial state for progress
        self.initial_unrouted = len(self.state.unrouted_nets)
        self.initial_violations = len(self.state.violations)

    @classmethod
    def from_pcb(
        cls,
        pcb_path: str,
        drc_path: Optional[str] = None,
        config: Optional[InterpreterConfig] = None,
    ) -> "PCBReasoningAgent":
        """Create agent from PCB file path."""
        drc_report = None
        if drc_path:
            drc_report = DRCReport.load(drc_path)

        return cls(
            pcb_path=pcb_path,
            drc_report=drc_report,
            config=config,
        )

    # =========================================================================
    # State Access
    # =========================================================================

    def get_state(self) -> PCBState:
        """Get current PCB state."""
        return self.state

    def get_progress(self) -> AgentProgress:
        """Get current progress."""
        nets_routed = len(self.state.routed_nets)
        nets_total = nets_routed + len(self.state.unrouted_nets)

        successful = sum(1 for s in self.history if s.result.success)
        failed = len(self.history) - successful

        return AgentProgress(
            nets_total=nets_total,
            nets_routed=nets_routed,
            violations_initial=self.initial_violations,
            violations_current=len(self.state.violations),
            steps_taken=len(self.history),
            commands_successful=successful,
            commands_failed=failed,
        )

    def get_prompt(
        self,
        include_progress: bool = True,
        include_violations: bool = True,
        include_history: bool = True,
        max_history: int = 5,
    ) -> str:
        """Generate a prompt for the LLM.

        This is the primary interface for getting state to the LLM.
        """
        sections = []

        # Progress
        if include_progress:
            progress = self.get_progress()
            sections.append(progress.to_prompt())

        # Current state
        sections.append(self.state.to_prompt(include_violations=include_violations))

        # Recent history
        if include_history and self.history:
            sections.append("## Recent Actions")
            for step in self.history[-max_history:]:
                status = "✓" if step.result.success else "✗"
                sections.append(f"{status} Step {step.step_number}: {step.command.describe()}")
                if not step.result.success and step.diagnosis:
                    sections.append(f"   {step.diagnosis[:100]}...")

        # Suggested next action
        sections.append(self._suggest_next_action())

        return "\n\n".join(sections)

    def _suggest_next_action(self) -> str:
        """Generate suggestions for what to do next."""
        lines = ["## Suggested Next Action"]

        # Check for critical violations first
        shorts = self.state.shorts
        if shorts:
            lines.append(
                f"PRIORITY: Fix {len(shorts)} short circuits before further routing"
            )
            v = shorts[0]
            if v.nets:
                lines.append(
                    f"  Suggestion: Delete traces for {v.nets[0]} near ({v.x:.1f}, {v.y:.1f})"
                )
            return "\n".join(lines)

        # Check for unrouted nets
        unrouted = sorted(self.state.unrouted_nets, key=lambda n: n.priority)
        if unrouted:
            net = unrouted[0]
            net_type = NetType.classify(net.name)
            lines.append(f"Route {net.name} ({net_type.value}, {net.pad_count} pads)")

            # Add routing hints based on net type
            if net_type == NetType.CLOCK:
                lines.append("  Note: Clock signal - minimize length, avoid analog section")
            elif net_type == NetType.POWER:
                lines.append("  Note: Power net - use wider traces, consider pour")

            return "\n".join(lines)

        # Check for clearance violations
        clearance = self.state.clearance_violations
        if clearance:
            lines.append(f"Fix {len(clearance)} clearance violations")
            v = clearance[0]
            lines.append(f"  First violation at ({v.x:.1f}, {v.y:.1f})")
            return "\n".join(lines)

        lines.append("All nets routed and no critical violations!")
        return "\n".join(lines)

    # =========================================================================
    # Command Execution
    # =========================================================================

    def execute(self, command: Command) -> tuple[CommandResult, Optional[str]]:
        """Execute a command and return result with diagnosis.

        Args:
            command: The command to execute

        Returns:
            Tuple of (result, diagnosis_string)
            Diagnosis is None on success, detailed analysis on failure.
        """
        self.step_count += 1

        # Execute via interpreter
        result = self.interpreter.execute(command)

        # Generate diagnosis if failed
        diagnosis = None
        if not result.success:
            diagnosis = self._diagnose_failure(command, result)

        # Record step
        step = ReasoningStep(
            step_number=self.step_count,
            timestamp=datetime.now().isoformat(),
            command=command,
            result=result,
            diagnosis=diagnosis,
        )
        self.history.append(step)

        return result, diagnosis

    def execute_dict(self, command_dict: dict) -> tuple[CommandResult, Optional[str]]:
        """Execute a command from dictionary representation."""
        command = parse_command(command_dict)
        return self.execute(command)

    def _diagnose_failure(self, command: Command, result: CommandResult) -> str:
        """Generate diagnosis for a failed command."""
        if isinstance(command, RouteNetCommand):
            # Get pads for the net
            pads = []
            for comp in self.state.components.values():
                for pad in comp.pads:
                    if pad.net == command.net:
                        pads.append(pad)

            if len(pads) >= 2:
                start = (pads[0].x, pads[0].y)
                end = (pads[1].x, pads[1].y)

                diagnosis = self.diagnosis_engine.diagnose_routing(
                    result, command.net, start, end
                )
                return diagnosis.to_prompt()

        elif isinstance(command, PlaceComponentCommand):
            target = command.at or (0, 0)
            diagnosis = self.diagnosis_engine.diagnose_placement(
                result, command.ref, target
            )
            return diagnosis.to_prompt()

        return f"Command failed: {result.message}"

    # =========================================================================
    # Completion Check
    # =========================================================================

    def is_complete(self, max_violations: int = 0) -> bool:
        """Check if layout is complete.

        Completion means:
        - All nets are routed
        - Violations are within acceptable limit
        """
        if self.state.unrouted_nets:
            return False

        if len(self.state.violations) > max_violations:
            return False

        return True

    def is_drc_clean(self) -> bool:
        """Check if there are no DRC violations."""
        return len(self.state.violations) == 0

    # =========================================================================
    # DRC Integration
    # =========================================================================

    def update_violations(self, drc_report: DRCReport):
        """Update state with new DRC report."""
        self.state.violations.clear()

        for v in drc_report.violations:
            from .state import ViolationState

            loc = v.primary_location
            self.state.violations.append(
                ViolationState(
                    type=v.type.value,
                    severity="error" if v.is_error else "warning",
                    message=v.message,
                    x=loc.x_mm if loc else 0,
                    y=loc.y_mm if loc else 0,
                    layer=loc.layer if loc else "",
                    nets=v.nets.copy(),
                    items=v.items.copy(),
                )
            )

    def run_drc_check(self, kicad_cli: str = "kicad-cli") -> Optional[DRCReport]:
        """Run DRC check using KiCad CLI (if available).

        Args:
            kicad_cli: Path to kicad-cli executable

        Returns:
            DRCReport if successful, None if DRC check failed
        """
        import subprocess
        import tempfile

        # Save current state first
        temp_output = Path(tempfile.mktemp(suffix=".kicad_pcb"))
        self.save(str(temp_output))

        # Run DRC
        drc_output = Path(tempfile.mktemp(suffix=".rpt"))
        try:
            result = subprocess.run(
                [
                    kicad_cli,
                    "pcb",
                    "drc",
                    "--exit-code-violations",
                    "-o",
                    str(drc_output),
                    str(temp_output),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if drc_output.exists():
                report = DRCReport.load(drc_output)
                self.update_violations(report)
                return report

        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            pass
        finally:
            # Cleanup
            if temp_output.exists():
                temp_output.unlink()
            if drc_output.exists():
                drc_output.unlink()

        return None

    # =========================================================================
    # Save / Export
    # =========================================================================

    def save(self, output_path: Optional[str] = None):
        """Save the current PCB state."""
        self.interpreter.save(output_path)

    def export_history(self, path: str):
        """Export reasoning history to JSON."""
        data = {
            "pcb_file": str(self.pcb_path),
            "steps": [s.to_dict() for s in self.history],
            "progress": {
                "nets_routed": len(self.state.routed_nets),
                "nets_total": len(self.state.routed_nets) + len(self.state.unrouted_nets),
                "violations": len(self.state.violations),
            },
        }
        Path(path).write_text(json.dumps(data, indent=2))

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    def route_priority_nets(self, max_nets: int = 10) -> list[CommandResult]:
        """Route the highest priority unrouted nets.

        This is a convenience method for automated routing
        without LLM involvement.
        """
        results = []
        unrouted = sorted(self.state.unrouted_nets, key=lambda n: n.priority)

        for net in unrouted[:max_nets]:
            command = RouteNetCommand(
                net=net.name,
                minimize_vias=True,
            )
            result, _ = self.execute(command)
            results.append(result)

        return results

    def fix_shorts(self) -> list[CommandResult]:
        """Attempt to fix short circuits by deleting offending traces."""
        results = []

        for v in self.state.shorts:
            if v.nets:
                command = DeleteTraceCommand(
                    net=v.nets[0],
                    near=(v.x, v.y),
                    radius=2.0,
                    reason="fixing short circuit",
                )
                result, _ = self.execute(command)
                results.append(result)

        return results

    def analyze_current_state(self) -> str:
        """Generate a detailed analysis of current state."""
        lines = []

        lines.append("# PCB Analysis")
        lines.append("")

        # Summary
        summary = self.state.summary()
        lines.append("## Summary")
        for key, value in summary.items():
            lines.append(f"- {key}: {value}")
        lines.append("")

        # Violation analysis
        lines.append(self.diagnosis_engine.analyze_violations())

        # Routing suggestions
        lines.append("## Routing Recommendations")
        unrouted = sorted(self.state.unrouted_nets, key=lambda n: n.priority)
        for net in unrouted[:5]:
            net_type = NetType.classify(net.name)
            lines.append(f"- {net.name} ({net_type.value})")

        return "\n".join(lines)
