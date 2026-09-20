/**
 * Shared fixtures and helpers for the "add a teammate" suites.
 *
 * The invitation flow and the number-buying flow both drive the SAME drawer from the Team
 * page, so the stub routes and the accessible-name-safe helpers live here once instead of
 * being copy-pasted into each suite and drifting apart.
 */
import { screen, within } from "@testing-library/react";
import type userEvent from "@testing-library/user-event";

import type { NumberAssignment } from "@/api/numberAssignments";
import type { SearchOut } from "@/api/numbers";
import type { Me } from "@/auth/AuthContext";
import { makeStubClient, type RouteStub } from "@/test/harness";

export type StubClient = ReturnType<typeof makeStubClient>;
export type User = ReturnType<typeof userEvent.setup>;

/** Every permission the "Add teammate" drawer gates on. */
export const FULL = ["members:invite", "numbers:manage", "inboxes:admin"];

export const MEMBERS = [
  {
    user_id: "u1",
    email: "sarah@example.com",
    full_name: "Sarah Chen",
    role_name: "agent",
  },
];

/** What `POST /api/v1/orgs/current/members` answers with once the invite is accepted. */
export const CREATED_MEMBER = {
  user_id: "u9",
  email: "new@example.com",
  full_name: "Nikhil Roy",
  role_name: "agent",
};

/**
 * The org's four inboxes as they look from Sarah's side: inbox-a is hers directly, inbox-b she
 * only reaches through the Billing department, and inbox-c / inbox-d nobody holds.
 */
export const ORG_ASSIGNMENTS: NumberAssignment[] = [
  {
    inbox_id: "inbox-a",
    inbox_name: "Sales line",
    number_id: "num-a",
    e164: "+15550100001",
    direct_role: "member",
    via_department: [],
  },
  {
    inbox_id: "inbox-b",
    inbox_name: "Support line",
    number_id: "num-b",
    e164: "+15550100002",
    direct_role: null,
    via_department: [
      { department_id: "d1", department_name: "Billing", role: "viewer" },
    ],
  },
  {
    inbox_id: "inbox-c",
    inbox_name: "Spare line",
    number_id: "num-c",
    e164: "+15550100003",
    direct_role: null,
    via_department: [],
  },
  {
    inbox_id: "inbox-d",
    inbox_name: "Second spare",
    number_id: "num-d",
    e164: "+15550100004",
    direct_role: null,
    via_department: [],
  },
];

/**
 * The inbox the carrier order creates. It only shows up once the order has actually gone
 * through — that is how the drawer learns the inbox_id of the number it just bought.
 */
export const NEW_INBOX: NumberAssignment = {
  inbox_id: "inbox-e",
  inbox_name: "New line",
  number_id: "num-e",
  e164: "+15550199999",
  direct_role: null,
  via_department: [],
};

/** A carrier availability hit for the number the drawer is about to buy. */
export const SEARCH_RESULT = {
  e164: "+15550199999",
  number_type: "local",
  region: "TX",
  locality: "Dallas",
  monthly_cost: "$15.00",
  setup_cost: "$0.00",
  monthly_cost_cents: 1500,
  setup_cost_cents: 0,
} as unknown as SearchOut;

/** The carrier's confirmation for that same number. */
export const ORDERED_NUMBER = {
  id: "num-e",
  e164: "+15550199999",
  carrier: "bandwidth",
  is_active: true,
  status: "active",
};

/** The signed-in admin, carrying the permissions the page reads off the membership. */
export function makeMe(permissions: string[]): Me {
  return {
    id: "u1",
    email: "admin@example.com",
    full_name: "Admin User",
    memberships: [
      {
        org_id: "org-1",
        org_name: "Acme",
        org_slug: "acme",
        role_name: "admin",
        permissions,
      },
    ],
  };
}

export function makeClient(options?: {
  permissions?: string[];
  /** Serves POST /api/v1/numbers/order. Return an Error to make the order fail. */
  order?: () => unknown;
  /** Extra or overriding route stubs, merged last. */
  routes?: Record<string, RouteStub | unknown>;
}): StubClient {
  const permissions = options?.permissions ?? FULL;
  const orderStub = options?.order;
  // Per-client, NOT module scope: a second suite must never inherit the first suite's order,
  // or a test that bought nothing would suddenly find a fifth inbox in its assignment list.
  let ordered = false;

  const routes: Record<string, RouteStub | unknown> = {
    "/api/v1/auth/me": makeMe(permissions),
    "/api/v1/me/capabilities": {
      permissions,
      org: {
        has_provider: true,
        has_number: true,
        member_count: MEMBERS.length,
        registration_state: "approved",
      },
    },
    "/api/v1/orgs/current/members": (_path: string, init: RequestInit & { json?: unknown }) =>
      init.method === "POST" ? CREATED_MEMBER : MEMBERS,
    "/api/v1/orgs/current/invites": [],
    "/api/v1/numbers/available": [SEARCH_RESULT],
    "/api/v1/numbers/order": () => {
      const result = orderStub ? orderStub() : ORDERED_NUMBER;
      // The harness THROWS a returned Error, so a failed order must leave `ordered` alone:
      // flipping it here would make the new inbox appear even though nothing was bought.
      if (result instanceof Error) return result;
      ordered = true;
      return result;
    },
    "/api/v1/inboxes/assignments": (path: string, init: RequestInit & { json?: unknown }) => {
      if (init.method === "PUT") return [];
      // The per-user GET grows a fifth entry once the carrier order has gone through.
      const list = ordered ? [...ORG_ASSIGNMENTS, NEW_INBOX] : ORG_ASSIGNMENTS;
      if (path.includes("user_id=")) return list;
      // The BULK read: one entry per member, each listing every inbox in the org.
      return [{ user_id: "u1", assignments: list }];
    },
    ...(options?.routes ?? {}),
  };

  return makeStubClient(routes);
}

/**
 * Opens the drawer from the Team page and returns its dialog element.
 *
 * The dialog's TITLE is also "Add teammate", so once it is open the page has two matching
 * accessible names. Every later query must be scoped with `within(dialog)` — hence returning
 * the dialog rather than the page.
 */
export async function openDrawer(user: User): Promise<HTMLElement> {
  await user.click(await screen.findByRole("button", { name: "Add teammate" }));
  return screen.findByRole("dialog");
}

/** Types the teammate's details into the open drawer and clicks Review. */
export async function fillDetails(
  user: User,
  dialog: HTMLElement,
  details?: { name?: string; email?: string; password?: string },
): Promise<void> {
  const name = details?.name ?? "Nikhil Roy";
  const email = details?.email ?? "new@example.com";
  const password = details?.password ?? "hunter2hunter2";
  const drawer = within(dialog);

  await user.type(drawer.getByLabelText("Full name"), name);
  await user.type(drawer.getByLabelText("Email"), email);
  await user.type(drawer.getByLabelText("Password"), password);
  await user.click(drawer.getByRole("button", { name: "Review" }));
}

/** Every write the client made, as its path with any query string stripped. */
export function writes(client: StubClient): string[] {
  return client.calls
    .filter((call) => call.init.method === "POST" || call.init.method === "PUT")
    .map((call) => call.path.split("?")[0]);
}

/** The bodies POSTed to a path. */
export function bodiesPostedTo(client: StubClient, path: string): unknown[] {
  return client.calls
    .filter((call) => call.path === path && call.init.method === "POST")
    .map((call) => call.init.json);
}
