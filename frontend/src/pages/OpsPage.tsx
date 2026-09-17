import * as React from "react";
import { DecisionPackCard, type DecisionPack } from "@/components/ops/DecisionPackCard";
import { MonitoringTab } from "@/components/ops/MonitoringTab";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAuthedBlob } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import {
  Button,
  Card,
  Input,
  Pill,
  Section,
  Select,
  Spinner,
  Tabs,
  TabPanel,
  Textarea,
  mutationErrorMessage,
} from "@/components/ui/primitives";

/** P41 operator console: review businesses, handle security alerts, keep the ban list.
 * P42: account support (unlock, reset 2FA, deactivate). */

type QueueItem = {
  org_id: string;
  org_name: string;
  legal_name: string | null;
  country: string | null;
  status: string;
  risk_tier: string | null;
  risk_reasons: string[];
  video_call_required: boolean;
  video_call_done: boolean;
  use_case_change_pending: boolean;
  submitted_at: string | null;
};

type Check = { result: string; summary: string; detail: Record<string, unknown> | null; at: string | null; manual: boolean };

type Application = {
  org: { id: string; name: string; slug: string };
  status: string;
  business: Record<string, unknown>;
  use_case: Record<string, unknown> | null;
  use_case_pending: Record<string, unknown> | null;
  risk: {
    tier: string | null;
    reasons: string[];
    submitted_from_flagged_login: boolean;
    video_call_required: boolean;
    video_call_done_at: string | null;
    video_call_note: string | null;
  };
  persons: {
    id: string;
    role: string;
    full_name: string;
    email: string | null;
    ownership_percent: number | null;
    status: string;
    verified_name: string | null;
    document_type: string | null;
    document_country: string | null;
  }[];
  documents: {
    id: string;
    kind: string;
    filename: string;
    content_type: string;
    size_bytes: number;
    person_id?: string | null;
    review_result?: string | null;
    review?: { reasons?: string[]; read?: Record<string, unknown> } | null;
  }[];
  checks: Record<string, Check>;
  approval_blockers: string[];
  deposit_required_cents: number | null;
  limits: Record<string, number> | null;
  info_request: string | null;
  suspension_reason: string | null;
};

type Alert = { id: string; kind: string; status: string; detail: Record<string, unknown> | null; created_at: string | null };
type Ban = { id: string; kind: string; hint: string | null; reason: string; added_by: string | null; added_at: string | null };

const RESULT_TONE: Record<string, "success" | "warning" | "danger" | "neutral" | "info"> = {
  pass: "success",
  warn: "warning",
  fail: "danger",
  error: "danger",
  pending: "info",
};

const CHECK_LABELS: Record<string, string> = {
  registry: "Business registry",
  sanctions: "Sanctions lists",
  ban_list: "Ban list",
  website: "Website & domain",
  email_domain: "Email domain",
  name_match: "Names on IDs",
  ai_summary: "AI reviewer summary",
  documents: "Documents (AI-read)",
};

function useOps<T>(key: unknown[], path: string, enabled = true) {
  const { api } = useAuth();
  return useQuery({ queryKey: ["ops", ...key], queryFn: () => api.request<T>(path), enabled });
}

function useOpsAction(orgId: string | null) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ path, json }: { path: string; json?: unknown }) =>
      api.request(path, { method: "POST", json: json ?? {} }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ops"] });
      if (orgId) void queryClient.invalidateQueries({ queryKey: ["ops", "application", orgId] });
    },
  });
}

