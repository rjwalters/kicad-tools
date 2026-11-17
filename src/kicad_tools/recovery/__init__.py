"""
Intelligent failure recovery for routing and placement operations.

This module provides infrastructure for analyzing failures and generating
actionable resolution strategies. When routing or placement operations fail,
this module transforms opaque error messages into concrete recovery options.

Example::

    from kicad_tools.recovery import (
        FailureAnalysis,
        FailureCause,
        StrategyGenerator,
        StrategyApplicator,
        PatternMatcher,
    )

    # Given a failure analysis from root cause analysis
    analysis = FailureAnalysis(
        root_cause=FailureCause.CONGESTION,
        confidence=0.85,
        failure_location=(45.2, 32.1),
        failure_area=Rectangle(40, 28, 50, 36),
        blocking_elements=[...],
        congestion_score=0.92,
    )

    # Generate resolution strategies
    generator = StrategyGenerator()
    strategies = generator.generate_strategies(pcb, analysis)

    # Apply the best placement strategy
    applicator = StrategyApplicator()
    if strategies and applicator.is_safe_to_apply(strategies[0], pcb):
        result = applicator.apply_strategy(pcb, strategies[0])
        if result.success:
            print(f"Moved {len(result.components_moved)} components")

    # Find matching patterns for better suggestions
    matcher = PatternMatcher()
    patterns = matcher.match_patterns(analysis)

Classes:
    FailureCause: Enum of root causes for routing/placement failures
    FailureAnalysis: Detailed analysis of why an operation failed
    BlockingElement: Something blocking the desired operation
    StrategyType: Types of resolution strategies
    Difficulty: Difficulty/risk level of a strategy
    ResolutionStrategy: A concrete strategy to resolve a failure
    Action: A single action in a strategy
    SideEffect: A potential side effect of a strategy
    StrategyGenerator: Generates resolution strategies from failure analysis
    StrategyApplicator: Applies strategies to modify PCB placement
    ApplicationResult: Result of applying a strategy
    PatternMatcher: Matches failures to known patterns for better suggestions
"""

from .applicator import ApplicationResult, StrategyApplicator
from .patterns import PatternMatcher
from .strategy import StrategyGenerator
from .types import (
    Action,
    BlockingElement,
    Difficulty,
    FailureAnalysis,
    FailureCause,
    PathAttempt,
    Rectangle,
    ResolutionStrategy,
    SideEffect,
    StrategyType,
)

__all__ = [
    # Types
    "FailureCause",
    "FailureAnalysis",
    "BlockingElement",
    "PathAttempt",
    "Rectangle",
    "StrategyType",
    "Difficulty",
    "ResolutionStrategy",
    "Action",
    "SideEffect",
    "ApplicationResult",
    # Classes
    "StrategyGenerator",
    "StrategyApplicator",
    "PatternMatcher",
]
