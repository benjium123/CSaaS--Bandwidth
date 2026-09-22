import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input, Textarea, Spinner, Pill, mutationErrorMessage } from "@/components/ui/primitives";

type Account = { id: string; email: string; full_name: string; created_at: string; is_active: boolean; email_verified: boolean; is_operator: boolean; workspaces: { id: string; name: string; account_type: string; role: string; status: string }[] };
type Detail = { id: string; email: string; identifiers: { key: string; kind: string; label: string }[]; delete_workspaces: { id: string; name: string }[]; blockers: string[] };

function AccountActions({ id, close }: { id: string; close: () => void }) {
  const { api } = useAuth();
  const cache = useQueryClient();
  const [selected, setSelected] = React.useState<string[]>([]);
  const [reason, setReason] = React.useState("");
  const [confirmation, setConfirmation] = React.useState("");
  const [deleting, setDeleting] = React.useState(false);
  const detail = useQuery({ queryKey: ["customer-account", id], queryFn: () => api.request<Detail>(`/api/v1/ops/customer-accounts/${id}`), retry: false });
  const action = useMutation({ mutationFn: (kind: "blacklist" | "delete") => api.request(`/api/v1/ops/customer-accounts/${id}/${kind}`, { method: "POST", json: { reason, identifiers: selected, confirmation } }), onSuccess: async () => { await cache.invalidateQueries({ queryKey: ["customer-accounts"] }); await cache.invalidateQueries({ queryKey: ["ops"] }); close(); } });
  if (detail.isPending) return <Spinner label="Loading account controls" />;
  if (detail.isError) return <div><Button onClick={close} variant="outline">Back to all accounts</Button><p role="alert">{mutationErrorMessage(detail.error)}</p></div>;
  const data = detail.data;
  return <section className="space-y-6 rounded-2xl border border-blue-100 bg-white p-6 shadow-sm">
    <Button variant="outline" onClick={close}>Back to all accounts</Button>
    <div><h2 className="text-xl font-semibold">Manage account</h2><p className="mt-1 break-all text-slate-600">{data.email}</p></div>
    <div className="rounded-xl border border-blue-100 bg-blue-50 p-5 space-y-3">
      <h3 className="font-semibold text-blue-900">Blacklist identifiers</h3>
      <p className="text-sm text-slate-600">Choose what to block from future signups and verification. Blacklisting also disables this login and revokes its sessions.</p>
      {data.identifiers.map(item => <label key={item.key} className="flex items-start gap-3 rounded-lg bg-white p-3 text-sm"><input type="checkbox" checked={selected.includes(item.key)} onChange={e => setSelected(old => e.target.checked ? [...old, item.key] : old.filter(k => k !== item.key))} /><span className="break-all">{item.label}<span className="block text-xs text-slate-500">{item.kind === "person" ? "Didit identity match (verified name and birth date)" : item.kind}</span></span></label>)}
      {!data.identifiers.some(i => i.kind === "person") && <p className="text-sm text-slate-500">No completed Didit identity is available to blacklist yet.</p>}
    </div>
    <label className="block space-y-2"><span className="font-medium">Reason</span><Textarea aria-label="Account action reason" value={reason} onChange={e => setReason(e.target.value)} maxLength={500} placeholder="Explain why you are taking this action" /></label>
    <Button disabled={!selected.length || !reason.trim() || action.isPending} onClick={() => action.mutate("blacklist")}>Blacklist selected identifiers</Button>
    <div className="space-y-3 border-t border-red-100 pt-6">
      <h3 className="font-semibold text-red-700">Delete account permanently</h3>
      <p className="text-sm text-slate-600">Removes this login, its sessions, and the workspaces listed below with their Ringlite data. Other people's accounts are preserved. Selected blacklist hashes and an operator audit record remain. Provider-held records and backups follow their retention policies.</p>
      <ul className="list-disc pl-5 text-sm">{data.delete_workspaces.map(org => <li key={org.id}>{org.name}</li>)}</ul>
      {data.blockers.length > 0 ? <div role="status" className="rounded-xl bg-amber-50 p-4 text-sm text-amber-900"><p className="font-semibold">Before deletion</p><ul className="list-disc pl-5">{data.blockers.map(b => <li key={b}>{b}</li>)}</ul></div> : !deleting ? <Button variant="outline" onClick={() => setDeleting(true)}>Review permanent deletion</Button> : <div className="space-y-3 rounded-xl border border-red-200 bg-red-50 p-4"><label className="block space-y-2"><span>Type {data.email} to confirm</span><Input aria-label="Confirm account email" value={confirmation} onChange={e => setConfirmation(e.target.value)} autoComplete="off" /></label><Button disabled={confirmation !== data.email || !reason.trim() || action.isPending} onClick={() => action.mutate("delete")}>Delete account permanently</Button><Button variant="outline" onClick={() => setDeleting(false)}>Cancel</Button></div>}
    </div>
    {action.isError && <p role="alert" className="text-red-700">{mutationErrorMessage(action.error)}</p>}
  </section>;
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
  return <section className="space-y-5">
    <div className="rounded-2xl border border-blue-100 bg-white p-6 shadow-sm"><h2 className="text-xl font-semibold">All accounts</h2><p className="mt-1 text-sm text-slate-500">Every signup, including unconfirmed emails and incomplete, approved or rejected applications.</p><form className="mt-4 flex gap-3" onSubmit={e => { e.preventDefault(); setSearch(text.trim()); setOffset(0); }}><Input aria-label="Search accounts" placeholder="Search name or email" value={text} onChange={e => setText(e.target.value)} /><Button type="submit">Search</Button></form></div>
    {query.isPending ? <Spinner label="Loading accounts" /> : query.isError ? <p role="alert">{mutationErrorMessage(query.error)}</p> : <>
      <div className="overflow-x-auto rounded-2xl border border-blue-100 bg-white shadow-sm"><table><thead><tr><th>Account</th><th>Signed up</th><th>Verification</th><th>Workspaces / applications</th><th>Actions</th></tr></thead><tbody>{query.data.accounts.map(account => <tr key={account.id}><td><div className="font-semibold">{account.full_name || "Unnamed account"}</div><div className="text-sm text-slate-500">{account.email}</div><Pill tone={account.is_active ? "info" : "danger"}>{account.is_active ? "Active login" : "Disabled"}</Pill>{account.is_operator && <Pill tone="neutral">Administrator</Pill>}</td><td className="text-sm">{new Date(account.created_at).toLocaleDateString()}</td><td className="text-sm">{account.email_verified ? "Email confirmed" : "Email not confirmed"}</td><td>{account.workspaces.length ? account.workspaces.map(org => <div key={org.id} className="mb-2 text-sm"><Link className="font-semibold text-blue-700 underline" to={`?section=queue&application=${org.id}`}>{org.name}</Link><div className="text-slate-500">{org.account_type} · {org.status.replace(/_/g, " ")} · {org.role}</div></div>) : <span className="text-sm text-slate-500">No workspace</span>}</td><td>{!account.is_operator && me?.operator_role === "admin" && <Button variant="outline" onClick={() => setParams({ section: "customers", account: account.id })}>Manage account</Button>}</td></tr>)}</tbody></table>{query.data.accounts.length === 0 && <p className="p-6 text-slate-500">No accounts match your search.</p>}</div>
      <div className="flex items-center justify-between"><p className="text-sm text-slate-500">{query.data.total} accounts</p><div className="flex gap-2"><Button variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</Button><Button variant="outline" disabled={offset + 50 >= query.data.total} onClick={() => setOffset(offset + 50)}>Next</Button></div></div>
    </>}
  </section>;
}
