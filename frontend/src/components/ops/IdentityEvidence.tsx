import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { Button, mutationErrorMessage } from "@/components/ui/primitives";
import { fetchAuthedBlob } from "@/api/client";
import { useEffect, useState } from "react";

function EvidenceValue({ value, label = "", path = "", base }: { value: unknown; label?: string; path?: string; base: string }) {
  if (value == null) return null;
  if (typeof value === "object") return <div className="space-y-3">{Object.entries(value).map(([key, item]) =>
    <div key={key} className="border-l-2 border-blue-100 pl-4"><p className="text-xs font-semibold text-slate-500">{key.replace(/_/g, " ")}</p><EvidenceValue label={key} value={item} path={path ? `${path}.${key}` : key} base={base} /></div>)}</div>;
  const text = String(value);
  if (text.startsWith("https://")) {
    return <div className="space-y-2">
      {/image|selfie|portrait/i.test(label) && <EvidenceMedia url={`${base}-media?path=${encodeURIComponent(path)}`} label={label} />}
      {/video/i.test(label) && <EvidenceMedia url={`${base}-media?path=${encodeURIComponent(path)}`} label={label} video />}
      <a href={text} target="_blank" rel="noreferrer" className="text-sm text-blue-700 underline">Open {label.replace(/_/g, " ") || "evidence"}</a>
    </div>;
  }
  return <p className="whitespace-pre-wrap break-words text-sm">{text}</p>;
}

export function IdentityEvidence({ orgId, person }: { orgId: string; person: { id: string; full_name: string; identity_provider?: string | null; provider_session_id?: string | null } }) {
  const { api } = useAuth();
  const [open, setOpen] = useState(false);
  const evidence = useQuery({
    queryKey: ["ops", "identity-evidence", orgId, person.id],
    queryFn: () => api.request<Record<string, unknown>>(`/api/v1/ops/applications/${orgId}/persons/${person.id}/evidence`),
    enabled: open, gcTime: 0, staleTime: 0, retry: false,
  });
  return <section className="rounded-xl border border-blue-100 bg-white p-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="font-semibold">{person.full_name}</h3><p className="break-all text-xs text-slate-500">{person.provider_session_id ? `Didit session: ${person.provider_session_id}` : "No Didit session available"}</p></div>
    {person.provider_session_id && <Button variant="outline" onClick={() => setOpen(!open)}>{open ? "Hide evidence" : "View ID, photos & results"}</Button>}</div>
    {open && <div className="mt-5 space-y-4">
      {evidence.isPending && <p>Loading evidence securely…</p>}
      {evidence.isError && <p role="alert">{mutationErrorMessage(evidence.error)} <button onClick={() => void evidence.refetch()} className="underline">Retry</button></p>}
      {evidence.data && <EvidenceValue value={evidence.data} base={`/api/v1/ops/applications/${orgId}/persons/${person.id}/evidence`} />}
    </div>}
  </section>;
}

function EvidenceMedia({ url, label, video = false }: { url: string; label: string; video?: boolean }) {
  const { api } = useAuth();
  const [src, setSrc] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let stopped = false; let objectUrl = "";
    fetchAuthedBlob(api, url).then(blob => {
      if (stopped) return;
      objectUrl = URL.createObjectURL(blob); setSrc(objectUrl);
    }).catch(e => { if (!stopped) setError(mutationErrorMessage(e)); });
    return () => { stopped = true; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [api, url]);
  if (error) return <p className="text-sm text-amber-700">Preview unavailable. Use the source link below.</p>;
  if (!src) return <p className="text-sm text-slate-500">Loading secure preview...</p>;
  return video ? <video src={src} controls className="max-h-80 max-w-full rounded-xl" /> : <a href={src} target="_blank" rel="noreferrer"><img src={src} alt={label.replace(/_/g, " ")} className="max-h-80 max-w-full rounded-xl border bg-slate-50 object-contain" /></a>;
}
