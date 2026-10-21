/** Reviewed interpretation from the collection writeup, bound to exact report bytes.
 * Never apply a board's old diagnosis to a rerun. Raw outcome/metrics stay authoritative.
 */
export interface RunContext {
  notes: string[];
  source: string;
  runtime?: string;
}

const collectionSource = "https://github.com/rjwalters/kicad-tools/blob/main/benchmarks/external/results/2026-09-12/README.md";
const partialRouteSource = "https://github.com/rjwalters/kicad-tools/blob/main/benchmarks/external/results/2026-09-15/README.md";
const parserLimitation = "The routing parser omitted 21 of 49 net names, including all eight USB/SPI rule entries. This limits both STRF protocols; the result does not demonstrate effective USB/SPI tuning. The parser defect (#5302) was fixed after this run.";
const septemberTwelfthRuntime = "The 2026-09-12 collection ran Linux amd64 through Rosetta on an arm64 host with four CPUs. The container requested 12 GiB, but the Docker VM had about 7.75 GiB available. These attempt durations are not comparable to physical-x86 vendor performance.";

const reviewed: Record<string, RunContext> = {
  "797c4819ea1ea43a2b6710d4ba4ab16e30b45b85562f7d65ee5ae0f2d2097b2e": {
    notes: ["The grid-size safety gate refused routing: the selected 0.127 mm pitch exceeded the clearance/2 limit of 0.075 mm after the 500,000-cell budget. No output copper was produced. The 94/296 connected baseline is not routing progress. The safety-gate blocker (#4945) was fixed after this run."],
    source: collectionSource,
    runtime: septemberTwelfthRuntime,
  },
  "382b52c36f2e1911e52b7c61ac423078c9cde1d93e51e07337ef2498a16bf764": {
    notes: ["Placement checks refused routing because five SH1 pads lay outside the board outline. No output copper was produced. The internal design check could not evaluate custom pad U6.9; its missing result is not a pass. Later work (#4946, #5348) enabled partial routing past this stop; see the 2026-09-15 run."],
    source: collectionSource,
    runtime: septemberTwelfthRuntime,
  },
  "4bfa2e872bd8351915e266d21bf24f3de42d94c14512ce43542572cd39c19603": {
    notes: [parserLimitation, "Measured connectivity fell from 23/98 to 21/98. GND had two additional islands after output generation. Retained versus regenerated zone fills are a possible explanation, but causation is unresolved; this does not prove two routed connections were removed."],
    source: collectionSource,
    runtime: septemberTwelfthRuntime,
  },
  "2aac01c8809e03aeb6c0e7c3ccd88f6ccad01762d3d8b9519409a6951f806fd8": {
    notes: [parserLimitation,
      "The tuned configuration was requested, but zero of eight sidecar entries matched. The raw note saying it was applied describes invocation, not effective tuning.",
      "The pathfinder fell back from C++ to Python four times. The 1,260.1-second routing-pass duration includes mixed execution and is not an exclusively native routing benchmark.",
      "Partial output contains one 70.25 mm segment but no increase over the 23/98 input connectivity. Both USB pairs remain incomplete; the design checks report failures."],
    source: collectionSource,
    runtime: septemberTwelfthRuntime,
  },
  "7ed834cde4651f59ee371e623e63262c6686e551ee2f0258740025d5252bfacc": {
    notes: ["Partial routing only: physical completion rose from 118/460 to 197/460 connections (42.83%) and the router exited 2 (partial). SH1 is placement-invalid, so the entire GND net stays excluded.",
      "Native DRC reports 383 violations, 199 of them invalid-outline findings. kct check could not run because its checker rejects custom pad U6.9. None of these are waived, and the result is not manufacturing acceptance.",
      "The 2175.9-second duration spans the default configuration search and includes Python fallbacks recorded by the router; it is not a pure-C++ speed claim."],
    source: partialRouteSource,
    runtime: "The 2026-09-15 run did not record a CPU count, RAM or container runtime. Artifact paths and the router extension in provenance.json indicate a native macOS build, not the Rosetta/Docker setup of the 2026-09-12 collection. The duration is not comparable to physical-x86 vendor performance.",
  },
};

export function contextForReport(sha256: string): RunContext | undefined {
  const context = reviewed[sha256];
  return context && { ...context };
}
