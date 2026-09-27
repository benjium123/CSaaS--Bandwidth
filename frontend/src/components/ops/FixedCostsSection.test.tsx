import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { formatMicros } from "@/api/spend";
import { FixedCostsSection, type FixedCost } from "./FixedCostsSection";

const mocks = vi.hoisted(() => ({ requestMock: vi.fn() }));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ api: { request: mocks.requestMock } }),
}));

const BASE = "/api/v1/ops/console/fixed-costs";

type RequestInitLike = { method?: string; json?: unknown };

function methodOf(init?: RequestInitLike): string {
  return init?.method ?? "GET";
}

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <FixedCostsSection />
    </QueryClientProvider>,
  );
}

function cost(overrides: Partial<FixedCost> & Pick<FixedCost, "id" | "name">): FixedCost {
  return {
    monthly_micros: 0,
    starts_on: "2020-01-01",
    ends_on: null,
    note: null,
    updated_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  mocks.requestMock.mockReset();
});

describe("FixedCostsSection", () => {
  it("lists costs, shows Ongoing for open-ended ones and totals the costs running today", async () => {
    const costs: FixedCost[] = [
      cost({ id: "fc-1", name: "VPS", monthly_micros: 18_000_000, note: "Hetzner" }),
      cost({ id: "fc-2", name: "Old database", monthly_micros: 5_000_000, ends_on: "2020-06-01" }),
    ];
    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") return { fixed_costs: costs };
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderSection();

    expect(await screen.findByText("VPS")).toBeTruthy();
    expect(screen.getByText("Old database")).toBeTruthy();
    expect(screen.getByText("Ongoing")).toBeTruthy();
    expect(screen.getByText("2020-06-01")).toBeTruthy();

    const totalLine = await screen.findByText(/^Total per month:/);
    expect(totalLine.textContent).toBe(`Total per month: ${formatMicros(18_000_000)}`);
  });

  it("creates a cost, converting the dollar input to micros", async () => {
    const user = userEvent.setup();
    let postBody: unknown = null;

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") return { fixed_costs: [] };
      if (path === BASE && method === "POST") {
        postBody = init?.json ?? null;
        return cost({ id: "fc-new", name: "VPS", monthly_micros: 18_000_000 });
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderSection();

    await user.click(await screen.findByRole("button", { name: "Add cost" }));
    await user.type(screen.getByLabelText("Name"), "VPS");
    await user.type(screen.getByLabelText("Per month in dollars"), "18");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(postBody).not.toBeNull());

    expect(postBody).toMatchObject({
      name: "VPS",
      monthly_micros: 18_000_000,
      ends_on: null,
    });
    const body = postBody as { starts_on: string };
    expect(body.starts_on).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("requires the inline confirmation before deleting", async () => {
    const user = userEvent.setup();
    let deletedPath: string | null = null;

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { fixed_costs: [cost({ id: "fc-42", name: "VPS", monthly_micros: 18_000_000 })] };
      }
      if (method === "DELETE") {
        deletedPath = path;
        return undefined;
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderSection();

    expect(await screen.findByText("VPS")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Confirm delete" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Delete" }));

    const confirm = await screen.findByRole("button", { name: "Confirm delete" });
    expect(deletedPath).toBeNull();

    await user.click(confirm);

    await waitFor(() => expect(deletedPath).toBe(`${BASE}/fc-42`));
  });

  it("shows the server error message when a write is rejected", async () => {
    const user = userEvent.setup();

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { fixed_costs: [cost({ id: "fc-403", name: "VPS", monthly_micros: 18_000_000 })] };
      }
      if (method === "DELETE") {
        throw Object.assign(new Error("Forbidden: operator needs the admin role"), { status: 403 });
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderSection();

    expect(await screen.findByText("VPS")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Confirm delete" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toMatch(/forbidden/i);
  });
});
