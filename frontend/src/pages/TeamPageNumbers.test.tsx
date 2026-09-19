/**
 * The USER side of number access, driven through the Team page.
 *
 * `PUT /api/v1/inboxes/assignments` REPLACES the user's direct grants wholesale, so the two
 * payload assertions below use exact equality — never `arrayContaining` / `objectContaining`.
 * Those are the tests that catch a delta bug, which would silently revoke every number the
 * admin did not touch.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { NumberAssignment } from "@/api/numberAssignments";
import type { Me } from "@/auth/AuthContext";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

import { TeamPage } from "./TeamPage";

afterEach(() => {
  cleanup();
});

type StubClient = ReturnType<typeof makeStubClient>;
type RecordedCall = StubClient["calls"][number];
type User = ReturnType<typeof userEvent.setup>;

const MEMBERS = [
  {
    user_id: "u1",
    email: "sarah@example.com",
    full_name: "Sarah Chen",
    role_name: "agent",
  },
  {
    user_id: "u2",
    email: "dan@example.com",
    full_name: "Dan Okafor",
    role_name: "agent",
  },
];

/** The four numbers in the org, as they look from Sarah's side. */
const U1_ASSIGNMENTS: NumberAssignment[] = [
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
    direct_role: "viewer",
    via_department: [],
  },
  {
    inbox_id: "inbox-c",
    inbox_name: "Billing line",
    number_id: "num-c",
    e164: "+15550100003",
    direct_role: "member",
    via_department: [
      { department_id: "dept-1", department_name: "Billing", role: "viewer" },
    ],
  },
  {
    inbox_id: "inbox-d",
    inbox_name: "Spare line",
    number_id: "num-d",
    e164: "+15550100004",
    direct_role: null,
    via_department: [],
  },
];

/** Dan has nothing of his own and nothing through a department: the empty case. */
const U2_ASSIGNMENTS: NumberAssignment[] = U1_ASSIGNMENTS.map((assignment) => ({
  ...assignment,
  direct_role: null,
  via_department: [],
}));

function makeMe(permissions: string[]): Me {
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

/**
 * Every route TeamPage touches, so nothing throws `No stub for ...`. The permissions go into
 * BOTH places the gate can read from (capabilities and the membership on /auth/me), so a test
 * that wants denial gets it on either path.
 */
function makeClient({ permissions }: { permissions: string[] }): StubClient {
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
    "/api/v1/orgs/current/members": MEMBERS,
    "/api/v1/orgs/current/invites": [],
    // Serves GET and PUT alike: the panel invalidates after a PUT and re-reads the server's
    // view, so the response body of the PUT is not what this suite asserts on.
    "/api/v1/inboxes/assignments": (path: string) =>
      path.includes("user_id=u1") ? U1_ASSIGNMENTS : U2_ASSIGNMENTS,
  };

  return makeStubClient(routes);
}

async function openNumbersPanel(
  user: User,
  userName: string,
  verb: "Manage" | "View",
): Promise<void> {
  const toggle = await screen.findByRole("button", {
    name: `${verb} numbers for ${userName}`,
  });
  await user.click(toggle);
}

function checkboxFor(label: string): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement;
}

function selectValue(label: string): string {
  return (screen.getByLabelText(label) as HTMLSelectElement).value;
}

function findPut(client: StubClient): RecordedCall {
  const put = client.calls.find((call) => call.init.method === "PUT");
  if (!put) {
    throw new Error("expected a PUT to /api/v1/inboxes/assignments");
  }
  return put;
}

function putInboxes(put: RecordedCall): { inbox_id: string; role: string }[] {
  return (put.init.json as { inboxes: { inbox_id: string; role: string }[] }).inboxes;
}

async function saveNumbers(user: User): Promise<void> {
  await user.click(screen.getByRole("button", { name: "Save numbers" }));
}

