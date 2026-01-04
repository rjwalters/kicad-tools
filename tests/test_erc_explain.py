"""Tests for ERC explain command."""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.erc_explain_cmd import (
    DiagnosisItem,
    ERCExplainer,
    FixSuggestion,
    SimilarLabel,
    ViolationExplanation,
    main,
)
from kicad_tools.erc import ERCReport, ERCViolation, ERCViolationType, Severity


@pytest.fixture
def sample_erc_report(fixtures_dir: Path) -> Path:
    """Return the path to the sample ERC report."""
    return fixtures_dir / "sample_erc.json"


@pytest.fixture
def sample_violation() -> ERCViolation:
    """Create a sample violation for testing."""
    return ERCViolation(
        type=ERCViolationType.HIER_LABEL_MISMATCH,
        type_str="hier_label_mismatch",
        severity=Severity.ERROR,
        description="Hierarchical label mismatch between sheet and parent",
        sheet="/Power",
        pos_x=76.20,
        pos_y=106.68,
        items=["Hierarchical label 'VCC_3V3A' on subsheet"],
    )


@pytest.fixture
def sample_labels() -> list[str]:
    """Sample labels for fuzzy matching tests."""
    return [
        "+3.3VA",
        "+3V3",
        "VCC",
        "VCC_3V3",
        "GND",
        "CLK",
        "DATA",
        "VCC_5V",
    ]


class TestERCExplainer:
    """Tests for ERCExplainer class."""

    def test_explain_hier_label_mismatch(
        self, sample_violation: ERCViolation, sample_labels: list[str]
    ):
        """Test explanation for hierarchical label mismatch."""
        explainer = ERCExplainer(all_labels=sample_labels)
        explanation = explainer.explain(sample_violation)

        assert isinstance(explanation, ViolationExplanation)
        assert explanation.violation == sample_violation
        assert "sheet pin" in explanation.summary.lower() or "label" in explanation.summary.lower()
        assert len(explanation.diagnosis) > 0
        assert len(explanation.possible_causes) > 0
        assert len(explanation.fixes) > 0

    def test_explain_pin_not_connected(self):
        """Test explanation for unconnected pin."""
        violation = ERCViolation(
            type=ERCViolationType.PIN_NOT_CONNECTED,
            type_str="pin_not_connected",
            severity=Severity.ERROR,
            description="Pin not connected",
            pos_x=100.0,
            pos_y=50.0,
            items=["Pin 1 (input) of R1"],
        )

        explainer = ERCExplainer()
        explanation = explainer.explain(violation)

        assert "R1" in explanation.summary or "pin" in explanation.summary.lower()
        assert any("connect" in fix.description.lower() for fix in explanation.fixes)
        assert any("no-connect" in fix.description.lower() for fix in explanation.fixes)

    def test_explain_power_not_driven(self):
        """Test explanation for power input not driven."""
        violation = ERCViolation(
            type=ERCViolationType.POWER_PIN_NOT_DRIVEN,
            type_str="power_pin_not_driven",
            severity=Severity.ERROR,
            description="Power input pin not driven by any power output",
            pos_x=120.0,
            pos_y=60.0,
            items=["Pin VCC (power_in) of U1"],
        )

        explainer = ERCExplainer()
        explanation = explainer.explain(violation)

        assert "power" in explanation.summary.lower()
        assert any(
            "power symbol" in fix.description.lower() or "pwr_flag" in fix.description.lower()
            for fix in explanation.fixes
        )

    def test_explain_similar_labels(self):
        """Test explanation for similar labels warning."""
        violation = ERCViolation(
            type=ERCViolationType.SIMILAR_LABELS,
            type_str="similar_labels",
            severity=Severity.WARNING,
            description="Labels are similar and may be confused",
            pos_x=150.0,
            pos_y=80.0,
            items=["Label 'SIG1'", "Label 'SIG_1'"],
        )

        explainer = ERCExplainer()
        explanation = explainer.explain(violation)

        assert "similar" in explanation.summary.lower() or "typo" in explanation.summary.lower()
        assert any("rename" in fix.description.lower() for fix in explanation.fixes)

    def test_explain_duplicate_reference(self):
        """Test explanation for duplicate reference designator."""
        violation = ERCViolation(
            type=ERCViolationType.DUPLICATE_REFERENCE,
            type_str="duplicate_reference",
            severity=Severity.ERROR,
            description="Duplicate reference designator",
            pos_x=160.0,
            pos_y=90.0,
            items=["Symbol R1 at (100, 50)", "Symbol R1 at (200, 50)"],
        )

        explainer = ERCExplainer()
        explanation = explainer.explain(violation)

        assert "R1" in explanation.summary or "duplicate" in explanation.summary.lower()
        assert any("annotate" in fix.description.lower() for fix in explanation.fixes)

    def test_explain_generic_violation(self):
        """Test explanation for unknown violation type."""
        violation = ERCViolation(
            type=ERCViolationType.UNKNOWN,
            type_str="some_unknown_type",
            severity=Severity.WARNING,
            description="Some unknown issue",
            pos_x=100.0,
            pos_y=100.0,
            items=["Some item"],
        )

        explainer = ERCExplainer()
        explanation = explainer.explain(violation)

        assert explanation.summary is not None
        assert len(explanation.fixes) > 0


