/**
 * Display-status helper for the gallery status badge (issue #3717).
 *
 * The raw `board.status` field ("ok"/"partial"/"no_artifacts") comes from
 * `kct board-metrics`. The gallery renders `status === "ok"` as a green
 * "Ready" badge — but "Ready" must mean *manufacturable*, and a board with
 * `drc_violations > 0` is not manufacturable.
 *
 * This module derives the *displayed* badge from BOTH `status` and
 * `drc_violations` so a stale or over-optimistic `status` can never surface
 * "Ready" over a violating board. It is defense-in-depth: `board-metrics`
 * already downgrades `status` away from "ok" when DRC > 0, but the site never
 * trusts that alone.
 */
import type { Board } from "./types.ts";

/** The display variants the status chip / row can take. */
export type DisplayStatus = "ready" | "drc" | "partial" | "no_artifacts";

/**
 * Compute the *displayed* status variant for a board.
 *
 * Rules:
 *   - `drc_violations > 0`  → "drc"   (warn-styled, never "Ready")
 *   - `status === "ok"`     → "ready" (only reachable when DRC is 0/absent)
 *   - `status === "partial"`→ "partial"
 *   - else                  → "no_artifacts"
 */
export function displayStatus(board: Board): DisplayStatus {
  if ((board.drc_violations ?? 0) > 0) return "drc";
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
    case "partial":
      return "Partial";
    case "no_artifacts":
      return "No artifacts";
  }
}
