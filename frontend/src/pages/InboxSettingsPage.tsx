import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Save, Trash2, UserRound, Building2 } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import {
  createDepartment,
  deleteDepartment,
  fetchDepartments,
  fetchInboxes,
  fetchInboxGrants,
  fetchOrgMembers,
  patchDepartment,
  patchInbox,
  putDepartmentMembers,
  putInboxGrants,
  type Department,
  type Inbox,
  type InboxGrant,
  type OrgMember,
} from "@/api/conversations";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

// F17: this is a plain filter, not a hook (it calls no hooks itself) - the `use` prefix
// was misleading.
function adminInboxes(inboxes: Inbox[]): Inbox[] {
  return inboxes.filter((inbox) => inbox.my_role === "admin");
}

/** Small inline pending/error readout, matching ContactPanel's EditableField pattern -
 * F14: every bare async mutation on this page now goes through useMutation so a failure
 * is visible instead of silently swallowed. */
function MutationStatus({ mutation }: { mutation: { isPending: boolean; isError: boolean; error: unknown } }) {
  if (mutation.isPending) {
    return (
      <span className="flex items-center gap-1 text-[10px] text-neutral-500">
        <Loader2 className="h-3 w-3 animate-spin" /> Saving…
      </span>
    );
  }
  if (mutation.isError) {
    return (
      <span role="alert" className="text-[10px] text-red-400">
        {(mutation.error as Error).message}
      </span>
    );
  }
  return null;
}

function DepartmentSection() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const departmentsQuery = useQuery({
    queryKey: ["departments"],
    queryFn: () => fetchDepartments(api),
  });
  const membersQuery = useQuery({
    queryKey: ["org-members"],
    queryFn: () => fetchOrgMembers(api),
  });

  const [newName, setNewName] = React.useState("");

  const createMutation = useMutation({
    mutationFn: (name: string) => createDepartment(api, { name }),
    onSuccess: () => {
      setNewName("");
      void queryClient.invalidateQueries({ queryKey: ["departments"] });
    },
  });

  function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    createMutation.mutate(newName.trim());
  }

  const departments = departmentsQuery.data ?? [];
  const members = membersQuery.data ?? [];

  return (
    <section className="space-y-4">
      <h2 className="text-base font-semibold text-neutral-50">Departments</h2>

      <form className="flex items-center gap-2" onSubmit={handleCreate}>
        <input
          aria-label="New department name"
          placeholder="Department name"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          className="h-9 flex-1 rounded-md border border-neutral-700 bg-neutral-950 px-2 text-sm text-neutral-100 placeholder:text-neutral-500"
        />
        <button
          type="submit"
          disabled={!newName.trim() || createMutation.isPending}
          className="rounded-md bg-neutral-100 px-3 text-sm font-medium text-neutral-900 disabled:opacity-50"
        >
          Create
        </button>
        <MutationStatus mutation={createMutation} />
      </form>

      {departments.length === 0 ? (
        <p className="text-sm text-neutral-400">No departments yet.</p>
      ) : (
        <div className="space-y-3">
          {departments.map((department) => (
            <DepartmentRow key={department.id} department={department} members={members} />
          ))}
        </div>
      )}
    </section>
  );
}

