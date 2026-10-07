// Account (B13): personal cabinet: profile, notification channels
// (Telegram), app tokens, LLM usage, plan (later), data export.
import { PlannedAction, PlannedPage } from "@/components/PlannedPage";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { navById } from "@/lib/nav";
import { ACCOUNT } from "@/mock/planned";

function Section({ title, children, testId }: { title: string; children: React.ReactNode; testId: string }) {
  return (
    <Card data-testid={testId}>
      <CardHeader className="p-4 pb-2">
        <CardTitle className="text-sm">{title}</CardTitle>
      </CardHeader>
      <CardContent className="p-4 pt-0 text-sm">{children}</CardContent>
    </Card>
  );
}

export default function Account() {
  const entry = navById("account");
  const stage = entry.stage ?? "B13";
  const a = ACCOUNT;
  return (
    <PlannedPage entry={entry}>
      <div className="grid gap-3 lg:grid-cols-2">
        <Section title="Profile" testId="account-profile">
          <div className="grid grid-cols-[120px_1fr] gap-y-1">
            <span className="text-muted-foreground">name</span>
            <span>{a.profile.name}</span>
            <span className="text-muted-foreground">role</span>
            <span>{a.profile.role}</span>
            <span className="text-muted-foreground">since</span>
            <span>{a.profile.since}</span>
          </div>
        </Section>
        <Section title="LLM usage" testId="account-llm">
          <div className="grid grid-cols-[120px_1fr] gap-y-1 tabular-nums">
            <span className="text-muted-foreground">month</span>
            <span>{a.llmUsage.month}</span>
            <span className="text-muted-foreground">requests</span>
            <span>{a.llmUsage.requests}</span>
            <span className="text-muted-foreground">tokens</span>
            <span>{a.llmUsage.tokens.toLocaleString("en-US")}</span>
            <span className="text-muted-foreground">cost</span>
            <span>{a.llmUsage.cost}</span>
          </div>
        </Section>
        <Section title="Notification channels" testId="account-channels">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>channel</TableHead>
                <TableHead>target</TableHead>
                <TableHead>status</TableHead>
                <TableHead>note</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {a.channels.map((c) => (
                <TableRow key={c.type}>
                  <TableCell className="font-medium">{c.type}</TableCell>
                  <TableCell>{c.target}</TableCell>
                  <TableCell>
                    <Badge variant={c.status === "active" ? "secondary" : "outline"}>{c.status}</Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">{c.note || "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Section>
        <Section title="App tokens" testId="account-tokens">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>label</TableHead>
                <TableHead>token</TableHead>
                <TableHead>created</TableHead>
                <TableHead>last used</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {a.tokens.map((t) => (
                <TableRow key={t.label}>
                  <TableCell className="font-medium">{t.label}</TableCell>
                  <TableCell className="font-mono">{t.masked}</TableCell>
                  <TableCell className="text-muted-foreground">{t.created}</TableCell>
                  <TableCell className="text-muted-foreground">{t.lastUsed}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <div className="mt-3 flex gap-2">
            <PlannedAction label="New token" stage={stage} />
            <PlannedAction label="Export my data" stage={stage} />
          </div>
        </Section>
      </div>
    </PlannedPage>
  );
}
