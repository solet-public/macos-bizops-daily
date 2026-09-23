"""Discovery confidence engine for intelligent result filtering.

This module implements a confidence-based approach to discovery result filtering.
Instead of returning all matches above a threshold, it assesses confidence in
the top match and returns an appropriate number of results:

- HIGH confidence: Strong match, return top 3 for LLM to verify
- MEDIUM confidence: Few good options, return top 3
- LOW confidence: Unclear, return more options or suggest clarification
- AMBIGUOUS: Multiple excellent matches, need user disambiguation
- NONE: No matches above threshold

The confidence assessment considers:
1. Absolute score of top match (semantic similarity)
2. Score gap between top and second match (differentiation)
3. Usage history (learned preferences)
"""

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class DiscoveryConfidence(Enum):
    """Confidence level in discovery results.

    Determines how many results to return and whether clarification is needed.
    """

    HIGH = "high"  # Strong match, return top 3 for LLM verification
    MEDIUM = "medium"  # Few good matches, LLM picks from short list
    LOW = "low"  # Multiple matches, may need clarification
    AMBIGUOUS = "ambiguous"  # Similar high scores, explicit disambiguation needed
    NONE = "none"  # No matches above threshold


@dataclass(frozen=True)
class ConfidenceThresholds:
    """Configurable thresholds for the confidence engine.

    These thresholds are tuned for cosine similarity scores from embedding search.
    Typical embedding similarity ranges:
    - 0.8+ = excellent match (nearly identical semantics)
    - 0.7-0.8 = good match (clearly related)
    - 0.6-0.7 = moderate match (possibly related)
    - <0.6 = weak match (filtered out by MIN_SIMILARITY_THRESHOLD)

    Score gaps indicate differentiation:
    - 0.10+ = very clear winner
    - 0.05-0.10 = clear winner
    - <0.05 = ambiguous, multiple valid options
    """

    # Absolute score thresholds (cosine similarity)
    excellent_score: float = 0.80  # Near-perfect semantic match
    good_score: float = 0.70  # Strong semantic match
    moderate_score: float = 0.50  # Acceptable match (lowered from 0.60 to catch TTS)

    # Score gap thresholds (top score minus second score)
    clear_gap: float = 0.05  # Meaningful differentiation between matches

    # Result limits per confidence level
    # Return multiple results so LLM can pick the best match based on descriptions.
    # Returning only 1 result causes hallucinations when the wrong process is top-ranked.
    high_max_results: int = 10  # Strong match, but give LLM options to verify
    medium_max_results: int = 10  # Good options, let LLM pick from descriptions
    low_max_results: int = 10  # Unclear match, provide alternatives
    ambiguous_max_results: int = 10  # Similar scores, let LLM disambiguate

    # Usage-based disambiguation thresholds
    min_usage_for_boost: int = 5  # Minimum executions to consider usage
    usage_dominance_factor: int = 5  # N times more usage = clear winner
    usage_dominance_absolute: int = 50  # Absolute usage gap = clear winner


