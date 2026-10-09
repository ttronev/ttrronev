// Build (live in B0): the stage map from docs/plan/stages.json (served live
// by /api/v1/build/stages; the bundled snapshot is the fallback) plus a
// "Now" strip listing the stages in progress.
import { LastUpdated } from "@/components/LastUpdated";
import { Offline } from "@/components/Offline";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";
import { navById } from "@/lib/nav";
import { BUNDLED_STAGES, type StageStatus } from "@/lib/stages";
import { usePolled } from "@/lib/usePolled";

const POLL_MS = 60_000;

const STATUS_VARIANT: Record<StageStatus, "default" | "secondary" | "destructive" | "outline"> = {
  not_started: "outline",
  in_progress: "default",
  in_review: "secondary",
  blocked: "destructive",
  done: "secondary",
};

export default function Build() {
  const entry = navById("build");
  const poll = usePolled((signal) => api.stages(signal), POLL_MS);
  const file = poll.data ?? BUNDLED_STAGES;
  const source = poll.data ? "live from the service" : "bundled snapshot";
  const now = file.stages.filter((s) => s.status === "in_progress");

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={entry.label} description={entry.blurb} />
      <Offline error={poll.error} lastOkAt={poll.lastOkAt} retryS={POLL_MS / 1000} />

      <Card data-testid="now-strip">
        <CardHeader className="p-4 pb-2">
          <CardTitle className="text-sm">Now</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2 p-4 pt-0">
          {now.length === 0 ? (
            <span className="text-sm text-muted-foreground">nothing in progress</span>
          ) : (
            now.map((s) => (
              <Badge key={s.id} data-testid="now-stage">
                {s.id} · {s.name}
              </Badge>
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>stage</TableHead>
                <TableHead>name</TableHead>
                <TableHead>track</TableHead>
                <TableHead>status</TableHead>
                <TableHead>depends on</TableHead>
                <TableHead>evidence</TableHead>
                <TableHead>updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {file.stages.map((s) => (
                <TableRow key={s.id} data-testid="stage-row" data-status={s.status}>
                  <TableCell className="font-medium">{s.id}</TableCell>
                  <TableCell>{s.name}</TableCell>
                  <TableCell>{s.track}</TableCell>
                  <TableCell>
                    <Badge variant={STATUS_VARIANT[s.status]}>{s.status}</Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">{s.depends_on.length ? s.depends_on.join(", ") : "—"}</TableCell>
                  <TableCell>
                    {s.evidence_url ? (
                      <a href={s.evidence_url} target="_blank" rel="noopener noreferrer" className="underline underline-offset-2">
                        link
                      </a>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="tabular-nums text-muted-foreground">{s.updated}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <LastUpdated at={poll.lastOkAt} note={`${source} · stage map updated ${file.updated}`} />
    </div>
  );
}
