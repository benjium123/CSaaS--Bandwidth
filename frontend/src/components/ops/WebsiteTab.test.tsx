import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { WebsiteTab } from "@/components/ops/WebsiteTab";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const CUSTOMER = {
  id: "chat-c",
  status: "waiting",
  kind: "customer",
  name: "Cara Customer",
  email: "cara@acme.test",
  phone: null,
  sms_consent: false,
  reason: "Pricing",
  page: "/pricing",
  agent_name: null,
  org_id: "org-9",
  org_name: "Acme Inc",
  assigned_user_id: null,
  assigned_name: null,
  unread: true,
  created_at: "2024-06-01T10:00:00.000Z",
  last_message_at: "2024-06-01T10:05:00.000Z",
  last_message: "How much does it cost?",
};

const VISITOR = {
  id: "chat-v",
  status: "waiting",
  kind: "visitor",
  name: "Vic Visitor",
  email: "vic@example.test",
  phone: null,
  sms_consent: false,
  reason: null,
  page: "/",
  agent_name: null,
  org_id: null,
  org_name: null,
  assigned_user_id: null,
  assigned_name: null,
  unread: false,
  created_at: "2024-06-01T09:00:00.000Z",
  last_message_at: "2024-06-01T09:10:00.000Z",
  last_message: "Hello?",
};

const DETAIL = {
  ...CUSTOMER,
  messages: [
    { id: "m1", role: "visitor", text: "How much does it cost?", at: "2024-06-01T10:05:00.000Z" },
    { id: "m2", role: "ai", text: "Growth is $29 a month.", at: "2024-06-01T10:06:00.000Z" },
  ],
  customer: { org_id: "org-9", org_name: "Acme Inc", plan: "growth", balance_usd: 12.34 },
};

/**
 * makeStubClient matches on `path.startsWith(key)`, longest key first - so the query string on
 * the list call (`/api/v1/ops/site/chats?status=open`) still resolves to the list key, while the
 * longer detail/read/assign keys win for their own paths. The "/" catch-all answers AuthProvider.
 */
function stubClient() {
  return makeStubClient({
    "/api/v1/ops/site/leads": { leads: [] },
    "/api/v1/ops/site/chats/unread": { count: 0, chats: [] },
    "/api/v1/ops/site/chats/chat-c/read": () => ({ unread: false }),
    "/api/v1/ops/site/chats/chat-c/assign": () => ({ assigned_user_id: "me-1", assigned_name: "Me" }),
    "/api/v1/ops/site/chats/chat-c": () => DETAIL,
    "/api/v1/ops/site/chats": () => ({ chats: [{ ...CUSTOMER, ai_state: "active" }, { ...VISITOR, ai_state: "handoff" }], staffed: true }),
    "/": {},
  });
}

async function openCustomer(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /Cara Customer/ }));
}

describe("WebsiteTab support inbox", () => {
  it("lists customer and visitor chats with their kind and workspace", async () => {
    const client = stubClient();
    renderWithProviders(<WebsiteTab />, client);

    expect(await screen.findByText("Cara Customer")).toBeTruthy();
    expect(screen.getByText("Vic Visitor")).toBeTruthy();
    expect(screen.getByText("Customer")).toBeTruthy();
    expect(screen.getByText("Visitor")).toBeTruthy();
    expect(screen.getByText("Acme Inc")).toBeTruthy();
  });

  it("requests customers only once the Customers filter is pressed", async () => {
    const client = stubClient();
    const user = userEvent.setup();
    renderWithProviders(<WebsiteTab />, client);
    await screen.findByText("Cara Customer");

    expect(screen.getByRole("button", { name: "Customers" }).getAttribute("aria-pressed")).toBe("false");

    await user.click(screen.getByRole("button", { name: "Customers" }));

    await waitFor(() =>
      expect(client.calls.some((call) => call.path.includes("kind=customer"))).toBe(true),
    );
    expect(screen.getByRole("button", { name: "Customers" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("marks an opened unread chat as read", async () => {
    const client = stubClient();
    const user = userEvent.setup();
    renderWithProviders(<WebsiteTab />, client);

    await openCustomer(user);

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) => call.path === "/api/v1/ops/site/chats/chat-c/read" && call.init.method === "POST",
        ),
      ).toBe(true),
    );
  });

  it("shows the customer card with plan and balance", async () => {
    const client = stubClient();
    const user = userEvent.setup();
    renderWithProviders(<WebsiteTab />, client);

    await openCustomer(user);

    expect(await screen.findByText("growth")).toBeTruthy();
    expect(screen.getByText("$12.34")).toBeTruthy();
  });

  it("assigns the open chat to me", async () => {
    const client = stubClient();
    const user = userEvent.setup();
    renderWithProviders(<WebsiteTab />, client);

    await openCustomer(user);
    await user.click(await screen.findByRole("button", { name: "Assign to me" }));

    await waitFor(() => {
      const call = client.calls.find(
        (entry) => entry.path === "/api/v1/ops/site/chats/chat-c/assign",
      );
      expect(call?.init.method).toBe("POST");
      expect(call?.init.json).toEqual({ to_me: true });
    });
  });

  it("shows the AI pills in the list and labels AI replies in the transcript", async () => {
    const client = stubClient();
    const user = userEvent.setup();
    renderWithProviders(<WebsiteTab />, client);

    expect(await screen.findByText("AI answering")).toBeTruthy();
    expect(screen.getByText("AI handed off")).toBeTruthy();

    await openCustomer(user);
    expect(await screen.findByText(/^AI assistant ·/)).toBeTruthy();
    expect(screen.getByText("Growth is $29 a month.")).toBeTruthy();
  });
});
