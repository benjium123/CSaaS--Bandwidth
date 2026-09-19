import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, Trash2, UserRound, Building2 } from "lucide-react";
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
import { formatPhone, initialsOf } from "@/lib/format";
import { cn } from "@/lib/utils";
import { InitialsAvatar } from "@/components/ui/consoleChrome";
import {
  Button,
  Card,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
} from "@/components/ui/primitives";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";

/* ── The console's shape, from docs/design/console-reference.html ─────────────────────── */

/** What a column header used to say, said once per field instead. */
const FIELD_LABEL =
  "block text-[11px] font-semibold uppercase tracking-[0.07em] text-[hsl(var(--cx-muted))]";

/** The reference's line-rail `.av`: a 50% disc on a 145deg gradient, and for a LINE the
 * hue is user data rather than palette - `.cx-line-avatar` reads it from `--cx-line-av`.
 * Fed from the DRAFT colour, so the disc repaints as the admin drags the colour picker and
 * shows what saving would actually do. Decorative: the name is in the field beside it. */
function LineAvatar({ inbox, draftColor }: { inbox: Inbox; draftColor: string }) {
  return (
    <span
      aria-hidden="true"
      style={{ "--cx-line-av": draftColor } as React.CSSProperties}
      className="cx-line-avatar mb-0.5 inline-grid h-10 w-10 flex-none place-items-center text-[12px] font-semibold text-[hsl(var(--cx-on-acc))]"
    >
      {initialsOf(inbox.name)}
    </span>
  );
}

// F17: this is a plain filter, not a hook (it calls no hooks itself) - the `use` prefix
// was misleading.
function adminInboxes(inboxes: Inbox[]): Inbox[] {
  return inboxes.filter((inbox) => inbox.my_role === "admin");
}

type InboxDraft = {
  name: string;
  color: string;
  firstReplyMinutes: string;
  resolveMinutes: string;
};

type InboxSaveVars = {
  id: string;
  name: string;
  color: string;
  sla_first_response_minutes?: number;
  sla_resolution_minutes?: number;
  clear_sla_first_response?: boolean;
  clear_sla_resolution?: boolean;
};

function seedDraft(inbox: Inbox): InboxDraft {
  return {
    name: inbox.name,
    color: inbox.color,
    firstReplyMinutes:
      inbox.sla_first_response_minutes == null
        ? ""
        : String(inbox.sla_first_response_minutes),
    resolveMinutes:
      inbox.sla_resolution_minutes == null ? "" : String(inbox.sla_resolution_minutes),
  };
}

/**
 * Reply times are held as text in state because a number input is still a text field to
 * React. Parsing on save keeps a user from clearing "15" and then having "15" jump back
 * under their "5".
 */
function parseMinutesField(value: string): number | "clear" | "invalid" {
  const trimmed = value.trim();
  if (trimmed === "") return "clear";
  const parsed = Number(trimmed);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 44640) return "invalid";
  return parsed;
}

