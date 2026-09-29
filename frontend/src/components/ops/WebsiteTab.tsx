import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import {
  Button,
  EmptyState,
  Pill,
  Section,
  Spinner,
  Textarea,
  mutationErrorMessage,
  type PillTone,
} from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

/* Website chats handed to a person, and "Talk to sales" leads (routes/site.py). */

type ChatStatus = "open" | "waiting" | "active" | "closed";
type ChatKind = "customer" | "visitor";

interface ChatSummary {
  id: string;
  status: ChatStatus;
  kind: ChatKind;
  name: string;
  email: string;
  phone: string | null;
  sms_consent: boolean;
  reason: string | null;
  page: string | null;
  agent_name: string | null;
  org_id: string | null;
  org_name: string | null;
  assigned_user_id: string | null;
  assigned_name: string | null;
  unread: boolean;
  created_at: string;
  last_message_at: string | null;
  last_message: string | null;
}
interface ChatMessage { id: string; role: "visitor" | "assistant" | "agent" | "system"; text: string; at: string }
interface ChatCustomer { org_id: string; org_name: string; plan: string | null; balance_usd: number }
interface ChatDetail extends ChatSummary { messages: ChatMessage[]; customer: ChatCustomer | null }
interface Lead {
  id: string;
  status: "new" | "contacted" | "won" | "lost";
  name: string;
  email: string;
  phone: string | null;
  company: string | null;
  team_size: string;
  numbers_needed: string;
  switching_from: string | null;
  message: string | null;
  sms_consent: boolean;
  plan: string | null;
  page: string | null;
  created_at: string;
}

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
const usd = (value: number) => `$${value.toFixed(2)}`;
const TONE: Record<ChatStatus, PillTone> = { open: "info", waiting: "warning", active: "success", closed: "neutral" };
const ROLE_LABEL = { visitor: "Visitor", assistant: "Assistant", agent: "Ringlite", system: "System" } as const;

type StatusFilter = "open" | "closed" | "all";
type KindFilter = "all" | "customer" | "visitor";

const STATUS_FILTERS: { value: StatusFilter; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "closed", label: "Closed" },
  { value: "all", label: "All" },
];
const KIND_FILTERS: { value: KindFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "customer", label: "Customers" },
  { value: "visitor", label: "Website visitors" },
];

function chatsPath(filters: { status: StatusFilter; kind: KindFilter; mine: boolean }): string {
  const params = new URLSearchParams();
  if (filters.status !== "all") params.set("status", filters.status);
  if (filters.kind !== "all") params.set("kind", filters.kind);
  if (filters.mine) params.set("mine", "true");
  const qs = params.toString();
  return `/api/v1/ops/site/chats${qs ? `?${qs}` : ""}`;
}

