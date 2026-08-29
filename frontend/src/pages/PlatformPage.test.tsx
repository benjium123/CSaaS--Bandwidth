import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PlatformPage } from "./PlatformPage";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const CREATED_KEY = {
  id: "key-1",
  name: "CI key",
  prefix: "csk_abcd1234",
  scopes: ["reports:read"],
  status: "active",
  expires_at: null,
  last_used_at: null,
  created_at: new Date().toISOString(),
  key: "csk_abcd1234_verysecretplaintextvalue",
};

const CREATED_ENDPOINT = {
  id: "ep-1",
  url: "https://example.com/hooks",
  event_types: ["call.completed"],
  status: "active",
  failure_streak: 0,
  created_at: new Date().toISOString(),
  secret: "whsec_verysecretplaintextvalue",
};

const AUDIT_ROWS = {
  items: [
    {
      id: "audit-1",
      actor_user_id: "user-1",
      actor_api_key_id: null,
      action: "apikey.created",
      target_type: "api_key",
      target_id: "key-1",
      detail: { name: "CI key" },
      created_at: new Date().toISOString(),
    },
  ],
  next_cursor: null,
};

function baseRoutes(overrides: Record<string, RouteStub | unknown> = {}) {
  return {
    "/api/v1/api-keys": (((_path, init) => {
      if (init.method === "POST") return CREATED_KEY;
      return [];
    }) as RouteStub),
    "/api/v1/webhook-endpoints": (((_path, init) => {
      if (init.method === "POST") return CREATED_ENDPOINT;
      return [];
    }) as RouteStub),
    "/api/v1/audit": AUDIT_ROWS,
    "/api/v1/usage/reconciliation": { date: "2026-08-29", items: [] },
    "/api/v1/usage": [],
    ...overrides,
  };
}

describe("PlatformPage", () => {
  it("creates an API key and shows the full key exactly once", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<PlatformPage />, client);

    await screen.findByText("No API keys yet.");

    await userEvent.type(screen.getByLabelText("Key name"), "CI key");
    await userEvent.click(screen.getByLabelText("reports:read"));
    await userEvent.click(screen.getByRole("button", { name: "Create key" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/api-keys" && c.init.method === "POST"),
      ).toBe(true),
    );
    const postCall = client.calls.find(
      (c) => c.path === "/api/v1/api-keys" && c.init.method === "POST",
    );
    expect(postCall?.init.json).toEqual({ name: "CI key", scopes: ["reports:read"] });

    expect(await screen.findByDisplayValue(CREATED_KEY.key)).toBeInTheDocument();
    expect(screen.getByText(/shown once/i)).toBeInTheDocument();
  });

  it("creates a webhook endpoint and shows the signing secret exactly once", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<PlatformPage />, client);

    await screen.findByText("No webhook endpoints yet.");

    await userEvent.type(screen.getByLabelText("Endpoint URL"), "https://example.com/hooks");
    await userEvent.click(screen.getByLabelText("call.completed"));
    await userEvent.click(screen.getByRole("button", { name: "Create endpoint" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/webhook-endpoints" && c.init.method === "POST",
        ),
      ).toBe(true),
    );
    const postCall = client.calls.find(
      (c) => c.path === "/api/v1/webhook-endpoints" && c.init.method === "POST",
    );
    expect(postCall?.init.json).toEqual({
      url: "https://example.com/hooks",
      event_types: ["call.completed"],
    });

    expect(await screen.findByDisplayValue(CREATED_ENDPOINT.secret)).toBeInTheDocument();
    expect(screen.getByText(/shown once/i)).toBeInTheDocument();
  });

  it("renders audit log rows from the API", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<PlatformPage />, client);

    expect(await screen.findByText("apikey.created")).toBeInTheDocument();
    const table = screen.getByText("apikey.created").closest("table") as HTMLElement;
    expect(within(table).getByText(/api_key/)).toBeInTheDocument();
  });
});
