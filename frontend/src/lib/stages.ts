// The stage map (docs/plan/stages.json) as types + the one string the shell
// derives from it. The JSON is bundled at build time through the @plan alias;
// the Build page later prefers the live copy from /api/v1/build/stages.
import bundled from "@plan/stages.json";

export const STAGE_STATUSES = ["not_started", "in_progress", "in_review", "blocked", "done"] as const;
export type StageStatus = (typeof STAGE_STATUSES)[number];

export interface Stage {
  id: string;
  name: string;
  track: string;
  depends_on: string[];
  status: StageStatus;
  evidence_url: string | null;
  /** ISO date (YYYY-MM-DD) of the last status change. */
  updated: string;
}

export interface StagesFile {
  updated: string;
  source: string;
  stages: Stage[];
}

export const BUNDLED_STAGES: StagesFile = bundled as StagesFile;

export function findStage(stages: readonly Stage[], id: string): Stage | undefined {
  return stages.find((s) => s.id === id);
}

/** Exact banner text for a page whose data arrives in a later stage (ТЗ-B0 §5). */
export function mockBannerText(stageId: string, stages: readonly Stage[]): string {
  const stage = findStage(stages, stageId);
  const status = stage ? stage.status : "unknown";
  return `Mock data — real data arrives in stage ${stageId} — status: ${status}`;
}
