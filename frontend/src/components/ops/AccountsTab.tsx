import * as React from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import {
  Button,
  Input,
  Pill,
  Spinner,
  mutationErrorMessage,
} from "@/components/ui/primitives";
import {
  ConsoleEmpty,
  FilterPill,
  InitialsAvatar,
  SectionLabel,
  SurfaceCard,
} from "@/components/ui/consoleChrome";

/** P44: the operator's directory of workspaces - the level the monitor has put each one
 * on, its risk score, and whether it is waiting on a human decision. Read-only here; the
 * decision itself happens on the monitoring tab's case view. */

export const OPS_ACCOUNTS_PATH = "/api/v1/ops/monitoring/accounts";

export type OpsAccount = {
  org_id: string;
  name: string;
  /** "normal" | "watch" | "restricted" | "paused" */
  level: string;
  score: number;
  recommendation: string | null;
  needs_decision: boolean;
  reviewed_at: string | null;
  level_changed_at: string | null;
};

export type OpsAccountsPage = {
  accounts: OpsAccount[];
  total: number;
  limit: number;
  offset: number;
  auto_action: boolean;
};

const PAGE_SIZE = 50;

const LEVEL_TONE: Record<string, "danger" | "warning" | "info" | "neutral"> = {
  paused: "danger",
  restricted: "warning",
  watch: "info",
  normal: "neutral",
};

const LEVEL_FILTERS = [
  { label: "All", value: null as string | null },
  { label: "Normal", value: "normal" as string | null },
  { label: "Watch", value: "watch" as string | null },
  { label: "Restricted", value: "restricted" as string | null },
  { label: "Paused", value: "paused" as string | null },
];

export function useOpsAccounts(params: {
  q: string;
  level: string | null;
  limit: number;
  offset: number;
}): UseQueryResult<OpsAccountsPage> {
  const { api } = useAuth();
  const { q, level, limit, offset } = params;
  return useQuery({
    queryKey: ["ops", "accounts", q, level, limit, offset],
    queryFn: () => {
      const search = new URLSearchParams();
      const trimmed = q.trim();
      if (trimmed) search.set("q", trimmed);
      if (level) search.set("level", level);
      search.set("limit", String(limit));
      search.set("offset", String(offset));
      const qs = search.toString();
      return api.request<OpsAccountsPage>(
        qs ? `${OPS_ACCOUNTS_PATH}?${qs}` : OPS_ACCOUNTS_PATH,
      );
    },
    retry: false,
  });
}

export function AccountsTab({
  onPickOrg,
}: {
  onPickOrg?: (account: OpsAccount) => void;
}): JSX.Element {
  // `text` is what the operator is typing; `applied` is what we actually query with, so a
  // request only fires on submit, never per keystroke.
  const [text, setText] = React.useState("");
  const [applied, setApplied] = React.useState("");
  const [level, setLevel] = React.useState<string | null>(null);
  const [offset, setOffset] = React.useState(0);

  const q = useOpsAccounts({ q: applied, level, limit: PAGE_SIZE, offset });

  const accounts = q.data?.accounts ?? [];
  const total = q.data?.total ?? 0;

  return (
    <div className="space-y-[14px]">
      <SurfaceCard className="space-y-[14px]">
        <SectionLabel>Find a workspace</SectionLabel>
        <form
          className="flex flex-wrap items-center gap-[11px]"
          onSubmit={(e) => {
            e.preventDefault();
            setApplied(text.trim());
            setOffset(0);
          }}
        >
          <Input
            aria-label="Search workspaces"
            placeholder="Search by name"
            value={text}
            onChange={(e) => setText(e.target.value)}
            className="w-full max-w-[280px]"
          />
          <Button type="submit" variant="outline">
            Search
          </Button>
        </form>
        <div
          className="flex flex-wrap gap-[9px]"
          role="group"
          aria-label="Filter by level"
        >
          {LEVEL_FILTERS.map((f) => (
            <FilterPill
              key={f.label}
              active={level === f.value}
              aria-pressed={level === f.value}
              onClick={() => {
                setLevel(f.value);
                setOffset(0);
              }}
            >
              {f.label}
            </FilterPill>
          ))}
        </div>
      </SurfaceCard>

      <SurfaceCard className="space-y-[11px]">
        {q.isPending ? (
          <Spinner label="Loading workspaces" />
        ) : q.isError ? (
          <p role="alert" className="text-sm text-destructive">
            {mutationErrorMessage(q.error)}
          </p>
        ) : accounts.length === 0 ? (
          <ConsoleEmpty>No workspaces match.</ConsoleEmpty>
        ) : (
          <>
            <ul className="space-y-[9px]">
              {accounts.map((a) => {
                const body = (
                  <>
                    <InitialsAvatar name={a.name} seed={a.org_id} size="sm" />
                    <Pill tone={LEVEL_TONE[a.level] ?? "neutral"}>{a.level}</Pill>
                    <span className="font-semibold text-[hsl(var(--cx-text))]">
                      {a.name}
                    </span>
                    <span className="text-[hsl(var(--cx-muted))]">score {a.score}</span>
                    {a.needs_decision ? (
                      <Pill tone="warning">Needs a decision</Pill>
                    ) : null}
                  </>
                );
                return (
                  <li key={a.org_id}>
                    {onPickOrg ? (
                      <button
                        type="button"
                        className="flex w-full flex-wrap items-center gap-[11px] rounded-[var(--cx-r-sm,12px)] px-[11px] py-[9px] text-left text-[13.5px] transition-colors hover:bg-[hsl(var(--cx-overlay))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--cx-accent))]"
                        onClick={() => onPickOrg(a)}
                      >
                        {body}
                      </button>
                    ) : (
                      <div className="flex w-full flex-wrap items-center gap-[11px] rounded-[var(--cx-r-sm,12px)] px-[11px] py-[9px] text-[13.5px]">
                        {body}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
            <div className="flex flex-wrap items-center justify-between gap-[11px]">
              <span className="text-xs text-[hsl(var(--cx-muted))]">
                Showing {offset + 1}-{offset + accounts.length} of {total}
              </span>
              <div className="flex gap-[9px]">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  Previous
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={offset + accounts.length >= total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </SurfaceCard>
    </div>
  );
}
