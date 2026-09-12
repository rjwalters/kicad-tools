/** Build metadata, separate from measurement/report dates. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

export function parseProjectVersion(toml: string): string {
  const project = toml.split(/^\[project\][ \t]*$/m)[1]?.split(/^\[/m)[0];
  const version = project?.match(/^version\s*=\s*["']([^"']+)["']\s*$/m)?.[1];
  if (!version || !/^\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)$/.test(version)) {
    throw new Error("pyproject.toml must declare a valid project.version");
  }
  return version;
}

export const projectVersion = parseProjectVersion(
  readFileSync(resolve(process.cwd(), "..", "pyproject.toml"), "utf8"),
);
export const siteBuiltAt = new Date().toISOString();
