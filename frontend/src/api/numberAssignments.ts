/**
 * Number access, from the USER's side.
 *
 * An Inbox is 1:1 with a phone number, so this speaks the same resource the Inbox settings
 * page edits — `PUT /api/v1/inboxes/assignments` — just keyed by person instead of by number.
 * The endpoint is REPLACE-ALL: the body must always carry the COMPLETE desired set of direct
 * grants, because anything omitted is revoked. Reusing this one write path from both pages is
 * what keeps the two views from drifting apart.
 *
 * `direct_role` is a grant to this person. `via_department` is access inherited from a
 * department they belong to, which this endpoint CANNOT change — the UI has to say so rather
 * than pretend an untick revoked it.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiClient } from "@/api/client";

export type GrantRole = "member" | "viewer";

export interface DepartmentGrant {
  department_id: string;
  department_name: string;
  role: GrantRole;
}

export interface NumberAssignment {
  inbox_id: string;
  inbox_name: string;
  number_id: string;
  e164: string;
  direct_role: GrantRole | null;
  via_department: DepartmentGrant[];
}

export interface AssignmentSelection {
  inbox_id: string;
  role: GrantRole;
}

export function assignmentsQueryKey(userId: string): unknown[] {
  return ["inbox-assignments", userId];
}

function assignmentsPath(userId: string): string {
  return `/api/v1/inboxes/assignments?user_id=${encodeURIComponent(userId)}`;
}

/** Every inbox in the org — not only the ones this person has been granted. */
export async function fetchNumberAssignments(
  api: ApiClient,
  userId: string,
): Promise<NumberAssignment[]> {
  return api.request<NumberAssignment[]>(assignmentsPath(userId));
}

/**
 * Replaces EVERY direct grant for `userId` across the whole org: inboxes named here get a
 * grant at that role, inboxes left out lose the user's direct grant. Always send the complete
 * desired set, never a delta.
 */
export async function putNumberAssignments(
  api: ApiClient,
  userId: string,
  inboxes: AssignmentSelection[],
): Promise<NumberAssignment[]> {
  return api.request<NumberAssignment[]>(assignmentsPath(userId), {
    method: "PUT",
    json: { inboxes },
  });
}

export function useNumberAssignments(
  api: ApiClient,
  userId: string,
  enabled = true,
) {
  return useQuery({
    queryKey: assignmentsQueryKey(userId),
    queryFn: () => fetchNumberAssignments(api, userId),
    enabled,
  });
}

export function usePutNumberAssignments(api: ApiClient, userId: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (inboxes: AssignmentSelection[]) =>
      putNumberAssignments(api, userId, inboxes),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: assignmentsQueryKey(userId),
      });
    },
  });
}

/**
 * The complete desired set, from the draft map. Sorted by `inbox_id` so the payload is stable
 * across renders (and diffable in tests).
 *
 * It deliberately does NOT filter down to "only the rows that changed": the PUT replaces the
 * user's direct grants wholesale, so a delta would silently revoke everything the admin did
 * not touch. Drafts that are null/undefined are omitted because omission IS the revocation.
 */
export function buildAssignmentPayload(
  assignments: NumberAssignment[],
  draft: Record<string, GrantRole | null>,
): AssignmentSelection[] {
  const selections: AssignmentSelection[] = [];

  for (const assignment of assignments) {
    const role = draft[assignment.inbox_id] ?? null;
    if (role === "member" || role === "viewer") {
      selections.push({ inbox_id: assignment.inbox_id, role });
    }
  }

  return selections.sort((a, b) => a.inbox_id.localeCompare(b.inbox_id));
}

/**
 * Numbers the person can still reach after `draft` is saved, but only via a department: the
 * direct grant is gone (or was never there) yet the row is inherited. The UI must not call
 * this "removed".
 */
export function stillInheritedAfterUntick(
  a: NumberAssignment,
  draftRole: GrantRole | null,
): boolean {
  return draftRole == null && a.via_department.length > 0;
}
