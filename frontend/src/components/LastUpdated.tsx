import { ageStr, fmtTsSeconds } from "@/lib/desk/format";
import { useNow } from "@/lib/useNow";

/** Per-page "last updated" line (ТЗ-B0 §5 Errors). */
export function LastUpdated({ at, note }: { at: Date | null; note?: string }) {
  const now = useNow();
  return (
    <div className="text-xs text-muted-foreground" data-testid="last-updated">
      {at ? `updated ${fmtTsSeconds(at.toISOString())} (${ageStr(at.toISOString(), now)})` : "not fetched yet"}
      {note ? ` · ${note}` : ""}
    </div>
  );
}
