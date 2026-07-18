"""Per-phase peak-RSS tracing for the ``kct route`` CLI tail (issue #4292).

The lattice/mesh negotiation engines stay comfortably under the epic #4267
512 MB rail, yet the full ``kct route`` CLI run was observed to peak at
~1.85 GB on softstart rev-C -- an extra ~1.4 GB materialising *after*
routing, in the post-route tail (cache write, zone fill, internal DRC).

This module provides a zero-cost-by-default tracer that samples the process
peak resident-set-size high-water mark at named phase boundaries and prints
the per-phase delta.  Because the peak is a monotonic high-water mark, a
positive delta at a phase means *that phase raised the peak* -- which is
exactly the attribution needed to pin down which tail consumer owns the
excess footprint, even when the memory is transiently allocated and freed
within the phase.

Crucially the tracer samples **two** figures:

* ``RUSAGE_SELF.ru_maxrss`` -- the *Python* process's own peak, and
* ``RUSAGE_CHILDREN.ru_maxrss`` -- the peak of the largest *subprocess* the
  run has reaped (e.g. ``kicad-cli pcb drc`` / zone-fill).

Splitting the two is the whole point of issue #4292: the observed ~1.85 GB
full-run peak on softstart rev-C is owned almost entirely by the external
``kicad-cli`` DRC/zone-fill child (~1.7 GB on its own), **not** by any
kicad-tools Python allocation -- the Python engine + tail stays around
0.56 GB.  A tracer that only watched ``RUSAGE_SELF`` (as the naive
``/usr/bin/time -l`` reading conflates) would misattribute the footprint to
the cache write / internal DRC Python code.

Enable it by setting the ``KCT_RSS_TRACE`` environment variable to any
non-empty value; output goes to stderr so it never contaminates the routed
board or JSON on stdout.  When disabled every method is a cheap no-op.
"""

from __future__ import annotations

import os
import resource
import sys
from typing import TextIO

__all__ = ["RSSTracer", "peak_rss_bytes", "child_peak_rss_bytes"]


def _maxrss_to_bytes(ru_maxrss: int) -> int:
    """Normalise a raw ``ru_maxrss`` figure to bytes.

    ``ru_maxrss`` is reported in **kilobytes** on Linux but in **bytes** on
    macOS / BSD (issue #4292 was measured on macOS via ``/usr/bin/time -l``).
    """
    if sys.platform == "darwin":
        return int(ru_maxrss)
    # Linux (and other platforms) report kilobytes.
    return int(ru_maxrss) * 1024


def peak_rss_bytes() -> int:
    """Return this (Python) process's peak RSS high-water mark in bytes."""
    return _maxrss_to_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def child_peak_rss_bytes() -> int:
    """Return the peak RSS of the largest reaped child process, in bytes.

    ``RUSAGE_CHILDREN.ru_maxrss`` is the high-water mark of the single
    largest child the process has ``wait()``-ed for.  This is where the
    ``kicad-cli`` DRC / zone-fill footprint shows up -- it is invisible to
    ``RUSAGE_SELF`` because the child is a separate process.
    """
    return _maxrss_to_bytes(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)


class RSSTracer:
    """Sample peak RSS at named phase boundaries and print per-phase deltas.

    A no-op unless enabled (defaults to the ``KCT_RSS_TRACE`` env var).  The
    first :meth:`mark` establishes the baseline; every subsequent mark prints
    the peak, the delta versus the previous mark, and the cumulative delta
    versus the baseline.
    """

    def __init__(self, enabled: bool | None = None, stream: TextIO | None = None) -> None:
        if enabled is None:
            enabled = bool(os.environ.get("KCT_RSS_TRACE"))
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stderr
        self._baseline: int | None = None
        self._last: int | None = None
        self.peak: int = 0
        self.child_peak: int = 0
        # Recorded (label, self_peak_bytes, child_peak_bytes) triples --
        # exposed for tests / callers that want to assert on the phase
        # ordering rather than parse stderr.
        self.marks: list[tuple[str, int, int]] = []

    def mark(self, label: str) -> int:
        """Record the peak RSS (self + children) under ``label``; print deltas.

        Returns this process's current peak RSS in bytes (0 when disabled) so
        callers may use the return value in assertions without re-reading
        getrusage.
        """
        if not self.enabled:
            return 0
        cur = peak_rss_bytes()
        child = child_peak_rss_bytes()
        self.peak = max(self.peak, cur)
        self.child_peak = max(self.child_peak, child)
        self.marks.append((label, cur, child))
        if self._baseline is None:
            self._baseline = cur
            self._last = cur
            print(
                f"[rss] {label:<28} self={cur / 1e6:8.1f} MB  "
                f"child={child / 1e6:8.1f} MB  (baseline)",
                file=self.stream,
                flush=True,
            )
            return cur
        assert self._last is not None
        delta = cur - self._last
        cum = cur - self._baseline
        print(
            f"[rss] {label:<28} self={cur / 1e6:8.1f} MB  "
            f"child={child / 1e6:8.1f} MB  "
            f"Δself={delta / 1e6:+8.1f} MB  cum={cum / 1e6:+8.1f} MB",
            file=self.stream,
            flush=True,
        )
        self._last = cur
        return cur
