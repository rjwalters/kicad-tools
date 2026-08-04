"""Tests for the .kct project specification module."""

from __future__ import annotations

import os
import stat
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
import yaml


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


class TestCopperWeight:
    """Tests for copper_weight parsing on ManufacturingRequirements."""

    def test_copper_weight_int(self):
        """Test copper_weight accepts plain int (backward compat)."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight=2)
        assert mfg.copper_weight == 2.0

    def test_copper_weight_float(self):
        """Test copper_weight accepts plain float."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight=0.5)
        assert mfg.copper_weight == 0.5

    def test_copper_weight_string_with_oz(self):
        """Test copper_weight accepts '2oz' string."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight="2oz")
        assert mfg.copper_weight == 2.0

    def test_copper_weight_fractional_string(self):
        """Test copper_weight accepts '0.5oz' string."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight="0.5oz")
        assert mfg.copper_weight == 0.5

    def test_copper_weight_string_with_space(self):
        """Test copper_weight accepts '2 oz' (space-tolerant)."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight="2 oz")
        assert mfg.copper_weight == 2.0

    def test_copper_weight_case_insensitive(self):
        """Test copper_weight accepts '2OZ' (case-insensitive)."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight="2OZ")
        assert mfg.copper_weight == 2.0

    def test_copper_weight_bare_number_string(self):
        """Test copper_weight accepts bare number string '2'."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight="2")
        assert mfg.copper_weight == 2.0

    def test_copper_weight_invalid_unit(self):
        """Test copper_weight rejects invalid unit like '2lb'."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import ManufacturingRequirements

        with pytest.raises(ValidationError, match="copper_weight"):
            ManufacturingRequirements(copper_weight="2lb")

    def test_copper_weight_invalid_string(self):
        """Test copper_weight rejects non-numeric strings."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import ManufacturingRequirements

        with pytest.raises(ValidationError, match="copper_weight"):
            ManufacturingRequirements(copper_weight="abc")

    def test_copper_weight_default_none(self):
        """Test copper_weight defaults to None."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements()
        assert mfg.copper_weight is None

    def test_copper_weight_extracted_from_layers(self):
        """Test copper_weight is promoted from layers dict to top-level field."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(layers={"count": 2, "copper_weight": "2oz"})
        assert mfg.copper_weight == 2.0
        assert "copper_weight" not in mfg.layers

    def test_layers_without_copper_weight(self):
        """Test layers dict without copper_weight still works."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(layers={"count": 2})
        assert mfg.layers == {"count": 2}
        assert mfg.copper_weight is None


class TestMountingHoleGroupSpec:
    """Tests for the MountingHoleGroupSpec schema (Issue #3352, P_AS1)."""

    def test_basic_construction(self):
        """MountingHoleGroupSpec accepts holes + anchor."""
        from kicad_tools.spec.schema import MountingHoleGroupSpec

        spec = MountingHoleGroupSpec(
            holes=[(0.0, 0.0), (10.0, 0.0)],
            anchor=(5.0, 5.0),
        )
        assert spec.holes == [(0.0, 0.0), (10.0, 0.0)]
        assert spec.anchor == (5.0, 5.0)
        assert spec.hole_diameter_mm == 3.2  # default
        assert spec.keepout_radius_mm == 5.0  # default

    def test_empty_holes_rejected(self):
        """An empty holes list raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import MountingHoleGroupSpec

        with pytest.raises(ValidationError, match="at least one hole"):
            MountingHoleGroupSpec(holes=[], anchor=(0.0, 0.0))

    def test_negative_dimensions_rejected(self):
        """Zero or negative hole_diameter/keepout raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import MountingHoleGroupSpec

        with pytest.raises(ValidationError, match="must be positive"):
            MountingHoleGroupSpec(
                holes=[(0.0, 0.0)],
                anchor=(0.0, 0.0),
                hole_diameter_mm=0.0,
            )
        with pytest.raises(ValidationError, match="must be positive"):
            MountingHoleGroupSpec(
                holes=[(0.0, 0.0)],
                anchor=(0.0, 0.0),
                keepout_radius_mm=-1.0,
            )

    def test_overrides_accepted(self):
        """Construction with explicit hole_diameter / keepout_radius works."""
        from kicad_tools.spec.schema import MountingHoleGroupSpec

        spec = MountingHoleGroupSpec(
            holes=[(0.0, 0.0)],
            anchor=(0.0, 0.0),
            hole_diameter_mm=2.5,
            keepout_radius_mm=3.0,
        )
        assert spec.hole_diameter_mm == 2.5
        assert spec.keepout_radius_mm == 3.0