/** Small inline pending/error readout, matching ContactPanel's EditableField pattern -
 * F14: every bare async mutation on this page now goes through useMutation so a failure
 * is visible instead of silently swallowed. */
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
    <Section title="Departments" description="Create and organize departments.">
      <form
        className="flex items-center gap-3 rounded-xl border border-border bg-[hsl(var(--cx-surface))] p-3.5"
        onSubmit={handleCreate}
      >
        <Input
          aria-label="New department name"
          placeholder="Department name"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          className="h-9 w-auto flex-1 px-3 text-sm"
        />
        {/* The reference's `.send`: a primary action is a pill. */}
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

      {departments.length === 0 ? (
        <EmptyState
          title="No departments yet."
          description="Create one to group team members."
        />
      ) : (
        <div className="space-y-3">
          {departments.map((department) => (
            <DepartmentRow key={department.id} department={department} members={members} />
          ))}
        </div>
      )}
    </Section>
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
    <Card
      className={cn(
        "rounded-xl border-border bg-[hsl(var(--cx-surface))] p-3.5",
        !department.is_active && "opacity-60",
      )}
    >
      <div className="flex flex-wrap items-center gap-3">
        {/* This disc's hue is NOT user data: a department has no colour of its own, so it
            takes one of the seven palette hues, hashed off its immutable id. */}
        <InitialsAvatar size="row" seed={department.id} name={department.name} />
        <Input
          aria-label={`Department name ${department.name}`}
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-auto min-w-[12rem] flex-1"
        />
        <Button
          type="button"
          className="rounded-full px-5"
          disabled={busy}
          onClick={() => patchMutation.mutate({ name })}
        >
          Rename
        </Button>
        <Button
          type="button"
          className="rounded-full px-5"
          disabled={busy}
          aria-pressed={department.is_active}
          onClick={() => patchMutation.mutate({ is_active: !department.is_active })}
          variant="outline"
        >
          {department.is_active ? "Deactivate" : "Activate"}
        </Button>
        <Button
          type="button"
          disabled={busy}
          onClick={() => deleteMutation.mutate()}
          aria-label={`Delete ${department.name}`}
          variant="ghost"
          size="icon"
          className="ml-auto h-9 w-9 text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="h-4 w-4" />
        </Button>
        <MutationStatus
          pending={patchMutation.isPending}
          error={patchMutation.isError ? patchMutation.error : undefined}
          className="text-[10px]"
        />
        <MutationStatus
          pending={deleteMutation.isPending}
          error={deleteMutation.isError ? deleteMutation.error : undefined}
          className="text-[10px]"
        />
      </div>

      <div className="mt-3.5 flex flex-wrap items-center gap-3 rounded-lg bg-[hsl(var(--cx-overlay)/0.55)] p-3">
        <span className={FIELD_LABEL}>Members</span>
        <Select
          aria-label={`Members for ${department.name}`}
          multiple
          value={selectedIds}
          onChange={(e) => {
            const values = Array.from(e.currentTarget.selectedOptions).map((o) => o.value);
            setSelectedIds(values);
          }}
          className="h-24 w-full min-w-0 px-2.5 py-1.5 text-xs"
        >
          {members.map((member) => (
            <option key={member.user_id} value={member.user_id}>
              {member.full_name} ({member.email})
            </option>
          ))}
        </Select>
        <Button
          type="button"
          className="rounded-full px-5"
          disabled={busy}
          onClick={() => membersMutation.mutate(selectedIds)}
        >
          Save members
        </Button>
        <MutationStatus
          pending={membersMutation.isPending}
          error={membersMutation.isError ? membersMutation.error : undefined}
          className="text-[10px]"
        />
      </div>
    </Card>
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
    return <p className="text-xs text-muted-foreground">Loading grants…</p>;
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
    <Card className="rounded-lg border-transparent bg-[hsl(var(--cx-overlay)/0.55)] p-3.5">
      <p className={FIELD_LABEL}>Grants</p>

      <div className="mt-2.5 space-y-2">
        {draftGrants.map((grant) => {
          const label =
            grant.grantee_type === "department"
              ? departments.find((d) => d.id === grant.grantee_id)?.name ?? grant.grantee_id
              : members.find((m) => m.user_id === grant.grantee_id)?.full_name ??
                grant.grantee_id;
          return (
            <div
              key={`${grant.grantee_type}-${grant.grantee_id}`}
              className="flex items-center gap-2.5 rounded-md bg-[hsl(var(--cx-lift)/0.7)] px-3 py-2 text-xs text-foreground"
            >
              {grant.grantee_type === "department" ? (
                <Building2 className="h-3.5 w-3.5 text-muted-foreground" />
              ) : (
                <UserRound className="h-3.5 w-3.5 text-muted-foreground" />
              )}
              <span className="flex-1 truncate">{label}</span>
              <span className="text-muted-foreground">
                {grant.role === "member" ? "Can send & call" : "Can view"}
              </span>
              <Button
                type="button"
                onClick={() => removeGrant(grant.grantee_type, grant.grantee_id)}
                aria-label={`Remove grant ${label}`}
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted-foreground hover:text-destructive"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
          );
        })}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Select
          aria-label="Grantee type"
          value={granteeType}
          onChange={(e) => setGranteeType(e.target.value as "department" | "user")}
          className="h-9 px-2.5 text-xs"
        >
          <option value="user">User</option>
          <option value="department">Department</option>
        </Select>
        <Select
          aria-label="Grantee"
          value={granteeId}
          onChange={(e) => setGranteeId(e.target.value)}
          className="h-9 min-w-40 px-2.5 text-xs"
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
        </Select>
        <Select
          aria-label="Grant role"
          value={grantRole}
          onChange={(e) => setGrantRole(e.target.value as "member" | "viewer")}
          className="h-9 px-2.5 text-xs"
        >
          <option value="member">Can send & call</option>
          <option value="viewer">Can view</option>
        </Select>
        <Button
          type="button"
          className="rounded-full px-5"
          onClick={addGrant}
          disabled={!granteeId}
        >
          Add grant
        </Button>
        <Button
          type="button"
          className="rounded-full px-5"
          onClick={() => saveGrantsMutation.mutate(draftGrants)}
          disabled={saveGrantsMutation.isPending}
        >
          <Save className="h-3.5 w-3.5" /> Save grants
        </Button>
        <MutationStatus
          pending={saveGrantsMutation.isPending}
          error={saveGrantsMutation.isError ? saveGrantsMutation.error : undefined}
          className="text-[10px]"
        />
      </div>
    </Card>
  );
}

