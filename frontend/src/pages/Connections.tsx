// Connections (B12 over B7): exchange accounts. Hyperliquid first (API agent
// wallet, trade-only, never withdraw), others via CCXT later. Test
// connection; keys encrypted at rest and never displayed again.
import { PlannedAction, PlannedPage } from "@/components/PlannedPage";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { navById } from "@/lib/nav";
import { CONNECTIONS } from "@/mock/planned";

export default function Connections() {
  const entry = navById("connections");
  const stage = entry.stage ?? "B12";
  return (
    <PlannedPage entry={entry}>
      <div>
        <PlannedAction label="Add connection" stage={stage} />
      </div>
      <Card>
        <CardContent className="p-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>exchange</TableHead>
                <TableHead>label</TableHead>
                <TableHead>permissions</TableHead>
                <TableHead>status</TableHead>
                <TableHead>last test</TableHead>
                <TableHead>actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {CONNECTIONS.map((c) => (
                <TableRow key={c.id} data-testid="connection-row">
                  <TableCell className="font-medium">{c.exchange}</TableCell>
                  <TableCell>{c.label}</TableCell>
                  <TableCell>{c.permissions}</TableCell>
                  <TableCell>
                    <Badge variant={c.status === "ok" ? "secondary" : c.status === "failed" ? "destructive" : "outline"}>{c.status}</Badge>
                  </TableCell>
                  <TableCell className="tabular-nums text-muted-foreground">{c.lastTest}</TableCell>
                  <TableCell>
                    <PlannedAction label="Test connection" stage={stage} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <p className="text-xs text-muted-foreground">Keys are encrypted at rest and never displayed again after entry.</p>
    </PlannedPage>
  );
}
