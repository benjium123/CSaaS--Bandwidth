import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProvidersPage } from "./ProvidersPage";
import { ApiError } from "@/api/client";
import { PROVIDER_FIELDS, SECRET_MASK, type ProviderAccount, type ProviderName } from "@/api/providers";
import type { NumberOut } from "@/api/hooks";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import backendFieldSnapshot from "@/api/providerFields.snapshot.json";

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

const SPEND_SUMMARY = {
  total_micros: 5_000_000,
  total_usd: "$5.00",
  by_provider: {
    telnyx: {
      cost_micros: 5_000_000,
      by_metric: { sms_out: { quantity: 500, cost_micros: 5_000_000 } },
      numbers: [{ number_id: "n1", e164: "+12145550100", cost_micros: 5_000_000 }],
    },
  },
};

const PROVIDER_RATES = [
  {
    provider: "telnyx",
    metric: "sms_out",
    unit_cost_micros: 4000,
    default_unit_cost_micros: 4000,
    is_override: false,
    currency: "USD",
  },
];

const NUMBERS: NumberOut[] = [
  {
    id: "n1",
    e164: "+12145550100",
    number_type: "local",
    carrier: "telnyx",
    status: "active",
    is_active: true,
    capabilities: {},
    registration: "approved",
    registration_detail: "",
    campaign_id: null,
  },
  {
    id: "n2",
    e164: "+12145550101",
    number_type: "local",
    carrier: "signalwire",
    status: "active",
    is_active: true,
    capabilities: {},
    registration: "approved",
    registration_detail: "",
    campaign_id: null,
  },
];

const ME_OWNER = {
  id: "u1",
  email: "owner@example.com",
  full_name: "Owner Example",
  memberships: [{ org_id: "org-1", org_name: "Test Org", org_slug: "test-org", role_name: "owner" }],
};

const ACCOUNTS: ProviderAccount[] = [
  {
    id: "p-telnyx",
    provider: "telnyx",
    label: "Telnyx prod",
    status: "unverified",
    last_probe_at: null,
    last_probe_detail: null,
    credentials: {
      api_key: SECRET_MASK,
      public_key: "pk-123",
      messaging_profile_id: "mp-1",
      voice_connection_id: "vc-1",
    },
  },
  {
    id: "p-twilio",
    provider: "twilio",
    label: "Twilio prod",
    status: "active",
    last_probe_at: "2025-01-01T00:00:00Z",
    last_probe_detail: "Twilio: authenticated",
    credentials: {
      account_sid: "AC-original",
      auth_token: SECRET_MASK,
      messaging_service_sid: "MS-original",
    },
  },
  {
    id: "p-plivo",
    provider: "plivo",
    label: "Plivo prod",
    status: "failed",
    last_probe_at: "2025-01-01T00:00:00Z",
    last_probe_detail: "Plivo: invalid credentials",
    credentials: {
      auth_id: "auth-id",
      auth_token: SECRET_MASK,
      powerpack_uuid: "powerpack-1",
    },
  },
  {
    id: "p-signalwire",
    provider: "signalwire",
    label: "SignalWire prod",
    status: "disabled",
    last_probe_at: null,
    last_probe_detail: "Disabled by admin",
    credentials: {
      project_id: "proj-1",
      api_token: SECRET_MASK,
      space_url: "example.signalwire.com",
    },
  },
];

