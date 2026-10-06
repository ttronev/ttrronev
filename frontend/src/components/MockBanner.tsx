import { Alert, AlertDescription } from "@/components/ui/alert";
import { BUNDLED_STAGES, mockBannerText, type Stage } from "@/lib/stages";

/** "Mock data — real data arrives in stage Bx — status: <from stages.json>".
 *  The status comes from the stages.json snapshot bundled at build time. */
export function MockBanner({ stage, stages = BUNDLED_STAGES.stages }: { stage: string; stages?: readonly Stage[] }) {
  return (
    <Alert variant="warning" data-testid="mock-banner" data-stage={stage}>
      <AlertDescription>{mockBannerText(stage, stages)}</AlertDescription>
    </Alert>
  );
}
