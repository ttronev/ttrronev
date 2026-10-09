import { Outlet, useLocation } from "react-router";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Sidebar } from "@/components/layout/Sidebar";
import { TopBar } from "@/components/layout/TopBar";
import { Button } from "@/components/ui/button";

export function AppShell() {
  const { pathname } = useLocation();
  return (
    <div className="flex h-full min-h-screen bg-background text-foreground">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main className="flex-1 overflow-auto p-6" data-testid="page">
          {/* keyed by path: a crashed page resets when you navigate away and back */}
          <ErrorBoundary
            key={pathname}
            fallback={(error, reset) => (
              <div className="rounded-lg border border-status-bad bg-status-bad/10 p-4 text-sm" role="alert" data-testid="page-error">
                <div className="font-medium">This page hit an error and was stopped.</div>
                <div className="mt-1 font-mono text-xs text-muted-foreground">{error.message}</div>
                <Button size="sm" variant="outline" className="mt-3" onClick={reset}>
                  Try again
                </Button>
              </div>
            )}
          >
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
