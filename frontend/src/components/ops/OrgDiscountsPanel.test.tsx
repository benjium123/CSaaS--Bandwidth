import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OrgDiscountsPanel } from "./OrgDiscountsPanel";
import type { Discount } from "@/api/opsDiscounts";

const mocks = vi.hoisted(() => ({ requestMock: vi.fn() }));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ api: { request: mocks.requestMock } }),
}));

const ORG_ID = "o1";
const BASE = `/api/v1/ops/console/orgs/${ORG_ID}/discounts`;
const ALL_CATEGORIES = ["subscription", "bundles", "usage", "numbers", "tendlc"];

type RequestInitLike = { method?: string; json?: unknown };

type PutBody = {
  categories: string[];
  percent: number;
  ends_at: string | null;
  note: string | null;
};

function methodOf(init?: RequestInitLike): string {
  return init?.method ?? "GET";
}

function renderPanel(canEdit = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <OrgDiscountsPanel orgId={ORG_ID} canEdit={canEdit} />
    </QueryClientProvider>,
  );
}

function discount(overrides: Partial<Discount> & Pick<Discount, "category">): Discount {
  return {
    percent_bps: 1250,
    percent: 12.5,
    ends_at: null,
    active: true,
    note: null,
    stripe_coupon_id: null,
    updated_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  mocks.requestMock.mockReset();
});

