import { describe, expect, it } from "vitest";
import { ADMIN_ENTRIES, MAIN_ENTRIES, NAV, navById, navByPath } from "@/lib/nav";
import { BUNDLED_STAGES, findStage } from "@/lib/stages";

const ORDER = [
  "Desk", "Strategies", "Backtests", "Bots", "Live log", "Connections", "Chat", "Account",
  "Health", "Logs", "Build", "Agent log", "Settings",
];

const PLANNED: Record<string, string> = {
  Strategies: "B11", Backtests: "B11", Bots: "B12", "Live log": "B6",
  Connections: "B12", Chat: "B8", Account: "B13", "Agent log": "B8",
};

describe("sidebar contract (ТЗ-B0 §5)", () => {
  it("has the 13 entries in order", () => {
    expect(NAV.map((e) => e.label)).toEqual(ORDER);
  });

  it("main group first, Admin group of five last", () => {
    expect(MAIN_ENTRIES.map((e) => e.label)).toEqual(ORDER.slice(0, 8));
    expect(ADMIN_ENTRIES.map((e) => e.label)).toEqual(ORDER.slice(8));
    expect(NAV.slice(8).every((e) => e.group === "admin")).toBe(true);
  });

  it("paths are absolute and unique", () => {
    const paths = NAV.map((e) => e.path);
    expect(new Set(paths).size).toBe(paths.length);
    expect(paths.every((p) => p.startsWith("/"))).toBe(true);
  });

  it("live in B0: exactly Desk, Health, Logs, Build, Settings", () => {
    expect(NAV.filter((e) => e.live).map((e) => e.label)).toEqual(["Desk", "Health", "Logs", "Build", "Settings"]);
  });

  it("every planned page names the stage the ТЗ assigns, and that stage exists", () => {
    for (const e of NAV.filter((x) => !x.live)) {
      expect(e.stage, e.label).toBe(PLANNED[e.label]);
      expect(findStage(BUNDLED_STAGES.stages, e.stage!), `${e.label} -> ${e.stage}`).toBeDefined();
    }
    expect(Object.keys(PLANNED)).toHaveLength(8);
  });

  it("lookups", () => {
    expect(navByPath("/admin/health/")?.id).toBe("health");
    expect(navByPath("/")?.id).toBe("desk");
    expect(navByPath("/nope")).toBeUndefined();
    expect(navById("settings").path).toBe("/admin/settings");
    expect(() => navById("nope")).toThrow();
  });
});
