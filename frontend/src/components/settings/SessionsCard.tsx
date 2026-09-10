import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import {
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

export function SessionsCard() {
  const { api } = useAuth();
  const sessionsQuery = useSessions(api);
  const revokeSession = useRevokeSession(api);
  const revokeAll = useRevokeAllSessions(api);
  const [confirmRevokeAll, setConfirmRevokeAll] = React.useState(false);

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
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-muted-foreground">
                  <th className="py-2 pr-4 font-medium">Device</th>
                  <th className="py-2 pr-4 font-medium">IP address</th>
                  <th className="py-2 pr-4 font-medium">Last seen</th>
                  <th className="py-2 font-medium">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((session) => {
                  const label = deviceLabel(session.user_agent);
                  const isPending =
                    revokeSession.isPending && revokeSession.variables === session.id;

                  return (
                    <tr
                      key={session.id}
                      className="border-b border-border last:border-0"
                    >
                      <td className="py-2 pr-4">
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
                      <td className="py-2 pr-4">{session.ip ?? "—"}</td>
                      <td className="py-2 pr-4">
                        {relativeTime(session.last_seen_at ?? session.created_at)}
                      </td>
                      <td className="py-2 text-right">
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
            {confirmRevokeAll ? (
              <div className="flex items-center gap-3 text-sm">
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
