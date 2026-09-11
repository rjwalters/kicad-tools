/** Content-bound manufacturing evidence. Runs only at build time. */
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";
import type { Readiness } from "./types.ts";

export function loadReadiness(boardPath: string): Readiness {
  try {
    const data = JSON.parse(readFileSync(join(boardPath, "output/readiness.json"), "utf8"));
    if (!data || data.schema_version !== 1 || !["assembly", "pcb_only"].includes(data.mode)
      || !["ready", "blocked", "unverified"].includes(data.status)
      || typeof data.checked_at !== "string" || !Number.isFinite(Date.parse(data.checked_at))
      || !Array.isArray(data.blockers) || !data.blockers.every((b: unknown) => typeof b === "string")
      || !data.inputs || typeof data.inputs !== "object" || Array.isArray(data.inputs)
      || Object.keys(data.inputs).length === 0) throw new Error("invalid readiness report");
    const checks = data.checks ?? [];
    const metrics = data.metrics === undefined ? {} : data.metrics;
    if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) throw new Error("invalid current metrics");
    for (const name of ["drc_violations", "lvs_mismatches", "nets_routed_pct"]) {
      const value = metrics[name];
      if (value !== undefined && (typeof value !== "number" || !Number.isFinite(value) || value < 0
        || (name !== "nets_routed_pct" && !Number.isInteger(value))
        || (name === "nets_routed_pct" && value > 100))) throw new Error(`invalid current metric: ${name}`);
    }
    if (metrics.lvs_clean !== undefined && typeof metrics.lvs_clean !== "boolean") throw new Error("invalid current metric: lvs_clean");
    if (!Array.isArray(checks) || checks.some((c: Record<string, unknown>) => !c
      || typeof c.name !== "string" || !["passed", "failed", "not_run"].includes(c.status as string)
      || (c.detail !== undefined && typeof c.detail !== "string"))) throw new Error("invalid checks");
    const root = realpathSync(boardPath);
    for (const [name, expected] of Object.entries(data.inputs)) {
      if (isAbsolute(name) || name.split("/").includes("..") || name.includes("\\")) {
        throw new Error("input path must be board-relative");
      }
      const target = realpathSync(resolve(root, name));
      const rel = relative(root, target);
      if (rel === ".." || rel.startsWith("../") || isAbsolute(rel)) throw new Error("input path leaves board directory");
      const actual = createHash("sha256").update(readFileSync(target)).digest("hex");
      if (actual !== expected) throw new Error(`Evidence is stale: ${name} changed`);
    }
    for (const name of readdirSync(join(root, "output"))) {
      if (statSync(join(root, "output", name)).isFile()
        && (/\.(kicad_pro|kicad_dru)$/.test(name) || name.endsWith("net_class_map.json") || name === "fab_profile.json")
        && !("output/" + name in data.inputs)) throw new Error("Evidence omits current project or rule files");
    }
    if (data.status === "ready") {
      if (!["kct_check", "native_drc", "artifacts", "bom"].every(name =>
        checks.some((c: { name: string }) => c.name === name))) {
        throw new Error("Ready requires kct_check, native_drc, artifacts and bom checks");
      }
      const names = Object.keys(data.inputs);
      if (!names.some(n => n.endsWith(".kicad_pcb")) || !names.some(n => n.endsWith(".kicad_sch"))
        || !("output/manufacturing/manifest.json" in data.inputs)) {
        throw new Error("Ready requires PCB, schematic and manufacturing evidence");
      }
      if (data.blockers.length || checks.some((c: { status: string }) => c.status !== "passed")) {
        throw new Error("Ready conflicts with incomplete or failed checks");
      }
    }
    if (data.status === "unverified") delete data.metrics;
    return data as Readiness;
  } catch (error) {
    const detail = error instanceof Error && !("code" in error)
      ? error.message : "Readiness report or checked files are missing or unreadable.";
    return { status: "unverified", blockers: [`Readiness needs verification: ${detail}`] };
  }
}
