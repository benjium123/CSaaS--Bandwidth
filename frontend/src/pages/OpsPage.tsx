import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";
import "@fontsource/instrument-serif/400.css";
import "@fontsource/instrument-serif/400-italic.css";
import "@fontsource-variable/familjen-grotesk";
import "@fontsource/fragment-mono/400.css";
import "./ops/switchboard.css";
import { IdentityEvidence } from "@/components/ops/IdentityEvidence";
import { MonitoringTab } from "@/components/ops/MonitoringTab";
import { CustomerAccountsTab } from "@/components/ops/CustomerAccountsTab";
import { AccountsTab } from "@/components/ops/AccountsTab";
import { BillingTab } from "@/components/ops/BillingTab";
import { ConsoleTab } from "@/components/ops/ConsoleTab";
import { PortReviewTab } from "@/components/ops/PortReviewTab";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAuthedBlob } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import {
  Button,
  Card,
  Input,
  Pill,
  Select,
  Spinner,
  mutationErrorMessage,
} from "@/components/ui/primitives";
import {
  InitialsAvatar,
  SurfaceCard,
} from "@/components/ui/consoleChrome";

/** P41 operator console: review businesses, handle security alerts, keep the ban list.
 * P42: account support (unlock, reset 2FA, deactivate). */

type AccountType = "business" | "individual";

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
  ai_recommendation?: "approve" | "needs_info" | "reject" | null;
  ai_confidence?: number | null;
  /** Optional: legacy backends omit it; treat missing as "business". */
  account_type?: AccountType;
};

const AI_LABEL: Record<string, string> = { approve: "AI: approve", needs_info: "AI: ask for info", reject: "AI: reject" };

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
    /** Optional: individual persons may carry a provider hint. */
    identity_provider?: string | null;
    provider_session_id?: string | null;
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
  /** Optional: legacy backends omit it; treat missing as "business". */
  account_type?: AccountType;
};

