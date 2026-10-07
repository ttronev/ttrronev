// Settings (live in B0): service URL, API token (stored locally, masked in
// the UI), "Test connection", theme follows the system. In B0a the app runs
// in the browser, so both values live in localStorage (the pywebview window
// and %LOCALAPPDATA% settings file are after B0b).
import { useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, DEFAULT_SERVICE_URL, api, apiToken, maskToken, serviceUrl, setApiToken, setServiceUrl } from "@/lib/api";
import { navById } from "@/lib/nav";
import { cn } from "@/lib/utils";

function systemTheme(): "dark" | "light" {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

export default function Settings() {
  const entry = navById("settings");
  const [url, setUrl] = useState(serviceUrl());
  const [token, setToken] = useState("");
  const [stored, setStored] = useState<string | null>(apiToken());
  const [note, setNote] = useState<string | null>(null);
  const [test, setTest] = useState<{ ok: boolean; text: string } | null>(null);
  const [testing, setTesting] = useState(false);

  const save = () => {
    setServiceUrl(url.trim() || null);
    if (token.trim()) setApiToken(token.trim());
    setToken("");
    setStored(apiToken());
    setNote("saved locally");
  };
  const clearToken = () => {
    setApiToken(null);
    setStored(null);
    setToken("");
    setNote("token cleared");
  };
  const testConnection = async () => {
    setTesting(true);
    setTest(null);
    const t0 = performance.now();
    try {
      const v = await api.version();
      setTest({ ok: true, text: `OK · app ${v.app_version} · commit ${v.git_commit} · ${Math.round(performance.now() - t0)} ms` });
    } catch (e) {
      if (e instanceof ApiError) {
        setTest({ ok: false, text: e.status === 401 ? "rejected: the API token is missing or wrong" : `HTTP ${e.status}` });
      } else {
        setTest({ ok: false, text: `unreachable: ${(e as Error).message}` });
      }
    } finally {
      setTesting(false);
    }
  };

  const effective = serviceUrl() || (typeof window !== "undefined" ? `same origin (${window.location.origin})` : "same origin");

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={entry.label} description={entry.blurb} />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Service</CardTitle>
          <CardDescription>
            Where /api/v1 lives. Empty = the page's own origin (how FastAPI serves this app). The dev server proxies to{" "}
            {DEFAULT_SERVICE_URL}.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="grid gap-1.5">
            <Label htmlFor="service-url">Service URL</Label>
            <Input
              id="service-url"
              data-testid="service-url"
              placeholder={DEFAULT_SERVICE_URL}
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
            <div className="text-xs text-muted-foreground" data-testid="service-url-effective">
              in use: {effective}
            </div>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="api-token">API token (X-API-Key)</Label>
            <Input
              id="api-token"
              data-testid="api-token"
              type="password"
              autoComplete="off"
              placeholder={stored ? "enter a new token to replace the stored one" : "not set"}
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
            <div className="text-xs text-muted-foreground" data-testid="api-token-stored">
              stored: {stored ? maskToken(stored) : "none (dev mode works without one)"}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button data-testid="settings-save" onClick={save}>
              Save
            </Button>
            <Button variant="outline" data-testid="settings-clear-token" onClick={clearToken} disabled={!stored}>
              Clear token
            </Button>
            <Button variant="secondary" data-testid="settings-test" onClick={() => void testConnection()} disabled={testing}>
              {testing ? "Testing…" : "Test connection"}
            </Button>
            {note && (
              <span className="text-xs text-muted-foreground" data-testid="settings-note">
                {note}
              </span>
            )}
          </div>
          {test && (
            <div
              data-testid="settings-test-result"
              data-ok={test.ok ? "1" : "0"}
              className={cn("rounded-md border px-3 py-2 text-sm", test.ok ? "border-status-ok text-status-ok" : "border-status-bad text-status-bad")}
            >
              {test.text}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Appearance</CardTitle>
          <CardDescription>The theme follows the system.</CardDescription>
        </CardHeader>
        <CardContent className="text-sm" data-testid="settings-theme">
          currently: {systemTheme()}
        </CardContent>
      </Card>
    </div>
  );
}
