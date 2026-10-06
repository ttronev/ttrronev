import { useLocation } from "react-router";
import { Badge } from "@/components/ui/badge";
import { MOCK } from "@/lib/api";
import { navByPath } from "@/lib/nav";

/** Page title + global pills. The health dot and the "dev mode, no auth"
 *  banner arrive with /api/v1 (later layers of B0a). */
export function TopBar() {
  const { pathname } = useLocation();
  const entry = navByPath(pathname);
  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b bg-background px-6">
      <div className="text-sm font-medium" data-testid="topbar-title">
        {entry ? entry.label : "ttrronev"}
      </div>
      {MOCK && (
        <Badge variant="secondary" data-testid="mock-mode-pill" title="The client serves fixtures from src/mock; no service is called.">
          mock mode
        </Badge>
      )}
    </header>
  );
}
