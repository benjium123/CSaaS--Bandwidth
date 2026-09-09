import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Me } from "@/auth/AuthContext";
import { ContactsPage } from "./ContactsPage";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const MEMBERS = [
  { user_id: "u1", email: "owner@example.com", full_name: "Owner Person", role_name: "owner" },
  { user_id: "u2", email: "agent@example.com", full_name: "Agent Person", role_name: "agent" },
];

const DEPARTMENTS = [
  { id: "d1", name: "Sales", is_active: true, member_user_ids: ["u1"] },
  { id: "d2", name: "Support", is_active: true, member_user_ids: ["u2"] },
];

const CONTACTS = [
  {
    id: "c1",
    display_name: "Ada Lovelace",
    phones: [{ e164: "+19725550199" }],
    owner_user_id: "u1",
    department_id: "d1",
  },
  {
    id: "c2",
    display_name: "Bob",
    phones: [{ e164: "+19725550200" }],
    owner_user_id: null,
    department_id: null,
  },
  {
    id: "c3",
    display_name: "Carol",
    phones: [{ e164: "+19725550201" }],
    owner_user_id: "u9",
    department_id: "d9",
  },
];

const ME_CAN_ASSIGN: Me = {
  id: "u1",
  email: "owner@example.com",
  full_name: "Owner Person",
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org",
      org_slug: "org",
      role_name: "owner",
      permissions: ["contacts:assign"],
    },
  ],
};

const ME_NO_ASSIGN: Me = {
  id: "u2",
  email: "agent@example.com",
  full_name: "Agent Person",
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org",
      org_slug: "org",
      role_name: "agent",
      permissions: ["contacts:read"],
    },
  ],
};

describe("ContactsPage", () => {
  // The stub matcher is startsWith-based, first insertion wins. The specific
  // owner and bulk routes MUST be inserted before the general "/api/v1/contacts"
  // key, otherwise every owner/bulk request is swallowed by the list stub.
  it("renders Owner and Team columns with resolved names and null fallbacks", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_CAN_ASSIGN,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/departments": DEPARTMENTS,
      "/api/v1/contacts": CONTACTS,
    });
    renderWithProviders(<ContactsPage />, client);

    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("Owner Person")).toBeInTheDocument();
    expect(screen.getByText("Sales")).toBeInTheDocument();
    expect(screen.getByText("Unassigned")).toBeInTheDocument();
    expect(screen.getByText("No team")).toBeInTheDocument();
    expect(screen.getAllByText("Unknown").length).toBe(2);
  });

  it("adds a contact and shows a success confirmation", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_CAN_ASSIGN,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/departments": DEPARTMENTS,
      "/api/v1/contacts": ((_path, init) => {
        if (init.method === "POST") {
          return {
            id: "c-new",
            display_name: "Dave",
            phones: [{ e164: "+19725550300" }],
            owner_user_id: null,
            department_id: null,
          };
        }
        return CONTACTS;
      }) as RouteStub,
    });
    renderWithProviders(<ContactsPage />, client);

    await screen.findByText("Ada Lovelace");
    await userEvent.type(screen.getByLabelText("Contact name"), "Dave");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      const call = client.calls.find(
        (entry) => entry.path === "/api/v1/contacts" && entry.init.method === "POST",
      );
      expect(call?.init.json).toEqual({ display_name: "Dave", phones: [] });
    });

    expect(await screen.findByText("Added Dave.")).toBeInTheDocument();
  });

  it("filters by My team and clears when the chip is clicked again", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_CAN_ASSIGN,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/departments": DEPARTMENTS,
      "/api/v1/contacts": CONTACTS,
    });
    renderWithProviders(<ContactsPage />, client);

    await screen.findByText("Ada Lovelace");

    const teamChip = screen.getByRole("button", { name: "My team" });
    await userEvent.click(teamChip);
    await waitFor(() => {
      expect(client.calls.some((call) => call.path === "/api/v1/contacts?filter=team")).toBe(
        true,
      );
    });

    const beforeClear = client.calls.filter((call) => call.path === "/api/v1/contacts").length;
    await userEvent.click(teamChip);
    await waitFor(() => {
      expect(
        client.calls.filter((call) => call.path === "/api/v1/contacts").length,
      ).toBeGreaterThan(beforeClear);
    });
    expect(client.calls[client.calls.length - 1].path).toBe("/api/v1/contacts");
  });

  it("assigns a single contact via the row drawer", async () => {
    const client = makeStubClient({
      // Specific owner route must appear before general contacts.
      "/api/v1/contacts/c1/owner": {
        ...CONTACTS[0],
        owner_user_id: "u2",
        department_id: "d2",
      },
      "/api/v1/contacts/bulk/assign": { updated: 1 },
      "/api/v1/auth/me": ME_CAN_ASSIGN,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/departments": DEPARTMENTS,
      "/api/v1/contacts": CONTACTS,
    });
    renderWithProviders(<ContactsPage />, client);

    await screen.findByText("Ada Lovelace");
    const row = screen.getByText("Ada Lovelace").closest("tr") as HTMLTableRowElement;
    await userEvent.click(within(row).getByRole("button", { name: "Assign" }));

    await screen.findByLabelText("Owner");
    await userEvent.selectOptions(screen.getByLabelText("Owner"), "u2");
    await userEvent.selectOptions(screen.getByLabelText("Team"), "d2");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const call = client.calls.find(
        (entry) => entry.path === "/api/v1/contacts/c1/owner" && entry.init.method === "PATCH",
      );
      expect(call?.init.json).toEqual({ owner_user_id: "u2", department_id: "d2" });
    });
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("bulk assigns selected contacts", async () => {
    const client = makeStubClient({
      "/api/v1/contacts/bulk/assign": { updated: 2 },
      "/api/v1/auth/me": ME_CAN_ASSIGN,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/departments": DEPARTMENTS,
      "/api/v1/contacts": CONTACTS,
    });
    renderWithProviders(<ContactsPage />, client);

    await screen.findByText("Ada Lovelace");
    await userEvent.click(screen.getByLabelText("Select Ada Lovelace"));
    await userEvent.click(screen.getByLabelText("Select Bob"));
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Assign selected" }));
    await screen.findByRole("dialog", { name: "Assign 2 contacts" });
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const call = client.calls.find(
        (entry) => entry.path === "/api/v1/contacts/bulk/assign" && entry.init.method === "POST",
      );
      expect(call?.init.json).toEqual({
        contact_ids: ["c1", "c2"],
        owner_user_id: null,
        department_id: null,
      });
    });
  });

  it("shows assign buttons disabled when the user lacks contacts:assign", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_NO_ASSIGN,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/departments": DEPARTMENTS,
      "/api/v1/contacts": CONTACTS,
    });
    renderWithProviders(<ContactsPage />, client);

    await screen.findByText("Ada Lovelace");
    const assignButtons = screen.getAllByRole("button", { name: "Assign" });
    expect(assignButtons.length).toBeGreaterThan(0);
    for (const button of assignButtons) {
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute(
        "title",
        "You don't have permission to reassign contacts.",
      );
    }
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows a list error and recovers via Retry", async () => {
    let contactsCalls = 0;
    const client = makeStubClient({
      "/api/v1/auth/me": ME_CAN_ASSIGN,
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/departments": DEPARTMENTS,
      "/api/v1/contacts": (() => {
        contactsCalls += 1;
        if (contactsCalls === 1) return new Error("Failed to load contacts");
        return CONTACTS;
      }) as RouteStub,
    });
    renderWithProviders(<ContactsPage />, client);

    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to load contacts");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