function InboxesTable({ inboxes }: { inboxes: Inbox[] }) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [drafts, setDrafts] = React.useState<Record<string, InboxDraft>>({});
  const [rowValidation, setRowValidation] = React.useState<Record<string, string | undefined>>(
    {},
  );

  // F15: key on the joined inbox ids, not the array reference - a background refetch of
  // the SAME inboxes must not reset name/color/reply-time drafts the admin is still editing.
  const inboxIdsKey = inboxes.map((inbox) => inbox.id).join(",");
  React.useEffect(() => {
    const next: Record<string, InboxDraft> = {};
    inboxes.forEach((inbox) => {
      next[inbox.id] = seedDraft(inbox);
    });
    setDrafts(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inboxIdsKey]);

  const saveMutation = useMutation({
    mutationFn: (vars: InboxSaveVars) => {
      const { id, ...data } = vars;
      return patchInbox(api, id, data);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["inboxes"] });
      setRowValidation({});
    },
  });

  return (
    <>
      {/* WAS a six-column table with a full-width grants row wedged underneath every line.
          It is a FORM PER INBOX, so it is now the card the reference actually draws: 14px
          corners, 11-14px padding, and the line's own colour carried by the circular
          avatar (`.cx-line-avatar`, the same treatment the reference's line rail uses).
          Nothing was dropped - name, colour, number, your role, both reply times, Save and
          the grants editor are all still here, each under the label the column header used
          to carry. */}
      <div className="space-y-3">
            {inboxes.map((inbox) => {
              const draft = drafts[inbox.id] ?? seedDraft(inbox);
              const rowSaving =
                saveMutation.isPending && saveMutation.variables?.id === inbox.id;
              const rowError =
                saveMutation.isError && saveMutation.variables?.id === inbox.id;
              const validationMessage = rowValidation[inbox.id];

              function updateDraft(patch: Partial<InboxDraft>) {
                setDrafts((prev) => {
                  const current = prev[inbox.id] ?? seedDraft(inbox);
                  return { ...prev, [inbox.id]: { ...current, ...patch } };
                });
                setRowValidation((prev) => ({ ...prev, [inbox.id]: undefined }));
              }

              function handleSave() {
                const first = parseMinutesField(draft.firstReplyMinutes);
                const resolve = parseMinutesField(draft.resolveMinutes);

                if (first === "invalid" || resolve === "invalid") {
                  setRowValidation((prev) => ({
                    ...prev,
                    [inbox.id]:
                      "Enter a whole number of minutes, or leave it blank.",
                  }));
                  return;
                }

                const vars: InboxSaveVars = {
                  id: inbox.id,
                  name: draft.name,
                  color: draft.color,
                };

                if (first === "clear") {
                  vars.clear_sla_first_response = true;
                } else {
                  vars.sla_first_response_minutes = first;
                }

                if (resolve === "clear") {
                  vars.clear_sla_resolution = true;
                } else {
                  vars.sla_resolution_minutes = resolve;
                }

                saveMutation.mutate(vars);
              }

              return (
                <div
                  key={inbox.id}
                  className="space-y-3.5 rounded-xl border border-border bg-[hsl(var(--cx-surface))] p-3.5"
                >
                  <div className="flex flex-wrap items-end gap-3">
                    <LineAvatar inbox={inbox} draftColor={draft.color} />
                    <div className="min-w-[11rem] flex-1 space-y-1.5">
                      <span className={FIELD_LABEL}>Name</span>
                      <Input
                        aria-label={`Inbox name ${inbox.name}`}
                        value={draft.name}
                        onChange={(e) => updateDraft({ name: e.target.value })}
                        className="w-full"
                      />
                    </div>
                    <div className="space-y-1.5">
                      <span className={FIELD_LABEL}>Color</span>
                      <Input
                        aria-label={`Inbox color ${inbox.name}`}
                        type="color"
                        value={draft.color}
                        onChange={(e) => updateDraft({ color: e.target.value })}
                        className="h-9 w-16 cursor-pointer p-1"
                      />
                    </div>
                    <div className="space-y-1.5">
                      <span className={FIELD_LABEL}>Number</span>
                      <p className="whitespace-nowrap py-1.5 text-[13px] text-foreground">
                        {formatPhone(inbox.e164)}
                      </p>
                    </div>
                    <div className="space-y-1.5">
                      <span className={FIELD_LABEL}>Your role</span>
                      <p className="py-1">
                        <Pill tone="info">{inbox.my_role}</Pill>
                      </p>
                    </div>
                    <div className="ml-auto flex flex-col gap-1">
                      <div className="flex items-center gap-3">
                        {/* The reference's `.send`: a primary action is a pill. */}
                        <Button
                          type="button"
                          className="rounded-full px-5"
                          disabled={rowSaving}
                          onClick={handleSave}
                        >
                          {rowSaving ? "Saving…" : "Save"}
                        </Button>
                        <MutationStatus
                          pending={false}
                          error={rowError ? saveMutation.error : undefined}
                          className="text-[10px]"
                        />
                      </div>
                      {validationMessage ? (
                        <p role="alert" className="text-xs text-destructive">
                          {validationMessage}
                        </p>
                      ) : null}
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-x-5 gap-y-3 rounded-lg bg-[hsl(var(--cx-overlay)/0.55)] px-3.5 py-3">
                    <div className="flex items-center gap-3">
                      <span className="text-xs text-muted-foreground">
                        First reply within (minutes)
                      </span>
                      <Input
                        type="number"
                        min={1}
                        max={44640}
                        aria-label={`First reply within (minutes) for ${inbox.name}`}
                        value={draft.firstReplyMinutes}
                        onChange={(e) => updateDraft({ firstReplyMinutes: e.target.value })}
                        className="h-9 w-24"
                      />
                    </div>
                    <div className="flex items-center gap-3">
                      <span className="text-xs text-muted-foreground">
                        Resolve within (minutes)
                      </span>
                      <Input
                        type="number"
                        min={1}
                        max={44640}
                        aria-label={`Resolve within (minutes) for ${inbox.name}`}
                        value={draft.resolveMinutes}
                        onChange={(e) => updateDraft({ resolveMinutes: e.target.value })}
                        className="h-9 w-24"
                      />
                    </div>
                  </div>

                  <InboxGrantEditor inbox={inbox} />
                </div>
              );
            })}
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        Leave a box blank to stop tracking that time.
      </p>
    </>
  );
}

