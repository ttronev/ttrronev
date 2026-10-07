// Backtests (B11): pick a strategy version, pairs, period and fee model;
// run; results land on that strategy's scorecard. Every run is kept with
// its config hash and git commit.
import { PlannedAction, PlannedPage } from "@/components/PlannedPage";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { navById } from "@/lib/nav";
import { BACKTESTS, STRATEGIES } from "@/mock/planned";

export default function Backtests() {
  const entry = navById("backtests");
  const stage = entry.stage ?? "B11";
  return (
    <PlannedPage entry={entry}>
      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle className="text-sm">Run a backtest</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 p-4 pt-0 sm:grid-cols-2 lg:grid-cols-4" data-testid="backtest-form">
          <div className="grid gap-1.5">
            <Label>strategy version</Label>
            <select disabled className="h-9 rounded-md border border-input bg-background px-3 text-sm opacity-60">
              {STRATEGIES.map((s) => (
                <option key={s.id}>
                  {s.name} @{s.version}
                </option>
              ))}
            </select>
          </div>
          <div className="grid gap-1.5">
            <Label>pairs</Label>
            <Input disabled defaultValue="SOL, BTC, ETH" />
          </div>
          <div className="grid gap-1.5">
            <Label>period</Label>
            <Input disabled defaultValue="2024-01-01 → 2025-12-31" />
          </div>
          <div className="grid gap-1.5">
            <Label>fee model</Label>
            <select disabled className="h-9 rounded-md border border-input bg-background px-3 text-sm opacity-60">
              <option>taker 0.05%</option>
              <option>taker 0.05% + funding</option>
            </select>
          </div>
          <div className="sm:col-span-2 lg:col-span-4">
            <PlannedAction label="Run" stage={stage} />
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardContent className="p-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>run</TableHead>
                <TableHead>strategy</TableHead>
                <TableHead>pairs</TableHead>
                <TableHead>period</TableHead>
                <TableHead>fees</TableHead>
                <TableHead>status</TableHead>
                <TableHead>result</TableHead>
                <TableHead>config · commit</TableHead>
                <TableHead>ran</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {BACKTESTS.map((b) => (
                <TableRow key={b.id} data-testid="backtest-row">
                  <TableCell className="font-medium">{b.id}</TableCell>
                  <TableCell>{b.strategy}</TableCell>
                  <TableCell>{b.pairs}</TableCell>
                  <TableCell className="tabular-nums">{b.period}</TableCell>
                  <TableCell>{b.fees}</TableCell>
                  <TableCell>
                    <Badge variant={b.status === "done" ? "secondary" : b.status === "failed" ? "destructive" : "default"}>{b.status}</Badge>
                  </TableCell>
                  <TableCell>{b.result}</TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {b.configHash} · {b.commit}
                  </TableCell>
                  <TableCell className="tabular-nums text-muted-foreground">{b.ran}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </PlannedPage>
  );
}
