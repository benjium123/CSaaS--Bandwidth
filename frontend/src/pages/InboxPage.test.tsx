import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InboxPage } from "./InboxPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

function makeItem(overrides: Record<string, unknown> = {}) {
  return {
    thread: {
      id: "t1",
      our_e164: "+12145550100",
      contact_e164: "+19725550199",
      status: "open",
      assigned_user_id: null,
      last_message_at: new Date().toISOString(),
    },
    last_message: null,
    unread: 0,
    contact: { id: "c1", display_name: "Ada Lovelace" },
    assignee: null,
    labels: [],
    ...overrides,
  };
}

function baseRoutes(overrides: Record<string, unknown> = {}) {
  return {
    "/api/v1/inbox/threads": { items: [makeItem()], next_cursor: null },
    "/api/v1/tags": [],
    "/api/v1/messages": [],
    "/api/v1/threads/t1/read": undefined,
    ...overrides,
  };
}

describe("InboxPage - AI state (plan DR-5)", () => {
  it("shows the AI chip on an active thread and takes over on click", async () => {
    let aiState = "active";
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/threads/t1/ai": (_path: string, init: RequestInit & { json?: unknown }) => {
          if ((init.method ?? "GET") === "POST") {
            aiState = (init.json as { state: string }).state;
          }
          return { id: "t1", ai_state: aiState };
        },
      }),
    );

    renderWithProviders(<InboxPage />, client);

    await userEvent.click(await screen.findByText("Ada Lovelace"));

    expect(await screen.findByText("AI")).toBeInTheDocument();
    const takeOver = await screen.findByRole("button", { name: "Take over" });
    await userEvent.click(takeOver);

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/threads/t1/ai" && c.init.method === "POST",
        ),
      ).toBe(true),
    );
    const postCall = client.calls.find(
      (c) => c.path === "/api/v1/threads/t1/ai" && c.init.method === "POST",
    );
    expect((postCall?.init.json as { state: string }).state).toBe("handed_off");
    expect(await screen.findByText("AI paused")).toBeInTheDocument();
  });

  it("shows Re-arm AI on a handed-off thread and re-arms on click", async () => {
    let aiState = "handed_off";
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/threads/t1/ai": (_path: string, init: RequestInit & { json?: unknown }) => {
          if ((init.method ?? "GET") === "POST") {
            aiState = (init.json as { state: string }).state;
          }
          return { id: "t1", ai_state: aiState };
        },
      }),
    );

    renderWithProviders(<InboxPage />, client);

    await userEvent.click(await screen.findByText("Ada Lovelace"));

    expect(await screen.findByText("AI paused")).toBeInTheDocument();
    const rearm = await screen.findByRole("button", { name: "Re-arm AI" });
    await userEvent.click(rearm);

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/threads/t1/ai" && c.init.method === "POST",
        ),
      ).toBe(true),
    );
    const postCall = client.calls.find(
      (c) => c.path === "/api/v1/threads/t1/ai" && c.init.method === "POST",
    );
    expect((postCall?.init.json as { state: string }).state).toBe("active");
    expect(await screen.findByText("AI")).toBeInTheDocument();
  });

  it("shows no chip or button when the thread has never had AI touch it", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/threads/t1/ai": { id: "t1", ai_state: "off" },
      }),
    );

    renderWithProviders(<InboxPage />, client);

    await userEvent.click(await screen.findByText("Ada Lovelace"));

    // Give the ai-state query a chance to resolve before asserting its absence.
    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/threads/t1/ai")).toBe(true),
    );
    expect(screen.queryByText("AI")).not.toBeInTheDocument();
    expect(screen.queryByText("AI paused")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Take over" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Re-arm AI" })).not.toBeInTheDocument();
  });
});