function baseRoutes(overrides: Record<string, unknown> = {}) {
  return {
    // NOTE: the stub harness matches routes via `path.startsWith(key)`, checking keys in
    // definition order. The id-scoped route MUST be defined before the bare list/create
    // route below, or every /provider-accounts/<id>... call (PATCH, probe, DELETE) would
    // incorrectly match the list route instead.
    "/api/v1/provider-accounts/": (path: string) => {
      const parts = path.split("/");
      const id = parts[4];
      const isProbe = parts[5] === "probe";
      const provider: ProviderName = "twilio";
      return {
        id,
        provider,
        label: "Twilio prod",
        status: "active" as const,
        last_probe_at: isProbe ? "2025-01-01T00:00:00Z" : null,
        last_probe_detail: isProbe ? "Twilio: authenticated" : null,
        credentials: {
          account_sid: "AC-updated",
          auth_token: SECRET_MASK,
          messaging_service_sid: "MS-updated",
        },
      };
    },
    "/api/v1/provider-accounts": ACCOUNTS,
    "/api/v1/routing/catalog": [BANDWIDTH_LIVE, TELNYX_LIVE, TWILIO_MISSING],
    "/api/v1/routing/policy": POLICY,
    "/api/v1/numbers": NUMBERS,
    "/api/v1/auth/me": ME_OWNER,
    "/api/v1/spend/summary": SPEND_SUMMARY,
    "/api/v1/provider-rates": PROVIDER_RATES,
    ...overrides,
  };
}

