import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import type { ApiClient } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type {
  PlatformMessagingHealthOut,
  PlatformMessagingHealthRow,
} from "@/api/hooks";

// The ops token helpers below are deliberately duplicated from PlatformBillingOps.tsx:
// they are module-private there, and this section only needs the same two small shapes.
const TOKEN_STORAGE_KEY = "csaas.platform.ops.token";
const MESSAGING_HEALTH_PATH = "/api/v1/platform/messaging/health?days=7";

function readStoredOpsToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function writeStoredOpsToken(token: string): void {
  try {
    if (token === "") sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    else sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    // Private mode / storage disabled - the token simply won't persist across reloads.
  }
}

/**
 * 403 first, as in PlatformBillingOps: ApiClient turns a 401 into a whole-app logout, so a
 * mistyped ops token must not sign the operator out of the console.
 */
function isUnauthorizedError(error: unknown): boolean {
  const status = (error as { status?: number } | null)?.status;
  if (status === 403 || status === 401) return true;
  return error instanceof Error && (error.message.includes("403") || error.message.includes("401"));
}

async function opsRequest<T>(
  api: ApiClient,
  token: string,
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const existingHeaders = (init.headers as Record<string, string> | undefined) ?? {};
  const headers: Record<string, string> = {
    ...existingHeaders,
    "X-Platform-Ops-Token": token,
  };
  return api.request<T>(path, { ...init, headers, json: init.json });
}

const LEVEL_LABELS: Record<PlatformMessagingHealthRow["level"], string> = {
  ok: "Healthy",
  warn: "Warning",
  critical: "Critical",
  no_data: "No data",
};

const LEVEL_STYLES: Record<PlatformMessagingHealthRow["level"], string> = {
  ok: "bg-emerald-100 text-emerald-800",
  warn: "bg-amber-100 text-amber-800",
  critical: "bg-red-100 text-red-800",
  no_data: "bg-muted text-muted-foreground",
};

function formatRate(rate: number | null): string {
  return rate == null ? "—" : `${(rate * 100).toFixed(1)}%`;
}

function formatShortDate(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

export function PlatformMessagingHealth() {
  const { api } = useAuth();
  const [token, setToken] = React.useState<string>(readStoredOpsToken);
  const [inputValue, setInputValue] = React.useState("");
  const [unauthorized, setUnauthorized] = React.useState(false);
  const unlocked = token !== "";

  const healthQuery = useQuery({
    queryKey: ["platform-messaging-health", token],
    queryFn: () => opsRequest<PlatformMessagingHealthOut>(api, token, MESSAGING_HEALTH_PATH),
    enabled: unlocked,
  });

  const rejected = unauthorized || (healthQuery.isError && isUnauthorizedError(healthQuery.error));
  const showLocked = !unlocked || rejected;

  React.useEffect(() => {
    if (healthQuery.isError && isUnauthorizedError(healthQuery.error)) {
      writeStoredOpsToken("");
      setToken("");
      setUnauthorized(true);
    }
  }, [healthQuery.isError, healthQuery.error]);

  function handleUnlock(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const next = inputValue.trim();
    if (!next) return;
    writeStoredOpsToken(next);
    setToken(next);
    setUnauthorized(false);
    setInputValue("");
  }

  function handleLock() {
    writeStoredOpsToken("");
    setToken("");
    setUnauthorized(false);
  }

  return (
    <div className="rounded-md border border-border p-4">
      <div className="mb-3 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-medium">Messaging health (all workspaces)</h2>
          <p className="text-sm text-muted-foreground">
            Delivery, spam-block and opt-out rates over the last 7 days, worst first.
          </p>
        </div>
        {showLocked ? null : (
          <Button type="button" variant="outline" onClick={handleLock}>
            Lock
          </Button>
        )}
      </div>

      {showLocked ? (
        <div className="space-y-3">
          {rejected ? (
            <div role="alert" className="text-sm text-destructive">
              That token was not accepted.
            </div>
          ) : null}
          <form className="flex gap-2" onSubmit={handleUnlock}>
            <Input
              aria-label="Platform ops token"
              placeholder="Platform ops token"
              type="password"
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
            />
            <Button type="submit" disabled={!inputValue.trim()}>
              Unlock
            </Button>
          </form>
        </div>
      ) : healthQuery.isLoading ? (
        <Spinner label="Loading messaging health" />
      ) : healthQuery.isError ? (
        <div role="alert" className="text-sm text-destructive">
          {healthQuery.error instanceof Error
            ? healthQuery.error.message
            : "Failed to load messaging health."}
        </div>
      ) : !healthQuery.data ? null : healthQuery.data.rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">No workspaces with text traffic yet.</p>
      ) : (
        // Rows arrive worst-first from the API; the table never re-sorts them.
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-border text-xs text-muted-foreground">
              <th className="py-2 pr-4 font-medium">Workspace</th>
              <th className="py-2 pr-4 font-medium">Level</th>
              <th className="py-2 pr-4 font-medium">Volume</th>
              <th className="py-2 pr-4 font-medium">Delivery</th>
              <th className="py-2 pr-4 font-medium">Spam blocks</th>
              <th className="py-2 pr-4 font-medium">Opt-outs</th>
              <th className="py-2 font-medium">Breached since</th>
            </tr>
          </thead>
          <tbody>
            {healthQuery.data.rows.map((row) => (
              <tr key={row.org_id} className="border-b border-border last:border-0">
                <td className="py-2 pr-4">{row.org_name}</td>
                <td className="py-2 pr-4">
                  <span
                    className={cn(
                      "rounded-full px-2 py-0.5 text-xs font-medium",
                      LEVEL_STYLES[row.level],
                    )}
                  >
                    {LEVEL_LABELS[row.level]}
                  </span>
                </td>
                <td className="py-2 pr-4">{row.volume}</td>
                <td className="py-2 pr-4">{formatRate(row.delivery_rate)}</td>
                <td className="py-2 pr-4">{formatRate(row.spam_block_rate)}</td>
                <td className="py-2 pr-4">{formatRate(row.opt_out_rate)}</td>
                <td className="py-2">{formatShortDate(row.first_breached_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
