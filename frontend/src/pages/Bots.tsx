// Bots (B12 over B7): a bot is one strategy version + one connection + risk
// limits, in paper, testnet or live mode. Create-bot wizard; a bot page
// shows status, positions, its own live log, pause and kill.
import { PlannedAction, PlannedPage } from "@/components/PlannedPage";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { navById } from "@/lib/nav";
import { BOTS } from "@/mock/planned";

export default function Bots() {
  const entry = navById("bots");
  const stage = entry.stage ?? "B12";
  return (
    <PlannedPage entry={entry}>
      <div>
        <PlannedAction label="Create bot (wizard)" stage={stage} />
      </div>
      <Card>
        <CardContent className="p-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>bot</TableHead>
                <TableHead>strategy version</TableHead>
                <TableHead>connection</TableHead>
                <TableHead>mode</TableHead>
                <TableHead>status</TableHead>
                <TableHead className="text-right">positions</TableHead>
                <TableHead className="text-right">PnL</TableHead>
                <TableHead>since</TableHead>
                <TableHead>actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {BOTS.map((b) => (
                <TableRow key={b.id} data-testid="bot-row">
                  <TableCell className="font-medium">{b.name}</TableCell>
                  <TableCell>{b.strategy}</TableCell>
                  <TableCell>{b.connection}</TableCell>
                  <TableCell>
                    <Badge variant={b.mode === "live" ? "destructive" : b.mode === "testnet" ? "secondary" : "outline"}>{b.mode}</Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={b.status === "running" ? "default" : "outline"}>{b.status}</Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{b.positions}</TableCell>
                  <TableCell className="text-right tabular-nums">{b.pnl}</TableCell>
                  <TableCell className="text-muted-foreground">{b.since}</TableCell>
                  <TableCell className="flex gap-1">
                    <PlannedAction label="pause" stage={stage} />
                    <PlannedAction label="kill" stage={stage} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <p className="text-xs text-muted-foreground">No order is placed by this project today. Execution arrives with B7; bots and connections with B12.</p>
    </PlannedPage>
  );
}