function DepartmentRow({
  department,
  members,
}: {
  department: Department;
  members: OrgMember[];
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [name, setName] = React.useState(department.name);
  const [selectedIds, setSelectedIds] = React.useState<string[]>(department.member_user_ids);

  // F15: key on the joined ids, not the array reference - a background refetch that
  // returns the SAME membership (new array object, same content) must not clobber
  // whatever the admin is mid-way through picking in the <select multiple>.
  const memberIdsKey = department.member_user_ids.join(",");
  React.useEffect(() => {
    setName(department.name);
    setSelectedIds(department.member_user_ids);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [department.name, memberIdsKey]);

  const patchMutation = useMutation({
    mutationFn: (data: { name?: string; is_active?: boolean }) =>
      patchDepartment(api, department.id, data),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["departments"] }),
  });
  const membersMutation = useMutation({
    mutationFn: (userIds: string[]) => putDepartmentMembers(api, department.id, userIds),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["departments"] }),
  });
  const deleteMutation = useMutation({
    mutationFn: () => deleteDepartment(api, department.id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["departments"] }),
  });

  const busy = patchMutation.isPending || membersMutation.isPending || deleteMutation.isPending;

  return (
    <div
      className={cn(
        "rounded-md border border-neutral-800 bg-neutral-900 p-3",
        !department.is_active && "opacity-60",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <input
          aria-label={`Department name ${department.name}`}
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="h-8 rounded-md border border-neutral-700 bg-neutral-950 px-2 text-sm text-neutral-100"
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => patchMutation.mutate({ name })}
          className="rounded-md px-3 py-1.5 text-xs font-medium text-neutral-900 bg-neutral-100 disabled:opacity-50"
        >
          Rename
        </button>
        <button
          type="button"
          disabled={busy}
          aria-pressed={department.is_active}
          onClick={() => patchMutation.mutate({ is_active: !department.is_active })}
          className="rounded-md border border-neutral-700 px-3 py-1.5 text-xs text-neutral-300 hover:bg-neutral-800 disabled:opacity-50"
        >
          {department.is_active ? "Deactivate" : "Activate"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => deleteMutation.mutate()}
          aria-label={`Delete ${department.name}`}
          className="ml-auto rounded-md p-2 text-neutral-400 hover:bg-neutral-800 hover:text-red-400 disabled:opacity-50"
        >
          <Trash2 className="h-4 w-4" />
        </button>
        <MutationStatus mutation={patchMutation} />
        <MutationStatus mutation={deleteMutation} />
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-xs text-neutral-500">Members</span>
        <select
          aria-label={`Members for ${department.name}`}
          multiple
          value={selectedIds}
          onChange={(e) => {
            const values = Array.from(e.currentTarget.selectedOptions).map((o) => o.value);
            setSelectedIds(values);
          }}
          className="h-24 w-full min-w-0 rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1 text-xs text-neutral-100"
        >
          {members.map((member) => (
            <option key={member.user_id} value={member.user_id}>
              {member.full_name} ({member.email})
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={busy}
          onClick={() => membersMutation.mutate(selectedIds)}
          className="rounded-md bg-neutral-100 px-3 py-1.5 text-xs font-medium text-neutral-900 disabled:opacity-50"
        >
          Save members
        </button>
        <MutationStatus mutation={membersMutation} />
      </div>
    </div>
  );
}

function InboxGrantEditor({ inbox }: { inbox: Inbox }) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const grantsQuery = useQuery({
    queryKey: ["inbox-grants", inbox.id],
    queryFn: () => fetchInboxGrants(api, inbox.id),
  });
  const departmentsQuery = useQuery({
    queryKey: ["departments"],
    queryFn: () => fetchDepartments(api),
  });
  const membersQuery = useQuery({
    queryKey: ["org-members"],
    queryFn: () => fetchOrgMembers(api),
  });

  const [draftGrants, setDraftGrants] = React.useState<InboxGrant[]>([]);
  const [granteeType, setGranteeType] = React.useState<"department" | "user">("user");
  const [granteeId, setGranteeId] = React.useState("");
  const [grantRole, setGrantRole] = React.useState<"member" | "viewer">("member");

  // F15: key on the joined grantee ids, not the array/object reference - a background
  // refetch returning the same set of grants must not overwrite an in-progress edit
  // (add/remove) the admin hasn't saved yet.
  const grantsKey = (grantsQuery.data ?? [])
    .map((g) => `${g.grantee_type}:${g.grantee_id}`)
    .sort()
    .join(",");
  React.useEffect(() => {
    if (grantsQuery.data) setDraftGrants(grantsQuery.data);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grantsKey]);

  const saveGrantsMutation = useMutation({
    mutationFn: (grants: InboxGrant[]) => putInboxGrants(api, inbox.id, grants),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["inbox-grants", inbox.id] }),
  });

  if (grantsQuery.isLoading) {
    return <p className="text-xs text-neutral-400">Loading grants…</p>;
  }

  const departments = departmentsQuery.data ?? [];
  const members = membersQuery.data ?? [];

  function addGrant() {
    if (!granteeId) return;
    setDraftGrants((prev) => [
      ...prev.filter(
        (grant) =>
          !(grant.grantee_type === granteeType && grant.grantee_id === granteeId),
      ),
      { grantee_type: granteeType, grantee_id: granteeId, role: grantRole },
    ]);
    setGranteeId("");
  }

  function removeGrant(granteeType: "department" | "user", granteeId: string) {
    setDraftGrants((prev) =>
      prev.filter(
        (grant) => !(grant.grantee_type === granteeType && grant.grantee_id === granteeId),
      ),
    );
  }

  return (
    <div className="mt-3 rounded-md border border-neutral-800 bg-neutral-950 p-3">
      <p className="text-xs font-medium text-neutral-300">Grants</p>

      <div className="mt-2 space-y-1">
        {draftGrants.map((grant) => {
          const label =
            grant.grantee_type === "department"
              ? departments.find((d) => d.id === grant.grantee_id)?.name ?? grant.grantee_id
              : members.find((m) => m.user_id === grant.grantee_id)?.full_name ??
                grant.grantee_id;
          return (
            <div
              key={`${grant.grantee_type}-${grant.grantee_id}`}
              className="flex items-center gap-2 rounded bg-neutral-900 px-2 py-1 text-xs text-neutral-200"
            >
              {grant.grantee_type === "department" ? (
                <Building2 className="h-3.5 w-3.5 text-neutral-500" />
              ) : (
                <UserRound className="h-3.5 w-3.5 text-neutral-500" />
              )}
              <span className="flex-1 truncate">{label}</span>
              <span className="text-neutral-500">{grant.role}</span>
              <button
                type="button"
                onClick={() => removeGrant(grant.grantee_type, grant.grantee_id)}
                aria-label={`Remove grant ${label}`}
                className="rounded p-1 text-neutral-400 hover:text-red-400"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          );
        })}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <select
          aria-label="Grantee type"
          value={granteeType}
          onChange={(e) => setGranteeType(e.target.value as "department" | "user")}
          className="h-8 rounded-md border border-neutral-700 bg-neutral-950 px-2 text-xs text-neutral-100"
        >
          <option value="user">User</option>
          <option value="department">Department</option>
        </select>
        <select
          aria-label="Grantee"
          value={granteeId}
          onChange={(e) => setGranteeId(e.target.value)}
          className="h-8 min-w-40 rounded-md border border-neutral-700 bg-neutral-950 px-2 text-xs text-neutral-100"
        >
          <option value="">Select…</option>
          {granteeType === "department"
            ? departments.map((department) => (
                <option key={department.id} value={department.id}>
                  {department.name}
                </option>
              ))
            : members.map((member) => (
                <option key={member.user_id} value={member.user_id}>
                  {member.full_name}
                </option>
              ))}
        </select>
        <select
          aria-label="Grant role"
          value={grantRole}
          onChange={(e) => setGrantRole(e.target.value as "member" | "viewer")}
          className="h-8 rounded-md border border-neutral-700 bg-neutral-950 px-2 text-xs text-neutral-100"
        >
          <option value="member">Member</option>
          <option value="viewer">Viewer</option>
        </select>
        <button
          type="button"
          onClick={addGrant}
          disabled={!granteeId}
          className="rounded-md bg-neutral-100 px-3 py-1.5 text-xs font-medium text-neutral-900 disabled:opacity-50"
        >
          Add grant
        </button>
        <button
          type="button"
          onClick={() => saveGrantsMutation.mutate(draftGrants)}
          disabled={saveGrantsMutation.isPending}
          className="inline-flex items-center gap-1 rounded-md bg-neutral-100 px-3 py-1.5 text-xs font-medium text-neutral-900 disabled:opacity-50"
        >
          <Save className="h-3.5 w-3.5" /> Save grants
        </button>
        <MutationStatus mutation={saveGrantsMutation} />
      </div>
    </div>
  );
}

