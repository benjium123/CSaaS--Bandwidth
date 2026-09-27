import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getErrorMessage } from "@/api/contacts";
import { useAuth } from "@/auth/AuthContext";
import { Button, EmptyState, Input, Pill, Section, Spinner, Textarea } from "@/components/ui/primitives";

/* Support requests raised from the in-app help menu, and the contacts it shows (routes/ops_console.py). */

type SupportStatus = "open" | "answered" | "closed";

interface SupportRequest {
  id: string;
  org_id: string | null;
  org_name: string | null;
  email: string;
  subject: string;
  body: string;
  page: string | null;
  status: SupportStatus;
  reply: string | null;
  replied_at: string | null;
  created_at: string;
}

interface SupportContacts {
  email: string | null;
  phone: string | null;
  knowledge_base_url: string | null;
  whats_new_url: string | null;
  status_url: string | null;
  terms_url: string | null;
  privacy_url: string | null;
}

interface ReplyResult {
  emailed?: boolean;
}

const LIST_KEY = ["ops", "support-requests"] as const;
const CONTACTS_KEY = ["ops", "support-contacts"] as const;

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");

const TONE: Record<SupportStatus, "warning" | "success" | "neutral"> = {
  open: "warning",
  answered: "success",
  closed: "neutral",
};

const FILTERS = [
  { id: "open", label: "Open" },
  { id: "answered", label: "Answered" },
  { id: "closed", label: "Closed" },
  { id: "all", label: "All" },
] as const;

type FilterId = (typeof FILTERS)[number]["id"];

const FIELDS = [
  { key: "email", label: "Support email" },
  { key: "phone", label: "Support phone" },
  { key: "knowledge_base_url", label: "Knowledge base URL" },
  { key: "whats_new_url", label: "What's new URL" },
  { key: "status_url", label: "Status page URL" },
  { key: "terms_url", label: "Terms URL" },
  { key: "privacy_url", label: "Privacy URL" },
] as const;

const emptyForm = (): Record<string, string> => ({
  email: "",
  phone: "",
  knowledge_base_url: "",
  whats_new_url: "",
  status_url: "",
  terms_url: "",
  privacy_url: "",
});

function RequestItem({ request }: { request: SupportRequest }) {
  const { api } = useAuth();
  const qc = useQueryClient();
  const [reply, setReply] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: ({ text, close }: { text: string; close: boolean }) =>
      api.request<ReplyResult>(`/api/v1/ops/console/support/${request.id}/reply`, {
        method: "POST",
        json: { reply: text, close },
      }),
    onSuccess: (data) => {
      setReply("");
      setNotice(data?.emailed ? "Emailed" : "Saved (email failed)");
      void qc.invalidateQueries({ queryKey: LIST_KEY });
    },
  });

  const canReply = request.status !== "closed";

  return (
    <li className="rounded-md border border-slate-200 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={TONE[request.status]}>{request.status}</Pill>
        <span className="font-medium">{request.subject}</span>
        <span className="text-slate-500">
          {request.org_name ?? "—"} · {request.email}
        </span>
        <span className="ml-auto text-xs text-slate-500">{when(request.created_at)}</span>
      </div>
      <div className="mt-1 text-xs text-slate-500">{request.page ?? "—"}</div>
      <p className="mt-2 whitespace-pre-wrap">{request.body}</p>
      {request.reply ? (
        <div className="mt-2 rounded-md bg-slate-50 p-2">
          <div className="text-xs text-slate-500">Replied {when(request.replied_at)}</div>
          <p className="whitespace-pre-wrap">{request.reply}</p>
        </div>
      ) : null}
      {canReply ? (
        <div className="mt-3 space-y-2">
          <Textarea
            aria-label={`Reply to ${request.subject}`}
            rows={3}
            maxLength={2000}
            value={reply}
            onChange={(e) => setReply(e.target.value)}
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              onClick={() => send.mutate({ text: reply, close: false })}
              disabled={send.isPending}
            >
              Send reply
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => send.mutate({ text: reply, close: true })}
              disabled={send.isPending}
            >
              Reply & close
            </Button>
            {notice ? <span className="text-slate-500">{notice}</span> : null}
            {send.isError ? <span className="text-red-600">{getErrorMessage(send.error)}</span> : null}
          </div>
        </div>
      ) : null}
    </li>
  );
}