type Alert = { id: string; kind: string; status: string; detail: Record<string, unknown> | null; created_at: string | null };
type Ban = { id: string; kind: string; hint: string | null; reason: string; added_by: string | null; added_at: string | null };


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
  if (!data) return <p className="text-[13px] text-[hsl(var(--cx-muted))]">Not provided</p>;
  return (
    <dl className="grid gap-x-4 gap-y-1.5 text-[13px] sm:grid-cols-[180px_1fr]">
      {Object.entries(data).map(([k, v]) => (
        <React.Fragment key={k}>
          <dt className="text-[hsl(var(--cx-muted))]">{k.replace(/_/g, " ")}</dt>
          <dd className="whitespace-pre-wrap break-words text-[hsl(var(--cx-text))]">{v == null || v === "" ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

function waited(iso: string | null): { text: string; late: boolean } {
  if (!iso) return { text: "—", late: false };
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (minutes < 60) return { text: `${minutes}m`, late: false };
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return { text: `${hours}h ${minutes % 60}m`, late: hours >= 4 };
  return { text: `${Math.floor(hours / 24)}d ${hours % 24}h`, late: true };
}

function Answers({ data }: { data: Record<string, unknown> }) {
  const rows = Object.entries(data).filter(([, v]) => v != null && v !== "" && typeof v !== "object");
  if (rows.length === 0) return <p className="sb-foot">Nothing provided.</p>;
  return (
    <dl className="sb-answers">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt>{k.replace(/_/g, " ")}</dt>
          <dd>{String(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function ApplicationView({ orgId, onBack }: { orgId: string; onBack: () => void }) {
  const { api, me } = useAuth();
  const appQ = useOps<Application>(["application", orgId], `/api/v1/ops/applications/${orgId}`);
  const action = useOpsAction(orgId);
  const [note, setNote] = React.useState("");
  const [override, setOverride] = React.useState(false);
  const [registryLink, setRegistryLink] = React.useState("");
  const [deposit, setDeposit] = React.useState("");
  const [dailyCalls, setDailyCalls] = React.useState("");
  const [dailyTexts, setDailyTexts] = React.useState("");
  const [ban, setBan] = React.useState(false);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = (e.target as HTMLElement | null)?.closest("input, textarea, select");
      if (e.key === "Escape" && !typing) onBack();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onBack]);

  if (appQ.isPending) return <Spinner label="Loading application" />;
  if (appQ.isError || !appQ.data) return <p role="alert" className="sb-error">{mutationErrorMessage(appQ.error)}</p>;
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

  const individual = app.account_type === "individual";
  const isAdmin = me?.operator_role === "admin";
  const canApprove = !individual || isAdmin;
  const blocked = app.approval_blockers.length > 0;
  const approveDisabled = (blocked && !(isAdmin && override && note.trim())) || action.isPending || !canApprove;
  const decided = app.status === "approved" || app.status === "rejected";
  const name = String(app.business.legal_name ?? app.org.name);
  const answers: Record<string, unknown> = individual
    ? {
        "Legal name": app.business.legal_name,
        Country: app.business.country,
        Phone: app.business.business_phone,
        Email: app.business.business_email,
        Industry: app.use_case?.vertical,
        "About their business": app.use_case?.business_description,
        "What they will use Ringlite for": app.use_case?.description,
        "Customer countries": (app.use_case?.destination_countries as string[] | undefined)?.join(", "),
      }
    : { ...(app.business as Record<string, unknown>), ...((app.use_case ?? {}) as Record<string, unknown>) };
  const autoChecks = Object.entries(app.checks).filter(([k]) => k !== "ai_summary" && k !== "ai_decision");
  const ai = app.checks.ai_summary;

  return (
    <div>
      <button type="button" className="sb-back" onClick={onBack}>
        ← Back to queue <span className="sb-foot" style={{ margin: 0 }}>esc</span>
      </button>
      <header className="sb-head sb-rise">
        <div>
          <div className="sb-eyebrow">
            {individual ? "Individual" : "Business"} · {app.status.replace(/_/g, " ")}
          </div>
          <h1 className="sb-title">{name}</h1>
          <div className="mt-3 flex flex-wrap gap-2">
            <span className="sb-tag" data-tone={app.risk.tier === "high" ? "stop" : app.risk.tier === "medium" ? "amber" : app.risk.tier ? "go" : undefined}>
              {app.risk.tier ? `${app.risk.tier} risk` : "not scored"}
            </span>
            {app.business.country ? <span className="sb-tag" data-plain="true">{String(app.business.country)}</span> : null}
            <span className="sb-tag" data-plain="true">Workspace {app.org.name}</span>
            {app.risk.submitted_from_flagged_login && <span className="sb-tag" data-tone="stop">Flagged sign-in</span>}
          </div>
        </div>
      </header>

      <div className="sb-review">
        <div className="sb-stack">
          <section className="sb-panel sb-panel-pad sb-rise" style={{ "--i": 1 } as React.CSSProperties}>
            <h2>Identity <small>via Didit</small></h2>
            {app.persons.length === 0 ? (
              <p className="sb-foot">No people on this application yet.</p>
            ) : (
              <div className="flex flex-col gap-6">
                {app.persons.map((person) => (
                  <div key={person.id}>
                    {app.persons.length > 1 && (
                      <p className="mb-2 font-semibold">
                        {person.full_name} <span className="sb-foot">· {person.role.replace(/_/g, " ")}</span>
                      </p>
                    )}
                    <IdentityEvidence orgId={orgId} person={person} />
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="sb-panel sb-panel-pad sb-rise applicant-answers" style={{ "--i": 2 } as React.CSSProperties}>
            <h2>What they told us</h2>
            <Answers data={answers} />
            {app.use_case_pending && (
              <div className="sb-warn mt-4">
                <span>Change</span>
                <span>They asked to change their use case; it applies when you approve.</span>
              </div>
            )}
          </section>

          {(autoChecks.length > 0 || app.risk.reasons.length > 0 || ai) && (
            <details className="sb-more sb-rise" style={{ "--i": 3 } as React.CSSProperties}>
              <summary>Automatic checks ({autoChecks.length})</summary>
              <div className="sb-more-body">
                {app.risk.reasons.length > 0 && (
                  <div className="flex flex-col gap-2">
                    {app.risk.reasons.map((r) => (
                      <div key={r} className="sb-warn"><span>Risk</span><span>{r}</span></div>
                    ))}
                  </div>
                )}
                {ai && (
                  <p className="sb-foot">AI note (advisory): {String((ai.detail?.summary as string) ?? ai.summary)}</p>
                )}
                <div className="sb-checks">
                  {autoChecks.map(([k, c]) => (
                    <div key={k} className="sb-check">
                      <span className="sb-lamp" data-v={c.result === "pass" ? "ok" : c.result === "warn" || c.result === "pending" ? "review" : "fail"} aria-hidden="true" />
                      <div>
                        <div className="sb-check-label">{CHECK_LABELS[k] ?? k}</div>
                        <div className="sb-check-detail">{c.summary}</div>
                      </div>
                      <div className="sb-reading">{c.result}</div>
                    </div>
                  ))}
                </div>
              </div>
            </details>
          )}

          {app.documents.length > 0 && (
            <details className="sb-more">
              <summary>Documents ({app.documents.length})</summary>
              <div className="sb-more-body">
                {app.documents.map((d) => (
                  <div key={d.id} className="flex flex-wrap items-center justify-between gap-2">
                    <span>
                      {d.kind.replace(/_/g, " ")} · <span className="sb-foot">{d.filename}</span>
                    </span>
                    <button type="button" className="sb-btn" data-kind="quiet" onClick={() => void openDocument(d.id, d.filename)}>
                      Download
                    </button>
                  </div>
                ))}
              </div>
            </details>
          )}

          <details className="sb-more">
            <summary>Limits, registry and suspension</summary>
            <div className="sb-more-body sb-legacy">
              <div className="flex flex-wrap gap-2">
                <Input aria-label="Registry link" placeholder="Registry page you checked (link)" value={registryLink} onChange={(e) => setRegistryLink(e.target.value)} />
                <button type="button" className="sb-btn" data-kind="quiet" disabled={!registryLink.trim()} onClick={() => run("registry", { result: "pass", link: registryLink, note })}>Registry: confirmed</button>
                <button type="button" className="sb-btn" data-kind="quiet" disabled={!registryLink.trim()} onClick={() => run("registry", { result: "fail", link: registryLink, note })}>Registry: not found</button>
                <button type="button" className="sb-btn" data-kind="quiet" onClick={() => run("rerun-checks")}>Re-run checks</button>
              </div>
              <div className="grid gap-2 sm:grid-cols-4">
                <Input aria-label="Deposit in cents" placeholder="Deposit (cents)" value={deposit} onChange={(e) => setDeposit(e.target.value)} />
                <Input aria-label="Daily calls" placeholder="Daily calls" value={dailyCalls} onChange={(e) => setDailyCalls(e.target.value)} />
                <Input aria-label="Daily texts" placeholder="Daily texts" value={dailyTexts} onChange={(e) => setDailyTexts(e.target.value)} />
                <button
                  type="button"
                  className="sb-btn"
                  data-kind="quiet"
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
                </button>
              </div>
              <p className="sb-foot">
                Now: deposit {app.deposit_required_cents ?? "none"} · limits {app.limits ? JSON.stringify(app.limits) : "none"}
              </p>
              {app.status === "suspended" ? (
                <button type="button" className="sb-btn" data-kind="quiet" onClick={() => run("unsuspend", { note })}>Lift suspension</button>
              ) : (
                <button type="button" className="sb-btn" data-kind="stop" disabled={!note.trim()} onClick={() => run("suspend", { reason: note, ban })}>
                  Suspend account now
                </button>
              )}
            </div>
          </details>
        </div>

        <aside className="sb-dock sb-rise" style={{ "--i": 1 } as React.CSSProperties}>
          <section className="sb-panel sb-verdict">
            <h2>Your decision</h2>
            {decided ? (
              <p className="sb-foot">Already {app.status}. You can still suspend it below.</p>
            ) : (
              <p className="sb-foot">Didit and the automatic checks advise; you decide. Everything is recorded.</p>
            )}
            {blocked && (
              <ul className="sb-blockers">
                {app.approval_blockers.map((b) => <li key={b}>{b}</li>)}
              </ul>
            )}
            {isAdmin && blocked && (
              <label className="sb-check-inline">
                <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />
                <span>I reviewed these failed checks and approve anyway. The reason goes in the note below and is recorded.</span>
              </label>
            )}
            {!canApprove && <p className="sb-foot">Only a platform admin can approve an individual account.</p>}
            <textarea
              className="sb-textarea"
              aria-label="Reviewer note"
              placeholder={blocked ? "Why you are approving anyway, or what they need to fix" : "Optional note, or the message they receive if you ask for info"}
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <div className="sb-actions">
              {app.status === "submitted" && (
                <button type="button" className="sb-btn" data-kind="quiet" onClick={() => run("review")}>
                  Start review
                </button>
              )}
              <button
                type="button"
                className="sb-btn"
                data-kind="go"
                disabled={approveDisabled}
                onClick={() => {
                  if (!canApprove) return;
                  run("approve", { note, manual_override: isAdmin && override && blocked });
                }}
              >
                Approve
              </button>
              <button type="button" className="sb-btn" data-kind="amber" disabled={!note.trim() || action.isPending} onClick={() => run("request-info", { message: note })}>
                Ask for more info
              </button>
              <button
                type="button"
                className="sb-btn"
                data-kind="stop"
                disabled={action.isPending}
                onClick={() => run("reject", { reason: note.trim() || "Application declined after manual review", ban })}
              >
                Reject
              </button>
              <label className="sb-check-inline">
                <input type="checkbox" checked={ban} onChange={(e) => setBan(e.target.checked)} />
                <span>Also ban their identity, email and phone from signing up again</span>
              </label>
            </div>
            {action.isError && <p role="alert" className="sb-error">{mutationErrorMessage(action.error)}</p>}
          </section>
        </aside>
      </div>
    </div>
  );
}

const QUEUE_FILTERS: [string, string][] = [
  ["", "Needs a decision"],
  ["needs_info", "Waiting on them"],
  ["suspended", "Suspended"],
  ["approved", "Approved"],
  ["rejected", "Rejected"],
];

function QueueTab({ onOpen }: { onOpen: (orgId: string) => void }) {
  const [status, setStatus] = React.useState("");
  const q = useOps<{ applications: QueueItem[]; open_security_alerts: number }>(
    ["queue", status],
    `/api/v1/ops/queue${status ? `?status=${status}` : ""}`,
  );
  const apps = [...(q.data?.applications ?? [])].sort(
    (a, b) => new Date(a.submitted_at ?? 0).getTime() - new Date(b.submitted_at ?? 0).getTime(),
  );
  return (
    <div>
      <div className="sb-seg" role="group" aria-label="Filter by status">
        {QUEUE_FILTERS.map(([value, label]) => (
          <button key={value || "open"} type="button" aria-pressed={status === value} onClick={() => setStatus(value)}>
            {label}
          </button>
        ))}
      </div>
      {q.isPending ? (
        <div className="mt-6"><Spinner label="Loading queue" /></div>
      ) : q.isError ? (
        <p role="alert" className="sb-error">{mutationErrorMessage(q.error)}</p>
      ) : apps.length === 0 ? (
        <div className="sb-empty mt-4">
          <div className="sb-title"><em>All clear.</em></div>
          <p>Nobody is waiting on a decision.</p>
        </div>
      ) : (
        <ul className="sb-tickets">
          {apps.map((a, index) => {
            const wait = waited(a.submitted_at);
            return (
              <li key={a.org_id} className="sb-rise" style={{ "--i": Math.min(index, 12) } as React.CSSProperties}>
                <button type="button" className="sb-ticket" data-risk={a.risk_tier ?? "none"} onClick={() => onOpen(a.org_id)}>
                  <div className="min-w-0">
                    <div className="sb-ticket-name">{a.legal_name ?? a.org_name}</div>
                    <div className="sb-ticket-sub">
                      {a.account_type === "individual" ? "Individual" : "Business"}
                      {a.country ? ` · ${a.country}` : ""} · {a.status.replace(/_/g, " ")}
                    </div>
                  </div>
                  <div className="sb-ticket-signals">
                    <span className="sb-tag" data-tone={a.risk_tier === "high" ? "stop" : a.risk_tier === "medium" ? "amber" : a.risk_tier ? "go" : undefined}>
                      {a.risk_tier ? `${a.risk_tier} risk` : "not scored"}
                    </span>
                    {a.ai_recommendation ? (
                      <span className="sb-tag" data-tone={a.ai_recommendation === "approve" ? "go" : a.ai_recommendation === "reject" ? "stop" : "amber"}>
                        {AI_LABEL[a.ai_recommendation]}
                        {a.ai_confidence != null ? ` ${a.ai_confidence}%` : ""}
                      </span>
                    ) : null}
                    {a.video_call_required && !a.video_call_done ? <span className="sb-tag" data-tone="sky">Video call needed</span> : null}
                    {a.use_case_change_pending ? <span className="sb-tag" data-tone="sky">Use-case change</span> : null}
                  </div>
                  <div className="sb-wait" data-late={wait.late}>
                    <b>{wait.text}</b>
                    waiting
                  </div>
                </button>
              </li>
            );
          })}
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
  if (q.data.length === 0) return <p className="text-[13px] text-[hsl(var(--cx-muted))]">No open alerts.</p>;
  return (
    <ul className="space-y-2">
      {q.data.map((a) => (
        <li
          key={a.id}
          className="space-y-2 rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] px-4 py-3"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="text-[13.5px] font-semibold text-[hsl(var(--cx-text))]">{a.kind.replace(/_/g, " ")}</span>
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

type OpsPurchase = {
  id: string;
  org_id: string;
  state: string;
  detail: string | null;
  numbers: { e164: string; state: string; number_id?: string }[];
  subscription_status: string | null;
  updated_at: string | null;
};

const STUCK_PURCHASE_STATES = ["needs_attention", "provisioning"];

/** Paid purchases that did not finish. Retry looks each number up at Telnyx before ordering,
 *  so it never buys twice; refund drops only numbers that were never set up. */
function PurchasesTab() {
  const q = useOps<OpsPurchase[]>(["number-purchases"], "/api/v1/ops/number-purchases");
  const action = useOpsAction(null);
  const [message, setMessage] = React.useState("");
  if (q.isPending) return <Spinner label="Loading purchases" />;
  if (q.isError) return <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(q.error)}</p>;
  const stuck = q.data.filter((p) => STUCK_PURCHASE_STATES.includes(p.state));
  async function run(p: OpsPurchase, kind: "retry" | "refund") {
    if (kind === "refund") {
      const count = p.numbers.filter((n) => !n.number_id && n.state !== "released" && n.state !== "refunded").length;
      if (!window.confirm(`Refund ${count} unprovisioned number(s) (${count * 15}) and stop billing for them?`)) return;
    }
    setMessage("");
    try {
      const result = (await action.mutateAsync({ path: `/api/v1/ops/number-purchases/${p.id}/${kind}` })) as {
        state: string;
        failures?: string[];
      };
      setMessage(
        result.failures?.length
          ? `Still needs attention: ${result.failures.join("; ")}`
          : `Purchase is now ${result.state.replace(/_/g, " ")}.`,
      );
    } catch (e) {
      setMessage(mutationErrorMessage(e));
    }
  }
  return (
    <div className="space-y-3">
      {message && <p role="status" className="text-[13px] text-[hsl(var(--cx-text))]">{message}</p>}
      {stuck.length === 0 ? (
        <p className="text-[13px] text-[hsl(var(--cx-muted))]">No paid purchases are waiting on provisioning.</p>
      ) : (
        <ul className="space-y-2">
          {stuck.map((p) => (
            <li key={p.id} className="space-y-2 rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] px-4 py-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[13.5px] font-semibold text-[hsl(var(--cx-text))]">
                  {p.state.replace(/_/g, " ")} · workspace {p.org_id.slice(0, 8)} · subscription {p.subscription_status ?? "none"}
                </span>
                <span className="flex gap-2">
                  <Button type="button" size="sm" variant="outline" disabled={action.isPending} onClick={() => void run(p, "retry")}>
                    Retry provisioning
                  </Button>
                  <Button type="button" size="sm" variant="outline" disabled={action.isPending} onClick={() => void run(p, "refund")}>
                    Refund unprovisioned
                  </Button>
                </span>
              </div>
              <ul className="text-[13px] text-[hsl(var(--cx-subtle))]">
                {p.numbers.map((n) => (
                  <li key={n.e164}>
                    {n.e164} — {n.number_id ? n.state : `${n.state} (not provisioned)`}
                  </li>
                ))}
              </ul>
              <p className="text-[12px] text-[hsl(var(--cx-muted))]">Purchase {p.id}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type OpsTextingRegistration = {
  id: string;
  org_id: string;
  org_name: string;
  brand_name: string;
  campaign_name: string;
  stage: string;
  fee_tier: string;
  detail: string | null;
  paid_at: string | null;
  updated_at: string | null;
};

const CANCELLABLE_TEXTING_STAGES = ["checkout", "paid", "brand_filed", "otp_pending", "brand_approved", "campaign_filed"];

/** 10DLC recovery: registrations the carrier left half-finished. Reconcile adopts the
 *  carrier-side brand/campaign; cancel stops the registration and refunds the rest. */
function TextingRegistrationsTab() {
  const q = useOps<OpsTextingRegistration[]>(["texting-registrations"], "/api/v1/ops/texting-registrations");
  const action = useOpsAction(null);
  const [message, setMessage] = React.useState("");
  const { me } = useAuth();
  const isAdmin = me?.operator_role === "admin";
  if (q.isPending) return <Spinner label="Loading texting registrations" />;
  if (q.isError) return <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(q.error)}</p>;
  async function run(r: OpsTextingRegistration, kind: "reconcile" | "cancel") {
    if (kind === "cancel") {
      if (!window.confirm(`Cancel the texting registration for ${r.org_name} and refund what the carrier never charged?`)) return;
    }
    setMessage("");
    try {
      const result = (await action.mutateAsync({ path: `/api/v1/ops/texting-registrations/${r.id}/${kind}` })) as {
        stage: string;
        outcome?: string;
        detail?: string;
      };
      setMessage(kind === "reconcile" ? result.outcome ?? "" : result.detail ?? "");
    } catch (e) {
      setMessage(mutationErrorMessage(e));
    }
  }
  return (
    <div className="space-y-3">
      {message && <p role="status" className="text-[13px] text-[hsl(var(--cx-text))]">{message}</p>}
      {q.data.length === 0 ? (
        <p className="text-[13px] text-[hsl(var(--cx-muted))]">No texting registrations yet.</p>
      ) : (
        <ul className="space-y-2">
          {q.data.map((r) => {
            const canReconcile = isAdmin && r.stage === "needs_attention";
            const canCancel = isAdmin && (r.stage === "needs_attention" || CANCELLABLE_TEXTING_STAGES.includes(r.stage));
            return (
              <li
                key={r.id}
                aria-label={`Texting registration for ${r.org_name}`}
                className="space-y-2 rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] px-4 py-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-[13.5px] font-semibold text-[hsl(var(--cx-text))]">
                    {r.stage.replace(/_/g, " ")} · {r.org_name}
                  </span>
                  <span className="flex gap-2">
                    {canReconcile && (
                      <Button type="button" size="sm" variant="outline" disabled={action.isPending} onClick={() => void run(r, "reconcile")}>
                        Reconcile with carrier
                      </Button>
                    )}
                    {canCancel && (
                      <Button type="button" size="sm" variant="outline" disabled={action.isPending} onClick={() => void run(r, "cancel")}>
                        Cancel and refund
                      </Button>
                    )}
                  </span>
                </div>
                <p className="text-[13px] text-[hsl(var(--cx-subtle))]">
                  {r.brand_name} / {r.campaign_name} · {r.fee_tier.replace(/_/g, " ")}
                </p>
                {r.detail && <p className="text-[12px] text-[hsl(var(--cx-muted))]">{r.detail}</p>}
                <p className="text-[12px] text-[hsl(var(--cx-muted))]">Registration {r.id}</p>
              </li>
            );
          })}
        </ul>
      )}
    </div>
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
        <ul className="space-y-1.5">
          {(q.data ?? []).map((b) => (
            <li
              key={b.id}
              className="flex items-center justify-between gap-2 rounded-[12px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] px-4 py-2.5 text-[13px]"
            >
              <span className="min-w-0 text-[hsl(var(--cx-text))]">
                <Pill>{b.kind.replace(/_/g, " ")}</Pill> {b.hint} · {b.reason}
                <span className="text-[11.5px] text-[hsl(var(--cx-muted))]"> {b.added_by ? `by ${b.added_by}` : ""}</span>
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
          <div className="flex flex-wrap items-center gap-2 text-[13px]">
            <InitialsAvatar name={user.full_name || user.email} seed={user.id} size="md" />
            <span className="font-semibold text-[hsl(var(--cx-text))]">{user.email}</span>
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
          <p className="text-[11.5px] leading-[1.55] text-[hsl(var(--cx-muted))]">
            Reset 2FA only after confirming who they are out of band. It removes every factor and starts a cool-down on sensitive actions.
          </p>
          {act.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(act.error)}</p>}
          {act.isSuccess && <p className="text-sm text-[hsl(var(--cx-live))]">Done.</p>}
        </Card>
      )}
    </div>
  );
}

type NavItem = { id: string; label: string; title: string; lede: string };
const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "Decide",
    items: [
      { id: "queue", label: "Review queue", title: "Review queue", lede: "People waiting to be let in. Oldest first; open one to see their ID and decide." },
      { id: "texting", label: "Texting registrations", title: "Texting registrations", lede: "Self-serve 10DLC filings. Only the stuck ones need you." },
      { id: "purchases", label: "Number purchases", title: "Number purchases", lede: "Paid orders that did not finish provisioning." },
      { id: "alerts", label: "Security alerts", title: "Security alerts", lede: "Sign-ins and actions the system flagged." },
      { id: "ports", label: "Ports & grants", title: "Ports & grants", lede: "Number ports waiting for review, and credit grants waiting for a second operator." },
    ],
  },
  {
    label: "Customers",
    items: [
      { id: "customers", label: "All accounts", title: "All accounts", lede: "Every signup. Find someone, block them, or remove an account." },
      { id: "accounts", label: "Workspaces", title: "Workspaces", lede: "Every workspace and its state." },
      { id: "users", label: "Users", title: "Users", lede: "Unlock an account or reset a second factor." },
    ],
  },
  {
    label: "Money",
    items: [
      { id: "console", label: "Console", title: "Console", lede: "Orgs, profit, traffic and credit tools." },
      { id: "billing", label: "Billing", title: "Billing", lede: "Prices, balances and payments." },
    ],
  },
  {
    label: "Safety",
    items: [
      { id: "monitoring", label: "Monitoring", title: "Monitoring", lede: "Traffic health and paused accounts." },
      { id: "bans", label: "Ban list", title: "Ban list", lede: "Identities that may not sign up again." },
    ],
  },
];
const TABS = NAV_GROUPS.flatMap((g) => g.items);

function RailMark() {
  return (
    <span className="sb-mark" aria-hidden="true">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path d="M3 3v4a5 5 0 0 0 10 0V3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        <circle cx="8" cy="13" r="1.6" fill="currentColor" />
      </svg>
    </span>
  );
}

function useNavCounts() {
  const queue = useOps<{ applications: QueueItem[]; open_security_alerts: number }>(["queue", ""], "/api/v1/ops/queue");
  const purchases = useOps<OpsPurchase[]>(["number-purchases"], "/api/v1/ops/number-purchases");
  const texting = useOps<OpsTextingRegistration[]>(["texting-registrations"], "/api/v1/ops/texting-registrations");
  return {
    queue: queue.data?.applications.length ?? 0,
    alerts: queue.data?.open_security_alerts ?? 0,
    purchases: (purchases.data ?? []).filter((p) => STUCK_PURCHASE_STATES.includes(p.state)).length,
    texting: (texting.data ?? []).filter((t) => t.stage === "needs_attention").length,
  } as Record<string, number>;
}

export function OpsPage() {
  const { me, orgId } = useAuth();
  const [params, setParams] = useSearchParams();
  const tab = TABS.some((t) => t.id === params.get("section")) ? params.get("section")! : "queue";
  const openOrg = params.get("application");
  const setOpenOrg = (application: string | null) =>
    setParams(application ? { section: "queue", application } : { section: "queue" });

  // `!me?.is_platform_operator` is true while /auth/me is still in flight, so the refusal
  // waits until `me` is known (rendering ON truth is safe, rendering on FALSITY is not).
  if (me == null) {
    return (
      <div className="p-6">
        <Spinner label="Checking your access" />
      </div>
    );
  }
  if (!me.is_platform_operator) {
    return (
      <div className="mx-auto max-w-3xl p-6 sm:p-8">
        <SurfaceCard className="p-6">
          <p className="text-[13.5px] text-[hsl(var(--cx-subtle))]">The operator console is for platform operators only.</p>
        </SurfaceCard>
      </div>
    );
  }
  return <Switchboard tab={tab} openOrg={openOrg} setOpenOrg={setOpenOrg} hasWorkspace={Boolean(orgId)} />;
}

function Switchboard({
  tab,
  openOrg,
  setOpenOrg,
  hasWorkspace,
}: {
  tab: string;
  openOrg: string | null;
  setOpenOrg: (id: string | null) => void;
  hasWorkspace: boolean;
}) {
  const { me } = useAuth();
  const counts = useNavCounts();
  const current = TABS.find((t) => t.id === tab)!;
  const eyebrow =
    tab === "queue"
      ? `${counts.queue} waiting`
      : NAV_GROUPS.find((g) => g.items.some((i) => i.id === tab))?.label ?? "";

  return (
    <div className="sb">
      <aside className="sb-rail">
        <div className="sb-brand">
          <RailMark />
          <div>
            <div className="sb-brand-name">Ringlite</div>
            <div className="sb-brand-sub">Switchboard</div>
          </div>
        </div>
        <nav className="sb-nav" aria-label="Administration navigation">
          {NAV_GROUPS.map((group) => (
            <div key={group.label}>
              <div className="sb-group-label">{group.label}</div>
              {group.items.map((item) => (
                <Link key={item.id} to={`?section=${item.id}`} aria-current={tab === item.id && !openOrg ? "page" : undefined}>
                  <span>{item.label}</span>
                  {counts[item.id] > 0 && (
                    <span className="sb-count" aria-hidden="true" data-tone={item.id === "alerts" || item.id === "purchases" ? "stop" : undefined}>
                      {counts[item.id]}
                    </span>
                  )}
                </Link>
              ))}
            </div>
          ))}
        </nav>
        <div className="sb-operator">
          <span>{me?.operator_role ?? "operator"}</span>
          <b title={me?.email}>{me?.email}</b>
          {hasWorkspace && <a href="/inbox">Back to your workspace</a>}
        </div>
      </aside>

      <main className="sb-main">
        {openOrg ? (
          <ApplicationView orgId={openOrg} onBack={() => setOpenOrg(null)} />
        ) : (
          <>
            <header className="sb-head sb-rise">
              <div>
                <div className="sb-eyebrow">{eyebrow}</div>
                <h1 className="sb-title">{current.title}</h1>
                <p className="sb-lede">{current.lede}</p>
              </div>
            </header>
            <section key={tab} aria-label={current.label} className={tab === "queue" ? "" : "sb-panel sb-panel-pad sb-legacy sb-rise"} style={{ "--i": 1 } as React.CSSProperties}>
              {tab === "console" && <ConsoleTab />}
              {tab === "customers" && <CustomerAccountsTab />}
              {tab === "queue" && <QueueTab onOpen={setOpenOrg} />}
              {tab === "alerts" && <AlertsTab />}
              {tab === "bans" && <BanListTab />}
              {tab === "monitoring" && <MonitoringTab />}
              {tab === "billing" && <BillingTab />}
              {tab === "purchases" && <PurchasesTab />}
              {tab === "texting" && <TextingRegistrationsTab />}
              {tab === "ports" && <PortReviewTab />}
              {tab === "accounts" && <AccountsTab />}
              {tab === "users" && <UsersTab />}
            </section>
          </>
        )}
      </main>
    </div>
  );
}
