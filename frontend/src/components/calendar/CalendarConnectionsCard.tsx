import * as React from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
import {
  CALENDAR_CONNECTIONS_KEY,
  deleteCalendarConnection,
  listCalendarConnections,
  startGoogleConnect,
  type CalendarConnection,
} from "@/api/calendars";
import { Button, Pill, Spinner } from "@/components/ui/primitives";
import { SurfaceCard } from "@/components/ui/consoleChrome";

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** Shared by "Connect" and "Reconnect": ask the server for Google's consent URL, then go. */
async function openGoogleConsent(api: ApiClient): Promise<void> {
  const url = await startGoogleConnect(api);
  window.location.assign(url);
}

function ConnectionRow({
  connection,
  googleAvailable,
}: {
  connection: CalendarConnection;
  googleAvailable: boolean;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = React.useState(false);
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function reconnect() {
    setError(null);
    setPending(true);
    try {
      await openGoogleConsent(api);
    } catch (err) {
      setError(`Could not start the Google connection: ${messageOf(err)}`);
    } finally {
      setPending(false);
    }
  }

  async function remove() {
    setError(null);
    setPending(true);
    try {
      await deleteCalendarConnection(api, connection.id);
      await queryClient.invalidateQueries({ queryKey: CALENDAR_CONNECTIONS_KEY });
      setConfirming(false);
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setPending(false);
    }
  }

  // Inline confirm: never window.confirm. "Keep" simply drops the row back to its normal
  // state, which is why the destructive action is the one that needs a second click.
  if (confirming) {
    return (
      <li className="flex flex-wrap items-center gap-[10px] py-[9px]">
        <span className="text-[13.5px]">Remove this calendar? The AI will stop checking it.</span>
        <Button size="sm" variant="destructive" onClick={remove} disabled={pending}>
          Remove
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setConfirming(false)} disabled={pending}>
          Keep
        </Button>
        {error && (
          <p role="alert" className="text-[12px] text-[hsl(var(--cx-danger))]">
            {error}
          </p>
        )}
      </li>
    );
  }

  return (
    <li className="flex flex-wrap items-center gap-[10px] py-[9px]">
      <span className="text-[13.5px] font-medium">Google</span>
      <span className="text-[13.5px] text-[hsl(var(--cx-muted))]">{connection.account_email}</span>
      {connection.status === "active" ? (
        <Pill tone="success">Connected</Pill>
      ) : (
        <Pill tone="warning">Reconnect needed</Pill>
      )}
      {connection.status === "error" && connection.last_error ? (
        <span className="text-[11.5px] text-[hsl(var(--cx-muted))]">{connection.last_error}</span>
      ) : null}
      <span className="ml-auto flex items-center gap-[8px]">
        {connection.status === "error" && googleAvailable ? (
          <Button size="sm" variant="outline" onClick={reconnect} disabled={pending}>
            Reconnect
          </Button>
        ) : null}
        <Button
          size="sm"
          variant="ghost"
          aria-label={`Disconnect ${connection.account_email}`}
          onClick={() => setConfirming(true)}
          disabled={pending}
        >
          Disconnect
        </Button>
      </span>
      {error && (
        <p role="alert" className="w-full text-[12px] text-[hsl(var(--cx-danger))]">
          {error}
        </p>
      )}
    </li>
  );
}

export function CalendarConnectionsCard() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  const { data, isLoading, error } = useQuery({
    queryKey: CALENDAR_CONNECTIONS_KEY,
    queryFn: () => listCalendarConnections(api),
  });

  const [pending, setPending] = React.useState(false);
  const [connectError, setConnectError] = React.useState<string | null>(null);

  const calendar = searchParams.get("calendar");
  const calendarError = searchParams.get("calendar_error");

  React.useEffect(() => {
    if (calendar === "connected") {
      void queryClient.invalidateQueries({ queryKey: CALENDAR_CONNECTIONS_KEY });
    }
  }, [calendar, queryClient]);

  function dismissBanner() {
    const next = new URLSearchParams(searchParams);
    next.delete("calendar");
    next.delete("calendar_error");
    setSearchParams(next, { replace: true });
  }

  async function connect() {
    setConnectError(null);
    setPending(true);
    try {
      await openGoogleConsent(api);
    } catch (err) {
      setConnectError(`Could not start the Google connection: ${messageOf(err)}`);
    } finally {
      setPending(false);
    }
  }

  let banner: React.ReactNode = null;
  if (calendar === "connected" || calendar === "cancelled" || calendar === "error") {
    const isError = calendar === "error";
    const text = isError
      ? `Google Calendar was not connected: ${calendarError ?? "please try again"}`
      : calendar === "connected"
        ? "Google Calendar connected."
        : "Google connection was cancelled.";
    banner = (
      <div
        role={isError ? "alert" : "status"}
        className={
          isError
            ? "mb-[10px] flex items-center justify-between gap-[10px] text-[13px] text-[hsl(var(--cx-danger))]"
            : "mb-[10px] flex items-center justify-between gap-[10px] text-[13px]"
        }
      >
        <span>{text}</span>
        <Button size="sm" variant="ghost" onClick={dismissBanner}>
          Dismiss
        </Button>
      </div>
    );
  }

  return (
    <SurfaceCard>
      {banner}
      <h3 className="text-[13.5px] font-semibold">Calendars</h3>
      <p className="mt-1 text-[13px] text-[hsl(var(--cx-muted))]">
        Ringlite's calendar is always on. Connect Google Calendar so the AI books around your busy
        times and adds each booking to it.
      </p>

      {isLoading ? (
        <Spinner label="Loading calendars" />
      ) : error ? (
        <p role="alert" className="mt-[10px] text-[13px] text-[hsl(var(--cx-danger))]">
          {messageOf(error)}
        </p>
      ) : data ? (
        <div className="mt-[10px] flex flex-col gap-[10px]">
          {data.google_available ? (
            <div>
              <Button size="sm" onClick={connect} disabled={pending}>
                {pending ? "Opening Google…" : "Connect Google Calendar"}
              </Button>
              {connectError && (
                <p role="alert" className="mt-1 text-[12px] text-[hsl(var(--cx-danger))]">
                  {connectError}
                </p>
              )}
            </div>
          ) : (
            <p className="text-[13px] text-[hsl(var(--cx-muted))]">
              Google Calendar connection is not available yet.
            </p>
          )}

          {data.connections.length > 0 && (
            <ul aria-label="Connected calendars" className="divide-y divide-[hsl(var(--cx-line))]">
              {data.connections.map((connection) => (
                <ConnectionRow
                  key={connection.id}
                  connection={connection}
                  googleAvailable={data.google_available}
                />
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </SurfaceCard>
  );
}
