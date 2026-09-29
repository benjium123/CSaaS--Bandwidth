import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { HelpMenu } from "@/components/shell/HelpMenu";
import { makeStubClient, renderWithProviders } from "@/test/harness";

type User = ReturnType<typeof userEvent.setup>;

const FULL = {
  email: "help@ringlite.io",
  phone: "+15551234567",
  knowledge_base_url: "https://help.ringlite.io",
  whats_new_url: "https://ringlite.io/whats-new",
  status_url: "https://status.ringlite.io",
  terms_url: "https://ringlite.io/terms",
  privacy_url: "https://ringlite.io/privacy",
};

function stub(contacts: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  return makeStubClient({ "/api/v1/support/contacts": contacts, ...extra });
}

async function openMenu(user: User) {
  await user.click(screen.getByRole("button", { name: "Help" }));
  return screen.findByRole("menu", { name: "Help" });
}

describe("HelpMenu", () => {
  it("opens and lists every item for a full contacts payload", async () => {
    const client = stub(FULL);
    renderWithProviders(<HelpMenu />, client);
    const user = userEvent.setup();

    const menu = await openMenu(user);
    expect(menu).toBeTruthy();

    expect((await screen.findByRole("menuitem", { name: "Contact support" })).textContent).toBe("Contact support");

    const email = await screen.findByRole("menuitem", { name: "Email us" });
    expect(email.getAttribute("href")).toBe("mailto:help@ringlite.io");

    const phone = await screen.findByRole("menuitem", { name: /Call us/ });
    expect(phone.getAttribute("href")).toBe("tel:+15551234567");
    expect(phone.textContent).toContain("+15551234567");

    const kb = await screen.findByRole("menuitem", { name: "Knowledge base" });
    expect(kb.getAttribute("href")).toBe("https://help.ringlite.io");
    expect(kb.getAttribute("target")).toBe("_blank");
    expect(kb.getAttribute("rel")).toBe("noreferrer");

    const whatsNew = await screen.findByRole("menuitem", { name: "What's new" });
    expect(whatsNew.getAttribute("href")).toBe("https://ringlite.io/whats-new");

    const status = await screen.findByRole("menuitem", { name: "System status" });
    expect(status.getAttribute("href")).toBe("https://status.ringlite.io");

    const profile = await screen.findByRole("menuitem", { name: "My profile" });
    expect(profile.getAttribute("href")).toBe("/settings/profile");

    expect(screen.getByRole("link", { name: "Terms of Service" }).getAttribute("href")).toBe("https://ringlite.io/terms");
    expect(screen.getByRole("link", { name: "Privacy Policy" }).getAttribute("href")).toBe("https://ringlite.io/privacy");
  });

  it("hides the email and phone items when those contacts are null", async () => {
    const client = stub({ ...FULL, email: null, phone: null });
    renderWithProviders(<HelpMenu />, client);
    const user = userEvent.setup();

    await openMenu(user);

    // Wait for the payload before asserting absence, otherwise the check is vacuous.
    expect(await screen.findByRole("menuitem", { name: "Knowledge base" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "Email us" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /Call us/ })).toBeNull();
  });

  it("sends a support request with subject, body and page and confirms", async () => {
    const client = stub(FULL, { "/api/v1/support/requests": {} });
    renderWithProviders(<HelpMenu />, client);
    const user = userEvent.setup();

    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: "Contact support" }));
    expect(await screen.findByRole("dialog", { name: "Contact support" })).toBeTruthy();

    const sendButton = screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
    expect(sendButton.disabled).toBe(true);

    await user.type(screen.getByLabelText("Subject"), "Cannot sign in");
    await user.type(screen.getByLabelText("Message"), "Locked out after three tries.");
    expect(sendButton.disabled).toBe(false);

    await user.click(sendButton);

    expect(await screen.findByText("Sent - we'll reply by email.")).toBeTruthy();
    expect((screen.getByLabelText("Subject") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Message") as HTMLTextAreaElement).value).toBe("");

    const call = client.calls.find((entry) => entry.path === "/api/v1/support/requests");
    expect(call).toBeTruthy();
    expect(call?.init.method).toBe("POST");
    expect(call?.init.json).toEqual({
      subject: "Cannot sign in",
      body: "Locked out after three tries.",
      page: window.location.pathname,
    });
  });

  it("shows an alert when the request fails", async () => {
    const client = stub(FULL, { "/api/v1/support/requests": new Error("Server exploded") });
    renderWithProviders(<HelpMenu />, client);
    const user = userEvent.setup();

    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: "Contact support" }));

    await user.type(screen.getByLabelText("Subject"), "Help");
    await user.type(screen.getByLabelText("Message"), "Something is broken.");
    await user.click(screen.getByRole("button", { name: "Send" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBeTruthy();
  });

  it("closes the menu on Escape", async () => {
    const client = stub(FULL);
    renderWithProviders(<HelpMenu />, client);
    const user = userEvent.setup();

    await openMenu(user);
    expect(screen.getByRole("menu", { name: "Help" })).toBeTruthy();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Help" })).toBeNull());
  });

  it("asks the console to open the support chat from the first item, and closes the menu", async () => {
    const client = stub(FULL);
    renderWithProviders(<HelpMenu />, client);
    const user = userEvent.setup();
    const dispatch = vi.spyOn(window, "dispatchEvent");

    await openMenu(user);

    const items = screen.getAllByRole("menuitem");
    expect(items[0].textContent).toBe("Chat with us");

    await user.click(items[0]);

    const forwarded = dispatch.mock.calls
      .map(([event]) => event)
      .filter((event) => event instanceof Event && event.type === "ringlite:open-support-chat");
    expect(forwarded).toHaveLength(1);
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Help" })).toBeNull());

    dispatch.mockRestore();
  });

  it("anchors itself on screen in the top bar, with readable text", async () => {
    renderWithProviders(<HelpMenu placement="topbar" />, stub(FULL));
    const menu = await openMenu(userEvent.setup());

    // Measured, not classed: the panel is fixed to the viewport so it cannot be drawn off
    // the right edge or under the bar.
    await waitFor(() => expect(menu.style.position).toBe("fixed"));
    expect(menu.className).not.toContain("bottom-0");
    expect(menu.className).not.toContain("top-full");
    expect(menu.className).toContain("max-h-[calc(100vh-5rem)]");
    expect(menu.className).toContain("overflow-y-auto");
    expect(menu.className).toContain("text-foreground");
  });

  it("keeps the rail placement by default", async () => {
    renderWithProviders(<HelpMenu />, stub(FULL));
    const menu = await openMenu(userEvent.setup());
    expect(menu.className).toContain("bottom-0");
    expect(menu.className).toContain("left-14");
    expect(menu.style.position).toBe("");
  });
});