class TestMechanicalRequirementsEnvelopeHard:
    """Tests for the envelope_hard field on MechanicalRequirements (Issue #3352)."""

    def test_envelope_hard_defaults_false(self):
        """envelope_hard defaults to False (back-compat with existing recipes)."""
        from kicad_tools.spec.schema import MechanicalRequirements

        mech = MechanicalRequirements()
        assert mech.envelope_hard is False

    def test_envelope_hard_true_when_set(self):
        """envelope_hard accepts True."""
        from kicad_tools.spec.schema import MechanicalRequirements

        mech = MechanicalRequirements(envelope_hard=True)
        assert mech.envelope_hard is True

    def test_mounting_hole_group_defaults_none(self):
        """mounting_hole_group defaults to None (back-compat)."""
        from kicad_tools.spec.schema import MechanicalRequirements

        mech = MechanicalRequirements()
        assert mech.mounting_hole_group is None

    def test_mounting_hole_group_attached(self):
        """mounting_hole_group field accepts a MountingHoleGroupSpec."""
        from kicad_tools.spec.schema import (
            MechanicalRequirements,
            MountingHoleGroupSpec,
        )

        group = MountingHoleGroupSpec(
            holes=[(0.0, 0.0), (140.0, 0.0), (0.0, 90.0), (140.0, 90.0)],
            anchor=(5.0, 5.0),
        )
        mech = MechanicalRequirements(mounting_hole_group=group)
        assert mech.mounting_hole_group is group


class TestEscalationPolicy:
    """Tests for the EscalationPolicy schema (Issue #3352, P_AS1)."""

    def test_defaults(self):
        """All EscalationPolicy fields have sensible defaults."""
        from kicad_tools.spec.schema import EscalationPolicy

        policy = EscalationPolicy()
        assert policy.ladder == "layers-first"
        assert policy.max_layers == 4
        assert policy.max_size_tier is None
        assert policy.density_threshold_viols_per_cm2 == 0.5

    def test_all_ladder_values_accepted(self):
        """The five documented ladder values all parse."""
        from kicad_tools.spec.schema import EscalationPolicy

        for value in [
            "layers-first",
            "size-first",
            "layers-only",
            "size-only",
            "none",
        ]:
            policy = EscalationPolicy(ladder=value)
            assert policy.ladder == value

    def test_invalid_ladder_rejected(self):
        """An unknown ladder value raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(ValidationError):
            EscalationPolicy(ladder="bogus")

    def test_max_layers_must_be_positive(self):
        """max_layers < 1 raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(ValidationError, match="max_layers must be >= 1"):
            EscalationPolicy(max_layers=0)

    def test_max_size_tier_negative_rejected(self):
        """A negative max_size_tier raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(ValidationError, match="max_size_tier must be >= 0"):
            EscalationPolicy(max_size_tier=-1)

    def test_density_threshold_negative_rejected(self):
        """A non-positive density threshold raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(
            ValidationError, match="density_threshold_viols_per_cm2 must be positive"
        ):
            EscalationPolicy(density_threshold_viols_per_cm2=0.0)

    def test_attached_to_manufacturing_requirements(self):
        """escalation field on ManufacturingRequirements accepts an EscalationPolicy."""
        from kicad_tools.spec.schema import (
            EscalationPolicy,
            ManufacturingRequirements,
        )

        policy = EscalationPolicy(ladder="size-first", max_layers=6)
        mfg = ManufacturingRequirements(escalation=policy)
        assert mfg.escalation is policy
        assert mfg.escalation.ladder == "size-first"
        assert mfg.escalation.max_layers == 6

    def test_escalation_defaults_none(self):
        """escalation defaults to None on ManufacturingRequirements (back-compat)."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements()
        assert mfg.escalation is None

    # Issue #3400: starting_layers field tests.
    def test_starting_layers_default_is_2(self):
        """starting_layers defaults to 2 (historical behaviour preserved)."""
        from kicad_tools.spec.schema import EscalationPolicy

        policy = EscalationPolicy()
        assert policy.starting_layers == 2

    def test_starting_layers_accepts_2_4_6(self):
        """starting_layers accepts the documented values {2, 4, 6}."""
        from kicad_tools.spec.schema import EscalationPolicy

        # 2 with default max_layers=4: trivially valid.
        assert EscalationPolicy(starting_layers=2).starting_layers == 2
        # 4 with default max_layers=4: equal, valid.
        assert EscalationPolicy(starting_layers=4).starting_layers == 4
        # 6 requires bumping max_layers (cross-field constraint).
        assert EscalationPolicy(starting_layers=6, max_layers=6).starting_layers == 6

    def test_starting_layers_rejects_below_2(self):
        """starting_layers < 2 raises ValidationError (Field ge=2 constraint)."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(ValidationError):
            EscalationPolicy(starting_layers=1)
        with pytest.raises(ValidationError):
            EscalationPolicy(starting_layers=0)

    def test_starting_layers_rejects_above_6(self):
        """starting_layers > 6 raises ValidationError (Field le=6 constraint)."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(ValidationError):
            EscalationPolicy(starting_layers=8, max_layers=8)

    def test_starting_layers_must_not_exceed_max_layers(self):
        """Issue #3400: starting_layers > max_layers raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        # Default max_layers is 4, so starting_layers=6 violates the
        # cross-field constraint.
        with pytest.raises(ValidationError, match="starting_layers"):
            EscalationPolicy(starting_layers=6, max_layers=4)
        # Equal is fine (the rung is reachable).
        EscalationPolicy(starting_layers=4, max_layers=4)
        # Strictly below is fine.
        EscalationPolicy(starting_layers=2, max_layers=4)

    def test_starting_layers_parses_from_yaml(self, tmp_path):
        """A project.kct with starting_layers parses through load_spec."""
        from kicad_tools.spec.parser import load_spec

        spec_text = (
            "project:\n"
            "  name: 'starting_layers smoke'\n"
            "  description: 'Issue #3400 smoke test'\n"
            "requirements:\n"
            "  manufacturing:\n"
            "    escalation:\n"
            "      ladder: layers-first\n"
            "      starting_layers: 4\n"
            "      max_layers: 6\n"
        )
        path = tmp_path / "project.kct"
        path.write_text(spec_text)

        spec = load_spec(path)
        assert spec.requirements.manufacturing.escalation.starting_layers == 4
        assert spec.requirements.manufacturing.escalation.max_layers == 6


