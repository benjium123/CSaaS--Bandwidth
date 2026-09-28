import * as React from "react";
import { QRCodeSVG } from "qrcode.react";
import { useAuth } from "@/auth/AuthContext";
import {
  type SessionOut,
  useCreateDeviceLinkCode,
  useRevokeAllSessions,
  useRevokeSession,
  useSessions,
} from "@/api/identity";
import {
  Button,
  EmptyState,
  MutationStatus,
  Pill,
  Section,
  Spinner,
  mutationErrorMessage,
} from "@/components/ui/primitives";
import { relativeTime } from "@/lib/format";

function deviceLabel(userAgent: string | null): string {
  if (!userAgent) return "Unknown device";
  const ua = userAgent;

  let browser: string | null = null;
  if (ua.includes("Edg")) browser = "Edge";
  else if (ua.includes("Chrome")) browser = "Chrome";
  else if (ua.includes("Firefox")) browser = "Firefox";
  else if (ua.includes("Safari")) browser = "Safari";

  // Order matters: an iPhone/iPad user agent carries "like Mac OS X", and Android carries
  // "Linux". The most specific platform has to be tested first or every phone reads as a
  // desktop.
  let platform: string | null = null;
  if (ua.includes("iPhone")) platform = "iPhone";
  else if (ua.includes("iPad")) platform = "iPad";
  else if (ua.includes("Android")) platform = "Android";
  else if (ua.includes("Windows")) platform = "Windows";
  else if (ua.includes("Mac")) platform = "macOS";
  else if (ua.includes("Linux")) platform = "Linux";

  if (browser && platform) return `${browser} on ${platform}`;
  if (browser) return browser;
  if (platform) return platform;
  return "Unknown device";
}

const APP_KIND_LABEL: Record<string, string> = {
  android: "Ringlite for Android",
  desktop: "Ringlite desktop",
  ios: "Ringlite for iPhone",
};

/** Ringlite apps sign in as device sessions: name them by app, not by user agent. */
function sessionLabel(session: SessionOut): string {
  const app = session.device_kind ? APP_KIND_LABEL[session.device_kind] : undefined;
  if (!app) return deviceLabel(session.user_agent);
  return session.device_name ? `${app} · ${session.device_name}` : app;
}

function LinkDevicePanel({ onClose }: { onClose: () => void }) {
  const { api } = useAuth();
  const create = useCreateDeviceLinkCode(api);
  const [now, setNow] = React.useState(() => Date.now());
  const { mutate } = create;

  React.useEffect(() => {
    mutate();
  }, [mutate]);
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const data = create.data;
  const secondsLeft = data
    ? Math.max(0, Math.round((new Date(data.expires_at).getTime() - now) / 1000))
    : 0;
  const expired = data !== undefined && secondsLeft === 0;

  return (
    <div className="space-y-3 rounded-lg border border-border bg-muted/40 px-3.5 py-3 text-sm">
      <p>
        Open Ringlite on your phone or computer, choose <strong>Link with QR code</strong>, and
        scan this code. It works once and expires in two minutes.
      </p>
      {create.isPending ? (
        <Spinner label="Creating a code" />
      ) : create.isError ? (
        <p role="alert" className="text-destructive">
          {mutationErrorMessage(create.error)}
        </p>
      ) : data && !expired ? (
        <div className="flex flex-wrap items-center gap-4">
          <div className="rounded-md bg-white p-3">
            <QRCodeSVG value={data.qr_payload} size={176} aria-label="Device link QR code" />
          </div>
          <span className="text-muted-foreground">Expires in {secondsLeft}s</span>
        </div>
      ) : (
        <p className="text-muted-foreground">This code expired.</p>
      )}
      <div className="flex gap-2">
        {expired || create.isError ? (
          <Button type="button" size="sm" onClick={() => create.mutate()}>
            New code
          </Button>
        ) : null}
        <Button type="button" size="sm" variant="outline" onClick={onClose}>
          Done
        </Button>
      </div>
    </div>
  );
}

