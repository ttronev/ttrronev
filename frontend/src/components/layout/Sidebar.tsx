import { NavLink } from "react-router";
import { ADMIN_ENTRIES, ADMIN_GROUP_LABEL, MAIN_ENTRIES, type NavEntry } from "@/lib/nav";
import { cn } from "@/lib/utils";

function Entry({ entry }: { entry: NavEntry }) {
  return (
    <li>
      <NavLink
        to={entry.path}
        end={entry.path === "/"}
        data-testid="nav-entry"
        data-nav-id={entry.id}
        className={({ isActive }) =>
          cn(
            "flex items-center rounded-md px-3 py-2 text-sm font-medium transition-colors",
            "hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring",
            isActive ? "bg-sidebar-accent text-sidebar-accent-foreground" : "text-sidebar-foreground",
          )
        }
      >
        {entry.label}
      </NavLink>
    </li>
  );
}

/** Thirteen entries in the contract order; the Admin group (operator only) is last. */
export function Sidebar() {
  return (
    <nav
      aria-label="Primary"
      data-testid="sidebar"
      className="flex h-full w-56 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground"
    >
      <div className="px-5 pb-3 pt-5">
        <div className="text-base font-semibold tracking-tight text-foreground">ttrronev</div>
        <div className="text-xs text-muted-foreground">structure monitor</div>
      </div>
      <ul className="flex flex-col gap-0.5 px-2" data-testid="sidebar-group-main">
        {MAIN_ENTRIES.map((e) => (
          <Entry key={e.id} entry={e} />
        ))}
      </ul>
      <div className="mt-auto px-2 pb-3" data-testid="sidebar-group-admin">
        <div
          className="px-3 pb-1 pt-4 text-xs font-semibold uppercase tracking-wider text-muted-foreground"
          data-testid="sidebar-group-admin-label"
        >
          {ADMIN_GROUP_LABEL}
        </div>
        <ul className="flex flex-col gap-0.5">
          {ADMIN_ENTRIES.map((e) => (
            <Entry key={e.id} entry={e} />
          ))}
        </ul>
      </div>
    </nav>
  );
}
