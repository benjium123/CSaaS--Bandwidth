import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { Button, Card, Pill, Spinner, Textarea, mutationErrorMessage } from "@/components/ui/primitives";
import { InitialsAvatar } from "@/components/ui/consoleChrome";

/** P43: operators' view of the AI traffic monitor - paused and flagged accounts with the
 * AI case file, texts waiting for a decision, and the daily report with the monitor's own
 * health (canary + exam). */

type QueueRow = {
  org_id: string;
  org_name: string;
  level: string;
  score: number;
  paused_at: string | null;
  appealed: boolean;
  case_status: string | null;
  recommendation: string | null;
};

type CaseFile = {
  status: string;
  summary?: string;
  what_they_claim?: string;
  what_we_saw?: string[];
  evidence_quotes?: { source: string; quote: string }[];
  false_alarm_signs?: string[];
  recommendation?: string;
  confidence?: number;
};

type CaseDetail = {
  org_id: string;
  org_name: string;
  level: string;
  score: number;
  paused_reason: string | null;
  case_file: CaseFile | null;
  appeal: string | null;
  signals: { id: string; kind: string; weight: number; summary: string; at: string | null }[];
  texts: { id: string; body: string | null; to: string; state: string; reason: string | null; at: string | null }[];
  calls: { call_id: string; verdict: string; confidence: number | null; summary: string | null; evidence: { speaker: string; quote: string }[] | null; at: string | null }[];
};

type HeldText = { id: string; org_id: string; body: string | null; to: string; reason: string | null; at: string | null };

type Report = {
  texts: { checked: number; allowed: number; held_now: number; blocked: number };
  calls: { reviewed: number; ok: number; suspicious: number; scam: number; waiting: number };
  accounts: Record<string, number>;
  public_reports: number;
  ai_tokens: { in: number; out: number };
  health: Record<string, { passed: boolean; at: string | null; detail: Record<string, unknown> | null } | null>;
};

const LEVEL_TONE: Record<string, "danger" | "warning" | "info" | "neutral"> = {
  paused: "danger",
  restricted: "warning",
  watch: "info",
  normal: "neutral",
};

const VERDICT_TONE: Record<string, "danger" | "warning" | "success"> = {
  scam: "danger",
  suspicious: "warning",
  ok: "success",
};

function useMonitorAction() {
  const { api } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ path, json }: { path: string; json?: unknown }) =>
      api.request(path, { method: "POST", json: json ?? {} }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["ops", "monitoring"] }),
  });
}

function ReportCard() {
  const { api } = useAuth();
  const q = useQuery({
    queryKey: ["ops", "monitoring", "report"],
    queryFn: () => api.request<Report>("/api/v1/ops/monitoring/report"),
  });
  if (q.isPending) return <Spinner label="Loading monitor report" />;
  if (q.isError || !q.data) return <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(q.error)}</p>;
  const r = q.data;
  const health = (kind: string, label: string) => {
    const h = r.health[kind];
    if (!h) return <Pill tone="neutral">{label}: not run yet</Pill>;
    return (
      <Pill tone={h.passed ? "success" : "danger"}>
        {label}: {h.passed ? "passing" : "FAILING"}
        {h.at ? ` (${new Date(h.at).toLocaleString()})` : ""}
      </Pill>
    );
  };
  return (
    <Card className="space-y-[11px]">
      <p className="text-sm font-medium">Last 24 hours</p>
      <div className="flex flex-wrap gap-[11px]">
        {health("canary", "Canary")}
        {health("exam", "Weekly exam")}
      </div>
      <p className="text-sm">
        Texts checked {r.texts.checked} · allowed {r.texts.allowed} · held now {r.texts.held_now} · blocked {r.texts.blocked}
      </p>
      <p className="text-sm">
        Calls reviewed {r.calls.reviewed} · ok {r.calls.ok} · suspicious {r.calls.suspicious} · scam {r.calls.scam} · waiting {r.calls.waiting}
      </p>
      <p className="text-sm">
        Accounts: {Object.entries(r.accounts).map(([k, v]) => `${k} ${v}`).join(" · ") || "none flagged"} · public reports {r.public_reports}
      </p>
      <p className="text-xs text-muted-foreground">
        AI usage: {r.ai_tokens.in.toLocaleString()} tokens in / {r.ai_tokens.out.toLocaleString()} out
      </p>
    </Card>
  );
}

