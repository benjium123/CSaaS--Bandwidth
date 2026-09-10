import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PlatformBillingOps } from "./PlatformBillingOps";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const CAPABILITIES = {
  permissions: ["settings:read"],
  org: {
    has_provider: true,
    has_number: true,
    member_count: 2,
    registration_state: "approved",
  },
};

const RATES = [
  { provider: "openai", metric: "ai_voice_seconds", cost_micros: 300, price_micros: 600 },
  { provider: "openai", metric: "stt_seconds", cost_micros: 100, price_micros: 150 },
];

const ORG_1 = {
  org_id: "org-1",
  name: "Acme",
  ai_markup_bps: null,
  ai_platform_fee_per_minute_micros: null,
  balance_micros: 123_456_000,
};

const MARGIN_REPORT = {
  items: [
    { day: "2025-01-01", price_micros: 1_000_000, cost_micros: 400_000, margin_micros: 600_000 },
    { day: "2025-01-02", price_micros: 1_000_000, cost_micros: 300_000, margin_micros: 700_000 },
  ],
};

function baseRoutes(overrides: Record<string, RouteStub | unknown> = {}) {
  return {
    "/api/v1/me/capabilities": CAPABILITIES,
    "/api/v1/platform/billing/rates": RATES,
    "/api/v1/platform/billing/orgs/org-1": ORG_1,
    "/api/v1/platform/billing/orgs/org-1/adjustments": undefined,
    "/api/v1/platform/billing/margin": MARGIN_REPORT,
    ...overrides,
  };
}

async function renderOpen(client: ReturnType<typeof makeStubClient>) {
  renderWithProviders(<PlatformBillingOps />, client);
  await userEvent.click(screen.getByRole("button", { name: "Platform operations" }));
}

async function openAndUnlock(client: ReturnType<typeof makeStubClient>) {
  renderWithProviders(<PlatformBillingOps />, client);
  await userEvent.click(screen.getByRole("button", { name: "Platform operations" }));
  await userEvent.type(await screen.findByLabelText("Platform operations token"), "t0ken");
  await userEvent.click(screen.getByRole("button", { name: "Unlock" }));
}

async function loadWorkspace(client: ReturnType<typeof makeStubClient>, orgId = "org-1") {
  await openAndUnlock(client);
  await userEvent.type(await screen.findByLabelText("Workspace id"), orgId);
  await userEvent.click(screen.getByRole("button", { name: "Load" }));
}

