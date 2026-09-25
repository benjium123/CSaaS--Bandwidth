import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { fetchAuthedBlob } from "@/api/client";
import { mutationErrorMessage } from "@/components/ui/primitives";

/** What the server distils from a Didit decision (services/didit_evidence.py). The full
 *  decision never reaches the browser; photos are streamed by their path in it. */
export type EvidenceSummary = {
  status: string | null;
  verdict: "ok" | "review" | "fail" | "none";
  session_number: number | null;
  created_at: string | null;
  identity: {
    full_name: string | null;
    date_of_birth: string | null;
    age: number | null;
    nationality: string | null;
    document_type: string | null;
    document_number: string | null;
    issuing_country: string | null;
    expiration_date: string | null;
    expired: boolean | null;
  };
  checks: { key: string; label: string; status: string | null; verdict: string; score: number | null; detail: string | null }[];
  warnings: { message: string; risk: string }[];
  aml_matches: { name: string; lists: string[]; score: number | null }[];
  photos: { label: string; path: string }[];
  device: string | null;
};

type Person = {
  id: string;
  full_name: string;
  identity_provider?: string | null;
  provider_session_id?: string | null;
};

const VERDICT_TEXT: Record<string, string> = {
  ok: "Didit approved",
  review: "Didit wants a human look",
  fail: "Didit declined",
  none: "Not finished",
};

export function IdentityEvidence({ orgId, person }: { orgId: string; person: Person }) {
  const { api } = useAuth();
  const base = `/api/v1/ops/applications/${orgId}/persons/${person.id}/evidence`;
  const evidence = useQuery({
    queryKey: ["ops", "identity-evidence", orgId, person.id],
    queryFn: () => api.request<EvidenceSummary>(base),
    enabled: Boolean(person.provider_session_id),
    gcTime: 0,
    staleTime: 0,
    retry: false,
  });

  if (!person.provider_session_id) {
    return (
      <p className="sb-foot">
        {person.full_name} has not completed an identity check yet.
      </p>
    );
  }
  if (evidence.isPending) return <p className="sb-foot">Loading the identity check…</p>;
  if (evidence.isError) {
    return (
      <p role="alert" className="sb-error">
        {mutationErrorMessage(evidence.error)}{" "}
        <button type="button" className="underline" onClick={() => void evidence.refetch()}>
          Retry
        </button>
      </p>
    );
  }
  const s = evidence.data;
  const id = s.identity;
  const facts: [string, string | null, boolean?][] = [
    ["Name on ID", id.full_name],
    ["Date of birth", id.date_of_birth ? `${id.date_of_birth}${id.age != null ? ` · ${id.age}` : ""}` : null],
    ["Document", [id.document_type, id.document_number].filter(Boolean).join(" ") || null],
    ["Issued by", id.issuing_country],
    ["Expires", id.expiration_date ? `${id.expiration_date}${id.expired ? " · expired" : ""}` : null, id.expired === true],
    ["Nationality", id.nationality],
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="sb-tag" data-tone={s.verdict === "ok" ? "go" : s.verdict === "fail" ? "stop" : "amber"}>
          {VERDICT_TEXT[s.verdict] ?? s.status}
        </span>
        {s.session_number != null && <span className="sb-tag" data-plain="true">Session #{s.session_number}</span>}
        {s.device && <span className="sb-tag" data-plain="true">{s.device}</span>}
      </div>

      {s.photos.length > 0 && (
        <div className="sb-photos">
          {s.photos.map((p) => (
            <EvidencePhoto key={p.path} url={`${base}-media?path=${encodeURIComponent(p.path)}`} label={p.label} />
          ))}
        </div>
      )}

      <dl className="sb-facts">
        {facts
          .filter(([, v]) => v)
          .map(([k, v, bad]) => (
            <div key={k} className="sb-fact">
              <dt>{k}</dt>
              <dd data-tone={bad ? "stop" : undefined}>{v}</dd>
            </div>
          ))}
      </dl>

      {s.checks.length > 0 && (
        <div className="sb-checks" aria-label="Identity checks">
          {s.checks.map((c) => (
            <div key={c.key} className="sb-check">
              <span className="sb-lamp" data-v={c.verdict} aria-hidden="true" />
              <div>
                <div className="sb-check-label">{c.label}</div>
                {c.detail && <div className="sb-check-detail">{c.detail}</div>}
              </div>
              <div className="sb-reading">
                {c.score != null ? `${c.score}` : c.status ?? "—"}
              </div>
            </div>
          ))}
        </div>
      )}

      {s.aml_matches.length > 0 && (
        <div className="flex flex-col gap-2">
          {s.aml_matches.map((m) => (
            <div key={m.name} className="sb-warn" data-risk="high">
              <span>Match</span>
              <span>
                {m.name}
                {m.lists.length > 0 ? ` — ${m.lists.join(", ")}` : ""}
                {m.score != null ? ` (${Math.round(m.score * 100)}%)` : ""}
              </span>
            </div>
          ))}
        </div>
      )}

      {s.warnings.length > 0 && (
        <div className="flex flex-col gap-2">
          {s.warnings.map((w) => (
            <div key={w.message} className="sb-warn" data-risk={w.risk.toLowerCase()}>
              <span>{w.risk}</span>
              <span>{w.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function EvidencePhoto({ url, label }: { url: string; label: string }) {
  const { api } = useAuth();
  const [src, setSrc] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let stopped = false;
    let objectUrl = "";
    fetchAuthedBlob(api, url)
      .then((blob) => {
        if (stopped) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch(() => {
        if (!stopped) setFailed(true);
      });
    return () => {
      stopped = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [api, url]);
  return (
    <div className="sb-photo">
      <figure>
        <div className="sb-photo-frame">
          {src ? (
            <a href={src} target="_blank" rel="noreferrer">
              <img src={src} alt={label} />
            </a>
          ) : failed ? (
            "Unavailable"
          ) : (
            "Loading…"
          )}
        </div>
        <figcaption>{label}</figcaption>
      </figure>
    </div>
  );
}
