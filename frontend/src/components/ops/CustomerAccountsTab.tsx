import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Spinner, mutationErrorMessage } from "@/components/ui/primitives";

type Account = { id: string; email: string; full_name: string; created_at: string; is_active: boolean; email_verified: boolean; is_operator: boolean; workspaces: { id: string; name: string; account_type: string; role: string; status: string }[] };
type Detail = { id: string; email: string; identifiers: { key: string; kind: string; label: string }[]; delete_workspaces: { id: string; name: string }[]; blockers: string[] };

/** One account: delete it (one confirmation), or block it from coming back. */
function AccountActions({ id, close }: { id: string; close: () => void }) {
  const { api } = useAuth();
  const cache = useQueryClient();
  const [confirming, setConfirming] = React.useState(false);
  const [selected, setSelected] = React.useState<string[]>([]);
  const [reason, setReason] = React.useState("");
  const detail = useQuery({ queryKey: ["customer-account", id], queryFn: () => api.request<Detail>(`/api/v1/ops/customer-accounts/${id}`), retry: false });
  const action = useMutation({
    mutationFn: ({ kind, json }: { kind: "blacklist" | "delete"; json: unknown }) =>
      api.request(`/api/v1/ops/customer-accounts/${id}/${kind}`, { method: "POST", json }),
    onSuccess: async () => {
      await cache.invalidateQueries({ queryKey: ["customer-accounts"] });
      await cache.invalidateQueries({ queryKey: ["ops"] });
      close();
    },
  });
  const back = <button type="button" className="sb-back" onClick={close}>← Back to all accounts</button>;
  if (detail.isPending) return <Spinner label="Loading account" />;
  if (detail.isError) return <div>{back}<p role="alert" className="sb-error">{mutationErrorMessage(detail.error)}</p></div>;
  const data = detail.data;
  const remove = () =>
    action.mutate({
      kind: "delete",
      // The server keeps these for the audit log; the operator should not have to type them.
      json: { reason: reason.trim() || "Deleted by an operator", identifiers: selected, confirmation: data.email },
    });

  return (
    <div className="flex flex-col gap-5">
      {back}
      <header>
        <div className="sb-eyebrow">Account</div>
        <h2 className="sb-title" style={{ fontSize: "clamp(26px, 5vw, 38px)" }}>{data.email}</h2>
      </header>

      <section className="sb-panel sb-panel-pad">
        <h2>Delete account</h2>
        <p className="sb-lede" style={{ marginTop: 0 }}>
          Removes this login and {data.delete_workspaces.length === 1 ? "its workspace" : "its workspaces"}
          {data.delete_workspaces.length > 0 ? ` (${data.delete_workspaces.map((o) => o.name).join(", ")})` : ""}.
          This cannot be undone.
        </p>
        {data.blockers.length > 0 ? (
          <ul role="status" className="sb-blockers">
            {data.blockers.map((b) => <li key={b}>{b}</li>)}
          </ul>
        ) : !confirming ? (
          <div className="sb-actions" style={{ maxWidth: 260 }}>
            <button type="button" className="sb-btn" data-kind="stop" onClick={() => setConfirming(true)}>
              Delete account
            </button>
          </div>
        ) : (
          <div className="sb-actions" style={{ maxWidth: 420 }}>
            <p className="sb-foot" style={{ marginTop: 0 }}>Delete {data.email} for good?</p>
            <div className="flex flex-wrap gap-2">
              <button type="button" className="sb-btn" data-kind="stop" style={{ background: "var(--sb-stop)", color: "#1d0703" }} disabled={action.isPending} onClick={remove}>
                {action.isPending ? "Deleting…" : "Yes, delete it"}
              </button>
              <button type="button" className="sb-btn" data-kind="quiet" onClick={() => setConfirming(false)}>Cancel</button>
            </div>
          </div>
        )}
      </section>

      <details className="sb-more">
        <summary>Block them from signing up again</summary>
        <div className="sb-more-body">
          <p className="sb-foot" style={{ marginTop: 0 }}>
            Blocking also signs them out and disables this login. Tick what to block; it also applies if you delete the account afterwards.
          </p>
          {data.identifiers.map((item) => (
            <label key={item.key} className="sb-check-inline" style={{ marginTop: 0 }}>
              <input type="checkbox" checked={selected.includes(item.key)} onChange={(e) => setSelected((old) => (e.target.checked ? [...old, item.key] : old.filter((k) => k !== item.key)))} />
              <span>
                {item.label}
                <span className="sb-foot" style={{ display: "block", margin: 0 }}>
                  {item.kind === "person" ? "Their verified identity (name and date of birth)" : item.kind}
                </span>
              </span>
            </label>
          ))}
          {!data.identifiers.some((i) => i.kind === "person") && <p className="sb-foot" style={{ margin: 0 }}>They have not finished an identity check, so only the email can be blocked.</p>}
          <textarea className="sb-textarea" aria-label="Account action reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} placeholder="Why (optional, kept in the audit log)" style={{ minHeight: 64 }} />
          <div>
            <button type="button" className="sb-btn" data-kind="amber" disabled={!selected.length || action.isPending} onClick={() => action.mutate({ kind: "blacklist", json: { reason: reason.trim() || "Blocked by an operator", identifiers: selected, confirmation: "" } })}>
              Block selected
            </button>
          </div>
        </div>
      </details>
      {action.isError && <p role="alert" className="sb-error">{mutationErrorMessage(action.error)}</p>}
    </div>
  );
}