describe("PlatformBillingOps", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
  });

  it("locked by default: renders the operator gate and makes no platform-billing requests", async () => {
    const client = makeStubClient(baseRoutes());
    await renderOpen(client);

    expect(screen.getByText("For platform operators.")).toBeInTheDocument();
    expect(
      client.calls.filter((call) => call.path.startsWith("/api/v1/platform/billing")),
    ).toHaveLength(0);
  });

  it("locked state contains neither cost nor margin", async () => {
    const client = makeStubClient(baseRoutes());
    await renderOpen(client);

    expect(document.body.textContent ?? "").not.toMatch(/cost|margin/i);
  });

  it("unlocking sends the token header on ops requests", async () => {
    const client = makeStubClient(baseRoutes());
    await openAndUnlock(client);

    await waitFor(() => {
      expect(
        client.calls.some((call) => call.path.startsWith("/api/v1/platform/billing")),
      ).toBe(true);
    });

    const opsCalls = client.calls.filter((call) =>
      call.path.startsWith("/api/v1/platform/billing"),
    );
    expect(opsCalls.length).toBeGreaterThan(0);
    for (const call of opsCalls) {
      expect(
        (call.init.headers as Record<string, string>)["X-Platform-Ops-Token"],
      ).toBe("t0ken");
    }
  });

  it("stores the token in sessionStorage and Lock removes it and hides the tables", async () => {
    const client = makeStubClient(baseRoutes());
    await openAndUnlock(client);

    await waitFor(() => {
      expect(sessionStorage.getItem("csaas.platform.ops.token")).toBe("t0ken");
    });

    await userEvent.click(screen.getByRole("button", { name: "Lock" }));

    expect(sessionStorage.getItem("csaas.platform.ops.token")).toBeNull();
    expect(screen.getByText("For platform operators.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("never renders the token on screen after unlocking", async () => {
    const client = makeStubClient(baseRoutes());
    await openAndUnlock(client);

    await waitFor(() => {
      expect(
        client.calls.some((call) => call.path.startsWith("/api/v1/platform/billing")),
      ).toBe(true);
    });

    expect(document.body.textContent ?? "").not.toContain("t0ken");
  });

  it("loading a workspace by id shows its balance and lets the operator PATCH null margin", async () => {
    const client = makeStubClient(baseRoutes());
    await loadWorkspace(client);

    await waitFor(() => expect(screen.getByText("$123.46")).toBeInTheDocument());

    // org-1 starts with ai_markup_bps: null, so the base draft value is "" - the Save
    // button stays disabled until the draft actually differs from that base.
    const marginInput = await screen.findByLabelText("Acme margin");
    await userEvent.type(marginInput, "25");
    await waitFor(() => expect(screen.getByRole("button", { name: "Save Acme" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Save Acme" }));

    await waitFor(() => {
      const patch = client.calls.find(
        (call) =>
          call.path === "/api/v1/platform/billing/orgs/org-1" &&
          (call.init.method ?? "GET") === "PATCH",
      );
      expect(patch).toBeDefined();
      expect(patch?.init.json).toEqual(expect.objectContaining({ ai_markup_bps: 2500 }));
    });
  });

  it("keeps Apply disabled until reason and amount are set and POSTs to the nested per-org path", async () => {
    const client = makeStubClient(baseRoutes());
    await openAndUnlock(client);

    await userEvent.type(await screen.findByLabelText("Workspace id"), "org-1");

    const applyButton = screen.getByRole("button", { name: "Apply" });
    expect(applyButton).toBeDisabled();

    await userEvent.type(screen.getByLabelText("Amount in dollars"), "-4.50");
    await userEvent.type(screen.getByLabelText("Reason"), "Test adjustment");

    await waitFor(() => expect(applyButton).toBeEnabled());
    await userEvent.click(applyButton);

    await waitFor(() => {
      const post = client.calls.find(
        (call) =>
          call.path === "/api/v1/platform/billing/orgs/org-1/adjustments" &&
          (call.init.method ?? "GET") === "POST",
      );
      expect(post).toBeDefined();
      expect(post?.init.json).toEqual({
        org_id: "org-1",
        entry_type: "adjustment",
        amount_micros: -4_500_000,
        note: "Test adjustment",
      });
    });
  });

  it("renders the margin table in dollars with a total row", async () => {
    const client = makeStubClient(baseRoutes());
    await openAndUnlock(client);

    const marginSection = (await screen.findByRole("heading", { name: "Margin report" }))
      .closest("section");
    expect(marginSection).not.toBeNull();

    // The per-day rows and the total row share dollar strings ($0.70 is both day 2's
    // margin and the 30-day cost total), so the totals are asserted against the footer
    // row's own cells - a document-wide getByText would match more than one node.
    await waitFor(() => {
      expect(
        within(marginSection as HTMLElement).getByRole("cell", { name: "Total" }),
      ).toBeInTheDocument();
    });

    const section = marginSection as HTMLElement;
    const totalRow = within(section).getByRole("cell", { name: "Total" }).closest("tr");
    expect(totalRow).not.toBeNull();
    const totals = within(totalRow as HTMLElement)
      .getAllByRole("cell")
      .map((cell) => cell.textContent);
    expect(totals).toEqual(["Total", "$2.00", "$0.70", "$1.30"]);

    // And a per-day row still renders dollars, not micros.
    const dayRow = within(section).getByRole("cell", { name: "2025-01-02" }).closest("tr");
    const dayCells = within(dayRow as HTMLElement)
      .getAllByRole("cell")
      .map((cell) => cell.textContent);
    expect(dayCells).toEqual(["2025-01-02", "$1.00", "$0.30", "$0.70"]);
  });

  it("shows a single alert and no table when an ops query 401s", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/platform/billing/rates": new Error("Request failed with 401"),
      }),
    );
    await openAndUnlock(client);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("That token was not accepted.");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  // 403 is what the backend SHOULD send for a rejected ops token, because ApiClient turns
  // any 401 into a whole-app logout. This asserts the rejected-token branch is reached by
  // status, not only by sniffing "401" out of a message string.
  it("treats a 403 from an ops query as a rejected token and keeps Lock usable", async () => {
    const rejected = Object.assign(new Error("Forbidden"), { status: 403 });
    const client = makeStubClient(baseRoutes({ "/api/v1/platform/billing/rates": rejected }));
    await openAndUnlock(client);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("That token was not accepted.");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Lock" })).toBeEnabled();
  });
});
