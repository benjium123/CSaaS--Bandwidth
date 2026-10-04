import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { PortingPanel, formatPortFocDate } from "./PortingPanel";
import { makeStubClient, renderWithProviders } from "@/test/harness";

type StubClient = ReturnType<typeof makeStubClient>;
type RequestCall = [string, RequestInit | undefined];

/**
 * Routes api.request by "<METHOD> <path>". The panel talks to four endpoints (ports list,
 * portability check, PATCH and cancel), so a bare URL map cannot tell them apart - same
 * helper as PlatformMessagingHealth.test.tsx. A route value that is an Error rejects.
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

/** Every call the panel made to one endpoint, in order. */
function requestCalls(
  spy: { mock: { calls: unknown[] } },
  method: string,
  path: string,
): RequestCall[] {
  return (spy.mock.calls as RequestCall[]).filter(
    ([callPath, init]) => callPath === path && (init?.method ?? "GET").toUpperCase() === method,
  );
}

const ORG_ID = "abcd1234-5678-90ab-cdef-1234567890ab";
const ACCOUNT = "RL-ABCD1234";
const FOC_DATE = "2026-03-17T15:00:00Z";

/** The auth context only ever supplies the signed-in user here; the account id now comes from
 * the ports response. */
const AUTH_ME = {
  id: "user-1",
  email: "ada@acme.test",
  full_name: "Ada Whitlock",
  memberships: [{ org_id: ORG_ID, org_name: "Acme", org_slug: "acme", role_name: "owner" }],
  permissions: [],
};

/** The summary GET /api/v1/ports/pin answers with; the reveal/rotate responses add `pin`. */
const PIN_STATUS = {
  account_id: ACCOUNT,
  has_pin: true,
  numbers_total: 3,
  numbers_protected: 2,
  rotated_at: null,
};

const CHECK_RESULTS = {
  results: [
    { phone_number: "+14155550100", portable: true, reason: null, fast_portable: true },
    { phone_number: "+14155550101", portable: true, reason: null, fast_portable: false },
    {
      phone_number: "+14155550102",
      portable: false,
      reason: "Already on our network",
      fast_portable: false,
    },
  ],
};

/** `carrier` is deliberately set: nothing on this screen may ever print it. */
function portFixture(overrides: Record<string, unknown> = {}) {
  return {
    id: "port-1",
    org_id: ORG_ID,
    direction: "in",
    carrier: "telnyx",
    numbers: ["+14155550100", "+14155550101"],
    status: "awaiting_review",
    foc_date: null,
    last_error: null,
    authorized_name: "Ada Whitlock",
    business_name: "Acme",
    manual: false,
    events: [],
    created_at: "2026-03-01T12:00:00Z",
    customer_reason: null,
    can_edit: false,
    can_cancel: false,
    account_number: "ACC-1",
    billing_number: "+14155550199",
    service_address: {
      street: "1 Main St",
      extended: "Suite 2",
      city: "Austin",
      state: "TX",
      zip: "78701",
    },
    ...overrides,
  };
}

