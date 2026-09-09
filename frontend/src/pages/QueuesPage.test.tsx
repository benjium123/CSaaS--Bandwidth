import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueuesPage } from "./QueuesPage";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const MEMBER_1 = { user_id: "user-1", full_name: "Ada Lovelace", email: "ada@example.com", role_name: "agent" };

const RING_GROUP_1 = {
  id: "rg-1",
  name: "Sales team",
  strategy: "simultaneous",
  member_user_ids: ["user-1"],
  ring_timeout_seconds: 20,
};

const QUEUE_1 = {
  id: "queue-1",
  name: "Support",
  hold_audio_url: null,
  max_wait_seconds: 300,
  overflow: "voicemail",
  ring_group_id: null,
};

const VOICEMAIL_1 = {
  id: "vm-1",
  call_id: "call-1",
  recording_id: null,
  greeting_node: "vm-greet",
  transcript: null,
  transcript_status: "disabled",
  status: "new",
  created_at: new Date().toISOString(),
};

const BASE_ROUTES = {
  "/api/v1/business-hours": [],
  "/api/v1/ring-groups": [],
  "/api/v1/queues": [],
  "/api/v1/orgs/current/members": [],
  "/api/v1/voicemails": [],
};

describe("QueuesPage", () => {
  it("creates a queue", async () => {
    const client = makeStubClient({
      ...BASE_ROUTES,
      "/api/v1/queues": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return QUEUE_1;
        return [];
      },
    });
    renderWithProviders(<QueuesPage />, client);

    await userEvent.type(await screen.findByLabelText("Queue name"), "Support");
    await userEvent.click(screen.getByRole("button", { name: "Create queue" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/queues" && c.init.method === "POST"),
      ).toBe(true),
    );
    const createCall = client.calls.find(
      (c) => c.path === "/api/v1/queues" && c.init.method === "POST",
    );
    expect(createCall?.init.json).toMatchObject({ name: "Support", overflow: "voicemail" });
  });

  it("renders the voicemail list", async () => {
    const client = makeStubClient({
      ...BASE_ROUTES,
      "/api/v1/voicemails": [VOICEMAIL_1],
    });
    renderWithProviders(<QueuesPage />, client);

    expect(await screen.findByText("Transcription not configured.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mark read" })).toBeInTheDocument();
  });

  it("creates a ring group with a member", async () => {
    const client = makeStubClient({
      ...BASE_ROUTES,
      "/api/v1/orgs/current/members": [MEMBER_1],
      "/api/v1/ring-groups": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return RING_GROUP_1;
        return [];
      },
    });
    renderWithProviders(<QueuesPage />, client);

    await userEvent.type(await screen.findByLabelText("Ring group name"), "Sales team");
    await userEvent.click(await screen.findByLabelText("Ada Lovelace (ada@example.com)"));
    await userEvent.click(screen.getByRole("button", { name: "Create ring group" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/ring-groups" && c.init.method === "POST"),
      ).toBe(true),
    );
    const createCall = client.calls.find(
      (c) => c.path === "/api/v1/ring-groups" && c.init.method === "POST",
    );
    expect(createCall?.init.json).toMatchObject({
      name: "Sales team",
      strategy: "simultaneous",
      member_user_ids: ["user-1"],
    });
  });

  it("shows a retry affordance when queues fail to load, and recovers on retry", async () => {
    let queuesCalls = 0;
    const client = makeStubClient({
      ...BASE_ROUTES,
      "/api/v1/queues": (() => {
        queuesCalls += 1;
        if (queuesCalls === 1) return new Error("Failed to load queues");
        return [QUEUE_1];
      }) as RouteStub,
    });
    renderWithProviders(<QueuesPage />, client);

    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to load queues");

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Support")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("resets the business hours form after a successful save", async () => {
    const client = makeStubClient({
      ...BASE_ROUTES,
      "/api/v1/business-hours": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return { id: "bh-1", name: "Custom", timezone: "America/New_York", schedule: {}, holidays: [] };
        return [];
      },
    });
    renderWithProviders(<QueuesPage />, client);

    const nameInput = await screen.findByLabelText("Business hours name");
    const tzInput = screen.getByLabelText("Timezone");
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, "Custom");
    await userEvent.clear(tzInput);
    await userEvent.type(tzInput, "America/New_York");
    await userEvent.click(screen.getByRole("button", { name: "Save business hours" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/business-hours" && c.init.method === "POST"),
      ).toBe(true),
    );

    await waitFor(() => expect(nameInput).toHaveValue("default"));
    expect(tzInput).toHaveValue("America/Chicago");
  });

  // Item 55
  it("labels the remove-window button so it's reachable by accessible name", async () => {
    const client = makeStubClient(BASE_ROUTES);
    renderWithProviders(<QueuesPage />, client);

    // One "Add window" button per weekday row - Monday is first.
    const addButtons = await screen.findAllByRole("button", { name: "Add window" });
    await userEvent.click(addButtons[0]);

    const removeButton = await screen.findByRole("button", { name: "Remove Mon window 1" });
    await userEvent.click(removeButton);

    expect(screen.queryByLabelText("Mon window 1 open")).not.toBeInTheDocument();
  });
});
