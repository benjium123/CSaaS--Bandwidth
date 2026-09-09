import * as React from "react";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import {
  useCreateInvite,
  useInvites,
  useOrgMembers,
  useRevokeInvite,
  type InviteCreatedOut,
  type InviteOut,
} from "@/api/hooks";
import {
  getErrorMessage,
  startingPointPermissions,
  STARTING_POINTS,
  useCreateRole,
  useDeleteRole,
  useRoles,
  useUpdateRole,
  type RoleOut,
} from "@/api/roles";
import { Badge, Button, Input, Spinner } from "@/components/ui/primitives";
import { RoleMatrix } from "@/components/team/RoleMatrix";

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
  const { api, me, orgId } = useAuth();
  const [activeTab, setActiveTab] = React.useState<"members" | "roles">("members");

  const {
    data: members,
    isPending: membersLoading,
    error: membersError,
    refetch: refetchMembers,
  } = useOrgMembers(api);
  const {
    data: invites,
    isPending: invitesLoading,
    error: invitesError,
    refetch: refetchInvites,
  } = useInvites(api);
  const createInvite = useCreateInvite(api);
  const revokeInvite = useRevokeInvite(api);

  const rolesQuery = useRoles(api, activeTab === "roles");
  const createRole = useCreateRole(api);
  const updateRole = useUpdateRole(api);
  const deleteRole = useDeleteRole(api);

  const [email, setEmail] = React.useState("");
  const [role, setRole] = React.useState("agent");
  const [error, setError] = React.useState<string | null>(null);
  const [created, setCreated] = React.useState<InviteCreatedOut | null>(null);

  const [rolePanelOpen, setRolePanelOpen] = React.useState(false);
  const [editingRole, setEditingRole] = React.useState<RoleOut | null>(null);
  const [roleName, setRoleName] = React.useState("");
  const [rolePermissions, setRolePermissions] = React.useState<string[]>([]);
  const [startingPoint, setStartingPoint] = React.useState("blank");
  const [cloneFrom, setCloneFrom] = React.useState<string | null>(null);
  const [roleError, setRoleError] = React.useState<string | null>(null);
  const [roleSaved, setRoleSaved] = React.useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = React.useState<string | null>(null);

  const canEditRoles = hasPermission(me, orgId, "roles:write");
  const grantablePermissions = React.useCallback(
    (key: string) => hasPermission(me, orgId, key),
    [me, orgId],
  );

  async function submitInvite(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const result = await createInvite.mutateAsync({ email, role_name: role });
      setCreated(result);
      setEmail("");
    } catch (err) {
      setError(getErrorMessage(err));
    }
  }

  async function revoke(id: string) {
    setError(null);
    try {
      await revokeInvite.mutateAsync(id);
    } catch (err) {
      setError(getErrorMessage(err));
    }
  }

  function openNewRole() {
    setRolePanelOpen(true);
    setEditingRole(null);
    setRoleName("");
    setRolePermissions([]);
    setStartingPoint("blank");
    setCloneFrom(null);
    setRoleError(null);
    setRoleSaved(false);
  }

  function openEditRole(target: RoleOut) {
    setRolePanelOpen(true);
    setEditingRole(target);
    setRoleName(target.name);
    setRolePermissions([...target.permissions]);
    setRoleError(null);
    setRoleSaved(false);
  }

  function chooseStartingPoint(point: string) {
    setStartingPoint(point);
    const resolved = startingPointPermissions(rolesQuery.data ?? [], point);
    setRolePermissions(resolved.permissions);
    setCloneFrom(resolved.clone_from ?? null);
  }

  async function saveRole(event: React.FormEvent) {
    event.preventDefault();
    setRoleError(null);
    setRoleSaved(false);
    try {
      if (editingRole) {
        await updateRole.mutateAsync({
          id: editingRole.id,
          name: roleName,
          permissions: rolePermissions,
        });
      } else {
        await createRole.mutateAsync({
          name: roleName,
          permissions: rolePermissions,
          ...(cloneFrom ? { clone_from: cloneFrom } : {}),
        });
      }
      setRoleSaved(true);
      setRolePanelOpen(false);
      setEditingRole(null);
    } catch (err) {
      setRoleError(getErrorMessage(err));
    }
  }

  async function confirmDelete() {
    if (!confirmDeleteId || !canEditRoles) return;
    setRoleError(null);
    setRoleSaved(false);
    try {
      await deleteRole.mutateAsync({ id: confirmDeleteId });
      setConfirmDeleteId(null);
      setRoleSaved(true);
    } catch (err) {
      setRoleError(getErrorMessage(err));
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8 p-6">
      <div role="tablist" className="flex gap-2">
        <Button
          type="button"
          role="tab"
          id="tab-members"
          aria-controls="panel-members"
          aria-selected={activeTab === "members"}
          variant={activeTab === "members" ? "default" : "outline"}
          onClick={() => setActiveTab("members")}
        >
          Members
        </Button>
        <Button
          type="button"
          role="tab"
          id="tab-roles"
          aria-controls="panel-roles"
          aria-selected={activeTab === "roles"}
          variant={activeTab === "roles" ? "default" : "outline"}
          onClick={() => setActiveTab("roles")}
        >
          Roles
        </Button>
      </div>

      {activeTab === "members" && (
        <div
          role="tabpanel"
          id="panel-members"
          aria-labelledby="tab-members"
          className="space-y-8"
        >
          <div className="space-y-4">
            <h1 className="text-lg font-semibold">Team</h1>

            {membersLoading ? (
              <Spinner />
            ) : membersError ? (
              <div className="space-y-2">
                <p role="alert" className="text-sm text-destructive">
                  {getErrorMessage(membersError)}
                </p>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => refetchMembers()}
                >
                  Retry
                </Button>
              </div>
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
                    {(members ?? []).map((member) => (
                      <tr key={member.user_id}>
                        <td className="px-3 py-2">{member.full_name}</td>
                        <td className="px-3 py-2 text-xs text-muted-foreground">
                          {member.email}
                        </td>
                        <td className="px-3 py-2 text-xs text-muted-foreground">
                          {member.role_name}
                        </td>
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
            ) : invitesError ? (
              <div className="space-y-2">
                <p role="alert" className="text-sm text-destructive">
                  {getErrorMessage(invitesError)}
                </p>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => refetchInvites()}
                >
                  Retry
                </Button>
              </div>
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
                    {(invites ?? []).map((invite) => {
                      const status = inviteStatus(invite);
                      return (
                        <tr key={invite.id}>
                          <td className="px-3 py-2">{invite.email}</td>
                          <td className="px-3 py-2 text-xs text-muted-foreground">
                            {invite.role_name}
                          </td>
                          <td className="px-3 py-2">
                            <Badge className={statusBadgeClass(status)}>{status}</Badge>
                          </td>
                          <td className="px-3 py-2">
                            {status === "Pending" ? (
                              <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                onClick={() => revoke(invite.id)}
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
                  onChange={(event) => setEmail(event.target.value)}
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
                  onChange={(event) => setRole(event.target.value)}
                >
                  {INVITABLE_ROLES.map((entry) => (
                    <option key={entry.value} value={entry.value}>
                      {entry.label}
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
                    onFocus={(event) => event.currentTarget.select()}
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
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => setCreated(null)}
                >
                  Dismiss
                </Button>
              </div>
            )}
          </div>
        </div>
      )}

      {activeTab === "roles" && (
        <div
          role="tabpanel"
          id="panel-roles"
          aria-labelledby="tab-roles"
          className="space-y-4"
        >
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold">Roles</h2>
            <Button
              type="button"
              disabled={!canEditRoles}
              title={!canEditRoles ? "You don't have permission to change roles." : undefined}
              onClick={openNewRole}
            >
              New role
            </Button>
          </div>

          {roleSaved && <Badge className="bg-green-100 text-green-800">Saved</Badge>}
          {roleError && (
            <p role="alert" className="text-sm text-destructive">
              {roleError}
            </p>
          )}

          {rolesQuery.isPending ? (
            <Spinner />
          ) : rolesQuery.isError ? (
            <div className="space-y-2">
              <p role="alert" className="text-sm text-destructive">
                {getErrorMessage(rolesQuery.error)}
              </p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => rolesQuery.refetch()}
              >
                Retry
              </Button>
            </div>
          ) : (rolesQuery.data ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No custom roles yet. Create one to give people exactly the access they need.
            </p>
          ) : (
            <div className="overflow-x-auto rounded-md border border-border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    <th className="px-3 py-2 font-medium">Role</th>
                    <th className="px-3 py-2 font-medium">Type</th>
                    <th className="px-3 py-2 font-medium">Members</th>
                    <th className="px-3 py-2 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(rolesQuery.data ?? []).map((target) => (
                    <tr key={target.id}>
                      <td className="px-3 py-2">{target.name}</td>
                      <td className="px-3 py-2">
                        {target.is_system ? (
                          <Badge className="bg-gray-100 text-gray-600">Built-in</Badge>
                        ) : null}
                      </td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">
                        {target.member_count === 1
                          ? "1 member"
                          : `${target.member_count} members`}
                      </td>
                      <td className="flex gap-2 px-3 py-2 text-right">
                        <Button
                          type="button"
                          size="sm"
                          variant={target.is_system ? "outline" : "default"}
                          onClick={() => openEditRole(target)}
                        >
                          {target.is_system ? "View" : "Edit"}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={!canEditRoles || target.is_system || target.member_count > 0}
                          title={
                            !canEditRoles
                              ? "You don't have permission to change roles."
                              : target.is_system
                                ? "Built-in roles can't be deleted."
                                : target.member_count > 0
                                  ? "This role is still assigned to someone."
                                  : undefined
                          }
                          onClick={() => setConfirmDeleteId(target.id)}
                        >
                          Delete
                        </Button>
                        {confirmDeleteId === target.id && (
                          <Button
                            type="button"
                            size="sm"
                            variant="destructive"
                            disabled={deleteRole.isPending}
                            onClick={confirmDelete}
                          >
                            {deleteRole.isPending ? "Deleting…" : "Confirm delete"}
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {rolePanelOpen && (
            <form className="space-y-4 rounded-md border border-border p-4" onSubmit={saveRole}>
              <div className="space-y-1">
                <label className="block text-xs text-muted-foreground" htmlFor="role-name">
                  Role name
                </label>
                <Input
                  id="role-name"
                  aria-label="Role name"
                  value={roleName}
                  onChange={(event) => setRoleName(event.target.value)}
                  disabled={Boolean(editingRole?.is_system) || !canEditRoles}
                />
              </div>

              {!editingRole && (
                <div className="space-y-2">
                  <p className="text-xs font-medium">Start from</p>
                  <div className="flex flex-wrap gap-2">
                    {STARTING_POINTS.map((point) => (
                      <Button
                        key={point.id}
                        type="button"
                        size="sm"
                        variant={startingPoint === point.id ? "default" : "outline"}
                        aria-pressed={startingPoint === point.id}
                        aria-label={`${point.label} starting point`}
                        onClick={() => chooseStartingPoint(point.id)}
                      >
                        <span className="flex flex-col items-start">
                          <span>{point.label}</span>
                          <span className="text-[10px] font-normal opacity-80">
                            {point.description}
                          </span>
                        </span>
                      </Button>
                    ))}
                  </div>
                </div>
              )}

              <RoleMatrix
                value={rolePermissions}
                onChange={setRolePermissions}
                readOnly={!canEditRoles || Boolean(editingRole?.is_system)}
                grantablePermissions={grantablePermissions}
              />

              <div className="flex gap-2">
                <Button
                  type="submit"
                  disabled={
                    !canEditRoles ||
                    Boolean(editingRole?.is_system) ||
                    createRole.isPending ||
                    updateRole.isPending
                  }
                  title={!canEditRoles ? "You don't have permission to change roles." : undefined}
                >
                  {createRole.isPending || updateRole.isPending ? "Saving…" : "Save"}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setRolePanelOpen(false);
                    setEditingRole(null);
                  }}
                >
                  Cancel
                </Button>
              </div>
            </form>
          )}
        </div>
      )}
    </div>
  );
}