function InboxesTable({ inboxes }: { inboxes: Inbox[] }) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [drafts, setDrafts] = React.useState<Record<string, { name: string; color: string }>>({});

  // F15: key on the joined inbox ids, not the array reference - a background refetch of
  // the SAME inboxes must not reset name/color drafts the admin is still editing.
  const inboxIdsKey = inboxes.map((inbox) => inbox.id).join(",");
  React.useEffect(() => {
    const next: Record<string, { name: string; color: string }> = {};
    inboxes.forEach((inbox) => {
      next[inbox.id] = { name: inbox.name, color: inbox.color };
    });
    setDrafts(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inboxIdsKey]);

  const saveMutation = useMutation({
    mutationFn: (vars: { id: string; name: string; color: string }) =>
      patchInbox(api, vars.id, { name: vars.name, color: vars.color }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["inboxes"] }),
  });

  return (
    <div className="overflow-x-auto rounded-md border border-neutral-800">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-neutral-800 text-left text-xs text-neutral-500">
            <th className="px-3 py-2 font-medium">Name</th>
            <th className="px-3 py-2 font-medium">Color</th>
            <th className="px-3 py-2 font-medium">Number</th>
            <th className="px-3 py-2 font-medium">Your role</th>
            <th className="px-3 py-2 font-medium">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-800">
          {inboxes.map((inbox) => {
            const draft = drafts[inbox.id] ?? { name: inbox.name, color: inbox.color };
            const rowSaving =
              saveMutation.isPending && saveMutation.variables?.id === inbox.id;
            const rowError =
              saveMutation.isError && saveMutation.variables?.id === inbox.id;
            return (
              <React.Fragment key={inbox.id}>
                <tr className="bg-neutral-900">
                  <td className="px-3 py-2">
                    <input
                      aria-label={`Inbox name ${inbox.name}`}
                      value={draft.name}
                      onChange={(e) =>
                        setDrafts((prev) => ({
                          ...prev,
                          [inbox.id]: { ...draft, name: e.target.value },
                        }))
                      }
                      className="h-8 w-full rounded-md border border-neutral-700 bg-neutral-950 px-2 text-xs text-neutral-100"
                    />
                  </td>
                  <td className="px-3 py-2">
                    <input
                      aria-label={`Inbox color ${inbox.name}`}
                      type="color"
                      value={draft.color}
                      onChange={(e) =>
                        setDrafts((prev) => ({
                          ...prev,
                          [inbox.id]: { ...draft, color: e.target.value },
                        }))
                      }
                      className="h-8 w-14 rounded-md border border-neutral-700 bg-neutral-950"
                    />
                  </td>
                  <td className="px-3 py-2 text-xs text-neutral-400">
                    {formatPhone(inbox.e164)}
                  </td>
                  <td className="px-3 py-2 text-xs text-neutral-400">{inbox.my_role}</td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        disabled={rowSaving}
                        onClick={() =>
                          saveMutation.mutate({ id: inbox.id, name: draft.name, color: draft.color })
                        }
                        className="rounded-md bg-neutral-100 px-3 py-1.5 text-xs font-medium text-neutral-900 disabled:opacity-50"
                      >
                        {rowSaving ? "Saving…" : "Save"}
                      </button>
                      {rowError && (
                        <span role="alert" className="text-[10px] text-red-400">
                          {(saveMutation.error as Error).message}
                        </span>
                      )}
                    </div>
                  </td>
                </tr>
                <tr className="border-b border-neutral-800 bg-neutral-950">
                  <td colSpan={5} className="px-3 py-2">
                    <InboxGrantEditor inbox={inbox} />
                  </td>
                </tr>
              </React.Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function InboxSettingsPage() {
  const { api } = useAuth();
  const inboxesQuery = useQuery({
    queryKey: ["inboxes"],
    queryFn: () => fetchInboxes(api),
  });

  const inboxes = inboxesQuery.data ?? [];
  const admin = adminInboxes(inboxes);

  if (inboxesQuery.isLoading) {
    return <p className="p-6 text-sm text-neutral-400">Loading settings…</p>;
  }

  // F16: a failed fetch must never render as "Admins only" - that reads as a permissions
  // denial when it might just be a network/server error.
  if (inboxesQuery.error) {
    return (
      <div className="flex h-full items-center justify-center bg-neutral-950 text-sm text-red-400">
        <p role="alert">
          Couldn&rsquo;t load inbox settings: {(inboxesQuery.error as Error).message}
        </p>
      </div>
    );
  }

  if (admin.length === 0) {
    return (
      <div className="flex h-full items-center justify-center bg-neutral-950 text-sm text-neutral-400">
        Admins only
      </div>
    );
  }

  return (
    <div className="dark mx-auto max-w-5xl space-y-8 bg-neutral-950 p-6 text-neutral-100">
      <h1 className="text-lg font-semibold text-neutral-50">Inbox settings</h1>
      <DepartmentSection />
      <section className="space-y-4">
        <h2 className="text-base font-semibold text-neutral-50">Inboxes</h2>
        <InboxesTable inboxes={admin} />
      </section>
    </div>
  );
}
