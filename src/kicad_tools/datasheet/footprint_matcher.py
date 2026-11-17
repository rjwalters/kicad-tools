"""
Footprint matching for extracted package information.

Matches extracted package info to KiCad standard library footprints
and provides generator parameter suggestions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.utils.scoring import adjust_confidence

from .package import PackageInfo


@dataclass
class FootprintMatch:
    """
    A matched KiCad footprint.

    Attributes:
        library: Library name (e.g., "Package_QFP")
        footprint: Footprint name (e.g., "LQFP-48_7x7mm_P0.5mm")
        confidence: Match confidence score (0-1)
        dimension_match: Dictionary of which dimensions matched
    """

    library: str
    footprint: str
    confidence: float
    dimension_match: dict[str, bool] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        """Get full footprint reference (library:footprint)."""
        return f"{self.library}:{self.footprint}"

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "library": self.library,
            "footprint": self.footprint,
            "full_name": self.full_name,
            "confidence": self.confidence,
            "dimension_match": self.dimension_match,
        }


@dataclass
class GeneratorSuggestion:
    """
    Suggestion for parametric footprint generation.

    Attributes:
        generator: Generator type (e.g., "qfp", "soic", "qfn")
        params: Parameters for the generator
        confidence: Confidence in the suggestion
        command: CLI command to generate the footprint
    """

    generator: str
    params: dict
    confidence: float
    command: str = ""

    def __post_init__(self):
        """Generate CLI command if not provided."""
        if not self.command:
            self.command = self._generate_command()

    def _generate_command(self) -> str:
        """Generate CLI command for footprint generation."""
        parts = ["kct", "lib", "generate-footprint", "my.pretty", self.generator]

        for key, value in self.params.items():
            if value is not None:
                # Convert underscore to hyphen for CLI args
                arg_name = key.replace("_", "-")
                parts.append(f"--{arg_name}")
                parts.append(str(value))

        return " ".join(parts)

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "generator": self.generator,
            "params": self.params,
            "confidence": self.confidence,
            "command": self.command,
        }


# KiCad standard library mappings
KICAD_LIBRARY_MAP = {
    "qfp": "Package_QFP",
    "qfn": "Package_DFN_QFN",
    "dfn": "Package_DFN_QFN",
    "soic": "Package_SO",
    "sop": "Package_SO",
    "ssop": "Package_SO",
    "tssop": "Package_SO",
    "msop": "Package_SO",
    "dip": "Package_DIP",
    "bga": "Package_BGA",
    "sot": "Package_TO_SOT_SMD",
    "to": "Package_TO_SOT_THT",
    "plcc": "Package_LCC",
    "lga": "Package_LGA",
}


class FootprintMatcher:
    """
    Matches package information to KiCad footprints.

    Uses heuristics and pattern matching to find compatible footprints
    in the KiCad standard library or suggest generator parameters.
    """

    def __init__(
        self,
        kicad_footprint_path: str | Path | None = None,
    ) -> None:
        """
        Initialize the matcher.

        Args:
            kicad_footprint_path: Optional path to KiCad footprint libraries.
                                  If not provided, uses heuristic matching only.
        """
        self.kicad_path = Path(kicad_footprint_path) if kicad_footprint_path else None
        self._footprint_cache: dict[str, list[str]] | None = None

    def find_matches(
        self,
        package: PackageInfo,
        max_results: int = 5,
    ) -> list[FootprintMatch]:
        """
        Find matching KiCad footprints for a package.

        Args:
            package: PackageInfo to match
            max_results: Maximum number of matches to return

        Returns:
            List of FootprintMatch objects sorted by confidence
        """
        matches: list[FootprintMatch] = []

        # Generate expected footprint patterns
        patterns = self._generate_footprint_patterns(package)

        # Get library name
        library = KICAD_LIBRARY_MAP.get(package.type.lower(), "Package_QFP")

        for pattern, base_confidence in patterns:
            dimension_match = self._check_dimension_match(pattern, package)
            confidence = self._calculate_confidence(base_confidence, dimension_match)

            matches.append(
                FootprintMatch(
                    library=library,
                    footprint=pattern,
                    confidence=confidence,
                    dimension_match=dimension_match,
                )
            )

        # Sort by confidence and limit results
        matches.sort(key=lambda m: m.confidence, reverse=True)
        return matches[:max_results]

    def suggest_generator(self, package: PackageInfo) -> GeneratorSuggestion:
        """
        Suggest parametric generator parameters for a package.

        Args:
            package: PackageInfo to generate parameters for

        Returns:
            GeneratorSuggestion with recommended parameters
        """
        pkg_type = package.type.lower()

        # Build parameter dictionary
        params: dict = {}

        # Common parameters
        params["pins"] = package.pin_count
        params["pitch"] = package.pitch

        # Body size
        if package.body_width == package.body_length:
            params["body_size"] = package.body_width
        else:
            params["body_width"] = package.body_width
            params["body_length"] = package.body_length

        # Exposed pad if present
        if package.exposed_pad:
            params["ep_width"] = package.exposed_pad[0]
            params["ep_length"] = package.exposed_pad[1]

        # Calculate confidence based on available information
        pitch_bonus = 0.2 if package.pitch > 0 else 0.0
        body_bonus = 0.2 if package.body_width > 0 and package.body_length > 0 else 0.0
        pkg_confidence_bonus = 0.1 if package.confidence > 0.7 else 0.0

        confidence = adjust_confidence(
            0.5,  # Base confidence
            bonus=pitch_bonus + body_bonus + pkg_confidence_bonus,
        )

        return GeneratorSuggestion(
            generator=pkg_type,
            params=params,
            confidence=confidence,
        )

    def _generate_footprint_patterns(
        self,
        package: PackageInfo,
    ) -> list[tuple[str, float]]:
        """
        Generate expected footprint name patterns.

        Returns list of (pattern, base_confidence) tuples.
        """
        patterns: list[tuple[str, float]] = []

        pkg_type = package.type.upper()
        name = package.name.upper()
        pins = package.pin_count
        width = package.body_width
        length = package.body_length
        pitch = package.pitch

        # Format body size
        if width == length:
            body_str = f"{width}x{width}mm"
        else:
            body_str = f"{width}x{length}mm"

        # Format pitch
        pitch_str = f"P{pitch}mm"

        # Standard KiCad naming patterns
        if pkg_type in ("QFP", "LQFP", "TQFP"):
            # LQFP-48_7x7mm_P0.5mm
            patterns.append(
                (f"{name.split('-')[0].split('_')[0]}-{pins}_{body_str}_{pitch_str}", 0.9)
            )
            # With exposed pad variant
            ep_size = min(width, length) * 0.5  # Estimate EP size
            patterns.append(
                (
                    f"{name.split('-')[0].split('_')[0]}-{pins}_{body_str}_{pitch_str}_EP{ep_size:.1f}x{ep_size:.1f}mm",
                    0.75,
                )
            )

        elif pkg_type in ("QFN", "DFN"):
            # QFN-32-1EP_5x5mm_P0.5mm
            patterns.append((f"{pkg_type}-{pins}-1EP_{body_str}_{pitch_str}", 0.85))
            patterns.append((f"{pkg_type}-{pins}_{body_str}_{pitch_str}", 0.8))

        elif pkg_type in ("SOIC", "SOP"):
            # SOIC-8_3.9x4.9mm_P1.27mm
            patterns.append((f"SOIC-{pins}_{body_str}_{pitch_str}", 0.9))
            patterns.append((f"SO-{pins}_{body_str}_{pitch_str}", 0.75))

        elif pkg_type in ("SSOP", "TSSOP", "MSOP"):
            patterns.append((f"{pkg_type}-{pins}_{body_str}_{pitch_str}", 0.9))

        elif pkg_type == "DIP":
            # DIP-8_W7.62mm
            patterns.append((f"DIP-{pins}_W{width:.2f}mm", 0.9))
            patterns.append((f"DIP-{pins}_W{width:.2f}mm_Socket", 0.7))

        elif pkg_type == "BGA":
            # BGA-100_10x10mm_P0.8mm
            patterns.append((f"BGA-{pins}_{body_str}_{pitch_str}", 0.85))

        elif "SOT" in pkg_type:
            # SOT-23
            patterns.append((pkg_type, 0.9))
            patterns.append((f"{pkg_type}-{pins}", 0.85))

        # Fallback: generic pattern
        if not patterns:
            patterns.append((f"{pkg_type}-{pins}_{body_str}_{pitch_str}", 0.5))

        return patterns

    def _check_dimension_match(
        self,
        pattern: str,
        package: PackageInfo,
    ) -> dict[str, bool]:
        """Check which dimensions match the pattern."""
        match_info = {
            "pin_count": False,
            "body_size": False,
            "pitch": False,
            "type": False,
        }

        pattern_upper = pattern.upper()

        # Check pin count
        pin_match = re.search(r"-(\d+)", pattern)
        if pin_match and int(pin_match.group(1)) == package.pin_count:
            match_info["pin_count"] = True

        # Check body size
        size_match = re.search(r"(\d+\.?\d*)[xX](\d+\.?\d*)mm", pattern)
        if size_match:
            pw = float(size_match.group(1))
            pl = float(size_match.group(2))
            if abs(pw - package.body_width) < 0.5 and abs(pl - package.body_length) < 0.5:
                match_info["body_size"] = True

        # Check pitch
        pitch_match = re.search(r"P(\d+\.?\d*)mm", pattern)
        if pitch_match:
            pp = float(pitch_match.group(1))
            if abs(pp - package.pitch) < 0.05:
                match_info["pitch"] = True

        # Check type
        for pkg_type in (
            "LQFP",
            "TQFP",
            "QFP",
            "QFN",
            "DFN",
            "SOIC",
            "SSOP",
            "TSSOP",
            "DIP",
            "BGA",
            "SOT",
        ):
            if pkg_type in pattern_upper and pkg_type.lower() in package.type.lower():
                match_info["type"] = True
                break

        return match_info

    def _calculate_confidence(
        self,
        base_confidence: float,
        dimension_match: dict[str, bool],
    ) -> float:
        """Calculate final confidence score using unified scoring."""
        # Pin count mismatch is a major penalty
        pin_multiplier = 1.0 if dimension_match.get("pin_count", False) else 0.5

        # Calculate bonuses for other dimension matches
        bonus = 0.0
        if dimension_match.get("body_size", False):
            bonus += 0.05
        if dimension_match.get("pitch", False):
            bonus += 0.03
        if dimension_match.get("type", False):
            bonus += 0.02

        return adjust_confidence(
            base_confidence,
            multiplier=pin_multiplier,
            bonus=bonus,
        )

    def get_all_suggestions(
        self,
        package: PackageInfo,
    ) -> dict:
        """
        Get both matches and generator suggestions for a package.

        Args:
            package: PackageInfo to analyze

        Returns:
            Dictionary with 'matches' and 'suggestion' keys
        """
        matches = self.find_matches(package)
        suggestion = self.suggest_generator(package)

        return {
            "matches": matches,
            "suggestion": suggestion,
            "best_match": matches[0] if matches else None,
        }
