import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MockBanner } from "@/components/MockBanner";
import type { Stage } from "@/lib/stages";

const STAGES: Stage[] = [
  { id: "B6", name: "Candidate cards", track: "B", depends_on: [], status: "blocked", evidence_url: null, updated: "2026-10-06" },
];

describe("MockBanner", () => {
  it("shows the contract text with the status from the given stages", () => {
    render(<MockBanner stage="B6" stages={STAGES} />);
    const banner = screen.getByTestId("mock-banner");
    expect(banner).toHaveTextContent("Mock data — real data arrives in stage B6 — status: blocked");
    expect(banner).toHaveAttribute("data-stage", "B6");
    expect(banner).toHaveAttribute("role", "alert");
  });
});
