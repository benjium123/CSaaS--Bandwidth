import { describe, expect, it, vi } from "vitest";
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
    "/api/v1/public/site-chat/handoff": { chat_id: "chat-1", token: "tok-1", staffed: true },
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
      expect(client.calls.some((call) => call.path === "/api/v1/public/site-chat/handoff")).toBe(true);
    });

    // The form the site shows is skipped entirely for a customer.
    expect(screen.queryByLabelText("Name")).toBeNull();

    const call = client.calls.find((entry) => entry.path === "/api/v1/public/site-chat/handoff");
    expect(call?.init.method).toBe("POST");
    const body = call?.init.json as Record<string, unknown>;
    expect(body).toMatchObject({
      name: "Dana Diaz",
      email: "dana@acme.test",
      phone: null,
      sms_consent: false,
      reason: "customer",
    });
    expect(String(body.page).startsWith("[customer] Acme")).toBe(true);
    expect(String(body.page).length).toBeLessThanOrEqual(200);
    expect(await screen.findByText("Connecting you to the team…")).toBeTruthy();
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
