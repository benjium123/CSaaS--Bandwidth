/**
 * E911 on the numbers page.
 *
 * Covers the 911 column's labels, the missing-address banner, the numbers:manage gate,
 * and the inline editor's two write paths: reusing a saved address (PUT only) and minting
 * a new one (POST then PUT). The saved-address list is fetched only while the editor is
 * open, so tests that never open it do not stub /numbers/emergency-addresses.
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { NumberOut } from "@/api/numbers";
import { formatPhone } from "@/lib/format";
import { NumbersPage } from "@/pages/NumbersPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const BANNER =
  "Some numbers have no 911 address. Emergency calls from them cannot be located.";
const E164 = "+12145550100";
const SET_BUTTON = `Set 911 address for ${E164}`;
const EDITOR = `911 address for ${E164}`;
const SAVE_LABEL = "Save 911 address";
const EMERGENCY_ADDRESSES = "/api/v1/numbers/emergency-addresses";
const EMERGENCY_ADDRESS = "/api/v1/numbers/num-t1/emergency-address";

const HQ_ADDRESS = {
  id: "addr-1",
  name: "HQ",
  street_address: "1 Main St",
  extended_address: null,
  locality: "Dallas",
  administrative_area: "TX",
  postal_code: "75201",
  country_code: "US",
  label: "1 Main St, Dallas, TX 75201",
};

/** A live Telnyx number that has no 911 address yet, unless a test overrides it. */
function numberFixture(overrides: Partial<NumberOut> = {}): NumberOut {
  return {
    id: "num-t1",
    e164: E164,
    carrier: "telnyx",
    status: "active",
    number_type: "local",
    inbox_id: null,
    inbox_name: null,
    campaign_id: null,
    registration: "approved",
    registration_detail: null,
    provider_account_id: null,
    provider_account_label: null,
    purchase_cost_cents: null,
    monthly_cost_cents: 1500,
    purchased_at: null,
    order_detail: null,
    emergency_status: "missing",
    emergency_address_id: null,
    emergency_detail: null,
    ...overrides,
  } as unknown as NumberOut;
}

function baseStubs(numbers: NumberOut[], permissions: string[] = ["numbers:manage"]) {
  return {
    "/api/v1/auth/me": {
      id: "u-1",
      email: "owner@example.com",
      full_name: "Owner",
      permissions,
      memberships: [
        {
          org_id: "org-1",
          org_name: "Org",
          org_slug: "org",
          role_name: "owner",
        },
      ],
    },
    "/api/v1/me/capabilities": {
      permissions,
      org: {
        has_provider: true,
        has_number: true,
        member_count: 1,
        registration_state: "approved",
      },
    },
    "/api/v1/agent/profiles": [],
    "/api/v1/registration/campaigns": [],
    "/api/v1/routing/catalog": [],
    "/api/v1/provider-accounts": [],
    "/api/v1/spend/summary": { total_micros: 0, total_usd: "$0.00", by_provider: {} },
    "/api/v1/numbers": numbers,
  };
}

type Routes = Parameters<typeof makeStubClient>[0];

function renderNumbersPage(routes: Routes) {
  const client = makeStubClient(routes);
  renderWithProviders(<NumbersPage />, client);
  return client;
}

/** The table row that carries a given number, found by its formatted e164. */
async function rowFor(e164: string): Promise<HTMLElement> {
  const label = await screen.findByText(formatPhone(e164));
  return label.closest("tr") as HTMLElement;
}

function createdAddress(id: string) {
  return {
    id,
    name: "Acme",
    street_address: "2 Oak Ave",
    extended_address: null,
    locality: "Plano",
    administrative_area: "TX",
    postal_code: "75024",
    country_code: "US",
    label: "2 Oak Ave, Plano, TX 75024",
  };
}

