// Logs (live in B0): tail of the worker log (N <= 2000 lines), auto-follow
// toggle, text filter. The service serves its own log file only
// (/api/v1/logs/tail takes nothing but `lines`).
import { useEffect, useMemo, useRef, useState } from "react";
import { LastUpdated } from "@/components/LastUpdated";
import { Offline } from "@/components/Offline";
import { PageHeader } from "@/components/PageHeader";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import { navById } from "@/lib/nav";
import { usePolled } from "@/lib/usePolled";

const POLL_MS = 5_000;
const LINE_OPTIONS = [200, 500, 1000, 2000] as const;

export default function Logs() {
  const entry = navById("logs");
  const [lines, setLines] = useState<number>(500);
  const [follow, setFollow] = useState(true);
  const [filter, setFilter] = useState("");
  const poll = usePolled((signal) => api.logsTail(lines, signal), POLL_MS, [lines]);
  const data = poll.data;
  const shown = useMemo(() => {
    const all = data?.lines ?? [];
    const q = filter.trim().toLowerCase();
    return q ? all.filter((l) => l.toLowerCase().includes(q)) : all;
  }, [data, filter]);
  const preRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (follow && preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight;
  }, [shown, follow]);

  return (
    <div className="flex h-full flex-col gap-4">
      <PageHeader title={entry.label} description={entry.blurb} />
      <Offline error={poll.error} lastOkAt={poll.lastOkAt} retryS={POLL_MS / 1000} />
      <div className="flex flex-wrap items-center gap-4 text-sm">
        <Label className="inline-flex items-center gap-2">
          lines
          <select
            data-testid="log-lines-select"
            className="h-8 rounded-md border border-input bg-background px-2 text-sm"
            value={lines}
            onChange={(e) => setLines(Number(e.target.value))}
          >
            {LINE_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </Label>
        <Label className="inline-flex cursor-pointer items-center gap-2">
          <input type="checkbox" data-testid="log-follow" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          follow
        </Label>
        <Input
          data-testid="log-filter"
          className="w-72"
          placeholder="filter (substring, case-insensitive)"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <span className="text-xs text-muted-foreground" data-testid="log-count">
          {data ? `showing ${shown.length} of ${data.lines.length} line(s) · ${data.file} · ${(data.size_bytes / 1024).toFixed(1)} KB${data.truncated ? " · older lines not shown" : ""}` : "—"}
        </span>
      </div>
      <div
        ref={preRef}
        data-testid="log-lines"
        className="min-h-[300px] flex-1 overflow-auto rounded-md border bg-card p-3 font-mono text-xs leading-5"
      >
        {shown.length === 0 ? (
          <div className="text-muted-foreground">{data ? "no lines" : poll.error ? "" : "loading…"}</div>
        ) : (
          shown.map((l, i) => (
            <div key={i} data-testid="log-line" className="whitespace-pre-wrap break-all">
              {l}
            </div>
          ))
        )}
      </div>
      <LastUpdated at={poll.lastOkAt} note="refreshes every 5 s" />
    </div>
  );
}
