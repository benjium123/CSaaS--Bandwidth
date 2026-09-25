import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import { Button, EmptyState, Pill, Section, Spinner, Textarea, mutationErrorMessage } from "@/components/ui/primitives";

/* Website chats handed to a person, and "Talk to sales" leads (routes/site.py). */

interface ChatSummary {
  id: string;
  status: "waiting" | "active" | "closed";
  name: string;
  email: string;
  phone: string | null;
  sms_consent: boolean;
  reason: string | null;
  page: string | null;
  agent_name: string | null;
  created_at: string;
  last_message_at: string | null;
  last_message: string | null;
}
interface ChatMessage { id: string; role: "visitor" | "assistant" | "agent" | "system"; text: string; at: string }
interface ChatDetail extends ChatSummary { messages: ChatMessage[] }
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
const TONE = { waiting: "warning", active: "success", closed: "neutral" } as const;
const ROLE_LABEL = { visitor: "Visitor", assistant: "Assistant", agent: "Ringlite", system: "System" } as const;

function ChatThread({ id }: { id: string }) {
  const { api } = useAuth();
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const chat = useQuery({
    queryKey: ["ops", "site-chat", id],
    queryFn: () => api.request<ChatDetail>(`/api/v1/ops/site/chats/${id}`),
    refetchInterval: 4000,
  });
  const done = () => void qc.invalidateQueries({ queryKey: ["ops"] });
  const reply = useMutation({
    mutationFn: () => api.request(`/api/v1/ops/site/chats/${id}/reply`, { method: "POST", json: { text } }),
    onSuccess: () => { setText(""); done(); },
  });
  const close = useMutation({
    mutationFn: () => api.request(`/api/v1/ops/site/chats/${id}/close`, { method: "POST", json: {} }),
    onSuccess: done,
  });
  if (chat.isLoading || !chat.data) return <Spinner label="Loading chat" />;
  const c = chat.data;
  return (
    <div className="space-y-3">
      <div className="text-sm">
        <div className="font-medium">{c.name} · {c.email}{c.phone ? ` · ${c.phone}` : ""}</div>
        <div className="text-slate-500">
          {c.reason ?? "—"} · from {c.page ?? "—"} · {c.sms_consent ? "OK to text" : "no texts"} · started {when(c.created_at)}
        </div>
      </div>
      <ol className="max-h-96 space-y-2 overflow-y-auto rounded-md border border-slate-200 p-3" aria-live="polite">
        {c.messages.map((m) => (
          <li key={m.id} className={m.role === "agent" ? "text-right" : ""}>
            <div className="text-xs text-slate-500">{ROLE_LABEL[m.role]} · {when(m.at)}</div>
            <div className="inline-block max-w-[85%] whitespace-pre-wrap rounded-md border border-slate-200 px-3 py-2 text-left text-sm">{m.text}</div>
          </li>
        ))}
      </ol>
      {c.status !== "closed" ? (
        <form
          className="space-y-2"
          onSubmit={(e) => { e.preventDefault(); if (text.trim()) reply.mutate(); }}
        >
          <label htmlFor={`reply-${id}`} className="block text-sm font-medium">Reply to {c.name}</label>
          <Textarea id={`reply-${id}`} value={text} onChange={(e) => setText(e.target.value)} rows={3} maxLength={2000} />
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" disabled={reply.isPending || !text.trim()}>{reply.isPending ? "Sending…" : "Send reply"}</Button>
            <Button type="button" variant="outline" onClick={() => close.mutate()} disabled={close.isPending}>Close chat</Button>
            {reply.isError ? <span className="text-sm text-red-600">{mutationErrorMessage(reply.error)}</span> : null}
          </div>
        </form>
      ) : (
        <p className="text-sm text-slate-500">This chat is closed. Follow up by email if needed.</p>
      )}
    </div>
  );
}

function ChatsSection() {
  const { api } = useAuth();
  const [open, setOpen] = useState<string | null>(null);
  const chats = useQuery({
    queryKey: ["ops", "site-chats"],
    queryFn: () => api.request<{ chats: ChatSummary[]; staffed: boolean }>("/api/v1/ops/site/chats"),
    refetchInterval: 8000,
  });
  return (
    <Section
      title="Website chats"
      description={chats.data ? (chats.data.staffed ? "Staffed hours now: visitors are told a person is joining." : "Outside staffed hours: visitors are told we reply by email.") : undefined}
    >
      {chats.isLoading ? <Spinner label="Loading chats" /> : !chats.data?.chats.length ? (
        <EmptyState title="No chats yet" description="When a website visitor asks for a person, the conversation appears here." />
      ) : (
        <ul className="space-y-2">
          {chats.data.chats.map((c) => (
            <li key={c.id} className="rounded-md border border-slate-200 p-3">
              <button type="button" className="flex w-full flex-wrap items-center gap-2 text-left" onClick={() => setOpen(open === c.id ? null : c.id)} aria-expanded={open === c.id}>
                <Pill tone={TONE[c.status]}>{c.status}</Pill>
                <span className="font-medium">{c.name}</span>
                <span className="text-sm text-slate-500">{c.email}</span>
                <span className="ml-auto text-xs text-slate-500">{when(c.last_message_at)}</span>
                {c.last_message ? <span className="w-full truncate text-sm text-slate-600">{c.last_message}</span> : null}
              </button>
              {open === c.id ? <div className="mt-3 border-t border-slate-200 pt-3"><ChatThread id={c.id} /></div> : null}
            </li>
          ))}
        </ul>
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
            <li key={l.id} className="rounded-md border border-slate-200 p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{l.name}</span>
                <span className="text-slate-500">{l.email}{l.phone ? ` · ${l.phone}` : ""}</span>
                <span className="ml-auto text-xs text-slate-500">{when(l.created_at)}</span>
              </div>
              <div className="mt-1 text-slate-600">
                {l.company ?? "No company"} · team {l.team_size} · numbers {l.numbers_needed} · from {l.switching_from ?? "—"}
                {l.plan ? ` · plan ${l.plan}` : ""} · {l.sms_consent ? "OK to text" : "no texts"}
              </div>
              {l.message ? <p className="mt-1 whitespace-pre-wrap">{l.message}</p> : null}
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <label htmlFor={`lead-${l.id}`} className="text-slate-500">Status</label>
                <select
                  id={`lead-${l.id}`}
                  className="rounded-md border border-slate-200 bg-transparent px-2 py-1"
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
