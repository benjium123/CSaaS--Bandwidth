import * as React from "react";
import { useMutation } from "@tanstack/react-query";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
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
import {
  CONSOLE_CELL as CELL,
  CONSOLE_CELL_L as CELL_L,
  CONSOLE_CELL_R as CELL_R,
  CONSOLE_HEAD as HEAD,
  CONSOLE_PANEL as PANEL,
  CONSOLE_ROW as ROW,
  CONSOLE_TABLE as TABLE,
  InitialsAvatar,
} from "@/components/ui/consoleChrome";
import { Button, Input, Pill, Select, Spinner, type PillTone } from "@/components/ui/primitives";
import { AddTeammateDrawer } from "@/components/team/AddTeammateDrawer";
import { RoleMatrix } from "@/components/team/RoleMatrix";
import {
  MemberNumbersCell,
  MemberNumbersPanel,
} from "@/components/team/MemberNumbersPanel";
import { cn } from "@/lib/utils";

/* ── The console's list shape, from docs/design/console-reference.html ──────────────────
 * The panel, the row and the cell padding are the shared console constants now
 * (components/ui/consoleChrome.tsx). The <table> stays: it is the element the rows have to
 * be - TeamPage.test.tsx walks `closest("tr")` to scope its queries to one role. */

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

/** P20b: these were LIGHT-mode palette classes (bg-amber-100/text-amber-800 ...) rendering
 * inside a dark app shell - washed-out chips nobody could read. Statuses now go through the
 * shared `Pill` tones, which are the one place status colour is defined. */
