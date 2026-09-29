import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConsoleSupportChat, openSupportChat } from "@/components/shell/ConsoleSupportChat";
import { makeStubClient, renderWithProviders } from "@/test/harness";

/**
 * useAuth is mocked rather than fed through AuthProvider: in the harness the provider has no
 * `/api/v1/auth/me` stub to answer it, so `me` is null and this component would render nothing.
 * Only `useAuth` is replaced - AuthProvider itself is the real one, so renderWithProviders keeps
 * working exactly as the other shell tests use it.
 */
const session = vi.hoisted(() => ({
  client: null as unknown,
  me: null as unknown,
}));

vi.mock("@/auth/AuthContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/auth/AuthContext")>();
  return {
    ...actual,
    useAuth: () => ({ api: session.client, me: session.me, orgId: "o1" }),
  };
});

const ME = {
  full_name: "Dana Diaz",
  email: "dana@acme.test",
  memberships: [{ org_id: "o1", org_name: "Acme" }],
};

function stubClient(extra: Record<string, unknown> = {}) {
  return makeStubClient({
    "/api/v1/support/chat": { chat_id: "chat-1", token: "tok-1", staffed: true },
    ...extra,
  });
}

function signedIn() {
  const client = stubClient();
  session.client = client;
  session.me = ME;
  return client;
}

describe("ConsoleSupportChat", () => {
  beforeEach(() => {
    // A handoff stores its live chat in sessionStorage; do not let one test resume another.
    window.sessionStorage.clear();
  });

  it("stays out of the way until the Help menu asks for it", async () => {
    const client = signedIn();
    renderWithProviders(<ConsoleSupportChat />, client);

    // Nothing at all before the event: no panel and no marketing launcher.
    expect(screen.queryByRole("dialog", { name: "Ringlite assistant" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open chat" })).toBeNull();

    act(() => {
      openSupportChat();
    });

    const dialog = await screen.findByRole("dialog", { name: "Ringlite assistant" });
    expect(within(dialog).getByText("Ringlite support")).toBeTruthy();

    // Console variant: the launcher and the teaser never render, even while open.
    expect(screen.queryByRole("button", { name: "Open chat" })).toBeNull();
    expect(screen.queryByText("Questions about pricing? Ask me.")).toBeNull();
  });

  it("hands a signed-in customer to the team without asking who they are", async () => {
    const client = signedIn();
    renderWithProviders(<ConsoleSupportChat />, client);
    const user = userEvent.setup();

    act(() => {
      openSupportChat();
    });
    const dialog = await screen.findByRole("dialog", { name: "Ringlite assistant" });

    await user.type(within(dialog).getByLabelText("Message"), "I want to talk to a human{Enter}");

    await waitFor(() => {
      expect(client.calls.some((call) => call.path === "/api/v1/support/chat")).toBe(true);
    });

    // The form the site shows is skipped entirely for a customer.
    expect(screen.queryByLabelText("Name")).toBeNull();

    // Signed-in customers use the authenticated endpoint; identity comes from the session.
    expect(client.calls.some((entry) => entry.path === "/api/v1/public/site-chat/handoff")).toBe(false);
    const call = client.calls.find((entry) => entry.path === "/api/v1/support/chat");
    expect(call?.init.method).toBe("POST");
    const body = call?.init.json as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["page", "transcript", "want_person"])
    expect(body.want_person).toBe(true);
    expect(String(body.page).startsWith("[customer] Acme")).toBe(true);
    expect(String(body.page).length).toBeLessThanOrEqual(200);
    expect(await screen.findByText("Connecting you to the team…")).toBeTruthy();
  });

  it("sends a signed-in customer's questions to the authenticated ask endpoint", async () => {
    const client = signedIn();
    const routes = makeStubClient({
      "/api/v1/support/ask": { answer: "Your plan renews on the 1st.", handoff: false },
    });
    client.request = routes.request.bind(routes) as typeof client.request;
    renderWithProviders(<ConsoleSupportChat />, client);
    const user = userEvent.setup();

    act(() => {
      openSupportChat();
    });
    const dialog = await screen.findByRole("dialog", { name: "Ringlite assistant" });
    await user.type(within(dialog).getByLabelText("Message"), "Why is my zebra widget purple{Enter}");

    expect(await screen.findByText("Your plan renews on the 1st.")).toBeTruthy();
    expect(routes.calls.some((c) => c.path === "/api/v1/support/ask")).toBe(true);
    expect(routes.calls.some((c) => c.path === "/api/v1/public/site-chat/ask")).toBe(false);
  });

  it("says the assistant keeps helping when the team is offline and ai is on", async () => {
    const client = makeStubClient({
      "/api/v1/support/chat": { chat_id: "chat-1", token: "tok-1", staffed: false, ai: true },
      "/api/v1/public/site-chat/chat-1/messages": {
        status: "active",
        agent_name: null,
        ai: true,
        messages: [{ id: "a1", role: "agent", text: "Happy to help.", at: "2024-06-01T10:00:00Z", ai: true }],
      },
    });
    session.client = client;
    session.me = ME;
    renderWithProviders(<ConsoleSupportChat />, client);
    const user = userEvent.setup();

    act(() => {
      openSupportChat();
    });
    const dialog = await screen.findByRole("dialog", { name: "Ringlite assistant" });
    await user.type(within(dialog).getByLabelText("Message"), "I want to talk to a human{Enter}");

    expect(
      await screen.findByText(
        "Our team is offline right now and will reply by email, usually within one business day. The Ringlite assistant can keep helping here in the meantime.",
      ),
    ).toBeTruthy();
    // A polled assistant reply is labelled as the assistant, not the team.
    expect(await screen.findByText("Happy to help.")).toBeTruthy();
    expect(screen.getByText("Ringlite Assistant")).toBeTruthy();
    expect(screen.queryByText("Ringlite team")).toBeNull();
  });

  it("hides the panel when the widget's own close button asks it to", async () => {
    const client = signedIn();
    renderWithProviders(<ConsoleSupportChat />, client);
    const user = userEvent.setup();

    act(() => {
      openSupportChat();
    });
    const dialog = await screen.findByRole("dialog", { name: "Ringlite assistant" });

    await user.click(within(dialog).getByRole("button", { name: "Close chat" }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Ringlite assistant" })).toBeNull(),
    );
  });
});
