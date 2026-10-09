import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { ApiError, apiGet, apiUrl, maskToken, setApiToken, setServiceUrl } from "@/lib/api";

type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;
type FetchMock = Mock<FetchFn>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function sentHeaders(fetchMock: FetchMock, call: number): Record<string, string> {
  const init = fetchMock.mock.calls[call][1];
  return (init?.headers ?? {}) as Record<string, string>;
}

describe("api client", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("builds same-origin /api/v1 URLs by default", () => {
    expect(apiUrl("/health")).toBe("/api/v1/health");
    expect(apiUrl("health")).toBe("/api/v1/health");
  });

  it("uses the stored service URL without a trailing slash", () => {
    setServiceUrl("http://127.0.0.1:8090/");
    expect(apiUrl("/pairs")).toBe("http://127.0.0.1:8090/api/v1/pairs");
    setServiceUrl(null);
    expect(apiUrl("/pairs")).toBe("/api/v1/pairs");
  });

  it("sends X-API-Key only when a token is stored", async () => {
    const fetchMock: FetchMock = vi.fn<FetchFn>(async () => jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await apiGet("/health");
    expect(sentHeaders(fetchMock, 0)).not.toHaveProperty("X-API-Key");

    setApiToken("secret-token-1234");
    await apiGet("/health");
    expect(sentHeaders(fetchMock, 1)["X-API-Key"]).toBe("secret-token-1234");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/health");
  });

  it("throws ApiError carrying the status on a non-2xx response", async () => {
    vi.stubGlobal("fetch", vi.fn<FetchFn>(async () => jsonResponse({ detail: "nope" }, 401)));
    await expect(apiGet("/health")).rejects.toMatchObject({ name: "ApiError", status: 401 });
    await expect(apiGet("/health")).rejects.toBeInstanceOf(ApiError);
  });

  it("masks the token to its last 4 characters at most", () => {
    expect(maskToken("secret-token-1234")).toBe("••••••••1234");
    expect(maskToken("abcd")).toBe("••••••••");
    expect(maskToken(null)).toBe("");
  });

  it("mock mode resolves endpoints from fixtures without touching fetch", async () => {
    const fetchMock = vi.fn<FetchFn>();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("VITE_MOCK", "1");
    vi.resetModules();
    const mod = await import("@/lib/api");
    expect(mod.MOCK).toBe(true);
    const health = await mod.api.health();
    expect(health.version).toBe("1");
    expect(health.status).toBe("ok");
    const tail = await mod.api.logsTail(2);
    expect(tail.lines).toHaveLength(2);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