function KeyValues({ data }: { data: Record<string, unknown> | null }) {
  if (!data) return <p className="text-sm text-muted-foreground">Not provided</p>;
  return (
    <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[180px_1fr]">
      {Object.entries(data).map(([k, v]) => (
        <React.Fragment key={k}>
          <dt className="text-muted-foreground">{k.replace(/_/g, " ")}</dt>
          <dd className="break-words">{v == null || v === "" ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

function ApplicationView({ orgId, onBack }: { orgId: string; onBack: () => void }) {
  const { api } = useAuth();
  const appQ = useOps<Application>(["application", orgId], `/api/v1/ops/applications/${orgId}`);
  const action = useOpsAction(orgId);
  const [note, setNote] = React.useState("");
  const [registryLink, setRegistryLink] = React.useState("");
  const [deposit, setDeposit] = React.useState("");
  const [dailyCalls, setDailyCalls] = React.useState("");
  const [dailyTexts, setDailyTexts] = React.useState("");
  const [ban, setBan] = React.useState(false);

  if (appQ.isPending) return <Spinner label="Loading application" />;
  if (appQ.isError || !appQ.data) return <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(appQ.error)}</p>;
  const app = appQ.data;
  const base = `/api/v1/ops/applications/${orgId}`;
  const run = (path: string, json?: unknown) => action.mutate({ path: `${base}/${path}`, json });

  async function openDocument(id: string, filename: string) {
    const blob = await fetchAuthedBlob(api, `${base}/documents/${id}`);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  const ai = app.checks.ai_summary;
  const decision = app.checks.ai_decision;

  return (
    <div className="space-y-4">
      <Button type="button" variant="ghost" onClick={onBack}>← Back to queue</Button>
      <Section
        title={`${String(app.business.legal_name ?? app.org.name)}`}
        description={`Workspace ${app.org.name} · ${app.status.replace(/_/g, " ")}`}
        actions={
          <Pill tone={app.risk.tier === "high" ? "danger" : "success"}>
            {app.risk.tier ? `${app.risk.tier} risk` : "not scored"}
          </Pill>
        }
      >
        {app.risk.reasons.length > 0 && (
          <Card>
            <p className="text-sm font-medium">Why this is {app.risk.tier} risk</p>
            <ul className="list-disc pl-5 text-sm">
              {app.risk.reasons.map((r) => <li key={r}>{r}</li>)}
            </ul>
          </Card>
        )}

        <DecisionPackCard
          pack={(decision?.detail as unknown as DecisionPack) ?? null}
          result={decision?.result ?? null}
          blockers={app.approval_blockers}
          pending={action.isPending}
          onApprove={async (approveNote, limits) => {
            const chosen = {
              ...(limits.daily_calls != null ? { daily_calls: limits.daily_calls } : {}),
              ...(limits.daily_texts != null ? { daily_texts: limits.daily_texts } : {}),
              ...(limits.max_numbers != null ? { max_numbers: limits.max_numbers } : {}),
            };
            if (Object.keys(chosen).length > 0) {
              await action.mutateAsync({
                path: `${base}/limits`,
                json: { deposit_required_cents: app.deposit_required_cents, limits: chosen },
              });
            }
            run("approve", { note: approveNote });
          }}
          onAskInfo={(message) => run("request-info", { message })}
          onReject={(reason, banIdentifiers) => run("reject", { reason, ban: banIdentifiers })}
        />

        {ai && !decision && (
          <Card>
            <p className="text-sm font-medium">AI reviewer summary <span className="text-xs text-muted-foreground">(advisory)</span></p>
            <p className="text-sm">{String((ai.detail?.summary as string) ?? ai.summary)}</p>
            {Array.isArray(ai.detail?.concerns) && (ai.detail?.concerns as string[]).length > 0 && (
              <ul className="mt-1 list-disc pl-5 text-sm text-amber-300">
                {(ai.detail?.concerns as string[]).map((c) => <li key={c}>{c}</li>)}
              </ul>
            )}
          </Card>
        )}

        <Card className="space-y-2">
          <p className="text-sm font-medium">Automatic checks</p>
          {Object.entries(app.checks).filter(([k]) => k !== "ai_summary" && k !== "ai_decision").map(([k, c]) => (
            <details key={k} className="rounded-md border border-border px-3 py-2">
              <summary className="flex cursor-pointer items-center gap-2 text-sm">
                <Pill tone={RESULT_TONE[c.result] ?? "neutral"}>{c.result}</Pill>
                <span className="font-medium">{CHECK_LABELS[k] ?? k}</span>
                <span className="text-muted-foreground">{c.summary}</span>
              </summary>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs text-muted-foreground">
                {JSON.stringify(c.detail, null, 2)}
              </pre>
            </details>
          ))}
        </Card>

        <Card><p className="mb-2 text-sm font-medium">Business</p><KeyValues data={app.business} /></Card>
        <Card>
          <p className="mb-2 text-sm font-medium">Declared use case</p>
          <KeyValues data={app.use_case} />
          {app.use_case_pending && (
            <div className="mt-3 rounded-md border border-amber-500/40 p-2">
              <p className="text-sm font-medium text-amber-300">Requested change (applies on approval)</p>
              <KeyValues data={app.use_case_pending} />
            </div>
          )}
        </Card>

        <Card className="space-y-2">
          <p className="text-sm font-medium">People</p>
          {app.persons.map((p) => (
            <div key={p.id} className="flex flex-wrap items-center gap-2 text-sm">
              <Pill tone={p.status === "verified" ? "success" : "warning"}>{p.status.replace(/_/g, " ")}</Pill>
              <span>{p.full_name}</span>
              <span className="text-muted-foreground">
                {p.role.replace(/_/g, " ")}
                {p.ownership_percent != null ? ` · ${p.ownership_percent}%` : ""}
                {p.verified_name ? ` · ID says "${p.verified_name}"` : ""}
                {p.document_country ? ` · ${p.document_type ?? "ID"} from ${p.document_country}` : ""}
              </span>
            </div>
          ))}
        </Card>

        <Card className="space-y-2">
          <p className="text-sm font-medium">Documents</p>
          {app.documents.map((d) => (
            <div key={d.id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="space-x-2">
                {d.review_result && <Pill tone={RESULT_TONE[d.review_result] ?? "neutral"}>{d.review_result}</Pill>}
                <span>
                  {d.kind.replace(/_/g, " ")}
                  {d.person_id ? ` for ${app.persons.find((p) => p.id === d.person_id)?.full_name ?? "an owner"}` : ""} · {d.filename}
                </span>
                {(d.review?.reasons ?? []).length > 0 && (
                  <span className="block text-xs text-muted-foreground">{d.review!.reasons!.join(" ")}</span>
                )}
              </span>
              <Button type="button" size="sm" variant="outline" onClick={() => void openDocument(d.id, d.filename)}>
                Download
              </Button>
            </div>
          ))}
        </Card>

        <Card className="space-y-3">
          <p className="text-sm font-medium">Decide</p>
          {app.approval_blockers.length > 0 && (
            <ul className="list-disc pl-5 text-sm text-amber-300">
              {app.approval_blockers.map((b) => <li key={b}>{b}</li>)}
            </ul>
          )}
          <Textarea aria-label="Reviewer note" rows={2} placeholder="Note, reason, or message to the business" value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="flex flex-wrap gap-2">
            {app.status === "submitted" && <Button type="button" variant="outline" onClick={() => run("review")}>Start review</Button>}
            <Button type="button" disabled={app.approval_blockers.length > 0 || action.isPending} onClick={() => run("approve", { note })}>Approve</Button>
            <Button type="button" variant="outline" disabled={!note.trim()} onClick={() => run("request-info", { message: note })}>Ask for more info</Button>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={ban} onChange={(e) => setBan(e.target.checked)} />
              also ban identifiers
            </label>
            <Button type="button" variant="destructive" disabled={!note.trim()} onClick={() => run("reject", { reason: note, ban })}>Reject</Button>
          </div>
          <div className="flex flex-wrap gap-2">
            <Input aria-label="Registry link" placeholder="Registry page you checked (link)" value={registryLink} onChange={(e) => setRegistryLink(e.target.value)} />
            <Button type="button" variant="outline" disabled={!registryLink.trim()} onClick={() => run("registry", { result: "pass", link: registryLink, note })}>Registry: confirmed</Button>
            <Button type="button" variant="outline" disabled={!registryLink.trim()} onClick={() => run("registry", { result: "fail", link: registryLink, note })}>Registry: not found</Button>
            <Button type="button" variant="ghost" onClick={() => run("rerun-checks")}>Re-run checks</Button>
          </div>
          <div className="grid gap-2 sm:grid-cols-4">
            <Input aria-label="Deposit in cents" placeholder="Deposit (cents)" value={deposit} onChange={(e) => setDeposit(e.target.value)} />
            <Input aria-label="Daily calls" placeholder="Daily calls" value={dailyCalls} onChange={(e) => setDailyCalls(e.target.value)} />
            <Input aria-label="Daily texts" placeholder="Daily texts" value={dailyTexts} onChange={(e) => setDailyTexts(e.target.value)} />
            <Button
              type="button"
              variant="outline"
              onClick={() =>
                run("limits", {
                  deposit_required_cents: deposit ? Number(deposit) : null,
                  limits: {
                    ...(dailyCalls ? { daily_calls: Number(dailyCalls) } : {}),
                    ...(dailyTexts ? { daily_texts: Number(dailyTexts) } : {}),
                  },
                })
              }
            >
              Save limits
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Current: deposit {app.deposit_required_cents ?? "none"} · limits {app.limits ? JSON.stringify(app.limits) : "none"}
          </p>
          <div className="flex flex-wrap gap-2 border-t border-border pt-3">
            {app.status === "suspended" ? (
              <Button type="button" variant="outline" onClick={() => run("unsuspend", { note })}>Lift suspension</Button>
            ) : (
              <Button type="button" variant="destructive" disabled={!note.trim()} onClick={() => run("suspend", { reason: note, ban })}>
                Suspend account now
              </Button>
            )}
          </div>
          {action.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(action.error)}</p>}
        </Card>
      </Section>
    </div>
  );
}

function QueueTab({ onOpen }: { onOpen: (orgId: string) => void }) {
  const [status, setStatus] = React.useState("");
  const q = useOps<{ applications: QueueItem[]; open_security_alerts: number }>(
    ["queue", status],
    `/api/v1/ops/queue${status ? `?status=${status}` : ""}`,
  );
  return (
    <div className="space-y-3">
      <Select aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)} className="max-w-xs">
        <option value="">Needs attention</option>
        {["submitted", "in_review", "needs_info", "reverification_due", "suspended", "approved", "rejected"].map((s) => (
          <option key={s} value={s}>{s.replace(/_/g, " ")}</option>
        ))}
      </Select>
      {q.isPending ? (
        <Spinner label="Loading queue" />
      ) : q.isError ? (
        <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(q.error)}</p>
      ) : q.data!.applications.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing waiting.</p>
      ) : (
        <ul className="space-y-2">
          {q.data!.applications.map((a) => (
            <li key={a.org_id}>
              <button
                type="button"
                onClick={() => onOpen(a.org_id)}
                className="flex w-full flex-wrap items-center gap-2 rounded-md border border-border px-3 py-2 text-left hover:bg-muted"
              >
                <Pill tone={a.risk_tier === "high" ? "danger" : "neutral"}>{a.risk_tier ?? "—"}</Pill>
                <span className="text-sm font-medium">{a.legal_name ?? a.org_name}</span>
                <span className="text-xs text-muted-foreground">
                  {a.country ?? ""} · {a.status.replace(/_/g, " ")}
                  {a.video_call_required && !a.video_call_done ? " · video call needed" : ""}
                  {a.use_case_change_pending ? " · use-case change" : ""}
                  {a.submitted_at ? ` · ${new Date(a.submitted_at).toLocaleString()}` : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AlertsTab() {
  const q = useOps<Alert[]>(["alerts"], "/api/v1/ops/alerts");
  const action = useOpsAction(null);
  if (q.isPending) return <Spinner label="Loading alerts" />;
  if (q.isError) return <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(q.error)}</p>;
  if (q.data.length === 0) return <p className="text-sm text-muted-foreground">No open alerts.</p>;
  return (
    <ul className="space-y-2">
      {q.data.map((a) => (
        <li key={a.id} className="space-y-1 rounded-md border border-border px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">{a.kind.replace(/_/g, " ")}</span>
            <Button type="button" size="sm" variant="outline" onClick={() => action.mutate({ path: `/api/v1/ops/alerts/${a.id}/review`, json: { note: "" } })}>
              Mark reviewed
            </Button>
          </div>
          <KeyValues data={a.detail} />
        </li>
      ))}
    </ul>
  );
}

function BanListTab() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const q = useOps<Ban[]>(["bans"], "/api/v1/ops/ban-list");
  const [kind, setKind] = React.useState("email");
  const [value, setValue] = React.useState("");
  const [reason, setReason] = React.useState("");
  const add = useMutation({
    mutationFn: () => api.request("/api/v1/ops/ban-list", { method: "POST", json: { kind, value, reason } }),
    onSuccess: () => {
      setValue("");
      void queryClient.invalidateQueries({ queryKey: ["ops", "bans"] });
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.request(`/api/v1/ops/ban-list/${id}`, { method: "DELETE" }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["ops", "bans"] }),
  });
  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-[160px_1fr_1fr_auto]">
        <Select aria-label="Identifier kind" value={kind} onChange={(e) => setKind(e.target.value)}>
          {["email", "email_domain", "phone", "website_domain", "registration_number", "tax_id", "address", "card_fingerprint", "ip"].map((k) => (
            <option key={k} value={k}>{k.replace(/_/g, " ")}</option>
          ))}
        </Select>
        <Input aria-label="Value to ban" placeholder="Value" value={value} onChange={(e) => setValue(e.target.value)} />
        <Input aria-label="Ban reason" placeholder="Reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        <Button type="button" disabled={!value.trim() || !reason.trim() || add.isPending} onClick={() => add.mutate()}>Ban</Button>
      </div>
      {(add.isError || remove.isError) && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(add.error ?? remove.error)}</p>}
      {q.isPending ? (
        <Spinner label="Loading ban list" />
      ) : (
        <ul className="space-y-1">
          {(q.data ?? []).map((b) => (
            <li key={b.id} className="flex items-center justify-between rounded-md border border-border px-3 py-2 text-sm">
              <span>
                <Pill>{b.kind.replace(/_/g, " ")}</Pill> {b.hint} · {b.reason}
                <span className="text-xs text-muted-foreground"> {b.added_by ? `by ${b.added_by}` : ""}</span>
              </span>
              <Button type="button" size="sm" variant="ghost" onClick={() => remove.mutate(b.id)}>Remove</Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type OpsUser = {
  id: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  totp_enabled: boolean;
  has_passkey: boolean;
  recovery_codes_remaining: number;
  locked_until: string | null;
  step_up_blocked_until: string | null;
};

/** P42: account support - unlock, reset sign-in factors, deactivate. Every action needs a
 * fresh operator 2FA check server-side and is audited and emailed to the person. */
function UsersTab() {
  const { api } = useAuth();
  const [email, setEmail] = React.useState("");
  const [reason, setReason] = React.useState("");
  const [search, setSearch] = React.useState<string | null>(null);
  const q = useQuery({
    queryKey: ["ops", "users", search],
    queryFn: () => api.request<OpsUser>(`/api/v1/ops/users?email=${encodeURIComponent(search ?? "")}`),
    enabled: Boolean(search),
    retry: false,
  });
  const act = useMutation({
    mutationFn: (action: "unlock" | "reset-2fa" | "deactivate" | "reactivate") =>
      api.request(`/api/v1/ops/users/${q.data?.id}/${action}`, {
        method: "POST",
        ...(action === "unlock" ? {} : { json: { reason } }),
      }),
    onSuccess: () => void q.refetch(),
  });
  const user = q.data;
  const needsReason = reason.trim().length < 3;
  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <Input aria-label="User email" placeholder="person@company.com" value={email} onChange={(e) => setEmail(e.target.value)} />
        <Button type="button" disabled={email.trim().length < 3} onClick={() => setSearch(email.trim())}>Find</Button>
      </div>
      {q.isFetching && <Spinner label="Looking up account" />}
      {q.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(q.error)}</p>}
      {user && (
        <Card className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">{user.email}</span>
            {!user.is_active && <Pill tone="danger">Deactivated</Pill>}
            {user.locked_until && <Pill tone="warning">Locked until {new Date(user.locked_until).toLocaleString()}</Pill>}
            <Pill>{user.has_passkey ? "Passkey" : "No passkey"}</Pill>
            <Pill>{user.totp_enabled ? "Authenticator app" : "No authenticator"}</Pill>
            <Pill>{user.recovery_codes_remaining} recovery codes</Pill>
            {user.step_up_blocked_until && <Pill tone="warning">Recovery cool-down</Pill>}
          </div>
          <Input aria-label="Support reason" placeholder="Reason (required, e.g. verified on video call with ID)" value={reason} onChange={(e) => setReason(e.target.value)} />
          <div className="flex flex-wrap gap-2">
            {user.locked_until && (
              <Button type="button" size="sm" disabled={act.isPending} onClick={() => act.mutate("unlock")}>Unlock</Button>
            )}
            <Button type="button" size="sm" variant="outline" disabled={act.isPending || needsReason} onClick={() => act.mutate("reset-2fa")}>
              Reset 2FA
            </Button>
            {user.is_active ? (
              <Button type="button" size="sm" variant="outline" disabled={act.isPending || needsReason} onClick={() => act.mutate("deactivate")}>
                Deactivate
              </Button>
            ) : (
              <Button type="button" size="sm" variant="outline" disabled={act.isPending || needsReason} onClick={() => act.mutate("reactivate")}>
                Reactivate
              </Button>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            Reset 2FA only after confirming who they are out of band. It removes every factor and starts a cool-down on sensitive actions.
          </p>
          {act.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(act.error)}</p>}
          {act.isSuccess && <p className="text-sm text-emerald-300">Done.</p>}
        </Card>
      )}
    </div>
  );
}

const TABS = [
  { id: "queue", label: "Review queue" },
  { id: "alerts", label: "Security alerts" },
  { id: "bans", label: "Ban list" },
  { id: "monitoring", label: "Monitoring" },
  { id: "users", label: "Users" },
];

export function OpsPage() {
  const { me } = useAuth();
  const [tab, setTab] = React.useState("queue");
  const [openOrg, setOpenOrg] = React.useState<string | null>(null);

  if (!me?.is_platform_operator) {
    return (
      <div className="p-6">
        <Card><p className="text-sm">The operator console is for platform operators only.</p></Card>
      </div>
    );
  }

  return (
    <div className="mx-auto h-full max-w-5xl space-y-4 overflow-y-auto p-6">
      <h1 className="text-lg font-semibold">Trust & safety</h1>
      {openOrg ? (
        <ApplicationView orgId={openOrg} onBack={() => setOpenOrg(null)} />
      ) : (
        <>
          <Tabs id="ops" tabs={TABS} value={tab} onChange={setTab} ariaLabel="Operator console" />
          <TabPanel tabsId="ops" id={tab}>
            {tab === "queue" && <QueueTab onOpen={setOpenOrg} />}
            {tab === "alerts" && <AlertsTab />}
            {tab === "bans" && <BanListTab />}
            {tab === "monitoring" && <MonitoringTab />}
            {tab === "users" && <UsersTab />}
          </TabPanel>
        </>
      )}
    </div>
  );
}
