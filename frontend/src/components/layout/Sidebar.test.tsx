import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { Sidebar } from "@/components/layout/Sidebar";
import { NAV } from "@/lib/nav";

describe("Sidebar", () => {
  it("renders the 13 entries in order with their hrefs", () => {
    render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );
    const links = screen.getAllByTestId("nav-entry");
    expect(links.map((l) => l.textContent)).toEqual(NAV.map((e) => e.label));
    expect(links.map((l) => l.getAttribute("href"))).toEqual(NAV.map((e) => e.path));
  });

  it("the Admin group is the last block and holds the last five entries", () => {
    render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );
    const nav = screen.getByTestId("sidebar");
    const admin = screen.getByTestId("sidebar-group-admin");
    expect(nav.lastElementChild).toBe(admin);
    expect(within(admin).getByTestId("sidebar-group-admin-label")).toHaveTextContent("Admin");
    expect(within(admin).getAllByTestId("nav-entry").map((l) => l.textContent)).toEqual([
      "Health", "Logs", "Build", "Agent log", "Settings",
    ]);
  });
});
