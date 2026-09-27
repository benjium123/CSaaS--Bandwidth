import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OrgInvoicesPanel } from "./OrgInvoicesPanel";
import type { CustomInvoice, InvoiceIn, InvoicePreview } from "@/api/customInvoices";

const mocks = vi.hoisted(() => ({ requestMock: vi.fn() }));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ api: { request: mocks.requestMock } }),
}));

const ORG_ID = "o1";
const BASE = `/api/v1/ops/console/orgs/${ORG_ID}/invoices`;

type RequestInitLike = { method?: string; json?: unknown };

function methodOf(init?: RequestInitLike): string {
  return init?.method ?? "GET";
}

function renderPanel(canEdit = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <OrgInvoicesPanel orgId={ORG_ID} canEdit={canEdit} />
    </QueryClientProvider>,
  );
}

function invoice(overrides: Partial<CustomInvoice> = {}): CustomInvoice {
  return {
    id: "i-1",
    state: "pending",
    number: null,
    memo: null,
    lines: [],
    list_micros: 0,
    discount_micros: 0,
    total_micros: 0,
    paid_micros: 0,
    error: null,
    hosted_invoice_url: null,
    invoice_pdf: null,
    created_at: "2026-02-01T10:00:00Z",
    paid_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  mocks.requestMock.mockReset();
});

