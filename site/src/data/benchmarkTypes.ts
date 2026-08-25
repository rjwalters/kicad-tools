/**
 * TypeScript types for the external-benchmark report contract (schema v1).
 *
 * Mirrors `docs/benchmark-external-report-schema.md` in the repository root
 * (Epic #4932, Phase 1, issue #4934). Keep these in sync with that document —
 * it is the canonical contract produced by `kct bench external` and consumed
 * by the kicad-tools.org benchmarks page (Phase 3, issue #4952).
 *
 * Unlike `board.json` (types.ts), this loader only reads fields the
 * benchmarks page actually renders — not the full schema. Fields below are
 * a subset of the real report; add more here if the page starts using them.
 */

export const BENCHMARK_SCHEMA_VERSION = 1;

/** `zero-touch` (rules as shipped) or `tuned` (declared netclass config). */
export type BenchmarkProtocol = "zero-touch" | "tuned" | string;

export interface BenchmarkCompletion {
  connections_routed: number;
  connections_total: number;
  completion_pct: number;
}

export interface BenchmarkCopper {
  via_count: number;
  wirelength_mm: number;
}

export interface BenchmarkTiming {
  wall_clock_s: number | null;
  valid: boolean;
  refusal_reason: string | null;
}

export interface BenchmarkKctCheck {
  ran: boolean;
  passed: boolean;
  error_count: number;
  warning_count: number;
}

export interface BenchmarkKicadCliDrc {
  ran: boolean;
  violation_count: number | null;
}

export interface BenchmarkDiffPairs {
  pairs_total: number;
  pairs_complete: number;
  completion_pct: number;
}

/**
 * A fully-parsed external-benchmark report matching schema v1.
 *
 * Required fields (always present per the schema): `$schema`,
 * `schema_version`, `board_id`, `protocol`. The nested blocks below are also
 * always present in a valid report (the schema never omits them — absent
 * *data* within a block is represented by `null`/`0`, not by dropping the
 * block), so they are typed as required here too.
 */
export interface BenchmarkReport {
  $schema: string;
  schema_version: number;
  generated_at: string;
  board_id: string;
  board_commit: string | null;
  board_source: string | null;
  protocol: BenchmarkProtocol;
  tool_commit: string;
  completion: BenchmarkCompletion;
  copper: BenchmarkCopper;
  timing: BenchmarkTiming;
  kct_check: BenchmarkKctCheck;
  kicad_cli_drc: BenchmarkKicadCliDrc;
  diff_pairs: BenchmarkDiffPairs | null;
  notes: string[];
}
