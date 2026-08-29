import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CampaignsPage } from "./CampaignsPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const LIST_1 = {
  id: "list-1",
  name: "Q3 buyers",
  source_filename: "buyers.csv",
  status: "ready",
  total_rows: 3,
  accepted_count: 3,
  invalid_count: 0,
  duplicate_count: 0,
  dnc_count: 0,
  error: null,
  created_at: new Date().toISOString(),
};

const NUMBER_1 = {
  id: "n1",
  e164: "+12145550100",
  carrier: "bandwidth",
  is_active: true,
  capabilities: {},
  number_type: "local",
};

const CAMPAIGN_DRAFT = {
  id: "camp-1",
  name: "August blast",
  channel: "sms",
  list_id: "list-1",
  status: "draft",
  body: "Hi {{first_name}}",
  from_numbers: [],
  rate_per_minute: 6,
  daily_cap: 200,
  respect_warmup: true,
  start_at: null,
  dialer_mode: null,
  parallel_lines: 1,
  local_presence: false,
  max_attempts: 2,
  retry_backoff_minutes: 240,
  created_at: new Date().toISOString(),
};

describe("CampaignsPage", () => {
  it("creates an SMS campaign", async () => {
    const client = makeStubClient({
      // Registered longest-prefix-first: makeStubClient matches via path.startsWith(key)
      // in object key order, and "/api/v1/outbound/campaigns/camp-1" is itself a prefix
      // of the progress route below it.
      "/api/v1/outbound/campaigns/camp-1/progress": {
        campaign_id: "camp-1",
        status: "draft",
        counts: {},
        total: 0,
      },
      "/api/v1/outbound/campaigns/camp-1": CAMPAIGN_DRAFT,
      "/api/v1/outbound/campaigns": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return CAMPAIGN_DRAFT;
        return [];
      },
      "/api/v1/outbound/lists": [LIST_1],
      "/api/v1/numbers": [NUMBER_1],
    });
    renderWithProviders(<CampaignsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New" }));

    await userEvent.type(screen.getByLabelText("Campaign name"), "August blast");
    await userEvent.selectOptions(await screen.findByLabelText("Contact list"), "list-1");
    // userEvent.type treats { and } as special key syntax, so the body is typed without
    // merge-field braces here; the {{merge}} rendering itself is backend behavior (P3
    // template renderer), not something this form computes.
    await userEvent.type(screen.getByLabelText("Message body"), "Hi there");

    await userEvent.click(screen.getByRole("button", { name: "Create campaign" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/outbound/campaigns" && c.init.method === "POST",
        ),
      ).toBe(true),
    );
    const createCall = client.calls.find(
      (c) => c.path === "/api/v1/outbound/campaigns" && c.init.method === "POST",
    );
    expect(createCall?.init.json).toMatchObject({
      name: "August blast",
      channel: "sms",
      list_id: "list-1",
      body: "Hi there",
    });

    expect(await screen.findByText("draft")).toBeInTheDocument();
  });

  it("requires a dialer mode before creating a voice campaign", async () => {
    const client = makeStubClient({
      "/api/v1/outbound/campaigns": [],
      "/api/v1/outbound/lists": [LIST_1],
      "/api/v1/numbers": [],
    });
    renderWithProviders(<CampaignsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New" }));
    await userEvent.type(screen.getByLabelText("Campaign name"), "Dial run");
    await userEvent.selectOptions(await screen.findByLabelText("Contact list"), "list-1");
    await userEvent.click(screen.getByRole("radio", { name: "Voice" }));

    expect(screen.getByRole("button", { name: "Create campaign" })).toBeDisabled();

    await userEvent.selectOptions(screen.getByLabelText("Dialer mode"), "power");
    expect(screen.getByRole("button", { name: "Create campaign" })).not.toBeDisabled();
  });

  it("starts and pauses a campaign, rendering live progress counts", async () => {
    let status = "draft";
    const client = makeStubClient({
      "/api/v1/outbound/campaigns/camp-1/progress": () => ({
        campaign_id: "camp-1",
        status,
        counts: { queued: 2, sent: 1 },
        total: 3,
      }),
      "/api/v1/outbound/campaigns/camp-1/start": () => {
        status = "running";
        return { ...CAMPAIGN_DRAFT, status };
      },
      "/api/v1/outbound/campaigns/camp-1/pause": () => {
        status = "paused";
        return { ...CAMPAIGN_DRAFT, status };
      },
      "/api/v1/outbound/campaigns/camp-1": () => ({ ...CAMPAIGN_DRAFT, status }),
      "/api/v1/outbound/campaigns": [CAMPAIGN_DRAFT],
      "/api/v1/outbound/lists": [LIST_1],
      "/api/v1/numbers": [NUMBER_1],
    });
    renderWithProviders(<CampaignsPage />, client);

    await userEvent.click(await screen.findByText("August blast"));
    expect(await screen.findByText("queued: 2")).toBeInTheDocument();
    expect(screen.getByText("sent: 1")).toBeInTheDocument();

    const startButton = screen.getByRole("button", { name: "Start" });
    expect(startButton).not.toBeDisabled();
    await userEvent.click(startButton);

    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/outbound/campaigns/camp-1/start")).toBe(
        true,
      ),
    );

    const pauseButton = await screen.findByRole("button", { name: "Pause" });
    await waitFor(() => expect(pauseButton).not.toBeDisabled());
    await userEvent.click(pauseButton);

    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/outbound/campaigns/camp-1/pause")).toBe(
        true,
      ),
    );
  });
});
