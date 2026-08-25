/**
 * Unit tests for the external-benchmark report loader.
 *
 * Mirrors `loadBoards.test.ts`'s fixture-directory style: build a temporary
 * `results/` fixture on disk and point the loader at it via the
 * `KCT_BENCHMARKS_DIR` override, exercising:
 *   - missing/empty results dir → []
 *   - a valid report              → parsed, typed BenchmarkReport
 *   - unknown schema_version      → skipped, with a warning
 *   - invalid JSON                → skipped, with a warning
 *   - missing required field      → skipped, with a warning
 *   - non-`.json` files ignored
 *   - sorted by board_id then protocol
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadBenchmarkFile, loadBenchmarks, benchmarksResultsDirExists } from "./loadBenchmarks.ts";

let root: string;

function writeReport(filename: string, data: unknown): void {
  writeFileSync(join(root, filename), JSON.stringify(data));
}

const validReport = (boardId: string, overrides: Record<string, unknown> = {}) => ({
  $schema: "https://kicad-tools.org/schemas/benchmark-external/v1.json",
  schema_version: 1,
  generated_at: "2026-08-25T04:10:36+00:00",
  board_id: boardId,
  board_commit: "abc1234",
  board_source: "https://example.com/repo",
  protocol: "zero-touch",
  tool_commit: "def5678",
  completion: { connections_routed: 10, connections_total: 20, completion_pct: 50.0 },
  copper: { via_count: 3, wirelength_mm: 120.5 },
  timing: { wall_clock_s: 4.2, valid: true, refusal_reason: null },
  kct_check: { ran: true, passed: true, error_count: 0, warning_count: 2 },
  kicad_cli_drc: { ran: true, violation_count: 0 },
  diff_pairs: null,
  notes: [],
  ...overrides,
});

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "kct-benchmarks-"));
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
  vi.restoreAllMocks();
});

describe("loadBenchmarks", () => {
  it("returns [] for a nonexistent directory", () => {
    expect(loadBenchmarks(join(root, "does-not-exist"))).toEqual([]);
    expect(benchmarksResultsDirExists(join(root, "does-not-exist"))).toBe(false);
  });

  it("returns [] for an empty directory", () => {
    expect(loadBenchmarks(root)).toEqual([]);
    expect(benchmarksResultsDirExists(root)).toBe(true);
  });

  it("parses a valid report into a typed BenchmarkReport", () => {
    writeReport("pocketbeagle.zero-touch.json", validReport("pocketbeagle"));
    const reports = loadBenchmarks(root);
    expect(reports).toHaveLength(1);
    expect(reports[0].board_id).toBe("pocketbeagle");
    expect(reports[0].protocol).toBe("zero-touch");
    expect(reports[0].completion.completion_pct).toBeCloseTo(50.0);
  });

  it("skips an unknown schema_version, with a warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    writeReport("future.json", validReport("future", { schema_version: 2 }));
    expect(loadBenchmarks(root)).toEqual([]);
    expect(warn).toHaveBeenCalled();
  });

  it("skips invalid JSON, with a warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    writeFileSync(join(root, "bad.json"), "{ not valid json");
    expect(loadBenchmarks(root)).toEqual([]);
    expect(warn).toHaveBeenCalled();
  });

  it("skips a report missing a required field, with a warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { completion: _drop, ...withoutCompletion } = validReport("incomplete");
    writeReport("incomplete.json", withoutCompletion);
    expect(loadBenchmarks(root)).toEqual([]);
    expect(warn).toHaveBeenCalled();
  });

  it("ignores non-.json files", () => {
    writeFileSync(join(root, "README.md"), "not a report");
    writeReport("beagleconnect_freedom.zero-touch.json", validReport("beagleconnect_freedom"));
    const reports = loadBenchmarks(root);
    expect(reports).toHaveLength(1);
    expect(reports[0].board_id).toBe("beagleconnect_freedom");
  });

  it("sorts by board_id then protocol", () => {
    writeReport("strf.zero-touch.json", validReport("strf", { protocol: "zero-touch" }));
    writeReport("strf.tuned.json", validReport("strf", { protocol: "tuned" }));
    writeReport("pocketbeagle.zero-touch.json", validReport("pocketbeagle"));
    const reports = loadBenchmarks(root);
    expect(reports.map((r) => `${r.board_id}:${r.protocol}`)).toEqual([
      "pocketbeagle:zero-touch",
      "strf:tuned",
      "strf:zero-touch",
    ]);
  });
});

describe("loadBenchmarkFile", () => {
  it("returns null when the file does not exist", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(loadBenchmarkFile(join(root, "missing.json"))).toBeNull();
    expect(warn).toHaveBeenCalled();
  });
});
