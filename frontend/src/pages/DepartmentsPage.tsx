import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import {
  createDepartment,
  deleteDepartment,
  fetchDepartments,
  fetchInboxes,
  fetchInboxGrants,
  fetchOrgMembers,
  patchDepartment,
  putDepartmentMembers,
  putInboxGrants,
  type Department,
  type Inbox,
  type OrgMember,
} from "@/api/conversations";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";
import { InitialsAvatar } from "@/components/ui/consoleChrome";
import {
  Button,
  EmptyState,
  Input,
  MutationStatus,
  Section,
  Select,
} from "@/components/ui/primitives";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";

const ROW_LABEL =
  "text-[11px] font-semibold uppercase tracking-[0.07em] text-[hsl(var(--cx-muted))]";
const CARD = "space-y-3 rounded-xl border border-border bg-[hsl(var(--cx-surface))] p-3.5";
const LINK =
  "rounded-full px-2 py-0.5 text-xs font-medium text-primary hover:underline disabled:opacity-50";

function LineChip({ inbox }: { inbox: Inbox }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-[hsl(var(--cx-overlay)/0.55)] px-2.5 py-1 text-xs text-foreground">
      <i aria-hidden="true" className="h-2 w-2 rounded-full" style={{ background: inbox.color }} />
      {inbox.name}
      <small className="text-muted-foreground">{formatPhone(inbox.e164)}</small>
    </span>
  );
}

/** Adds a department grant (role member) to a line, keeping its other grants. */
async function addDepartmentToLine(
  api: ReturnType<typeof useAuth>["api"],
  inboxId: string,
  departmentId: string,
) {
  const grants = await fetchInboxGrants(api, inboxId);
  return putInboxGrants(api, inboxId, [
    ...grants.filter((g) => !(g.grantee_type === "department" && g.grantee_id === departmentId)),
    { grantee_type: "department", grantee_id: departmentId, role: "member" },
  ]);
}

