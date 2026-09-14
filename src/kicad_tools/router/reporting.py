"""Attempt-local routing reporting contracts, independent of CLI activation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kicad_tools.placement.routing import RoutingPlacementDisposition


@dataclass(frozen=True)
class RouteAttemptResult:
    """Additive alternative to an integer routing callback result.

    Return this from the route invocation itself, including preflight/no-output
    failures. Consumers never discover metadata in sidecars or global state.
    The CLI can continue returning integers until its separate activation work.
    """

    exit_code: int
    placement_disposition: RoutingPlacementDisposition | None = None


@dataclass(frozen=True)
class RoutingPlacementReport:
    """Public named populations; completion is limited to eligible requests.

    ``completed_nets`` comes from connectivity evidence, not exclusion counts.
    Benchmarks pass physical complete names from their measured artifact; router
    diagnostics pass their connectivity-validated routes. Neither counts blocked
    copper as attempted routing. Physical board metrics remain a separate axis.
    """

    disposition: RoutingPlacementDisposition
    completed_nets: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "completed_nets", self.completed_nets & self.disposition.eligible_nets
        )

    def meets_completion(self, minimum: float = 1.0) -> bool:
        """A permissive threshold never hides unresolved requested placement.

        This is a routing-population check only, not a DRC/manufacturing verdict.
        Unavailable analysis preserves the existing permissive routing policy;
        the separate clean_success field still cannot attest to clean placement.
        """
        if not 0 <= minimum <= 1:
            raise ValueError("minimum completion must be between 0 and 1")
        d = self.disposition
        if d.requested_invalid_nets:
            return False
        return not d.requested_nets or len(self.completed_nets) / len(d.requested_nets) >= minimum

    def to_dict(self) -> dict[str, Any]:
        d = self.disposition
        return {
            "check_available": d.check_available,
            "status": "unavailable"
            if not d.check_available
            else "placement_invalid"
            if d.invalid_nets or d.invalid_references
            else "valid",
            "invalid_references": sorted(d.invalid_references),
            "direct_invalid_nets": sorted(d.direct_invalid_nets),
            "coupled_invalid_nets": sorted(d.coupled_invalid_nets),
            "requested_nets": sorted(d.requested_nets),
            "eligible_nets": sorted(d.eligible_nets),
            "completed_nets": sorted(self.completed_nets),
            "requested_blocked_nets": sorted(d.requested_invalid_nets),
            "user_excluded_nets": sorted(d.user_excluded_nets),
            "plane_excluded_nets": sorted(d.plane_excluded_nets),
            "unrequested_nets": sorted(d.unrequested_nets),
            "nets_requested": len(d.requested_nets),
            "nets_eligible": len(d.eligible_nets),
            "nets_completed": len(self.completed_nets),
            "nets_placement_blocked": len(d.requested_invalid_nets),
            "invalid_net_status": "placement-invalid, not attempted",
            "clean_success": d.check_available and self.meets_completion(),
        }
