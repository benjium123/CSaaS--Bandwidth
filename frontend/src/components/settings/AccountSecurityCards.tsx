import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { Button, Input, Spinner, mutationErrorMessage } from "@/components/ui/primitives";

/** P42 account security: password change, recovery codes, account activity, and the
 * workspace's session timeouts. */

export function ChangePasswordCard() {
  const { api } = useAuth();
  const [current, setCurrent] = React.useState("");
  const [next, setNext] = React.useState("");
  const change = useMutation({
    mutationFn: () =>
      api.request("/api/v1/auth/password/change", {
        method: "POST",
        json: { current_password: current, new_password: next },
      }),
    onSuccess: () => {
      setCurrent("");
      setNext("");
    },
  });

  return (
    <div className="space-y-3">
      <div>
        <p className="text-sm font-medium">Password</p>
        <p className="text-xs text-muted-foreground">
          Changing it signs out every other device. At least 12 characters; passwords found in
          data breaches are refused.
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto]">
        <Input aria-label="Current password" type="password" autoComplete="current-password" placeholder="Current password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        <Input aria-label="New password" type="password" autoComplete="new-password" placeholder="New password" value={next} onChange={(e) => setNext(e.target.value)} />
        <Button type="button" disabled={!current || !next || change.isPending} onClick={() => change.mutate()}>
          Change password
        </Button>
      </div>
      {change.isSuccess && <p className="text-sm text-[hsl(var(--cx-live))]">Password changed.</p>}
      {change.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(change.error)}</p>}
    </div>
  );
}

export function RecoveryCodesCard() {
  const { api, me } = useAuth();
  const queryClient = useQueryClient();
  const [codes, setCodes] = React.useState<string[] | null>(null);
  const status = useQuery({
    queryKey: ["auth", "recovery-codes"],
    queryFn: () => api.request<{ remaining: number }>("/api/v1/auth/recovery-codes"),
    enabled: Boolean(me?.totp_enabled || me?.has_passkey || me?.email_2fa_enabled),
  });
  const generate = useMutation({
    mutationFn: () => api.request<{ codes: string[] }>("/api/v1/auth/recovery-codes", { method: "POST" }),
    onSuccess: (res) => {
      setCodes(res.codes);
      void queryClient.invalidateQueries({ queryKey: ["auth", "recovery-codes"] });
    },
  });

  if (!me?.totp_enabled && !me?.has_passkey && !me?.email_2fa_enabled) return null;

  return (
    <div className="space-y-3">
      <div>
        <p className="text-sm font-medium">Recovery codes</p>
        <p className="text-xs text-muted-foreground">
          One-time codes for signing in if you lose your passkey and phone. Store them somewhere
          safe, like a password manager.
        </p>
      </div>
      {codes ? (
        <div className="space-y-2">
          <p className="rounded-lg border border-[hsl(var(--cx-flag)/0.35)] bg-[hsl(var(--cx-flag)/0.1)] px-3.5 py-2.5 text-sm font-medium text-[hsl(var(--cx-flag))]">Save these now - they will not be shown again.</p>
          <ul className="grid grid-cols-2 gap-2 rounded-lg bg-muted p-3.5 font-mono text-sm">
            {codes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
          <Button type="button" variant="outline" onClick={() => void navigator.clipboard?.writeText(codes.join("\n"))}>
            Copy codes
          </Button>
        </div>
      ) : status.isPending ? (
        <Spinner label="Loading" />
      ) : (
        <p className="text-sm">
          {status.data?.remaining ? `${status.data.remaining} unused codes left.` : "You have no recovery codes."}
        </p>
      )}
      <Button type="button" variant={status.data?.remaining ? "outline" : "default"} onClick={() => generate.mutate()} disabled={generate.isPending}>
        {status.data?.remaining ? "Generate new codes" : "Generate recovery codes"}
      </Button>
      {generate.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(generate.error)}</p>}
    </div>
  );
}

const ACTIVITY_LABELS: Record<string, string> = {
  "password.changed": "Password changed",
  "password.reset_requested": "Password reset requested",
  "password.reset": "Password reset",
  "totp.enabled": "Authenticator app added",
  "totp.disabled": "Authenticator app removed",
  "email_2fa.enabled": "Email codes turned on",
  "email_2fa.disabled": "Email codes turned off",
  "passkey.added": "Passkey added",
  "passkey.removed": "Passkey removed",
  "recovery_codes.generated": "Recovery codes generated",
  "recovery_code.used": "Signed in with a recovery code",
  "account.recovered_identity": "Account recovered with ID check",
  "factors.reset_by_admin": "Sign-in methods reset by a workspace admin",
  "factors.reset_by_operator": "Sign-in methods reset by support",
  "sessions.revoked": "Sessions signed out",
  "account.locked": "Account temporarily locked",
  "account.unlocked": "Account unlocked",
  "account.deactivated": "Account deactivated",
  "account.reactivated": "Account reactivated",
};

