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

/** The auth context only ever supplies the workspace id here; it is the same org as the
 * ports below so the account number is the same whichever source answers. */
const AUTH_ME = {
  id: "user-1",
  email: "ada@acme.test",
  full_name: "Ada Whitlock",
  memberships: [{ org_id: ORG_ID, org_name: "Acme", org_slug: "acme", role_name: "owner" }],
  permissions: [],
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
      "GET /api/v1/ports": { ports: [portFixture({ id: "port-1" })] },
      "GET /api/v1/auth/me": AUTH_ME,
    });
    renderWithProviders(<PortingPanel api={client} numbers={[]} />, client);

    await screen.findByText("Moving a number away");
    await screen.findByText(ACCOUNT);
    expect(screen.getByText(/Ask your new provider to start a transfer/)).toBeInTheDocument();
    expect(screen.getByText(/Turn off Port lock for/)).toBeInTheDocument();
  });
});