describe("OrgDiscountsPanel", () => {
  it("renders each discount's label, percent, status pill and end date", async () => {
    const discounts: Discount[] = [
      discount({
        category: "subscription",
        percent_bps: 1250,
        percent: 12.5,
        ends_at: "2026-12-31T23:59:59Z",
        active: true,
      }),
      discount({
        category: "usage",
        percent_bps: 500,
        percent: 5,
        ends_at: null,
        active: false,
      }),
    ];

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { org_id: ORG_ID, categories: ALL_CATEGORIES, discounts };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel(false);

    expect(await screen.findByText("Subscription")).toBeTruthy();
    expect(screen.getByText("Usage")).toBeTruthy();
    expect(screen.getByText("12.5%")).toBeTruthy();
    expect(screen.getByText("5%")).toBeTruthy();
    expect(screen.getByText("Active")).toBeTruthy();
    expect(screen.getByText("Expired")).toBeTruthy();
    expect(screen.getByText("Until 2026-12-31")).toBeTruthy();
    expect(screen.getByText("No end date")).toBeTruthy();
  });

  it("shows the empty state when the workspace has no discounts", async () => {
    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { org_id: ORG_ID, categories: ALL_CATEGORIES, discounts: [] };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel(false);

    expect(
      await screen.findByText("No discounts. This workspace pays list prices."),
    ).toBeTruthy();
  });

  it("hides the form and the Remove buttons when the operator cannot edit", async () => {
    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return {
          org_id: ORG_ID,
          categories: ALL_CATEGORIES,
          discounts: [discount({ category: "usage", percent_bps: 1000, percent: 10 })],
        };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel(false);

    await screen.findByText("10%");
    expect(screen.queryByRole("button", { name: "Apply discount" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Remove / })).toBeNull();
  });

  it("PUTs the ticked categories and percent when a discount is applied", async () => {
    const user = userEvent.setup();
    let putBody: PutBody | null = null;

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { org_id: ORG_ID, categories: ALL_CATEGORIES, discounts: [] };
      }
      if (path === BASE && method === "PUT") {
        putBody = (init?.json ?? null) as PutBody | null;
        return { org_id: ORG_ID, categories: ALL_CATEGORIES, discounts: [], stripe: null };
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    await user.click(await screen.findByRole("checkbox", { name: "Usage" }));
    await user.click(screen.getByRole("checkbox", { name: "Bundles" }));
    await user.type(screen.getByLabelText("Discount %"), "10");
    await user.click(screen.getByRole("button", { name: "Apply discount" }));

    await waitFor(() => expect(putBody).not.toBeNull());
    const body = putBody as unknown as PutBody;
    expect(body.categories).toHaveLength(2);
    expect(body.categories).toEqual(expect.arrayContaining(["usage", "bundles"]));
    expect(body.percent).toBe(10);
    expect(body.ends_at).toBeNull();
    expect(body.note).toBeNull();
  });

  it("sends the chosen end date as the end of that day in UTC", async () => {
    const user = userEvent.setup();
    let putBody: PutBody | null = null;

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { org_id: ORG_ID, categories: ALL_CATEGORIES, discounts: [] };
      }
      if (path === BASE && method === "PUT") {
        putBody = (init?.json ?? null) as PutBody | null;
        return { org_id: ORG_ID, categories: ALL_CATEGORIES, discounts: [], stripe: null };
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    await user.click(await screen.findByRole("checkbox", { name: "Usage" }));
    await user.type(screen.getByLabelText("Discount %"), "10");
    fireEvent.change(screen.getByLabelText("Ends on"), { target: { value: "2026-12-31" } });
    await user.click(screen.getByRole("button", { name: "Apply discount" }));

    await waitFor(() => expect(putBody).not.toBeNull());
    expect((putBody as unknown as PutBody).ends_at).toBe("2026-12-31T23:59:59Z");
  });

  it("disables Apply until a category is ticked and the percent is valid", async () => {
    const user = userEvent.setup();

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { org_id: ORG_ID, categories: ALL_CATEGORIES, discounts: [] };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel();

    const apply = (await screen.findByRole("button", {
      name: "Apply discount",
    })) as HTMLButtonElement;

    // No category ticked yet, even with a valid percent.
    await user.type(screen.getByLabelText("Discount %"), "10");
    expect(apply.disabled).toBe(true);

    await user.click(screen.getByRole("checkbox", { name: "Usage" }));
    expect(apply.disabled).toBe(false);

    await user.clear(screen.getByLabelText("Discount %"));
    await user.type(screen.getByLabelText("Discount %"), "0");
    expect(apply.disabled).toBe(true);

    await user.clear(screen.getByLabelText("Discount %"));
    await user.type(screen.getByLabelText("Discount %"), "150");
    expect(apply.disabled).toBe(true);
  });

  it("pre-fills the percent from an existing discount when its category is ticked", async () => {
    const user = userEvent.setup();

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return {
          org_id: ORG_ID,
          categories: ALL_CATEGORIES,
          discounts: [discount({ category: "usage", percent_bps: 1250, percent: 12.5 })],
        };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel();

    await user.click(await screen.findByRole("checkbox", { name: "Usage" }));

    expect((screen.getByLabelText("Discount %") as HTMLInputElement).value).toBe("12.5");
  });

  it("DELETEs the discount for the category whose Remove button is clicked", async () => {
    const user = userEvent.setup();
    let deletePath: string | null = null;

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return {
          org_id: ORG_ID,
          categories: ALL_CATEGORIES,
          discounts: [discount({ category: "tendlc", percent_bps: 2000, percent: 20 })],
        };
      }
      if (method === "DELETE") {
        deletePath = path;
        return { org_id: ORG_ID, categories: ALL_CATEGORIES, discounts: [], stripe: null };
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Remove 10DLC fee discount" }));

    await waitFor(() => expect(deletePath).not.toBeNull());
    expect(deletePath).toBe(`${BASE}/tendlc`);
  });

  it("warns when Stripe could not be updated", async () => {
    const user = userEvent.setup();

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { org_id: ORG_ID, categories: ALL_CATEGORIES, discounts: [] };
      }
      if (path === BASE && method === "PUT") {
        return { org_id: ORG_ID, categories: ALL_CATEGORIES, discounts: [], stripe: "failed" };
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    await user.click(await screen.findByRole("checkbox", { name: "Usage" }));
    await user.type(screen.getByLabelText("Discount %"), "10");
    await user.click(screen.getByRole("button", { name: "Apply discount" }));

    expect(
      await screen.findByText(
        "Saved, but Stripe could not be updated. It will retry on their next plan change.",
      ),
    ).toBeTruthy();
  });
});
