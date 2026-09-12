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
    const phase = report.timing.measured_phase;
    if (phase && phase !== "completed" && phase !== "unknown") {
      // A real, backend-eligible elapsed time on a non-completed attempt
      // is time-to-refusal/partial-progress, NOT a completed-routing
      // performance number -- never render it as a bare seconds figure
      // (issue #5280).
      return `${report.timing.wall_clock_s.toFixed(1)} s (${phase.replace(/_/g, " ")})`;
    }
    return `${report.timing.wall_clock_s.toFixed(1)} s`;
  }
  return "refused";
}

/** `"unknown (legacy)"` when `route_outcome` is absent -- never success. */
export function fmtOutcome(report: BenchmarkReport): string {
  const outcome = report.route_outcome;
  if (outcome == null) return "unknown (legacy)";
  return outcome.outcome.replace(/_/g, " ");
}

/** Whether the measured board is router output, a fallback input, or unknown. */
export function fmtArtifactSource(report: BenchmarkReport): string {
  const outcome = report.route_outcome;
  if (outcome == null) return "unknown";
  return outcome.artifact_source.replace(/_/g, " ");
}

/** True when this report predates route-outcome/artifact-provenance tracking (#5280). */
export function isLegacyOutcome(report: BenchmarkReport): boolean {
  return report.route_outcome == null;
}

/** True when the measured board is the pre-route input, not router output. */
export function isFallbackArtifact(report: BenchmarkReport): boolean {
  return report.route_outcome?.artifact_source === "fallback_input";
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
