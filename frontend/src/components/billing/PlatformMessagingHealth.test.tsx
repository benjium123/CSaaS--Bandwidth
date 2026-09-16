import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
  beforeEach(() => {
    sessionStorage.clear();
  });

  it("is locked by default", () => {
    const client = makeStubClient({});
    renderWithProviders(<PlatformMessagingHealth />, client);

    expect(screen.getByText("Messaging health (all workspaces)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unlock" })).toBeInTheDocument();
    expect(
      client.calls.some((c) => c.path === "/api/v1/platform/messaging/health?days=7"),
    ).toBe(false);
  });

  it("renders mocked rows in the API order after entering a token", async () => {
    const client = makeStubClient({
      "/api/v1/platform/messaging/health?days=7": PLATFORM_ROWS,
    });
    renderWithProviders(<PlatformMessagingHealth />, client);

    await userEvent.type(screen.getByLabelText("Platform ops token"), "platform-token");
    await userEvent.click(screen.getByRole("button", { name: "Unlock" }));

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

  it("returns to locked state when the ops token gets a 403", async () => {
    const client = makeStubClient({});
    vi.spyOn(client, "request").mockRejectedValue({ status: 403 });
    renderWithProviders(<PlatformMessagingHealth />, client);

    await userEvent.type(screen.getByLabelText("Platform ops token"), "bad-token");
    await userEvent.click(screen.getByRole("button", { name: "Unlock" }));

    expect(await screen.findByText("That token was not accepted.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unlock" })).toBeInTheDocument();
  });
});
