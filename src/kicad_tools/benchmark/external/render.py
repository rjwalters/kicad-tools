"""Markdown rendering of external-benchmark reports (issue #4934).

The JSON contract in :mod:`kicad_tools.benchmark.external.metrics` is the
machine-readable source of truth; this module renders the same data as a
per-board table for human review (and, later, for the kicad-tools.org
results section, Epic #4932 Phase 3).

Rendering rules that keep the published table honest:

* A refused timing renders as ``refused`` with the reason in a footnote --
  never as a blank cell that a reader could mistake for "fast".
* A DRC engine that did not run renders as ``not run``, never ``0``.
* Both DRC engines get their own column, because the process rule is that
  ``kct check`` alone is not sufficient evidence of a clean board.
"""

from __future__ import annotations

from collections.abc import Sequence

from .metrics import BenchmarkReport

__all__ = ["render_markdown", "render_report_markdown"]


def _fmt_timing(report: BenchmarkReport) -> str:
    if report.timing.valid and report.timing.wall_clock_s is not None:
        return f"{report.timing.wall_clock_s:.1f} s"
    return "refused"


def _fmt_kct_check(report: BenchmarkReport) -> str:
    summary = report.kct_check
    if not summary.ran:
        return "not run"
    errors = summary.error_count or 0
    warnings = summary.warning_count or 0
    verdict = "PASS" if summary.passed else "FAIL"
    return f"{verdict} ({errors}E / {warnings}W)"


def _fmt_cli_drc(report: BenchmarkReport) -> str:
    summary = report.kicad_cli_drc
    if not summary.ran or summary.violation_count is None:
        return "not run"
    return str(summary.violation_count)


def _fmt_diff_pairs(report: BenchmarkReport) -> str:
    pairs = report.diff_pairs
    if pairs is None:
        return "n/a"
    return f"{pairs.pairs_complete}/{pairs.pairs_total}"


def _fmt_backend(report: BenchmarkReport) -> str:
    backend = report.backend
    return backend.backend if backend.available else f"{backend.backend} (unavailable)"


def render_markdown(
    reports: Sequence[BenchmarkReport],
    *,
    title: str = "External autorouter benchmark results",
) -> str:
    """Render one markdown table covering ``reports`` (one row per board).

    Returns a document with the table, the environment-validity footnotes
    for any refused timing, and every report's free-form notes.
    """
    lines: list[str] = [f"# {title}", ""]

    if not reports:
        lines.append("_No benchmark reports._")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "| Board | Protocol | Completion | Connections | Vias | Wirelength (mm) "
            "| Runtime | Backend | kct check | kicad-cli DRC | Diff pairs |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )

    for report in reports:
        completion = report.completion
        cells = [
            report.board_id,
            report.protocol,
            f"{completion.completion_pct:.1f}%",
            f"{completion.connections_routed} of {completion.connections_total}",
            str(report.copper.via_count),
            f"{report.copper.wirelength_mm:.2f}",
            _fmt_timing(report),
            _fmt_backend(report),
            _fmt_kct_check(report),
            _fmt_cli_drc(report),
            _fmt_diff_pairs(report),
        ]
        lines.append("| " + " | ".join(cells) + " |")

    refusals = [r for r in reports if not r.timing.valid]
    if refusals:
        lines.extend(["", "**Timing refusals**", ""])
        for report in refusals:
            reason = report.timing.refusal_reason or "no reason recorded"
            lines.append(f"- `{report.board_id}` ({report.protocol}): {reason}")

    skipped_drc = [r for r in reports if not r.kicad_cli_drc.ran]
    if skipped_drc:
        lines.extend(["", "**`kicad-cli pcb drc --refill-zones` did not run**", ""])
        for report in skipped_drc:
            note = report.kicad_cli_drc.note or "no reason recorded"
            lines.append(
                f"- `{report.board_id}` ({report.protocol}): {note} "
                "— this board's DRC status is UNKNOWN, not clean."
            )

    annotated = [r for r in reports if r.notes]
    if annotated:
        lines.extend(["", "**Notes**", ""])
        for report in annotated:
            for note in report.notes:
                lines.append(f"- `{report.board_id}` ({report.protocol}): {note}")

    lines.extend(["", "**Reproduction**", ""])
    for report in reports:
        commit = report.board_commit or "unpinned"
        source = report.board_source or "source not recorded"
        lines.append(
            f"- `{report.board_id}`: {source} @ `{commit}` "
            f"(kicad-tools `{report.tool_commit}`, protocol `{report.protocol}`)"
        )

    return "\n".join(lines) + "\n"


def render_report_markdown(report: BenchmarkReport) -> str:
    """Render a single report as a one-row table (convenience wrapper)."""
    return render_markdown([report], title=f"Benchmark: {report.board_id}")
