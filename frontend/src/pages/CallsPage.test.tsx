import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CallsPage } from "./CallsPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const CALL_1 = {
  id: "call-1",
  direction: "outbound",
  contact_e164: "+19725550199",
  our_e164: "+12145550100",
  carrier: "bandwidth",
  status: "completed",
  tag: null,
  answered_at: new Date().toISOString(),
  ended_at: new Date().toISOString(),
  duration_seconds: 65,
  created_at: new Date().toISOString(),
};

const NEW_CALL_DETAIL = {
  id: "call-2",
  direction: "outbound",
  contact_e164: "+19725550111",
  our_e164: "+12145550100",
  carrier: "bandwidth",
  status: "queued",
  tag: null,
  answered_at: null,
  ended_at: null,
  duration_seconds: null,
  created_at: new Date().toISOString(),
  legs: [],
  recordings: [],
};

describe("CallsPage", () => {
  it("renders the call list and places a new call", async () => {
    const client = makeStubClient({
      "/api/v1/numbers": [
        { id: "n1", e164: "+12145550100", carrier: "bandwidth", is_active: true },
      ],
      "/api/v1/calls": (path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return NEW_CALL_DETAIL;
        if (/^\/api\/v1\/calls(\?|$)/.test(path)) return [CALL_1];
        return NEW_CALL_DETAIL;
      },
    });
    renderWithProviders(<CallsPage />, client);

    expect(await screen.findByText("(972) 555-0199")).toBeInTheDocument();
    expect(screen.getByText("completed")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Number to call"), "+19725550111");
    await userEvent.click(screen.getByRole("button", { name: "Place call" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/calls" && c.init.method === "POST"),
      ).toBe(true),
    );
    const createCall = client.calls.find(
      (c) => c.path === "/api/v1/calls" && c.init.method === "POST",
    );
    expect(createCall?.init.json).toEqual({ to: "+19725550111", from: undefined });

    expect(await screen.findByText(/status: queued/)).toBeInTheDocument();
  });

  it("renders the transcript panel with ordered chat bubbles", async () => {
    const detail = {
      ...NEW_CALL_DETAIL,
      id: "call-3",
      contact_e164: "+19725550188",
      status: "completed",
      transcript: [
        { role: "agent", text: "Hello, how can I help?", at_ms: 0 },
        { role: "user", text: "I have a question.", at_ms: 1000 },
      ],
    };
    const listRow = { ...CALL_1, id: "call-3", contact_e164: "+19725550188" };
    const client = makeStubClient({
      "/api/v1/numbers": [],
      "/api/v1/calls": (path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return detail;
        if (/^\/api\/v1\/calls(\?|$)/.test(path)) return [listRow];
        return detail;
      },
    });
    renderWithProviders(<CallsPage />, client);

    await userEvent.click(await screen.findByText("(972) 555-0188"));

    const transcript = await screen.findByLabelText("Transcript");
    const bubbles = within(transcript).getAllByRole("listitem");
    expect(bubbles.map((b) => b.textContent)).toEqual([
      "Hello, how can I help?",
      "I have a question.",
    ]);
  });

  it("does not render a transcript panel when there is nothing to show", async () => {
    const client = makeStubClient({
      "/api/v1/numbers": [
        { id: "n1", e164: "+12145550100", carrier: "bandwidth", is_active: true },
      ],
      "/api/v1/calls": (path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return NEW_CALL_DETAIL;
        if (/^\/api\/v1\/calls(\?|$)/.test(path)) return [CALL_1];
        return NEW_CALL_DETAIL;
      },
    });
    renderWithProviders(<CallsPage />, client);

    await userEvent.click(await screen.findByText("(972) 555-0199"));
    await screen.findByText("Legs");
    expect(screen.queryByLabelText("Transcript")).not.toBeInTheDocument();
  });

  it("sends the AI agent to a live room call", async () => {
    const detail = { ...NEW_CALL_DETAIL, id: "call-4", status: "bridged" };
    const listRow = { ...CALL_1, id: "call-4", status: "bridged" };
    const client = makeStubClient({
      "/api/v1/numbers": [],
      // Must be registered BEFORE the generic "/api/v1/calls" entry: makeStubClient
      // matches routes via path.startsWith(key) in object key order, and this path
      // (correctly, post-F-pre-existing-bug-fix) starts with "/api/v1/calls" too.
      "/api/v1/calls/call-4/agent": { dispatched: "ai", room: "call-call-4", id: "disp-1" },
      "/api/v1/calls": (path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return detail;
        if (/^\/api\/v1\/calls(\?|$)/.test(path)) return [listRow];
        return detail;
      },
    });
    renderWithProviders(<CallsPage />, client);

    await userEvent.click(await screen.findByText("(972) 555-0199"));
    await userEvent.click(await screen.findByRole("button", { name: "Send AI agent" }));

    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/calls/call-4/agent")).toBe(true),
    );
    const dispatchCall = client.calls.find((c) => c.path === "/api/v1/calls/call-4/agent");
    expect(dispatchCall?.init.method).toBe("POST");
    expect(dispatchCall?.init.json).toEqual({ agent_name: "ai" });
    expect(await screen.findByText(/AI agent joined room call-call-4/)).toBeInTheDocument();
  });

  it("surfaces the backend's dispatch error verbatim", async () => {
    const detail = { ...NEW_CALL_DETAIL, id: "call-5", status: "bridged" };
    const listRow = { ...CALL_1, id: "call-5", status: "bridged" };
    const client = makeStubClient({
      "/api/v1/numbers": [],
      // See the ordering note in the previous test - the specific dispatch route must
      // come before the generic "/api/v1/calls" prefix match.
      "/api/v1/calls/call-5/agent": new Error("Agents can only join room calls (via=room)"),
      "/api/v1/calls": (path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return detail;
        if (/^\/api\/v1\/calls(\?|$)/.test(path)) return [listRow];
        return detail;
      },
    });
    renderWithProviders(<CallsPage />, client);

    await userEvent.click(await screen.findByText("(972) 555-0199"));
    await userEvent.click(await screen.findByRole("button", { name: "Send AI agent" }));

    expect(
      await screen.findByText("Agents can only join room calls (via=room)"),
    ).toBeInTheDocument();
  });
});
