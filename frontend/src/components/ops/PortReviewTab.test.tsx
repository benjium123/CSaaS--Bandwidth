import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { makeStubClient, renderWithProviders } from "@/test/harness";

import { PortReviewTab } from "./PortReviewTab";

type StubClient = ReturnType<typeof makeStubClient>;
type RequestCall = [string, RequestInit | undefined];

/** Routes api.request by "<METHOD> <path>" - the tab talks to the ports list, the grants
 * queue and the port-out decision endpoints, and several of them share a URL prefix. */
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
/** Far enough out that the renderer never treats it as urgent. */
const RESPOND_BY = "2099-01-01T12:00:00Z";

const AUTH_ME = {
  id: "user-1",
  email: "ada@acme.test",
  full_name: "Ada Whitlock",
  memberships: [{ org_id: ORG_ID, org_name: "Acme", org_slug: "acme", role_name: "owner" }],
  permissions: [],
};

function opsPortFixture(overrides: Record<string, unknown> = {}) {
  return {
    id: "out-1",
    org_id: ORG_ID,
    direction: "out",
    carrier: "telnyx",
    numbers: ["+14155550100"],
    status: "pending",
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
    service_address: null,
    gaining_carrier: "Northwind Telecom",
    respond_by: RESPOND_BY,
    disputed: false,
    can_dispute: true,
    ...overrides,
  };
}

/** The routes every test in this file needs, plus whatever the case adds. */
function baseRoutes(extra: Record<string, unknown> = {}) {
  return {
    "GET /api/v1/auth/me": AUTH_ME,
    "GET /api/v1/ops/console/grants/pending": [],
    "GET /api/v1/ops/ports?status=awaiting_review": { ports: [] },
    "GET /api/v1/ops/ports?status=pending": { ports: [opsPortFixture()] },
    ...extra,
  };
}

/** Selects the port-out filter; it is the only combo box on screen at that point. */
async function showPendingPortOuts() {
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "pending" } });
}

afterEach(() => cleanup());

describe("PortReviewTab", () => {
  it("shows the respond-by time and the decisions for a pending port-out", async () => {
    const client = makeStubClient({});
    stubRequests(client, baseRoutes());
    renderWithProviders(<PortReviewTab />, client);

    await showPendingPortOuts();

    await screen.findByText(/Respond by/);
    expect(screen.getByText("Requested by Northwind Telecom")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Approve transfer" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  it("authorizes a port-out only after the inline confirmation", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({});
    const request = stubRequests(
      client,
      baseRoutes({
        "POST /api/v1/ops/ports/out-1/port-out/authorize": opsPortFixture({
          status: "authorized",
        }),
      }),
    );
    renderWithProviders(<PortReviewTab />, client);

    await showPendingPortOuts();
    await user.click(await screen.findByRole("button", { name: "Approve transfer" }));

    expect(requestCalls(request, "POST", "/api/v1/ops/ports/out-1/port-out/authorize")).toHaveLength(
      0,
    );

    await user.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      expect(
        requestCalls(request, "POST", "/api/v1/ops/ports/out-1/port-out/authorize"),
      ).toHaveLength(1);
    });
  });

  it("rejects a port-out with a code that requires an explanation", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({});
    const request = stubRequests(
      client,
      baseRoutes({
        "GET /api/v1/ops/ports/out-1/port-out/rejection-codes": {
          codes: [
            { code: 1001, label: "PIN does not match", reason_required: true },
            { code: 1002, label: "Details do not match", reason_required: false },
          ],
        },
        "POST /api/v1/ops/ports/out-1/port-out/reject": opsPortFixture({ status: "rejected" }),
      }),
    );
    renderWithProviders(<PortReviewTab />, client);

    await showPendingPortOuts();
    await user.click(await screen.findByRole("button", { name: "Reject" }));

    const codeSelect = await screen.findByRole("combobox", { name: "Reason code" });
    await user.selectOptions(codeSelect, "1001");

    const submit = screen.getByRole("button", { name: "Reject" });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Reason for rejection"), "PIN on file does not match.");
    expect(submit).toBeEnabled();

    await user.click(submit);

    await waitFor(() => {
      const calls = requestCalls(request, "POST", "/api/v1/ops/ports/out-1/port-out/reject");
      expect(calls).toHaveLength(1);
      expect((calls[0][1] as { json?: unknown } | undefined)?.json).toEqual({
        code: 1001,
        reason: "PIN on file does not match.",
      });
    });
  });

  it("does not load the rejection codes until someone asks to reject", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({});
    const request = stubRequests(
      client,
      baseRoutes({
        "GET /api/v1/ops/ports/out-1/port-out/rejection-codes": { codes: [] },
      }),
    );
    renderWithProviders(<PortReviewTab />, client);

    await showPendingPortOuts();
    await screen.findByRole("button", { name: "Reject" });

    expect(
      requestCalls(request, "GET", "/api/v1/ops/ports/out-1/port-out/rejection-codes"),
    ).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(
        requestCalls(request, "GET", "/api/v1/ops/ports/out-1/port-out/rejection-codes"),
      ).toHaveLength(1);
    });
  });

  it("flags a port-out the owner has disputed", async () => {
    const client = makeStubClient({});
    stubRequests(
      client,
      baseRoutes({
        "GET /api/v1/ops/ports?status=pending": { ports: [opsPortFixture({ disputed: true })] },
      }),
    );
    renderWithProviders(<PortReviewTab />, client);

    await showPendingPortOuts();

    expect(await screen.findByText("DISPUTED BY OWNER")).toBeTruthy();
  });
});
