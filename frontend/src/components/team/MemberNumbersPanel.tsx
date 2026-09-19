/**
 * Which numbers can this person use?
 *
 * The INBOX side (`InboxGrantEditor`) edits the same grants from the other end. Both write
 * through `PUT /api/v1/inboxes/assignments`, which REPLACES every direct grant for the user,
 * so this panel always sends the whole set and never a delta.
 *
 * A tick is a DIRECT grant. The `Via <department>` pills are access the person gets from a
 * department, which that PUT cannot take away; when the admin unticks one of those rows the
 * row keeps saying so, in words, rather than reporting a removal that did not happen.
 *
 * `MemberNumbersCell` is the table-cell summary TeamPage renders per member; it lives here so
 * the page imports one module and never calls these hooks inside a row map.
 */
import * as React from "react";

import { useGate } from "@/api/capabilities";
import {
  buildAssignmentPayload,
  stillInheritedAfterUntick,
  useNumberAssignments,
  usePutNumberAssignments,
  type DepartmentGrant,
  type GrantRole,
} from "@/api/numberAssignments";
import { useAuth } from "@/auth/AuthContext";
import {
  ConsoleCard,
  ConsoleEmpty,
  SectionLabel,
} from "@/components/ui/consoleChrome";
import {
  Button,
  MutationStatus,
  Pill,
  Select,
  Spinner,
} from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