describe("OrgInvoicesPanel", () => {
  it("lists each invoice with its number, state and failure reason", async () => {
    const invoices: CustomInvoice[] = [
      invoice({
        id: "paid",
        state: "paid",
        number: "INV-0007",
        total_micros: 25_000_000,
        hosted_invoice_url: "https://stripe.test/inv/0007",
        paid_at: "2026-02-01T10:05:00Z",
      }),
      invoice({
        id: "failed",
        state: "failed",
        number: null,
        total_micros: 5_000_000,
        error: "Your card was declined.",
      }),
      invoice({ id: "pending", state: "pending", number: "INV-0009", total_micros: 12_500_000 }),
      invoice({ id: "void", state: "void", number: "INV-0008", total_micros: 1_000_000 }),
    ];

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { invoices };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel(false);

    expect(await screen.findByText("INV-0007")).toBeTruthy();
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(4);

    expect(within(rows[0]).getByText("Paid")).toBeTruthy();
    expect(within(rows[0]).getByText("$25.00")).toBeTruthy();
    expect(within(rows[0]).getByText("2026-02-01")).toBeTruthy();
    expect(within(rows[0]).getByRole("link", { name: "View" })).toBeTruthy();

    expect(within(rows[1]).getByText("Draft")).toBeTruthy();
    expect(within(rows[1]).getByText("Failed")).toBeTruthy();
    expect(within(rows[1]).getByText("Your card was declined.")).toBeTruthy();
    expect(within(rows[1]).queryByRole("link", { name: "View" })).toBeNull();

    expect(within(rows[2]).getByText("Pending")).toBeTruthy();
    expect(within(rows[3]).getByText("Void")).toBeTruthy();
  });

  it("shows the empty state when the workspace has no invoices", async () => {
    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { invoices: [] };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel(false);

    expect(await screen.findByText("No invoices yet.")).toBeTruthy();
  });

  it("hides the form and the row actions when the operator cannot edit", async () => {
    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { invoices: [invoice({ id: "i-2", state: "failed", number: "INV-0002" })] };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel(false);

    await screen.findByText("INV-0002");
    expect(screen.queryByRole("button", { name: "New invoice" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Charge again" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Void" })).toBeNull();
  });

  it("shows Charge again and Void on failed and pending rows when the operator can edit", async () => {
    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return {
          invoices: [
            invoice({ id: "i-1", state: "failed", number: "INV-0002" }),
            invoice({ id: "i-2", state: "pending", number: "INV-0003" }),
            invoice({ id: "i-3", state: "paid", number: "INV-0004" }),
          ],
        };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel();

    await screen.findByText("INV-0002");
    expect(screen.getAllByRole("button", { name: "Charge again" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Void" })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "New invoice" })).toBeTruthy();
  });

  it("prices the form with a preview and charges the card with the same lines", async () => {
    const user = userEvent.setup();
    let previewBody: InvoiceIn | null = null;
    let sendBody: InvoiceIn | null = null;

    const preview: InvoicePreview = {
      lines: [
        {
          type: "package",
          description: "3,000 SMS bundles",
          amount_cents: 4999,
          package: "sms",
          quantity: 3,
        },
        { type: "discount", description: "10% off", amount_cents: 500 },
      ],
      list: 55_000_000,
      discount: 5_500_000,
      paid: 49_500_000,
    };

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { invoices: [] };
      }
      if (path === `${BASE}/preview` && method === "POST") {
        previewBody = (init?.json ?? null) as InvoiceIn | null;
        return preview;
      }
      if (path === BASE && method === "POST") {
        sendBody = (init?.json ?? null) as InvoiceIn | null;
        return invoice({
          id: "new",
          state: "paid",
          number: "INV-0010",
          total_micros: 49_500_000,
          paid_micros: 49_500_000,
        });
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    await screen.findByText("No invoices yet.");
    await user.click(screen.getByRole("button", { name: "New invoice" }));

    await user.clear(screen.getByLabelText("Line 1 quantity"));
    await user.type(screen.getByLabelText("Line 1 quantity"), "3");
    await user.type(screen.getByLabelText("Line 1 price"), "49.99");
    await user.selectOptions(screen.getByLabelText("Discount type"), "percent");
    await user.type(screen.getByLabelText("Discount value"), "10");

    // Charging is only offered against a preview of the form as it stands now.
    expect((screen.getByRole("button", { name: "Charge card" }) as HTMLButtonElement).disabled).toBe(
      true,
    );

    await user.click(screen.getByRole("button", { name: "Preview" }));

    expect(await screen.findByText("Subtotal")).toBeTruthy();
    expect(screen.getByText("$49.99")).toBeTruthy();
    expect(screen.getByText("$55.00")).toBeTruthy();
    expect(screen.getByText("-$5.50")).toBeTruthy();
    expect(screen.getByText("$49.50")).toBeTruthy();

    const expectedLines = [
      { type: "package", package: "sms", quantity: 3, amount_cents: 4999 },
      { type: "discount", percent: 10 },
    ];
    expect(previewBody).toEqual({ lines: expectedLines });
    expect(
      (screen.getByRole("button", { name: "Charge card" }) as HTMLButtonElement).disabled,
    ).toBe(false);

    await user.click(screen.getByRole("button", { name: "Charge card" }));

    await waitFor(() => expect(sendBody).not.toBeNull());
    expect(sendBody).toEqual({ lines: expectedLines });
    expect(await screen.findByText("Invoice paid")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Charge card" })).toBeNull();
  });

  it("drops the preview when the form is edited after a preview", async () => {
    const user = userEvent.setup();
    let previewCalls = 0;

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { invoices: [] };
      }
      if (path === `${BASE}/preview` && method === "POST") {
        previewCalls += 1;
        return { lines: [], list: 0, discount: 0, paid: 0 } as InvoicePreview;
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    await screen.findByText("No invoices yet.");
    await user.click(screen.getByRole("button", { name: "New invoice" }));
    await user.click(screen.getByRole("button", { name: "Preview" }));

    await waitFor(() => expect(previewCalls).toBe(1));
    expect(
      (screen.getByRole("button", { name: "Charge card" }) as HTMLButtonElement).disabled,
    ).toBe(false);

    await user.clear(screen.getByLabelText("Line 1 quantity"));
    await user.type(screen.getByLabelText("Line 1 quantity"), "4");

    expect(
      (screen.getByRole("button", { name: "Charge card" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("retries a failed invoice through the retry endpoint", async () => {
    const user = userEvent.setup();
    let retryPath: string | null = null;

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return {
          invoices: [
            invoice({
              id: "i-2",
              state: "failed",
              number: "INV-0002",
              error: "Your card was declined.",
            }),
          ],
        };
      }
      if (path === `${BASE}/i-2/retry` && method === "POST") {
        retryPath = path;
        return invoice({ id: "i-2", state: "pending", number: "INV-0002" });
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Charge again" }));

    await waitFor(() => expect(retryPath).not.toBeNull());
    expect(retryPath).toBe(`${BASE}/i-2/retry`);
  });

  it("voids an invoice through the void endpoint", async () => {
    const user = userEvent.setup();
    let voidPath: string | null = null;

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { invoices: [invoice({ id: "i-3", state: "pending", number: "INV-0003" })] };
      }
      if (path === `${BASE}/i-3/void` && method === "POST") {
        voidPath = path;
        return invoice({ id: "i-3", state: "void", number: "INV-0003" });
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Void" }));

    await waitFor(() => expect(voidPath).not.toBeNull());
    expect(voidPath).toBe(`${BASE}/i-3/void`);
  });

  it("reports a card decline after charging", async () => {
    const user = userEvent.setup();

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { invoices: [] };
      }
      if (path === `${BASE}/preview` && method === "POST") {
        return { lines: [], list: 1_000_000, discount: 0, paid: 1_000_000 } as InvoicePreview;
      }
      if (path === BASE && method === "POST") {
        return invoice({
          id: "new",
          state: "failed",
          number: "INV-0011",
          error: "Your card was declined.",
        });
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    await screen.findByText("No invoices yet.");
    await user.click(screen.getByRole("button", { name: "New invoice" }));
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText("Subtotal");
    await user.click(screen.getByRole("button", { name: "Charge card" }));

    expect(await screen.findByText("Card declined: Your card was declined.")).toBeTruthy();
  });

  it("surfaces a server error from the api helper", async () => {
    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { invoices: [] };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel();

    await screen.findByText("No invoices yet.");
    expect(mocks.requestMock).toHaveBeenCalledWith(BASE);
  });
});