describe("NumbersPage 911", () => {
  it("labels the 911 column for every carrier state", async () => {
    renderNumbersPage(
      baseStubs([
        numberFixture({
          id: "num-active",
          e164: "+12145550101",
          emergency_status: "active",
          emergency_address_id: "addr-1",
        }),
        numberFixture({
          id: "num-prov",
          e164: "+12145550102",
          emergency_status: "provisioning",
        }),
        numberFixture({
          id: "num-failed",
          e164: "+12145550103",
          emergency_status: "failed",
          emergency_detail: "Carrier rejected the address",
        }),
        numberFixture({ id: "num-missing", e164: "+12145550104" }),
        numberFixture({
          id: "num-bw",
          e164: "+12145550105",
          carrier: "bandwidth",
          emergency_status: "unsupported",
        }),
      ]),
    );

    // The Status column also says "Active", so the active row must show it twice once the
    // 911 column agrees.
    const active = await rowFor("+12145550101");
    expect(within(active).getAllByText("Active")).toHaveLength(2);

    const provisioning = await rowFor("+12145550102");
    expect(within(provisioning).getByText("Setting up")).toBeInTheDocument();

    const failed = await rowFor("+12145550103");
    expect(within(failed).getByText("Needs attention")).toBeInTheDocument();
    expect(within(failed).getByText("Carrier rejected the address")).toBeInTheDocument();

    const missing = await rowFor("+12145550104");
    expect(within(missing).getByText("Not set")).toBeInTheDocument();

    const bandwidth = await rowFor("+12145550105");
    expect(within(bandwidth).getByText("Managed by Bandwidth")).toBeInTheDocument();
  });

  it("warns when an active Telnyx number is missing its 911 address", async () => {
    renderNumbersPage(baseStubs([numberFixture({ emergency_status: "missing" })]));

    expect(await screen.findByRole("alert")).toHaveTextContent(BANNER);
  });

  it("warns when an active Telnyx number failed to provision its 911 address", async () => {
    renderNumbersPage(
      baseStubs([
        numberFixture({
          emergency_status: "failed",
          emergency_detail: "Carrier rejected the address",
        }),
      ]),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(BANNER);
  });

  it("does not warn about a released number with no 911 address", async () => {
    renderNumbersPage(
      baseStubs([numberFixture({ status: "released", emergency_status: "missing" })]),
    );

    await screen.findByText(formatPhone(E164));
    expect(screen.queryByText(BANNER)).toBeNull();
  });

  it("does not warn when every Telnyx number has a 911 address", async () => {
    renderNumbersPage(
      baseStubs([numberFixture({ emergency_status: "active", emergency_address_id: "addr-1" })]),
    );

    await screen.findByText(formatPhone(E164));
    expect(screen.queryByText(BANNER)).toBeNull();
  });

  it("offers no 911 controls without numbers:manage", async () => {
    // Positive control: the "offers Set/Change controls ... with numbers:manage" test below
    // renders the same fixtures with the permission present and DOES find these buttons.
    // Without that control this test could pass vacuously while the query was still loading -
    // gate.can() is false during loading - so we also wait for the capabilities response to be
    // issued and then flush the microtask queue before asserting absence.
    const client = renderNumbersPage(baseStubs([numberFixture()], ["numbers:read"]));

    await screen.findByText(formatPhone(E164));
    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/me/capabilities")).toBe(true),
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(screen.queryAllByRole("button", { name: /911 address for/ })).toHaveLength(0);
  });

  it("offers Set/Change controls for Telnyx only, with numbers:manage", async () => {
    renderNumbersPage(
      baseStubs([
        numberFixture({ id: "num-missing", e164: E164, emergency_status: "missing" }),
        numberFixture({
          id: "num-set",
          e164: "+12145550106",
          emergency_status: "active",
          emergency_address_id: "addr-1",
        }),
        numberFixture({
          id: "num-bw",
          e164: "+12145550105",
          carrier: "bandwidth",
          emergency_status: "unsupported",
        }),
      ]),
    );

    expect(await screen.findByRole("button", { name: SET_BUTTON })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Change 911 address for +12145550106" }),
    ).toBeInTheDocument();
    // The carrier-managed number never gets a control.
    expect(screen.queryAllByRole("button", { name: /911 address for/ })).toHaveLength(2);
  });

  it("saves a saved address with a PUT and never POSTs", async () => {
    const client = renderNumbersPage({
      ...baseStubs([numberFixture()]),
      [EMERGENCY_ADDRESSES]: { addresses: [HQ_ADDRESS], notice: "Addresses on file." },
      [EMERGENCY_ADDRESS]: numberFixture({ emergency_status: "provisioning" }),
    });

    fireEvent.click(await screen.findByRole("button", { name: SET_BUTTON }));
    await screen.findByRole("group", { name: EDITOR });

    expect(screen.getByRole("button", { name: SAVE_LABEL })).toBeDisabled();

    fireEvent.click(await screen.findByRole("radio", { name: /1 Main St, Dallas, TX 75201/ }));
    expect(screen.getByRole("button", { name: SAVE_LABEL })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: SAVE_LABEL }));

    await waitFor(() => expect(screen.queryByRole("group", { name: EDITOR })).toBeNull());

    const put = client.calls.find((c) => c.path === EMERGENCY_ADDRESS);
    expect(put?.init.method).toBe("PUT");
    expect(put?.init.json).toEqual({ address_id: "addr-1" });
    expect(
      client.calls.filter(
        (c) => c.path === EMERGENCY_ADDRESSES && c.init.method === "POST",
      ),
    ).toHaveLength(0);
  });

  it("mints a new address with a POST and then PUTs its id", async () => {
    const client = renderNumbersPage({
      ...baseStubs([numberFixture()]),
      [EMERGENCY_ADDRESSES]: (_path: string, init: { method?: string }) =>
        init.method === "POST"
          ? createdAddress("addr-new")
          : { addresses: [], notice: "No saved addresses." },
      [EMERGENCY_ADDRESS]: numberFixture({ emergency_status: "provisioning" }),
    });

    fireEvent.click(await screen.findByRole("button", { name: SET_BUTTON }));
    await screen.findByRole("group", { name: EDITOR });

    // No saved addresses, so the new-address form is shown straight away.
    fireEvent.change(await screen.findByLabelText("Business or person at this location"), {
      target: { value: "Acme" },
    });
    fireEvent.change(screen.getByLabelText("Street address"), {
      target: { value: "2 Oak Ave" },
    });
    fireEvent.change(screen.getByLabelText("City"), { target: { value: "Plano" } });
    fireEvent.change(screen.getByLabelText("State (2-letter)"), { target: { value: "tx" } });
    fireEvent.change(screen.getByLabelText("ZIP code"), { target: { value: "75024" } });

    fireEvent.click(screen.getByRole("button", { name: SAVE_LABEL }));

    await waitFor(() => expect(screen.queryByRole("group", { name: EDITOR })).toBeNull());

    const postIndex = client.calls.findIndex(
      (c) => c.path === EMERGENCY_ADDRESSES && c.init.method === "POST",
    );
    const putIndex = client.calls.findIndex((c) => c.path === EMERGENCY_ADDRESS);
    expect(postIndex).toBeGreaterThanOrEqual(0);
    expect(putIndex).toBeGreaterThan(postIndex);

    // The state is upper-cased and the blank suite/floor line is omitted entirely.
    expect(client.calls[postIndex].init.json).toEqual({
      name: "Acme",
      street_address: "2 Oak Ave",
      locality: "Plano",
      administrative_area: "TX",
      postal_code: "75024",
      country_code: "US",
    });
    expect(client.calls[putIndex].init.method).toBe("PUT");
    expect(client.calls[putIndex].init.json).toEqual({ address_id: "addr-new" });
  });

  it("shows the carrier's validation error verbatim and keeps the editor open", async () => {
    renderNumbersPage({
      ...baseStubs([numberFixture()]),
      [EMERGENCY_ADDRESSES]: { addresses: [HQ_ADDRESS], notice: "Addresses on file." },
      [EMERGENCY_ADDRESS]: () =>
        new Error("Address could not be validated. Did you mean: 1 Main Street?"),
    });

    fireEvent.click(await screen.findByRole("button", { name: SET_BUTTON }));
    const group = await screen.findByRole("group", { name: EDITOR });

    fireEvent.click(await within(group).findByRole("radio", { name: /1 Main St, Dallas, TX 75201/ }));
    fireEvent.click(within(group).getByRole("button", { name: SAVE_LABEL }));

    expect(await within(group).findByRole("alert")).toHaveTextContent(
      "Address could not be validated. Did you mean: 1 Main Street?",
    );
    expect(screen.getByRole("group", { name: EDITOR })).toBeInTheDocument();
  });
});
