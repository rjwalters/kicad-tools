/** Readiness badges require content-validated, current manufacturing evidence. */
import type { Board } from "./types.ts";

/** The display variants the status chip / row can take. */
export type DisplayStatus =
  | "development"
  | "ready"
  | "blocked"
  | "drc"
  | "lvs"
  | "unverified"
  | "partial"
  | "no_artifacts";

/** Current readiness is authoritative; historical DRC/LVS remain defensive gates. */
export function displayStatus(board: Board): DisplayStatus {
  if (board.status === "no_artifacts" && ["ready", "blocked"].includes(board.readiness?.status ?? "")) return "development";
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
    case "development":
      return "In development";
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

/** Shared legend text on cards and detail pages; describes the badge's limits. */
export function displayStatusExplanation(board: Board): string {
  switch (displayStatus(board)) {
    case "development": return "Verified development checks are available; complete design artifacts are not published here. This is not an ordering-ready package.";
    case "ready": return board.readiness?.mode === "pcb_only"
      ? "Current evidence passes the bare-PCB release checks; assembly and hardware testing are not included."
      : "Current evidence passes the assembly release checks; this does not establish tested hardware performance.";
    case "blocked": return "Current evidence records failed or incomplete release requirements.";
    case "drc": return "Recorded design-rule violations need resolution before release.";
    case "lvs": return "The recorded schematic and PCB connectivity do not agree.";
    case "unverified": return "Current readiness evidence is missing, invalid, or no longer matches the checked files.";
    case "partial": return "Only part of the design artifact set is available.";
    case "no_artifacts": return "No usable design artifact summary is available.";
  }
}
