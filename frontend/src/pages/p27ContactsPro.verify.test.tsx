/**
 * P27 verification: the contacts-pro surface (Lists tab, saved views, export, merge
 * preview, erasure, retention) and, just as much, who is allowed to see each control.
 */
import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import type { Me } from "@/auth/AuthContext";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";
import { ContactsPage } from "@/pages/ContactsPage";
import { ContactDetailDrawer } from "@/components/contacts/ContactDetailDrawer";
import { DataRetentionCard } from "@/components/settings/DataRetentionCard";
import type { ContactOut } from "@/api/contacts";

const { dialMock, navigateMock } = vi.hoisted(() => ({
  dialMock: vi.fn(),
  navigateMock: vi.fn(),
}));

vi.mock("@/softphone/SoftphoneProvider", () => ({
  SoftphoneProvider: ({ children }: { children: ReactNode }) => children,
  useSoftphone: () => ({ dial: dialMock }),
}));

vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => navigateMock,
}));

const CONTACT: ContactOut = {
  id: "c1",
  display_name: "Ada Lovelace",
  phones: [{ e164: "+19725550199" }],
  owner_user_id: null,
  department_id: null,
};

const CONTACT_2: ContactOut = {
  id: "c2",
  display_name: "Bob",
  phones: [],
  owner_user_id: null,
  department_id: null,
};

function meWith(permissions: string[]): Me {
  return {
    id: "u1",
    email: "o@example.com",
    full_name: "Owner",
    memberships: [
      {
        org_id: "org-1",
        org_name: "Org",
        org_slug: "org",
        role_name: "owner",
        permissions,
      },
    ],
  };
}

/** The views route must come first: the harness matches by startsWith in key order. */
const contactsPageBase = (permissions: string[]): Record<string, unknown> => ({
  "/api/v1/contacts/views": [],
  "/api/v1/contacts": [CONTACT, CONTACT_2],
  "/api/v1/orgs/current/members": [],
  "/api/v1/departments": [],
  "/api/v1/auth/me": meWith(permissions),
});

describe("P27 Contacts page", () => {
  it("renders the Lists tab, which carries the list import UI", async () => {
    const client = makeStubClient({
      "/api/v1/outbound/lists": [],
      ...contactsPageBase(["contacts:read", "contacts:write"]),
    });
    const user = userEvent.setup();

    renderWithProviders(<ContactsPage />, client);

    const peopleTab = await screen.findByRole("tab", { name: "People" });
    expect(peopleTab).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Lists" }));
    expect(await screen.findByLabelText("Upload list file")).toBeInTheDocument();
  });

  it("saves the current filters as a view and applies it", async () => {
    const savedView = {
      id: "v1",
      name: "My leads",
      filters: { q: "ada", scope: null },
      sort: null,
      shared: false,
      created_at: "2026-09-11T00:00:00Z",
    };

    const client = makeStubClient({
      "/api/v1/contacts/views": ((_path, init) => {
        if (init.method === "POST") return savedView;
        return [];
      }) as RouteStub,
      "/api/v1/contacts": [CONTACT, CONTACT_2],
      "/api/v1/orgs/current/members": [],
      "/api/v1/departments": [],
      "/api/v1/auth/me": meWith(["contacts:read", "contacts:write"]),
    });
    const user = userEvent.setup();

    renderWithProviders(<ContactsPage />, client);

    await user.type(await screen.findByLabelText("Search contacts"), "ada");
    await user.click(screen.getByRole("button", { name: "Save current filters" }));
    await user.type(screen.getByLabelText("View name"), "My leads");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const post = client.calls.find(
        (call) =>
          call.path === "/api/v1/contacts/views" && call.init.method === "POST",
      );
      expect(post).toBeDefined();
      expect(post?.init.json).toEqual({
        name: "My leads",
        filters: { q: "ada", scope: null },
        shared: false,
      });
    });
  });

  it("hides the share option from someone who cannot edit contacts", async () => {
    const client = makeStubClient(contactsPageBase(["contacts:read"]));
    const user = userEvent.setup();

    renderWithProviders(<ContactsPage />, client);

    await user.click(
      await screen.findByRole("button", { name: "Save current filters" }),
    );

    expect(
      screen.queryByRole("checkbox", { name: /Share with the team/i }),
    ).toBeNull();
  });

  it("runs the export flow and only downloads when the user asks", async () => {
    const client = makeStubClient({
      "/api/v1/contacts/export/job-1": {
        job_id: "job-1",
        status: "done",
        rows: 12,
        error: null,
        download_url: "/api/v1/contacts/export/job-1/download",
      },
      "/api/v1/contacts/export": { job_id: "job-1", status: "running" },
      "/api/v1/contacts/views": [],
      "/api/v1/contacts": [CONTACT, CONTACT_2],
      "/api/v1/orgs/current/members": [],
      "/api/v1/departments": [],
      "/api/v1/auth/me": meWith(["contacts:read", "contacts:write"]),
    });
    const user = userEvent.setup();

    renderWithProviders(<ContactsPage />, client);

    await user.click(await screen.findByRole("button", { name: "Export CSV" }));

    expect(await screen.findByText(/Your export is ready/)).toHaveTextContent("12");
    expect(screen.getByRole("button", { name: "Download CSV" })).toBeInTheDocument();
    expect(client.calls.every((call) => !call.path.endsWith("/download"))).toBe(true);
  });
});