function ChatThread({ id }: { id: string }) {
  const { api, me } = useAuth();
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const transcriptRef = useRef<HTMLOListElement | null>(null);
  const readKeyRef = useRef<string | null>(null);
  const chat = useQuery({
    queryKey: ["ops", "site-chat", id],
    queryFn: () => api.request<ChatDetail>(`/api/v1/ops/site/chats/${id}`),
    refetchInterval: 4000,
  });
  const invalidateLists = () => {
    void qc.invalidateQueries({ queryKey: ["ops", "site-chats"] });
    void qc.invalidateQueries({ queryKey: ["ops", "site-chats-unread"] });
  };
  const reply = useMutation({
    mutationFn: () => api.request(`/api/v1/ops/site/chats/${id}/reply`, { method: "POST", json: { text } }),
    onSuccess: () => {
      setText("");
      invalidateLists();
      void qc.invalidateQueries({ queryKey: ["ops", "site-chat", id] });
    },
  });
  const close = useMutation({
    mutationFn: () => api.request(`/api/v1/ops/site/chats/${id}/close`, { method: "POST", json: {} }),
    onSuccess: () => {
      invalidateLists();
      void qc.invalidateQueries({ queryKey: ["ops", "site-chat", id] });
    },
  });
  const assign = useMutation({
    mutationFn: (toMe: boolean) =>
      api.request<{ assigned_user_id: string | null; assigned_name: string | null }>(
        `/api/v1/ops/site/chats/${id}/assign`,
        { method: "POST", json: { to_me: toMe } },
      ),
    onSuccess: () => {
      invalidateLists();
      void qc.invalidateQueries({ queryKey: ["ops", "site-chat", id] });
    },
  });

  // Opening a chat that is still unread clears it once per (chat id + last visitor message).
  const unread = chat.data?.unread ?? false;
  const lastMessageAt = chat.data?.last_message_at ?? null;
  useEffect(() => {
    if (!unread) return;
    const key = `${id}:${lastMessageAt ?? ""}`;
    if (readKeyRef.current === key) return;
    readKeyRef.current = key;
    api
      .request(`/api/v1/ops/site/chats/${id}/read`, { method: "POST", json: {} })
      .then(() => {
        // Same effect as replying: the list badge and its totals both move.
        void qc.invalidateQueries({ queryKey: ["ops", "site-chats"] });
        void qc.invalidateQueries({ queryKey: ["ops", "site-chats-unread"] });
      })
      .catch(() => {
        // The badge simply stays lit until the next successful poll.
      });
  }, [api, id, lastMessageAt, qc, unread]);

  // The transcript follows the newest message, as a chat client does.
  const messageCount = chat.data?.messages.length ?? 0;
  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [id, messageCount]);

  if (chat.isLoading || !chat.data) return <Spinner label="Loading chat" />;
  const c = chat.data;
  const isMine = c.assigned_user_id != null && c.assigned_user_id === me?.id;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="text-sm">
          <div className="font-medium text-foreground">{c.name} · {c.email}{c.phone ? ` · ${c.phone}` : ""}</div>
          <div className="text-muted-foreground">
            {c.reason ?? "—"} · from {c.page ?? "—"} · {c.sms_consent ? "OK to text" : "no texts"} · started {when(c.created_at)}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">
            {c.assigned_name ? `Assigned to ${c.assigned_name}` : "Unassigned"}
          </span>
          {isMine ? (
            <Button type="button" variant="outline" size="sm" onClick={() => assign.mutate(false)} disabled={assign.isPending}>
              Unassign
            </Button>
          ) : (
            <Button type="button" variant="outline" size="sm" onClick={() => assign.mutate(true)} disabled={assign.isPending}>
              Assign to me
            </Button>
          )}
        </div>
      </div>
      {c.customer ? (
        <div className="rounded-md border border-border bg-background p-3 text-sm">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
            <dt className="text-muted-foreground">Workspace</dt>
            <dd>{c.customer.org_name}</dd>
            <dt className="text-muted-foreground">Plan</dt>
            <dd>{c.customer.plan ?? "—"}</dd>
            <dt className="text-muted-foreground">Balance</dt>
            <dd>{usd(c.customer.balance_usd)}</dd>
          </dl>
        </div>
      ) : null}
      <ol ref={transcriptRef} className="max-h-96 space-y-2 overflow-y-auto rounded-md border border-border p-3" aria-live="polite">
        {c.messages.map((m) => (
          <li key={m.id} className={m.role === "agent" ? "text-right" : ""}>
            <div className="text-xs text-muted-foreground">
              {m.role === "visitor" && c.kind === "customer" ? "Customer" : ROLE_LABEL[m.role]} · {when(m.at)}
            </div>
            <div className="inline-block max-w-[85%] whitespace-pre-wrap rounded-md border border-border px-3 py-2 text-left text-sm">{m.text}</div>
          </li>
        ))}
      </ol>
      {c.status !== "closed" ? (
        <form
          className="space-y-2"
          onSubmit={(e) => { e.preventDefault(); if (text.trim()) reply.mutate(); }}
        >
          <label htmlFor={`reply-${id}`} className="block text-sm font-medium">Reply to {c.name}</label>
          <Textarea
            id={`reply-${id}`}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends, Shift+Enter is a newline.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (text.trim() && !reply.isPending) reply.mutate();
              }
            }}
            rows={3}
            maxLength={2000}
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" disabled={reply.isPending || !text.trim()}>{reply.isPending ? "Sending…" : "Send reply"}</Button>
            <Button type="button" variant="outline" onClick={() => close.mutate()} disabled={close.isPending}>Close chat</Button>
            {reply.isError ? <span className="text-sm text-destructive">{mutationErrorMessage(reply.error)}</span> : null}
          </div>
        </form>
      ) : (
        <p className="text-sm text-muted-foreground">This chat is closed. Follow up by email if needed.</p>
      )}
    </div>
  );
}

