/**
 * DeepPCB's own published reference numbers for the three benchmark boards
 * (Epic #4932, Phase 3, issue #4952).
 *
 * Source of truth: `benchmarks/external/boards.toml` → each board's
 * `[<slug>.deep_pcb_reference]` table, itself sourced from
 * https://deeppcb.ai/benchmarks/ and
 * https://deeppcb.ai/deeppcb-vs-quilter-open-source-routing-compared-2026/.
 *
 * This is a small, hand-kept mirror rather than a build-time TOML parse:
 * three boards' worth of reference numbers change rarely (only when
 * `boards.toml` is deliberately re-pinned), and pulling in a TOML dependency
 * for three records would be disproportionate. If `boards.toml`'s
 * `deep_pcb_reference` tables are ever edited, update this file to match in
 * the same change.
 */

export interface DeepPcbReference {
  slug: string;
  name: string;
  repoUrl: string;
  /** Pinned commit from `boards.toml` — used for reproduction instructions
   *  when no committed benchmark report exists yet for this board. */
  commit: string;
  /** A field is omitted when DeepPCB did not publish that number for this board. */
  airwires?: number;
  completionPct?: number;
  vias?: number;
  wallClockMinutesMax?: number;
}

export const DEEP_PCB_REFERENCE: readonly DeepPcbReference[] = [
  {
    slug: "strf",
    name: "STRF RF mixed-signal",
    repoUrl: "https://github.com/pms67/STRF-Kicad",
    commit: "0525ef655e460ff6d91d770582b47925e7852e7a",
    airwires: 98,
    completionPct: 100,
    vias: 68,
    wallClockMinutesMax: 3,
  },
  {
    slug: "pocketbeagle",
    name: "PocketBeagle",
    repoUrl: "https://github.com/beagleboard/pocketbeagle",
    commit: "d793a63f48dd3041e333362d5ec870377d255f89",
    airwires: 290,
  },
  {
    slug: "beagleconnect_freedom",
    name: "BeagleConnect Freedom",
    repoUrl: "https://git.beagleboard.org/beagleconnect/freedom",
    commit: "3f99c08de7d81991f95eb2c23f30798a129ffdcf",
    airwires: 414,
  },
];

export function deepPcbReferenceFor(slug: string): DeepPcbReference | undefined {
  return DEEP_PCB_REFERENCE.find((b) => b.slug === slug);
}
