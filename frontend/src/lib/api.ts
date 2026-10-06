// The /api/v1 client. The shell reads ONLY /api/v1/*: never server files,
// never the un-versioned /api/* routes (those belong to the legacy page).
// No data processing here: fetch, type, hand over.
import type { CandlesV1, HealthV1, LogsTailV1, PairsV1, StateV1, VersionV1 } from "@/lib/types";
import type { StagesFile } from "@/lib/stages";

/** Documented default (ТЗ-B0): the local Docker stack. Under the Vite dev
 *  server /api is proxied there; when FastAPI serves the build the service is
 *  the page's own origin, so the client defaults to same-origin URLs. */
export const DEFAULT_SERVICE_URL = "http://127.0.0.1:8090";

/** True when the build was made with `--mode mock` (or VITE_MOCK=1). */
export const MOCK = import.meta.env.VITE_MOCK === "1";

export const LS_SERVICE_URL = "ttrronev.serviceUrl";
export const LS_API_TOKEN = "ttrronev.apiToken";

function readLocal(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeLocal(key: string, value: string | null): void {
  try {
    if (value === null || value === "") window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private window): settings simply do not persist */
  }
}

/** Base URL of the service; "" means same origin. Trailing slashes stripped. */
export function serviceUrl(): string {
  const override = readLocal(LS_SERVICE_URL);
  return (override ?? "").replace(/\/+$/, "");
}

export function setServiceUrl(url: string | null): void {
  writeLocal(LS_SERVICE_URL, url ? url.trim() : null);
}

export function apiToken(): string | null {
  const t = readLocal(LS_API_TOKEN);
  return t && t.length > 0 ? t : null;
}

export function setApiToken(token: string | null): void {
  writeLocal(LS_API_TOKEN, token ? token.trim() : null);
}

/** Masked for display: never more than the last 4 characters. */
export function maskToken(token: string | null): string {
  if (!token) return "";
  const tail = token.length > 4 ? token.slice(-4) : "";
  return "••••••••" + tail;
}

export class ApiError extends Error {
  readonly status: number;
  readonly url: string;
  constructor(status: number, url: string, message?: string) {
    super(message ?? `HTTP ${status} for ${url}`);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
  }
}

export function apiUrl(path: string): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  return `${serviceUrl()}/api/v1${p}`;
}

export async function apiGet<T>(path: string, init?: { signal?: AbortSignal }): Promise<T> {
  const url = apiUrl(path);
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = apiToken();
  if (token) headers["X-API-Key"] = token;
  const res = await fetch(url, { headers, signal: init?.signal, credentials: "omit" });
  if (!res.ok) throw new ApiError(res.status, url);
  return (await res.json()) as T;
}

async function mockModule() {
  return import("@/mock/api");
}

/** Typed endpoints. In mock mode every call resolves from src/mock. */
export const api = {
  health: (): Promise<HealthV1> => (MOCK ? mockModule().then((m) => m.health()) : apiGet("/health")),
  pairs: (): Promise<PairsV1> => (MOCK ? mockModule().then((m) => m.pairs()) : apiGet("/pairs")),
  state: (pair: string): Promise<StateV1> =>
    MOCK ? mockModule().then((m) => m.state(pair)) : apiGet(`/state/${encodeURIComponent(pair)}`),
  candles: (pair: string, tf: string, limit = 500): Promise<CandlesV1> =>
    MOCK
      ? mockModule().then((m) => m.candles(pair, tf, limit))
      : apiGet(`/candles/${encodeURIComponent(pair)}/${encodeURIComponent(tf)}?limit=${limit}`),
  logsTail: (lines = 500): Promise<LogsTailV1> =>
    MOCK ? mockModule().then((m) => m.logsTail(lines)) : apiGet(`/logs/tail?lines=${lines}`),
  stages: (): Promise<StagesFile> => (MOCK ? mockModule().then((m) => m.stages()) : apiGet("/build/stages")),
  version: (): Promise<VersionV1> => (MOCK ? mockModule().then((m) => m.version()) : apiGet("/version")),
};