function DepartmentCard({
  department,
  members,
  inboxes,
  canManage,
}: {
  department: Department;
  members: OrgMember[];
  inboxes: Inbox[];
  canManage: boolean;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["departments"] });
    void queryClient.invalidateQueries({ queryKey: ["inboxes"] });
  };

  const [menuOpen, setMenuOpen] = React.useState(false);
  const [renaming, setRenaming] = React.useState(false);
  const [name, setName] = React.useState(department.name);
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [adding, setAdding] = React.useState<"person" | "line" | null>(null);
  const [pick, setPick] = React.useState("");

  const patchMutation = useMutation({
    mutationFn: (data: { name?: string; is_active?: boolean }) =>
      patchDepartment(api, department.id, data),
    onSuccess: () => {
      setRenaming(false);
      refresh();
    },
  });
  const membersMutation = useMutation({
    mutationFn: (ids: string[]) => putDepartmentMembers(api, department.id, ids),
    onSuccess: () => {
      setAdding(null);
      setPick("");
      refresh();
    },
  });
  const lineMutation = useMutation({
    mutationFn: (inboxId: string) => addDepartmentToLine(api, inboxId, department.id),
    onSuccess: () => {
      setAdding(null);
      setPick("");
      refresh();
    },
  });
  const deleteMutation = useMutation({
    mutationFn: () => deleteDepartment(api, department.id),
    onSuccess: refresh,
  });

  const busy =
    patchMutation.isPending ||
    membersMutation.isPending ||
    lineMutation.isPending ||
    deleteMutation.isPending;

  const people = department.member_user_ids.map(
    (id) => members.find((m) => m.user_id === id) ?? { user_id: id, full_name: id },
  );
  const addablePeople = members.filter((m) => !department.member_user_ids.includes(m.user_id));
  const lines = inboxes.filter((i) => i.departments?.some((d) => d.id === department.id));
  const addableLines = inboxes.filter((i) => !i.departments?.some((d) => d.id === department.id));

  return (
    <div className={cn(CARD, !department.is_active && "opacity-70")}>
      <div className="flex flex-wrap items-center gap-2.5">
        <InitialsAvatar size="row" seed={department.id} name={department.name} />
        {renaming ? (
          <>
            <Input
              aria-label={`Department name ${department.name}`}
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="h-9 w-auto min-w-[10rem] flex-1"
            />
            <Button
              type="button"
              className="rounded-full px-4"
              disabled={busy || !name.trim()}
              onClick={() => patchMutation.mutate({ name: name.trim() })}
            >
              Save name
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="rounded-full px-3"
              onClick={() => {
                setRenaming(false);
                setName(department.name);
              }}
            >
              Cancel
            </Button>
          </>
        ) : (
          <b className="text-sm text-foreground">{department.name}</b>
        )}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            role="switch"
            aria-checked={department.is_active}
            aria-label={`Active ${department.name}`}
            disabled={!canManage || busy}
            onClick={() => patchMutation.mutate({ is_active: !department.is_active })}
            className={cn(
              "relative h-5 w-9 rounded-full transition-colors disabled:opacity-60",
              department.is_active ? "bg-primary" : "bg-[hsl(var(--cx-overlay))]",
            )}
          >
            <span
              className={cn(
                "absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all",
                department.is_active ? "left-[18px]" : "left-0.5",
              )}
            />
          </button>
          {canManage ? (
            <div className="relative">
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`More for ${department.name}`}
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((v) => !v)}
              >
                ···
              </Button>
              {menuOpen ? (
                <div className="absolute right-0 z-10 mt-1 flex w-32 flex-col rounded-lg border border-border bg-[hsl(var(--cx-surface))] p-1 shadow-md">
                  <button
                    type="button"
                    className="rounded-md px-2.5 py-1.5 text-left text-xs hover:bg-muted"
                    onClick={() => {
                      setMenuOpen(false);
                      setRenaming(true);
                    }}
                  >
                    Rename
                  </button>
                  <button
                    type="button"
                    className="rounded-md px-2.5 py-1.5 text-left text-xs text-destructive hover:bg-muted"
                    onClick={() => {
                      setMenuOpen(false);
                      setConfirmDelete(true);
                    }}
                  >
                    Delete
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>

      {confirmDelete ? (
        <div
          role="alertdialog"
          aria-label={`Delete ${department.name}?`}
          className="flex flex-wrap items-center gap-3 rounded-lg bg-[hsl(var(--cx-overlay)/0.55)] px-3 py-2 text-xs"
        >
          <span className="flex-1">
            Delete {department.name}? Its people and lines stay; the department goes away.
          </span>
          <Button
            type="button"
            variant="destructive"
            className="rounded-full px-4"
            disabled={busy}
            onClick={() => deleteMutation.mutate()}
          >
            Delete department
          </Button>
          <Button
            type="button"
            variant="ghost"
            className="rounded-full px-3"
            onClick={() => setConfirmDelete(false)}
          >
            Keep
          </Button>
        </div>
      ) : null}

      <div className="space-y-1.5">
        <p className={ROW_LABEL}>People</p>
        <div className="flex flex-wrap items-center gap-2">
          {people.length === 0 ? (
            <span className="text-xs text-muted-foreground">No one yet</span>
          ) : null}
          {people.map((p) => (
            <span
              key={p.user_id}
              className="inline-flex items-center gap-1.5 rounded-full bg-[hsl(var(--cx-overlay)/0.55)] py-0.5 pl-0.5 pr-2.5 text-xs"
            >
              <InitialsAvatar
                size="row"
                seed={p.user_id}
                name={p.full_name}
                className="!h-6 !w-6 !text-[9px]"
              />
              {p.full_name}
              {canManage ? (
                <button
                  type="button"
                  aria-label={`Remove ${p.full_name} from ${department.name}`}
                  disabled={busy}
                  className="text-muted-foreground hover:text-destructive"
                  onClick={() =>
                    membersMutation.mutate(
                      department.member_user_ids.filter((id) => id !== p.user_id),
                    )
                  }
                >
                  ×
                </button>
              ) : null}
            </span>
          ))}
          {canManage && adding !== "person" ? (
            <button
              type="button"
              className={LINK}
              aria-label={`Add person to ${department.name}`}
              onClick={() => {
                setAdding("person");
                setPick("");
              }}
            >
              + Add
            </button>
          ) : null}
        </div>
        {canManage && adding === "person" ? (
          <div className="flex flex-wrap items-center gap-2">
            <Select
              aria-label={`Person to add to ${department.name}`}
              value={pick}
              onChange={(e) => setPick(e.target.value)}
              className="h-9 min-w-40 px-2.5 text-xs"
            >
              <option value="">Select a person…</option>
              {addablePeople.map((m) => (
                <option key={m.user_id} value={m.user_id}>
                  {m.full_name}
                </option>
              ))}
            </Select>
            <Button
              type="button"
              className="rounded-full px-4"
              disabled={!pick || busy}
              onClick={() => membersMutation.mutate([...department.member_user_ids, pick])}
            >
              Add person
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="rounded-full px-3"
              onClick={() => setAdding(null)}
            >
              Cancel
            </Button>
          </div>
        ) : null}
      </div>

      <div className="space-y-1.5">
        <p className={ROW_LABEL}>Lines</p>
        <div className="flex flex-wrap items-center gap-2">
          {lines.length === 0 ? (
            <span className="text-xs text-muted-foreground">No lines yet</span>
          ) : null}
          {lines.map((i) => (
            <LineChip key={i.id} inbox={i} />
          ))}
          {canManage && adding !== "line" ? (
            <button
              type="button"
              className={LINK}
              aria-label={`Add line to ${department.name}`}
              onClick={() => {
                setAdding("line");
                setPick("");
              }}
            >
              + Add line
            </button>
          ) : null}
        </div>
        {canManage && adding === "line" ? (
          <div className="flex flex-wrap items-center gap-2">
            <Select
              aria-label={`Line to add to ${department.name}`}
              value={pick}
              onChange={(e) => setPick(e.target.value)}
              className="h-9 min-w-40 px-2.5 text-xs"
            >
              <option value="">Select a line…</option>
              {addableLines.map((i) => (
                <option key={i.id} value={i.id}>
                  {i.name} · {formatPhone(i.e164)}
                </option>
              ))}
            </Select>
            <Button
              type="button"
              className="rounded-full px-4"
              disabled={!pick || busy}
              onClick={() => lineMutation.mutate(pick)}
            >
              Add to department
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="rounded-full px-3"
              onClick={() => setAdding(null)}
            >
              Cancel
            </Button>
          </div>
        ) : null}
      </div>

      <div className="flex items-center gap-2 border-t border-border pt-2.5 text-xs text-muted-foreground">
        Members can <b className="text-foreground">send &amp; call</b>
        <MutationStatus
          pending={busy}
          error={
            patchMutation.error ??
            membersMutation.error ??
            lineMutation.error ??
            deleteMutation.error ??
            undefined
          }
          className="ml-auto text-[10px]"
        />
      </div>
    </div>
  );
}

function UnassignedCard({
  inboxes,
  departments,
  canManage,
}: {
  inboxes: Inbox[];
  departments: Department[];
  canManage: boolean;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const addMutation = useMutation({
    mutationFn: ({ inboxId, departmentId }: { inboxId: string; departmentId: string }) =>
      addDepartmentToLine(api, inboxId, departmentId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["inboxes"] }),
  });

  return (
    <div className={cn(CARD, "border-dashed")}>
      <b className="text-sm text-foreground">Lines with no department</b>
      {inboxes.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          None. New numbers land here until you give them a department.
        </p>
      ) : (
        <ul className="space-y-2">
          {inboxes.map((inbox) => (
            <li key={inbox.id} className="flex flex-wrap items-center gap-2">
              <LineChip inbox={inbox} />
              {canManage && departments.length > 0 ? (
                <Select
                  aria-label={`Add ${inbox.name} to a department`}
                  value=""
                  disabled={addMutation.isPending}
                  onChange={(e) => {
                    if (e.target.value)
                      addMutation.mutate({ inboxId: inbox.id, departmentId: e.target.value });
                  }}
                  className="ml-auto h-8 w-auto px-2 text-xs"
                >
                  <option value="">Add to department…</option>
                  {departments.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </Select>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      <MutationStatus
        pending={addMutation.isPending}
        error={addMutation.isError ? addMutation.error : undefined}
        className="text-[10px]"
      />
    </div>
  );
}

export function DepartmentsPage() {
  const { theme } = useSurfaceTheme();
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const departmentsQuery = useQuery({
    queryKey: ["departments"],
    queryFn: () => fetchDepartments(api),
  });
  const inboxesQuery = useQuery({ queryKey: ["inboxes"], queryFn: () => fetchInboxes(api) });
  const membersQuery = useQuery({ queryKey: ["org-members"], queryFn: () => fetchOrgMembers(api) });
  const [newName, setNewName] = React.useState("");
  const [creating, setCreating] = React.useState(false);

  const createMutation = useMutation({
    mutationFn: (name: string) => createDepartment(api, { name }),
    onSuccess: () => {
      setNewName("");
      setCreating(false);
      void queryClient.invalidateQueries({ queryKey: ["departments"] });
    },
  });

  const wrap = (children: React.ReactNode) => (
    <div
      className={cn(
        surfaceThemeClass(theme),
        "mx-auto max-w-5xl space-y-5 bg-background p-6 text-foreground",
      )}
    >
      {children}
    </div>
  );

  if (departmentsQuery.isLoading || inboxesQuery.isLoading || membersQuery.isLoading) {
    return wrap(<p className="text-sm text-muted-foreground">Loading departments…</p>);
  }
  const error = departmentsQuery.error ?? inboxesQuery.error ?? membersQuery.error;
  if (error) {
    return wrap(
      <p role="alert" className="text-sm text-destructive">
        Couldn&rsquo;t load departments: {(error as Error).message}
      </p>,
    );
  }

  const departments = departmentsQuery.data ?? [];
  const inboxes = inboxesQuery.data ?? [];
  const members = membersQuery.data ?? [];
  // Same manage check InboxSettingsPage uses: an admin role on at least one inbox.
  const canManage = inboxes.some((i) => i.my_role === "admin");
  const unassigned = inboxes.filter((i) => !i.departments || i.departments.length === 0);

  return wrap(
    <Section
      title="Departments"
      description="A department is a team and the lines it works. Add a person and they get every line in it."
      actions={
        canManage ? (
          <Button type="button" className="rounded-full px-4" onClick={() => setCreating((v) => !v)}>
            + New department
          </Button>
        ) : undefined
      }
    >
      {!canManage ? (
        <p className="text-xs text-muted-foreground">
          You can view departments but not change them.
        </p>
      ) : null}
      {creating && canManage ? (
        <form
          className="flex items-center gap-3 rounded-xl border border-border bg-[hsl(var(--cx-surface))] p-3.5"
          onSubmit={(e) => {
            e.preventDefault();
            if (newName.trim()) createMutation.mutate(newName.trim());
          }}
        >
          <Input
            aria-label="New department name"
            placeholder="Department name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            className="h-9 w-auto flex-1 px-3 text-sm"
          />
          <Button
            type="submit"
            className="rounded-full px-5"
            disabled={!newName.trim() || createMutation.isPending}
          >
            Create
          </Button>
          <MutationStatus
            pending={createMutation.isPending}
            error={createMutation.isError ? createMutation.error : undefined}
            className="text-[10px]"
          />
        </form>
      ) : null}
      {departments.length === 0 ? (
        <EmptyState
          title="No departments yet."
          description="Create one to group people and lines."
        />
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {departments.map((d) => (
            <DepartmentCard
              key={d.id}
              department={d}
              members={members}
              inboxes={inboxes}
              canManage={canManage}
            />
          ))}
        </div>
      )}
      <UnassignedCard inboxes={unassigned} departments={departments} canManage={canManage} />
    </Section>,
  );
}
