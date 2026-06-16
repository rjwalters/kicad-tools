/**
 * Display-status helper for the gallery status badge (issues #3717, #3749).
 *
 * The raw `board.status` field ("ok"/"partial"/"no_artifacts") comes from
 * `kct board-metrics`. The gallery renders `status === "ok"` as a green
 * "Ready" badge — but "Ready" must mean *manufacturable + electrically
 * verified*, so we add two additional gates here:
 *
 *   - `drc_violations > 0`     → "drc"        (board cannot be fabricated)
 *   - `lvs_clean === false`    → "lvs"        (board will not function)
 *   - `lvs_clean === undefined`→ "unverified" (LVS has not run; unknown)
 *
 * Defense-in-depth: `board-metrics` already downgrades `status` away from
 * "ok" when DRC > 0 or when LVS records an explicit mismatch, but the site
 * never trusts that alone. The "unverified" case is purely a site-layer
 * stricter gate — a missing `lvs.json` does NOT downgrade `status` at the
 * producer layer (boards without an LVS step yet keep their existing status).
 */
import type { Board } from "./types.ts";

/** The display variants the status chip / row can take. */
export type DisplayStatus =
  | "ready"
  | "drc"
  | "lvs"
  | "unverified"
  | "partial"
  | "no_artifacts";

/**
 * Compute the *displayed* status variant for a board.
 *
 * Display-priority order (load-bearing): DRC > LVS > unverified > status.
 *
 * Rationale: a DRC violation is the strongest negative signal — the board
 * cannot be manufactured. An LVS mismatch is next — the board can be made
 * but will not function as designed. "Unverified" is a neutral unknown that
 * must never display as Ready. Only when all three gates pass does the raw
 * `status` field decide.
 */
export function displayStatus(board: Board): DisplayStatus {
  if ((board.drc_violations ?? 0) > 0) return "drc";
  if (board.lvs_clean === false) return "lvs";
  if (board.lvs_clean === undefined) return "unverified";
  if (board.status === "ok") return "ready";
  if (board.status === "partial") return "partial";
  return "no_artifacts";
}

/** Human-readable label for a display-status variant. */
export function displayStatusLabel(board: Board): string {
  const variant = displayStatus(board);
  switch (variant) {
    case "ready":
      return "Ready";
    case "drc": {
      const n = board.drc_violations ?? 0;
      return `${n} DRC violation${n === 1 ? "" : "s"}`;
    }
    case "lvs": {
      const n = board.lvs_mismatches ?? 0;
      if (n > 0) {
        return `LVS: ${n} mismatch${n === 1 ? "" : "es"}`;
      }
      return "LVS mismatch";
    }
    case "unverified":
      return "LVS not run";
    case "partial":
      return "Partial";
    case "no_artifacts":
      return "No artifacts";
  }
}