describe("ProvidersPage", () => {
  it("renders five provider account cards with their statuses", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const bandwidthCard = await screen.findByRole("region", { name: "bandwidth account" });
    expect(within(bandwidthCard).getByText("Not configured")).toBeInTheDocument();

    expect(screen.getByRole("region", { name: "telnyx account" })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "telnyx account" })).getByText("Unverified")).toBeInTheDocument();

    expect(screen.getByRole("region", { name: "twilio account" })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "twilio account" })).getByText("Active")).toBeInTheDocument();

    expect(screen.getByRole("region", { name: "plivo account" })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "plivo account" })).getByText("Failed")).toBeInTheDocument();

    expect(screen.getByRole("region", { name: "signalwire account" })).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "signalwire account" })).getByText("Disabled")).toBeInTheDocument();
  });

  // F16(a): keeps the frontend field catalogue honest against the backend's
  // PROVIDER_CREDENTIAL_FIELDS (backend/app/models/provider_accounts.py). The shapes differ
  // (frontend also carries a display `label`), so this projects PROVIDER_FIELDS down to the
  // same {provider: {field: secret}} shape the backend snapshot uses before comparing.
  it("provider field catalogue matches the backend field catalogue exactly", () => {
    const projected: Record<string, Record<string, boolean>> = {};
    for (const [provider, fields] of Object.entries(PROVIDER_FIELDS)) {
      projected[provider] = Object.fromEntries(fields.map((field) => [field.name, field.secret]));
    }
    expect(projected).toEqual(backendFieldSnapshot);
  });

  it("shows a stored-secret placeholder for existing secret fields without exposing them", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const twilioCard = await screen.findByRole("region", { name: "twilio account" });
    const authTokenInput = within(twilioCard).getByLabelText(/^auth token$/i) as HTMLInputElement;

    expect(authTokenInput.type).toBe("password");
    expect(authTokenInput.value).toBe("");
    expect(authTokenInput.placeholder).toBe("stored — leave blank to keep");

    const accountSidInput = within(twilioCard).getByLabelText(/^account sid$/i) as HTMLInputElement;
    expect(accountSidInput.value).toBe("AC-original");
  });

  it("POSTs a new telnyx account with the complete credentials body", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/provider-accounts": (_path: string, init?: RequestInit) => {
          if (init?.method === "POST") {
            return {
              id: "p-new-telnyx",
              provider: "telnyx",
              label: "Main",
              status: "unverified",
              last_probe_at: null,
              last_probe_detail: null,
              credentials: {},
            };
          }
          return ACCOUNTS.filter((account) => account.provider !== "telnyx");
        },
      }),
    );
    renderWithProviders(<ProvidersPage />, client);

    const telnyxCard = await screen.findByRole("region", { name: "telnyx account" });

    // F6: Save must stay disabled until every field for this provider is filled in.
    const saveButton = within(telnyxCard).getByRole("button", { name: "Save telnyx" });
    expect(saveButton).toBeDisabled();
    expect(within(telnyxCard).getByText(/missing:/i)).toBeInTheDocument();

    await userEvent.type(within(telnyxCard).getByLabelText(/^label$/i), "Main");
    await userEvent.type(within(telnyxCard).getByLabelText(/^api key$/i), "key-123");
    await userEvent.type(within(telnyxCard).getByLabelText(/^public key$/i), "pub-123");
    await userEvent.type(within(telnyxCard).getByLabelText(/^messaging profile id$/i), "mp-1");
    await userEvent.type(within(telnyxCard).getByLabelText(/^voice connection id$/i), "vc-1");

    expect(saveButton).toBeEnabled();
    await userEvent.click(saveButton);

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/provider-accounts" && c.init?.method === "POST",
      );
      expect(call).toBeTruthy();
    });

    const call = client.calls.find(
      (c) => c.path === "/api/v1/provider-accounts" && c.init?.method === "POST",
    )!;
    const body = call.init?.json as {
      provider: string;
      label: string;
      credentials: Record<string, string>;
    };

    expect(body.provider).toBe("telnyx");
    expect(body.label).toBe("Main");
    expect(body.credentials).toMatchObject({
      api_key: "key-123",
      public_key: "pub-123",
      messaging_profile_id: "mp-1",
      voice_connection_id: "vc-1",
    });
  });

  it("PATCHes an existing twilio account and omits blank fields (secret or not)", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const twilioCard = await screen.findByRole("region", { name: "twilio account" });

    const accountSidInput = within(twilioCard).getByLabelText(/^account sid$/i) as HTMLInputElement;
    const messagingSidInput = within(twilioCard).getByLabelText(/^messaging service sid$/i) as HTMLInputElement;

    await userEvent.clear(accountSidInput);
    await userEvent.type(accountSidInput, "AC-new");
    await userEvent.clear(messagingSidInput);
    await userEvent.type(messagingSidInput, "MS-new");

    await userEvent.click(within(twilioCard).getByRole("button", { name: "Save twilio" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/provider-accounts/p-twilio" && c.init?.method === "PATCH",
      );
      expect(call).toBeTruthy();
    });

    const call = client.calls.find(
      (c) => c.path === "/api/v1/provider-accounts/p-twilio" && c.init?.method === "PATCH",
    )!;
    const body = call.init?.json as { label?: string; credentials: Record<string, string> };

    expect(body.credentials).toMatchObject({
      account_sid: "AC-new",
      messaging_service_sid: "MS-new",
    });
    expect(body.credentials).not.toHaveProperty("auth_token");
  });

  // F4: label unchanged from its blank/default state must never be sent - the backend
  // rejects a blank label (min_length=1).
  it("PATCHes with a blank label and omits label from the body entirely", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const twilioCard = await screen.findByRole("region", { name: "twilio account" });
    const labelInput = within(twilioCard).getByLabelText(/^label$/i) as HTMLInputElement;

    await userEvent.clear(labelInput);
    await userEvent.click(within(twilioCard).getByRole("button", { name: "Save twilio" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/provider-accounts/p-twilio" && c.init?.method === "PATCH",
      );
      expect(call).toBeTruthy();
    });

    const call = client.calls.find(
      (c) => c.path === "/api/v1/provider-accounts/p-twilio" && c.init?.method === "PATCH",
    )!;
    const body = call.init?.json as { label?: string; credentials: Record<string, string> };

    expect(body).not.toHaveProperty("label");
  });

  // F16(f): a masked value must never round-trip back to the server as if it were real.
  it("never submits the secret mask placeholder as a credential value", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const twilioCard = await screen.findByRole("region", { name: "twilio account" });
    // Submit without touching anything - auth_token's input holds "" (F1/initial load),
    // never the mask string itself.
    await userEvent.click(within(twilioCard).getByRole("button", { name: "Save twilio" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/provider-accounts/p-twilio" && c.init?.method === "PATCH",
      );
      expect(call).toBeTruthy();
    });

    const call = client.calls.find(
      (c) => c.path === "/api/v1/provider-accounts/p-twilio" && c.init?.method === "PATCH",
    )!;
    const body = call.init?.json as { credentials: Record<string, string> };

    expect(Object.values(body.credentials)).not.toContain(SECRET_MASK);
    expect(body.credentials).not.toHaveProperty("auth_token");
  });

  // F1: a submitted secret must never linger in the form after a successful save.
  it("clears secret inputs after a successful save", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const twilioCard = await screen.findByRole("region", { name: "twilio account" });
    const authTokenInput = within(twilioCard).getByLabelText(/^auth token$/i) as HTMLInputElement;

    await userEvent.type(authTokenInput, "sk-new-secret");
    expect(authTokenInput.value).toBe("sk-new-secret");

    await userEvent.click(within(twilioCard).getByRole("button", { name: "Save twilio" }));

    await waitFor(() => expect(authTokenInput.value).toBe(""));
  });

  it("probe updates the provider status pill to active", async () => {
    const accounts = ACCOUNTS.map((account) =>
      account.provider === "twilio"
        ? { ...account, status: "unverified" as const, last_probe_detail: null }
        : account,
    );
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/provider-accounts": accounts,
      }),
    );
    renderWithProviders(<ProvidersPage />, client);

    const twilioCard = await screen.findByRole("region", { name: "twilio account" });
    expect(within(twilioCard).getByText("Unverified")).toBeInTheDocument();

    await userEvent.click(within(twilioCard).getByRole("button", { name: "Probe twilio" }));

    expect(await within(twilioCard).findByText("Active")).toBeInTheDocument();
  });

  it("renders a clear banner when credential storage is not configured", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/provider-accounts": () => {
          throw new ApiError(503, "credential_storage", "credential storage not configured");
        },
      }),
    );
    renderWithProviders(<ProvidersPage />, client);

    expect(await screen.findByText("credential storage not configured")).toBeInTheDocument();
    // F9: the list call failed - none of the five cards should offer an editable form.
    expect(screen.queryByRole("region", { name: /account$/ })).not.toBeInTheDocument();
  });

  it("renders a clear banner verbatim for any other 503, not just the credential-storage message", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/provider-accounts": () => {
          throw new ApiError(503, "some_other_code", "routing catalog temporarily unavailable");
        },
      }),
    );
    renderWithProviders(<ProvidersPage />, client);

    expect(
      await screen.findByText("routing catalog temporarily unavailable"),
    ).toBeInTheDocument();
  });

  // F10: a settings:read-only role (e.g. "agent") must render status-only, before any 403
  // ever happens - the 403 handler is only a backstop.
  it("renders read-only for a non owner/admin role", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/auth/me": {
          id: "u2",
          email: "agent@example.com",
          full_name: "Agent Example",
          memberships: [
            { org_id: "org-1", org_name: "Test Org", org_slug: "test-org", role_name: "agent" },
          ],
        },
      }),
    );
    renderWithProviders(<ProvidersPage />, client);

    const twilioCard = await screen.findByRole("region", { name: "twilio account" });
    await waitFor(() =>
      expect(within(twilioCard).getByRole("button", { name: "Save twilio" })).toBeDisabled(),
    );
    expect(within(twilioCard).getByRole("button", { name: "Probe twilio" })).toBeDisabled();
    expect(within(twilioCard).getByRole("button", { name: "Disable twilio" })).toBeDisabled();
    expect(
      screen.getByText(/read-only: your role can view provider status/i),
    ).toBeInTheDocument();
  });

  it("requires confirmation before disabling an account", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const signalwireCard = await screen.findByRole("region", { name: "signalwire account" });

    await userEvent.click(within(signalwireCard).getByRole("button", { name: "Disable signalwire" }));
    await userEvent.click(
      within(signalwireCard).getByRole("button", { name: "Confirm disable signalwire" }),
    );

    await waitFor(() => {
      expect(
        client.calls.some(
          (c) =>
            c.path === "/api/v1/provider-accounts/p-signalwire" &&
            c.init?.method === "DELETE",
        ),
      ).toBe(true);
    });
  });

  // F16(b): carrier-health + routing-policy coverage re-added from the pre-P17 version of
  // this page (git show HEAD:frontend/src/pages/ProvidersPage.test.tsx) - the P17 rework
  // kept these sections verbatim, so their tests must survive verbatim too.
  it("renders capability badges and the primary marker for a live carrier", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    // Scoped to the carrier-health list: "bandwidth" also appears as a provider-account
    // card heading elsewhere on the page.
    const carriersList = await screen.findByRole("list", { name: "Carriers" });
    const bandwidthCard = within(carriersList).getByText("bandwidth").closest("li")!;
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

    const carriersList = await screen.findByRole("list", { name: "Carriers" });
    const twilioCarrierCard = within(carriersList).getByText("twilio").closest("li")!;
    expect(within(twilioCarrierCard).getByText("TWILIO_ACCOUNT_SID")).toBeInTheDocument();
    expect(within(twilioCarrierCard).getByText("TWILIO_AUTH_TOKEN")).toBeInTheDocument();
    expect(
      within(twilioCarrierCard).getByRole("button", { name: "Test credentials" }),
    ).toBeDisabled();
  });

  it("probes a carrier and renders the failing detail verbatim, without clearing another carrier's result", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/routing/carriers/": (path: string) => {
          if (path.includes("/bandwidth/")) {
            return {
              name: "bandwidth",
              ok: false,
              detail: "Bandwidth: invalid account credentials",
              checked: "https://dashboard.bandwidth.com/api/accounts/123",
            };
          }
          if (path.includes("/telnyx/")) {
            return {
              name: "telnyx",
              ok: true,
              detail: "Telnyx: authenticated",
              checked: "https://api.telnyx.com/v2/balance",
            };
          }
          throw new Error(`unexpected probe path ${path}`);
        },
      }),
    );
    renderWithProviders(<ProvidersPage />, client);

    const carriersList = await screen.findByRole("list", { name: "Carriers" });
    const bandwidthCard = within(carriersList).getByText("bandwidth").closest("li")!;
    const telnyxCard = within(carriersList).getByText("telnyx").closest("li")!;

    await userEvent.click(within(bandwidthCard).getByRole("button", { name: "Test credentials" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/routing/carriers/bandwidth/probe"),
      ).toBe(true),
    );

    expect(
      await within(bandwidthCard).findByText("Bandwidth: invalid account credentials"),
    ).toBeInTheDocument();
    expect(
      within(bandwidthCard).getByText("https://dashboard.bandwidth.com/api/accounts/123"),
    ).toBeInTheDocument();

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

    const carriersList = await screen.findByRole("list", { name: "Carriers" });
    const telnyxCard = within(carriersList).getByText("telnyx").closest("li")!;
    expect(within(telnyxCard).getByRole("alert")).toHaveTextContent(/open/i);
    expect(within(telnyxCard).getByRole("alert")).toHaveTextContent(/failing/i);
  });

  // P19: per-card spend line, and the page-level Rates drawer.
  it("shows a spend line on the provider card that has spend this month", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    const telnyxCard = await screen.findByRole("region", { name: "telnyx account" });
    expect(await within(telnyxCard).findByText("$5.00")).toBeInTheDocument();
    expect(within(telnyxCard).getByText(/Spend this month/)).toBeInTheDocument();

    const bandwidthCard = await screen.findByRole("region", { name: "bandwidth account" });
    expect(within(bandwidthCard).getByText("No spend yet.")).toBeInTheDocument();
  });

  it("opens the rates drawer from the Rates button and lets an owner save a rate", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<ProvidersPage />, client);

    await userEvent.click(screen.getByRole("button", { name: "Rates" }));

    const dialog = await screen.findByRole("dialog", { name: "Provider rates" });
    const input = within(dialog).getByLabelText("telnyx sms_out rate") as HTMLInputElement;
    expect(input.value).toBe("0.0040");

    await userEvent.clear(input);
    await userEvent.type(input, "0.0050");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save rates" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/provider-rates" && c.init?.method === "PUT",
      );
      expect(call).toBeTruthy();
    });

    const call = client.calls.find(
      (c) => c.path === "/api/v1/provider-rates" && c.init?.method === "PUT",
    )!;
    expect(call.init?.json).toEqual({
      rates: [{ provider: "telnyx", metric: "sms_out", unit_cost_micros: 5000 }],
    });
  });
});
