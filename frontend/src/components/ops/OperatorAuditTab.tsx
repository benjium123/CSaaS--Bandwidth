import * as React from "react";
import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import {
  Button,
  Input,
  Pill,
  Spinner,
  mutationErrorMessage,
} from "@/components/ui/primitives";
import {
  CONSOLE_CELL,
  CONSOLE_CELL_L,
  CONSOLE_CELL_R,
  CONSOLE_HEAD,
  CONSOLE_ROW,
  CONSOLE_TABLE,
  ConsoleEmpty,
  SectionLabel,
  SurfaceCard,
} from "@/components/ui/consoleChrome";
import { cn } from "@/lib/utils";

/** The operator audit log: every state-changing console call, with who made it, which route
 * (parameters filled in), which workspace and what the server answered. Reads need an admin
 * operator - a reviewer gets a 403 that we surface verbatim rather than showing an empty
 * table, so a refused read can never be mistaken for "nothing happened". */

export type OperatorAuditEntry = {
  id: string;
  at: string;
  operator_user_id: string | null;
  operator_email: string | null;
  operator_role: string;
  method: string;
  route: string;
  path_params: Record<string, string> | null;
  org_id: string | null;
  status_code: number;
  reason: string | null;
  ip: string | null;
};

type ConsoleApi = ReturnType<typeof useAuth>["api"];

const AUDIT_PATH = "/api/v1/ops/console/audit";
/** Server default; also the page size the paging controls assume. */
const PAGE_SIZE = 50;
const ORG_ID_PREVIEW_CHARS = 8;

const EMPTY_MARK = "—";

type AuditFilters = {
  q: string;
  org_id: string;
  operator_user_id: string;
};

const NO_FILTERS: AuditFilters = { q: "", org_id: "", operator_user_id: "" };

function auditPath(filters: AuditFilters, before: string | null): string {
  const params = new URLSearchParams();
  if (filters.q !== "") params.set("q", filters.q);
  if (filters.org_id !== "") params.set("org_id", filters.org_id);
  if (filters.operator_user_id !== "") {
    params.set("operator_user_id", filters.operator_user_id);
  }
  if (before !== null && before !== "") params.set("before", before);
  params.set("limit", String(PAGE_SIZE));
  return `${AUDIT_PATH}?${params.toString()}`;
}

async function fetchAudit(
  api: ConsoleApi,
  filters: AuditFilters,
  before: string | null,
): Promise<OperatorAuditEntry[]> {
  const data = await api.request<{ entries?: OperatorAuditEntry[] }>(
    auditPath(filters, before),
  );
  return data.entries ?? [];
}

/** `/orgs/{org_id}/features/{key}` + `{org_id: "abc", key: "fax"}` -> `/orgs/abc/features/fax`.
 * A placeholder with no matching path param is left as-is rather than blanked out. */
function routeWithParams(entry: OperatorAuditEntry): string {
  const params = entry.path_params ?? {};
  return entry.route.replace(/\{([^}]+)\}/g, (placeholder: string, key: string) =>
    key in params ? params[key] : placeholder,
  );
}

function actorLabel(entry: OperatorAuditEntry): string {
  if (entry.operator_role === "ops_token") return "Shared ops token";
  return entry.operator_email ?? EMPTY_MARK;
}

function workspaceLabel(orgId: string | null): string {
  if (orgId == null || orgId === "") return EMPTY_MARK;
  return orgId.slice(0, ORG_ID_PREVIEW_CHARS);
}

export function OperatorAuditTab(): JSX.Element {
  const { api } = useAuth();

  const [draft, setDraft] = React.useState<AuditFilters>(NO_FILTERS);
  const [filters, setFilters] = React.useState<AuditFilters>(NO_FILTERS);
  const [before, setBefore] = React.useState<string | null>(null);

  const auditQuery = useQuery({
    queryKey: ["ops", "console", "audit", filters, before],
    queryFn: () => fetchAudit(api, filters, before),
  });

  const entries = auditQuery.data ?? [];

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setFilters({
      q: draft.q.trim(),
      org_id: draft.org_id.trim(),
      operator_user_id: draft.operator_user_id.trim(),
    });
    // A new search starts at the newest page again.
    setBefore(null);
  }

  return (
    <SurfaceCard className="space-y-[11px]">
      <SectionLabel>Operator audit log</SectionLabel>
      <p className="text-xs text-muted-foreground">
        Every change an operator made through the console, newest first. Admin only.
      </p>

      <form className="flex flex-wrap items-center gap-2" onSubmit={handleSubmit}>
        <Input
          aria-label="Search route"
          placeholder="Search route"
          value={draft.q}
          onChange={(e) => setDraft((prev) => ({ ...prev, q: e.target.value }))}
        />
        <Input
          aria-label="Workspace ID"
          placeholder="Workspace ID"
          value={draft.org_id}
          onChange={(e) => setDraft((prev) => ({ ...prev, org_id: e.target.value }))}
        />
        <Input
          aria-label="Operator ID"
          placeholder="Operator ID"
          value={draft.operator_user_id}
          onChange={(e) =>
            setDraft((prev) => ({ ...prev, operator_user_id: e.target.value }))
          }
        />
        <Button type="submit">Filter</Button>
      </form>

      {auditQuery.isPending ? (
        <Spinner label="Loading audit log" />
      ) : auditQuery.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(auditQuery.error)}
        </p>
      ) : (
        <div className="space-y-[11px]">
          {entries.length > 0 ? (
            <div className="overflow-x-auto">
              <table className={CONSOLE_TABLE}>
                <thead>
                  <tr>
                    <th className={CONSOLE_HEAD}>When</th>
                    <th className={CONSOLE_HEAD}>Who</th>
                    <th className={CONSOLE_HEAD}>Action</th>
                    <th className={CONSOLE_HEAD}>Workspace</th>
                    <th className={cn(CONSOLE_HEAD, "text-right")}>Result</th>
                    <th className={CONSOLE_HEAD}>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((entry) => (
                    <tr key={entry.id} className={CONSOLE_ROW}>
                      <td className={CONSOLE_CELL_L}>
                        {new Date(entry.at).toLocaleString()}
                      </td>
                      <td className={CONSOLE_CELL}>
                        <div className="font-medium">{actorLabel(entry)}</div>
                        <div className="text-[11.5px] text-[hsl(var(--cx-muted))]">
                          {entry.operator_role}
                        </div>
                      </td>
                      <td className={CONSOLE_CELL}>
                        <span className="font-mono text-[12px]">
                          {`${entry.method} ${routeWithParams(entry)}`}
                        </span>
                      </td>
                      <td className={CONSOLE_CELL}>{workspaceLabel(entry.org_id)}</td>
                      <td className={cn(CONSOLE_CELL, "text-right")}>
                        <Pill tone={entry.status_code < 400 ? "success" : "danger"}>
                          {String(entry.status_code)}
                        </Pill>
                      </td>
                      <td className={CONSOLE_CELL_R}>{entry.reason ?? EMPTY_MARK}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <ConsoleEmpty>No operator changes yet</ConsoleEmpty>
          )}

          {entries.length === PAGE_SIZE || before !== null ? (
            <div className="flex flex-wrap items-center gap-2">
              {entries.length === PAGE_SIZE ? (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setBefore(entries[entries.length - 1].at)}
                >
                  Older
                </Button>
              ) : null}
              {before !== null ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setBefore(null)}
                >
                  Newest
                </Button>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
    </SurfaceCard>
  );
}
