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

const octoberTwentyFirstSource = "https://github.com/rjwalters/kicad-tools/blob/main/benchmarks/external/results/2026-10-21/README.md";
const octoberTwentyFirstRuntime = "The 2026-10-21 collection ran on a GitHub-hosted x86_64 Linux runner in the pinned KiCad container (12 GiB limit, 120-minute job limit) with the C++ backend present. Runner CPU and RAM were not recorded, and the reports do not show whether any pass fell back to Python. The durations are not comparable to physical-x86 vendor performance.";

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
  "489520c04dca58d2b81d1a16cbc1d25def3c3b5cb385298056b8539f938b7370": {
    notes: ["The router raised a layer error (Layer 'In1.Cu' is not available; the declared copper layer list was empty) after 4.6 seconds and placed no copper. The earlier grid-size refusal (#4945) was fixed and is no longer the stop. The new failure is not diagnosed here, and the 0/296 figure is not a measure of routing ability."],
    source: octoberTwentyFirstSource,
    runtime: octoberTwentyFirstRuntime,
  },
  "6bae1cfd01ec269f08b99b1e531510a96dc2444955fab524e8a611b2d442d198": {
    notes: ["Connectivity ended at 131/460, below the 134/460 input baseline (three fewer), with no vias or routed wirelength; the router exited 1. SH1 is placement-invalid, so the GND net stays excluded, and no differential pair is complete. This is lower than the 197/460 of the 2026-09-15 run and is reported as measured; its cause is not established here. kct check could not run (custom pad U6.9) and native DRC reports 426 violations, none waived."],
    source: octoberTwentyFirstSource,
    runtime: octoberTwentyFirstRuntime,
  },
  "366270bd0bb23090d63e50b6ee38c20fa30026b64ab7d9654a11d77a583c5486": {
    notes: ["Connectivity rose from 18/98 to 44/98 and both USB pairs are complete; the router exited 4 (partial). kct check fails (317 errors) and native DRC reports 148 violations, none waived. This is a partial result, not a clean board. It follows the parser fix (#5302) that was fixed, so the 2026-09-12 parser limitation does not apply."],
    source: octoberTwentyFirstSource,
    runtime: octoberTwentyFirstRuntime,
  },
  "907e5e968fd05d6dbf16eb305a3840ffb2b99b37aaa5d3540e4495e5de14e5ce": {
    notes: ["Connectivity rose from 18/98 to 44/98, the same count as the zero-touch run, with one of two differential pairs complete (USB_D+ is incomplete) against two of two zero-touch. The router exited 4 (partial); kct check fails (321 errors) and native DRC reports 152 violations, none waived. These counts do not show an advantage from tuning."],
    source: octoberTwentyFirstSource,
    runtime: octoberTwentyFirstRuntime,
  },
};

export function contextForReport(sha256: string): RunContext | undefined {
  const context = reviewed[sha256];
  return context && { ...context };
}