class TestBackCompatExistingBoards:
    """Smoke test: existing project.kct files parse cleanly without the new fields."""

    def test_softstart_loads(self):
        """The canonical softstart project.kct parses cleanly through the schema.

        ``boards/external/softstart`` is a symlink into the canonical
        ``rjwalters/softstart`` repo (local-only, like chorus-test-revA);
        ``project.kct`` is the real product spec.  This is a back-compat
        smoke test that a real-world spec file loads end-to-end -- it skips
        when the external repo is not checked out (the symlink dangles).
        """
        from pathlib import Path

        from kicad_tools.spec.parser import load_spec

        path = (
            Path(__file__).resolve().parent.parent
            / "boards"
            / "external"
            / "softstart"
            / "project.kct"
        )
        if not path.exists():
            pytest.skip(f"softstart project.kct not found at {path}")
        spec = load_spec(path)
        # The spec parses and carries the rev B chassis dimensions.
        assert spec.requirements.mechanical.dimensions == {
            "width": "150mm",
            "height": "100mm",
        }

    @pytest.mark.parametrize(
        "board_dir",
        [
            "00-simple-led",
            "01-voltage-divider",
            "02-charlieplex-led",
            "03-usb-joystick",
            "04-stm32-devboard",
            "05-bldc-motor-controller",
            "06-diffpair-test",
            "07-matchgroup-test",
        ],
    )
    def test_board_loads(self, board_dir: str):
        """boards/<dir>/project.kct loads without error (back-compat smoke)."""
        from pathlib import Path

        from kicad_tools.spec.parser import load_spec

        path = Path(__file__).resolve().parent.parent / "boards" / board_dir / "project.kct"
        if not path.exists():
            pytest.skip(f"project.kct not found at {path}")
        spec = load_spec(path)
        # Spec object is well-formed
        assert spec.project is not None


