/**
 * Per-number access, exercised through the rail that owns the affordance.
 *
 * The rail owns the CONTROL (a sibling Button on each line, gated on `inboxes:admin`) and
 * NumberAccessDrawer owns the GRANTS, so this suite is the only place the two are driven
 * together: the control follows the capability, the line's own click still selects the
 * scope, and - the reason the suite exists - Save PUTs the COMPLETE grant list rather
 * than a delta that would silently revoke everyone the admin did not touch.
 *
 * Every request either component makes is stubbed explicitly. `makeStubClient` matches by
 * LONGEST prefix and throws `No stub for <path>` on anything unmatched, so an endpoint the
 * drawer grows later fails here rather than drifting through as a silent stub miss.
 */
import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InboxColumn } from "./InboxColumn";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import type { Department, Inbox, InboxGrant, OrgMember } from "@/api/conversations";

// InboxColumn's rail mounts the ONE command palette the app already has, exactly as
// InboxColumn.test.tsx does. Stubbed so the real overlay is not mounted in this tree.
vi.mock("@/components/ui/CommandPalette", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/ui/CommandPalette")>();
  return { ...actual, openCommandPalette: vi.fn() };
});

const ME = {
  id: "u1",
  email: "u@example.com",
  full_name: "U Ser",
  memberships: [
    { org_id: "org-1", org_name: "Acme Plumbing", org_slug: "acme", role_name: "owner" },
  ],
};

const ORG = {
  has_provider: true,
  has_number: true,
  member_count: 3,
  registration_state: "approved",
};

/** The capability the access affordance is gated on, plus what the rail's nav reads so
 * the column renders the way an admin really sees it. */
const ADMIN_CAPS = {
  permissions: ["inboxes:admin", "contacts:read", "org:read", "settings:read"],
  org: ORG,
};

/** The same workspace seen by an agent: no `inboxes:admin`, so no per-number access. */
const AGENT_CAPS = {
  permissions: ["inbox:read", "inbox:send", "contacts:read"],
  org: ORG,
};

function inbox(overrides: Partial<Inbox> = {}): Inbox {
  return {
    id: "i1",
    name: "Sales",
    color: "#22c55e",
    e164: "+14694617576",
    number_id: "n1",
    my_role: "admin",
    ...overrides,
  };
}

const inboxes = [
  inbox(),
  inbox({
    id: "i2",
    name: "Support",
    color: "#3b82f6",
    e164: "+12145550111",
    number_id: "n2",
  }),
];

/** The number's existing access as the server sends it: one user, one department. */
const GRANTS: InboxGrant[] = [
  { grantee_type: "user", grantee_id: "u2", role: "member" },
  { grantee_type: "department", grantee_id: "d1", role: "viewer" },
];

/** OrgMember carries role_name as well as the two display fields the drawer reads - the
 * fixture carries it so the stub answers with the server's shape, not a subset of it. */
const MEMBERS: OrgMember[] = [
  { user_id: "u2", full_name: "Ada Whitlock", email: "ada@example.com", role_name: "member" },
  { user_id: "u3", full_name: "Boris Kane", email: "boris@example.com", role_name: "member" },
];

const DEPARTMENTS: Department[] = [
  { id: "d1", name: "Support", is_active: true, member_user_ids: [] },
];

function renderRail({
  capabilities = ADMIN_CAPS,
  onSelect = vi.fn(),
  grants = GRANTS,
}: {
  capabilities?: unknown;
  onSelect?: ReturnType<typeof vi.fn>;
  grants?: InboxGrant[];
} = {}) {
  const client = makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/me/capabilities": capabilities,
    "/api/v1/notifications": { items: [], unread_count: 0 },
    // One key, two verbs: the drawer GETs the current grants and PUTs the whole saved
    // list to the same path. The PUT echoes the body back, which is what the endpoint
    // does and what the mutation caches on success.
    "/api/v1/inboxes/i1/grants": (
      _path: string,
      init: RequestInit & { json?: unknown },
    ) => (init.method === "PUT" ? (init.json as { grants: InboxGrant[] }).grants : grants),
    "/api/v1/departments": DEPARTMENTS,
    "/api/v1/orgs/current/members": MEMBERS,
  });

  const props: React.ComponentProps<typeof InboxColumn> = {
    inboxes,
    isLoading: false,
    error: null,
    selection: { kind: "all" },
    onSelect,
    unread: {},
    unreadTruncated: false,
  };

  return { client, onSelect, ...renderWithProviders(<InboxColumn {...props} />, client) };
}

/** Opens the drawer the way an admin does - the manage-access control on the Sales line -
 * and returns the dialog. */
async function openDrawer(): Promise<HTMLElement> {
  await userEvent.click(
    await screen.findByRole("button", { name: /^Manage access to Sales/ }),
  );
  return screen.findByRole("dialog");
}

/**
 * The grant ROW for `label`, addressed through its remove button.
 *
 * Scoping is not tidiness here: "Ada Whitlock" is ALSO an <option> in the Grantee select
 * and "Can send & call" / "Can view" are the Access select's options, so a dialog-wide
 * `getByText` for any of them matches more than one element and throws. The row that owns
 * the remove button is the only element that carries the grant's own label and role.
 */
async function grantRow(dialog: HTMLElement, label: string): Promise<HTMLElement> {
  const remove = await within(dialog).findByRole("button", {
    name: `Remove access for ${label}`,
  });
  const row = remove.parentElement;
  if (!row) throw new Error(`No grant row for ${label}`);
  return row;
}