export function AccountActivityCard() {
  const { api } = useAuth();
  const q = useQuery({
    queryKey: ["auth", "activity"],
    queryFn: () =>
      api.request<{ action: string; at: string; ip: string | null; by_someone_else: boolean }[]>(
        "/api/v1/auth/activity?limit=20",
      ),
  });
  return (
    <div className="space-y-2">
      <p className="text-sm font-medium">Account security activity</p>
      {q.isPending ? (
        <Spinner label="Loading activity" />
      ) : q.isError ? (
        <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(q.error)}</p>
      ) : q.data.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing yet.</p>
      ) : (
        <ul className="space-y-1 text-sm">
          {q.data.map((row) => (
            <li key={`${row.action}-${row.at}`} className="flex justify-between gap-3 rounded-md px-3 py-2 odd:bg-muted/40">
              <span>
                {ACTIVITY_LABELS[row.action] ?? row.action}
                {row.by_someone_else ? " (by someone else)" : ""}
              </span>
              <span className="shrink-0 text-muted-foreground">
                {new Date(row.at).toLocaleString()}
                {row.ip ? ` · ${row.ip}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type SecurityPolicy = {
  session_idle_minutes: number | null;
  session_max_hours: number | null;
  trust_idp_mfa: boolean;
};

export function SessionPolicyCard() {
  const { api, me, orgId } = useAuth();
  const queryClient = useQueryClient();
  const canEdit = hasPermission(me, orgId, "settings:write");
  const q = useQuery({
    queryKey: ["orgs", orgId, "security"],
    queryFn: () => api.request<SecurityPolicy>("/api/v1/orgs/current/security"),
    enabled: Boolean(orgId),
  });
  const [idle, setIdle] = React.useState("");
  const [max, setMax] = React.useState("");
  React.useEffect(() => {
    if (!q.data) return;
    setIdle(q.data.session_idle_minutes?.toString() ?? "");
    setMax(q.data.session_max_hours?.toString() ?? "");
  }, [q.data]);
  const save = useMutation({
    mutationFn: (body: Partial<SecurityPolicy>) =>
      api.request("/api/v1/orgs/current/security", { method: "PATCH", json: body }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["orgs", orgId, "security"] }),
  });

  if (!orgId || q.isError) return null;
  return (
    <div className="space-y-3">
      <div>
        <p className="text-sm font-medium">Session timeouts for this workspace</p>
        <p className="text-xs text-muted-foreground">
          Leave blank to use the platform default (30 minutes idle, 12 hours maximum). You can only make them shorter.
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto]">
        <Input aria-label="Idle minutes" type="number" min={5} placeholder="Idle minutes" value={idle} disabled={!canEdit} onChange={(e) => setIdle(e.target.value)} />
        <Input aria-label="Maximum hours" type="number" min={1} placeholder="Maximum hours" value={max} disabled={!canEdit} onChange={(e) => setMax(e.target.value)} />
        <Button
          type="button"
          disabled={!canEdit || save.isPending}
          onClick={() =>
            save.mutate({
              session_idle_minutes: idle ? Number(idle) : null,
              session_max_hours: max ? Number(max) : null,
            })
          }
        >
          Save
        </Button>
      </div>
      <label className="flex items-start gap-2.5 text-sm">
        <input
          type="checkbox"
          checked={Boolean(q.data?.trust_idp_mfa)}
          disabled={!canEdit || save.isPending}
          onChange={(e) => save.mutate({ trust_idp_mfa: e.target.checked })}
        />
        Our SSO provider enforces phishing-resistant MFA (lets admins sign in via SSO instead of a passkey)
      </label>
      {save.isSuccess && <p className="text-sm text-[hsl(var(--cx-live))]">Saved.</p>}
      {save.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(save.error)}</p>}
    </div>
  );
}

/** Email codes as a second factor: a six-digit code sent to the account address. */
export function EmailCodesCard() {
  const { api, me, refreshMe } = useAuth();
  const [password, setPassword] = React.useState("");
  const [sent, setSent] = React.useState(false);
  const [code, setCode] = React.useState("");
  const on = Boolean(me?.email_2fa_enabled);
  const act = useMutation({
    mutationFn: async (step: "send" | "activate" | "disable") => {
      if (step === "send") {
        await api.request("/api/v1/auth/2fa/email/enrol/send", { method: "POST", json: { password } });
        setSent(true);
      } else if (step === "activate") {
        await api.request("/api/v1/auth/2fa/email/enrol/activate", { method: "POST", json: { code } });
      } else {
        await api.request("/api/v1/auth/2fa/email/disable", { method: "POST", json: { password } });
      }
    },
    onSuccess: async (_data, step) => {
      if (step === "send") return;
      setPassword("");
      setCode("");
      setSent(false);
      await refreshMe();
    },
  });

  return (
    <div className="space-y-3">
      <div>
        <p className="text-sm font-medium">Email codes {on ? <span className="text-[hsl(var(--cx-live))]">· On</span> : null}</p>
        <p className="text-xs text-muted-foreground">
          {on
            ? `Signing in asks for a six-digit code sent to ${me?.email ?? "your email"}.`
            : "Get a six-digit code by email when you sign in. Simple, but a passkey or an authenticator app is stronger: anyone who can read your email could also get the code."}
        </p>
      </div>
      {!sent ? (
        <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
          <Input aria-label="Password for email codes" type="password" autoComplete="current-password" placeholder="Your password" value={password} onChange={(e) => setPassword(e.target.value)} />
          <Button type="button" variant={on ? "outline" : "default"} disabled={!password || act.isPending} onClick={() => act.mutate(on ? "disable" : "send")}>
            {on ? "Turn off email codes" : "Send me a code"}
          </Button>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
          <Input aria-label="Email code" inputMode="numeric" autoComplete="one-time-code" placeholder="Six-digit code" value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} />
          <Button type="button" disabled={code.length !== 6 || act.isPending} onClick={() => act.mutate("activate")}>
            Turn on email codes
          </Button>
        </div>
      )}
      {sent && <p className="text-xs text-muted-foreground">We sent a code to {me?.email}. It works for 10 minutes.</p>}
      {act.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(act.error)}</p>}
    </div>
  );
}
