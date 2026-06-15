/**
 * TypeScript types for the `board.json` data contract (schema v1).
 *
 * Mirrors `docs/board-json-schema.md` in the repository root. Keep these in
 * sync with that document — it is the canonical contract produced by
 * `kct board-metrics` and consumed by this Astro site.
 *
 * Optional-field rule: a missing source artifact means the field is OMITTED,
 * never emitted as `null`. Accordingly every optional field is typed `?: T`
 * (not `T | null`). Downstream code should treat a missing key as "unknown".
 */

/** Schema version this loader understands. Documents with a different major
 *  version are skipped (forward-compat guard). */
export const SCHEMA_VERSION = 1;

/** Board status enum — see `docs/board-json-schema.md` "status values". */
export type BoardStatus = "ok" | "partial" | "no_artifacts";

/** Physical board dimensions in millimeters. */
export interface BoardSize {
  width: number;
  height: number;
}

/** Cost estimate block. All members optional (omitted when absent). */
export interface CostEstimate {
  per_board_usd?: number;
  batch_qty?: number;
  batch_total_usd?: number;
}

/**
 * A fully-parsed board record matching `board.json` schema v1.
 *
 * Required fields (always present): `$schema`, `schema_version`,
 * `generated_at`, `slug`, `status`. All other fields are optional and omitted
 * when the underlying artifact is missing.
 */
export interface Board {
  $schema: string;
  schema_version: number;
  generated_at: string;
  slug: string;
  status: BoardStatus;
  name?: string;
  description?: string;
  layer_count?: number;
  board_size_mm?: BoardSize;
  part_count?: number;
  nets_routed_pct?: number;
  drc_violations?: number;
  cost?: CostEstimate;
  /** Map of render id → path relative to the board.json location. */
  renders?: Record<string, string>;
  manufacturing_package?: string;
  manifest_generated_at?: string;
}
