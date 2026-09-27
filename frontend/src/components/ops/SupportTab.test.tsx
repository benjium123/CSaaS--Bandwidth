import { afterEach, describe, expect, it } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { makeStubClient, renderWithProviders } from "@/test/harness";

import { SupportTab } from "./SupportTab";

const LIST_OPEN = "/api/v1/ops/console/support?status=open";
const LIST_ANSWERED = "/api/v1/ops/console/support?status=answered";
const LIST_ALL = "/api/v1/ops/console/support";
const CONTACTS = "/api/v1/ops/console/support-contacts";

const NO_CONTACTS = {
  email: null,
  phone: null,
  knowledge_base_url: null,
  whats_new_url: null,
  status_url: null,
  terms_url: null,
  privacy_url: null,
};

const openRequest = {
  id: "req-1",
  org_id: "org-1",
  org_name: "Acme Law",
  email: "owner@acme.test",
  subject: "Porting failed",
  body: "Line 2 never ported.",
  page: "/ops/numbers",
  status: "open",
  reply: null,
  replied_at: null,
  created_at: "2024-05-01T10:00:00Z",
};

const answeredRequest = {
  id: "req-2",
  org_id: "org-1",
  org_name: "Beta Clinic",
  email: "owner@beta.test",
  subject: "Invoice question",
  body: "Where is my invoice?",
  page: null,
  status: "answered",
  reply: "Sent it again.",
  replied_at: "2024-05-02T09:00:00Z",
  created_at: "2024-05-01T08:00:00Z",
};

afterEach(() => cleanup());

describe("SupportTab", () => {
  it("lists the open support requests", async () => {
    const client = makeStubClient({
      [LIST_OPEN]: { requests: [openRequest] },
      [CONTACTS]: NO_CONTACTS,
    });
    renderWithProviders(<SupportTab />, client);

    expect(await screen.findByText("Porting failed")).toBeTruthy();
    expect(screen.getByText(/owner@acme\.test/)).toBeTruthy();
    expect(screen.getByText("Line 2 never ported.")).toBeTruthy();
    expect(screen.getByLabelText("Reply to Porting failed")).toBeTruthy();
    expect(client.calls.some((c) => c.path === LIST_OPEN)).toBe(true);
  });

  it("requests a different list when the status filter changes", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      [LIST_OPEN]: { requests: [openRequest] },
      [LIST_ANSWERED]: { requests: [answeredRequest] },
      [CONTACTS]: NO_CONTACTS,
    });
    renderWithProviders(<SupportTab />, client);
    await screen.findByText("Porting failed");

    await user.click(screen.getByRole("button", { name: "Answered" }));

    expect(await screen.findByText("Invoice question")).toBeTruthy();
    expect(client.calls.some((c) => c.path === LIST_ANSWERED)).toBe(true);
  });

  it("omits the status query parameter for All", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      [LIST_OPEN]: { requests: [openRequest] },
      [LIST_ALL]: { requests: [openRequest, answeredRequest] },
      [CONTACTS]: NO_CONTACTS,
    });
    renderWithProviders(<SupportTab />, client);
    await screen.findByText("Porting failed");

    await user.click(screen.getByRole("button", { name: "All" }));

    await waitFor(() => expect(client.calls.some((c) => c.path === LIST_ALL)).toBe(true));
    expect(await screen.findByText("Invoice question")).toBeTruthy();
  });

  it("posts a reply without closing", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      [LIST_OPEN]: { requests: [openRequest] },
      "/api/v1/ops/console/support/req-1/reply": { emailed: true },
      [CONTACTS]: NO_CONTACTS,
    });
    renderWithProviders(<SupportTab />, client);

    const box = await screen.findByLabelText("Reply to Porting failed");
    await user.type(box, "We fixed the port.");
    await user.click(screen.getByRole("button", { name: "Send reply" }));

    await waitFor(() => {
      const call = client.calls.find((c) => c.path === "/api/v1/ops/console/support/req-1/reply");
      expect(call).toBeTruthy();
      expect(call?.init.method).toBe("POST");
      expect(call?.init.json).toEqual({ reply: "We fixed the port.", close: false });
    });

    expect(await screen.findByText("Emailed")).toBeTruthy();
    expect((box as HTMLTextAreaElement).value).toBe("");
  });

  it("posts close:true from Reply & close", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      [LIST_OPEN]: { requests: [openRequest] },
      "/api/v1/ops/console/support/req-1/reply": { emailed: false },
      [CONTACTS]: NO_CONTACTS,
    });
    renderWithProviders(<SupportTab />, client);

    await user.type(await screen.findByLabelText("Reply to Porting failed"), "Closing this out.");
    await user.click(screen.getByRole("button", { name: "Reply & close" }));

    await waitFor(() => {
      const call = client.calls.find((c) => c.path === "/api/v1/ops/console/support/req-1/reply");
      expect(call).toBeTruthy();
      expect(call?.init.json).toEqual({ reply: "Closing this out.", close: true });
    });

    expect(await screen.findByText("Saved (email failed)")).toBeTruthy();
  });

  it("saves the help menu contacts, sending null for empty fields", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      [LIST_OPEN]: { requests: [] },
      [CONTACTS]: { ...NO_CONTACTS, phone: "+44 20 7946 0000" },
    });
    renderWithProviders(<SupportTab />, client);

    // Wait for the loaded contacts to land in the form before typing, so the
    // query's effect cannot overwrite what the user typed.
    const phone = await screen.findByLabelText("Support phone");
    await waitFor(() => expect((phone as HTMLInputElement).value).toBe("+44 20 7946 0000"));

    await user.type(screen.getByLabelText("Support email"), "help@acme.test");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const call = client.calls.find((c) => c.init.method === "PUT");
      expect(call).toBeTruthy();
      expect(call?.path).toBe(CONTACTS);
      expect(call?.init.json).toEqual({
        email: "help@acme.test",
        phone: "+44 20 7946 0000",
        knowledge_base_url: null,
        whats_new_url: null,
        status_url: null,
        terms_url: null,
        privacy_url: null,
      });
    });

    expect(await screen.findByText("Saved")).toBeTruthy();
  });

  it("shows an alert when saving the contacts fails", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      [LIST_OPEN]: { requests: [] },
      [CONTACTS]: new Error("That email address is not valid."),
    });
    renderWithProviders(<SupportTab />, client);

    await screen.findByLabelText("Support email");
    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.trim().length).toBeGreaterThan(0);
  });
});
