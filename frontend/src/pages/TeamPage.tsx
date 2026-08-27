import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import {
  useCreateInvite,
  useInvites,
  useOrgMembers,
  useRevokeInvite,
  type InviteCreatedOut,
  type InviteOut,
} from "@/api/hooks";
import { Badge, Button, Input, Spinner } from "@/components/ui/primitives";

// Owner is deliberately absent - an invite may never mint one. See
// services/invites.INVITABLE_ROLES on the backend.
const INVITABLE_ROLES = [
  { value: "admin", label: "Admin" },
  { value: "agent", label: "Agent" },
];

type InviteStatus = "Pending" | "Accepted" | "Revoked" | "Expired";

function inviteStatus(invite: InviteOut): InviteStatus {
  if (invite.revoked_at) return "Revoked";
  if (invite.accepted_at) return "Accepted";
  if (new Date(invite.expires_at).getTime() < Date.now()) return "Expired";
  return "Pending";
}

function statusBadgeClass(status: InviteStatus): string {
  switch (status) {
    case "Pending":
      return "bg-amber-100 text-amber-800";
    case "Accepted":
      return "bg-green-100 text-green-800";
    case "Revoked":
      return "bg-gray-100 text-gray-600";
    case "Expired":
      return "bg-red-100 text-red-800";
  }
}

export function TeamPage() {
  const { api } = useAuth();
  const { data: members, isLoading: membersLoading } = useOrgMembers(api);
  const { data: invites, isLoading: invitesLoading } = useInvites(api);
  const createInvite = useCreateInvite(api);
  const revokeInvite = useRevokeInvite(api);

  const [email, setEmail] = React.useState("");
  const [role, setRole] = React.useState("agent");
  const [error, setError] = React.useState<string | null>(null);
  const [created, setCreated] = React.useState<InviteCreatedOut | null>(null);

  async function submitInvite(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const result = await createInvite.mutateAsync({ email, role_name: role });
      setCreated(result);
      setEmail("");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function revoke(id: string) {
    setError(null);
    try {
      await revokeInvite.mutateAsync(id);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8 p-6">
      <div className="space-y-4">
        <h1 className="text-lg font-semibold">Team</h1>

        {membersLoading ? (
          <Spinner />
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Name</th>
                  <th className="px-3 py-2 font-medium">Email</th>
                  <th className="px-3 py-2 font-medium">Role</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {(members ?? []).map((m) => (
                  <tr key={m.user_id}>
                    <td className="px-3 py-2">{m.full_name}</td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">{m.email}</td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">{m.role_name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="space-y-4">
        <h2 className="text-base font-semibold">Invitations</h2>

        {invitesLoading ? (
          <Spinner />
        ) : (invites ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground">No invitations yet.</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Email</th>
                  <th className="px-3 py-2 font-medium">Role</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {(invites ?? []).map((inv) => {
                  const status = inviteStatus(inv);
                  return (
                    <tr key={inv.id}>
                      <td className="px-3 py-2">{inv.email}</td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">{inv.role_name}</td>
                      <td className="px-3 py-2">
                        <Badge className={statusBadgeClass(status)}>{status}</Badge>
                      </td>
                      <td className="px-3 py-2">
                        {status === "Pending" ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            onClick={() => revoke(inv.id)}
                            disabled={revokeInvite.isPending}
                          >
                            Revoke
                          </Button>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="space-y-4">
        <h2 className="text-base font-semibold">Invite someone</h2>
        <form className="flex flex-wrap items-end gap-2" onSubmit={submitInvite}>
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="invite-email">
              Email
            </label>
            <Input
              id="invite-email"
              aria-label="Email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="invite-role">
              Role
            </label>
            <select
              id="invite-role"
              aria-label="Role"
              className="h-9 rounded-md border border-border bg-background px-2 text-sm"
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              {INVITABLE_ROLES.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </div>
          <Button type="submit" disabled={createInvite.isPending}>
            Send invite
          </Button>
        </form>

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        {created && (
          <div className="space-y-2 rounded-md border border-amber-300 bg-amber-50 p-4 text-sm">
            <p className="font-medium">Invitation created for {created.email}</p>
            <p className="text-xs text-muted-foreground">
              This link is shown once and cannot be retrieved again. If it is lost, revoke this
              invitation and send a new one.
            </p>
            <div className="flex gap-2">
              <Input
                readOnly
                aria-label="Invite link"
                value={created.accept_url}
                onFocus={(e) => e.currentTarget.select()}
              />
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  navigator.clipboard?.writeText(created.accept_url).catch(() => {});
                }}
              >
                Copy
              </Button>
            </div>
            <Button type="button" size="sm" variant="ghost" onClick={() => setCreated(null)}>
              Dismiss
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
