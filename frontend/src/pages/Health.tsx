// Health (live in B0): worker heartbeat per timeframe, pairs and bootstrap
// state, last 5m cycle duration, worker memory, API latency. Green / yellow
// / red is the service's own verdict (/api/v1/health status, the same
// thresholds as the legacy /api/health). Refreshes every 15 s.
import { useRef } from "react";
import { LastUpdated } from "@/components/LastUpdated";
import { Offline } from "@/components/Offline";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";
import { ageStr, fmtTs, pairLabel } from "@/lib/desk/format";
import { navById } from "@/lib/nav";
import type { HealthStatus } from "@/lib/types";
import { useNow } from "@/lib/useNow";
import { usePolled } from "@/lib/usePolled";
import { cn } from "@/lib/utils";

const POLL_MS = 15_000;
const TF_ORDER = ["5m", "1h", "2h", "4h", "1d", "1w"];

const STATUS_STYLE: Record<HealthStatus, { dot: string; label: string }> = {
  ok: { dot: "bg-status-ok", label: "green" },
  degraded: { dot: "bg-status-warn", label: "yellow" },
  down: { dot: "bg-status-bad", label: "red" },
};

function ageLabel(s: number | null | undefined): string {
  if (s === null || s === undefined) return "—";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

function Stat({ label, value, sub, testId }: { label: string; value: React.ReactNode; sub?: string; testId?: string }) {
  return (
    <Card>
      <CardHeader className="p-4 pb-1">
        <CardTitle className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">{label}</CardTitle>
      </CardHeader>
      <CardContent className="p-4 pt-0">
        <div className="text-2xl font-semibold tabular-nums" data-testid={testId}>
          {value}
        </div>
        {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );
}

export default function Health() {
  const entry = navById("health");
  const latencyRef = useRef<number | null>(null);
  const health = usePolled(async (signal) => {
    const t0 = performance.now();
    const h = await api.health(signal);
    latencyRef.current = Math.round(performance.now() - t0);
    return h;
  }, POLL_MS);
  const pairs = usePolled((signal) => api.pairs(signal).then((r) => r.pairs), POLL_MS);
  const now = useNow();
  const h = health.data;
  const status: HealthStatus | null = h ? h.status : health.error ? "down" : null;
  const style = status ? STATUS_STYLE[status] : null;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={entry.label} description={entry.blurb} />
      <Offline error={health.error} lastOkAt={health.lastOkAt} />

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <Card data-testid="health-status" data-status={status ?? "unknown"}>
          <CardHeader className="p-4 pb-1">
            <CardTitle className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">Status</CardTitle>
          </CardHeader>
          <CardContent className="p-4 pt-0">
            <div className="flex items-center gap-2 text-2xl font-semibold">
              <span className={cn("inline-block h-3 w-3 rounded-full", style ? style.dot : "bg-muted")} aria-hidden />
              <span data-testid="health-status-text">{status ?? "…"}</span>
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              {style ? style.label : "waiting"}
              {h?.pairs_stale.length ? ` · stale: ${h.pairs_stale.map(pairLabel).join(", ")}` : ""}
            </div>
          </CardContent>
        </Card>
        <Stat
          label="Worker"
          value={h ? (h.worker_alive ? "alive" : "dead") : "—"}
          sub={h ? `phase: ${h.worker_phase ?? "—"}` : undefined}
          testId="health-worker"
        />
        <Stat label="Last 5m cycle" value={h?.cycle_5m_s != null ? `${h.cycle_5m_s.toFixed(1)} s` : "—"} sub="duration of the last pass" testId="health-cycle" />
        <Stat label="Worker memory" value={h?.rss_mb != null ? `${Math.round(h.rss_mb)} MB` : "—"} sub="peak RSS so far" testId="health-rss" />
        <Stat label="API latency" value={latencyRef.current != null ? `${latencyRef.current} ms` : "—"} sub="this page's health fetch" testId="health-latency" />
        <Stat
          label="Pairs"
          value={h ? `${h.pairs_ready} / ${h.pairs_total}` : "—"}
          sub={h ? `ready / registered · ${h.pairs_stale.length} stale` : undefined}
          testId="health-pairs"
        />
      </div>

      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle className="text-sm">Heartbeat per timeframe</CardTitle>
        </CardHeader>
        <CardContent className="p-4 pt-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>TF</TableHead>
                <TableHead>last regen</TableHead>
                <TableHead>age</TableHead>
                <TableHead>worst pair</TableHead>
                <TableHead>pairs stamped</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {TF_ORDER.map((tf) => {
                const t = h?.tfs[tf];
                const warn = tf === "5m" && h && t?.max_age_s != null && t.max_age_s > h.warn_after_s;
                const bad = tf === "5m" && h && t?.max_age_s != null && t.max_age_s > h.stale_after_s;
                return (
                  <TableRow key={tf} data-testid="tf-row">
                    <TableCell className="font-medium">{tf.toUpperCase()}</TableCell>
                    <TableCell className="tabular-nums">{t?.last_regen_ts ? fmtTs(t.last_regen_ts) : "—"}</TableCell>
                    <TableCell className="tabular-nums">{ageLabel(t?.age_s)}</TableCell>
                    <TableCell className={cn("tabular-nums", bad ? "text-status-bad" : warn ? "text-status-warn" : "")}>
                      {ageLabel(t?.max_age_s)}
                    </TableCell>
                    <TableCell className="tabular-nums">{t ? t.pairs_stamped : "—"}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="p-4 pb-2">
          <CardTitle className="text-sm">Pairs and bootstrap state</CardTitle>
        </CardHeader>
        <CardContent className="p-4 pt-0">
          <Offline error={pairs.error} lastOkAt={pairs.lastOkAt} />
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>pair</TableHead>
                <TableHead>status</TableHead>
                <TableHead>TFs ready</TableHead>
                <TableHead>5m age</TableHead>
                <TableHead>added</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(pairs.data ?? []).map((p) => {
                const age = h?.pair_age_5m_s[p.pair];
                const stale = h?.pairs_stale.includes(p.pair) ?? false;
                return (
                  <TableRow key={p.pair} data-testid="pair-row" data-stale={stale ? "1" : "0"}>
                    <TableCell className="font-medium">{pairLabel(p.pair)}</TableCell>
                    <TableCell>
                      <Badge variant={p.status === "ready" ? "secondary" : p.status === "error" ? "destructive" : "outline"}>{p.status}</Badge>
                      {p.error_reason ? <span className="ml-2 text-xs text-status-bad">{p.error_reason}</span> : null}
                    </TableCell>
                    <TableCell className="tabular-nums">{p.tfs_ready.length} / 6</TableCell>
                    <TableCell className={cn("tabular-nums", stale ? "text-status-bad" : "")}>
                      {age != null ? ageLabel(age) : "—"}
                      {stale ? " · stale" : ""}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">{p.added_ts ? ageStr(p.added_ts, now) : "—"}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <LastUpdated at={health.lastOkAt} note="refreshes every 15 s" />
    </div>
  );
}
