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
      <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
        <Input aria-label="Current password" type="password" autoComplete="current-password" placeholder="Current password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        <Input aria-label="New password" type="password" autoComplete="new-password" placeholder="New password" value={next} onChange={(e) => setNext(e.target.value)} />
        <Button type="button" disabled={!current || !next || change.isPending} onClick={() => change.mutate()}>
          Change password
        </Button>
      </div>
      {change.isSuccess && <p className="text-sm text-emerald-300">Password changed.</p>}
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
    enabled: Boolean(me?.totp_enabled || me?.has_passkey),
  });
  const generate = useMutation({
    mutationFn: () => api.request<{ codes: string[] }>("/api/v1/auth/recovery-codes", { method: "POST" }),
    onSuccess: (res) => {
      setCodes(res.codes);
      void queryClient.invalidateQueries({ queryKey: ["auth", "recovery-codes"] });
    },
  });

  if (!me?.totp_enabled && !me?.has_passkey) return null;

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
          <p className="text-sm font-medium text-amber-300">Save these now - they will not be shown again.</p>
          <ul className="grid grid-cols-2 gap-1 rounded-md bg-muted p-3 font-mono text-sm">
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
            <li key={`${row.action}-${row.at}`} className="flex justify-between gap-2">
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
      <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
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
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={Boolean(q.data?.trust_idp_mfa)}
          disabled={!canEdit || save.isPending}
          onChange={(e) => save.mutate({ trust_idp_mfa: e.target.checked })}
        />
        Our SSO provider enforces phishing-resistant MFA (lets admins sign in via SSO instead of a passkey)
      </label>
      {save.isSuccess && <p className="text-sm text-emerald-300">Saved.</p>}
      {save.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(save.error)}</p>}
    </div>
  );
}