export function SessionsCard() {
  const { api } = useAuth();
  const sessionsQuery = useSessions(api);
  const revokeSession = useRevokeSession(api);
  const revokeAll = useRevokeAllSessions(api);
  const [confirmRevokeAll, setConfirmRevokeAll] = React.useState(false);
  const [linking, setLinking] = React.useState(false);

  React.useEffect(() => {
    if (revokeAll.isSuccess) setConfirmRevokeAll(false);
  }, [revokeAll.isSuccess]);

  const sessions = sessionsQuery.data ?? [];

  return (
    <Section
      title="Active sessions"
      description="These are the devices currently signed in to your account."
    >
      {sessionsQuery.isPending ? (
        <Spinner label="Loading sessions" />
      ) : sessionsQuery.isError ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-destructive">
            {mutationErrorMessage(sessionsQuery.error)}
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              void sessionsQuery.refetch();
            }}
          >
            Retry
          </Button>
        </div>
      ) : sessions.length === 0 ? (
        <EmptyState
          title="No active sessions"
          description="No other devices are signed in to your account."
        />
      ) : (
        <>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/40 text-xs text-muted-foreground">
                  <th className="px-3 py-2.5 font-medium">Device</th>
                  <th className="px-3 py-2.5 font-medium">IP address</th>
                  <th className="px-3 py-2.5 font-medium">Last seen</th>
                  <th className="px-3 py-2.5 font-medium">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {sessions.map((session) => {
                  const label = sessionLabel(session);
                  const isPending =
                    revokeSession.isPending && revokeSession.variables === session.id;

                  return (
                    <tr key={session.id}>
                      <td className="px-3 py-2.5">
                        <span
                          className="flex items-center gap-2"
                          title={session.user_agent ?? undefined}
                        >
                          <span>{label}</span>
                          {session.current ? (
                            <Pill tone="success">This device</Pill>
                          ) : null}
                        </span>
                      </td>
                      <td className="px-3 py-2.5">{session.ip ?? "—"}</td>
                      <td className="px-3 py-2.5">
                        {relativeTime(session.last_seen_at ?? session.created_at)}
                      </td>
                      <td className="px-3 py-2.5 text-right">
                        {session.current ? (
                          "—"
                        ) : (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            aria-label={`Sign out ${label}`}
                            disabled={isPending}
                            onClick={() => revokeSession.mutate(session.id)}
                          >
                            Sign out
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {revokeSession.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {mutationErrorMessage(revokeSession.error)}
            </p>
          ) : null}

          <div className="space-y-2">
            {linking ? (
              <LinkDevicePanel
                onClose={() => {
                  setLinking(false);
                  void sessionsQuery.refetch();
                }}
              />
            ) : (
              <Button type="button" size="sm" onClick={() => setLinking(true)}>
                Link a device
              </Button>
            )}
          </div>

          <div className="space-y-2">
            {confirmRevokeAll ? (
              <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-muted/40 px-3.5 py-3 text-sm">
                <span>
                  This signs out every other device. You stay signed in here.
                </span>
                <Button
                  type="button"
                  size="sm"
                  onClick={() => revokeAll.mutate()}
                  disabled={revokeAll.isPending}
                >
                  Sign out other devices
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => setConfirmRevokeAll(false)}
                >
                  Cancel
                </Button>
              </div>
            ) : (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setConfirmRevokeAll(true)}
              >
                Sign out everywhere
              </Button>
            )}

            <MutationStatus
              pending={revokeAll.isPending}
              error={revokeAll.error}
              success={
                revokeAll.isSuccess
                  ? `Signed out ${revokeAll.data?.revoked ?? 0} other ${
                      revokeAll.data?.revoked === 1 ? "session" : "sessions"
                    }.`
                  : undefined
              }
            />
          </div>
        </>
      )}
    </Section>
  );
}
