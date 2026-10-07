import type { ReactNode } from "react";
import { MockBanner } from "@/components/MockBanner";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardContent } from "@/components/ui/card";
import type { NavEntry } from "@/lib/nav";

/** A page whose data arrives in a later stage: header, the mock banner, and
 *  its real layout on fixtures (src/mock/planned.ts). Actions are rendered
 *  disabled: they arrive with the stage. */
export function PlannedPage({ entry, children }: { entry: NavEntry; children?: ReactNode }) {
  const stage = entry.stage ?? "?";
  return (
    <div className="flex flex-col gap-4" data-testid="planned-page" data-stage={stage}>
      <PageHeader title={entry.label} description={entry.blurb} />
      <MockBanner stage={stage} />
      {children ?? (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground" data-testid="scaffold-note">
            Scaffold. Real data in stage {stage}.
          </CardContent>
        </Card>
      )}
    </div>
  );
}

/** A disabled action with the stage that brings it. */
export function PlannedAction({ label, stage }: { label: string; stage: string }) {
  return (
    <button
      type="button"
      disabled
      data-testid="planned-action"
      title={`arrives in stage ${stage}`}
      className="inline-flex h-8 items-center rounded-md border border-input px-3 text-xs font-medium text-muted-foreground opacity-60"
    >
      {label}
    </button>
  );
}
