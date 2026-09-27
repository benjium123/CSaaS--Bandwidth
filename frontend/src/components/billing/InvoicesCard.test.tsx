import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { InvoicesCard } from "./InvoicesCard";
import type { CustomInvoice } from "@/api/customInvoices";

const mocks = vi.hoisted(() => ({ requestMock: vi.fn() }));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ api: { request: mocks.requestMock } }),
}));

const BASE = "/api/v1/billing/invoices";
const STORAGE_KEY = "settings.billing.invoices";

type RequestInitLike = { method?: string; json?: unknown };

function methodOf(init?: RequestInitLike): string {
  return init?.method ?? "GET";
}

function renderCard() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <InvoicesCard />
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
    collection: "charge_card",
    emailed_to: null,
    hosted_invoice_url: null,
    invoice_pdf: null,
    created_at: "2026-01-05T12:00:00Z",
    paid_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  mocks.requestMock.mockReset();
  window.localStorage.clear();
});

describe("InvoicesCard", () => {
  it("starts collapsed and lists each invoice with its number, lines, total and state", async () => {
    const user = userEvent.setup();
    const invoices: CustomInvoice[] = [
      invoice({
        id: "paid",
        state: "paid",
        number: "INV-0001",
        total_micros: 12_500_000,
        lines: [
          { type: "package", description: "1,000 SMS bundles", amount_cents: 1000 },
          { type: "discount", description: "10% off", amount_cents: 100 },
        ],
        hosted_invoice_url: "https://stripe.test/inv/0001",
        paid_at: "2026-01-05T12:05:00Z",
      }),
      invoice({
        id: "failed",
        state: "failed",
        number: null,
        total_micros: 2_000_000,
        lines: [{ type: "credit", description: "Balance credit", amount_cents: 2000 }],
        invoice_pdf: "https://stripe.test/inv/0002.pdf",
      }),
      invoice({ id: "pending", state: "pending", number: "INV-0003", total_micros: 0 }),
      invoice({ id: "void", state: "void", number: "INV-0004", total_micros: 0 }),
    ];

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { invoices };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderCard();

    const toggle = screen.getByRole("button", { name: "Invoices" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    await user.click(toggle);

    expect(await screen.findByText("INV-0001")).toBeTruthy();
    expect(screen.getByText("Paid")).toBeTruthy();
    expect(screen.getAllByText("2026-01-05").length).toBeGreaterThan(0);
    expect(screen.getByText("1,000 SMS bundles, 10% off")).toBeTruthy();
    expect(screen.getByText("$12.50")).toBeTruthy();
    expect(screen.getByText("Draft")).toBeTruthy();
    expect(screen.getByText("Payment failed")).toBeTruthy();
    expect(screen.getByText("Balance credit")).toBeTruthy();
    expect(screen.getByText("Processing")).toBeTruthy();
    expect(screen.getByText("Void")).toBeTruthy();

    const links = screen.getAllByRole("link", { name: "View / receipt" });
    expect(links).toHaveLength(2);
    expect((links[0] as HTMLAnchorElement).href).toBe("https://stripe.test/inv/0001");
    expect((links[1] as HTMLAnchorElement).href).toBe("https://stripe.test/inv/0002.pdf");

    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("true");
  });

  it("renders the empty state", async () => {
    const user = userEvent.setup();

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { invoices: [] };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderCard();
    await user.click(screen.getByRole("button", { name: "Invoices" }));

    expect(await screen.findByText("No invoices yet.")).toBeTruthy();
  });

  it("remembers a section the customer left open", async () => {
    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { invoices: [] };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });
    window.localStorage.setItem(STORAGE_KEY, "true");

    renderCard();

    expect(
      screen.getByRole("button", { name: "Invoices" }).getAttribute("aria-expanded"),
    ).toBe("true");
    expect(await screen.findByText("No invoices yet.")).toBeTruthy();
  });

  it("shows the server error with a retry", async () => {
    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        throw new Error("invoices down");
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderCard();
    await userEvent.click(screen.getByRole("button", { name: "Invoices" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("invoices down");
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
});
