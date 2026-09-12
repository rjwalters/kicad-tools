import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, beforeEach, expect, it } from "vitest";
import { loadBoard } from "./loadBoards.ts";
import { displayStatus, displayStatusLabel } from "./boardStatus.ts";

let root: string;
let report: any;
function save() { writeFileSync(join(root, "output/readiness.json"), JSON.stringify(report)); }
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "kct-readiness-"));
  const names = ["output/demo_routed.kicad_pcb", "output/demo.kicad_sch", "output/demo.kicad_pro",
    "output/net_class_map.json", "output/manufacturing/manifest.json", "output/manufacturing/bom_jlcpcb.csv"];
  const inputs: Record<string, string> = {};
  for (const name of names) {
    mkdirSync(dirname(join(root, name)), { recursive: true });
    writeFileSync(join(root, name), name);
    inputs[name] = createHash("sha256").update(name).digest("hex");
  }
  report = { schema_version: 1, checked_at: "2026-09-09T10:00:00Z", mode: "pcb_only",
    status: "ready", blockers: [], inputs,
    checks: ["kct_check", "native_drc", "artifacts", "bom"].map(name => ({ name, status: "passed" })) };
  save();
  writeFileSync(join(root, "output/board.json"), JSON.stringify({
    $schema: "https://kicad-tools.org/schemas/board/v1.json", schema_version: 1,
    generated_at: "2026-07-09T00:00:00Z", slug: "demo", status: "ok", lvs_clean: true,
    drc_violations: 0, readiness: { status: "ready" },
  }));
});
afterEach(() => rmSync(root, { recursive: true, force: true }));

it("reads fresh evidence even when board.json is old and identifies fabrication scope", () => {
  const board = loadBoard(root);
  expect(displayStatus(board)).toBe("ready");
  expect(displayStatusLabel(board)).toBe("PCB fabrication ready");
});
it.each(["demo_routed.kicad_pcb", "demo.kicad_pro", "net_class_map.json", "manufacturing/bom_jlcpcb.csv"])(
  "invalidates saved success after %s changes", name => {
    writeFileSync(join(root, "output", name), "changed");
    const board = loadBoard(root);
    expect(displayStatus(board)).toBe("unverified");
    expect(board.readiness?.blockers[0]).toContain("stale");
  });
it("requires current reports, ignoring saved board.json readiness", () => {
  rmSync(join(root, "output/readiness.json"));
  expect(displayStatus(loadBoard(root))).toBe("unverified");
});
it("shows fresh failures and their reasons", () => {
  report.status = "blocked";
  report.blockers = ["Native DRC: 13 opens"];
  save();
  const board = loadBoard(root);
  expect(displayStatusLabel(board)).toBe("Needs work");
  expect(board.readiness?.blockers).toEqual(["Native DRC: 13 opens"]);
});
it("uses current metrics only while their evidence is valid", () => {
  report.status = "blocked";
  report.metrics = { drc_violations: 13, nets_routed_pct: 93.5, lvs_clean: false, lvs_mismatches: 2 };
  save();
  const board = loadBoard(root);
  expect(board.drc_violations).toBe(13);
  expect(board.nets_routed_pct).toBe(93.5);
  expect(board.lvs_clean).toBe(false);
  writeFileSync(join(root, "output/demo_routed.kicad_pcb"), "changed");
  const stale = loadBoard(root);
  expect(stale.drc_violations).toBe(0);
  expect(stale.readiness?.metrics).toBeUndefined();
  expect(displayStatus(stale)).toBe("unverified");
});
it("refreshes an old partial summary from verified metrics without trusting stale evidence", () => {
  writeFileSync(join(root, "output/board.json"), JSON.stringify({
    $schema: "https://kicad-tools.org/schemas/board/v1.json", schema_version: 1,
    generated_at: "2026-07-09T00:00:00Z", slug: "demo", status: "partial",
    lvs_clean: false, drc_violations: 42, nets_routed_pct: 20,
  }));
  report.metrics = { drc_violations: 0, nets_routed_pct: 100, lvs_clean: true, lvs_mismatches: 0 };
  save();
  const board = loadBoard(root);
  expect(board.status).toBe("ok");
  expect(board.drc_violations).toBe(0);
  expect(board.nets_routed_pct).toBe(100);
  expect(displayStatus(board)).toBe("ready");
  writeFileSync(join(root, "output/demo_routed.kicad_pcb"), "changed");
  const stale = loadBoard(root);
  expect(stale.status).toBe("partial");
  expect(stale.drc_violations).toBe(42);
  expect(displayStatus(stale)).toBe("unverified");
});
it.each(["missing check", "missing project hash", "malformed", "path traversal"])("fails closed: %s", kind => {
  if (kind === "missing check") report.checks.pop();
  if (kind === "missing project hash") delete report.inputs["output/demo.kicad_pro"];
  if (kind === "malformed") report = [];
  if (kind === "path traversal") report.inputs["../outside"] = "0".repeat(64);
  save();
  expect(displayStatus(loadBoard(root))).toBe("unverified");
});


it.each(["missing", "malformed", "unknown schema"])("retains verified readiness with %s board metadata", kind => {
  if (kind === "missing") rmSync(join(root, "output/board.json"));
  if (kind === "malformed") writeFileSync(join(root, "output/board.json"), "{");
  if (kind === "unknown schema") writeFileSync(join(root, "output/board.json"), JSON.stringify({schema_version: 99}));
  // Development evidence is useful independently of a manufacturing export.
  rmSync(join(root, "output/manufacturing"), { recursive: true });
  for (const name of Object.keys(report.inputs)) if (name.includes("manufacturing/")) delete report.inputs[name];
  report.status = "blocked";
  report.blockers = ["Manufacturing export not run"];
  report.checks = [
    { name: "native_erc_clean", status: "passed" },
    { name: "native_drc", status: "failed" },
    { name: "copper_lvs_clean", status: "passed" },
    { name: "manufacturing_release", status: "not_run" },
  ];
  save();
  const board = loadBoard(root);
  expect(board.status).toBe("no_artifacts");
  expect(displayStatus(board)).toBe("development");
  expect(board.readiness?.checks).toEqual(report.checks);
  expect(board.manufacturing_package).toBeUndefined();
  expect(board.drc_violations).toBeUndefined();
  writeFileSync(join(root, "output/demo_routed.kicad_pcb"), "changed");
  const stale = loadBoard(root);
  expect(displayStatus(stale)).toBe("unverified");
  expect(stale.readiness?.checks).toBeUndefined();
});

it.each(["missing", "malformed", "bad hash"])("fails closed on %s readiness without board metadata", kind => {
  rmSync(join(root, "output/board.json"));
  if (kind === "missing") rmSync(join(root, "output/readiness.json"));
  if (kind === "malformed") writeFileSync(join(root, "output/readiness.json"), "{");
  if (kind === "bad hash") { report.inputs["output/demo_routed.kicad_pcb"] = "0".repeat(64); save(); }
  const board = loadBoard(root);
  expect(displayStatus(board)).toBe("unverified");
  expect(board.readiness?.checks).toBeUndefined();
  expect(board.manufacturing_package).toBeUndefined();
});

it("does not promote a missing summary from a ready report", () => {
  rmSync(join(root, "output/board.json"));
  expect(displayStatus(loadBoard(root))).toBe("development");
});
