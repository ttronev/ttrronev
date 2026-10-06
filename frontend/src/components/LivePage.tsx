import { PageHeader } from "@/components/PageHeader";
import { Card, CardContent } from "@/components/ui/card";
import type { NavEntry } from "@/lib/nav";

/** A page that is live in B0 (reads /api/v1). Until its layer lands it shows
 *  the scaffold note; it never shows the mock banner. */
export function LivePage({ entry, children }: { entry: NavEntry; children?: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={entry.label} description={entry.blurb} />
      {children ?? (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground" data-testid="scaffold-note">
            Scaffold. Live in B0: this page is wired to /api/v1 in a later layer of B0a.
          </CardContent>
        </Card>
      )}
    </div>
  );
}