export function InboxSettingsPage() {
  // The console follows the one stored theme preference the front door writes. See
  // src/auth/useSurfaceTheme.ts: this is a shared store, so the toggle in the sidebar moves
  // every wrapper in the console on the same commit rather than only its own.
  const { theme } = useSurfaceTheme();
  const { api } = useAuth();
  const inboxesQuery = useQuery({
    queryKey: ["inboxes"],
    queryFn: () => fetchInboxes(api),
  });

  const inboxes = inboxesQuery.data ?? [];
  const admin = adminInboxes(inboxes);

  if (inboxesQuery.isLoading) {
    return <p className="p-6 text-sm text-muted-foreground">Loading settings…</p>;
  }

  // F16: a failed fetch must never render as "Admins only" - that reads as a permissions
  // denial when it might just be a network/server error.
  if (inboxesQuery.error) {
    return (
      <div className="flex h-full items-center justify-center bg-background text-sm text-destructive">
        <p role="alert">
          Couldn&rsquo;t load inbox settings: {(inboxesQuery.error as Error).message}
        </p>
      </div>
    );
  }

  if (admin.length === 0) {
    return (
      <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
        Admins only
      </div>
    );
  }

  return (
    <div className={cn(surfaceThemeClass(theme), "mx-auto max-w-5xl space-y-7 bg-background p-6 text-foreground")}>
      <h1 className="text-[19px] font-semibold tracking-[-0.015em] text-foreground">
        Inbox settings
      </h1>
      <DepartmentSection />
      <Section title="Inboxes" description="Manage the inboxes you administer.">
        <InboxesTable inboxes={admin} />
      </Section>
    </div>
  );
}