describe("InboxColumn: per-number access", () => {
  it("the manage-access control appears on a line for an admin", async () => {
    renderRail({ capabilities: ADMIN_CAPS });

    const sales = await screen.findByRole("button", { name: /^Manage access to Sales/ });
    // The name is built from the LINE NAME and the FORMATTED number - not the raw e164 -
    // so this pins both halves of that sentence.
    expect(sales).toHaveAttribute("aria-label", "Manage access to Sales ((469) 461-7576)");

    // One per line: BOTH lines carry it, so it is a per-line affordance and not a single
    // control that happens to sit on the first row.
    expect(screen.getAllByRole("button", { name: /^Manage access to/ })).toHaveLength(2);
  });

  it("and is absent for a user without inboxes:admin", async () => {
    const { client } = renderRail({ capabilities: AGENT_CAPS });

    await screen.findByRole("button", { name: "Sales" });
    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/me/capabilities")).toBe(true),
    );
    // ...and the gate has RESOLVED rather than merely started. useRailNav reports the nav
    // as no longer busy only once the capability list is in hand, so this rules out the
    // first paint - where useGate fails CLOSED for everyone - as the reason for the
    // absence asserted below.
    await waitFor(() =>
      expect(screen.getByRole("navigation", { name: "Workspace" })).toHaveAttribute(
        "aria-busy",
        "false",
      ),
    );

    expect(screen.queryByRole("button", { name: /Manage access/ })).toBeNull();
  });

  it("clicking the line still selects that inbox", async () => {
    const { onSelect } = renderRail({ capabilities: ADMIN_CAPS });

    await userEvent.click(screen.getByRole("button", { name: "Sales" }));

    // The console's primary interaction: one click, one scope change, nothing else. The
    // access control is a SIBLING precisely so it cannot steal this.
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith({ kind: "inbox", inboxId: "i1" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("clicking manage access opens the drawer and does not select the inbox", async () => {
    const { onSelect } = renderRail({ capabilities: ADMIN_CAPS });

    const dialog = await openDrawer();

    expect(onSelect).not.toHaveBeenCalled();
    // The dialog's accessible name is the drawer title, "Access \u00b7 Sales" - addressed
    // through the role query so the real name computation is what is asserted.
    expect(screen.getByRole("dialog", { name: /Sales/ })).toBe(dialog);
  });

  it("the drawer lists the number's current access", async () => {
    renderRail({ capabilities: ADMIN_CAPS });

    const dialog = await openDrawer();

    // The user grant: the member's NAME (resolved through /orgs/current/members, not the
    // raw user id) and the role in words.
    const adaRow = await grantRow(dialog, "Ada Whitlock");
    expect(within(adaRow).getByText("Ada Whitlock")).toBeInTheDocument();
    expect(within(adaRow).getByText("Can send & call")).toBeInTheDocument();

    // The department grant: "Support" is the department's name, resolved through
    // /departments - if the drawer fell back to the id this would read "d1".
    const supportRow = await grantRow(dialog, "Support");
    expect(within(supportRow).getByText("Support")).toBeInTheDocument();
    expect(within(supportRow).getByText("Can view")).toBeInTheDocument();

    // The drawer is about THIS number, and says which one it is.
    expect(within(dialog).getByText("(469) 461-7576")).toBeInTheDocument();
  });

  it("saving sends the COMPLETE grant list, not only the newly added grantee", async () => {
    const { client } = renderRail({ capabilities: ADMIN_CAPS, grants: GRANTS });

    const dialog = await openDrawer();

    await userEvent.selectOptions(await within(dialog).findByLabelText("Grantee"), "u3");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save access" }));

    const put = await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/inboxes/i1/grants" && c.init.method === "PUT",
      );
      expect(call).toBeDefined();
      return call!;
    });

    // PUT /inboxes/{id}/grants REPLACES the whole list. A payload carrying only the newly
    // added grantee would silently revoke Ada and the Support department - this assertion
    // is what catches that. Both sides are sorted by grantee_id, so the SET is asserted
    // exactly while the wire order is left free.
    const body = put.init.json as { grants: InboxGrant[] };
    const byGranteeId = (a: InboxGrant, b: InboxGrant) =>
      a.grantee_id.localeCompare(b.grantee_id);
    expect([...body.grants].sort(byGranteeId)).toEqual([
      { grantee_type: "department", grantee_id: "d1", role: "viewer" },
      { grantee_type: "user", grantee_id: "u2", role: "member" },
      { grantee_type: "user", grantee_id: "u3", role: "member" },
    ]);
  });

  it("removing a grant saves only the remaining one", async () => {
    const { client } = renderRail({ capabilities: ADMIN_CAPS, grants: GRANTS });

    const dialog = await openDrawer();

    await userEvent.click(
      await within(dialog).findByRole("button", { name: "Remove access for Ada Whitlock" }),
    );
    await userEvent.click(within(dialog).getByRole("button", { name: "Save access" }));

    const put = await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/inboxes/i1/grants" && c.init.method === "PUT",
      );
      expect(call).toBeDefined();
      return call!;
    });

    // The removal is a REMOVAL, not an empty save: the department grant the admin did not
    // touch has to survive it, and the removed user must be gone from the body.
    expect(put.init.json).toEqual({
      grants: [{ grantee_type: "department", grantee_id: "d1", role: "viewer" }],
    });
  });
});