# ---------------------------------------------------------------------------
# Round-trip fidelity / append-only decision log (issue #4538)
# ---------------------------------------------------------------------------

_BOARD_DIRS = [
    "00-simple-led",
    "01-voltage-divider",
    "02-charlieplex-led",
    "03-usb-joystick",
    "04-stm32-devboard",
    "05-bldc-motor-controller",
    "06-diffpair-test",
    "07-matchgroup-test",
]


def _repo_board(board_dir: str) -> Path:
    return Path(__file__).resolve().parent.parent / "boards" / board_dir / "project.kct"


def _leaf_values(obj):
    """Yield every scalar leaf of a parsed YAML tree, as strings."""
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _leaf_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _leaf_values(value)
    else:
        yield str(obj)


class TestSpecRoundTripFidelity:
    """A writer must never silently drop content it does not understand.

    ``.kct`` files are human-authored and rewritten in place. Before #4538 the
    spec models used pydantic's default ``extra="ignore"``, so a single
    ``kct spec decide`` destroyed every key the schema did not define --
    ``requirements.components``, ``progress.checklist``, and the unit-bearing
    ``requirements.electrical`` / ``requirements.mechanical`` keys all vanished
    with a zero exit code and no warning.
    """

    def test_unknown_top_level_key_survives_save(self, tmp_path):
        from kicad_tools.spec import load_spec, save_spec

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\nproject:\n  name: "T"\ncustom_top_level:\n  nested: value\n',
            encoding="utf-8",
        )

        spec = load_spec(path)
        save_spec(spec, path)

        reloaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert reloaded["custom_top_level"] == {"nested": "value"}

    def test_explicitly_null_unknown_key_survives_save(self, tmp_path):
        """``exclude_none`` must prune unset schema fields, never unknown keys."""
        from kicad_tools.spec import load_spec, save_spec

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "mystery_key:\n"
            "requirements:\n"
            "  electrical:\n"
            "    weird:\n"
            "    keep: 5V\n",
            encoding="utf-8",
        )

        save_spec(load_spec(path), path)

        reloaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "mystery_key" in reloaded
        assert "weird" in reloaded["requirements"]["electrical"]
        assert reloaded["requirements"]["electrical"]["keep"] == "5V"

    def test_unknown_nested_key_survives_save(self, tmp_path):
        from kicad_tools.spec import load_spec, save_spec

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "requirements:\n"
            "  electrical:\n"
            "    input_voltage: 5V\n"
            "  components:\n"
            "    - id: R1\n"
            "      value: 330 ohm\n"
            "progress:\n"
            "  phase: concept\n"
            "  checklist:\n"
            '    - "[x] Schematic"\n',
            encoding="utf-8",
        )

        spec = load_spec(path)
        save_spec(spec, path)

        reloaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert reloaded["requirements"]["electrical"]["input_voltage"] == "5V"
        assert reloaded["requirements"]["components"] == [{"id": "R1", "value": "330 ohm"}]
        assert reloaded["progress"]["checklist"] == ["[x] Schematic"]

    @pytest.mark.parametrize("board_dir", _BOARD_DIRS)
    def test_board_round_trip_loses_no_data(self, board_dir: str, tmp_path):
        """load_spec -> save_spec with no mutation loses no leaf value.

        The single tolerated difference is ``copper_weight``, which a
        pre-existing field validator canonicalizes from ``"2oz"`` to the float
        ``2.0``. That is a documented unit parse, not a dropped key.
        """
        from kicad_tools.spec import load_spec, save_spec

        source = _repo_board(board_dir)
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        before = yaml.safe_load(source.read_text(encoding="utf-8"))
        out = tmp_path / "rt.kct"
        save_spec(load_spec(source), out)
        after = yaml.safe_load(out.read_text(encoding="utf-8"))

        tolerated = set()
        weight = (
            (before.get("requirements") or {}).get("manufacturing", {}).get("copper_weight")
            if isinstance(before.get("requirements"), dict)
            else None
        )
        if weight is not None:
            tolerated.add(str(weight))

        missing = Counter(_leaf_values(before)) - Counter(_leaf_values(after))
        assert not (set(missing) - tolerated), f"{board_dir}: lost leaf values {dict(missing)}"

    def test_collect_unknown_keys_reports_schema_drift(self):
        """Unknown keys are preserved, but they must not be invisible."""
        from kicad_tools.spec import collect_unknown_keys, load_spec

        source = _repo_board("00-simple-led")
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        unknown = collect_unknown_keys(load_spec(source))

        assert "requirements.components" in unknown
        assert "progress.checklist" in unknown
        assert "requirements.electrical.input_voltage" in unknown
        assert "requirements.mechanical.board_width" in unknown

    def test_validate_spec_detailed_warns_without_erroring(self):
        from kicad_tools.spec import validate_spec_detailed

        source = _repo_board("00-simple-led")
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        is_valid, errors, warnings = validate_spec_detailed(source)

        assert is_valid, errors
        assert errors == []
        assert any("requirements.components" in w for w in warnings)

    @pytest.mark.parametrize(
        "template_name", ["minimal", "power_supply", "sensor_board", "mcu_breakout"]
    )
    def test_template_parses_and_round_trips(self, template_name: str, tmp_path):
        from kicad_tools.spec import get_template, load_spec, save_spec

        content = get_template(template_name).format(date=date.today().isoformat())
        path = tmp_path / "t.kct"
        path.write_text(content, encoding="utf-8")

        before = yaml.safe_load(content)
        assert isinstance(before, dict), f"{template_name} template is not a YAML mapping"

        save_spec(load_spec(path), path)
        after = yaml.safe_load(path.read_text(encoding="utf-8"))

        missing = Counter(_leaf_values(before)) - Counter(_leaf_values(after))
        assert not missing, f"{template_name}: lost leaf values {dict(missing)}"

    @pytest.mark.parametrize(
        "template_name", ["minimal", "power_supply", "sensor_board", "mcu_breakout"]
    )
    def test_template_carries_decisions_stub(self, template_name: str):
        """Every scaffolded board starts with the design-memory log present."""
        from kicad_tools.spec import get_template

        content = get_template(template_name).format(date=date.today().isoformat())

        assert "\ndecisions: []\n" in content
        assert "kct spec decide" in content
        assert "append" in content.lower()


