/**
 * Unit tests for the board data loader.
 *
 * These build a temporary `boards/` fixture tree on disk and point the loader
 * at it via the `KCT_BOARDS_DIR` override, exercising:
 *   - missing board.json   → stub with status "no_artifacts"
 *   - valid board.json      → parsed, typed Board
 *   - unknown schema_version → skipped (stub), build does not crash
 *   - external/ descent + hidden/underscore directory skipping
 *   - slug-sorted output
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { discoverBoardDirs, loadBoard, loadBoards } from "./loadBoards.ts";

let root: string;

function makeBoardDir(slug: string): string {
  const dir = join(root, slug);
  mkdirSync(join(dir, "output"), { recursive: true });
  return dir;
}

function writeBoardJson(slug: string, data: unknown): void {
  const dir = makeBoardDir(slug);
  writeFileSync(join(dir, "output", "board.json"), JSON.stringify(data));
}

const validBoard = (slug: string, overrides: Record<string, unknown> = {}) => ({
  $schema: "https://kicad-tools.org/schemas/board/v1.json",
  schema_version: 1,
  generated_at: "2026-06-15T00:00:00+00:00",
  slug,
  status: "ok",
  ...overrides,
});

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "kct-boards-"));
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
  vi.restoreAllMocks();
});

describe("discoverBoardDirs", () => {
  it("returns [] when the boards root does not exist", () => {
    expect(discoverBoardDirs(join(root, "does-not-exist"))).toEqual([]);
  });

  it("lists immediate board dirs and descends into external/", () => {
    makeBoardDir("00-simple-led");
    makeBoardDir("01-voltage-divider");
    mkdirSync(join(root, "external", "softstart", "output"), { recursive: true });

    const dirs = discoverBoardDirs(root).map((d) => d.replace(root + "/", ""));
    expect(dirs).toContain("00-simple-led");
    expect(dirs).toContain("01-voltage-divider");
    expect(dirs).toContain(join("external", "softstart"));
  });

  it("skips hidden and underscore-prefixed directories", () => {
    makeBoardDir("00-simple-led");
    mkdirSync(join(root, ".hidden"), { recursive: true });
    mkdirSync(join(root, "_scratch"), { recursive: true });

    const names = discoverBoardDirs(root).map((d) => d.replace(root + "/", ""));
    expect(names).toEqual(["00-simple-led"]);
  });
});

describe("loadBoard", () => {
  it("synthesizes a no_artifacts stub when board.json is absent", () => {
    const dir = makeBoardDir("00-simple-led");
    const board = loadBoard(dir);
    expect(board.slug).toBe("00-simple-led");
    expect(board.status).toBe("no_artifacts");
    expect(board.schema_version).toBe(1);
  });

  it("parses a valid board.json into a typed Board", () => {
    writeBoardJson(
      "05-bldc",
      validBoard("05-bldc", {
        name: "bldc_controller",
        layer_count: 4,
        board_size_mm: { width: 80, height: 100 },
        nets_routed_pct: 82.1,
        drc_violations: 14,
        cost: { per_board_usd: 9.16, batch_qty: 5, batch_total_usd: 45.78 },
        renders: { pcb_front: "renders/pcb-front.png" },
      }),
    );
    const board = loadBoard(join(root, "05-bldc"));
    expect(board.status).toBe("ok");
    expect(board.name).toBe("bldc_controller");
    expect(board.layer_count).toBe(4);
    expect(board.board_size_mm).toEqual({ width: 80, height: 100 });
    expect(board.cost?.per_board_usd).toBeCloseTo(9.16);
    expect(board.renders?.pcb_front).toBe("renders/pcb-front.png");
  });

  it("skips an unknown schema_version and returns a stub, with a warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    writeBoardJson("99-future", validBoard("99-future", { schema_version: 2 }));
    const board = loadBoard(join(root, "99-future"));
    expect(board.status).toBe("no_artifacts");
    expect(board.schema_version).toBe(1); // stub uses the version we understand
    expect(warn).toHaveBeenCalled();
  });

  it("falls back to a stub on invalid JSON, with a warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const dir = makeBoardDir("bad-json");
    writeFileSync(join(dir, "output", "board.json"), "{ not valid json");
    const board = loadBoard(dir);
    expect(board.status).toBe("no_artifacts");
    expect(warn).toHaveBeenCalled();
  });

  it("falls back to a stub when a required field is missing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    writeBoardJson("missing-status", {
      $schema: "https://kicad-tools.org/schemas/board/v1.json",
      schema_version: 1,
      generated_at: "2026-06-15T00:00:00+00:00",
      slug: "missing-status",
      // status omitted
    });
    const board = loadBoard(join(root, "missing-status"));
    expect(board.status).toBe("no_artifacts");
    expect(warn).toHaveBeenCalled();
  });
});

describe("loadBoards", () => {
  it("returns boards sorted by slug, mixing parsed and stub records", () => {
    writeBoardJson("02-charlieplex", validBoard("02-charlieplex"));
    makeBoardDir("00-simple-led"); // no board.json → stub
    writeBoardJson("01-voltage-divider", validBoard("01-voltage-divider", { status: "partial" }));

    const boards = loadBoards(root);
    expect(boards.map((b) => b.slug)).toEqual([
      "00-simple-led",
      "01-voltage-divider",
      "02-charlieplex",
    ]);
    expect(boards[0]?.status).toBe("no_artifacts");
    expect(boards[1]?.status).toBe("partial");
    expect(boards[2]?.status).toBe("ok");
  });

  it("returns [] for an empty boards root (build must still succeed)", () => {
    expect(loadBoards(root)).toEqual([]);
  });
});

describe("excluded slugs (#3696)", () => {
  it("drops chorus-test-revA from discovery under external/", () => {
    mkdirSync(join(root, "external", "softstart", "output"), { recursive: true });
    mkdirSync(join(root, "external", "chorus-test-revA", "output"), { recursive: true });

    const slugs = discoverBoardDirs(root).map((d) =>
      d.split(/[\\/]/).filter(Boolean).pop(),
    );
    expect(slugs).toContain("softstart");
    expect(slugs).not.toContain("chorus-test-revA");
  });

  it("emits no Board for an excluded slug", () => {
    mkdirSync(join(root, "external", "chorus-test-revA", "output"), { recursive: true });
    writeBoardJson("00-simple-led", validBoard("00-simple-led"));

    const boards = loadBoards(root);
    expect(boards.map((b) => b.slug)).not.toContain("chorus-test-revA");
    expect(boards.map((b) => b.slug)).toContain("00-simple-led");
  });
});

describe("board category (#3696)", () => {
  it("tags numbered top-level boards as demo", () => {
    const board = loadBoard(makeBoardDir("00-simple-led"));
    expect(board.category).toBe("demo");
  });

  it("tags boards under external/ as project", () => {
    mkdirSync(join(root, "external", "softstart", "output"), { recursive: true });
    const board = loadBoard(join(root, "external", "softstart"));
    expect(board.category).toBe("project");
  });

  it("assigns category across a mixed set", () => {
    writeBoardJson("01-voltage-divider", validBoard("01-voltage-divider"));
    mkdirSync(join(root, "external", "softstart", "output"), { recursive: true });
    writeFileSync(
      join(root, "external", "softstart", "output", "board.json"),
      JSON.stringify(validBoard("softstart")),
    );

    const boards = loadBoards(root);
    const bySlug = Object.fromEntries(boards.map((b) => [b.slug, b.category]));
    expect(bySlug["01-voltage-divider"]).toBe("demo");
    expect(bySlug["softstart"]).toBe("project");
  });
});
