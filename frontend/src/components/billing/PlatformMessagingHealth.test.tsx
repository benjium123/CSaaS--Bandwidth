import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { PlatformMessagingHealth } from "./PlatformMessagingHealth";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const HEALTH_PATH = "/api/v1/platform/messaging/health?days=7";
const DIGESTS_PATH = "/api/v1/platform/messaging/digests";

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

const PLATFORM_DIGESTS = [
  {
    org_id: "org-2",
    org_name: "Beta Workspace",
    enabled: true,
    hour: 7,
    tz: "America/Chicago",
    recipients: ["ops@beta.test"],
    default_recipients: ["owners@beta.test"],
    last_sent_at: "2026-08-28T13:00:00Z",
  },
  {
    org_id: "org-1",
    org_name: "Alpha Workspace",
    enabled: false,
    hour: 9,
    tz: "America/New_York",
    recipients: ["ops@alpha.test"],
    default_recipients: ["owners@alpha.test"],
    last_sent_at: null,
  },
];

type StubClient = ReturnType<typeof makeStubClient>;

/**
 * Routes api.request by "<METHOD> <path>". The panel now issues two GETs (health + digests)
 * and PUT/POST mutations, so the bare URL map the original tests use cannot tell them apart.
 * A route value that is an Error rejects the request.
 */
function stubRequests(client: StubClient, routes: Record<string, unknown>) {
  const spy = vi.spyOn(client, "request");
  const implementation = async (path: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const key = `${method} ${path}`;
    if (!(key in routes)) {
      throw new Error(`Unexpected request: ${key}`);
    }
    const route = routes[key];
    if (route instanceof Error) throw route;
    return route;
  };
  spy.mockImplementation(implementation as never);
  return spy;
}

describe("PlatformMessagingHealth", () => {
  it("renders mocked rows in the API order", async () => {
    const client = makeStubClient({
      "/api/v1/platform/messaging/health?days=7": PLATFORM_ROWS,
      "/api/v1/platform/messaging/digests": [],
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
      "/api/v1/platform/messaging/digests": [],
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

  it("renders a Daily email checkbox per row reflecting the stored digest", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      [`GET ${HEALTH_PATH}`]: PLATFORM_ROWS,
      [`GET ${DIGESTS_PATH}`]: PLATFORM_DIGESTS,
    });
    renderWithProviders(<PlatformMessagingHealth />, client);

    await waitFor(() => {
      expect(
        screen.getByRole("checkbox", { name: "Daily email for Beta Workspace" }),
      ).toBeChecked();
    });
    expect(
      screen.getByRole("checkbox", { name: "Daily email for Alpha Workspace" }),
    ).not.toBeChecked();
  });

  it("sends the PUT with default hour, tz and recipients for an org with no digest row", async () => {
    const client = makeStubClient({});
    const request = stubRequests(client, {
      [`GET ${HEALTH_PATH}`]: PLATFORM_ROWS,
      [`GET ${DIGESTS_PATH}`]: [PLATFORM_DIGESTS[0]],
      "PUT /api/v1/platform/orgs/org-1/delivery-digest": {},
    });
    renderWithProviders(<PlatformMessagingHealth />, client);

    // Wait for the digests list so org-1 is genuinely "no row" and not "not loaded yet".
    await waitFor(() => {
      expect(
        screen.getByRole("checkbox", { name: "Daily email for Beta Workspace" }),
      ).toBeChecked();
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "Daily email for Alpha Workspace" }));

    await waitFor(() => {
      expect(
        (request.mock.calls as [string, RequestInit | undefined][]).some(
          ([path, init]) =>
            path === "/api/v1/platform/orgs/org-1/delivery-digest" && init?.method === "PUT",
        ),
      ).toBe(true);
    });

    const putCall = (request.mock.calls as [string, RequestInit | undefined][]).find(
      ([path, init]) =>
        path === "/api/v1/platform/orgs/org-1/delivery-digest" && init?.method === "PUT",
    );
    expect(putCall).toBeDefined();
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
      enabled: true,
      hour: 8,
      tz: "America/Chicago",
      recipients: [],
    });
  });

  it("shows the error text when the delivery-digest PUT is forbidden", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      [`GET ${HEALTH_PATH}`]: PLATFORM_ROWS,
      [`GET ${DIGESTS_PATH}`]: PLATFORM_DIGESTS,
      "PUT /api/v1/platform/orgs/org-1/delivery-digest": Object.assign(
        new Error("Operator role required"),
        { status: 403 },
      ),
    });
    renderWithProviders(<PlatformMessagingHealth />, client);

    await waitFor(() => {
      expect(
        screen.getByRole("checkbox", { name: "Daily email for Beta Workspace" }),
      ).toBeChecked();
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "Daily email for Alpha Workspace" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Operator role required");
  });

  it("emails the ops report from the header", async () => {
    const client = makeStubClient({});
    const request = stubRequests(client, {
      [`GET ${HEALTH_PATH}`]: PLATFORM_ROWS,
      [`GET ${DIGESTS_PATH}`]: PLATFORM_DIGESTS,
      "POST /api/v1/platform/messaging/ops-digest/test": { sent: true },
    });
    renderWithProviders(<PlatformMessagingHealth />, client);

    fireEvent.click(await screen.findByRole("button", { name: "Email me the ops report" }));

    expect(await screen.findByText("Sent. Check your inbox.")).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith("/api/v1/platform/messaging/ops-digest/test", {
      method: "POST",
    });
  });

  it("shows the per-row test result when nothing was sent", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      [`GET ${HEALTH_PATH}`]: PLATFORM_ROWS,
      [`GET ${DIGESTS_PATH}`]: PLATFORM_DIGESTS,
      "POST /api/v1/platform/orgs/org-2/delivery-digest/test": { sent: false, day: null },
    });
    renderWithProviders(<PlatformMessagingHealth />, client);

    fireEvent.click(await screen.findByRole("button", { name: "Send test for Beta Workspace" }));

    expect(await screen.findByText("No texts in 7 days")).toBeInTheDocument();
  });

  it("still renders the health table when the digests request fails", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      [`GET ${HEALTH_PATH}`]: PLATFORM_ROWS,
      [`GET ${DIGESTS_PATH}`]: Object.assign(new Error("Digests unavailable"), { status: 500 }),
    });
    renderWithProviders(<PlatformMessagingHealth />, client);

    const betaLabel = await screen.findByText("Beta Workspace");
    const betaRow = betaLabel.closest("tr");
    expect(betaRow).not.toBeNull();
    if (!betaRow) throw new Error("Beta Workspace row not found");
    expect(screen.getByText("Alpha Workspace")).toBeInTheDocument();

    // The digests query retries before it settles into an error, so give it the same
    // generous window the existing failure test uses.
    await waitFor(
      () => {
        expect(within(betaRow).getByText("—")).toBeInTheDocument();
      },
      { timeout: 10_000 },
    );
    expect(within(betaRow).queryByRole("checkbox")).not.toBeInTheDocument();
  });
});
