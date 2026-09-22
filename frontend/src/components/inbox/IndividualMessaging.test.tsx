import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Composer } from "./Composer";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const MEMBERS = [
  { user_id: "u1", full_name: "Ada Lovelace", email: "ada@example.com", role_name: "Admin" },
];

function meWith(accountType: "business" | "individual") {
  return {
    id: "me-1",
    email: "me@example.com",
    full_name: "Me",
    permissions: ["inbox:send"],
    memberships: [
      {
        org_id: "org-1",
        org_name: "Org One",
        org_slug: "org-one",
        role_name: "owner",
        account_type: accountType,
      },
    ],
  };
}

function noteRoute() {
  return (_path: string, init: RequestInit & { json?: unknown }) => ({
    id: "n1",
    thread_id: "t1",
    author_name: "Me",
    body: (init.json as { body: string }).body,
    mentions: [],
    created_at: new Date().toISOString(),
  });
}

describe("Composer individual (calling-only) workspace", () => {
  it("individual with a thread can type and Post note -> notes endpoint, onSend never called", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({
      "/api/v1/auth/me": meWith("individual"),
      "/api/v1/orgs/current/members": MEMBERS,
      "/api/v1/conversations/t1/notes": noteRoute(),
    });

    renderWithProviders(<Composer onSend={onSend} threadId="t1" />, client);

    // The composer lands on note mode for a calling-only workspace with a thread.
    await userEvent.click(await screen.findByRole("tab", { name: "Internal note" }));
    const noteField = await screen.findByLabelText("Note");
    await userEvent.type(noteField, "hello note");

    const postButton = screen.getByRole("button", { name: "Post note" });
    expect(postButton).toBeEnabled();
    await userEvent.click(postButton);

    await waitFor(() =>
      expect(client.calls.some((call) => call.path === "/api/v1/conversations/t1/notes")).toBe(
        true,
      ),
    );
    const call = client.calls.find(
      (candidate) => candidate.path === "/api/v1/conversations/t1/notes",
    );
    expect(call?.init.json).toEqual({ body: "hello note", mention_user_ids: [] });
    expect(onSend).not.toHaveBeenCalled();
  });

  it.each(["individual", "business"] as const)("%s workspace can send through the registered-number path", async (accountType) => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({
      "/api/v1/auth/me": meWith(accountType),
    });

    renderWithProviders(<Composer onSend={onSend} />, client);

    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Reply" })).toBeEnabled(),
    );
    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(onSend).toHaveBeenCalledWith("hello", false));
  });
});
