/**
 * Build-time loader for the committed external-benchmark reports (Epic
 * #4932, Phase 3, issue #4952).
 *
 * Reads every `*.json` file under `benchmarks/external/results/` (the
 * schema-v1 contract produced by `kct bench external` — see
 * `docs/benchmark-external-report-schema.md`) and returns a typed,
 * board-then-protocol-sorted `BenchmarkReport[]`.
 *
 * Design notes (mirrors `loadBoards.ts`):
 *   - Runs at build time only (Node `fs`). Not bundled into client output.
 *   - Resilient to a missing/empty results directory: a fresh checkout with
 *     zero committed reports yields `[]`, not a build failure.
 *   - A malformed or unrecognized-schema JSON file is logged and SKIPPED —
 *     one bad file never aborts the whole build.
 *   - Deliberately does NOT fetch/route boards itself. `results/` only ever
 *     contains what a human deliberately committed (see
 *     `benchmarks/external/results/README.md`) — a board with no committed
 *     report (e.g. STRF, as of Phase 3) simply does not appear here. The
 *     page that consumes this loader is responsible for saying so honestly
 *     rather than silently omitting the board.
 */

import { readdirSync, existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { BENCHMARK_SCHEMA_VERSION } from "./benchmarkTypes.ts";
import type { BenchmarkReport } from "./benchmarkTypes.ts";

/** Directory of this module when executed unbundled (Node ESM, vitest). */
const MODULE_DIR = dirname(fileURLToPath(import.meta.url));

/** True if `path` exists and is a directory. */
function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/**
 * Absolute path to `benchmarks/external/results/`.
 *
 * Resolution order (mirrors `loadBoards.ts`'s `boardsDir()`):
 *   1. `KCT_BENCHMARKS_DIR` env override (used by tests).
 *   2. `<cwd>/../benchmarks/external/results` — `astro build`/`dev` run with
 *      cwd = `site/`, so the repo root is one level up.
 *   3. `<module>/../../../benchmarks/external/results` — fallback for direct
 *      unbundled execution (e.g. vitest importing this module directly).
 */
export function benchmarksResultsDir(): string {
  const override = process.env.KCT_BENCHMARKS_DIR;
  if (override) return resolve(override);

  const fromCwd = resolve(process.cwd(), "..", "benchmarks", "external", "results");
  if (isDir(fromCwd)) return fromCwd;

  return resolve(MODULE_DIR, "..", "..", "..", "benchmarks", "external", "results");
}

/**
 * Validate a parsed JSON value against the required-field shape of schema v1.
 * Returns the value typed as `BenchmarkReport` when valid, `null` otherwise.
 */
function validateReport(data: unknown, file: string): BenchmarkReport | null {
  if (typeof data !== "object" || data === null) {
    console.warn(`[loadBenchmarks] ${file}: report is not an object; skipping`);
    return null;
  }
  const obj = data as Record<string, unknown>;

  if (obj.schema_version !== BENCHMARK_SCHEMA_VERSION) {
    console.warn(
      `[loadBenchmarks] ${file}: unknown schema_version ` +
        `${JSON.stringify(obj.schema_version)} (expected ${BENCHMARK_SCHEMA_VERSION}); skipping`,
    );
    return null;
  }

  const required = ["board_id", "protocol", "completion", "copper", "timing"] as const;
  for (const key of required) {
    if (!(key in obj)) {
      console.warn(`[loadBenchmarks] ${file}: report missing required field "${key}"; skipping`);
      return null;
    }
  }

  return obj as unknown as BenchmarkReport;
}

/** Read and validate a single report JSON file. Returns `null` on any failure. */
export function loadBenchmarkFile(jsonPath: string): BenchmarkReport | null {
  let raw: string;
  try {
    raw = readFileSync(jsonPath, "utf8");
  } catch (err) {
    console.warn(`[loadBenchmarks] failed to read ${jsonPath} (${String(err)}); skipping`);
    return null;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    console.warn(`[loadBenchmarks] ${jsonPath} is not valid JSON (${String(err)}); skipping`);
    return null;
  }

  return validateReport(parsed, jsonPath);
}

/**
 * Load every committed benchmark report, sorted by board id then protocol.
 *
 * Never throws for a missing directory or malformed files — those become
 * empty/skipped so the static build always succeeds, even on a checkout with
 * zero committed reports.
 */
export function loadBenchmarks(root: string = benchmarksResultsDir()): BenchmarkReport[] {
  if (!isDir(root)) return [];

  const reports: BenchmarkReport[] = [];
  for (const entry of readdirSync(root).sort()) {
    if (!entry.endsWith(".json")) continue;
    const report = loadBenchmarkFile(join(root, entry));
    if (report) reports.push(report);
  }

  reports.sort((a, b) => {
    const byBoard = a.board_id.localeCompare(b.board_id);
    return byBoard !== 0 ? byBoard : a.protocol.localeCompare(b.protocol);
  });
  return reports;
}

/** True if a `results/` directory exists at all (vs. simply being empty). */
export function benchmarksResultsDirExists(root: string = benchmarksResultsDir()): boolean {
  return existsSync(root);
}
