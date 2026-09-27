import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { PlatformMessagingHealth } from "./PlatformMessagingHealth";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const PLATFORM_ROWS = {
  rows: [
    {
      org_id: "org-2",
      org_name: "Beta Workspace",
      level: "critical",
      volume: 200,
      delivery_rate: 0.72,
      spam_block_rate: 0.11,
      opt_out_rate: 0.03,
      first_breached_at: "2026-08-28T09:00:00Z",
    },
    {
      org_id: "org-1",
      org_name: "Alpha Workspace",
      level: "warn",
      volume: 100,
      delivery_rate: 0.9,
      spam_block_rate: 0.02,
      opt_out_rate: null,
      first_breached_at: null,
    },
  ],
};

describe("PlatformMessagingHealth", () => {
  it("renders mocked rows in the API order", async () => {
    const client = makeStubClient({
      "/api/v1/platform/messaging/health?days=7": PLATFORM_ROWS,
    });
    renderWithProviders(<PlatformMessagingHealth />, client);

    expect(await screen.findByText("Beta Workspace")).toBeInTheDocument();
    expect(screen.getByText("Alpha Workspace")).toBeInTheDocument();
    expect(screen.getByText("72.0%")).toBeInTheDocument();
    expect(screen.getByText("11.0%")).toBeInTheDocument();
    expect(screen.getByText("3.0%")).toBeInTheDocument();
    expect(screen.getByText("90.0%")).toBeInTheDocument();
    expect(screen.getByText("2.0%")).toBeInTheDocument();
    expect(screen.getAllByText("—")).toHaveLength(2);

    const dataRows = screen.getAllByRole("row").slice(1);
    expect(dataRows[0]).toHaveTextContent("Beta Workspace");
    expect(dataRows[1]).toHaveTextContent("Alpha Workspace");
  });

  it("requests messaging health without the shared ops token header", async () => {
    const client = makeStubClient({
      "/api/v1/platform/messaging/health?days=7": PLATFORM_ROWS,
    });
    const request = vi.spyOn(client, "request");
    renderWithProviders(<PlatformMessagingHealth />, client);

    await screen.findByText("Beta Workspace");

    // The panel rides the signed-in operator's session, so the request is the bare path:
    // no init (and therefore no X-Platform-Ops-Token header) is passed along.
    expect(request).toHaveBeenCalledWith("/api/v1/platform/messaging/health?days=7");
    const [, init] = request.mock.calls[0] as [string, RequestInit | undefined];
    expect(init?.headers).toBeUndefined();
  });

  it("renders an alert when the request fails", async () => {
    const client = makeStubClient({});
    vi.spyOn(client, "request").mockRejectedValue(
      Object.assign(new Error("Operator role required"), { status: 403 }),
    );
    renderWithProviders(<PlatformMessagingHealth />, client);

    // Generous wait so the assertion still holds if the harness leaves react-query retries on.
    const alert = await screen.findByRole("alert", {}, { timeout: 10_000 });
    expect(alert.textContent?.trim()).not.toBe("");
  });
});
