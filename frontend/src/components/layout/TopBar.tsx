import { useLocation } from "react-router";
import { Badge } from "@/components/ui/badge";
import { ApiError, MOCK } from "@/lib/api";
import { navByPath } from "@/lib/nav";
import { useHealth } from "@/lib/useHealth";

/** Page title + global pills: mock mode, the persistent "dev mode, no auth"
 *  banner (ТЗ-B0 §5 Auth) and a token hint when the service answers 401.
 *  The health dot itself lands with the Health page. */
export function TopBar() {
  const { pathname } = useLocation();
  const entry = navByPath(pathname);
  const { health, error } = useHealth();
  const devMode = health?.auth_enabled === false;
  const unauthorized = error instanceof ApiError && error.status === 401;

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b bg-background px-6">
      <div className="text-sm font-medium" data-testid="topbar-title">
        {entry ? entry.label : "ttrronev"}
      </div>
      <div className="ml-auto flex items-center gap-2">
        {MOCK && (
          <Badge
            variant="secondary"
            data-testid="mock-mode-pill"
            title="The client serves fixtures from src/mock; no service is called."
          >
            mock mode
          </Badge>
        )}
        {devMode && (
          <Badge
            variant="outline"
            data-testid="dev-mode-banner"
            className="border-status-warn text-status-warn"
            title="TTRRONEV_API_KEY is unset on the service: /api/v1 accepts every request."
          >
            dev mode, no auth
          </Badge>
        )}
        {unauthorized && (
          <Badge variant="destructive" data-testid="auth-required-pill" title="The service rejected the API token.">
            token required: set it in Settings
          </Badge>
        )}
      </div>
    </header>
  );
}
