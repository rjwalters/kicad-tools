import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { parseProjectVersion, projectVersion, siteBuiltAt } from "./siteMetadata.ts";

describe("site metadata", () => {
  it("reads the repository project version, not another table's version", () => {
    const source = readFileSync(new URL("../../../pyproject.toml", import.meta.url), "utf8");
    expect(projectVersion).toBe(parseProjectVersion(source));
    expect(projectVersion).toMatch(/^\d+\.\d+\.\d+/);
    expect(parseProjectVersion('[tool.other]\nversion="9.9.9"\n[project]\nversion="1.2.3"\n[other]\nversion="8.8.8"')).toBe("1.2.3");
  });
  it("fails visibly when the project version is absent or malformed", () => {
    for (const source of ['[tool.other]\nversion="1.2.3"', '[project]\nname="x"\n[other]\nversion="1.2.3"', '[project]\nversion=""']) {
      expect(() => parseProjectVersion(source)).toThrow();
    }
  });
  it("records a parseable build timestamp independently of report metadata", () => {
    expect(new Date(siteBuiltAt).toISOString()).toBe(siteBuiltAt);
  });
});