describe("TeamPage number assignments", () => {
  it("renders a member's current numbers", async () => {
    const user = userEvent.setup();
    const client = makeClient({ permissions: ["inboxes:admin"] });
    renderWithProviders(<TeamPage />, client);

    await screen.findByText("sarah@example.com");
    await openNumbersPanel(user, "Sarah Chen", "Manage");

    expect(await screen.findByText("(555) 010-0001")).toBeTruthy();
    expect(screen.getByText("(555) 010-0002")).toBeTruthy();
    expect(screen.getByText("(555) 010-0003")).toBeTruthy();
    expect(screen.getByText("(555) 010-0004")).toBeTruthy();
    expect(screen.getByText("Sales line")).toBeTruthy();
    expect(screen.getByText("Spare line")).toBeTruthy();

    expect(checkboxFor("+15550100001 for Sarah Chen").checked).toBe(true);
    expect(checkboxFor("+15550100002 for Sarah Chen").checked).toBe(true);
    expect(checkboxFor("+15550100004 for Sarah Chen").checked).toBe(false);

    expect(selectValue("Access level for +15550100002")).toBe("viewer");
    expect(selectValue("Access level for +15550100001")).toBe("member");
    // Unticked rows carry no role control at all.
    expect(screen.queryByLabelText("Access level for +15550100004")).toBeNull();
  });

  it("saving sends the COMPLETE desired set, not a delta", async () => {
    const user = userEvent.setup();
    const client = makeClient({ permissions: ["inboxes:admin"] });
    renderWithProviders(<TeamPage />, client);

    await screen.findByText("sarah@example.com");
    await openNumbersPanel(user, "Sarah Chen", "Manage");
    await screen.findByLabelText("+15550100004 for Sarah Chen");

    await user.click(checkboxFor("+15550100004 for Sarah Chen"));
    await saveNumbers(user);

    await waitFor(() =>
      expect(client.calls.some((call) => call.init.method === "PUT")).toBe(true),
    );

    const put = findPut(client);
    expect(put.path).toBe("/api/v1/inboxes/assignments?user_id=u1");
    // The PUT replaces EVERY direct grant for this user, so a delta containing only the newly
    // ticked row would silently revoke the three numbers the admin never touched.
    expect(put.init.json).toEqual({
      inboxes: [
        { inbox_id: "inbox-a", role: "member" },
        { inbox_id: "inbox-b", role: "viewer" },
        { inbox_id: "inbox-c", role: "member" },
        { inbox_id: "inbox-d", role: "member" },
      ],
    });
  });

  it("unticking a number removes it from the payload", async () => {
    const user = userEvent.setup();
    const client = makeClient({ permissions: ["inboxes:admin"] });
    renderWithProviders(<TeamPage />, client);

    await screen.findByText("sarah@example.com");
    await openNumbersPanel(user, "Sarah Chen", "Manage");
    await screen.findByLabelText("+15550100002 for Sarah Chen");

    await user.click(checkboxFor("+15550100002 for Sarah Chen"));
    await saveNumbers(user);

    await waitFor(() =>
      expect(client.calls.some((call) => call.init.method === "PUT")).toBe(true),
    );

    const put = findPut(client);
    // inbox-d was never ticked, so it is absent too — omission is the revocation.
    expect(put.init.json).toEqual({
      inboxes: [
        { inbox_id: "inbox-a", role: "member" },
        { inbox_id: "inbox-c", role: "member" },
      ],
    });
    expect(putInboxes(put).every((inbox) => inbox.inbox_id !== "inbox-b")).toBe(true);
  });

  it("a user without inboxes:admin sees the state but no edit controls", async () => {
    const user = userEvent.setup();
    const client = makeClient({ permissions: ["contacts:read"] });
    renderWithProviders(<TeamPage />, client);

    await screen.findByText("sarah@example.com");
    await openNumbersPanel(user, "Sarah Chen", "View");

    // The state is still shown, it just cannot be changed.
    expect(await screen.findByText("(555) 010-0001")).toBeTruthy();
    const first = checkboxFor("+15550100001 for Sarah Chen");
    expect(first.checked).toBe(true);
    expect(first.disabled).toBe(true);

    expect(screen.queryByRole("button", { name: "Save numbers" })).toBeNull();
    expect(
      await screen.findByText("You don't have permission to change number access."),
    ).toBeTruthy();
  });

  it("a number inherited via a department is not reported as removed when unticked", async () => {
    const user = userEvent.setup();
    const client = makeClient({ permissions: ["inboxes:admin"] });
    renderWithProviders(<TeamPage />, client);

    await screen.findByText("sarah@example.com");
    await openNumbersPanel(user, "Sarah Chen", "Manage");
    await screen.findByText("(555) 010-0003");

    expect(screen.getByText("Via Billing")).toBeTruthy();

    await user.click(checkboxFor("+15550100003 for Sarah Chen"));

    const note = await screen.findByRole("note");
    const noteText = note.textContent ?? "";
    expect(noteText).toContain("Still has access through Billing");
    expect(noteText).toContain("they keep this number through that department");
    expect(noteText).toContain("Settings › Departments & inboxes");

    // The row must never be described as gone: the department still grants it.
    expect(screen.queryByText(/removed|no access|revoked/i)).toBeNull();

    await saveNumbers(user);
    await waitFor(() =>
      expect(client.calls.some((call) => call.init.method === "PUT")).toBe(true),
    );

    const put = findPut(client);
    expect(put.init.json).toEqual({
      inboxes: [
        { inbox_id: "inbox-a", role: "member" },
        { inbox_id: "inbox-b", role: "viewer" },
      ],
    });

    // Give the invalidated re-read time to land, then check the warning survived it: the
    // department access genuinely outlives the save, so the panel must keep saying so.
    const putIndex = client.calls.findIndex((call) => call.init.method === "PUT");
    await waitFor(() =>
      expect(
        client.calls
          .slice(putIndex + 1)
          .some((call) => call.path.startsWith("/api/v1/inboxes/assignments")),
      ).toBe(true),
    );
    expect(screen.getByRole("note").textContent ?? "").toContain(
      "Still has access through Billing",
    );
  });

  it("a member with no numbers at all reads as \"No numbers\"", async () => {
    const client = makeClient({ permissions: ["inboxes:admin"] });
    renderWithProviders(<TeamPage />, client);

    await screen.findByText("dan@example.com");
    const row = screen.getByText("dan@example.com").closest("tr")!;

    expect(await within(row).findByText("No numbers")).toBeTruthy();
    // Not merely an empty cell: no "0 numbers" / "1 number" summary either.
    expect(within(row).queryByText(/^\d+ numbers?$/)).toBeNull();
  });
});
