/** Readiness badges require content-validated, current manufacturing evidence. */
import type { Board } from "./types.ts";

/** The display variants the status chip / row can take. */
export type DisplayStatus =
  | "ready"
  | "blocked"
  | "drc"
  | "lvs"
  | "unverified"
  | "partial"
  | "no_artifacts";

/** Current readiness is authoritative; historical DRC/LVS remain defensive gates. */
export function displayStatus(board: Board): DisplayStatus {
  if (board.readiness?.status === "blocked") return "blocked";
  if (board.readiness?.status !== "ready") return "unverified";
  if ((board.drc_violations ?? 0) > 0) return "drc";
  if (board.lvs_clean === false) return "lvs";
  if (board.lvs_clean === undefined) return "unverified";
  if (board.drc_violations === undefined) return "unverified";
  if (board.status === "ok") return "ready";
  if (board.status === "partial") return "partial";
  return "no_artifacts";
}

/** Human-readable label for a display-status variant. */
export function displayStatusLabel(board: Board): string {
  const variant = displayStatus(board);
  switch (variant) {
    case "ready":
      return board.readiness?.mode === "pcb_only" ? "PCB fabrication ready" : "Assembly ready";
    case "blocked":
      return "Needs work";
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
      return "Readiness unverified";
    case "partial":
      return "Partial";
    case "no_artifacts":
      return "No artifacts";
  }
}
