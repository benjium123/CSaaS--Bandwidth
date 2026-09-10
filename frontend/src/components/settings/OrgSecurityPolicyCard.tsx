import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
import {
  SECURITY_POLICY_ERROR_CODES,
  securityPolicyErrorCode,
  securityPolicyErrorMessage,
  useSecurityPolicy,
  useUpdateSecurityPolicy,
  type SsoConfigIn,
} from "@/api/identity";
import {
  Button,
  Input,
  MutationStatus,
  Pill,
  Section,
  Spinner,
  Textarea,
  mutationErrorMessage,
} from "@/components/ui/primitives";
/** `relativeTime` only measures the past: a future timestamp collapses to "now", which would
 * render the grace deadline as "(now)". The countdown has to look forward instead. */
function daysLeft(iso: string): string {
  const days = Math.ceil((new Date(iso).getTime() - Date.now()) / 86_400_000);
  if (days <= 0) return "today";
  return days === 1 ? "1 day left" : `${days} days left`;
}

export function OrgSecurityPolicyCard() {
  const { api, me, orgId } = useAuth();
  const gate = useGate();
  const canRead = gate.can("settings:read");
  const canWrite = gate.can("settings:write");
  const policyQuery = useSecurityPolicy(api, canRead);
  const update = useUpdateSecurityPolicy(api);

  const [lastPatchError, setLastPatchError] = React.useState<unknown>(null);
  const [lastSubmittedBlock, setLastSubmittedBlock] = React.useState<
    "2fa" | "ip" | "sso" | null
  >(null);

  const [ipText, setIpText] = React.useState("");
  const [ipDirty, setIpDirty] = React.useState(false);

  const [ssoIssuer, setSsoIssuer] = React.useState("");
  const [ssoClientId, setSsoClientId] = React.useState("");
  const [ssoClientSecret, setSsoClientSecret] = React.useState("");
  const [ssoDomain, setSsoDomain] = React.useState("");
  const [ssoEnforce, setSsoEnforce] = React.useState(false);
  const [ssoDirty, setSsoDirty] = React.useState(false);

  const policy = policyQuery.data;

  const ipSeed = React.useMemo(
    () => policy?.ip_allowlist?.join("\n") ?? "",
    [policy?.ip_allowlist],
  );
  React.useEffect(() => {
    if (!ipDirty) setIpText(ipSeed);
  }, [ipSeed, ipDirty]);

  const ssoSeed = React.useMemo(
    () => ({
      issuer: policy?.sso?.issuer ?? "",
      client_id: policy?.sso?.client_id ?? "",
      domain: policy?.sso?.domain ?? "",
      enforce: policy?.sso?.enforce ?? false,
    }),
    [
      policy?.sso?.issuer,
      policy?.sso?.client_id,
      policy?.sso?.domain,
      policy?.sso?.enforce,
    ],
  );
  React.useEffect(() => {
    if (!ssoDirty) {
      setSsoIssuer(ssoSeed.issuer);
      setSsoClientId(ssoSeed.client_id);
      setSsoClientSecret("");
      setSsoDomain(ssoSeed.domain);
      setSsoEnforce(ssoSeed.enforce);
    }
  }, [ssoSeed, ssoDirty]);

  if (!canRead) return null;

  if (policyQuery.isPending) {
    return (
      <Section
        title="Workspace security"
        description="Sign-in rules for everyone in this workspace."
      >
        <Spinner label="Loading security policy" />
      </Section>
    );
  }

  if (policyQuery.isError) {
    return (
      <Section
        title="Workspace security"
        description="Sign-in rules for everyone in this workspace."
      >
        <div className="space-y-2">
          <p role="alert" className="text-sm text-destructive">
            {mutationErrorMessage(policyQuery.error)}
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              void policyQuery.refetch();
            }}
          >
            Retry
          </Button>
        </div>
      </Section>
    );
  }

  if (!policy) return null;

  const errorCode = securityPolicyErrorCode(lastPatchError);
  const specific2faError =
    lastSubmittedBlock === "2fa" &&
    errorCode === SECURITY_POLICY_ERROR_CODES.TWO_FACTOR_REQUIRED_FOR_ACTOR;
  const specificIpError =
    lastSubmittedBlock === "ip" &&
    errorCode === SECURITY_POLICY_ERROR_CODES.IP_ALLOWLIST_WOULD_LOCK_YOU_OUT;
  const genericError =
    lastPatchError != null && !specific2faError && !specificIpError;

  const grace = policy.require_2fa_grace_until;
  const graceFuture = grace != null && new Date(grace).getTime() > Date.now();

  const hasSavedSecret = Boolean(policy.sso?.client_secret_set);
  const ssoConfigComplete =
    Boolean(ssoIssuer.trim() && ssoClientId.trim() && ssoDomain.trim()) &&
    Boolean(ssoClientSecret.trim() || hasSavedSecret);

  const orgSlug = me?.memberships.find((m) => m.org_id === orgId)?.org_slug;

  const onRequire2faChange = (next: boolean) => {
    setLastPatchError(null);
    setLastSubmittedBlock("2fa");
    update.mutate(
      { require_2fa: next },
      { onError: (err) => setLastPatchError(err) },
    );
  };

  const onSaveIp = () => {
    setLastPatchError(null);
    setLastSubmittedBlock("ip");
    const lines = ipText
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
    update.mutate(
      { ip_allowlist: lines.length ? lines : null },
      {
        onSuccess: () => setIpDirty(false),
        onError: (err) => setLastPatchError(err),
      },
    );
  };

  const onSaveSso = () => {
    setLastPatchError(null);
    setLastSubmittedBlock("sso");
    const payload: SsoConfigIn = {};
    if (ssoIssuer.trim()) payload.issuer = ssoIssuer.trim();
    if (ssoClientId.trim()) payload.client_id = ssoClientId.trim();
    if (ssoClientSecret.trim()) payload.client_secret = ssoClientSecret.trim();
    if (ssoDomain.trim()) payload.domain = ssoDomain.trim();
    payload.enforce = ssoEnforce;
    update.mutate(
      { sso: payload },
      {
        onSuccess: () => setSsoDirty(false),
        onError: (err) => setLastPatchError(err),
      },
    );
  };

  return (
    <Section
      title="Workspace security"
      description="Sign-in rules for everyone in this workspace."
    >
      {genericError ? (
        <p role="alert" className="text-sm text-destructive">
          {securityPolicyErrorMessage(lastPatchError)}
        </p>
      ) : null}

      {!canWrite ? (
        <p className="text-xs text-muted-foreground">
          You can view this, but only an admin can make changes here.
        </p>
      ) : null}

      <MutationStatus
        pending={update.isPending}
        error={undefined}
        success={update.isSuccess ? "Saved." : undefined}
      />

      <fieldset disabled={!canWrite} className="m-0 min-w-0 border-0 p-0">
        <div className="space-y-4">
          <div className="space-y-2">
            <label htmlFor="require-2fa" className="flex items-center gap-2 text-sm">
              <input
                id="require-2fa"
                type="checkbox"
                checked={policy.require_2fa}
                onChange={(event) => onRequire2faChange(event.target.checked)}
                disabled={update.isPending}
              />
              <span className="font-medium">
                Require two-factor authentication
              </span>
            </label>

            {specific2faError ? (
              <p role="alert" className="text-sm text-destructive">
                {securityPolicyErrorMessage(lastPatchError)}
              </p>
            ) : null}

            <p className="text-xs text-muted-foreground">
              Everyone in this workspace must set up an authenticator app to keep
              signing in.
            </p>

            {policy.require_2fa && grace != null ? (
              graceFuture ? (
                <p className="text-xs text-muted-foreground">
                  People who haven&apos;t set it up yet have until{" "}
                  {new Date(grace).toLocaleDateString()} ({daysLeft(grace)}).
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  The grace period has ended — anyone without 2FA is locked out of
                  this workspace until they set it up.
                </p>
              )
            ) : null}
          </div>

          <div className="space-y-2">
            <div className="text-sm font-medium">
              Allowed IP ranges{" "}
              {policy.ip_allowlist && policy.ip_allowlist.length > 0 ? (
                <Pill tone="warning">Restricting access</Pill>
              ) : null}
            </div>

            <Textarea
              aria-label="Allowed IP ranges"
              value={ipText}
              onChange={(event) => {
                setIpText(event.target.value);
                setIpDirty(true);
              }}
              disabled={update.isPending}
              rows={5}
            />

            {specificIpError ? (
              <p role="alert" className="text-sm text-destructive">
                {securityPolicyErrorMessage(lastPatchError)}
              </p>
            ) : null}

            <p className="text-xs text-muted-foreground">
              Sign-ins and API keys are both restricted to these ranges. Leave
              this empty to allow every address.
            </p>
            <p className="text-xs text-muted-foreground">
              One CIDR range per line, for example 203.0.113.0/24 or
              198.51.100.7/32.
            </p>

            <Button
              type="button"
              size="sm"
              onClick={onSaveIp}
              disabled={update.isPending}
            >
              Save IP ranges
            </Button>
          </div>

          <div className="space-y-3">
            <div className="text-sm font-medium">Single sign-on</div>

            <label className="block space-y-1">
              <span className="text-sm text-muted-foreground">Issuer URL</span>
              <Input
                aria-label="Issuer URL"
                type="url"
                value={ssoIssuer}
                onChange={(event) => {
                  setSsoIssuer(event.target.value);
                  setSsoDirty(true);
                }}
                disabled={update.isPending}
                placeholder="https://..."
              />
            </label>

            <label className="block space-y-1">
              <span className="text-sm text-muted-foreground">Client ID</span>
              <Input
                aria-label="Client ID"
                value={ssoClientId}
                onChange={(event) => {
                  setSsoClientId(event.target.value);
                  setSsoDirty(true);
                }}
                disabled={update.isPending}
              />
            </label>

            <label className="block space-y-1">
              <span className="text-sm text-muted-foreground">
                Client secret
              </span>
              <Input
                aria-label="Client secret"
                type="password"
                value={ssoClientSecret}
                onChange={(event) => {
                  setSsoClientSecret(event.target.value);
                  setSsoDirty(true);
                }}
                disabled={update.isPending}
                placeholder={
                  policy.sso?.client_secret_set
                    ? "Saved — leave blank to keep it"
                    : undefined
                }
              />
              {policy.sso?.client_secret_set ? (
                <span className="text-xs text-muted-foreground">
                  Leave blank to keep the secret you already saved.
                </span>
              ) : null}
            </label>

            <label className="block space-y-1">
              <span className="text-sm text-muted-foreground">
                Email domain
              </span>
              <Input
                aria-label="Email domain"
                type="text"
                value={ssoDomain}
                onChange={(event) => {
                  setSsoDomain(event.target.value);
                  setSsoDirty(true);
                }}
                disabled={update.isPending}
                placeholder="acme.com"
              />
            </label>

            <label className="flex items-center gap-2 text-sm">
              <input
                aria-label="Enforce single sign-on"
                type="checkbox"
                checked={ssoEnforce}
                onChange={(event) => {
                  setSsoEnforce(event.target.checked);
                  setSsoDirty(true);
                }}
                disabled={update.isPending}
              />
              <span className="font-medium">Enforce single sign-on</span>
            </label>

            <p className="text-xs text-muted-foreground">
              Members of this email domain can only sign in through your identity
              provider. Workspace owners can still sign in with a password so you
              cannot lock yourself out.
            </p>

            {ssoEnforce && !ssoConfigComplete ? (
              <p className="text-xs text-muted-foreground">
                Add the issuer, client ID, client secret and email domain before
                enforcing.
              </p>
            ) : null}

            <Button
              type="button"
              size="sm"
              onClick={onSaveSso}
              disabled={update.isPending}
            >
              Save single sign-on
            </Button>

            {policy.sso?.issuer && policy.sso.client_secret_set && orgSlug ? (
              <div>
                <a
                  className="text-sm underline"
                  href={`/api/v1/auth/sso/${orgSlug}/start`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Test sign-in
                </a>
              </div>
            ) : null}
          </div>
        </div>
      </fieldset>
    </Section>
  );
}
