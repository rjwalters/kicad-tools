"""Tests for the .kct project specification module."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest


class TestUnitParsing:
    """Tests for unit value parsing."""

    def test_parse_voltage(self):
        """Test parsing voltage values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("5V")
        assert result.value == 5.0
        assert result.unit == "V"

        result = parse_unit_value("3.3V")
        assert result.value == 3.3
        assert result.unit == "V"

    def test_parse_millivolt(self):
        """Test parsing millivolt values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("100mV")
        assert result.value == 0.1
        assert result.unit == "V"

    def test_parse_current(self):
        """Test parsing current values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("2A")
        assert result.value == 2.0
        assert result.unit == "A"

        result = parse_unit_value("500mA")
        assert result.value == 0.5
        assert result.unit == "A"

    def test_parse_resistance_with_unit(self):
        """Test parsing resistance with explicit unit."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("10kΩ")
        assert result.value == 10000.0
        assert result.unit == "Ω"

    def test_parse_bare_resistance(self):
        """Test parsing bare resistance values like '10k'."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("10k")
        assert result.value == 10000.0
        assert result.unit == "Ω"

        result = parse_unit_value("4.7M")
        assert result.value == 4700000.0
        assert result.unit == "Ω"

    def test_parse_capacitance(self):
        """Test parsing capacitance values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("100nF")
        assert result.value == pytest.approx(100e-9)
        assert result.unit == "F"

        result = parse_unit_value("4.7μF")
        assert result.value == pytest.approx(4.7e-6)
        assert result.unit == "F"

    def test_parse_length(self):
        """Test parsing length values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("0.15mm")
        assert result.value == 0.15
        assert result.unit == "mm"

    def test_parse_percentage(self):
        """Test parsing percentage values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("5%")
        assert result.value == 5.0
        assert result.unit == "%"

    def test_parse_with_suffix(self):
        """Test parsing values with measurement suffix."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("50mV_pp")
        assert result.value == 0.05
        assert result.unit == "V_pp"

    def test_parse_negative(self):
        """Test parsing negative values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("-40°C")
        assert result.value == -40.0
        assert result.unit == "°C"

    def test_parse_invalid(self):
        """Test that invalid values raise ValueError."""
        from kicad_tools.spec.units import parse_unit_value

        with pytest.raises(ValueError):
            parse_unit_value("invalid")

    def test_unit_value_comparison(self):
        """Test UnitValue comparison."""
        from kicad_tools.spec.units import parse_unit_value

        v1 = parse_unit_value("5V")
        v2 = parse_unit_value("3.3V")
        v3 = parse_unit_value("5V")

        assert v1 == v3
        assert v1 > v2
        assert v2 < v1


class TestProjectSpec:
    """Tests for ProjectSpec schema."""

    def test_create_minimal_spec(self):
        """Test creating a minimal spec."""
        from kicad_tools.spec import DesignIntent, ProjectMetadata, ProjectSpec

        spec = ProjectSpec(
            project=ProjectMetadata(name="Test Project"),
            intent=DesignIntent(summary="A test board"),
        )

        assert spec.project.name == "Test Project"
        assert spec.intent.summary == "A test board"
        assert spec.kct_version == "1.0"

    def test_create_full_spec(self):
        """Test creating a spec with all sections."""
        from kicad_tools.spec import (
            DesignIntent,
            ElectricalRequirements,
            Progress,
            ProjectMetadata,
            ProjectSpec,
            Requirements,
        )
        from kicad_tools.spec.schema import DesignPhase

        spec = ProjectSpec(
            project=ProjectMetadata(
                name="Power Supply",
                revision="B",
                created=date(2024, 1, 15),
                author="test@example.com",
            ),
            intent=DesignIntent(
                summary="USB-C PD power supply",
                use_cases=["Bench power", "Dev board power"],
            ),
            requirements=Requirements(
                electrical=ElectricalRequirements(
                    esd_protection="IEC 61000-4-2 Level 4",
                ),
            ),
            progress=Progress(
                phase=DesignPhase.SCHEMATIC,
            ),
        )

        assert spec.project.revision == "B"
        assert len(spec.intent.use_cases) == 2
        assert spec.requirements.electrical.esd_protection == "IEC 61000-4-2 Level 4"
        assert spec.progress.phase == DesignPhase.SCHEMATIC

    def test_invalid_version(self):
        """Test that invalid KCT version raises error."""
        from pydantic import ValidationError

        from kicad_tools.spec import ProjectMetadata, ProjectSpec

        with pytest.raises(ValidationError):
            ProjectSpec(
                kct_version="2.0",
                project=ProjectMetadata(name="Test"),
            )

    def test_completion_percentage_empty(self):
        """Test completion percentage with no progress."""
        from kicad_tools.spec import ProjectMetadata, ProjectSpec

        spec = ProjectSpec(project=ProjectMetadata(name="Test"))
        assert spec.get_completion_percentage() == 0.0

    def test_completion_percentage(self):
        """Test completion percentage calculation."""
        from kicad_tools.spec import PhaseProgress, Progress, ProjectMetadata, ProjectSpec

        spec = ProjectSpec(
            project=ProjectMetadata(name="Test"),
            progress=Progress(
                phase="schematic",
                phases={
                    "schematic": PhaseProgress(
                        checklist=[
                            "[x] Item 1",
                            "[x] Item 2",
                            "[ ] Item 3",
                            "[ ] Item 4",
                        ]
                    )
                },
            ),
        )

        assert spec.get_completion_percentage() == 50.0


class TestSpecParser:
    """Tests for spec file loading and saving."""

    def test_save_and_load(self):
        """Test saving and loading a spec file."""
        from kicad_tools.spec import (
            DesignIntent,
            ProjectMetadata,
            ProjectSpec,
            load_spec,
            save_spec,
        )

        spec = ProjectSpec(
            project=ProjectMetadata(
                name="Test Project",
                revision="A",
                created=date.today(),
            ),
            intent=DesignIntent(summary="Test board for testing"),
        )

        with tempfile.NamedTemporaryFile(suffix=".kct", delete=False) as f:
            path = Path(f.name)

        try:
            save_spec(spec, path)
            loaded = load_spec(path)

            assert loaded.project.name == "Test Project"
            assert loaded.intent.summary == "Test board for testing"
        finally:
            path.unlink()

    def test_load_nonexistent(self):
        """Test loading nonexistent file raises error."""
        from kicad_tools.spec import load_spec

        with pytest.raises(FileNotFoundError):
            load_spec("/nonexistent/file.kct")

    def test_validate_valid_spec(self):
        """Test validation of valid spec."""
        from kicad_tools.spec import (
            ProjectMetadata,
            ProjectSpec,
            save_spec,
            validate_spec,
        )

        spec = ProjectSpec(project=ProjectMetadata(name="Test"))

        with tempfile.NamedTemporaryFile(suffix=".kct", delete=False) as f:
            path = Path(f.name)

        try:
            save_spec(spec, path)
            is_valid, errors = validate_spec(path)

            assert is_valid
            assert len(errors) == 0
        finally:
            path.unlink()

    def test_validate_invalid_yaml(self):
        """Test validation of invalid YAML."""
        from kicad_tools.spec import validate_spec

        with tempfile.NamedTemporaryFile(suffix=".kct", delete=False, mode="w") as f:
            f.write("invalid: yaml: content: [")
            path = Path(f.name)

        try:
            is_valid, errors = validate_spec(path)
            assert not is_valid
            assert len(errors) > 0
        finally:
            path.unlink()

    def test_create_minimal_spec_function(self):
        """Test create_minimal_spec helper."""
        from kicad_tools.spec import create_minimal_spec

        spec = create_minimal_spec("My Board", summary="Custom summary")

        assert spec.project.name == "My Board"
        assert spec.intent.summary == "Custom summary"
        assert spec.project.created == date.today()

    def test_get_template(self):
        """Test getting templates."""
        from kicad_tools.spec import get_template

        # Test minimal template
        content = get_template("minimal")
        assert "kct_version" in content
        assert "project:" in content

        # Test power supply template
        content = get_template("power_supply")
        assert "Power Supply" in content or "power" in content.lower()

        # Test invalid template
        with pytest.raises(ValueError):
            get_template("invalid_template")


class TestDecisions:
    """Tests for decision recording."""

    def test_add_decision(self):
        """Test adding a decision to spec."""
        from kicad_tools.spec import (
            Decision,
            ProjectMetadata,
            ProjectSpec,
        )
        from kicad_tools.spec.schema import DesignPhase

        spec = ProjectSpec(
            project=ProjectMetadata(name="Test"),
            decisions=[],
        )

        decision = Decision(
            date=date.today(),
            phase=DesignPhase.SCHEMATIC,
            topic="Regulator Selection",
            choice="LM7805",
            rationale="Common, cheap, adequate for requirements",
            alternatives=["TPS562201", "MP1584"],
        )

        spec.decisions.append(decision)

        assert len(spec.decisions) == 1
        assert spec.decisions[0].topic == "Regulator Selection"
        assert spec.decisions[0].choice == "LM7805"
        assert len(spec.decisions[0].alternatives) == 2

    def test_decision_optional_date_and_phase(self):
        """Test that date and phase fields are optional on Decision.

        Validates fix for issue #806: project.kct files should not require
        date and phase fields on decisions.
        """
        from kicad_tools.spec import Decision

        # Should not raise ValidationError even without date and phase
        decision = Decision(
            topic="Resistor value",
            choice="330 ohm",
            rationale="Standard E24 value closest to ideal",
        )

        assert decision.topic == "Resistor value"
        assert decision.choice == "330 ohm"
        assert decision.date is None
        assert decision.phase is None


class TestProgress:
    """Tests for progress tracking."""

    def test_phase_progress(self):
        """Test phase progress tracking."""
        from kicad_tools.spec import PhaseProgress, Progress
        from kicad_tools.spec.schema import PhaseStatus

        progress = Progress(
            phase="schematic",
            phases={
                "concept": PhaseProgress(
                    status=PhaseStatus.COMPLETED,
                    checklist=["[x] Define requirements"],
                ),
                "schematic": PhaseProgress(
                    status=PhaseStatus.IN_PROGRESS,
                    checklist=[
                        "[x] Power input",
                        "[ ] Buck converter",
                    ],
                ),
            },
        )

        assert progress.phases["concept"].status == PhaseStatus.COMPLETED
        assert progress.phases["schematic"].status == PhaseStatus.IN_PROGRESS

    def test_get_current_phase_progress(self):
        """Test getting current phase progress."""
        from kicad_tools.spec import (
            PhaseProgress,
            Progress,
            ProjectMetadata,
            ProjectSpec,
        )
        from kicad_tools.spec.schema import DesignPhase

        spec = ProjectSpec(
            project=ProjectMetadata(name="Test"),
            progress=Progress(
                phase=DesignPhase.LAYOUT,
                phases={
                    "layout": PhaseProgress(
                        checklist=["[ ] Placement", "[ ] Routing"],
                    ),
                },
            ),
        )

        current = spec.get_current_phase_progress()
        assert current is not None
        assert len(current.checklist) == 2