function HeldTexts() {
  const { api } = useAuth();
  const q = useQuery({
    queryKey: ["ops", "monitoring", "held"],
    queryFn: () => api.request<HeldText[]>("/api/v1/ops/monitoring/held-texts"),
  });
  const act = useMonitorAction();
  if (q.isPending) return <Spinner label="Loading held texts" />;
  const rows = q.data ?? [];
  return (
    <Card className="space-y-[11px]">
      <p className="text-sm font-medium">Texts waiting for a decision</p>
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">None - the AI's second look handles most holds by itself.</p>
      ) : (
        <ul className="space-y-[11px]">
          {rows.map((t) => (
            <li key={t.id} className="space-y-[9px] rounded-[var(--cx-r-md,14px)] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] p-[14px]">
              <p className="whitespace-pre-wrap text-sm">{t.body}</p>
              <p className="text-xs text-muted-foreground">
                to {t.to} · {t.reason ?? "held"} · {t.at ? new Date(t.at).toLocaleString() : ""}
              </p>
              <div className="flex gap-[11px]">
                <Button type="button" size="sm" variant="outline" disabled={act.isPending} onClick={() => act.mutate({ path: `/api/v1/ops/monitoring/texts/${t.id}`, json: { decision: "release" } })}>
                  Release
                </Button>
                <Button type="button" size="sm" variant="destructive" disabled={act.isPending} onClick={() => act.mutate({ path: `/api/v1/ops/monitoring/texts/${t.id}`, json: { decision: "block" } })}>
                  Block
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {act.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(act.error)}</p>}
    </Card>
  );
}

function CaseView({ orgId, onBack }: { orgId: string; onBack: () => void }) {
  const { api, me } = useAuth();
  const isAdmin = me?.operator_role === "admin";
  const q = useQuery({
    queryKey: ["ops", "monitoring", "case", orgId],
    queryFn: () => api.request<CaseDetail>(`/api/v1/ops/monitoring/orgs/${orgId}`),
  });
  const act = useMonitorAction();
  const [note, setNote] = React.useState("");
  if (q.isPending) return <Spinner label="Loading case" />;
  if (q.isError || !q.data) return <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(q.error)}</p>;
  const c = q.data;
  const file = c.case_file;
  return (
    <div className="space-y-[14px]">
      <Button type="button" variant="ghost" onClick={onBack}>
        ← Back to monitoring
      </Button>
      <div className="flex flex-wrap items-center gap-[11px]">
        <InitialsAvatar name={c.org_name} seed={c.org_id} size="lg" />
        <h2 className="text-[19px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]">{c.org_name}</h2>
        <Pill tone={LEVEL_TONE[c.level] ?? "neutral"}>{c.level}</Pill>
        <span className="text-sm text-muted-foreground">risk score {c.score}</span>
      </div>
      {c.paused_reason && <p className="text-sm text-muted-foreground">{c.paused_reason}</p>}

      <Card className="space-y-[11px]">
        <p className="text-sm font-medium">AI case file</p>
        {!file || file.status === "pending" ? (
          <p className="text-sm text-muted-foreground">The AI is writing the case file.</p>
        ) : file.status === "unavailable" ? (
          <p className="text-sm text-muted-foreground">The AI couldn't write it yet - the evidence is below.</p>
        ) : (
          <>
            <div className="flex flex-wrap gap-[11px]">
              {file.recommendation && (
                <Pill tone={file.recommendation === "unpause" ? "success" : "danger"}>
                  AI recommends: {file.recommendation.replace(/_/g, " ")}
                </Pill>
              )}
              {file.confidence != null && <span className="text-sm text-muted-foreground">{file.confidence}% confident</span>}
            </div>
            <p className="text-sm">{file.summary}</p>
            {file.what_they_claim && <p className="text-sm text-muted-foreground">Claims to be: {file.what_they_claim}</p>}
            {(file.what_we_saw ?? []).length > 0 && (
              <ul className="list-disc pl-5 text-sm">
                {file.what_we_saw!.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            )}
            {(file.evidence_quotes ?? []).length > 0 && (
              <ul className="space-y-[9px]">
                {file.evidence_quotes!.map((e, i) => (
                  <li key={i} className="rounded-[var(--cx-r-sm,12px)] bg-[hsl(var(--cx-overlay))] px-[11px] py-[9px] text-[13.5px]">
                    <span className="text-xs text-muted-foreground">{e.source}: </span>“{e.quote}”
                  </li>
                ))}
              </ul>
            )}
            {(file.false_alarm_signs ?? []).length > 0 && (
              <div>
                <p className="text-xs font-medium text-muted-foreground">Could be a false alarm because</p>
                <ul className="list-disc pl-5 text-sm">
                  {file.false_alarm_signs!.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </Card>

      {c.appeal && (
        <Card>
          <p className="text-sm font-medium">The business says</p>
          <p className="whitespace-pre-wrap text-sm">{c.appeal}</p>
        </Card>
      )}

      {isAdmin ? (
      <Card className="space-y-[11px]">
        <p className="text-sm font-medium">Decide</p>
        <Textarea aria-label="Decision note" rows={2} placeholder="What you checked and why" value={note} onChange={(e) => setNote(e.target.value)} />
        <div className="flex flex-wrap gap-[11px]">
          <Button type="button" variant="outline" disabled={note.trim().length < 3 || act.isPending} onClick={() => act.mutate({ path: `/api/v1/ops/monitoring/orgs/${orgId}/unpause`, json: { note } })}>
            False alarm - unpause
          </Button>
          <Button type="button" variant="destructive" disabled={note.trim().length < 3 || act.isPending} onClick={() => act.mutate({ path: `/api/v1/ops/monitoring/orgs/${orgId}/suspend`, json: { note } })}>
            Confirmed - suspend and ban
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">Your decision also teaches the monitor: the texts and calls below are saved as examples for its exam.</p>
        {act.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(act.error)}</p>}
      </Card>
      ) : (
        <p className="text-xs text-muted-foreground">Only admin operators can unpause or suspend an account.</p>
      )}

      <Card className="space-y-[11px]">
        <p className="text-sm font-medium">Signals (last 30 days)</p>
        <ul className="space-y-[9px]">
          {c.signals.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center gap-[11px] text-[13.5px]">
              <Pill tone="neutral">+{s.weight}</Pill>
              <span>{s.summary}</span>
              <span className="text-xs text-muted-foreground">{s.at ? new Date(s.at).toLocaleString() : ""}</span>
            </li>
          ))}
        </ul>
      </Card>

      {c.texts.length > 0 && (
        <Card className="space-y-[11px]">
          <p className="text-sm font-medium">Flagged texts</p>
          {c.texts.map((t) => (
            <div key={t.id} className="rounded-[var(--cx-r-md,14px)] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] p-[14px] text-[13.5px]">
              <Pill tone={t.state === "blocked" ? "danger" : "warning"}>{t.state}</Pill> <span className="whitespace-pre-wrap">{t.body}</span>
              {t.reason && <p className="text-xs text-muted-foreground">{t.reason}</p>}
            </div>
          ))}
        </Card>
      )}

      {c.calls.length > 0 && (
        <Card className="space-y-[11px]">
          <p className="text-sm font-medium">Reviewed calls</p>
          {c.calls.map((call) => (
            <div key={call.call_id} className="space-y-[6px] rounded-[var(--cx-r-md,14px)] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] p-[14px] text-[13.5px]">
              <Pill tone={VERDICT_TONE[call.verdict] ?? "neutral"}>{call.verdict}</Pill> {call.summary}
              {(call.evidence ?? []).map((e, i) => (
                <p key={i} className="text-xs text-muted-foreground">
                  {e.speaker}: “{e.quote}”
                </p>
              ))}
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}

export function MonitoringTab() {
  const { api } = useAuth();
  const [openOrg, setOpenOrg] = React.useState<string | null>(null);
  const q = useQuery({
    queryKey: ["ops", "monitoring", "queue"],
    queryFn: () => api.request<QueueRow[]>("/api/v1/ops/monitoring"),
    enabled: openOrg === null,
  });
  if (openOrg) return <CaseView orgId={openOrg} onBack={() => setOpenOrg(null)} />;
  return (
    <div className="space-y-[14px]">
      <ReportCard />
      <Card className="space-y-[11px]">
        <p className="text-sm font-medium">Accounts the monitor flagged</p>
        {q.isPending ? (
          <Spinner label="Loading flagged accounts" />
        ) : (q.data ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing flagged.</p>
        ) : (
          <ul className="space-y-[9px]">
            {q.data!.map((row) => (
              <li key={row.org_id}>
                {/* A 12px list row, not a boxed table cell. The avatar is decorative — the
                    org name it stands for is the next thing in the row. */}
                <button
                  type="button"
                  className="flex w-full flex-wrap items-center gap-[11px] rounded-[var(--cx-r-sm,12px)] px-[11px] py-[9px] text-left text-[13.5px] transition-colors hover:bg-[hsl(var(--cx-overlay))]"
                  onClick={() => setOpenOrg(row.org_id)}
                >
                  <InitialsAvatar name={row.org_name} seed={row.org_id} size="sm" />
                  <Pill tone={LEVEL_TONE[row.level] ?? "neutral"}>{row.level}</Pill>
                  <span className="font-semibold text-[hsl(var(--cx-text))]">{row.org_name}</span>
                  <span className="text-[hsl(var(--cx-muted))]">score {row.score}</span>
                  {row.recommendation && <span className="text-[hsl(var(--cx-muted))]">· AI: {row.recommendation.replace(/_/g, " ")}</span>}
                  {row.appealed && <Pill tone="info">appealed</Pill>}
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <HeldTexts />
    </div>
  );
}