describe("PortingPanel", () => {
  it("starts a transfer for the portable numbers only, prefilled and read-only", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      "POST /api/v1/ports/check": CHECK_RESULTS,
      "GET /api/v1/ports": { ports: [] },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    fireEvent.change(screen.getByPlaceholderText(/\+14155550100/), {
      target: { value: "+14155550100\n+14155550101\n+14155550102" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Check" }));

    // The number that cannot move still shows why, right where it was reported.
    await screen.findByText("Already on our network");
    expect(screen.getByText("Can't be ported")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Start a transfer for 2 portable number(s)" }),
    );

    const numbersField = screen.getByLabelText(/Numbers to port/);
    expect(numbersField).toHaveValue("+14155550100\n+14155550101");
    expect(numbersField).toHaveAttribute("readonly");
  });

  it("describes each status in plain language", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      "GET /api/v1/ports": {
        ports: [
          portFixture({ id: "p-review", status: "awaiting_review" }),
          portFixture({ id: "p-foc", status: "foc_confirmed", foc_date: FOC_DATE }),
          portFixture({
            id: "p-exception",
            status: "exception",
            customer_reason: "We need a copy of your latest bill.",
          }),
          portFixture({ id: "p-ported", status: "ported" }),
        ],
      },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    // The list is rendered from a polled query, so findBy (which retries) is the right
    // assertion for text that is on screen; the expected date comes from the same formatter
    // the panel uses, rather than a hard-coded weekday.
    await screen.findByText("Waiting for our review");
    await screen.findByText(`Moving on ${formatPortFocDate(FOC_DATE)}`);
    await screen.findByText("Needs your attention");
    await screen.findByText("We need a copy of your latest bill.");
    await screen.findByText("Done — your numbers are live");
  });

  it("saves the fix as a multipart PATCH and leaves an empty PIN out", async () => {
    const client = makeStubClient({});
    const request = stubRequests(client, {
      "GET /api/v1/ports": {
        ports: [
          portFixture({
            id: "port-7",
            status: "exception",
            can_edit: true,
            customer_reason: "The account number did not match.",
          }),
        ],
      },
      "PATCH /api/v1/ports/port-7": portFixture({
        id: "port-7",
        status: "exception",
        can_edit: true,
        account_number: "ACC-NEW",
      }),
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    fireEvent.click(await screen.findByRole("button", { name: "Fix and resubmit" }));

    const account = await screen.findByLabelText("Account number with the old carrier");
    expect(account).toHaveValue("ACC-1");
    expect(screen.getByLabelText("Business name on the old account")).toHaveValue("Acme");
    expect(screen.getByLabelText("Street address")).toHaveValue("1 Main St");
    expect(screen.getByLabelText("Account PIN / passcode")).toHaveValue("");
    expect(screen.getByPlaceholderText("Leave empty to keep the current PIN")).toBeInTheDocument();

    fireEvent.change(account, { target: { value: "ACC-NEW" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(requestCalls(request, "PATCH", "/api/v1/ports/port-7")).toHaveLength(1);
    });

    const [, init] = requestCalls(request, "PATCH", "/api/v1/ports/port-7")[0];
    const body = init?.body as FormData;
    expect(body).toBeInstanceOf(FormData);
    expect(body.get("account_number")).toBe("ACC-NEW");
    expect(body.get("business_name")).toBe("Acme");
    expect(body.get("service_street")).toBe("1 Main St");
    expect(body.get("service_state")).toBe("TX");
    // Empty PIN means "keep the current one": no key at all, not an empty string.
    expect(body.has("pin")).toBe(false);
    expect(body.has("loa")).toBe(false);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    });
  });

  it("cancels only after the inline confirmation and shows a 409 verbatim", async () => {
    const client = makeStubClient({});
    const conflict = "This transfer has already been filed with your provider.";
    const request = stubRequests(client, {
      "GET /api/v1/ports": {
        ports: [portFixture({ id: "port-9", status: "submitted", can_cancel: true })],
      },
      "POST /api/v1/ports/port-9/cancel": Object.assign(new Error(conflict), { status: 409 }),
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    fireEvent.click(await screen.findByRole("button", { name: "Cancel transfer" }));
    await screen.findByText("Stop this transfer? Your numbers stay with your current provider.");

    // Nothing has been sent until Confirm is pressed.
    expect(requestCalls(request, "POST", "/api/v1/ports/port-9/cancel")).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      expect(requestCalls(request, "POST", "/api/v1/ports/port-9/cancel")).toHaveLength(1);
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(conflict);
  });

  it("never prints a carrier name", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      "GET /api/v1/ports": { ports: [portFixture({ id: "port-1", status: "submitted" })] },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(
      <PortingPanel api={client} numbers={[{ id: "n1", e164: "+14155550100", port_locked: true }]} />,
      client,
    );

    await screen.findByText("Filed — waiting for your current provider");

    for (const element of Array.from(document.body.querySelectorAll("*"))) {
      const text = (element.textContent ?? "").toLowerCase();
      expect(text).not.toContain("telnyx");
      expect(text).not.toContain("signalwire");
    }
  });

  it("tells the customer the account number their new provider will ask for", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      "GET /api/v1/ports": {
        ports: [portFixture({ id: "port-1" })],
        account_id: ACCOUNT,
        pin_holder: false,
      },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    await screen.findByText("Moving a number away");
    await screen.findByText(ACCOUNT);
    expect(screen.getByText(/Ask your new provider to start a transfer/)).toBeInTheDocument();
    expect(
      screen.getByText(/We review every transfer request before your number is released/),
    ).toBeInTheDocument();
  });

  it("shows the Ringlite account id the API minted", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      "GET /api/v1/ports": {
        ports: [portFixture({ id: "port-1" })],
        account_id: ACCOUNT,
        pin_holder: false,
      },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    await screen.findByText(/Ringlite account ID/);
    expect(await screen.findByText(ACCOUNT)).toBeInTheDocument();
  });

  it("points people who cannot see the PIN at an owner", async () => {
    const client = makeStubClient({});
    const request = stubRequests(client, {
      "GET /api/v1/ports": {
        ports: [portFixture({ id: "port-1" })],
        account_id: ACCOUNT,
        pin_holder: false,
      },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    await screen.findByText("Only the workspace owner or an admin can see the transfer PIN.");
    expect(screen.queryByRole("button", { name: "Show PIN" })).not.toBeInTheDocument();
    // No PIN summary is fetched for someone who cannot hold it.
    expect(requestCalls(request, "GET", "/api/v1/ports/pin")).toHaveLength(0);
  });

  it("reveals the transfer PIN through the reveal endpoint", async () => {
    const client = makeStubClient({});
    const request = stubRequests(client, {
      "GET /api/v1/ports": { ports: [], account_id: ACCOUNT, pin_holder: true },
      "GET /api/v1/ports/pin": PIN_STATUS,
      "POST /api/v1/ports/pin/reveal": { ...PIN_STATUS, pin: "482913" },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    await screen.findByText("Protects 2 of 3 numbers");

    fireEvent.click(await screen.findByRole("button", { name: "Show PIN" }));

    expect(await screen.findByText("482913")).toBeInTheDocument();
    await waitFor(() => {
      expect(requestCalls(request, "POST", "/api/v1/ports/pin/reveal")).toHaveLength(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByText("482913")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show PIN" })).toBeInTheDocument();
  });

  it("rotates the PIN only after the inline confirmation", async () => {
    const client = makeStubClient({});
    const request = stubRequests(client, {
      "GET /api/v1/ports": { ports: [], account_id: ACCOUNT, pin_holder: true },
      "GET /api/v1/ports/pin": PIN_STATUS,
      "POST /api/v1/ports/pin/rotate": {
        ...PIN_STATUS,
        pin: "111222",
        rotated_at: "2026-01-01T00:00:00Z",
      },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    fireEvent.click(await screen.findByRole("button", { name: "Change PIN" }));
    await screen.findByText("Change the PIN? The old one stops working on all your numbers.");

    expect(requestCalls(request, "POST", "/api/v1/ports/pin/rotate")).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      expect(requestCalls(request, "POST", "/api/v1/ports/pin/rotate")).toHaveLength(1);
    });
    expect(await screen.findByText("111222")).toBeInTheDocument();
  });

  it("keeps Change PIN from firing until it is confirmed", async () => {
    const client = makeStubClient({});
    const request = stubRequests(client, {
      "GET /api/v1/ports": { ports: [], account_id: ACCOUNT, pin_holder: true },
      "GET /api/v1/ports/pin": PIN_STATUS,
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    fireEvent.click(await screen.findByRole("button", { name: "Change PIN" }));
    await screen.findByText("Change the PIN? The old one stops working on all your numbers.");
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));

    expect(
      screen.queryByText("Change the PIN? The old one stops working on all your numbers."),
    ).not.toBeInTheDocument();
    expect(requestCalls(request, "POST", "/api/v1/ports/pin/rotate")).toHaveLength(0);
  });

  it("disputes a pending port-out after an inline confirmation", async () => {
    const client = makeStubClient({});
    const request = stubRequests(client, {
      "GET /api/v1/ports": {
        ports: [
          portFixture({
            id: "port-out-1",
            direction: "out",
            status: "pending",
            can_dispute: true,
            disputed: false,
            gaining_carrier: "Northwind Telecom",
          }),
        ],
        account_id: ACCOUNT,
        pin_holder: false,
      },
      "POST /api/v1/ports/port-out-1/dispute": portFixture({
        id: "port-out-1",
        direction: "out",
        status: "pending",
        disputed: true,
      }),
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    await screen.findByText("Another provider asked for these numbers");
    expect(screen.getByText("Requested by Northwind Telecom")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "I didn't request this" }));
    await screen.findByText(
      "Tell Ringlite this transfer is not yours? We'll stop it if we can.",
    );

    expect(requestCalls(request, "POST", "/api/v1/ports/port-out-1/dispute")).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      expect(requestCalls(request, "POST", "/api/v1/ports/port-out-1/dispute")).toHaveLength(1);
    });
  });

  it("confirms a disputed port-out instead of offering the button again", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      "GET /api/v1/ports": {
        ports: [
          portFixture({
            id: "port-out-2",
            direction: "out",
            status: "pending",
            can_dispute: true,
            disputed: true,
          }),
        ],
        account_id: ACCOUNT,
        pin_holder: false,
      },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    await screen.findByText("You told us this wasn't you. Our team is on it.");
    expect(screen.queryByRole("button", { name: "I didn't request this" })).not.toBeInTheDocument();
  });

  it("keeps the port lock copy honest about transfers away", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      "GET /api/v1/ports": { ports: [], account_id: ACCOUNT, pin_holder: false },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    expect(
      await screen.findByText(/Port lock stops a number being released or deleted inside Ringlite/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/It does not affect transfers to another provider/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Turn off Port lock/)).not.toBeInTheDocument();
  });

  it("explains the separate Number Transfer PIN in the port-in form", async () => {
    const client = makeStubClient({});
    stubRequests(client, {
      "GET /api/v1/ports": { ports: [], account_id: ACCOUNT, pin_holder: false },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    fireEvent.click(await screen.findByRole("button", { name: "Start a port request" }));

    expect(
      screen.getByText(/give you a separate Number Transfer PIN/),
    ).toBeInTheDocument();
  });
});