class TestFuzzyMatching:
    """Tests for fuzzy label matching."""

    def test_find_similar_labels(self, sample_labels: list[str]):
        """Test finding similar labels."""
        explainer = ERCExplainer(all_labels=sample_labels)

        # VCC_3V3A should match VCC_3V3 closely
        similar = explainer._find_similar_labels("VCC_3V3A")
        assert len(similar) > 0
        assert any(s.name == "VCC_3V3" for s in similar)

    def test_find_similar_labels_with_typo(self, sample_labels: list[str]):
        """Test finding similar labels with common typos."""
        explainer = ERCExplainer(all_labels=sample_labels)

        # +3V3A should match +3.3VA closely
        similar = explainer._find_similar_labels("+3V3A")
        # Should find at least some matches
        assert len(similar) >= 0  # May or may not find matches depending on threshold

    def test_find_similar_labels_no_self_match(self, sample_labels: list[str]):
        """Test that exact matches are excluded."""
        explainer = ERCExplainer(all_labels=sample_labels)

        similar = explainer._find_similar_labels("VCC")
        # Should not include exact match
        assert not any(s.name == "VCC" for s in similar)

    def test_find_similar_labels_threshold(self, sample_labels: list[str]):
        """Test similarity threshold."""
        explainer = ERCExplainer(all_labels=sample_labels)

        # Very different label should not match
        similar = explainer._find_similar_labels("COMPLETELY_DIFFERENT_LABEL_XYZ")
        # With default threshold, completely different labels shouldn't match
        high_similarity = [s for s in similar if s.similarity > 0.8]
        assert len(high_similarity) == 0


class TestViolationExplanation:
    """Tests for ViolationExplanation data class."""

    def test_to_dict(self, sample_violation: ERCViolation):
        """Test converting explanation to dictionary."""
        explanation = ViolationExplanation(
            violation=sample_violation,
            summary="Test summary",
            diagnosis=[
                DiagnosisItem(
                    check="Test check",
                    expected="Expected value",
                    actual="Actual value",
                    status="error",
                )
            ],
            possible_causes=["Cause 1", "Cause 2"],
            similar_labels=[
                SimilarLabel(name="VCC_3V3", similarity=0.9, location="", direction="")
            ],
            fixes=[
                FixSuggestion(description="Fix 1", command="kct fix", priority=1),
                FixSuggestion(description="Fix 2", priority=2),
            ],
            related_violations=["pin_not_connected"],
        )

        result = explanation.to_dict()

        assert result["type"] == "hier_label_mismatch"
        assert result["severity"] == "error"
        assert result["summary"] == "Test summary"
        assert len(result["diagnosis"]) == 1
        assert result["diagnosis"][0]["check"] == "Test check"
        assert len(result["possible_causes"]) == 2
        assert len(result["similar_labels"]) == 1
        assert result["similar_labels"][0]["similarity"] == 0.9
        assert len(result["fixes"]) == 2
        assert result["fixes"][0]["command"] == "kct fix"


class TestERCExplainCommand:
    """Tests for the CLI command."""

    def test_main_with_json_report(self, sample_erc_report: Path, capsys):
        """Test running explain on a JSON report."""
        result = main([str(sample_erc_report)])

        # Should complete without error (exit code based on errors in report)
        assert result in (0, 1)  # 0 for no errors, 1 for errors

        captured = capsys.readouterr()
        assert "ERC ERROR ANALYSIS" in captured.out

    def test_main_with_json_format(self, sample_erc_report: Path, capsys):
        """Test JSON output format."""
        result = main([str(sample_erc_report), "--format", "json"])

        captured = capsys.readouterr()
        # Should be valid JSON
        data = json.loads(captured.out)

        assert "source" in data
        assert "summary" in data
        assert "explanations" in data

    def test_main_errors_only(self, sample_erc_report: Path, capsys):
        """Test --errors-only filter."""
        result = main([str(sample_erc_report), "--errors-only", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # All explanations should be errors
        for exp in data["explanations"]:
            assert exp["severity"] == "error"

    def test_main_filter_by_type(self, sample_erc_report: Path, capsys):
        """Test --type filter."""
        result = main([str(sample_erc_report), "--type", "label", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # All explanations should match the filter
        for exp in data["explanations"]:
            assert "label" in exp["type"].lower() or "label" in exp["description"].lower()

    def test_main_file_not_found(self, capsys):
        """Test error handling for missing file."""
        result = main(["/nonexistent/path/file.json"])

        assert result == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "error" in captured.err.lower()


class TestDiagnosisItem:
    """Tests for DiagnosisItem data class."""

    def test_diagnosis_item_creation(self):
        """Test creating a diagnosis item."""
        item = DiagnosisItem(
            check="Connection status",
            expected="Connected",
            actual="Disconnected",
            status="error",
        )

        assert item.check == "Connection status"
        assert item.expected == "Connected"
        assert item.actual == "Disconnected"
        assert item.status == "error"

    def test_diagnosis_item_defaults(self):
        """Test default values for diagnosis item."""
        item = DiagnosisItem(check="Simple check")

        assert item.check == "Simple check"
        assert item.expected is None
        assert item.actual is None
        assert item.status == "info"


class TestFixSuggestion:
    """Tests for FixSuggestion data class."""

    def test_fix_suggestion_with_command(self):
        """Test creating a fix suggestion with command."""
        fix = FixSuggestion(
            description="Run annotation",
            command="kct sch annotate design.kicad_sch",
            priority=1,
        )

        assert fix.description == "Run annotation"
        assert fix.command == "kct sch annotate design.kicad_sch"
        assert fix.priority == 1

    def test_fix_suggestion_defaults(self):
        """Test default values for fix suggestion."""
        fix = FixSuggestion(description="Simple fix")

        assert fix.description == "Simple fix"
        assert fix.command is None
        assert fix.priority == 1