@dataclass
class ConfidenceAssessment:
    """Result of confidence assessment.

    Provides the confidence level, recommended result count, and reasoning
    for logging and debugging.
    """

    confidence: DiscoveryConfidence
    top_score: float
    score_gap: float | None  # None if only one match
    recommended_results: int
    reasoning: str
    usage_influenced: bool = False

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for logging/serialization."""
        return {
            "confidence": self.confidence.value,
            "top_score": round(self.top_score, 4),
            "score_gap": round(self.score_gap, 4) if self.score_gap else None,
            "recommended_results": self.recommended_results,
            "reasoning": self.reasoning,
            "usage_influenced": self.usage_influenced,
        }


@dataclass
class ProcessScore:
    """Simplified process match for confidence assessment."""

    process_key: str
    score: float


class DiscoveryConfidenceEngine:
    """Assess confidence in discovery results and determine result filtering.

    The confidence engine applies a multi-factor assessment:

    1. Single match: Confidence based purely on absolute score
    2. Multiple matches with clear gap: HIGH confidence if top is good
    3. Multiple matches with small gap: AMBIGUOUS if scores are high
    4. Usage history can boost confidence when scores are close

    Usage:
        engine = DiscoveryConfidenceEngine()
        assessment = engine.assess(matches, usage_stats)
        filtered = matches[:assessment.recommended_results]
    """

    def __init__(self, thresholds: ConfidenceThresholds | None = None) -> None:
        """Initialize with optional custom thresholds."""
        self.thresholds = thresholds or ConfidenceThresholds()

    def assess(
        self,
        matches: list[ProcessScore],
        usage_stats: dict[str, int] | None = None,
    ) -> ConfidenceAssessment:
        """Assess confidence in discovery results.

        Args:
            matches: List of ProcessScore objects, need not be sorted
            usage_stats: Optional dict of process_key -> execution_count

        Returns:
            ConfidenceAssessment with confidence level and recommended result count
        """
        if not matches:
            return ConfidenceAssessment(
                confidence=DiscoveryConfidence.NONE,
                top_score=0.0,
                score_gap=None,
                recommended_results=0,
                reasoning="No matches found above similarity threshold",
            )

        # Sort by score descending
        sorted_matches = sorted(matches, key=lambda x: x.score, reverse=True)
        top = sorted_matches[0]
        top_score = top.score

        # Single match case
        if len(sorted_matches) == 1:
            return self._assess_single_match(top_score)

        # Multiple matches - calculate gap and assess
        second_score = sorted_matches[1].score
        score_gap = top_score - second_score

        # Check for usage-based winner
        usage_winner = self._check_usage_winner(sorted_matches, usage_stats)

        return self._assess_multiple_matches(
            top_score=top_score,
            score_gap=score_gap,
            usage_winner=usage_winner,
            match_count=len(sorted_matches),
        )

    def _assess_single_match(self, score: float) -> ConfidenceAssessment:
        """Assess confidence when there's only one match."""
        t = self.thresholds

        if score >= t.excellent_score:
            confidence = DiscoveryConfidence.HIGH
            reasoning = f"Single excellent match (score={score:.3f})"
        elif score >= t.good_score:
            confidence = DiscoveryConfidence.HIGH
            reasoning = f"Single good match (score={score:.3f})"
        elif score >= t.moderate_score:
            confidence = DiscoveryConfidence.MEDIUM
            reasoning = f"Single moderate match (score={score:.3f})"
        else:
            confidence = DiscoveryConfidence.LOW
            reasoning = f"Single weak match (score={score:.3f})"

        return ConfidenceAssessment(
            confidence=confidence,
            top_score=score,
            score_gap=None,
            recommended_results=1,  # Only one match available
            reasoning=reasoning,
        )

    def _assess_multiple_matches(
        self,
        top_score: float,
        score_gap: float,
        usage_winner: bool,
        match_count: int,
    ) -> ConfidenceAssessment:
        """Assess confidence when there are multiple matches."""
        # Determine confidence level
        confidence, reasoning, usage_influenced = self._determine_confidence(
            top_score, score_gap, usage_winner
        )

        # Get recommended result count based on confidence
        recommended = self._get_result_count(confidence, match_count)

        return ConfidenceAssessment(
            confidence=confidence,
            top_score=top_score,
            score_gap=score_gap,
            recommended_results=recommended,
            reasoning=reasoning,
            usage_influenced=usage_influenced,
        )

    def _determine_confidence(
        self,
        top_score: float,
        score_gap: float,
        usage_winner: bool,
    ) -> tuple[DiscoveryConfidence, str, bool]:
        """Determine confidence level from score and gap.

        Returns:
            Tuple of (confidence, reasoning, usage_influenced)
        """
        # Try each confidence rule in priority order
        result = self._check_high_confidence(top_score, score_gap, usage_winner)
        if result:
            return result

        result = self._check_ambiguous_confidence(top_score, score_gap)
        if result:
            return result

        result = self._check_medium_confidence(top_score, score_gap)
        if result:
            return result

        result = self._check_low_confidence(top_score, score_gap)
        if result:
            return result

        return (
            DiscoveryConfidence.NONE,
            f"Score ({top_score:.3f}) below acceptable threshold",
            False,
        )

    def _check_high_confidence(
        self, top_score: float, score_gap: float, usage_winner: bool
    ) -> tuple[DiscoveryConfidence, str, bool] | None:
        """Check for HIGH confidence conditions."""
        t = self.thresholds

        # Excellent score with clear gap
        if top_score >= t.excellent_score and score_gap >= t.clear_gap:
            return (
                DiscoveryConfidence.HIGH,
                f"Excellent match ({top_score:.3f}) with clear gap ({score_gap:.3f})",
                False,
            )

        # Good score with clear gap
        if top_score >= t.good_score and score_gap >= t.clear_gap:
            return (
                DiscoveryConfidence.HIGH,
                f"Good match ({top_score:.3f}) with clear gap ({score_gap:.3f})",
                False,
            )

        # Usage winner with good score
        if usage_winner and top_score >= t.good_score:
            return (
                DiscoveryConfidence.HIGH,
                f"Usage-based winner ({top_score:.3f}) with dominant history",
                True,
            )

        return None

    def _check_ambiguous_confidence(
        self, top_score: float, score_gap: float
    ) -> tuple[DiscoveryConfidence, str, bool] | None:
        """Check for AMBIGUOUS confidence conditions."""
        t = self.thresholds

        # Excellent scores but small gap
        if top_score >= t.excellent_score and score_gap < t.clear_gap:
            return (
                DiscoveryConfidence.AMBIGUOUS,
                f"Multiple excellent matches ({top_score:.3f}), small gap ({score_gap:.3f})",
                False,
            )

        # Good scores but small gap
        if top_score >= t.good_score and score_gap < t.clear_gap:
            return (
                DiscoveryConfidence.AMBIGUOUS,
                f"Multiple good matches ({top_score:.3f}), small gap ({score_gap:.3f})",
                False,
            )

        return None

    def _check_medium_confidence(
        self, top_score: float, score_gap: float
    ) -> tuple[DiscoveryConfidence, str, bool] | None:
        """Check for MEDIUM confidence conditions."""
        t = self.thresholds

        if top_score >= t.moderate_score and score_gap >= t.clear_gap:
            return (
                DiscoveryConfidence.MEDIUM,
                f"Moderate match ({top_score:.3f}) with gap ({score_gap:.3f})",
                False,
            )

        return None

    def _check_low_confidence(
        self, top_score: float, score_gap: float
    ) -> tuple[DiscoveryConfidence, str, bool] | None:
        """Check for LOW confidence conditions."""
        t = self.thresholds

        if top_score >= t.moderate_score:
            return (
                DiscoveryConfidence.LOW,
                f"Moderate match ({top_score:.3f}), no clear winner (gap={score_gap:.3f})",
                False,
            )

        return None

    def _check_usage_winner(
        self,
        matches: list[ProcessScore],
        usage_stats: dict[str, int] | None,
    ) -> bool:
        """Check if top match is a clear winner by usage history.

        A process is a usage winner if:
        1. It has at least min_usage_for_boost executions
        2. AND either:
           - Has usage_dominance_factor times more usage than second
           - OR has usage_dominance_absolute more executions than second
        """
        if not usage_stats or len(matches) < 2:
            return False

        t = self.thresholds
        top_key = matches[0].process_key
        second_key = matches[1].process_key

        top_usage = usage_stats.get(top_key, 0)
        second_usage = usage_stats.get(second_key, 0)

        # Must have minimum usage to be considered
        if top_usage < t.min_usage_for_boost:
            return False

        # Check for dominance (5x more usage OR 50+ more executions)
        if top_usage >= second_usage * t.usage_dominance_factor:
            return True

        if top_usage - second_usage >= t.usage_dominance_absolute:
            return True

        return False

    def _get_result_count(
        self,
        confidence: DiscoveryConfidence,
        available: int,
    ) -> int:
        """Get recommended result count based on confidence level."""
        t = self.thresholds

        limits = {
            DiscoveryConfidence.HIGH: t.high_max_results,
            DiscoveryConfidence.MEDIUM: t.medium_max_results,
            DiscoveryConfidence.LOW: t.low_max_results,
            DiscoveryConfidence.AMBIGUOUS: t.ambiguous_max_results,
            DiscoveryConfidence.NONE: 0,
        }

        max_for_confidence = limits.get(confidence, t.low_max_results)
        return min(max_for_confidence, available)
