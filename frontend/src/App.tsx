import { Route, Routes } from "react-router";
import { AppShell } from "@/components/layout/AppShell";
import { NAV } from "@/lib/nav";
import { PAGES } from "@/pages";
import NotFound from "@/pages/NotFound";

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        {NAV.map((entry) => {
          const Page = PAGES[entry.id];
          if (!Page) throw new Error(`no page for nav entry ${entry.id}`);
          return entry.path === "/" ? (
            <Route key={entry.id} index element={<Page />} />
          ) : (
            <Route key={entry.id} path={entry.path.slice(1)} element={<Page />} />
          );
        })}
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
