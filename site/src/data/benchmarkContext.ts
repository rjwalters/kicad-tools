/** Reviewed interpretation from the collection writeup, bound to exact report bytes.
 * Never apply a board's old diagnosis to a rerun. Raw outcome/metrics stay authoritative.
 */
export interface RunContext {
  notes: string[];
  source: string;
}

const collectionSource = "https://github.com/rjwalters/kicad-tools/blob/main/benchmarks/external/results/2026-09-12/README.md";
const parserLimitation = "The routing parser omitted 21 of 49 net names, including all eight USB/SPI rule entries. This limits both STRF protocols; the result does not demonstrate effective USB/SPI tuning (tracked in #5302).";

const reviewed: Record<string, RunContext> = {
  "797c4819ea1ea43a2b6710d4ba4ab16e30b45b85562f7d65ee5ae0f2d2097b2e": {
    notes: ["The grid-size safety gate refused routing: the selected 0.127 mm pitch exceeded the clearance/2 limit of 0.075 mm after the 500,000-cell budget. No output copper was produced. The 94/296 connected baseline is not routing progress."],
    source: collectionSource,
  },
  "382b52c36f2e1911e52b7c61ac423078c9cde1d93e51e07337ef2498a16bf764": {
    notes: ["Placement checks refused routing because five SH1 pads lay outside the board outline. No output copper was produced. The internal design check could not evaluate custom pad U6.9; its missing result is not a pass."],
    source: collectionSource,
  },
  "4bfa2e872bd8351915e266d21bf24f3de42d94c14512ce43542572cd39c19603": {
    notes: [parserLimitation, "Measured connectivity fell from 23/98 to 21/98. GND had two additional islands after output generation. Retained versus regenerated zone fills are a possible explanation, but causation is unresolved; this does not prove two routed connections were removed."],
    source: collectionSource,
  },
};

export function contextForReport(sha256: string): RunContext | undefined {
  return reviewed[sha256];
}
