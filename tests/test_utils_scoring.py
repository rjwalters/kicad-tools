"""
Tests for the unified confidence scoring framework.
"""

import pytest

from kicad_tools.utils.scoring import (
    ConfidenceLevel,
    MatchResult,
    adjust_confidence,
    calculate_string_confidence,
    combine_confidences,
)


class TestConfidenceLevel:
    """Tests for ConfidenceLevel enum."""

    def test_from_score_exact(self):
        """Test EXACT level for scores >= 0.95."""
        assert ConfidenceLevel.from_score(1.0) == ConfidenceLevel.EXACT
        assert ConfidenceLevel.from_score(0.95) == ConfidenceLevel.EXACT
        assert ConfidenceLevel.from_score(0.99) == ConfidenceLevel.EXACT

    def test_from_score_high(self):
        """Test HIGH level for scores 0.8-0.94."""
        assert ConfidenceLevel.from_score(0.9) == ConfidenceLevel.HIGH
        assert ConfidenceLevel.from_score(0.8) == ConfidenceLevel.HIGH
        assert ConfidenceLevel.from_score(0.85) == ConfidenceLevel.HIGH

    def test_from_score_medium(self):
        """Test MEDIUM level for scores 0.6-0.79."""
        assert ConfidenceLevel.from_score(0.7) == ConfidenceLevel.MEDIUM
        assert ConfidenceLevel.from_score(0.6) == ConfidenceLevel.MEDIUM
        assert ConfidenceLevel.from_score(0.75) == ConfidenceLevel.MEDIUM

    def test_from_score_low(self):
        """Test LOW level for scores 0.4-0.59."""
        assert ConfidenceLevel.from_score(0.5) == ConfidenceLevel.LOW
        assert ConfidenceLevel.from_score(0.4) == ConfidenceLevel.LOW
        assert ConfidenceLevel.from_score(0.55) == ConfidenceLevel.LOW

    def test_from_score_very_low(self):
        """Test VERY_LOW level for scores 0.2-0.39."""
        assert ConfidenceLevel.from_score(0.3) == ConfidenceLevel.VERY_LOW
        assert ConfidenceLevel.from_score(0.2) == ConfidenceLevel.VERY_LOW
        assert ConfidenceLevel.from_score(0.35) == ConfidenceLevel.VERY_LOW

    def test_from_score_none(self):
        """Test NONE level for scores < 0.2."""
        assert ConfidenceLevel.from_score(0.0) == ConfidenceLevel.NONE
        assert ConfidenceLevel.from_score(0.1) == ConfidenceLevel.NONE
        assert ConfidenceLevel.from_score(0.19) == ConfidenceLevel.NONE


class TestMatchResult:
    """Tests for MatchResult dataclass."""

    def test_creation(self):
        """Test basic creation."""
        result = MatchResult(confidence=0.9, match_type="exact", matched_value="test")
        assert result.confidence == 0.9
        assert result.match_type == "exact"
        assert result.matched_value == "test"

    def test_level_property(self):
        """Test level property returns correct ConfidenceLevel."""
        result = MatchResult(confidence=0.95)
        assert result.level == ConfidenceLevel.EXACT

        result = MatchResult(confidence=0.5)
        assert result.level == ConfidenceLevel.LOW

    def test_confidence_clamped_high(self):
        """Test that confidence > 1.0 is clamped."""
        result = MatchResult(confidence=1.5)
        assert result.confidence == 1.0

    def test_confidence_clamped_low(self):
        """Test that confidence < 0.0 is clamped."""
        result = MatchResult(confidence=-0.5)
        assert result.confidence == 0.0


class TestCalculateStringConfidence:
    """Tests for calculate_string_confidence function."""

    def test_exact_match(self):
        """Test exact match returns highest confidence."""
        result = calculate_string_confidence("STM32F103", "STM32F103")
        assert result.confidence == 1.0
        assert result.match_type == "exact"

    def test_exact_match_case_insensitive(self):
        """Test case-insensitive exact match."""
        result = calculate_string_confidence("stm32f103", "STM32F103")
        assert result.confidence == 1.0
        assert result.match_type == "exact"

    def test_exact_match_case_sensitive(self):
        """Test case-sensitive mode - different cases don't match."""
        result = calculate_string_confidence("stm32f103", "STM32F103", case_sensitive=True)
        assert result.confidence == 0.7  # no match in case-sensitive mode
        assert result.match_type == "search"

    def test_substring_query_in_target(self):
        """Test when query is substring of target."""
        result = calculate_string_confidence("STM32", "STM32F103C8T6")
        assert result.confidence == 0.9
        assert result.match_type == "substring"

    def test_substring_target_in_query(self):
        """Test when target is substring of query."""
        result = calculate_string_confidence("STM32F103C8T6-FULL", "STM32F103C8T6")
        assert result.confidence == 0.9
        assert result.match_type == "substring"

    def test_no_match(self):
        """Test when there's no direct match."""
        result = calculate_string_confidence("STM32", "ATMEGA328P")
        assert result.confidence == 0.7
        assert result.match_type == "search"

    def test_empty_query(self):
        """Test with empty query - empty is substring of any string."""
        result = calculate_string_confidence("", "STM32F103")
        assert result.confidence == 0.9
        assert result.match_type == "substring"

    def test_empty_target(self):
        """Test with empty target - empty is substring of any string."""
        result = calculate_string_confidence("STM32F103", "")
        assert result.confidence == 0.9
        assert result.match_type == "substring"

    def test_both_empty(self):
        """Test with both empty strings - exact match."""
        result = calculate_string_confidence("", "")
        assert result.confidence == 1.0
        assert result.match_type == "exact"

    def test_custom_scores(self):
        """Test with custom confidence scores."""
        result = calculate_string_confidence(
            "STM32",
            "ATMEGA328P",
            exact_score=0.99,
            substring_score=0.85,
            no_match_score=0.5,
        )
        assert result.confidence == 0.5


