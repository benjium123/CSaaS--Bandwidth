import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueuesPage } from "./QueuesPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

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
});
