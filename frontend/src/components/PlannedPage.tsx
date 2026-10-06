import { MockBanner } from "@/components/MockBanner";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardContent } from "@/components/ui/card";
import type { NavEntry } from "@/lib/nav";

/** A page whose data arrives in a later stage: header, the mock banner, and
 *  (from the "pages on mock data" layer of B0a) its real layout on fixtures. */
export function PlannedPage({ entry }: { entry: NavEntry }) {
  const stage = entry.stage ?? "?";
  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={entry.label} description={entry.blurb} />
      <MockBanner stage={stage} />
      <Card>
        <CardContent className="pt-6 text-sm text-muted-foreground" data-testid="scaffold-note">
          Scaffold. The page layout on mock data lands later in B0a; real data in stage {stage}.
        </CardContent>
      </Card>
    </div>
  );
}
