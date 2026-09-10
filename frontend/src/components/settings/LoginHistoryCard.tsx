import * as React from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
import {
  downloadOrgLoginEventsCsv,
  loginOutcomeLabel,
  loginOutcomeTone,
  useMyLoginEvents,
  useOrgLoginEvents,
  type LoginEventOut,
} from "@/api/identity";
import {
  Button,
  EmptyState,
  Pill,
  Section,
  Select,
  Spinner,
  mutationErrorMessage,
} from "@/components/ui/primitives";
import { relativeTime } from "@/lib/format";

function LoginEventsTable({
  query,
  showWho,
}: {
  query: UseQueryResult<LoginEventOut[]>;
  showWho: boolean;
}) {
  if (query.isPending) return <Spinner label="Loading sign-ins" />;

  if (query.isError) {
    return (
      <div className="space-y-2">
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(query.error)}
        </p>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => {
            void query.refetch();
          }}
        >
          Retry
        </Button>
      </div>
    );
  }

  const rows = query.data ?? [];
  if (rows.length === 0) {
    return <EmptyState title="No sign-ins recorded yet" />;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-border text-xs text-muted-foreground">
            <th className="py-2 pr-4 font-medium">When</th>
            {showWho ? <th className="py-2 pr-4 font-medium">Who</th> : null}
            <th className="py-2 pr-4 font-medium">Outcome</th>
            <th className="py-2 pr-4 font-medium">IP address</th>
            <th className="py-2 font-medium">Details</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((event) => (
            <tr
              key={event.id}
              className="border-b border-border last:border-0"
            >
              <td className="py-2 pr-4" title={event.at}>
                {relativeTime(event.at)}
              </td>
              {showWho ? (
                <td className="py-2 pr-4">{event.email}</td>
              ) : null}
              <td className="py-2 pr-4">
                <Pill tone={loginOutcomeTone(event.outcome)}>
                  {loginOutcomeLabel(event.outcome)}
                </Pill>
              </td>
              <td className="py-2 pr-4">{event.ip ?? "—"}</td>
              <td className="py-2">{event.detail ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MyLoginHistory() {
  const { api } = useAuth();
  const query = useMyLoginEvents(api, 50);

  return (
    <Section
      title="Your recent sign-ins"
      description="These are sign-in attempts on your own account."
    >
      <LoginEventsTable query={query} showWho={false} />
    </Section>
  );
}

function OrgLoginHistory() {
  const { api } = useAuth();
  const gate = useGate();
  const [outcome, setOutcome] = React.useState("");
  const [exportBusy, setExportBusy] = React.useState(false);
  const [exportError, setExportError] = React.useState<string | null>(null);
  // Everyone's sign-in attempts are admin data: the endpoint requires members:read, so
  // without it this panel must not render at all rather than fire a request that 403s.
  const canRead = gate.can("members:read");
  const query = useOrgLoginEvents(api, { limit: 100, outcome, enabled: canRead });

  async function onExport() {
    setExportBusy(true);
    setExportError(null);
    try {
      await downloadOrgLoginEventsCsv(api, 200);
    } catch (err) {
      setExportError(
        err instanceof Error ? err.message : "Something went wrong.",
      );
    } finally {
      setExportBusy(false);
    }
  }

  if (!canRead) return null;

  return (
    <Section
      title="Workspace sign-in history"
      description="These are sign-in attempts by everyone in the workspace."
      actions={
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => {
            void onExport();
          }}
          disabled={exportBusy}
        >
          {exportBusy ? "Exporting..." : "Export CSV"}
        </Button>
      }
    >
      {exportError ? (
        <p role="alert" className="text-sm text-destructive">
          {exportError}
        </p>
      ) : null}

      <Select
        aria-label="Filter by outcome"
        value={outcome}
        onChange={(event) => setOutcome(event.target.value)}
      >
        <option value="">All outcomes</option>
        <option value="ok">{loginOutcomeLabel("ok")}</option>
        <option value="sso">{loginOutcomeLabel("sso")}</option>
        <option value="bad_password">{loginOutcomeLabel("bad_password")}</option>
        <option value="bad_2fa">{loginOutcomeLabel("bad_2fa")}</option>
        <option value="locked">{loginOutcomeLabel("locked")}</option>
        <option value="blocked_ip">{loginOutcomeLabel("blocked_ip")}</option>
      </Select>

      <LoginEventsTable query={query} showWho />
    </Section>
  );
}

export function LoginHistoryCard({ scope }: { scope: "me" | "org" }) {
  return scope === "me" ? <MyLoginHistory /> : <OrgLoginHistory />;
}