class TestCombineConfidences:
    """Tests for combine_confidences function."""

    def test_weighted_average_equal_weights(self):
        """Test weighted average with equal weights (default)."""
        result = combine_confidences(0.9, 0.8, 0.7)
        assert result == pytest.approx(0.8, abs=0.001)

    def test_weighted_average_custom_weights(self):
        """Test weighted average with custom weights."""
        result = combine_confidences(0.9, 0.8, 0.7, weights=[0.5, 0.3, 0.2])
        expected = (0.9 * 0.5 + 0.8 * 0.3 + 0.7 * 0.2) / 1.0
        assert result == pytest.approx(expected, abs=0.001)

    def test_min_method(self):
        """Test minimum method."""
        result = combine_confidences(0.9, 0.8, 0.7, method="min")
        assert result == 0.7

    def test_max_method(self):
        """Test maximum method."""
        result = combine_confidences(0.9, 0.8, 0.7, method="max")
        assert result == 0.9

    def test_product_method(self):
        """Test product method."""
        result = combine_confidences(0.9, 0.8, method="product")
        assert result == pytest.approx(0.72, abs=0.001)

    def test_empty_scores(self):
        """Test with no scores."""
        result = combine_confidences()
        assert result == 0.0

    def test_single_score(self):
        """Test with single score."""
        result = combine_confidences(0.85)
        assert result == 0.85

    def test_invalid_method(self):
        """Test with invalid method raises ValueError."""
        with pytest.raises(ValueError, match="Unknown combination method"):
            combine_confidences(0.9, 0.8, method="invalid")

    def test_weights_mismatch(self):
        """Test mismatched weights count raises ValueError."""
        with pytest.raises(ValueError, match="Number of weights must match"):
            combine_confidences(0.9, 0.8, weights=[0.5])

    def test_result_clamped(self):
        """Test result is clamped to [0, 1]."""
        # Product of values < 1 is always <= 1, so test max method
        result = combine_confidences(0.9, 0.9, 0.9, method="max")
        assert result <= 1.0
        assert result >= 0.0


class TestAdjustConfidence:
    """Tests for adjust_confidence function."""

    def test_no_adjustment(self):
        """Test with no adjustments returns base value."""
        result = adjust_confidence(0.5)
        assert result == 0.5

    def test_bonus(self):
        """Test additive bonus."""
        result = adjust_confidence(0.5, bonus=0.2)
        assert result == 0.7

    def test_penalty(self):
        """Test subtractive penalty."""
        result = adjust_confidence(0.8, penalty=0.3)
        assert result == 0.5

    def test_multiplier(self):
        """Test multiplicative factor."""
        result = adjust_confidence(0.8, multiplier=0.5)
        assert result == 0.4

    def test_combined_adjustments(self):
        """Test multiple adjustments together."""
        # base * multiplier + bonus - penalty
        # 0.8 * 0.5 + 0.1 - 0.05 = 0.45
        result = adjust_confidence(0.8, multiplier=0.5, bonus=0.1, penalty=0.05)
        assert result == pytest.approx(0.45, abs=0.001)

    def test_clamped_to_maximum(self):
        """Test result is clamped to maximum."""
        result = adjust_confidence(0.8, bonus=0.5)
        assert result == 1.0

    def test_clamped_to_minimum(self):
        """Test result is clamped to minimum."""
        result = adjust_confidence(0.3, penalty=0.5)
        assert result == 0.0

    def test_custom_minimum(self):
        """Test custom minimum value."""
        result = adjust_confidence(0.3, penalty=0.5, minimum=0.1)
        assert result == 0.1

    def test_custom_maximum(self):
        """Test custom maximum value."""
        result = adjust_confidence(0.8, bonus=0.5, maximum=0.9)
        assert result == 0.9
