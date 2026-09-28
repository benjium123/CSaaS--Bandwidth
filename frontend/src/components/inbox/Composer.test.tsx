import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Composer } from "./Composer";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const MEMBERS = [
  { user_id: "u1", full_name: "Ada Lovelace", email: "ada@example.com", role_name: "Admin" },
  { user_id: "u2", full_name: "Bob Barker", email: "bob@example.com", role_name: "Member" },
];

const TEMPLATE = {
  id: "s1",
  name: "Greeting",
  body: "Hi {{contact.first_name}}, thanks for reaching out.",
  media_asset_ids: [],
  tokens: ["contact.first_name"],
};

describe("Composer reply/note toggle", () => {
  it("toggle shows Reply and Note, Reply is selected by default", () => {
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={vi.fn().mockResolvedValue(undefined)} />, client);

    expect(screen.getByRole("tab", { name: "Reply" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Internal note" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("Note tab is disabled with no threadId, and enabled with one", () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({});

    renderWithProviders(<Composer onSend={onSend} />, client);
    expect(screen.getByRole("tab", { name: "Internal note" })).toBeDisabled();
    cleanup();

    const client2 = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} threadId="t1" />, client2);
    expect(screen.getByRole("tab", { name: "Internal note" })).toBeEnabled();
  });

  it("switching to Note relabels the field to Note and shows the privacy sentence", async () => {
    const client = makeStubClient({ "/api/v1/orgs/current/members": MEMBERS });
    renderWithProviders(
      <Composer onSend={vi.fn().mockResolvedValue(undefined)} threadId="t1" />,
      client,
    );

    await userEvent.click(screen.getByRole("tab", { name: "Internal note" }));

    expect(screen.getByLabelText("Note")).toBeInTheDocument();
    expect(
      screen.getByText("Only your team can see this. It is never sent to the contact."),
    ).toBeInTheDocument();
  });

  it("posting a note POSTs to /api/v1/conversations/t1/notes with body and clears the field, and onSend is NEVER called", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/conversations/t1/notes": (
        _path: string,
        init: RequestInit & { json?: unknown },
      ) => ({
        id: "n1",
        thread_id: "t1",
        author_name: "Me",
        body: (init.json as { body: string }).body,
        mentions: [],
        created_at: new Date().toISOString(),
      }),
    });

    renderWithProviders(<Composer onSend={onSend} threadId="t1" />, client);
    await userEvent.click(screen.getByRole("tab", { name: "Internal note" }));
    await userEvent.type(screen.getByLabelText("Note"), "hello note");
    await userEvent.click(screen.getByRole("button", { name: "Post note" }));

    await waitFor(() =>
      expect(client.calls.some((call) => call.path === "/api/v1/conversations/t1/notes")).toBe(
        true,
      ),
    );

    const call = client.calls.find(
      (candidate) => candidate.path === "/api/v1/conversations/t1/notes",
    );
    expect(call?.init.json).toEqual({ body: "hello note", mention_user_ids: [] });
    expect(screen.getByLabelText("Message")).toHaveValue("");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("a failed note POST renders its message in a role=alert", async () => {
    const client = makeStubClient({
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/conversations/t1/notes": (
        _path: string,
        _init: RequestInit & { json?: unknown },
      ) => {
        throw new Error("note boom");
      },
    });

    renderWithProviders(
      <Composer onSend={vi.fn().mockResolvedValue(undefined)} threadId="t1" />,
      client,
    );
    await userEvent.click(screen.getByRole("tab", { name: "Internal note" }));
    await userEvent.type(screen.getByLabelText("Note"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Post note" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("note boom");
  });

  it("typing @a in note mode lists matching teammates and picking one inserts @Ada Lovelace", async () => {
    const client = makeStubClient({ "/api/v1/orgs/current/members": MEMBERS });
    renderWithProviders(
      <Composer onSend={vi.fn().mockResolvedValue(undefined)} threadId="t1" />,
      client,
    );

    await userEvent.click(screen.getByRole("tab", { name: "Internal note" }));
    await userEvent.type(screen.getByLabelText("Note"), "@a");

    const option = await screen.findByRole("option", { name: "Ada Lovelace" });
    await userEvent.click(option);

    expect(screen.getByLabelText("Note")).toHaveValue("@Ada Lovelace ");
  });

  it("posting after picking sends that member's user_id in mention_user_ids", async () => {
    const client = makeStubClient({
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/conversations/t1/notes": (
        _path: string,
        init: RequestInit & { json?: unknown },
      ) => ({
        id: "n1",
        thread_id: "t1",
        author_name: "Me",
        body: (init.json as { body: string }).body,
        mentions: [],
        created_at: new Date().toISOString(),
      }),
    });

    renderWithProviders(
      <Composer onSend={vi.fn().mockResolvedValue(undefined)} threadId="t1" />,
      client,
    );
    await userEvent.click(screen.getByRole("tab", { name: "Internal note" }));
    await userEvent.type(screen.getByLabelText("Note"), "@a");
    await userEvent.click(await screen.findByRole("option", { name: "Ada Lovelace" }));
    await userEvent.click(screen.getByRole("button", { name: "Post note" }));

    await waitFor(() =>
      expect(client.calls.some((call) => call.path === "/api/v1/conversations/t1/notes")).toBe(
        true,
      ),
    );
    const call = client.calls.find(
      (candidate) => candidate.path === "/api/v1/conversations/t1/notes",
    );
    expect(call?.init.json).toEqual({
      body: "@Ada Lovelace",
      mention_user_ids: ["u1"],
    });
  });

  it("deleting the inserted name before posting sends mention_user_ids: []", async () => {
    const client = makeStubClient({
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/conversations/t1/notes": (
        _path: string,
        init: RequestInit & { json?: unknown },
      ) => ({
        id: "n1",
        thread_id: "t1",
        author_name: "Me",
        body: (init.json as { body: string }).body,
        mentions: [],
        created_at: new Date().toISOString(),
      }),
    });

    renderWithProviders(
      <Composer onSend={vi.fn().mockResolvedValue(undefined)} threadId="t1" />,
      client,
    );
    await userEvent.click(screen.getByRole("tab", { name: "Internal note" }));
    const field = screen.getByLabelText("Note");
    await userEvent.type(field, "@a");
    await userEvent.click(await screen.findByRole("option", { name: "Ada Lovelace" }));
    await userEvent.clear(field);
    await userEvent.type(field, "just text");
    await userEvent.click(screen.getByRole("button", { name: "Post note" }));

    await waitFor(() =>
      expect(client.calls.some((call) => call.path === "/api/v1/conversations/t1/notes")).toBe(
        true,
      ),
    );
    const call = client.calls.find(
      (candidate) => candidate.path === "/api/v1/conversations/t1/notes",
    );
    expect(call?.init.json).toEqual({
      body: "just text",
      mention_user_ids: [],
    });
  });

  it("Enter while the mention list is open picks a teammate and does not post", async () => {
    const client = makeStubClient({
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/conversations/t1/notes": (
        _path: string,
        _init: RequestInit & { json?: unknown },
      ) => ({
        id: "n1",
        thread_id: "t1",
        author_name: "Me",
        body: "",
        mentions: [],
        created_at: new Date().toISOString(),
      }),
    });

    renderWithProviders(
      <Composer onSend={vi.fn().mockResolvedValue(undefined)} threadId="t1" />,
      client,
    );
    await userEvent.click(screen.getByRole("tab", { name: "Internal note" }));
    const field = screen.getByLabelText("Note");
    await userEvent.type(field, "@a");
    await screen.findByRole("option", { name: "Ada Lovelace" });

    await userEvent.keyboard("{Enter}");

    expect(field).toHaveValue("@Ada Lovelace ");
    expect(client.calls.some((call) => call.path === "/api/v1/conversations/t1/notes")).toBe(
      false,
    );
  });

  it("typing / opens the saved-reply list and requests /api/v1/templates?q=hel as you type", async () => {
    const client = makeStubClient({
      "/api/v1/templates": (_path: string, _init: RequestInit & { json?: unknown }) => [
        TEMPLATE,
      ],
    });

    renderWithProviders(<Composer onSend={vi.fn().mockResolvedValue(undefined)} />, client);
    await userEvent.type(screen.getByLabelText("Message"), "/hel");

    expect(
      await screen.findByRole("listbox", { name: "Insert a saved reply" }),
    ).toBeInTheDocument();

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path.includes("/api/v1/templates") && call.path.includes("q=hel"),
        ),
      ).toBe(true),
    );
  });

  it("picking a saved reply puts its body, merge field included, in the field and closes the list", async () => {
    const client = makeStubClient({
      "/api/v1/templates": (_path: string, _init: RequestInit & { json?: unknown }) => [
        TEMPLATE,
      ],
    });

    renderWithProviders(<Composer onSend={vi.fn().mockResolvedValue(undefined)} />, client);
    const field = screen.getByLabelText("Message");
    await userEvent.type(field, "/");
    await userEvent.click(await screen.findByRole("option", { name: /Greeting/ }));

    expect(field).toHaveValue(TEMPLATE.body);
    expect(
      screen.queryByRole("listbox", { name: "Insert a saved reply" }),
    ).not.toBeInTheDocument();
  });

  it("Escape closes the saved-reply list and leaves the typed text alone", async () => {
    const client = makeStubClient({
      "/api/v1/templates": (_path: string, _init: RequestInit & { json?: unknown }) => [
        TEMPLATE,
      ],
    });

    renderWithProviders(<Composer onSend={vi.fn().mockResolvedValue(undefined)} />, client);
    await userEvent.type(screen.getByLabelText("Message"), "/hel");
    await screen.findByRole("listbox", { name: "Insert a saved reply" });

    await userEvent.keyboard("{Escape}");

    expect(
      screen.queryByRole("listbox", { name: "Insert a saved reply" }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toHaveValue("/hel");
  });

  it("shows no segment count in note mode - a note is not sent anywhere", async () => {
    const client = makeStubClient({ "/api/v1/orgs/current/members": MEMBERS });
    renderWithProviders(
      <Composer onSend={vi.fn().mockResolvedValue(undefined)} threadId="t1" />,
      client,
    );

    expect(screen.getByText(/segment/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Internal note" }));
    expect(screen.queryByText(/segment/)).toBeNull();
  });

  it("disables the Note tab on a read-only inbox", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <Composer onSend={vi.fn().mockResolvedValue(undefined)} threadId="t1" disabled />,
      client,
    );

    expect(screen.getByRole("tab", { name: "Internal note" })).toBeDisabled();
  });

  // 2026-09-29: the user asked for visible Saved replies / emoji / save-as buttons in the
  // composer toolbar (docs/design/INBOX_NAV_SPEC.md). Typing "/" still works as before.
  it("Saved replies button opens the saved-reply list", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      "/api/v1/templates": [{ id: "t1", name: "Intro", body: "Hi there", media_asset_ids: [], tokens: [] }],
    });
    renderWithProviders(<Composer onSend={vi.fn().mockResolvedValue(undefined)} />, client);

    await user.click(screen.getByRole("button", { name: "Saved replies" }));
    const list = await screen.findByRole("listbox", { name: "Insert a saved reply" });
    await user.click(await within(list).findByRole("option", { name: /Intro/ }));
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Hi there");
  });

  it("saves the typed message as a saved reply (templates:manage only)", async () => {
    const user = userEvent.setup();
    const perms = ["templates:manage"];
    const me = {
      id: "u1", email: "ada@example.com", full_name: "Ada", permissions: perms,
      memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner", permissions: perms }],
    };
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/me": me,
      "/api/v1/templates": (_p: string, init: RequestInit & { json?: unknown }) =>
        init.method === "POST"
          ? { id: "t9", name: (init.json as { name: string }).name, body: "x", media_asset_ids: [], tokens: [] }
          : [],
    });
    renderWithProviders(<Composer onSend={vi.fn().mockResolvedValue(undefined)} />, client);

    await user.type(screen.getByRole("textbox"), "We can close Friday");
    await user.click(await screen.findByRole("button", { name: "Save as a saved reply" }));
    await user.type(screen.getByRole("textbox", { name: "Saved reply name" }), "Close date");
    await user.click(screen.getByRole("button", { name: "Save reply" }));

    await waitFor(() =>
      expect(client.calls.find((c) => c.init.method === "POST")?.init.json).toEqual({
        name: "Close date",
        body: "We can close Friday",
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent('Saved as "Close date"');
  });

  it("hides save-as-reply without templates:manage", () => {
    renderWithProviders(<Composer onSend={vi.fn().mockResolvedValue(undefined)} />, makeStubClient({}));
    expect(screen.queryByRole("button", { name: "Save as a saved reply" })).toBeNull();
  });

  it("emoji picker inserts at the caret", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={vi.fn().mockResolvedValue(undefined)} />, client);

    await user.type(screen.getByRole("textbox"), "Thanks ");
    await user.click(screen.getByRole("button", { name: "Insert emoji" }));
    const grid = screen.getByRole("group", { name: "Emoji" });
    await user.click(within(grid).getAllByRole("button")[0]);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Thanks \u{1F44D}");
  });

  it("reply mode still sends: Enter calls onSend and does not POST a note", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/conversations/t1/notes": (
        _path: string,
        _init: RequestInit & { json?: unknown },
      ) => ({
        id: "n1",
        thread_id: "t1",
        author_name: "Me",
        body: "",
        mentions: [],
        created_at: new Date().toISOString(),
      }),
    });

    renderWithProviders(<Composer onSend={onSend} threadId="t1" />, client);
    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(onSend).toHaveBeenCalledWith("hello", false));
    expect(client.calls.some((call) => call.path === "/api/v1/conversations/t1/notes")).toBe(
      false,
    );
  });

  it("renders with no threadId and a routeless stub client and still sends", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(onSend).toHaveBeenCalledWith("hello", false));
  });
});