class TestAppendDecision:
    """``kct spec decide`` must be append-only, not a whole-file rewrite."""

    def _decision(self, topic: str = "PROBE"):
        from kicad_tools.spec import Decision

        return Decision(topic=topic, choice="C", rationale="R", date=date(2026, 1, 1))

    @pytest.mark.parametrize("board_dir", _BOARD_DIRS)
    def test_append_is_byte_level_additive_on_real_boards(self, board_dir: str, tmp_path):
        """The only change to a real board file is the appended entry."""
        from kicad_tools.spec import append_decision

        source = _repo_board(board_dir)
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        original = source.read_text(encoding="utf-8")
        work = tmp_path / "project.kct"
        work.write_text(original, encoding="utf-8")

        append_decision(work, self._decision())
        updated = work.read_text(encoding="utf-8")

        # Every original line survives, in order, unmodified.
        original_lines = original.splitlines()
        updated_lines = updated.splitlines()
        added = [line for line in updated_lines if line not in original_lines]
        remaining = [line for line in updated_lines if line in original_lines]
        assert remaining == original_lines, "existing lines were reordered or rewritten"
        assert any("PROBE" in line for line in added)

        # ... and the parsed data differs only by the appended decision.
        before = yaml.safe_load(original)
        after = yaml.safe_load(updated)
        expected = dict(before)
        expected["decisions"] = [*(before.get("decisions") or []), after["decisions"][-1]]
        assert after == expected

    def test_deciding_twice_leaves_the_first_entry_unchanged(self, tmp_path):
        from kicad_tools.spec import append_decision

        source = _repo_board("00-simple-led")
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        work = tmp_path / "project.kct"
        work.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        pre_existing = yaml.safe_load(work.read_text(encoding="utf-8"))["decisions"]

        append_decision(work, self._decision("FIRST"))
        after_first = yaml.safe_load(work.read_text(encoding="utf-8"))["decisions"]

        append_decision(work, self._decision("SECOND"))
        after_second = yaml.safe_load(work.read_text(encoding="utf-8"))["decisions"]

        assert after_first[: len(pre_existing)] == pre_existing
        assert after_second[: len(after_first)] == after_first
        assert [d["topic"] for d in after_second[-2:]] == ["FIRST", "SECOND"]

    def test_creates_the_list_when_no_decisions_key_exists(self, tmp_path):
        from kicad_tools.spec import append_decision

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\nproject:\n  name: "T"\nprogress:\n  phase: concept\n',
            encoding="utf-8",
        )

        append_decision(path, self._decision())

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert [d["topic"] for d in data["decisions"]] == ["PROBE"]
        assert data["project"]["name"] == "T"
        assert data["progress"]["phase"] == "concept"

    def test_appends_into_an_explicitly_empty_list(self, tmp_path):
        from kicad_tools.spec import append_decision

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "decisions: []\n"
            "progress:\n"
            "  phase: concept\n",
            encoding="utf-8",
        )

        append_decision(path, self._decision())

        text = path.read_text(encoding="utf-8")
        assert "progress:" in text
        data = yaml.safe_load(text)
        assert [d["topic"] for d in data["decisions"]] == ["PROBE"]
        assert data["progress"]["phase"] == "concept"

    def test_preserves_unknown_keys_that_follow_the_decisions_block(self, tmp_path):
        from kicad_tools.spec import append_decision

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "requirements:\n"
            "  electrical:\n"
            "    input_voltage: 5V\n"
            "  components:\n"
            "    - id: R1\n"
            "decisions:\n"
            "  - topic: Existing\n"
            "    choice: X\n"
            "    rationale: |\n"
            "      Multi-line rationale.\n"
            "\n"
            "      With a blank line.\n"
            "\n"
            "# trailing comment section\n"
            "progress:\n"
            "  phase: concept\n"
            "  checklist:\n"
            '    - "[x] Schematic"\n',
            encoding="utf-8",
        )

        append_decision(path, self._decision())

        text = path.read_text(encoding="utf-8")
        assert "# trailing comment section" in text, "comments must survive an append"
        data = yaml.safe_load(text)
        assert data["requirements"]["electrical"]["input_voltage"] == "5V"
        assert data["requirements"]["components"] == [{"id": "R1"}]
        assert data["progress"]["checklist"] == ["[x] Schematic"]
        assert [d["topic"] for d in data["decisions"]] == ["Existing", "PROBE"]
        assert data["decisions"][0]["rationale"] == (
            "Multi-line rationale.\n\nWith a blank line.\n"
        )

    # Shapes where the textual splice *succeeds* and still re-parses as valid
    # YAML, but produces the wrong data -- so only the ``reparsed != expected``
    # comparison in ``append_decision`` can catch them. Each entry is
    # (source text, topics the splice alone would have written).
    _UNSPLICEABLE_SHAPES = {
        # A column-0 comment terminates the block scan early, so the entry is
        # inserted *between* the two existing decisions instead of after them.
        "comment_inside_block": (
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "decisions:\n"
            "  - topic: First\n"
            "    choice: A\n"
            "    rationale: R1\n"
            "# column-0 comment inside the decisions block\n"
            "  - topic: Second\n"
            "    choice: B\n"
            "    rationale: R2\n"
            "progress:\n"
            "  phase: concept\n",
            ["First", "PROBE", "Second"],
            ["First", "Second", "PROBE"],
        ),
        # A quoted key misses the top-level-key regex, so the splice starts a
        # *second* ``decisions:`` block; pyyaml resolves the duplicate
        # last-wins and the pre-existing entry disappears silently.
        "quoted_key": (
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            '"decisions":\n'
            "  - topic: First\n"
            "    choice: A\n"
            "    rationale: R1\n"
            "progress:\n"
            "  phase: concept\n",
            ["PROBE"],
            ["First", "PROBE"],
        ),
    }

    @pytest.mark.parametrize("shape", sorted(_UNSPLICEABLE_SHAPES))
    def test_unfaithful_splice_falls_back_to_a_structural_rewrite(self, shape, tmp_path):
        """A splice that parses but says the wrong thing must never be written.

        ``_splice_decision`` returns text for both of these shapes and that text
        is valid YAML, so nothing but the ``reparsed != expected`` comparison in
        :func:`append_decision` detects that it is wrong. Deleting that guard
        makes this test fail.
        """
        from kicad_tools.spec import append_decision
        from kicad_tools.spec.parser import _splice_decision

        source, spliced_topics, expected_topics = self._UNSPLICEABLE_SHAPES[shape]

        # The raw splice really does produce wrong data for this shape -- that
        # is what makes the guard load-bearing rather than decorative.
        entry = self._decision().model_dump(exclude_none=True, mode="json")
        raw = _splice_decision(source, entry)
        assert raw is not None, f"{shape}: splice declined, so it never reaches the guard"
        assert [d["topic"] for d in yaml.safe_load(raw)["decisions"]] == spliced_topics

        # append_decision must reject it and take the structural-rewrite path.
        path = tmp_path / "p.kct"
        path.write_text(source, encoding="utf-8")
        append_decision(path, self._decision())

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert [d["topic"] for d in data["decisions"]] == expected_topics
        # The rest of the file survives the fallback rewrite intact.
        assert data["project"]["name"] == "T"
        assert data["progress"]["phase"] == "concept"
        assert data["kct_version"] == "1.0"

    def test_malformed_spec_fails_loudly_without_truncating(self, tmp_path):
        from kicad_tools.spec import append_decision

        path = tmp_path / "p.kct"
        original = "invalid: yaml: content: ["
        path.write_text(original, encoding="utf-8")

        with pytest.raises(ValueError):
            append_decision(path, self._decision())

        assert path.read_text(encoding="utf-8") == original

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    @pytest.mark.parametrize("original_mode", [0o644, 0o664, 0o600])
    def test_writing_preserves_the_files_permission_bits(self, original_mode, tmp_path):
        """Neither write path may silently re-permission an existing ``.kct``.

        The atomic write goes through ``tempfile.mkstemp`` (which creates its
        file 0600) plus ``os.replace`` (which carries that mode onto the
        target), so without an explicit chmod every save would narrow a normal
        0644 spec to 0600 -- cumulatively and invisibly, since git tracks only
        the exec bit.
        """
        from kicad_tools.spec import append_decision, load_spec, save_spec

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "decisions: []\n"
            "progress:\n"
            "  phase: concept\n",
            encoding="utf-8",
        )
        os.chmod(path, original_mode)
        assert stat.S_IMODE(path.stat().st_mode) == original_mode

        append_decision(path, self._decision())
        assert stat.S_IMODE(path.stat().st_mode) == original_mode, (
            "append_decision changed the file's permission bits"
        )

        save_spec(load_spec(path), path)
        assert stat.S_IMODE(path.stat().st_mode) == original_mode, (
            "save_spec changed the file's permission bits"
        )

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_new_file_permissions_follow_the_umask(self, tmp_path):
        """A *created* spec gets ordinary umask permissions, not mkstemp's 0600.

        The exact bits depend on the ambient umask, so this asserts equivalence
        with what a plain ``Path.write_text`` produces in the same process
        rather than pinning a literal mode.
        """
        from kicad_tools.spec import load_spec, save_spec

        seed = tmp_path / "seed.kct"
        seed.write_text(
            'kct_version: "1.0"\nproject:\n  name: "T"\nprogress:\n  phase: concept\n',
            encoding="utf-8",
        )

        reference = tmp_path / "reference.kct"
        reference.write_text("x\n", encoding="utf-8")

        created = tmp_path / "created.kct"
        save_spec(load_spec(seed), created)

        assert stat.S_IMODE(created.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)

    def test_cli_decide_on_a_board_copy_is_append_only(self, tmp_path):
        """End-to-end through the CLI handler, not just the library."""
        from argparse import Namespace

        from kicad_tools.cli.commands.spec import run_spec_command

        source = _repo_board("00-simple-led")
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        original = source.read_text(encoding="utf-8")
        work = tmp_path / "project.kct"
        work.write_text(original, encoding="utf-8")

        rc = run_spec_command(
            Namespace(
                spec_command="decide",
                spec_file=str(work),
                decide_topic="CLI PROBE",
                decide_choice="C",
                decide_rationale="R",
                decide_alternatives=None,
            )
        )

        assert rc == 0
        updated = work.read_text(encoding="utf-8")
        # Every original line survives verbatim and in order; only the new
        # entry's lines are added.
        original_lines = original.splitlines()
        kept = [line for line in updated.splitlines() if line in original_lines]
        assert kept == original_lines, "decide rewrote bytes it was not asked to touch"
        assert "CLI PROBE" in updated

        before = yaml.safe_load(original)
        after = yaml.safe_load(updated)
        for key in before:
            if key != "decisions":
                assert after[key] == before[key], f"{key} changed"
        assert after["decisions"][:-1] == before["decisions"]
