import { describe, expect, it } from "vitest";
import {
  fmtCompletion,
  fmtConnections,
  fmtVias,
  fmtWirelength,
  fmtTiming,
  fmtKctCheck,
  fmtCliDrc,
  fmtDiffPairs,
  fmtOutcome,
  fmtArtifactSource,
  isLegacyOutcome,
  isFallbackArtifact,
} from "./benchmarkFormat.ts";
import type { BenchmarkReport } from "./benchmarkTypes.ts";

const base: BenchmarkReport = {
  $schema: "https://kicad-tools.org/schemas/benchmark-external/v1.json",
  schema_version: 1,
  generated_at: "2026-08-25T04:10:36+00:00",
  board_id: "pocketbeagle",
  board_commit: "d793a63f",
  board_source: "https://github.com/beagleboard/pocketbeagle",
  protocol: "zero-touch",
  tool_commit: "636fd368",
  completion: { connections_routed: 94, connections_total: 296, completion_pct: 31.76 },
  copper: { via_count: 0, wirelength_mm: 0 },
  timing: { wall_clock_s: 2.821, valid: true, refusal_reason: null },
  kct_check: { ran: true, passed: false, error_count: 394, warning_count: 801 },
  kicad_cli_drc: { ran: true, violation_count: 0 },
  diff_pairs: null,
  notes: [],
};

describe("benchmarkFormat", () => {
  it("formats completion, connections, vias, wirelength", () => {
    expect(fmtCompletion(base)).toBe("31.8%");
    expect(fmtConnections(base)).toBe("94 of 296");
    expect(fmtVias(base)).toBe("0");
    expect(fmtWirelength(base)).toBe("0.00");
  });

  it("formats valid timing as seconds", () => {
    expect(fmtTiming(base)).toBe("2.8 s");
  });

  it("formats refused timing as 'refused', never blank", () => {
    const refused: BenchmarkReport = {
      ...base,
      timing: { wall_clock_s: null, valid: false, refusal_reason: "C++ backend not installed" },
    };
    expect(fmtTiming(refused)).toBe("refused");
  });

  it("formats kct_check as PASS/FAIL with error/warning counts", () => {
    expect(fmtKctCheck(base)).toBe("FAIL (394E / 801W)");
    const passing: BenchmarkReport = {
      ...base,
      kct_check: { ran: true, passed: true, error_count: 0, warning_count: 3 },
    };
    expect(fmtKctCheck(passing)).toBe("PASS (0E / 3W)");
  });

  it("formats kct_check as 'not run' when it did not run", () => {
    const skipped: BenchmarkReport = {
      ...base,
      kct_check: { ran: false, passed: false, error_count: 0, warning_count: 0 },
    };
    expect(fmtKctCheck(skipped)).toBe("not run");
  });

  it("formats kicad-cli DRC count, or 'not run' — never 0 for a skipped run", () => {
    expect(fmtCliDrc(base)).toBe("0");
    const skipped: BenchmarkReport = {
      ...base,
      kicad_cli_drc: { ran: false, violation_count: null },
    };
    expect(fmtCliDrc(skipped)).toBe("not run");
  });

  it("formats diff pairs as complete/total, or 'n/a' when absent", () => {
    expect(fmtDiffPairs(base)).toBe("n/a");
    const withPairs: BenchmarkReport = {
      ...base,
      diff_pairs: { pairs_total: 4, pairs_complete: 0, completion_pct: 0 },
    };
    expect(fmtDiffPairs(withPairs)).toBe("0/4");
  });

  it("formats missing route_outcome as legacy, never success (#5280)", () => {
    expect(fmtOutcome(base)).toBe("unknown (legacy)");
    expect(fmtArtifactSource(base)).toBe("unknown");
    expect(isLegacyOutcome(base)).toBe(true);
    expect(isFallbackArtifact(base)).toBe(false);
  });

  it("formats a completed outcome and router_output artifact", () => {
    const completed: BenchmarkReport = {
      ...base,
      route_outcome: {
        outcome: "completed",
        artifact_source: "router_output",
        exit_code: 0,
        reason: null,
      },
    };
    expect(fmtOutcome(completed)).toBe("completed");
    expect(fmtArtifactSource(completed)).toBe("router output");
    expect(isLegacyOutcome(completed)).toBe(false);
    expect(isFallbackArtifact(completed)).toBe(false);
  });

  it("formats stopped_before_routing / fallback_input and flags them", () => {
    const stopped: BenchmarkReport = {
      ...base,
      route_outcome: {
        outcome: "stopped_before_routing",
        artifact_source: "fallback_input",
        exit_code: 1,
        reason: "router exited 1 and produced no output file",
      },
    };
    expect(fmtOutcome(stopped)).toBe("stopped before routing");
    expect(fmtArtifactSource(stopped)).toBe("fallback input");
    expect(isFallbackArtifact(stopped)).toBe(true);
  });

  it("labels a valid timing by its non-completed phase, never bare seconds", () => {
    const stoppedTiming: BenchmarkReport = {
      ...base,
      timing: { wall_clock_s: 4.4, valid: true, refusal_reason: null, measured_phase: "failed" },
    };
    expect(fmtTiming(stoppedTiming)).toBe("4.4 s (failed)");
  });

  it("renders a completed/unknown phase as bare seconds", () => {
    const completedTiming: BenchmarkReport = {
      ...base,
      timing: {
        wall_clock_s: 4.4,
        valid: true,
        refusal_reason: null,
        measured_phase: "completed",
      },
    };
    expect(fmtTiming(completedTiming)).toBe("4.4 s");
  });
});
