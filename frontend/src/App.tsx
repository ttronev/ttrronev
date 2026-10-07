import { Suspense } from "react";
import { Route, Routes } from "react-router";
import { AppShell } from "@/components/layout/AppShell";
import { NAV } from "@/lib/nav";
import { PAGES } from "@/pages";
import NotFound from "@/pages/NotFound";

function Loading() {
  return (
    <div className="text-sm text-muted-foreground" data-testid="page-loading">
      loading…
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        {NAV.map((entry) => {
          const Page = PAGES[entry.id];
          if (!Page) throw new Error(`no page for nav entry ${entry.id}`);
          const element = (
            <Suspense fallback={<Loading />}>
              <Page />
            </Suspense>
          );
          return entry.path === "/" ? (
            <Route key={entry.id} index element={element} />
          ) : (
            <Route key={entry.id} path={entry.path.slice(1)} element={element} />
          );
        })}
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
