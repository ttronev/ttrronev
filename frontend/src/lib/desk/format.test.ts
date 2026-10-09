import { describe, expect, it } from "vitest";
import { ageStr, fmtPrice, fmtTs, fmtTsSeconds, pairLabel, priceDecimals } from "@/lib/desk/format";

describe("price formatting (mirrors shared/pricefmt.display_decimals)", () => {
  it("decimals follow the price scale", () => {
    expect(priceDecimals(78419.9)).toBe(1);
    expect(priceDecimals(96.98)).toBe(2);
    expect(priceDecimals(0.9123)).toBe(4);
    expect(priceDecimals(0.0912)).toBe(6);
    expect(priceDecimals(0.000005158)).toBe(9);
    expect(priceDecimals(0)).toBe(2);
    expect(priceDecimals(Number.NaN)).toBe(2);
  });

  it("formats like the Python fmt_price", () => {
    expect(fmtPrice(96.98)).toBe("$96.98");
    expect(fmtPrice(78419.9)).toBe("$78,419.9");
    expect(fmtPrice(0.0912)).toBe("$0.091200");
    expect(fmtPrice(0.000005158)).toBe("$0.000005158");
    expect(fmtPrice(null)).toBe("—");
    expect(fmtPrice(undefined)).toBe("—");
  });
});

describe("timestamps", () => {
  it("minute and second precision, UTC", () => {
    expect(fmtTs("2026-10-06T19:35:12+00:00")).toBe("2026-10-06 19:35 UTC");
    expect(fmtTsSeconds("2026-10-06T19:35:12+00:00")).toBe("2026-10-06 19:35:12 UTC");
    expect(fmtTs(null)).toBe("—");
    expect(fmtTs("garbage")).toBe("garbage");
  });

  it("ages", () => {
    const now = Date.parse("2026-10-06T20:00:00Z");
    expect(ageStr("2026-10-06T19:59:37Z", now)).toBe("23s ago");
    expect(ageStr("2026-10-06T19:48:00Z", now)).toBe("12m ago");
    expect(ageStr("2026-10-06T17:00:00Z", now)).toBe("3.0h ago");
    expect(ageStr(null, now)).toBe("");
  });

  it("pair labels", () => {
    expect(pairLabel("SOL_USDT")).toBe("SOL/USDT");
  });
});
