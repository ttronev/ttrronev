// Strategies (B11): list with lifecycle state and headline numbers; a
// strategy page has Overview (scorecard), Versions + strategy log,
// Backtests, Bots using it. New strategy: Template, Builder, or Chat.
import { PlannedAction, PlannedPage } from "@/components/PlannedPage";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { navById } from "@/lib/nav";
import { STRATEGIES, type Lifecycle } from "@/mock/planned";

const LIFECYCLE_VARIANT: Record<Lifecycle, "default" | "secondary" | "destructive" | "outline"> = {
  draft: "outline",
  backtested: "secondary",
  paper: "default",
  live: "destructive",
  retired: "outline",
};

const pct = (x: number | null) => (x === null ? "—" : `${(x * 100).toFixed(0)}%`);
const num = (x: number | null, d = 2, suffix = "") => (x === null ? "—" : `${x.toFixed(d)}${suffix}`);

export default function Strategies() {
  const entry = navById("strategies");
  const stage = entry.stage ?? "B11";
  return (
    <PlannedPage entry={entry}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-muted-foreground">New strategy:</span>
        <PlannedAction label="Template" stage={stage} />
        <PlannedAction label="Builder" stage={stage} />
        <PlannedAction label="Chat" stage="B8" />
      </div>
      <Card>
        <CardContent className="p-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>strategy</TableHead>
                <TableHead>lifecycle</TableHead>
                <TableHead>version</TableHead>
                <TableHead className="text-right">trades</TableHead>
                <TableHead className="text-right">win rate</TableHead>
                <TableHead className="text-right">EV / trade</TableHead>
                <TableHead className="text-right">profit factor</TableHead>
                <TableHead className="text-right">max DD</TableHead>
                <TableHead>updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {STRATEGIES.map((s) => (
                <TableRow key={s.id} data-testid="strategy-row">
                  <TableCell className="font-medium">{s.name}</TableCell>
                  <TableCell>
                    <Badge variant={LIFECYCLE_VARIANT[s.lifecycle]}>{s.lifecycle}</Badge>
                  </TableCell>
                  <TableCell className="tabular-nums">{s.version}</TableCell>
                  <TableCell className="text-right tabular-nums">{s.trades ?? "—"}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(s.winRate)}</TableCell>
                  <TableCell className="text-right tabular-nums">{num(s.evPerTrade, 2, "R")}</TableCell>
                  <TableCell className="text-right tabular-nums">{num(s.profitFactor)}</TableCell>
                  <TableCell className="text-right tabular-nums">{num(s.maxDrawdown, 1, "%")}</TableCell>
                  <TableCell className="text-muted-foreground">{s.updated}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <p className="text-xs text-muted-foreground">
        A strategy page has four parts: Overview (the scorecard), Versions and strategy log, Backtests, Bots using it. Every number
        on it is computed by code; the agent explains and never edits one.
      </p>
    </PlannedPage>
  );
}
