import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BillingLedger } from "./BillingLedger";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";
import type { LedgerEntry } from "@/api/billing";

const CAPABILITIES = {
  permissions: ["org:billing"],
  org: {
    has_provider: true,
    has_number: true,
    member_count: 2,
    registration_state: "approved",
  },
};

function entry(overrides: Partial<LedgerEntry> = {}): LedgerEntry {
  return {
    id: "e-1",
    entry_type: "topup",
    amount_micros: 100_000,
    balance_after_micros: 200_000,
    note: null,
    reference: null,
    created_at: "2026-01-02T03:04:05Z",
    ...overrides,
  };
}

const PAGE1 = {
  items: [entry({ id: "first", note: "First page" })],
  next_cursor: "abc",
};

const PAGE2 = {
  items: [entry({ id: "second", note: "Second page" })],
  next_cursor: null,
};

function baseRoutes(overrides: Record<string, RouteStub | unknown> = {}) {
  return {
    "/api/v1/me/capabilities": CAPABILITIES,
    "/api/v1/billing/ledger": ((path, _init) => {
      if (path.includes("cursor=abc")) return PAGE2;
      return PAGE1;
    }) as RouteStub,
    ...overrides,
  };
}

describe("BillingLedger", () => {
  it("renders plain words for every entry type", async () => {
    const sixEntries: LedgerEntry[] = [
      entry({ id: "t", entry_type: "topup" }),
      entry({ id: "u", entry_type: "usage" }),
      entry({ id: "a", entry_type: "adjustment" }),
      entry({ id: "r", entry_type: "refund" }),
      entry({ id: "res", entry_type: "reserve" }),
      entry({ id: "rel", entry_type: "release" }),
    ];

    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/ledger": { items: sixEntries, next_cursor: null },
      }),
    );
    renderWithProviders(<BillingLedger />, client);

    expect(await screen.findByText("Credits added")).toBeInTheDocument();
    expect(screen.getByText("Used by your assistant")).toBeInTheDocument();
    expect(screen.getByText("Adjustment")).toBeInTheDocument();
    expect(screen.getByText("Refund")).toBeInTheDocument();
    expect(screen.getByText("Held for a call in progress")).toBeInTheDocument();
    expect(screen.getByText("Hold released")).toBeInTheDocument();
  });

  it("renders a debit with a minus sign", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/ledger": {
          items: [
            entry({
              id: "debit",
              entry_type: "usage",
              amount_micros: -123_000_000,
              balance_after_micros: 1_000_000,
            }),
          ],
          next_cursor: null,
        },
      }),
    );
    renderWithProviders(<BillingLedger />, client);

    expect(await screen.findByText("-$123.00")).toBeInTheDocument();
  });

  it("loads the next page with the cursor and appends rather than replacing", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<BillingLedger />, client);

    expect(await screen.findByText("First page")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("Second page")).toBeInTheDocument();
    expect(
      client.calls.some((call) => call.path.includes("cursor=abc")),
    ).toBe(true);
    expect(screen.getByText("First page")).toBeInTheDocument();
    expect(screen.getByText("Second page")).toBeInTheDocument();
  });

  // Supervisor-added: the accumulate-in-state pattern this page inherits from the audit
  // log appends whatever the query hands it, so the same page arriving twice used to show
  // every entry twice. Loading page two and then page two's cursor again must not
  // duplicate a row.
  it("does not show an entry twice when two pages overlap", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/ledger": ((path) => {
          if (path.includes("cursor=abc")) {
            return {
              // A cursor page that repeats the last entry of the previous page - which is
              // what a seq-based cursor does at a boundary, and what a plain concat would
              // render twice.
              items: [entry({ id: "first", note: "First page" }), entry({ id: "second", note: "Second page" })],
              next_cursor: null,
            };
          }
          return PAGE1;
        }) as RouteStub,
      }),
    );
    renderWithProviders(<BillingLedger />, client);

    expect(await screen.findByText("First page")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("Second page");

    expect(screen.getAllByText("First page")).toHaveLength(1);
    expect(screen.getAllByText("Second page")).toHaveLength(1);
  });

  it("omits Load more when next_cursor is null", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/ledger": {
          items: [entry({ id: "only", note: "Only page" })],
          next_cursor: null,
        },
      }),
    );
    renderWithProviders(<BillingLedger />, client);

    expect(await screen.findByText("Only page")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("renders the note and never the internal reference", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/ledger": {
          items: [
            entry({
              id: "noted",
              note: "Customer requested",
              reference: "internal-ref-123",
            }),
          ],
          next_cursor: null,
        },
      }),
    );
    renderWithProviders(<BillingLedger />, client);

    expect(await screen.findByText("Customer requested")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("internal-ref-123");
  });

  it("renders the empty state", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/ledger": { items: [], next_cursor: null },
      }),
    );
    renderWithProviders(<BillingLedger />, client);

    expect(await screen.findByText("Nothing here yet.")).toBeInTheDocument();
    expect(
      screen.getByText("Credits you add and use will show up here."),
    ).toBeInTheDocument();
  });

  it("shows an error with a retry that recovers", async () => {
    let ledgerCalls = 0;
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/ledger": ((_path, _init) => {
          ledgerCalls += 1;
          if (ledgerCalls === 1) return new Error("activity down");
          return { items: [entry({ id: "recovered", note: "Recovered" })], next_cursor: null };
        }) as RouteStub,
      }),
    );
    renderWithProviders(<BillingLedger />, client);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("activity down")).toBeInTheDocument();

    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Recovered")).toBeInTheDocument();
  });
});
