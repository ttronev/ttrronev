import { ApiError } from "@/lib/api";
import { fmtTsSeconds } from "@/lib/desk/format";

/** The offline rule (ТЗ-B0 §3): the page stays up, says "service
 *  unreachable" with the last successful fetch time, and keeps retrying. A
 *  401 is not an outage: it points at Settings instead. */
export function Offline({ error, lastOkAt, retryS = 15 }: { error: Error | null; lastOkAt: Date | null; retryS?: number }) {
  if (!error) return null;
  const unauthorized = error instanceof ApiError && error.status === 401;
  const detail = error instanceof ApiError ? `HTTP ${error.status}` : error.message;
  return (
    <div
      role="alert"
      data-testid="offline"
      data-kind={unauthorized ? "unauthorized" : "unreachable"}
      className="rounded-md border border-status-bad bg-status-bad/10 px-4 py-2 text-sm text-status-bad"
    >
      {unauthorized
        ? "not authorized: the service rejected the API token. Set it in Settings."
        : `service unreachable (${detail}) — last successful fetch ${lastOkAt ? fmtTsSeconds(lastOkAt.toISOString()) : "never"} · retrying every ${retryS} s`}
    </div>
  );
}
