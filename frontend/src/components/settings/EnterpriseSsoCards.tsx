import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
import {
  SECURITY_POLICY_QUERY_KEY,
  useSecurityPolicy,
  useUpdateSecurityPolicy,
} from "@/api/identity";
import {
  Button,
  Input,
  Pill,
  Section,
  Spinner,
  Textarea,
  mutationErrorMessage,
} from "@/components/ui/primitives";

/** P42 enterprise sign-in: verified email domains, SAML single sign-on, and SCIM user sync. */

/**
 * P43: the identity provider's signing certificate has a life, and until now nothing in
 * the console said so - a workspace found out when someone read a security alert.
 *
 * What this renders, and what it refuses to render:
 *  - Nothing at all when the backend sends no expiry. An older API must produce SILENCE
 *    here, never a "certificate valid" line: the absence of a warning is not evidence of
 *    health, and a UI that manufactures reassurance out of missing data is worse than one
 *    that says nothing.
 *  - Nothing beyond 30 days. That horizon is the point of the warning - certificates are
 *    usually annual and a rotation has to be scheduled with whoever runs the IdP, so the
 *    window has to be long enough to plan in, and quiet before that.
 *  - Fact and date only. "Expires on 3 October" and "in 12 days" are the server's fact
 *    restated. There is no computed severity here - no "action required", no "at risk" -
 *    because the backend never asserted one.
 *  - Expired reads differently from expiring, deliberately. Before the date nothing is
 *    wrong yet. After it, sign-ins are still being accepted against a key the identity
 *    provider has retired, which is a real if bounded position, and the two must not look
 *    the same.
 * It offers no "renew" action because there is no endpoint for one: the certificate is
 * replaced by pasting a new one into the field below, which is where this points.
 */
function SsoCertificateStatus({
  expiresAt,
  expired,
}: {
  expiresAt?: string | null;
  expired?: boolean;
}) {
  if (!expiresAt) return null;
  const when = new Date(expiresAt);
  if (Number.isNaN(when.getTime())) return null;

  const days = Math.ceil((when.getTime() - Date.now()) / 86_400_000);
  const isExpired = expired === true || days < 0;
  if (!isExpired && days > 30) return null;

  const date = when.toLocaleDateString(undefined, {
    day: "numeric",
    month: "long",
    year: "numeric",
  });

  return (
    <div
      role="status"
      className={
        isExpired
          ? "rounded-lg border-l-2 border-[hsl(var(--cx-danger))] bg-[hsl(var(--cx-danger)/0.1)] px-3.5 py-3"
          : days <= 7
            ? "rounded-lg border-l-2 border-[hsl(var(--cx-flag))] bg-[hsl(var(--cx-flag)/0.1)] px-3.5 py-3"
            : "rounded-lg border-l-2 border-border bg-muted px-3.5 py-3"
      }
    >
      <p className="text-sm font-medium">
        {isExpired
          ? `Your identity provider's signing certificate expired on ${date}.`
          : `Your identity provider's signing certificate expires on ${date}${
              days >= 0 ? ` - in ${days} ${days === 1 ? "day" : "days"}` : ""
            }.`}
      </p>
      <p className="mt-0.5 text-xs text-muted-foreground">
        {isExpired
          ? "Single sign-on still works, so nobody is locked out. Paste the new certificate from your identity provider below to replace it."
          : "Ask whoever runs your identity provider for the replacement, then paste it below."}
      </p>
    </div>
  );
}

type DomainOut = {
  id: string;
  domain: string;
  verified: boolean;
  verified_at: string | null;
  last_checked_at: string | null;
  txt_name: string;
  txt_value: string;
};

type SamlSetupOut = {
  sp_entity_id: string;
  acs_url: string;
  metadata_url: string;
  start_url: string;
};

type ScimTokenOut = {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
};

const DOMAINS_KEY = ["org", "current", "domains"] as const;
const SCIM_TOKENS_KEY = ["org", "current", "scim-tokens"] as const;

