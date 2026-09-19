import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { makeStubClient, renderWithProviders } from "@/test/harness";
import { BillingTab, type OrgBilling } from "@/components/ops/BillingTab";

/**
 * P45: the operator's billing console.
 *
 * The operator is already signed in - `require_platform_operator` accepts the admin
 * operator's session cookie - so these stubs only cover plain `api.request` calls. There
 * is no unlock form and no `X-Platform-Ops-Token` header to drive.
 *
 * Stub keys are prefixes, and the harness picks the LONGEST match, so
 * `/api/v1/platform/billing/orgs/org-9/adjustments` is not shadowed by
 * `/api/v1/platform/billing/orgs/org-9`. Paths that carry a query string use function
 * stubs so the assertion never depends on whichever querystring happened to be baked in.
 */

const ME = {
  id: "u1",
  email: "owner@acme.com",
  full_name: "Owner",
  is_platform_operator: true,
  memberships: [
    {
      org_id: "org-1",
      org_name: "Acme",
      org_slug: "acme",
      role_name: "owner",
      permissions: ["org:read", "org:update"],
    },
  ],
  permissions: ["org:read", "org:update"],
};

/** The workspace the tests pick out of the directory. */
const ORG_ID = "org-9";

const ACCOUNTS = {
  accounts: [
    {
      org_id: ORG_ID,
      name: "Acme",
      level: "normal",
      score: 12,
      recommendation: null,
      needs_decision: false,
      reviewed_at: null,
      level_changed_at: null,
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
  auto_action: false,
};

/** No `org_id` and no `name`: the read does not echo the picker back, the heading uses
 * whatever the directory handed us. */
const BILLING: OrgBilling = {
  balance_micros: 42_500_000,
  reserved_micros: 2_500_000,
  ai_markup_bps: 250,
  ai_platform_fee_per_minute_micros: 1_500,
  ai_key_mode: "platform",
  telephony_prepaid: true,
  telephony_prepaid_since: "2026-01-02T00:00:00Z",
};

/** `GET /api/v1/platform/billing/margin` answers with a BARE array. */
const MARGIN = [
  {
    day: "2026-09-01",
    cost_micros: 1_000_000,
    price_micros: 2_000_000,
    margin_micros: 1_000_000,
  },
];

const ADJUSTMENT_RESULT = {
  id: "adj-1",
  amount_micros: 12_340_000,
  balance_after_micros: 54_840_000,
};

const ADJUSTMENTS_PATH = `/api/v1/platform/billing/orgs/${ORG_ID}/adjustments`;

function makeClient() {
  return makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/ops/monitoring/accounts": () => ACCOUNTS,
    [ADJUSTMENTS_PATH]: ADJUSTMENT_RESULT,
    [`/api/v1/platform/billing/orgs/${ORG_ID}`]: BILLING,
    "/api/v1/platform/billing/margin": () => MARGIN,
  });
}

/** BillingTab starts on the directory; the panel (and every money control) only exists
 * once a workspace is picked. */
async function openAcmeWorkspace(): Promise<void> {
  await userEvent.click(await screen.findByRole("button", { name: /Acme/ }));
}

describe("BillingTab", () => {
  it("renders the balance and prepaid state for the picked workspace", async () => {
    renderWithProviders(<BillingTab />, makeClient());
    await openAcmeWorkspace();

    expect((await screen.findByTestId("ops-billing-balance")).textContent).toBe(
      "$42.50",
    );
    expect(screen.getByText("Prepaid on")).toBeTruthy();
  });

  it("states the exact dollar amount in the adjustment preview", async () => {
    renderWithProviders(<BillingTab />, makeClient());
    await openAcmeWorkspace();

    await userEvent.selectOptions(screen.getByLabelText("Direction"), "debit");
    await userEvent.type(screen.getByLabelText("Amount in dollars"), "12.34");

    expect(screen.getByTestId("ops-adjustment-preview").textContent).toBe(
      "You are about to DEBIT $12.34 from Acme.",
    );
  });

  it("posts an adjustment with the signed integer micros, note and entry type", async () => {
    const client = makeClient();
    renderWithProviders(<BillingTab />, client);
    await openAcmeWorkspace();

    await userEvent.selectOptions(screen.getByLabelText("Direction"), "credit");
    await userEvent.selectOptions(screen.getByLabelText("Entry type"), "refund");
    await userEvent.type(screen.getByLabelText("Amount in dollars"), "12.34");
    await userEvent.type(screen.getByLabelText("Note"), "goodwill");
    await userEvent.click(
      screen.getByRole("button", { name: "Apply adjustment" }),
    );

    await waitFor(() => {
      expect(client.calls.some((c) => c.path === ADJUSTMENTS_PATH)).toBe(true);
    });

    const call = client.calls.find((c) => c.path === ADJUSTMENTS_PATH);
    expect(call?.init.method).toBe("POST");
    const json = call?.init.json as {
      amount_micros: number;
      note: string;
      entry_type: string;
    };
    expect(json).toEqual({
      amount_micros: 12_340_000,
      note: "goodwill",
      entry_type: "refund",
    });
    // `amount_micros` is a SIGNED integer on the wire - a float here is a 422.
    expect(Number.isInteger(json.amount_micros)).toBe(true);
  });

  it("blocks the adjustment while the note is blank", async () => {
    const client = makeClient();
    renderWithProviders(<BillingTab />, client);
    await openAcmeWorkspace();

    await userEvent.type(screen.getByLabelText("Amount in dollars"), "12.34");
    const apply = screen.getByRole("button", { name: "Apply adjustment" });
    expect(apply).toBeDisabled();

    await userEvent.click(apply);
    expect(client.calls.some((c) => c.path.endsWith("/adjustments"))).toBe(false);
  });

  it("renders margin from a bare array", async () => {
    renderWithProviders(<BillingTab />, makeClient());
    await openAcmeWorkspace();

    expect(await screen.findByText("2026-09-01")).toBeTruthy();
    const totalRow = screen.getByText("Total").closest("tr");
    expect(totalRow).not.toBeNull();
    // Price total for the single 2_000_000-micro day.
    expect(totalRow?.textContent).toContain("$2.00");
  });

  it("pages and filters the accounts list", async () => {
    const client = makeClient();
    renderWithProviders(<BillingTab />, client);

    await waitFor(() => {
      expect(
        client.calls.some(
          (c) => c.path.includes("limit=50") && c.path.includes("offset=0"),
        ),
      ).toBe(true);
    });

    await userEvent.click(screen.getByText("Paused"));

    await waitFor(() => {
      expect(client.calls.some((c) => c.path.includes("level=paused"))).toBe(
        true,
      );
    });
  });
});
