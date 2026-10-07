import { describe, expect, it } from "vitest";
import { cssHsl, hslToHex } from "@/components/desk/Chart";

describe("theme tokens for the chart library (hex only)", () => {
  it("converts hsl triplets to hex", () => {
    expect(hslToHex(0, 0, 45.1)).toBe("#737373");
    expect(hslToHex(240, 5.9, 10)).toBe("#18181b"); // shadcn zinc-900
    expect(hslToHex(0, 0, 100)).toBe("#ffffff");
    expect(hslToHex(0, 0, 0)).toBe("#000000");
    expect(hslToHex(217.2, 91.2, 59.8)).toBe("#3b82f6"); // tailwind blue-500
  });

  it("reads a token from the root element", () => {
    const root = document.createElement("div");
    root.style.setProperty("--muted-foreground", "0 0% 45.1%");
    document.body.appendChild(root);
    expect(cssHsl("--muted-foreground", "#898781", root)).toBe("#737373");
    root.remove();
  });

  it("falls back when the token is missing or not a triplet", () => {
    const root = document.createElement("div");
    root.style.setProperty("--border", "red");
    document.body.appendChild(root);
    expect(cssHsl("--border", "#2c2c2a", root)).toBe("#2c2c2a");
    expect(cssHsl("--nope", "#2c2c2a", root)).toBe("#2c2c2a");
    expect(cssHsl("--nope", "#2c2c2a", null)).toBe("#2c2c2a");
    root.remove();
  });
});