function CopyRow({ label, value }: { label: string; value: string }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <Input
        aria-label={label}
        readOnly
        value={value}
        onFocus={(e) => e.currentTarget.select()}
      />
    </label>
  );
}

export function VerifiedDomainsCard() {
  const { api } = useAuth();
  const gate = useGate();
  const qc = useQueryClient();
  const [domain, setDomain] = React.useState("");
  const canRead = gate.can("settings:read");
  const canWrite = gate.can("settings:write");

  const domains = useQuery({
    queryKey: DOMAINS_KEY,
    queryFn: () => api.request<DomainOut[]>("/api/v1/orgs/current/domains"),
    enabled: canRead,
  });
  const refresh = () => void qc.invalidateQueries({ queryKey: DOMAINS_KEY });
  const add = useMutation({
    mutationFn: () =>
      api.request<DomainOut>("/api/v1/orgs/current/domains", {
        method: "POST",
        json: { domain },
      }),
    onSuccess: () => {
      setDomain("");
      refresh();
    },
  });
  const verify = useMutation({
    mutationFn: (id: string) =>
      api.request<DomainOut>(`/api/v1/orgs/current/domains/${id}/verify`, {
        method: "POST",
      }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: string) =>
      api.request(`/api/v1/orgs/current/domains/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });

  if (!canRead) return null;
  const error = add.error ?? verify.error ?? remove.error;

  return (
    <Section
      className="rounded-lg border border-border p-4"
      title="Verified domains"
      description="Prove your company owns its email domain. Single sign-on and user sync only work for verified domains."
    >
      {domains.isPending ? (
        <Spinner label="Loading domains" />
      ) : (
        <ul className="space-y-3">
          {(domains.data ?? []).map((row) => (
            <li
              key={row.id}
              className="space-y-2.5 rounded-lg border border-border p-3.5"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium">{row.domain}</span>
                {row.verified ? (
                  <Pill tone="success">Verified</Pill>
                ) : (
                  <Pill tone="warning">Not verified</Pill>
                )}
              </div>
              {!row.verified && (
                <>
                  <p className="text-xs text-muted-foreground">
                    Add this TXT record at your DNS provider, then check again.
                    DNS changes can take a few minutes to appear.
                  </p>
                  <CopyRow
                    label={`TXT name for ${row.domain}`}
                    value={row.txt_name}
                  />
                  <CopyRow
                    label={`TXT value for ${row.domain}`}
                    value={row.txt_value}
                  />
                  {row.last_checked_at && (
                    <p className="text-xs text-muted-foreground">
                      Record not found yet (last checked{" "}
                      {new Date(row.last_checked_at).toLocaleTimeString()}).
                    </p>
                  )}
                </>
              )}
              {canWrite && (
                <div className="flex gap-2.5">
                  {!row.verified && (
                    <Button
                      type="button"
                      size="sm"
                      disabled={verify.isPending}
                      onClick={() => verify.mutate(row.id)}
                    >
                      Check DNS
                    </Button>
                  )}
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(row.id)}
                  >
                    Remove
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {canWrite && (
        <div className="flex gap-3">
          <Input
            aria-label="Domain to verify"
            placeholder="acme.com"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
          />
          <Button
            type="button"
            disabled={!domain.trim() || add.isPending}
            onClick={() => add.mutate()}
          >
            Add domain
          </Button>
        </div>
      )}
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(error)}
        </p>
      ) : null}
    </Section>
  );
}

export function SamlSsoCard() {
  const { api } = useAuth();
  const gate = useGate();
  const qc = useQueryClient();
  const canRead = gate.can("settings:read");
  const canWrite = gate.can("settings:write");
  const policy = useSecurityPolicy(api, canRead);
  const update = useUpdateSecurityPolicy(api);
  const setup = useQuery({
    queryKey: ["org", "current", "sso", "saml"],
    queryFn: () => api.request<SamlSetupOut>("/api/v1/orgs/current/sso/saml"),
    enabled: canRead,
    retry: false,
  });

  const sso = policy.data?.sso;
  const [entityId, setEntityId] = React.useState("");
  const [ssoUrl, setSsoUrl] = React.useState("");
  const [cert, setCert] = React.useState("");
  const [domain, setDomain] = React.useState("");
  const [dirty, setDirty] = React.useState(false);

  React.useEffect(() => {
    if (dirty) return;
    setEntityId(sso?.idp_entity_id ?? "");
    setSsoUrl(sso?.idp_sso_url ?? "");
    setDomain(sso?.domain ?? "");
  }, [dirty, sso?.idp_entity_id, sso?.idp_sso_url, sso?.domain]);

  if (!canRead) return null;
  const isSaml = sso?.protocol === "saml";
  const edit =
    (setter: (v: string) => void) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      setter(e.target.value);
      setDirty(true);
    };

  const save = () =>
    update.mutate(
      {
        sso: {
          protocol: "saml",
          idp_entity_id: entityId.trim(),
          idp_sso_url: ssoUrl.trim(),
          domain: domain.trim(),
          ...(cert.trim() ? { idp_x509_cert: cert.trim() } : {}),
        },
      },
      {
        onSuccess: () => {
          setDirty(false);
          setCert("");
          void qc.invalidateQueries({ queryKey: SECURITY_POLICY_QUERY_KEY });
        },
      },
    );

  return (
    <Section
      className="rounded-lg border border-border p-4"
      title="SAML single sign-on"
      description="For Okta, Microsoft Entra ID, Google Workspace, OneLogin and other SAML identity providers."
    >
      {isSaml ? (
        <Pill tone="success">SAML is this workspace&apos;s sign-in method</Pill>
      ) : null}
      {setup.data ? (
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">
            Give these to your identity provider when you create the app.
          </p>
          <CopyRow
            label="Entity ID (audience)"
            value={setup.data.sp_entity_id}
          />
          <CopyRow label="ACS URL (reply URL)" value={setup.data.acs_url} />
          <CopyRow label="Metadata URL" value={setup.data.metadata_url} />
        </div>
      ) : null}
      <fieldset
        disabled={!canWrite || update.isPending}
        className="m-0 min-w-0 space-y-3 border-0 p-0"
      >
        <label className="block space-y-1">
          <span className="text-sm text-muted-foreground">
            Identity provider entity ID
          </span>
          <Input
            aria-label="Identity provider entity ID"
            value={entityId}
            onChange={edit(setEntityId)}
          />
        </label>
        <label className="block space-y-1">
          <span className="text-sm text-muted-foreground">
            Identity provider sign-in URL
          </span>
          <Input
            aria-label="Identity provider sign-in URL"
            type="url"
            placeholder="https://..."
            value={ssoUrl}
            onChange={edit(setSsoUrl)}
          />
        </label>
        <SsoCertificateStatus
          expiresAt={sso?.idp_cert_expires_at}
          expired={sso?.idp_cert_expired}
        />
        <label className="block space-y-1">
          <span className="text-sm text-muted-foreground">
            Signing certificate
          </span>
          <Textarea
            aria-label="Signing certificate"
            rows={4}
            placeholder={
              sso?.idp_cert_set
                ? "Saved. Paste a new one to replace it."
                : "-----BEGIN CERTIFICATE-----"
            }
            value={cert}
            onChange={edit(setCert)}
          />
        </label>
        <label className="block space-y-1">
          <span className="text-sm text-muted-foreground">Email domain</span>
          <Input
            aria-label="SAML email domain"
            placeholder="acme.com"
            value={domain}
            onChange={edit(setDomain)}
          />
        </label>
        <p className="text-xs text-muted-foreground">
          Saving switches this workspace&apos;s single sign-on to SAML. Only
          signed responses are accepted, and the email domain must be verified
          above.
        </p>
        {canWrite && (
          <Button
            type="button"
            size="sm"
            disabled={
              !entityId.trim() ||
              !ssoUrl.trim() ||
              !domain.trim() ||
              (!cert.trim() && !sso?.idp_cert_set)
            }
            onClick={save}
          >
            Save SAML settings
          </Button>
        )}
      </fieldset>
      {update.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(update.error)}
        </p>
      ) : null}
    </Section>
  );
}

export function ScimTokensCard() {
  const { api, me, orgId } = useAuth();
  const gate = useGate();
  const qc = useQueryClient();
  const canRead = gate.can("members:update");
  // Capabilities expand the owner's wildcard into every permission, so "owner only" is read
  // from the membership. The server enforces it either way.
  const canCreate =
    me?.memberships.find((m) => m.org_id === orgId)?.role_name === "owner";
  const [name, setName] = React.useState("");
  const [created, setCreated] = React.useState<{
    token: string;
    base_url: string;
  } | null>(null);

  const tokens = useQuery({
    queryKey: SCIM_TOKENS_KEY,
    queryFn: () =>
      api.request<ScimTokenOut[]>("/api/v1/orgs/current/scim-tokens"),
    enabled: canRead,
  });
  const refresh = () =>
    void qc.invalidateQueries({ queryKey: SCIM_TOKENS_KEY });
  const create = useMutation({
    mutationFn: () =>
      api.request<ScimTokenOut & { token: string; base_url: string }>(
        "/api/v1/orgs/current/scim-tokens",
        { method: "POST", json: { name: name.trim() } },
      ),
    onSuccess: (res) => {
      setCreated({ token: res.token, base_url: res.base_url });
      setName("");
      refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) =>
      api.request(`/api/v1/orgs/current/scim-tokens/${id}`, {
        method: "DELETE",
      }),
    onSuccess: refresh,
  });

  if (!canRead) return null;
  const active = (tokens.data ?? []).filter((t) => !t.revoked_at);

  return (
    <Section
      className="rounded-lg border border-border p-4"
      title="User sync (SCIM)"
      description="Let your identity provider add people and remove leavers automatically. Removing someone there ends their access here at once."
    >
      {created ? (
        <div className="space-y-2.5 rounded-lg border border-border p-3.5">
          <p className="text-sm font-medium">
            Copy this token now. It will not be shown again.
          </p>
          <CopyRow label="SCIM base URL" value={created.base_url} />
          <CopyRow label="SCIM token" value={created.token} />
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setCreated(null)}
          >
            Done
          </Button>
        </div>
      ) : null}
      {tokens.isPending ? (
        <Spinner label="Loading SCIM tokens" />
      ) : active.length === 0 ? (
        <p className="text-sm text-muted-foreground">No SCIM tokens yet.</p>
      ) : (
        <ul className="space-y-2.5">
          {active.map((t) => (
            <li
              key={t.id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border px-3.5 py-2.5 text-sm"
            >
              <span>
                {t.name}{" "}
                <span className="text-muted-foreground">scim_{t.prefix}_…</span>
                <span className="ml-2 text-xs text-muted-foreground">
                  {t.last_used_at
                    ? `last used ${new Date(t.last_used_at).toLocaleString()}`
                    : "never used"}
                </span>
              </span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={revoke.isPending}
                onClick={() => revoke.mutate(t.id)}
              >
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
      {canCreate ? (
        <div className="flex gap-3">
          <Input
            aria-label="SCIM token name"
            placeholder="Okta"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <Button
            type="button"
            disabled={!name.trim() || create.isPending}
            onClick={() => create.mutate()}
          >
            Create token
          </Button>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">
          Only the workspace owner can create SCIM tokens.
        </p>
      )}
      {(create.error ?? revoke.error) ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(create.error ?? revoke.error)}
        </p>
      ) : null}
    </Section>
  );
}
