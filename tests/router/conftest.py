"""Shared fixtures for ``tests/router``."""

from __future__ import annotations

import pytest

from kicad_tools.router import stuck_classifier as sc


@pytest.fixture
def no_routability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the #7832 branch-4 routability probe in the stuck classifier.

    For tests of the *consumers* of a PLACEMENT_BOUND diagnosis (placement
    nudge, placement-delta endpoint alignment, placement feedback) whose
    synthetic boards carry an Edge.Cuts outline: on those open boards the
    probe would find a route and relabel the net BUDGET_STARVED.  The probe
    reports "could not run", so the pre-probe geometric label stands.  The
    probe itself is covered by
    ``test_stuck_classifier.py::TestBranch4RoutabilityProbe``.
    """
    monkeypatch.setattr(sc._RoutabilityProbe, "routable", lambda self, net, pads: None)
