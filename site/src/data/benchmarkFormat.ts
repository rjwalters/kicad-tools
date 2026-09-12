/**
 * Cell-formatting helpers for rendering a `BenchmarkReport` as a table row.
 *
 * Mirrors `src/kicad_tools/benchmark/external/render.py`'s markdown renderer
 * cell-for-cell, so the kicad-tools.org benchmarks page (Epic #4932, Phase 3,
 * issue #4952) reads identically to `kct bench external`'s own CLI output —
 * same honesty rules: a refused timing renders as `refused`, never blank; a
 * DRC engine that did not run renders as `not run`, never `0`.
 */
import type { BenchmarkReport } from "./benchmarkTypes.ts";

export function fmtCompletion(report: BenchmarkReport): string {
  return `${report.completion.completion_pct.toFixed(1)}%`;
}

export function fmtConnections(report: BenchmarkReport): string {
  return `${report.completion.connections_routed} of ${report.completion.connections_total}`;
}

export function fmtVias(report: BenchmarkReport): string {
  return String(report.copper.via_count);
}

export function fmtWirelength(report: BenchmarkReport): string {
  return report.copper.wirelength_mm.toFixed(2);
}

export function fmtTiming(report: BenchmarkReport): string {
  if (report.timing.valid && report.timing.wall_clock_s !== null) {
    return `${report.timing.wall_clock_s.toFixed(1)} s`;
  }
  return "refused";
}

export function fmtKctCheck(report: BenchmarkReport): string {
  const summary = report.kct_check;
  if (!summary.ran) return "not run";
  const verdict = summary.passed ? "PASS" : "FAIL";
  return `${verdict} (${summary.error_count}E / ${summary.warning_count}W)`;
}

export function fmtCliDrc(report: BenchmarkReport): string {
  const summary = report.kicad_cli_drc;
  if (!summary.ran || summary.violation_count === null) return "not run";
  return String(summary.violation_count);
}

export function fmtDiffPairs(report: BenchmarkReport): string {
  const pairs = report.diff_pairs;
  if (pairs === null) return "n/a";
  return `${pairs.pairs_complete}/${pairs.pairs_total}`;
}

/**
 * True when a report's `copper` block shows no routed copper at all — the
 * schema-native signal this page uses to distinguish "an attempt that
 * stopped before routing" from a genuinely routed (even if incomplete)
 * result. Mirrors the criterion `benchmarks/external/results/README.md`
 * itself uses for the two committed 2026-08-25 reports: "`copper.via_count`
 * and `copper.wirelength_mm` are both `0` for both boards, confirming the
 * router placed no copper."
 *
 * Not a universal proof for every conceivable board (a board could in
 * principle finish routing needing zero vias and zero length), but it is
 * the concrete, JSON-derived signal available today — never a hardcoded
 * per-board flag that could go stale.
 */
export function producedNoRoutedOutput(report: BenchmarkReport): boolean {
  return report.copper.via_count === 0 && report.copper.wirelength_mm === 0;
}

/** Render an ISO-8601 timestamp as a bare `YYYY-MM-DD` date, matching the
 *  dated style `benchmarks/external/results/README.md` and the schema doc
 *  already use (e.g. "2026-08-24"). */
export function fmtDateOnly(iso: string): string {
  return iso.slice(0, 10);
}
