import { expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { contextForReport } from "./benchmarkContext.ts";

const SEPT_12 = [
  "797c4819ea1ea43a2b6710d4ba4ab16e30b45b85562f7d65ee5ae0f2d2097b2e",
  "382b52c36f2e1911e52b7c61ac423078c9cde1d93e51e07337ef2498a16bf764",
  "4bfa2e872bd8351915e266d21bf24f3de42d94c14512ce43542572cd39c19603",
  "2aac01c8809e03aeb6c0e7c3ccd88f6ccad01762d3d8b9519409a6951f806fd8",
];
const SEPT_15 = "7ed834cde4651f59ee371e623e63262c6686e551ee2f0258740025d5252bfacc";
/** Issues cited as current/open on purpose. Keep this short. */
const OPEN_ISSUE_ALLOWLIST = new Set<string>();

it("marks every cited issue as historical or allowlisted", () => {
  const source = readFileSync(new URL("./benchmarkContext.ts", import.meta.url), "utf8");
  const cited = [...source.matchAll(/[^.\n"]*#(\d+)[^.\n"]*/g)];
  expect(cited.length).toBeGreaterThan(0);
  for (const [sentence, issue] of cited) {
    if (OPEN_ISSUE_ALLOWLIST.has(issue)) continue;
    expect(sentence, `#${issue}`).toMatch(/fixed|closed|Later work/);
    expect(sentence, `#${issue}`).not.toMatch(/tracked in|ongoing/);
  }
});
it("gives the 2026-09-15 run its own limitations and runtime", () => {
  const context = contextForReport(SEPT_15)!;
  expect(context.notes.join(" ")).toContain("383 violations");
  expect(context.notes.join(" ")).toContain("U6.9");
  expect(context.notes.join(" ")).toContain("2175.9");
  expect(context.runtime).toContain("2026-09-15");
  expect(context.runtime).not.toMatch(/12 GiB|7\.75/);
});
it("keeps the 2026-09-12 runs bound to their runtime note", () => {
  for (const sha of SEPT_12) expect(contextForReport(sha)?.runtime).toMatch(/^The 2026-09-12 collection ran Linux amd64 through Rosetta/);
  expect(contextForReport("0".repeat(64))).toBeUndefined();
});
