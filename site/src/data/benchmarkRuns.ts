/** Dated evidence collections, kept separate from the root-level historical archive. */
import { readdirSync } from "node:fs";
import { join } from "node:path";
import { benchmarksResultsDir, loadBenchmarkFile } from "./loadBenchmarks.ts";
import type { BenchmarkReport } from "./benchmarkTypes.ts";

export interface BenchmarkRun {
  report: BenchmarkReport;
  /** Loader-owned relative path, never supplied by report JSON. */
  path: string;
  collection: string;
}

export function loadBenchmarkRuns(root = benchmarksResultsDir()): BenchmarkRun[] {
  const runs: BenchmarkRun[] = [];
  let collections;
  try { collections = readdirSync(root, { withFileTypes: true }); }
  catch { return runs; }
  for (const directory of collections) {
    if (!directory.isDirectory() || !/^\d{4}-\d{2}-\d{2}$/.test(directory.name)) continue;
    for (const file of readdirSync(join(root, directory.name), { withFileTypes: true })) {
      // Provenance manifests are companions, not route reports. Ignore symlinks.
      if (!file.isFile() || !file.name.endsWith(".json") || file.name === "run-provenance.json") continue;
      const report = loadBenchmarkFile(join(root, directory.name, file.name));
      if (report) runs.push({ report, collection: directory.name, path: `${directory.name}/${file.name}` });
    }
  }
  return runs.sort((a, b) => b.collection.localeCompare(a.collection)
    || a.report.board_id.localeCompare(b.report.board_id)
    || a.report.protocol.localeCompare(b.report.protocol)
    || a.path.localeCompare(b.path));
}

export const EXPECTED_CASES = [
  { board: "pocketbeagle", protocol: "zero-touch" },
  { board: "beagleconnect_freedom", protocol: "zero-touch" },
  { board: "strf", protocol: "zero-touch" },
  { board: "strf", protocol: "tuned" },
];

export function outcomeExplanation(report: BenchmarkReport): string {
  switch (report.route_outcome?.outcome) {
    case "completed": return "The routing attempt completed. Completion does not establish manufacturing readiness or tested hardware.";
    case "partial": return "The attempt ended with incomplete routing; some connections remain unresolved.";
    case "stopped_before_routing": return "The attempt stopped before routing. Input-board connectivity is not newly routed progress.";
    case "failed": return "The attempt failed. Any measured board must be interpreted using its recorded artifact source.";
    case "timeout": return "The attempt reached its time limit. This is not a completed-route performance result.";
    default: return "The report does not establish a known routing outcome; measurements alone do not prove success.";
  }
}

export function attemptDuration(report: BenchmarkReport): string {
  if (!report.timing.valid || report.timing.wall_clock_s === null) {
    return `Not reported: ${report.timing.refusal_reason ?? "no eligible timing evidence"}`;
  }
  const phase = report.timing.measured_phase ?? "unknown";
  return `${report.timing.wall_clock_s.toFixed(1)} s — ${phase.replaceAll("_", " ")}${phase === "unknown" ? " phase (not established as route speed)" : " phase"}`;
}
