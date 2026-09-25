import * as React from "react";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import {
  getErrorMessage,
  useCurrentOrg,
  useUpdateContactVisibility,
  type ContactVisibility,
} from "@/api/contacts";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { TotpEnrolment } from "@/components/security/TotpEnrolment";
import { PageHeader, SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";
import { SessionsCard } from "@/components/settings/SessionsCard";
import { PasskeysCard } from "@/components/settings/PasskeysCard";
import {
  AccountActivityCard,
  ChangePasswordCard,
  EmailCodesCard,
  RecoveryCodesCard,
  SessionPolicyCard,
} from "@/components/settings/AccountSecurityCards";
import { LoginHistoryCard } from "@/components/settings/LoginHistoryCard";
import { OrgSecurityPolicyCard } from "@/components/settings/OrgSecurityPolicyCard";
import {
  SamlSsoCard,
  ScimTokensCard,
  VerifiedDomainsCard,
} from "@/components/settings/EnterpriseSsoCards";

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
  const [enrollPassword, setEnrollPassword] = React.useState("");
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
      // The endpoint requires a password (PasswordIn) - confirming it stops someone using
      // an unattended screen from attaching their own authenticator.
      const res = await api.request<{ secret: string; provisioning_uri: string }>(
        "/api/v1/auth/2fa/enroll",
        { method: "POST", json: { password: enrollPassword } },
      );
      setEnroll({ secret: res.secret, uri: res.provisioning_uri });
      setEnrollPassword("");
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
    <div className="mx-auto max-w-3xl space-y-[14px] p-6 sm:p-8">
      <PageHeader
        title="Security"
        description="Sign-in, sessions, and who in the workspace can see what."
      />

      {message && (
        // Deliberately NOT green: this same line says "Two-factor authentication is off."
        <p className="text-[13px] text-[hsl(var(--cx-text))]">{message}</p>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-[hsl(var(--cx-danger))]">
          {error}
        </p>
      )}

      <SurfaceCard className="space-y-[12px]">
        <SectionLabel>Two-factor authentication</SectionLabel>
        {!enroll ? (
          <div className="space-y-[12px]">
            <p className="text-[13px] text-[hsl(var(--cx-subtle))]">
              Confirming your password stops someone using an unattended screen from
              attaching their own authenticator.
            </p>
            <div className="flex gap-[11px]">
              <Input
                aria-label="Your password"
                type="password"
                autoComplete="current-password"
                placeholder="Confirm your password"
                value={enrollPassword}
                onChange={(event) => setEnrollPassword(event.target.value)}
                disabled={enrolling}
              />
              <Button
                className="rounded-full"
                onClick={startEnroll}
                disabled={!enrollPassword || enrolling}
              >
                Set up two-factor authentication
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-[12px]">
            <TotpEnrolment secret={enroll.secret} uri={enroll.uri} />
            <p className="text-[13px] text-[hsl(var(--cx-subtle))]">
              Or open it directly:{" "}
              <a className="break-all underline" href={enroll.uri}>
                {enroll.uri}
              </a>
            </p>
            <div className="flex gap-[11px]">
              <Input
                readOnly
                aria-label="Provisioning URI"
                value={enroll.uri}
                onFocus={(event) => event.currentTarget.select()}
              />
              <Button
                type="button"
                variant="outline"
                className="rounded-full"
                onClick={copyUri}
              >
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
            <div className="flex gap-[11px]">
              <Input
                aria-label="Authenticator code"
                inputMode="numeric"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                disabled={activating}
              />
              <Button
                className="rounded-full"
                onClick={activate}
                disabled={code.length < 6 || activating}
              >
                Activate
              </Button>
            </div>
          </div>
        )}
      </SurfaceCard>

      {totpEnabled && (
        <SurfaceCard className="space-y-[12px]">
          <SectionLabel>Disable two-factor authentication</SectionLabel>
          {disableError && (
            <p role="alert" className="text-[13px] text-[hsl(var(--cx-danger))]">
              {disableError}
            </p>
          )}
          <div className="flex flex-wrap gap-[11px]">
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
              className="rounded-full"
              onClick={disable}
              disabled={disableCode.length < 6 || disabling}
            >
              Disable 2FA
            </Button>
          </div>
        </SurfaceCard>
      )}

      <SurfaceCard className="space-y-[12px]">
        <PasskeysCard />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <EmailCodesCard />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <RecoveryCodesCard />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <ChangePasswordCard />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <AccountActivityCard />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <SessionPolicyCard />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <SessionsCard />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <LoginHistoryCard scope="me" />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <OrgSecurityPolicyCard />
      </SurfaceCard>

      {/* The enterprise cards render their own <Section>; they were the only blocks on
          this page standing on the bare page background, so they get the same surface. */}
      <SurfaceCard className="space-y-[12px]">
        <VerifiedDomainsCard />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <SamlSsoCard />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <ScimTokensCard />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <LoginHistoryCard scope="org" />
      </SurfaceCard>

      <SurfaceCard className="space-y-[12px]">
        <fieldset>
          <legend className="text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
            Contact visibility
          </legend>

          {contactVisQuery.isPending ? (
            <Spinner />
          ) : contactVisQuery.isError ? (
            <div className="mt-[12px] space-y-[11px]">
              <p role="alert" className="text-[13px] text-[hsl(var(--cx-danger))]">
                {getErrorMessage(contactVisQuery.error)}
              </p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="rounded-full"
                onClick={() => contactVisQuery.refetch()}
              >
                Retry
              </Button>
            </div>
          ) : (
            <div className="mt-[12px] space-y-[8px]">
              {VISIBILITY_OPTIONS.map((option) => {
                const descriptionId = `${option.id}-description`;
                const serverValue = contactVisQuery.data?.contact_visibility;
                return (
                  <div
                    key={option.value}
                    className="rounded-[12px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-[12px] py-[11px]"
                  >
                    <label
                      htmlFor={option.id}
                      className="flex items-center gap-[10px] text-[13px] text-[hsl(var(--cx-text))]"
                    >
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
                      className="ml-[26px] mt-[4px] text-[11.5px] leading-[1.5] text-[hsl(var(--cx-muted))]"
                    >
                      {option.description}
                    </p>
                  </div>
                );
              })}
            </div>
          )}

          {updateContactVisibility.isPending && (
            <p className="mt-[11px] text-[13px] text-[hsl(var(--cx-muted))]">Saving…</p>
          )}
          {updateContactVisibility.isSuccess && (
            <p className="mt-[11px] text-[13px] text-[hsl(var(--cx-live))]">Saved.</p>
          )}
          {updateContactVisibility.isError && (
            <p role="alert" className="mt-[11px] text-[13px] text-[hsl(var(--cx-danger))]">
              {getErrorMessage(updateContactVisibility.error)}
            </p>
          )}
        </fieldset>
      </SurfaceCard>
    </div>
  );
}
