import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { DepartmentsPage } from "./DepartmentsPage";
import type { Department, Inbox, OrgMember } from "@/api/conversations";

const dept: Department = {
  id: "d1",
  name: "Acquisitions",
  is_active: true,
  member_user_ids: ["u1"],
};
const members: OrgMember[] = [
  { user_id: "u1", full_name: "Hamza Khan", email: "h@x.com", role_name: "member" },
  { user_id: "u2", full_name: "Ali Raza", email: "a@x.com", role_name: "member" },
];
const inLine: Inbox = {
  id: "i1",
  name: "Hamza 1",
  color: "#112233",
  e164: "+15035551212",
  number_id: "n1",
  my_role: "admin",
  departments: [{ id: "d1", name: "Acquisitions" }],
};
const freeLine: Inbox = {
  id: "i2",
  name: "Spare line",
  color: "#445566",
  e164: "+15035559999",
  number_id: "n2",
  my_role: "admin",
  departments: [],
};

function renderPage(opts: { role?: "admin" | "member"; depts?: Department[] } = {}) {
  const role = opts.role ?? "admin";
  const client = makeStubClient({
    "/api/v1/inboxes/i2/grants": (_p: string, init: RequestInit & { json?: unknown }) =>
      init.method === "PUT" ? (init.json as { grants: unknown }).grants : [],
    "/api/v1/inboxes": [
      { ...inLine, my_role: role },
      { ...freeLine, my_role: role },
    ],
    "/api/v1/departments/d1/members": (_p: string, init: RequestInit & { json?: unknown }) => ({
      ...dept,
      member_user_ids: (init.json as { user_ids: string[] }).user_ids,
    }),
    "/api/v1/departments/d1": (_p: string, init: RequestInit & { json?: unknown }) => ({
      ...dept,
      ...((init.json ?? {}) as object),
    }),
    "/api/v1/departments": opts.depts ?? [dept],
    "/api/v1/orgs/current/members": members,
  });
  renderWithProviders(<DepartmentsPage />, client);
  return client;
}

describe("DepartmentsPage", () => {
  it("renders department, people, lines and the no-department card", async () => {
    renderPage();
    expect(await screen.findByRole("switch", { name: "Active Acquisitions" })).toBeInTheDocument();
    expect(screen.getByText("Hamza Khan")).toBeInTheDocument();
    expect(screen.getByText("Hamza 1")).toBeInTheDocument();
    expect(screen.getByText("Lines with no department")).toBeInTheDocument();
    expect(screen.getByText("Spare line")).toBeInTheDocument();
    expect(screen.getByText(/Members can/)).toBeInTheDocument();
  });

  it("shows an empty state when there are no departments", async () => {
    renderPage({ depts: [] });
    expect(await screen.findByText("No departments yet.")).toBeInTheDocument();
  });

  it("renames a department", async () => {
    const client = renderPage();
    await screen.findByRole("switch", { name: "Active Acquisitions" });
    await userEvent.click(screen.getByRole("button", { name: "More for Acquisitions" }));
    await userEvent.click(screen.getByRole("button", { name: "Rename" }));
    const input = screen.getByLabelText("Department name Acquisitions");
    await userEvent.clear(input);
    await userEvent.type(input, "Acq East");
    await userEvent.click(screen.getByRole("button", { name: "Save name" }));
    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/departments/d1" && c.init.method === "PATCH",
      );
      expect(call?.init.json).toEqual({ name: "Acq East" });
    });
  });

  it("toggles active", async () => {
    const client = renderPage();
    await userEvent.click(await screen.findByRole("switch", { name: "Active Acquisitions" }));
    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/departments/d1" && c.init.method === "PATCH",
      );
      expect(call?.init.json).toEqual({ is_active: false });
    });
  });

  it("adds a member", async () => {
    const client = renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Add person to Acquisitions" }));
    await userEvent.selectOptions(screen.getByLabelText("Person to add to Acquisitions"), "u2");
    await userEvent.click(screen.getByRole("button", { name: "Add person" }));
    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/departments/d1/members" && c.init.method === "PUT",
      );
      expect(call?.init.json).toEqual({ user_ids: ["u1", "u2"] });
    });
  });

  it("adds a line to a department", async () => {
    const client = renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Add line to Acquisitions" }));
    await userEvent.selectOptions(screen.getByLabelText("Line to add to Acquisitions"), "i2");
    await userEvent.click(screen.getByRole("button", { name: "Add to department" }));
    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/inboxes/i2/grants" && c.init.method === "PUT",
      );
      expect(call?.init.json).toEqual({
        grants: [{ grantee_type: "department", grantee_id: "d1", role: "member" }],
      });
    });
  });

  it("adds an unassigned line to a department from the no-department card", async () => {
    const client = renderPage();
    await userEvent.selectOptions(
      await screen.findByLabelText("Add Spare line to a department"),
      "d1",
    );
    await waitFor(() => {
      expect(
        client.calls.some((c) => c.path === "/api/v1/inboxes/i2/grants" && c.init.method === "PUT"),
      ).toBe(true);
    });
  });

  it("is read-only without the manage permission", async () => {
    renderPage({ role: "member" });
    expect(await screen.findByRole("switch", { name: "Active Acquisitions" })).toBeInTheDocument();
    expect(screen.getByText(/view departments but not change/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /More for/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Add person to/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Add line to/ })).toBeNull();
    expect(screen.getByRole("switch", { name: "Active Acquisitions" })).toBeDisabled();
  });
});
