/**
 * WHY this page exists: the backend's SSO callback returns the minted token as JSON
 * instead of redirecting with it in the URL, so the console has to make the call itself
 * and hand the token to AuthContext.
 */
import * as React from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Button, Spinner } from "@/components/ui/primitives";

function ssoErrorMessage(code: string | null): string {
  switch (code) {
    case "sso_domain_unverified":
      return "Your workspace has not verified its email domain yet, so single sign-on is switched off. Ask your admin, or sign in with your password.";
    case "sso_domain_mismatch":
      return "This account's email address is not allowed to sign in to that workspace.";
    case "account_locked":
      return "This account is disabled. Contact your workspace admin.";
    default:
      return "We could not complete that sign-in. Start again from the sign-in page.";
  }
}

export function SsoCallbackPage() {
  const [params] = useSearchParams();
  const code = params.get("code");
  const state = params.get("state");
  const error = params.get("error");
  const errorDescription = params.get("error_description");
  // P42 SAML: the backend already verified the response and set the session cookie, then
  // sent the browser here with only the workspace id.
  const samlOrgId = params.get("saml") === "1" ? params.get("org_id") : null;

  const { api, completeSso } = useAuth();
  const navigate = useNavigate();
  const [status, setStatus] = React.useState<"working" | "error">("working");
  const [message, setMessage] = React.useState<string | null>(null);
  const exchangedRef = React.useRef(false);

  React.useEffect(() => {
    if (exchangedRef.current) return;
    exchangedRef.current = true;

    async function run() {
      if (error || ((!code || !state) && !samlOrgId)) {
        setStatus("error");
        setMessage(ssoErrorMessage(error));
        return;
      }

      if (samlOrgId) {
        const res = await completeSso(null, samlOrgId);
        if (res.kind === "ok") {
          navigate("/inbox", { replace: true });
        } else {
          setStatus("error");
          setMessage(res.kind === "error" ? res.message : ssoErrorMessage(null));
        }
        return;
      }
      if (!code || !state) return;

      try {
        const data = await api.request<{
          access_token: string | null;
          token_type: string;
          org_id: string;
        }>(
          "/api/v1/auth/sso/callback?code=" +
            encodeURIComponent(code) +
            "&state=" +
            encodeURIComponent(state),
        );
        const res = await completeSso(data.access_token, data.org_id);
        if (res.kind === "ok") {
          navigate("/inbox", { replace: true });
        } else {
          // LoginResult also carries a "needs_2fa" arm for the password flow; SSO never
          // returns it (the identity provider owns the second factor), so anything that
          // is not "ok" is reported as a failure.
          setStatus("error");
          setMessage(
            res.kind === "error" ? res.message : "We could not complete that sign-in.",
          );
        }
      } catch (err) {
        setStatus("error");
        setMessage(err instanceof Error ? err.message : "Something went wrong.");
      }
    }

    void run();
  }, [api, code, completeSso, error, navigate, samlOrgId, state]);

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      {status === "working" ? (
        <Spinner label="Finishing sign-in" />
      ) : (
        <div className="w-full max-w-sm space-y-4 rounded-lg border border-border p-6">
          <h1 className="text-lg font-semibold">Sign-in failed</h1>
          <p role="alert" className="text-sm text-destructive">
            {message}
          </p>
          {errorDescription && (
            <p className="text-xs text-muted-foreground">{errorDescription}</p>
          )}
          <Button type="button" variant="outline" onClick={() => navigate("/", { replace: true })}>
            Back to sign in
          </Button>
        </div>
      )}
    </div>
  );
}
