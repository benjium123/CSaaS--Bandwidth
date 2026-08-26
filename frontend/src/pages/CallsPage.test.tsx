import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
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
});
