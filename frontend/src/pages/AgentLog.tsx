// Agent log (B8, admin): what the agents proposed, ran and were refused.
// Agents propose; the owner approves every promotion (the standing rule).
import { PlannedPage } from "@/components/PlannedPage";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { navById } from "@/lib/nav";
import { AGENT_LOG, type AgentLogRow } from "@/mock/planned";

const STATUS_VARIANT: Record<AgentLogRow["status"], "default" | "secondary" | "destructive" | "outline"> = {
  proposed: "default",
  ran: "secondary",
  refused: "outline",
  vetoed: "destructive",
};

export default function AgentLog() {
  const entry = navById("agent-log");
  return (
    <PlannedPage entry={entry}>
      <Card>
        <CardContent className="p-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>time (UTC)</TableHead>
                <TableHead>agent</TableHead>
                <TableHead>action</TableHead>
                <TableHead>result</TableHead>
                <TableHead>status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {AGENT_LOG.map((r, i) => (
                <TableRow key={i} data-testid="agentlog-row">
                  <TableCell className="tabular-nums text-muted-foreground">{r.ts}</TableCell>
                  <TableCell className="font-medium">{r.agent}</TableCell>
                  <TableCell>{r.action}</TableCell>
                  <TableCell>{r.result}</TableCell>
                  <TableCell>
                    <Badge variant={STATUS_VARIANT[r.status]}>{r.status}</Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <p className="text-xs text-muted-foreground">Agents propose only. Nothing here changes the service without the owner's approval.</p>
    </PlannedPage>
  );
}