export function CustomerAccountsTab() {
  const { api, me } = useAuth();
  const [params, setParams] = useSearchParams();
  const selected = params.get("account");
  const [text, setText] = React.useState("");
  const [search, setSearch] = React.useState("");
  const [offset, setOffset] = React.useState(0);
  const query = useQuery({ queryKey: ["customer-accounts", search, offset], queryFn: () => api.request<{ accounts: Account[]; total: number }>(`/api/v1/ops/customer-accounts?q=${encodeURIComponent(search)}&offset=${offset}&limit=50`) });
  if (selected) return <AccountActions key={selected} id={selected} close={() => setParams({ section: "customers" })} />;
  return (
    <div className="flex flex-col gap-4">
      <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); setSearch(text.trim()); setOffset(0); }}>
        <input className="sb-textarea" style={{ minHeight: 0, height: 44, marginTop: 0, flex: "1 1 240px" }} aria-label="Search accounts" placeholder="Search name or email" value={text} onChange={(e) => setText(e.target.value)} />
        <button type="submit" className="sb-btn" data-kind="amber">Search</button>
      </form>
      {query.isPending ? <Spinner label="Loading accounts" /> : query.isError ? <p role="alert" className="sb-error">{mutationErrorMessage(query.error)}</p> : (
        <>
          {query.data.accounts.length === 0 ? (
            <div className="sb-empty"><p>No accounts match your search.</p></div>
          ) : (
            <ul className="sb-tickets" style={{ marginTop: 0 }}>
              {query.data.accounts.map((account) => (
                <li key={account.id}>
                  <div className="sb-ticket" data-risk={account.is_active ? "low" : "high"} style={{ cursor: "default" }}>
                    <div className="min-w-0">
                      <div className="sb-ticket-name">{account.full_name || "Unnamed account"}</div>
                      <div className="sb-ticket-sub" style={{ overflowWrap: "anywhere" }}>{account.email} · joined {new Date(account.created_at).toLocaleDateString()}</div>
                    </div>
                    <div className="sb-ticket-signals">
                      <span className="sb-tag" data-tone={account.is_active ? "go" : "stop"}>{account.is_active ? "Active" : "Disabled"}</span>
                      <span className="sb-tag" data-tone={account.email_verified ? undefined : "amber"}>{account.email_verified ? "Email confirmed" : "Email not confirmed"}</span>
                      {account.is_operator && <span className="sb-tag" data-tone="sky">Administrator</span>}
                      {account.workspaces.map((org) => (
                        <Link key={org.id} className="sb-tag" data-plain="true" to={`?section=queue&application=${org.id}`}>
                          {org.name} · {org.account_type} · {org.status.replace(/_/g, " ")}
                        </Link>
                      ))}
                      {account.workspaces.length === 0 && <span className="sb-tag" data-plain="true">No workspace</span>}
                    </div>
                    <div>
                      {!account.is_operator && me?.operator_role === "admin" && (
                        <button type="button" className="sb-btn" data-kind="quiet" onClick={() => setParams({ section: "customers", account: account.id })}>
                          Manage account
                        </button>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
          <div className="flex items-center justify-between">
            <p className="sb-foot" style={{ margin: 0 }}>{query.data.total} accounts</p>
            <div className="flex gap-2">
              <button type="button" className="sb-btn" data-kind="quiet" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button>
              <button type="button" className="sb-btn" data-kind="quiet" disabled={offset + 50 >= query.data.total} onClick={() => setOffset(offset + 50)}>Next</button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
