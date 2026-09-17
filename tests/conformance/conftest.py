"""Shared gating for the conformance suite.

Two things happen here, and both are load-bearing for the epic's "report-only"
guarantee.

**kicad-cli gating is a skip, never a pass.**  A test that needs ground truth
cannot conclude anything without it, so it skips with a visible reason rather
than trivially passing.  Same pattern as ``tests/test_kicad_cli_roundtrip.py``,
exported here as :data:`requires_kicad_cli` so each module opts in explicitly.
Modules that genuinely do not need the tool -- the canonicaliser unit test
against a checked-in report, the table renderer, the determinism check -- run
everywhere, including on a laptop with no KiCad installed.

**Consumer rows are automatically ``xfail(strict=False)``.**  Every
adapter-vs-truth test carries ``@pytest.mark.consumer``; the collection hook
below attaches a non-strict ``xfail`` to each of them.  That is the mechanism,
not a convention -- a future adapter test cannot accidentally turn a *measured*
disagreement into a red build before its consumer has been switched to the
shared kernel in its own epic phase.  kicad-cli-truth assertions carry no such
marker and are hard failures.
"""

from __future__ import annotations

import pytest

from tests.conformance.oracle import kicad_cli_available

requires_kicad_cli = pytest.mark.skipif(not kicad_cli_available(), reason="kicad-cli not installed")
"""Module-level gate for tests that need ``kicad-cli`` as ground truth."""

CONSUMER_XFAIL_REASON = "consumer verdict is report-only until that consumer's epic phase (#5509)"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "consumer: an adapter-vs-kicad-cli comparison; report-only, auto-xfail",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Attach a non-strict ``xfail`` to every ``@pytest.mark.consumer`` item."""
    del config
    for item in items:
        if item.get_closest_marker("consumer") is None:
            continue
        item.add_marker(pytest.mark.xfail(strict=False, reason=CONSUMER_XFAIL_REASON))
