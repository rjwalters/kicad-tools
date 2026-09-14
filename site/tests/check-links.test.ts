import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { expect, test } from "vitest";

function check(files: Record<string, string>) {
  const dist = mkdtempSync(join(tmpdir(), "site-links-"));
  try {
    for (const [name, html] of Object.entries(files)) {
      const path = join(dist, name);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, html);
    }
    return spawnSync(process.execPath, [resolve("scripts/check-links.mjs"), `--dist=${dist}`], { encoding: "utf8" });
  } finally {
    rmSync(dist, { recursive: true, force: true });
  }
}

test.each(["missing.html", "missing.png", "./missing.html", "../missing.html", "/missing/"])("rejects missing target %s", (url) => {
  const result = check({ "guide/index.html": `<a href="${url}">link</a>` });
  expect(result.status).toBe(1);
  expect(result.stderr).toContain("Broken internal link");
});

test("resolves bare, dot, parent, query and fragment references against the source page", () => {
  const result = check({
    "guide/index.html": '<a href="next.html">bare</a><a href="./next.html?x=1#ok">dot</a><a href="../index.html#home">parent</a><img src="image.png"><a href="?x=1#local">query</a><div id="local"></div>',
    "guide/next.html": '<div id="ok"></div>',
    "guide/image.png": "asset",
    "index.html": '<div id="home"></div>',
  });
  expect(result.status, result.stderr).toBe(0);
});

test("a root file cannot mask a missing nested relative target", () => {
  const result = check({ "guide/index.html": '<a href="./next.html">link</a>', "next.html": "wrong location" });
  expect(result.status).toBe(1);
});

test("rejects an empty build", () => {
  const result = check({});
  expect(result.status).toBe(1);
  expect(result.stderr).toContain("no built HTML");
});

test("checks fragments on relative target pages", () => {
  const result = check({ "guide/index.html": '<a href="next.html#missing">link</a>', "guide/next.html": '<div id="present"></div>' });
  expect(result.status).toBe(1);
  expect(result.stderr).toContain("Unresolved fragment");
});

test("resolves encoded fragments and dotted directory routes", () => {
  const result = check({ "index.html": '<a href="/release.v1/#some%20id">link</a>', "release.v1/index.html": '<div id="some id"></div>' });
  expect(result.status, result.stderr).toBe(0);
});
