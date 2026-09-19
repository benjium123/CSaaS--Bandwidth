import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, Save, Trash2, UserRound } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import {
  fetchDepartments,
  fetchInboxGrants,
  fetchOrgMembers,
  putInboxGrants,
  type Department,
  type Inbox,
  type InboxGrant,
  type OrgMember,
} from "@/api/conversations";
import { formatPhone } from "@/lib/format";
import { ConsoleCard, ConsoleEmpty, SectionLabel } from "@/components/ui/consoleChrome";
import {
  Button,
  Drawer,
  MutationStatus,
  Select,
  Spinner,
} from "@/components/ui/primitives";

/** The two access levels explained once, in the same words the rows and the role select
 * use, so "Can send & call" reads as the same thing everywhere it appears. */
const EXPLANATION =
  'Choose who can use this number. "Can send & call" allows reading, texting and dialling from it; "Can view" is read-only.';

/** A role in words. Kept beside the select that offers the two options so their labels
 * cannot drift apart. */
function roleLabel(role: InboxGrant["role"]): string {
  return role === "member" ? "Can send & call" : "Can view";
}

/**
 * Per-NUMBER access, reached from the Lines rail.
 *
 * The behaviour is the existing InboxGrantEditor (pages/InboxSettingsPage.tsx) moved into
 * a Drawer: the same React Query keys so the two editors share cache, the same
 * refetch-safe draft seeding, and the same PUT semantics - grants REPLACE the whole list,
 * so Save always sends the complete draftGrants array, never a delta that would silently
 * revoke everyone the admin did not touch.
 *
 * An Inbox is 1:1 with a phone number, so the copy says "number", never "inbox".
 */
export function NumberAccessDrawer({
  inbox,
  open,
  onClose,
}: {
  inbox: Inbox;
  open: boolean;
  onClose: () => void;
}): React.JSX.Element {
  const { api } = useAuth();
  const queryClient = useQueryClient();

  const grantsQuery = useQuery({
    queryKey: ["inbox-grants", inbox.id],
    queryFn: () => fetchInboxGrants(api, inbox.id),
    enabled: open,
  });
  const departmentsQuery = useQuery({
    queryKey: ["departments"],
    queryFn: () => fetchDepartments(api),
    enabled: open,
  });
  const membersQuery = useQuery({
    queryKey: ["org-members"],
    queryFn: () => fetchOrgMembers(api),
    enabled: open,
  });

  const [draftGrants, setDraftGrants] = React.useState<InboxGrant[]>([]);
  const [granteeType, setGranteeType] = React.useState<"department" | "user">("user");
  const [granteeId, setGranteeId] = React.useState("");
  const [grantRole, setGrantRole] = React.useState<"member" | "viewer">("member");

  // F15: key on the joined grantee ids, not the array/object reference - a background
  // refetch returning the SAME set of grants must not overwrite an in-progress edit
  // (add/remove) the admin has not saved yet. Keyed on `open` too, so every (re)open
  // re-seeds from the server and a cancelled edit does not persist.
  const grantsKey = (grantsQuery.data ?? [])
    .map((g) => `${g.grantee_type}:${g.grantee_id}`)
    .sort()
    .join(",");
  React.useEffect(() => {
    if (open && grantsQuery.data) setDraftGrants(grantsQuery.data);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, grantsKey]);

  const saveMutation = useMutation({
    mutationFn: (grants: InboxGrant[]) => putInboxGrants(api, inbox.id, grants),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["inbox-grants", inbox.id] });
      void queryClient.invalidateQueries({ queryKey: ["inboxes"] });
    },
  });

  const departments: Department[] = departmentsQuery.data ?? [];
  const members: OrgMember[] = membersQuery.data ?? [];

  function labelFor(grant: InboxGrant): string {
    if (grant.grantee_type === "department") {
      return departments.find((d) => d.id === grant.grantee_id)?.name ?? grant.grantee_id;
    }
    return (
      members.find((m) => m.user_id === grant.grantee_id)?.full_name ?? grant.grantee_id
    );
  }

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

  function removeGrant(
    removeType: "department" | "user",
    removeId: string,
  ) {
    setDraftGrants((prev) =>
      prev.filter(
        (grant) =>
          !(grant.grantee_type === removeType && grant.grantee_id === removeId),
      ),
    );
  }

  return (
    <Drawer open={open} onClose={onClose} title={"Access \u00b7 " + inbox.name}>
      <p className="cx-num text-[0.8125rem] text-muted-foreground">
        {formatPhone(inbox.e164)}
      </p>
      <p className="mt-1 text-[0.8125rem] text-muted-foreground">{EXPLANATION}</p>

      {grantsQuery.isLoading ? (
        <Spinner label="Loading access" />
      ) : grantsQuery.isError ? (
        <p role="alert" className="mt-3 text-sm text-destructive">
          Couldn&rsquo;t load access: {(grantsQuery.error as Error).message}
        </p>
      ) : (
        <div className="mt-4 space-y-5">
          <div className="space-y-2">
            <SectionLabel>Current access</SectionLabel>
            {draftGrants.length === 0 ? (
              <ConsoleEmpty>Nobody else has access to this number yet.</ConsoleEmpty>
            ) : (
              <ConsoleCard className="space-y-2">
                {draftGrants.map((grant) => {
                  const label = labelFor(grant);
                  return (
                    <div
                      key={`${grant.grantee_type}-${grant.grantee_id}`}
                      className="flex items-center gap-2.5 text-[13px] text-foreground"
                    >
                      {grant.grantee_type === "department" ? (
                        <Building2
                          className="h-3.5 w-3.5 flex-none text-muted-foreground"
                          aria-hidden="true"
                        />
                      ) : (
                        <UserRound
                          className="h-3.5 w-3.5 flex-none text-muted-foreground"
                          aria-hidden="true"
                        />
                      )}
                      <span className="flex-1 truncate">{label}</span>
                      <span className="text-muted-foreground">
                        {roleLabel(grant.role)}
                      </span>
                      <Button
                        type="button"
                        onClick={() => removeGrant(grant.grantee_type, grant.grantee_id)}
                        aria-label={`Remove access for ${label}`}
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 flex-none text-muted-foreground hover:text-destructive"
                      >
                        <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                      </Button>
                    </div>
                  );
                })}
              </ConsoleCard>
            )}
          </div>

          <div className="space-y-2">
            <SectionLabel>Add access</SectionLabel>
            <div className="flex flex-wrap items-center gap-3">
              <Select
                aria-label="Grantee type"
                value={granteeType}
                onChange={(e) =>
                  setGranteeType(e.target.value as "department" | "user")
                }
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
                <option value="">Select...</option>
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
                aria-label="Access"
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
                Add
              </Button>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Button
              type="button"
              className="rounded-full px-5"
              onClick={() => saveMutation.mutate(draftGrants)}
              disabled={saveMutation.isPending}
            >
              <Save className="h-3.5 w-3.5" aria-hidden="true" /> Save access
            </Button>
            <MutationStatus
              pending={saveMutation.isPending}
              error={saveMutation.isError ? saveMutation.error : undefined}
            />
          </div>
        </div>
      )}
    </Drawer>
  );
}