describe("P27 contact drawer", () => {
  const duplicateCandidate = {
    contact_id: "c2",
    display_name: "Bob",
    phones: ["+19725550200"],
    reason: "phone",
  };

  it("previews exactly what a merge will do before the user commits", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": meWith(["contacts:write"]),
      "/api/v1/contacts/c1/duplicates": [duplicateCandidate],
    });
    const user = userEvent.setup();

    renderWithProviders(
      <ContactDetailDrawer contact={CONTACT} onClose={vi.fn()} />,
      client,
    );

    expect(await screen.findByText("Shares a phone number")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Merge Bob"));

    expect(screen.getByText(/Kept: Ada Lovelace/)).toBeInTheDocument();
    expect(screen.getByText(/version wins/)).toBeInTheDocument();
    expect(screen.getByText(/This cannot be undone/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Merge 1 contact" })).toBeInTheDocument();
  });

  it("does not offer merge to someone who cannot edit contacts", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": meWith(["contacts:read"]),
      "/api/v1/contacts/c1/duplicates": [duplicateCandidate],
    });

    renderWithProviders(
      <ContactDetailDrawer contact={CONTACT} onClose={vi.fn()} />,
      client,
    );

    await screen.findByText("Shares a phone number");

    expect(screen.getByLabelText("Merge Bob")).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^Merge/ })).toBeNull();
  });

  it("spells out what erasure destroys and keeps, and needs the word typed", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": meWith(["contacts:read", "compliance:manage"]),
      "/api/v1/contacts/c1/duplicates": [],
    });
    const user = userEvent.setup();

    renderWithProviders(
      <ContactDetailDrawer contact={CONTACT} onClose={vi.fn()} />,
      client,
    );

    await user.click(
      await screen.findByRole("button", { name: "Erase this person" }),
    );

    expect(screen.getByText(/This cannot be undone/)).toBeInTheDocument();
    expect(screen.getByText(/\[erased\]/)).toBeInTheDocument();
    expect(screen.getAllByText(/opt-out/i).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Erase permanently" })).toBeDisabled();

    await user.type(screen.getByLabelText("Type ERASE to confirm"), "ERASE");
    expect(screen.getByRole("button", { name: "Erase permanently" })).toBeEnabled();
  });

  it("hides erasure entirely from someone without compliance:manage", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": meWith(["contacts:write"]),
      "/api/v1/contacts/c1/duplicates": [],
    });

    renderWithProviders(
      <ContactDetailDrawer contact={CONTACT} onClose={vi.fn()} />,
      client,
    );

    expect(await screen.findByText("No duplicates found.")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Erase this person" }),
    ).toBeNull();
  });
});

describe("P27 retention settings", () => {
  const policy = {
    messages_days: null,
    recordings_days: 90,
    transcripts_days: 365,
    imports_days: 30,
  };

  it("shows the saved numbers in words and saves only what changed", async () => {
    const current = { ...policy };
    const client = makeStubClient({
      "/api/v1/auth/me": meWith(["settings:read", "settings:write"]),
      "/api/v1/orgs/current/retention": ((_path, init) => {
        if (init.method === "PATCH") {
          return { ...current, ...(init.json as object) };
        }
        return current;
      }) as RouteStub,
    });
    const user = userEvent.setup();

    renderWithProviders(<DataRetentionCard />, client);

    await screen.findByText("Kept forever");
    expect(screen.getByText("Kept for 90 days")).toBeInTheDocument();

    const recordingsInput = screen.getByLabelText("Call recordings days");
    await user.clear(recordingsInput);
    await user.type(recordingsInput, "30");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const patch = client.calls.find(
        (call) =>
          call.path === "/api/v1/orgs/current/retention" &&
          call.init.method === "PATCH",
      );
      expect(patch).toBeDefined();
      expect(patch?.init.json).toEqual({ recordings_days: 30 });
    });
  });

  it("will not let an admin ask for zero days", async () => {
    const current = { ...policy };
    const client = makeStubClient({
      "/api/v1/auth/me": meWith(["settings:read", "settings:write"]),
      "/api/v1/orgs/current/retention": ((_path, init) => {
        if (init.method === "PATCH") {
          return { ...current, ...(init.json as object) };
        }
        return current;
      }) as RouteStub,
    });
    const user = userEvent.setup();

    renderWithProviders(<DataRetentionCard />, client);

    const recordingsInput = await screen.findByLabelText("Call recordings days");
    await user.clear(recordingsInput);
    await user.type(recordingsInput, "0");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/at least one day/);

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(client.calls.some((call) => call.init.method === "PATCH")).toBe(false);
  });

  it("does not offer Save to someone who cannot change settings", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": meWith(["settings:read"]),
      "/api/v1/orgs/current/retention": policy,
    });

    renderWithProviders(<DataRetentionCard />, client);

    const recordingsInput = await screen.findByLabelText("Call recordings days");
    expect(recordingsInput).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });
});