function statusBadgeTone(status: InviteStatus): PillTone {
  switch (status) {
    case "Pending":
      return "warning";
    case "Accepted":
      return "success";
    case "Revoked":
      return "neutral";
    case "Expired":
      return "danger";
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

  /** Which member's numbers are expanded. One at a time: the panel is tall, and two of them
   *  open at once turns the table into a wall. */
  const [expandedUserId, setExpandedUserId] = React.useState<string | null>(null);

  /** Whether the Add-teammate drawer is open. */
  const [addOpen, setAddOpen] = React.useState(false);

  const canEditRoles = hasPermission(me, orgId, "roles:write");
  const canResetMembers = hasPermission(me, orgId, "members:update");
  /** Inviting is a capability, not a membership flag. This page already loads capabilities
   *  through MemberNumbersCell, so useGate adds no extra request. */
  const gate = useGate();
  const canInvite = gate.can("members:invite");
  /** Name, Email, Role, Numbers (+ Sign-in). One constant so the expanded row's colSpan
   *  cannot drift away from the header when a column is added. */
  const memberColumnCount = canResetMembers ? 5 : 4;
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
    <div className="mx-auto max-w-4xl space-y-7 p-6">
      {/* The reference's `.pills` filter row: full pills on `overlay`, the selected one on
          the accent. Same buttons, same roles, same names - only the shape moved. */}
      <div
        role="tablist"
        className="inline-flex w-fit gap-1.5 rounded-full bg-[hsl(var(--cx-overlay)/0.55)] p-1"
      >
        <Button
          type="button"
          role="tab"
          id="tab-members"
          aria-controls="panel-members"
          aria-selected={activeTab === "members"}
          variant={activeTab === "members" ? "default" : "ghost"}
          className="h-8 rounded-full px-4 text-[12.5px]"
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
          variant={activeTab === "roles" ? "default" : "ghost"}
          className="h-8 rounded-full px-4 text-[12.5px]"
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
          <div className="space-y-3">
            {/* Heading on the left, the invitation action on the right - the same header
                shape the Roles tab uses. The button renders only for people who may invite. */}
            <div className="flex items-center justify-between">
              <h1 className="text-[19px] font-semibold tracking-[-0.015em]">Team</h1>
              {canInvite && (
                <Button
                  type="button"
                  className="rounded-full px-5"
                  onClick={() => setAddOpen(true)}
                >
                  Add teammate
                </Button>
              )}
            </div>

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
                  className="rounded-full px-3.5"
                  variant="outline"
                  onClick={() => refetchMembers()}
                >
                  Retry
                </Button>
              </div>
            ) : (
              <div className={cn(PANEL, "overflow-x-auto")}>
                <table className={TABLE}>
                  <thead>
                    <tr>
                      <th className={HEAD}>Name</th>
                      <th className={HEAD}>Email</th>
                      <th className={HEAD}>Role</th>
                      <th className={HEAD}>Numbers</th>
                      {canResetMembers && <th className={HEAD}>Sign-in</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {(members ?? []).map((member) => (
                      <React.Fragment key={member.user_id}>
                      <tr className={ROW}>
                        <td className={CELL_L}>
                          <span className="flex items-center gap-3">
                            <InitialsAvatar
                              size="row"
                              seed={member.user_id}
                              name={member.full_name}
                            />
                            <span className="font-semibold text-foreground">{member.full_name}</span>
                          </span>
                        </td>
                        <td className={cn(CELL, "text-muted-foreground")}>{member.email}</td>
                        <td className={CELL}>
                          <Pill tone="info">{member.role_name}</Pill>
                        </td>
                        {/* Last cell in the row carries the right-hand radius, and which
                            cell that is depends on whether Sign-in is rendered at all. */}
                        <td className={canResetMembers ? CELL : CELL_R}>
                          <MemberNumbersCell
                            userId={member.user_id}
                            userName={member.full_name}
                            expanded={expandedUserId === member.user_id}
                            onToggle={() =>
                              setExpandedUserId((prev) =>
                                prev === member.user_id ? null : member.user_id,
                              )
                            }
                          />
                        </td>
                        {canResetMembers && (
                          <td className={CELL_R}>
                            {member.user_id !== me?.id && <ResetMemberTwoFactor userId={member.user_id} />}
                          </td>
                        )}
                      </tr>
                      {expandedUserId === member.user_id && (
                        <tr className={ROW}>
                          <td className={cn(CELL, "rounded-md")} colSpan={memberColumnCount}>
                            <MemberNumbersPanel
                              userId={member.user_id}
                              userName={member.full_name}
                            />
                          </td>
                        </tr>
                      )}
                      </React.Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="space-y-4">
            <h2 className="text-[15px] font-semibold tracking-[-0.01em]">Invitations</h2>

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
                  className="rounded-full px-3.5"
                  variant="outline"
                  onClick={() => refetchInvites()}
                >
                  Retry
                </Button>
              </div>
            ) : (invites ?? []).length === 0 ? (
              <p className="text-sm text-muted-foreground">No invitations yet.</p>
            ) : (
              <div className={cn(PANEL, "overflow-x-auto")}>
                <table className={TABLE}>
                  <thead>
                    <tr>
                      <th className={HEAD}>Email</th>
                      <th className={HEAD}>Role</th>
                      <th className={HEAD}>Status</th>
                      <th className={HEAD}>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(invites ?? []).map((invite) => {
                      const status = inviteStatus(invite);
                      return (
                        <tr key={invite.id} className={ROW}>
                          <td className={CELL_L}>
                            <span className="flex items-center gap-3">
                              <InitialsAvatar size="row" seed={invite.id} name={invite.email} />
                              <span className="font-medium text-foreground">{invite.email}</span>
                            </span>
                          </td>
                          <td className={CELL}>
                            <Pill tone="info">{invite.role_name}</Pill>
                          </td>
                          <td className={CELL}>
                            <Pill tone={statusBadgeTone(status)}>{status}</Pill>
                          </td>
                          <td className={CELL_R}>
                            {status === "Pending" ? (
                              <Button
                                type="button"
                                size="sm"
                                className="rounded-full px-3.5"
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
            <h2 className="text-[15px] font-semibold tracking-[-0.01em]">Invite someone</h2>
            <form
              className={cn(PANEL, "flex flex-wrap items-end gap-3 p-3.5")}
              onSubmit={submitInvite}
            >
              <div className="space-y-1.5">
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
              <div className="space-y-1.5">
                <label className="block text-xs text-muted-foreground" htmlFor="invite-role">
                  Role
                </label>
                <Select
                  id="invite-role"
                  aria-label="Role"
                  className="h-9 w-auto px-2.5"
                  value={role}
                  onChange={(event) => setRole(event.target.value)}
                >
                  {INVITABLE_ROLES.map((entry) => (
                    <option key={entry.value} value={entry.value}>
                      {entry.label}
                    </option>
                  ))}
                </Select>
              </div>
              {/* The reference's `.send`: a primary action is a pill. */}
              <Button type="submit" className="rounded-full px-5" disabled={createInvite.isPending}>
                Send invite
              </Button>
            </form>

            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}

            {created && (
              <div className="space-y-2.5 rounded-xl border border-border bg-[hsl(var(--cx-surface))] p-4 text-sm">
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
                  className="rounded-full px-3.5"
                  variant="ghost"
                  onClick={() => setCreated(null)}
                >
                  Dismiss
                </Button>
              </div>
            )}
          </div>

          {/* Mounted only for people who can invite, so an unauthorised viewer fires no
              capability-gated requests from the drawer. */}
          {canInvite && (
            <AddTeammateDrawer open={addOpen} onClose={() => setAddOpen(false)} />
          )}
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
            <h2 className="text-[15px] font-semibold tracking-[-0.01em]">Roles</h2>
            <Button
              type="button"
              className="rounded-full px-5"
              disabled={!canEditRoles}
              title={!canEditRoles ? "You don't have permission to change roles." : undefined}
              onClick={openNewRole}
            >
              New role
            </Button>
          </div>

          {roleSaved && <Pill tone="success">Saved</Pill>}
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
                className="rounded-full px-3.5"
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
            <div className={cn(PANEL, "overflow-x-auto")}>
              <table className={TABLE}>
                <thead>
                  <tr>
                    <th className={HEAD}>Role</th>
                    <th className={HEAD}>Type</th>
                    <th className={HEAD}>Members</th>
                    <th className={cn(HEAD, "text-right")}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {(rolesQuery.data ?? []).map((target) => (
                    <tr key={target.id} className={ROW}>
                      <td className={cn(CELL_L, "font-semibold text-foreground")}>{target.name}</td>
                      <td className={CELL}>
                        {target.is_system ? (
                          <Pill tone="neutral">Built-in</Pill>
                        ) : null}
                      </td>
                      <td className={cn(CELL, "text-muted-foreground")}>
                        {target.member_count === 1
                          ? "1 member"
                          : `${target.member_count} members`}
                      </td>
                      <td className={cn(CELL_R, "text-right")}>
                        <span className="flex justify-end gap-2">
                        <Button
                          type="button"
                          size="sm"
                          className="rounded-full px-3.5"
                          variant={target.is_system ? "outline" : "default"}
                          onClick={() => openEditRole(target)}
                        >
                          {target.is_system ? "View" : "Edit"}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          className="rounded-full px-3.5"
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
                            className="rounded-full px-3.5"
                            variant="destructive"
                            disabled={deleteRole.isPending}
                            onClick={confirmDelete}
                          >
                            {deleteRole.isPending ? "Deleting…" : "Confirm delete"}
                          </Button>
                        )}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {rolePanelOpen && (
            <form
              className="space-y-4 rounded-xl border border-border bg-[hsl(var(--cx-surface))] p-5"
              onSubmit={saveRole}
            >
              <div className="space-y-1.5">
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
                  <div className="flex flex-wrap gap-3">
                    {STARTING_POINTS.map((point) => (
                      <Button
                        key={point.id}
                        type="button"
                        size="sm"
                        // Two lines of text is a CARD, not a chip: 14px, and a height that
                        // grows with the description instead of clipping it.
                        className="h-auto rounded-lg px-3.5 py-2.5 text-left"
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

              <div className="flex gap-3">
                <Button
                  type="submit"
                  className="rounded-full px-5"
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
                  className="rounded-full px-5"
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

/** P42: a member who lost their phone gets their 2FA reset by an admin (fresh 2FA check,
 * audited, emailed). Owners/admins/billing recover through ID + selfie instead - the server
 * refuses those here. */
function ResetMemberTwoFactor({ userId }: { userId: string }) {
  const { api } = useAuth();
  const [confirming, setConfirming] = React.useState(false);
  const reset = useMutation({
    mutationFn: () =>
      api.request(`/api/v1/orgs/current/members/${userId}/reset-2fa`, { method: "POST" }),
    onSettled: () => setConfirming(false),
  });
  if (reset.isSuccess) return <span className="text-xs text-muted-foreground">2FA reset</span>;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {confirming ? (
        <>
          <Button type="button" size="sm" variant="outline" disabled={reset.isPending} onClick={() => reset.mutate()}>
            Confirm reset
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setConfirming(false)}>
            Cancel
          </Button>
        </>
      ) : (
        <Button type="button" size="sm" variant="ghost" onClick={() => setConfirming(true)}>
          Reset 2FA
        </Button>
      )}
      {reset.isError && (
        <span role="alert" className="text-xs text-destructive">
          {getErrorMessage(reset.error)}
        </span>
      )}
    </div>
  );
}
