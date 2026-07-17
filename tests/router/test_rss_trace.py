"""Tests for the per-phase peak-RSS tracer (issue #4292).

The tracer is the primary diagnostic instrument that attributed the
``kct route`` CLI-tail memory footprint.  These tests lock in its contract:
zero-cost no-op when disabled, monotone peak marks when enabled, and a
platform-correct byte normalisation.
"""

from __future__ import annotations

import io

from kicad_tools.router.rss_trace import (
    RSSTracer,
    child_peak_rss_bytes,
    peak_rss_bytes,
)


def test_peak_rss_bytes_positive():
    """The peak-RSS reading is a positive byte count on every platform."""
    assert peak_rss_bytes() > 0


def test_child_peak_rss_bytes_nonnegative():
    """The child peak-RSS reading is a non-negative byte count.

    It is 0 before any subprocess is reaped, which is the whole point of the
    self-vs-child split (issue #4292): the ``kicad-cli`` footprint lands here,
    not in RUSAGE_SELF.
    """
    assert child_peak_rss_bytes() >= 0


def test_disabled_tracer_is_noop():
    """A disabled tracer prints nothing and records no marks."""
    stream = io.StringIO()
    tracer = RSSTracer(enabled=False, stream=stream)
    assert tracer.mark("a") == 0
    assert tracer.mark("b") == 0
    assert stream.getvalue() == ""
    assert tracer.marks == []


def test_enabled_tracer_records_and_prints():
    """An enabled tracer records each mark and prints one line per mark."""
    stream = io.StringIO()
    tracer = RSSTracer(enabled=True, stream=stream)
    tracer.mark("route-start")
    tracer.mark("post-drc")
    out = stream.getvalue()
    assert "route-start" in out
    assert "post-drc" in out
    assert out.count("[rss]") == 2
    # Every line reports both the self and child figures.
    assert out.count("child=") == 2
    assert "self=" in out
    # First mark is the baseline; the second reports a delta.
    assert "(baseline)" in out
    assert "Δself=" in out
    assert [m[0] for m in tracer.marks] == ["route-start", "post-drc"]


def test_peak_is_monotone_high_water_mark():
    """Recorded peaks never decrease -- getrusage reports a high-water mark."""
    stream = io.StringIO()
    tracer = RSSTracer(enabled=True, stream=stream)
    tracer.mark("a")
    # Allocate a chunk to (potentially) raise the peak, then release it.
    ballast = [b"x" * 1024 for _ in range(50_000)]
    tracer.mark("b")
    del ballast
    tracer.mark("c")
    peaks = [self_peak for _, self_peak, _ in tracer.marks]
    assert peaks == sorted(peaks), "peak RSS marks must be non-decreasing"
    assert tracer.peak == max(peaks)


def test_env_gating(monkeypatch):
    """Absent the KCT_RSS_TRACE env var the tracer defaults to disabled."""
    monkeypatch.delenv("KCT_RSS_TRACE", raising=False)
    assert RSSTracer().enabled is False
    monkeypatch.setenv("KCT_RSS_TRACE", "1")
    assert RSSTracer().enabled is True
