/**
 * Unit tests for the display-status helper (issues #3717, #3749).
 *
 * The displayed gallery badge must read "Ready" ONLY when a board has zero
 * DRC violations AND an explicit `lvs_clean === true`. A board with
 * `drc_violations > 0` must never show "Ready". A board with an LVS mismatch
 * (`lvs_clean === false`) must never show "Ready". A board that has not run
 * LVS at all (`lvs_clean === undefined`) must never show "Ready" — the chip
 * reads "LVS not run" instead.
 */
import { describe, expect, it } from "vitest";
import { displayStatus, displayStatusLabel } from "./boardStatus.ts";
import type { Board } from "./types.ts";

function makeBoard(overrides: Partial<Board> = {}): Board {
  // Default fixture is "Ready" — explicitly LVS-clean. Tests for the
  // unverified path opt OUT via `lvs_clean: undefined`.
  return {
    $schema: "https://kicad-tools.org/schemas/board/v1.json",
    schema_version: 1,
    generated_at: "2026-06-15T00:00:00+00:00",
    slug: "demo",
    status: "ok",
    category: "demo",
    lvs_clean: true,
    ...overrides,
  };
}

describe("displayStatus", () => {
  it("returns 'ready' for status ok with zero DRC violations and lvs_clean true", () => {
    const b = makeBoard({ status: "ok", drc_violations: 0, lvs_clean: true });
    expect(displayStatus(b)).toBe("ready");
    expect(displayStatusLabel(b)).toBe("Ready");
  });

  it("returns 'ready' for status ok when drc_violations is absent", () => {
    const b = makeBoard({ status: "ok" });
    expect(displayStatus(b)).toBe("ready");
    expect(displayStatusLabel(b)).toBe("Ready");
  });

  it("never returns 'ready' when drc_violations > 0, even for status ok", () => {
    const b = makeBoard({ status: "ok", drc_violations: 3 });
    expect(displayStatus(b)).toBe("drc");
    expect(displayStatusLabel(b)).not.toBe("Ready");
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

  it("returns 'no_artifacts' for a stub board", () => {
    const b = makeBoard({ status: "no_artifacts" });
    expect(displayStatus(b)).toBe("no_artifacts");
    expect(displayStatusLabel(b)).toBe("No artifacts");
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
    expect(displayStatusLabel(b)).toBe("LVS not run");
    expect(displayStatusLabel(b)).not.toBe("Ready");
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
