import * as React from "react";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import {
  getErrorMessage,
  useCurrentOrg,
  useUpdateContactVisibility,
  type ContactVisibility,
} from "@/api/contacts";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { SessionsCard } from "@/components/settings/SessionsCard";
import { LoginHistoryCard } from "@/components/settings/LoginHistoryCard";
import { OrgSecurityPolicyCard } from "@/components/settings/OrgSecurityPolicyCard";

const VISIBILITY_OPTIONS: {
  value: ContactVisibility;
  label: string;
  description: string;
  id: string;
}[] = [
  {
    value: "everyone",
    label: "Everyone",
    description: "Every member of the workspace can see every contact.",
    id: "contact-visibility-everyone",
  },
  {
    value: "department",
    label: "Their team",
    description:
      "People see contacts owned by them or by anyone on their team. Admins and connected integrations always see everything.",
    id: "contact-visibility-department",
  },
  {
    value: "owner",
    label: "Only the owner",
    description:
      "People see only the contacts they own. Team leads also see their team's. Admins and connected integrations always see everything.",
    id: "contact-visibility-owner",
  },
];

export function SettingsSecurityPage() {
  const { api, me, orgId } = useAuth();

  const [enroll, setEnroll] = React.useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = React.useState("");
  const [message, setMessage] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [enrolling, setEnrolling] = React.useState(false);
  const [activating, setActivating] = React.useState(false);
  const [copied, setCopied] = React.useState(false);

  const [disableCode, setDisableCode] = React.useState("");
  const [disablePassword, setDisablePassword] = React.useState("");
  const [disableError, setDisableError] = React.useState<string | null>(null);
  const [disabling, setDisabling] = React.useState(false);

  // Item 8: the Disable-2FA panel only makes sense when 2FA is actually on - gated on
  // /auth/me's `totp_enabled` (undefined, i.e. backend hasn't shipped it yet for this
  // user, is treated as false/not-enabled rather than showing the panel regardless).
  const totpEnabled = Boolean(me?.totp_enabled);

  const contactVisQuery = useCurrentOrg(api);
  const updateContactVisibility = useUpdateContactVisibility(api);
  const canUpdateVisibility = hasPermission(me, orgId, "settings:write");

  async function startEnroll() {
    setError(null);
    setEnrolling(true);
    try {
      const res = await api.request<{ secret: string; provisioning_uri: string }>(
        "/api/v1/auth/2fa/enroll",
        { method: "POST" },
      );
      setEnroll({ secret: res.secret, uri: res.provisioning_uri });
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setEnrolling(false);
    }
  }

  async function activate() {
    setError(null);
    setActivating(true);
    try {
      await api.request("/api/v1/auth/2fa/activate", { method: "POST", json: { code } });
      setMessage("Two-factor authentication is on.");
      setEnroll(null);
      setCode("");
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setActivating(false);
    }
  }

  async function copyUri() {
    if (!enroll) return;
    try {
      await navigator.clipboard.writeText(enroll.uri);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard unavailable - the link and secret are still visible to copy by hand */
    }
  }

  async function disable() {
    setDisableError(null);
    setDisabling(true);
    try {
      // Item 8: the password field is always visible while this panel shows (2FA is on)
      // - only send `password` when the user actually filled it in, since the backend
      // treats it as optional depending on the account.
      const json: { code: string; password?: string } = { code: disableCode };
      if (disablePassword) json.password = disablePassword;
      await api.request("/api/v1/auth/2fa/disable", { method: "POST", json });
      setMessage("Two-factor authentication is off.");
      setDisableCode("");
      setDisablePassword("");
    } catch (err) {
      setDisableError(getErrorMessage(err));
    } finally {
      setDisabling(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <h1 className="text-lg font-semibold">Security</h1>

      {message && <p className="text-sm">{message}</p>}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {!enroll ? (
        <Button onClick={startEnroll} disabled={enrolling}>
          Set up two-factor authentication
        </Button>
      ) : (
        <div className="space-y-3 rounded-md border border-border p-4">
          <p className="text-sm">
            Add this secret to your authenticator app, then enter the six-digit code.
          </p>
          <code className="block break-all rounded bg-muted p-2 text-xs">{enroll.secret}</code>
          <p className="text-sm">
            Or open it directly:{" "}
            <a className="underline" href={enroll.uri}>
              {enroll.uri}
            </a>
          </p>
          <div className="flex gap-2">
            <Input
              readOnly
              aria-label="Provisioning URI"
              value={enroll.uri}
              onFocus={(event) => event.currentTarget.select()}
            />
            <Button type="button" variant="outline" onClick={copyUri}>
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
          <div className="flex gap-2">
            <Input
              aria-label="Authenticator code"
              inputMode="numeric"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              disabled={activating}
            />
            <Button onClick={activate} disabled={code.length < 6 || activating}>
              Activate
            </Button>
          </div>
        </div>
      )}

      {totpEnabled && (
        <div className="space-y-3 rounded-md border border-border p-4">
          <p className="text-sm font-medium">Disable two-factor authentication</p>
          {disableError && (
            <p role="alert" className="text-sm text-destructive">
              {disableError}
            </p>
          )}
          <div className="flex gap-2">
            <Input
              aria-label="Confirmation code"
              inputMode="numeric"
              value={disableCode}
              onChange={(event) => setDisableCode(event.target.value)}
              disabled={disabling}
            />
            <Input
              aria-label="Password"
              type="password"
              value={disablePassword}
              onChange={(event) => setDisablePassword(event.target.value)}
              disabled={disabling}
            />
            <Button
              variant="outline"
              onClick={disable}
              disabled={disableCode.length < 6 || disabling}
            >
              Disable 2FA
            </Button>
          </div>
        </div>
      )}

      <section className="space-y-3 rounded-md border border-border p-4">
        <SessionsCard />
      </section>

      <section className="space-y-3 rounded-md border border-border p-4">
        <LoginHistoryCard scope="me" />
      </section>

      <section className="space-y-3 rounded-md border border-border p-4">
        <OrgSecurityPolicyCard />
      </section>

      <section className="space-y-3 rounded-md border border-border p-4">
        <LoginHistoryCard scope="org" />
      </section>

      <section className="space-y-3 rounded-md border border-border p-4">
        <fieldset>
          <legend className="text-sm font-medium">Contact visibility</legend>

          {contactVisQuery.isPending ? (
            <Spinner />
          ) : contactVisQuery.isError ? (
            <div className="space-y-2">
              <p role="alert" className="text-sm text-destructive">
                {getErrorMessage(contactVisQuery.error)}
              </p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => contactVisQuery.refetch()}
              >
                Retry
              </Button>
            </div>
          ) : (
            <div className="mt-2 space-y-3">
              {VISIBILITY_OPTIONS.map((option) => {
                const descriptionId = `${option.id}-description`;
                const serverValue = contactVisQuery.data?.contact_visibility;
                return (
                  <div key={option.value}>
                    <label htmlFor={option.id} className="flex items-center gap-2 text-sm">
                      <input
                        id={option.id}
                        type="radio"
                        name="contact-visibility"
                        value={option.value}
                        checked={serverValue === option.value}
                        disabled={!canUpdateVisibility || updateContactVisibility.isPending}
                        title={
                          !canUpdateVisibility
                            ? "You don't have permission to change this."
                            : undefined
                        }
                        aria-describedby={descriptionId}
                        onChange={() => {
                          if (!canUpdateVisibility) return;
                          updateContactVisibility.mutate({
                            contact_visibility: option.value,
                          });
                        }}
                      />
                      <span className="font-medium">{option.label}</span>
                    </label>
                    <p
                      id={descriptionId}
                      className="ml-6 text-xs text-muted-foreground"
                    >
                      {option.description}
                    </p>
                  </div>
                );
              })}
            </div>
          )}

          {updateContactVisibility.isPending && (
            <p className="text-sm text-muted-foreground">Saving…</p>
          )}
          {updateContactVisibility.isSuccess && (
            <p className="text-sm text-green-400">Saved.</p>
          )}
          {updateContactVisibility.isError && (
            <p role="alert" className="text-sm text-destructive">
              {getErrorMessage(updateContactVisibility.error)}
            </p>
          )}
        </fieldset>
      </section>
    </div>
  );
}
