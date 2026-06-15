/**
 * Unit tests for the display-status helper (issue #3717).
 *
 * The displayed gallery badge must read "Ready" ONLY when a board has zero DRC
 * violations. A board with `drc_violations > 0` must never show "Ready" — even
 * if its raw `status` field is a stale/over-optimistic "ok".
 */
import { describe, expect, it } from "vitest";
import { displayStatus, displayStatusLabel } from "./boardStatus.ts";
import type { Board } from "./types.ts";

function makeBoard(overrides: Partial<Board> = {}): Board {
  return {
    $schema: "https://kicad-tools.org/schemas/board/v1.json",
    schema_version: 1,
    generated_at: "2026-06-15T00:00:00+00:00",
    slug: "demo",
    status: "ok",
    category: "demo",
    ...overrides,
  };
}

describe("displayStatus", () => {
  it("returns 'ready' for status ok with zero DRC violations", () => {
    const b = makeBoard({ status: "ok", drc_violations: 0 });
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
});