/** Inbox id -> the role the admin currently wants. `null` means "no direct grant". */
type DraftMap = Record<string, GrantRole | null>;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** "A", "A and B", "A, B and C" — how the inherited departments are spoken. */
function departmentNames(grants: DepartmentGrant[]): string {
  const names = grants.map((grant) => grant.department_name);
  if (names.length === 0) return "";
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * The sentence that stops the UI lying: unticking a row cannot revoke what a department
 * grants. The wording is fixed by design review — em dash and the "›" included.
 */
function inheritedWarning(grants: DepartmentGrant[], userName: string): string {
  return (
    `Still has access through ${departmentNames(grants)}. Unticking only removes ` +
    `${userName}'s own grant — they keep this number through that department. To remove ` +
    `it completely, change it in Settings › Departments & inboxes.`
  );
}

export function MemberNumbersPanel({
  userId,
  userName,
}: {
  userId: string;
  userName: string;
}) {
  const { api } = useAuth();
  const gate = useGate();
  const canEdit = gate.can("inboxes:admin");

  const assignmentsQuery = useNumberAssignments(api, userId);
  const assignments = React.useMemo(
    () => assignmentsQuery.data ?? [],
    [assignmentsQuery.data],
  );

  const [draft, setDraft] = React.useState<DraftMap>({});

  // Seed the draft from the server's DIRECT roles. The effect is keyed on a STRING of the
  // grants, never on the array reference: a background refetch that happens to return the
  // same grants must not clobber ticks the admin has not saved yet. Same idea as
  // InboxGrantEditor, which this panel mirrors from the user's side.
  const grantsKey = React.useMemo(
    () =>
      assignments
        .map((a) => `${a.inbox_id}:${a.direct_role ?? ""}`)
        .sort()
        .join("|"),
    [assignments],
  );

  React.useEffect(() => {
    const next: DraftMap = {};
    for (const assignment of assignments) {
      next[assignment.inbox_id] = assignment.direct_role;
    }
    setDraft(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grantsKey]);

  const mutation = usePutNumberAssignments(api, userId);

  const handleToggle = (inboxId: string, next: boolean) => {
    setDraft((prev) => ({ ...prev, [inboxId]: next ? "member" : null }));
  };

  const handleRole = (inboxId: string, role: GrantRole) => {
    setDraft((prev) => ({ ...prev, [inboxId]: role }));
  };

  const handleSave = () => {
    // ALWAYS the complete desired set — the PUT replaces every direct grant, so a delta here
    // would silently revoke everything the admin did not touch.
    mutation.mutate(buildAssignmentPayload(assignments, draft));
  };

  if (assignmentsQuery.isLoading) {
    return <Spinner />;
  }

  if (assignmentsQuery.error) {
    return (
      <p role="alert" className="text-[13px] text-destructive">
        {errorMessage(assignmentsQuery.error)}
      </p>
    );
  }

  if (assignments.length === 0) {
    return <ConsoleEmpty>No numbers in this workspace yet.</ConsoleEmpty>;
  }

  return (
    <div className="flex flex-col gap-[11px]">
      <SectionLabel>Numbers</SectionLabel>

      <div className="flex flex-col gap-2">
        {assignments.map((assignment) => {
          const role = draft[assignment.inbox_id] ?? null;
          const inherited = assignment.via_department.length > 0;

          return (
            <ConsoleCard
              key={assignment.inbox_id}
              className="flex flex-col gap-[9px]"
            >
              <div className="flex flex-wrap items-center gap-3">
                <input
                  type="checkbox"
                  className="h-4 w-4 flex-none accent-[hsl(var(--cx-accent))]"
                  aria-label={`${assignment.e164} for ${userName}`}
                  checked={role != null}
                  disabled={!canEdit}
                  onChange={(event) =>
                    handleToggle(assignment.inbox_id, event.target.checked)
                  }
                />

                <div className="min-w-0 flex-1">
                  <div className="text-[14px] font-medium text-[hsl(var(--cx-text))]">
                    {formatPhone(assignment.e164)}
                  </div>
                  <div className="text-[12px] text-[hsl(var(--cx-muted))]">
                    {assignment.inbox_name}
                  </div>
                </div>

                {role != null ? (
                  <Select
                    aria-label={`Access level for ${assignment.e164}`}
                    value={role}
                    disabled={!canEdit}
                    onChange={(event) =>
                      handleRole(
                        assignment.inbox_id,
                        event.target.value as GrantRole,
                      )
                    }
                  >
                    <option value="member">
                      Member — can read, send and dial
                    </option>
                    <option value="viewer">Viewer — read-only</option>
                  </Select>
                ) : null}
              </div>

              {inherited ? (
                <div className="flex flex-wrap items-center gap-2">
                  {assignment.via_department.map((grant) => (
                    <Pill
                      key={`${grant.department_id}:${grant.role}`}
                      tone="info"
                    >
                      Via {grant.department_name}
                    </Pill>
                  ))}
                </div>
              ) : null}

              {stillInheritedAfterUntick(assignment, role) ? (
                <p
                  role="note"
                  className="text-[12px] text-[hsl(var(--cx-muted))]"
                >
                  {inheritedWarning(assignment.via_department, userName)}
                </p>
              ) : null}
            </ConsoleCard>
          );
        })}
      </div>

      {/* While the gate is still loading `canEdit` is false; that is correct, but the panel
          must not tell a real admin they lack permission before the gate resolves. Leave the
          slot empty until we know. */}
      {canEdit ? (
        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            className="rounded-full px-5"
            disabled={mutation.isPending}
            onClick={handleSave}
          >
            Save numbers
          </Button>
          <MutationStatus
            pending={mutation.isPending}
            error={mutation.error}
            success={mutation.isSuccess ? "Saved" : undefined}
          />
        </div>
      ) : gate.isLoading ? null : (
        <p className="text-[12.5px] text-[hsl(var(--cx-muted))]">
          You don't have permission to change number access.
        </p>
      )}
    </div>
  );
}

/**
 * The Team table's per-member number summary plus the expand toggle.
 *
 * It owns its own query and gate so TeamPage never calls hooks inside a `members.map`.
 * Loading deliberately shows a bare em dash rather than a spinner: the reference table has no
 * row spinners, and a toggle that opens an unfinished panel is worse than waiting a beat.
 */
export function MemberNumbersCell({
  userId,
  userName,
  expanded,
  onToggle,
}: {
  userId: string;
  userName: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { api } = useAuth();
  const gate = useGate();
  const canEdit = gate.can("inboxes:admin");

  const assignmentsQuery = useNumberAssignments(api, userId);

  if (assignmentsQuery.isLoading) {
    return <span className="text-[13px] text-[hsl(var(--cx-muted))]">—</span>;
  }

  if (assignmentsQuery.error) {
    return (
      <span className="text-[13px] text-[hsl(var(--cx-muted))]">
        Unavailable
      </span>
    );
  }

  const data = assignmentsQuery.data ?? [];
  const direct = data.filter((a) => a.direct_role != null).length;
  const inheritedOnly = data.filter(
    (a) => a.direct_role == null && a.via_department.length > 0,
  ).length;

  return (
    <span className="flex flex-wrap items-center gap-2">
      {direct === 0 && inheritedOnly === 0 ? (
        <Pill tone="neutral">No numbers</Pill>
      ) : (
        <>
          {direct > 0 ? (
            <span className="text-[13px] text-[hsl(var(--cx-text))]">
              {direct} {direct === 1 ? "number" : "numbers"}
            </span>
          ) : null}
          {inheritedOnly > 0 ? (
            <span className="text-[13px] text-[hsl(var(--cx-muted))]">
              {inheritedOnly === 1
                ? "+1 via a department"
                : `+${inheritedOnly} via departments`}
            </span>
          ) : null}
        </>
      )}

      <Button
        type="button"
        size="sm"
        variant="ghost"
        className="rounded-full px-3.5"
        aria-expanded={expanded}
        // Every row carries one of these, so the visible label alone is ambiguous to a
        // screen reader (and to a test): name the person it belongs to.
        aria-label={`${canEdit ? "Manage" : "View"} numbers for ${userName}`}
        onClick={onToggle}
      >
        {canEdit ? "Manage numbers" : "View numbers"}
      </Button>
    </span>
  );
}
