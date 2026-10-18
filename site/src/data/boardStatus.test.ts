/** Manufacturing badges require current readiness as well as known DRC/LVS. */
import { describe, expect, it } from "vitest";
import { displayStatus, displayStatusLabel, knownIssueHref } from "./boardStatus.ts";
import type { Board } from "./types.ts";

function makeBoard(overrides: Partial<Board> = {}): Board {
  // Default fixture is assembly-ready with explicit current evidence.
  return {
    $schema: "https://kicad-tools.org/schemas/board/v1.json",
    schema_version: 1,
    generated_at: "2026-06-15T00:00:00+00:00",
    slug: "demo",
    status: "ok",
    category: "demo",
    lvs_clean: true,
    drc_violations: 0,
    readiness: { status: "ready", mode: "assembly", blockers: [] },
    ...overrides,
  };
}

describe("displayStatus", () => {
  it("returns 'ready' for status ok with zero DRC violations and lvs_clean true", () => {
    const b = makeBoard({ status: "ok", drc_violations: 0, lvs_clean: true });
    expect(displayStatus(b)).toBe("ready");
    expect(displayStatusLabel(b)).toBe("Assembly ready");
  });

  it("requires an explicit zero DRC count", () => {
    const b = makeBoard({ status: "ok", drc_violations: undefined });
    expect(displayStatus(b)).toBe("unverified");
    expect(displayStatusLabel(b)).toBe("Readiness unverified");
  });

  it("never returns 'ready' when drc_violations > 0, even for status ok", () => {
    const b = makeBoard({ status: "ok", drc_violations: 3 });
    expect(displayStatus(b)).toBe("drc");
    expect(displayStatusLabel(b)).not.toBe("Assembly ready");
    expect(displayStatusLabel(b)).toBe("3 DRC violations");
  });

  it("singularizes the DRC label for one violation", () => {
    const b = makeBoard({ status: "partial", drc_violations: 1 });
    expect(displayStatus(b)).toBe("drc");
    expect(displayStatusLabel(b)).toBe("1 DRC violation");
  });

  it("returns 'partial' for status partial with zero DRC violations", () => {
    const b = makeBoard({ status: "partial", drc_violations: 0 });
    expect(displayStatus(b)).toBe("partial");
    expect(displayStatusLabel(b)).toBe("Partial");
  });

  it("shows development progress rather than ready for a stub with verified evidence", () => {
    const b = makeBoard({ status: "no_artifacts" });
    expect(displayStatus(b)).toBe("development");
    expect(displayStatusLabel(b)).toBe("In development");
  });

  // ── LVS gates (#3749) ───────────────────────────────────────────────────

  it("returns 'lvs' for lvs_clean false even when status is ok", () => {
    const b = makeBoard({ status: "ok", drc_violations: 0, lvs_clean: false });
    expect(displayStatus(b)).toBe("lvs");
    expect(displayStatusLabel(b)).toBe("LVS mismatch");
  });

  it("includes the LVS mismatch count when lvs_mismatches > 0 (plural)", () => {
    const b = makeBoard({ lvs_clean: false, lvs_mismatches: 3 });
    expect(displayStatus(b)).toBe("lvs");
    expect(displayStatusLabel(b)).toBe("LVS: 3 mismatches");
  });

  it("singularizes the LVS mismatch label for one mismatch", () => {
    const b = makeBoard({ lvs_clean: false, lvs_mismatches: 1 });
    expect(displayStatus(b)).toBe("lvs");
    expect(displayStatusLabel(b)).toBe("LVS: 1 mismatch");
  });

  it("falls back to 'LVS mismatch' when lvs_mismatches is absent or zero", () => {
    const b = makeBoard({ lvs_clean: false, lvs_mismatches: 0 });
    expect(displayStatusLabel(b)).toBe("LVS mismatch");
  });

  it("returns 'unverified' when lvs_clean is absent, even for status ok (regression #3749)", () => {
    // This is the bug board 00 reproduced today: status="ok" + DRC=0 but no
    // LVS verification → chip MUST NOT read "Ready".
    const b = makeBoard({ status: "ok", drc_violations: 0, lvs_clean: undefined });
    expect(displayStatus(b)).toBe("unverified");
    expect(displayStatusLabel(b)).toBe("Readiness unverified");
    expect(displayStatusLabel(b)).not.toBe("Assembly ready");
  });

  it("DRC takes priority over LVS when both fail (DRC is the fabrication blocker)", () => {
    const b = makeBoard({
      status: "ok",
      drc_violations: 5,
      lvs_clean: false,
      lvs_mismatches: 2,
    });
    expect(displayStatus(b)).toBe("drc");
    expect(displayStatusLabel(b)).toBe("5 DRC violations");
  });

  it("DRC takes priority over 'unverified' when LVS is missing but DRC > 0", () => {
    const b = makeBoard({
      status: "ok",
      drc_violations: 1,
      lvs_clean: undefined,
    });
    expect(displayStatus(b)).toBe("drc");
  });
});

describe("known issues (#7261)", () => {
  const entry = (issue: number, kind: "defect" | "exception" | "advisory") =>
    ({ issue, kind, summary: `issue ${issue}`, recorded: "2026-10-18" });

  it("counts disclosed issues on a ready badge", () => {
    const one = makeBoard({ readiness: { status: "ready", mode: "assembly", blockers: [], known_issues: [entry(7220, "advisory")] } });
    expect(displayStatusLabel(one)).toBe("Assembly ready · 1 known issue");
    const two = makeBoard({ readiness: { status: "ready", mode: "assembly", blockers: [],
      known_issues: [entry(6269, "exception"), entry(7220, "advisory")] } });
    expect(displayStatusLabel(two)).toBe("Assembly ready · 2 known issues");
  });

  it("follows the non-ready path for a board blocked by a defect", () => {
    const b = makeBoard({ readiness: { status: "blocked", mode: "assembly",
      blockers: ["known defect #7250: issue 7250"], known_issues: [entry(7250, "defect")] } });
    expect(displayStatus(b)).toBe("blocked");
    expect(displayStatusLabel(b)).toBe("Needs work");
  });

  it("links each issue to the tracker", () => {
    expect(knownIssueHref(7250)).toBe("https://github.com/rjwalters/kicad-tools/issues/7250");
  });
});
