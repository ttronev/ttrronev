import { describe, expect, it } from "vitest";
import idsFile from "@plan/stage_ids.json";
import { BUNDLED_STAGES, STAGE_STATUSES, mockBannerText, type Stage } from "@/lib/stages";

const FIXTURE: Stage[] = [
  { id: "B11", name: "Strategies + backtests", track: "B", depends_on: [], status: "in_review", evidence_url: null, updated: "2026-10-06" },
];

describe("stages", () => {
  it("banner text is exactly the contract string with the status from the file", () => {
    expect(mockBannerText("B11", FIXTURE)).toBe("Mock data — real data arrives in stage B11 — status: in_review");
  });

  it("an unknown stage reads status: unknown instead of crashing", () => {
    expect(mockBannerText("B99", FIXTURE)).toBe("Mock data — real data arrives in stage B99 — status: unknown");
  });

  it("the bundled stages.json has valid statuses and the committed id set", () => {
    const ids = BUNDLED_STAGES.stages.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect([...ids].sort()).toEqual([...(idsFile as { ids: string[] }).ids].sort());
    for (const s of BUNDLED_STAGES.stages) expect(STAGE_STATUSES).toContain(s.status);
  });
});