function ChatsSection() {
  const { api } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedId = searchParams.get("chat");
  const [status, setStatus] = useState<StatusFilter>("open");
  const [kind, setKind] = useState<KindFilter>("all");
  const [mine, setMine] = useState(false);
  const filters = { status, kind, mine };
  const chats = useQuery({
    queryKey: ["ops", "site-chats", filters],
    queryFn: () => api.request<{ chats: ChatSummary[]; staffed: boolean }>(chatsPath(filters)),
    refetchInterval: 5000,
  });

  // The open chat lives in the URL (?chat=<id>) so a push notification can link straight to it,
  // while every other param on /ops (notably ?section=) is preserved.
  const selectChat = (id: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("chat", id);
    setSearchParams(next, { replace: true });
  };

  const list = chats.data?.chats ?? [];
  const unreadCount = list.filter((c) => c.unread).length;

  return (
    <Section
      title="Support chats"
      description={chats.data ? (chats.data.staffed ? "Staffed hours now: visitors are told a person is joining." : "Outside staffed hours: visitors are told we reply by email.") : undefined}
      actions={
        unreadCount > 0 ? (
          <Pill tone="info" aria-label={`${unreadCount} unread chats`}>{unreadCount} unread</Pill>
        ) : null
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Status" className="flex flex-wrap items-center gap-1">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              aria-pressed={status === f.value}
              onClick={() => setStatus(f.value)}
              className={cn(
                "rounded-full border border-border px-3 py-1 text-xs",
                status === f.value ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted",
              )}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div role="group" aria-label="Kind" className="flex flex-wrap items-center gap-1">
          {KIND_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              aria-pressed={kind === f.value}
              onClick={() => setKind(f.value)}
              className={cn(
                "rounded-full border border-border px-3 py-1 text-xs",
                kind === f.value ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted",
              )}
            >
              {f.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          aria-pressed={mine}
          onClick={() => setMine((value) => !value)}
          className={cn(
            "rounded-full border border-border px-3 py-1 text-xs",
            mine ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted",
          )}
        >
          Mine
        </button>
      </div>

      {chats.isLoading ? (
        <Spinner label="Loading chats" />
      ) : (
        <div className="grid gap-4 md:grid-cols-[320px_1fr]">
          <div className="min-w-0">
            {list.length === 0 ? (
              <EmptyState title="No chats yet" description="When a website visitor asks for a person, the conversation appears here." />
            ) : (
              <ul className="space-y-2 md:max-h-[70vh] md:overflow-y-auto" aria-label="Support chats">
                {list.map((c) => {
                  const isSelected = c.id === selectedId;
                  return (
                    <li key={c.id}>
                      <button
                        type="button"
                        aria-current={isSelected ? "true" : undefined}
                        onClick={() => selectChat(c.id)}
                        className={cn(
                          "w-full rounded-md border border-border bg-background p-3 text-left",
                          isSelected ? "bg-muted ring-1 ring-border" : "hover:bg-muted",
                        )}
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          {c.unread ? (
                            <>
                              <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full bg-blue-500" />
                              <span className="sr-only">Unread</span>
                            </>
                          ) : null}
                          <span className="font-semibold text-foreground">{c.name}</span>
                          {c.kind === "customer" ? (
                            <>
                              <Pill tone="info">Customer</Pill>
                              {c.org_name ? <span className="text-xs text-muted-foreground">{c.org_name}</span> : null}
                            </>
                          ) : (
                            <Pill tone="neutral">Visitor</Pill>
                          )}
                          <Pill tone={TONE[c.status]}>{c.status}</Pill>
                          <span className="ml-auto text-xs text-muted-foreground">{when(c.last_message_at)}</span>
                        </div>
                        <div className="mt-1 text-xs text-muted-foreground">
                          {c.assigned_name ?? "Unassigned"}
                        </div>
                        {c.last_message ? <div className="mt-1 truncate text-sm text-muted-foreground">{c.last_message}</div> : null}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
          <div className="min-w-0">
            {selectedId ? (
              <ChatThread id={selectedId} />
            ) : (
              <EmptyState title="No chat selected" description="Pick a conversation from the list to read and reply." />
            )}
          </div>
        </div>
      )}
    </Section>
  );
}

function LeadsSection() {
  const { api } = useAuth();
  const qc = useQueryClient();
  const leads = useQuery({
    queryKey: ["ops", "site-leads"],
    queryFn: () => api.request<{ leads: Lead[] }>("/api/v1/ops/site/leads"),
  });
  const setStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: Lead["status"] }) =>
      api.request(`/api/v1/ops/site/leads/${id}/status`, { method: "POST", json: { status } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["ops", "site-leads"] }),
  });
  return (
    <Section title="Talk to sales" description="Every enquiry from the website form, newest first.">
      {leads.isLoading ? <Spinner label="Loading leads" /> : !leads.data?.leads.length ? (
        <EmptyState title="No enquiries yet" description="Submissions from ringlite.io/sales appear here." />
      ) : (
        <ul className="space-y-2">
          {leads.data.leads.map((l) => (
            <li key={l.id} className="rounded-md border border-border p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{l.name}</span>
                <span className="text-muted-foreground">{l.email}{l.phone ? ` · ${l.phone}` : ""}</span>
                <span className="ml-auto text-xs text-muted-foreground">{when(l.created_at)}</span>
              </div>
              <div className="mt-1 text-muted-foreground">
                {l.company ?? "No company"} · team {l.team_size} · numbers {l.numbers_needed} · from {l.switching_from ?? "—"}
                {l.plan ? ` · plan ${l.plan}` : ""} · {l.sms_consent ? "OK to text" : "no texts"}
              </div>
              {l.message ? <p className="mt-1 whitespace-pre-wrap">{l.message}</p> : null}
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <label htmlFor={`lead-${l.id}`} className="text-muted-foreground">Status</label>
                <select
                  id={`lead-${l.id}`}
                  className="rounded-md border border-border bg-background px-2 py-1"
                  value={l.status}
                  onChange={(e) => setStatus.mutate({ id: l.id, status: e.target.value as Lead["status"] })}
                >
                  <option value="new">New</option>
                  <option value="contacted">Contacted</option>
                  <option value="won">Won</option>
                  <option value="lost">Lost</option>
                </select>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

export function WebsiteTab() {
  return (
    <div className="space-y-6">
      <ChatsSection />
      <LeadsSection />
    </div>
  );
}
