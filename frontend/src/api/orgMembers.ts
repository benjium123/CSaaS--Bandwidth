/**
 * Org members and the BULK number-assignment read, from the ADMIN's side.
 *
 * Creating a member is one POST that carries the personal details AND the initial number
 * grants together, so a new teammate either lands with their inboxes or not at all. The bulk
 * read exists for the invite drawer: every user's assignments in one request, instead of one
 * request per person, because the drawer needs the whole org to offer the *unheld* numbers.
 *
 * `unheldNumbers` is deliberately pure and exported: the set arithmetic (which inboxes does
 * NOBODY hold) is the part worth unit-testing without a network or a render.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiClient } from "@/api/client";
import type { MemberOut } from "@/api/hooks";
import type { NumberAssignment } from "@/api/numberAssignments";

/** The exact body POSTed to create a member. All five keys are always sent; `inbox_ids` is
 *  `[]` when the new teammate is not given a number yet. */
export type MemberCreateIn = {
  email: string;
  full_name: string;
  password: string;
  role_name: "admin" | "agent";
  inbox_ids: string[];
};

/** One user's complete assignment list. The backend lists EVERY inbox in the org here,
 *  held or not, which is what makes `unheldNumbers` possible from a single entry. */
export type UserAssignments = {
  user_id: string;
  assignments: NumberAssignment[];
};

const MEMBERS_PATH = "/api/v1/orgs/current/members";
/** No query string: this is the org-wide read, unlike the per-user `?user_id=` one. */
const ALL_ASSIGNMENTS_PATH = "/api/v1/inboxes/assignments";

export function useCreateMember(api: ApiClient) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (vars: MemberCreateIn) =>
      api.request<MemberOut>(MEMBERS_PATH, { method: "POST", json: vars }),
    onSuccess: () => {
      // The member list gained a row, and the new person may now hold numbers, so the
      // bulk-assignment view (and any per-user one under the same prefix) is stale too.
      void queryClient.invalidateQueries({ queryKey: ["org-members"] });
      void queryClient.invalidateQueries({ queryKey: ["inbox-assignments"] });
    },
  });
}

/**
 * Every user's assignments in one request. `enabled` lets the caller hold the request until
 * the drawer that needs it is actually open.
 */
export function useAllAssignments(api: ApiClient, enabled = true) {
  return useQuery({
    queryKey: ["inbox-assignments", "all"],
    queryFn: () => api.request<UserAssignments[]>(ALL_ASSIGNMENTS_PATH),
    enabled,
    retry: false,
  });
}

/**
 * The numbers that no one in the org currently holds, so the invite drawer can offer exactly
 * the free ones.
 *
 * Every entry's `assignments` lists EVERY inbox in the org (the backend guarantees this),
 * so the FIRST entry alone is the catalogue of all inboxes. A number is "held" when ANY user
 * has a direct grant (`direct_role != null`) or inherits it from a department
 * (`via_department.length > 0`). The catalogue is then filtered against the held set,
 * de-duplicated by `inbox_id`, and sorted by `e164` ascending.
 *
 * Pure and exported so it is unit-testable without a network or a render.
 */
export function unheldNumbers(entries: UserAssignments[]): NumberAssignment[] {
  if (entries.length === 0) return [];

  const held = new Set<string>();
  for (const entry of entries) {
    for (const assignment of entry.assignments) {
      if (assignment.direct_role != null || assignment.via_department.length > 0) {
        held.add(assignment.inbox_id);
      }
    }
  }

  const catalogue = entries[0]?.assignments ?? [];
  const seen = new Set<string>();
  const unheld: NumberAssignment[] = [];

  for (const assignment of catalogue) {
    if (held.has(assignment.inbox_id) || seen.has(assignment.inbox_id)) continue;
    seen.add(assignment.inbox_id);
    unheld.push(assignment);
  }

  return unheld.sort((a, b) => a.e164.localeCompare(b.e164));
}
