import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProvidersPage } from "./ProvidersPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const BANDWIDTH_LIVE = {
  name: "bandwidth",
  live: true,
  reason: "",
  missing: [],
  enabled_flag: null,
  capabilities: { max_media_bytes: 5_000_000 },
  primary: true,
  state: "closed",
  supports_numbers: true,
  supports_voice: true,
};

const TELNYX_LIVE = {
  name: "telnyx",
  live: true,
  reason: "",
  missing: [],
  enabled_flag: true,
  capabilities: {},
  primary: false,
  state: "open",
  supports_numbers: true,
  supports_voice: true,
};

const TWILIO_MISSING = {
  name: "twilio",
  live: false,
  reason: "Missing required credentials",
  missing: ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"],
  enabled_flag: null,
  capabilities: {},
  primary: false,
  state: "",
  supports_numbers: false,
  supports_voice: false,
};

const POLICY = {
  preference: ["bandwidth", "telnyx"],
  allow_intra_carrier_failover: true,
  allow_cross_carrier_failover: false,
  pinned_carrier: null,
};

function baseRoutes(overrides: Record<string, unknown> = {}) {
  return {
    "/api/v1/routing/catalog": [BANDWIDTH_LIVE, TELNYX_LIVE, TWILIO_MISSING],
    "/api/v1/routing/policy": POLICY,
    ...overrides,
  };
}

describe("ProvidersPage", () => {
  it("renders capability badges and the primary marker for a live carrier", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const bandwidthCard = (await screen.findByText("bandwidth")).closest("li")!;
    expect(within(bandwidthCard).getByText("Live")).toBeInTheDocument();
    expect(within(bandwidthCard).getByText("SMS")).toBeInTheDocument();
    expect(within(bandwidthCard).getByText("MMS")).toBeInTheDocument();
    expect(within(bandwidthCard).getByText("Voice")).toBeInTheDocument();
    expect(within(bandwidthCard).getByText("Numbers")).toBeInTheDocument();
    expect(within(bandwidthCard).getByText("Primary")).toBeInTheDocument();
  });

  it("shows every missing variable name and disables Test for a carrier missing credentials", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const twilioCard = (await screen.findByText("twilio")).closest("li")!;
    expect(within(twilioCard).getByText("TWILIO_ACCOUNT_SID")).toBeInTheDocument();
    expect(within(twilioCard).getByText("TWILIO_AUTH_TOKEN")).toBeInTheDocument();
    expect(within(twilioCard).getByRole("button", { name: "Test credentials" })).toBeDisabled();
  });

  it("probes a carrier and renders the failing detail verbatim, without clearing another carrier's result", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/routing/carriers/": (path: string) => {
          if (path.includes("/bandwidth/")) {
            return { name: "bandwidth", ok: false, detail: "Bandwidth: invalid account credentials", checked: "https://dashboard.bandwidth.com/api/accounts/123" };
          }
          if (path.includes("/telnyx/")) {
            return { name: "telnyx", ok: true, detail: "Telnyx: authenticated", checked: "https://api.telnyx.com/v2/balance" };
          }
          throw new Error(`unexpected probe path ${path}`);
        },
      }),
    );
    renderWithProviders(<ProvidersPage />, client);

    await screen.findByText("bandwidth");
    const bandwidthCard = (await screen.findByText("bandwidth")).closest("li")!;
    const telnyxCard = (await screen.findByText("telnyx")).closest("li")!;

    await userEvent.click(within(bandwidthCard).getByRole("button", { name: "Test credentials" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/routing/carriers/bandwidth/probe"),
      ).toBe(true),
    );

    expect(
      await within(bandwidthCard).findByText("Bandwidth: invalid account credentials"),
    ).toBeInTheDocument();
    expect(within(bandwidthCard).getByText("https://dashboard.bandwidth.com/api/accounts/123")).toBeInTheDocument();

    // Probing carrier A must not clear carrier B's previously-shown result.
    await userEvent.click(within(telnyxCard).getByRole("button", { name: "Test credentials" }));
    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/routing/carriers/telnyx/probe")).toBe(
        true,
      ),
    );
    expect(await within(telnyxCard).findByText("Telnyx: authenticated")).toBeInTheDocument();

    expect(
      within(bandwidthCard).getByText("Bandwidth: invalid account credentials"),
    ).toBeInTheDocument();
  });

  it("visually flags a carrier whose breaker state is open", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const telnyxCard = (await screen.findByText("telnyx")).closest("li")!;
    expect(within(telnyxCard).getByRole("alert")).toHaveTextContent(/open/i);
    expect(within(telnyxCard).getByRole("alert")).toHaveTextContent(/failing/i);
  });
});
