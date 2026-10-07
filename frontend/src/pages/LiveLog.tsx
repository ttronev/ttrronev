// Live log (B6 over B4): one event stream across all bots: bias flips,
// setups, entries, exits, errors, agent actions. Filter by bot, pair, type.
import { useMemo, useState } from "react";
import { PlannedPage } from "@/components/PlannedPage";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { navById } from "@/lib/nav";
import { LIVE_LOG, type LiveLogRow } from "@/mock/planned";

const TYPE_VARIANT: Record<LiveLogRow["type"], "default" | "secondary" | "destructive" | "outline"> = {
  bias_flip: "secondary",
  setup: "outline",
  entry: "default",
  exit: "default",
  error: "destructive",
  agent: "secondary",
};

function Filter({ label, value, options, onChange, testId }: { label: string; value: string; options: string[]; onChange: (v: string) => void; testId: string }) {
  return (
    <label className="inline-flex items-center gap-2 text-sm">
      {label}
      <select data-testid={testId} className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">all</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

export default function LiveLog() {
  const entry = navById("live-log");
  const [bot, setBot] = useState("");
  const [pair, setPair] = useState("");
  const [type, setType] = useState("");
  const uniq = (xs: string[]) => Array.from(new Set(xs)).filter((x) => x !== "—");
  const rows = useMemo(
    () => LIVE_LOG.filter((r) => (!bot || r.bot === bot) && (!pair || r.pair === pair) && (!type || r.type === type)),
    [bot, pair, type],
  );
  return (
    <PlannedPage entry={entry}>
      <div className="flex flex-wrap items-center gap-4">
        <Filter label="bot" value={bot} options={uniq(LIVE_LOG.map((r) => r.bot))} onChange={setBot} testId="filter-bot" />
        <Filter label="pair" value={pair} options={uniq(LIVE_LOG.map((r) => r.pair))} onChange={setPair} testId="filter-pair" />
        <Filter label="type" value={type} options={uniq(LIVE_LOG.map((r) => r.type))} onChange={setType} testId="filter-type" />
        <span className="text-xs text-muted-foreground" data-testid="livelog-count">
          {rows.length} of {LIVE_LOG.length} events
        </span>
      </div>
      <Card>
        <CardContent className="p-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>time (UTC)</TableHead>
                <TableHead>bot</TableHead>
                <TableHead>pair</TableHead>
                <TableHead>type</TableHead>
                <TableHead>event</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r, i) => (
                <TableRow key={i} data-testid="livelog-row">
                  <TableCell className="tabular-nums text-muted-foreground">{r.ts}</TableCell>
                  <TableCell>{r.bot}</TableCell>
                  <TableCell>{r.pair}</TableCell>
                  <TableCell>
                    <Badge variant={TYPE_VARIANT[r.type]}>{r.type}</Badge>
                  </TableCell>
                  <TableCell>{r.message}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </PlannedPage>
  );
}