function RequestsSection() {
  const { api } = useAuth();
  const [status, setStatus] = useState<FilterId>("open");
  const requests = useQuery({
    queryKey: ["ops", "support-requests", status],
    queryFn: () =>
      api.request<{ requests: SupportRequest[] }>(
        status === "all" ? "/api/v1/ops/console/support" : `/api/v1/ops/console/support?status=${status}`,
      ),
  });

  return (
    <Section title="Support requests" description="Messages sent from the in-app help menu, newest first.">
      <div className="mb-3 flex flex-wrap items-center gap-2" role="group" aria-label="Filter by status">
        {FILTERS.map((filter) => {
          const active = status === filter.id;
          return (
            <button
              key={filter.id}
              type="button"
              aria-pressed={active}
              onClick={() => setStatus(filter.id)}
              className={
                active
                  ? "rounded-md border border-slate-900 bg-slate-900 px-3 py-1 text-sm text-white"
                  : "rounded-md border border-slate-200 px-3 py-1 text-sm text-slate-700 hover:bg-slate-50"
              }
            >
              {filter.label}
            </button>
          );
        })}
      </div>
      {requests.isLoading ? (
        <Spinner label="Loading support requests" />
      ) : !requests.data?.requests.length ? (
        <EmptyState title="No requests" description="Nothing has been sent from the help menu with this status." />
      ) : (
        <ul className="space-y-2">
          {requests.data.requests.map((request) => (
            <RequestItem key={request.id} request={request} />
          ))}
        </ul>
      )}
    </Section>
  );
}

function ContactsSection() {
  const { api } = useAuth();
  const [form, setForm] = useState<Record<string, string>>(emptyForm);

  const contacts = useQuery({
    queryKey: CONTACTS_KEY,
    queryFn: () => api.request<SupportContacts>("/api/v1/ops/console/support-contacts"),
  });

  useEffect(() => {
    const c = contacts.data;
    if (!c) return;
    setForm({
      email: c.email ?? "",
      phone: c.phone ?? "",
      knowledge_base_url: c.knowledge_base_url ?? "",
      whats_new_url: c.whats_new_url ?? "",
      status_url: c.status_url ?? "",
      terms_url: c.terms_url ?? "",
      privacy_url: c.privacy_url ?? "",
    });
  }, [contacts.data]);

  const save = useMutation({
    mutationFn: () => {
      const json: Record<string, string | null> = {};
      for (const field of FIELDS) json[field.key] = form[field.key] === "" ? null : form[field.key];
      return api.request("/api/v1/ops/console/support-contacts", { method: "PUT", json });
    },
  });

  return (
    <details className="rounded-md border border-slate-200">
      <summary className="cursor-pointer px-4 py-3 text-sm font-medium">Help menu contacts</summary>
      <form
        className="space-y-3 border-t border-slate-200 p-4"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <div className="grid gap-3 sm:grid-cols-2">
          {FIELDS.map((field) => (
            <label key={field.key} className="block text-sm">
              <span className="mb-1 block text-slate-600">{field.label}</span>
              <Input
                value={form[field.key]}
                onChange={(e) => setForm((prev) => ({ ...prev, [field.key]: e.target.value }))}
              />
            </label>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
          {save.isSuccess ? <span className="text-sm text-slate-500">Saved</span> : null}
          {save.isError ? (
            <span role="alert" className="text-sm text-red-600">
              {getErrorMessage(save.error)}
            </span>
          ) : null}
        </div>
      </form>
    </details>
  );
}

export function SupportTab(): JSX.Element {
  return (
    <div className="space-y-6">
      <RequestsSection />
      <ContactsSection />
    </div>
  );
}
