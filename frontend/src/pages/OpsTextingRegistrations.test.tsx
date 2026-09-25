import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { OpsPage } from "@/pages/OpsPage";

const ADMIN = {
  id: "u1",
  email: "admin@example.com",
  full_name: "Admin",
  permissions: [],
  memberships: [{ org_id: "org-1", role_name: "owner" }],
  is_platform_operator: true,
  operator_role: "admin",
};

const REVIEWER = { ...ADMIN, operator_role: "reviewer" };

function registration(over: Record<string, unknown>): Record<string, unknown> {
  return {
    id: "reg-x",
    org_id: "org-x",
    org_name: "Org X",
    brand_name: "Brand X",
    campaign_name: "Campaign X",
    stage: "paid",
    fee_tier: "standard",
    detail: null,
    paid_at: null,
    updated_at: null,
    ...over,
  };
}

const REGISTRATIONS: Record<string, unknown>[] = [
  registration({
    id: "reg-att",
    org_id: "org-ada",
    org_name: "Ada Bakery",
    brand_name: "Ada",
    campaign_name: "Alerts",
    stage: "needs_attention",
    detail: "Brand filing was interrupted",
  }),
  registration({ id: "reg-paid", org_id: "org-bo", org_name: "Bo Cafe", brand_name: "Bo", campaign_name: "Bo Alerts" }),
  registration({ id: "reg-active", org_id: "org-cy", org_name: "Cy Shop", brand_name: "Cy", campaign_name: "Cy Alerts", stage: "active" }),
  registration({
    id: "reg-cancelled",
    org_id: "org-di",
    org_name: "Di Deli",
    brand_name: "Di",
    campaign_name: "Di Alerts",
    stage: "cancelled",
    detail: "Cancelled by support",
  }),
];

/** OpsPage defaults to the "queue" tab, so every stub set needs the queue payload too. */
function stubsFor(me: unknown, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    "/api/v1/auth/me": me,
    "/api/v1/ops/queue": { applications: [], open_security_alerts: 0 },
    "/api/v1/ops/texting-registrations": REGISTRATIONS,
    ...extra,
  };
}

function renderOps(stubs: Record<string, unknown>) {
  const client = makeStubClient(stubs);
  renderWithProviders(<OpsPage />, client);
  return client;
}

/** renderWithProviders has no initial entries, and a nested MemoryRouter is not allowed,
 *  so the tab is reached by clicking the nav link. */
async function openTextingTab() {
  await userEvent.click(await screen.findByRole("link", { name: "Texting registrations" }));
}

function rowFor(orgName: string): HTMLElement {
  return screen.getByRole("listitem", { name: `Texting registration for ${orgName}` });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("OpsPage - texting registrations", () => {
  it("lists each registration's stage, org, brand, campaign and detail", async () => {
    renderOps(stubsFor(ADMIN));
    await openTextingTab();

    const ada = await screen.findByRole("listitem", { name: "Texting registration for Ada Bakery" });
    expect(ada).toHaveTextContent("needs attention · Ada Bakery");
    expect(ada).toHaveTextContent("Ada / Alerts · standard");
    expect(ada).toHaveTextContent("Brand filing was interrupted");
  });

  it("offers reconcile and cancel only for the stages that allow them", async () => {
    renderOps(stubsFor(ADMIN));
    await openTextingTab();
    await screen.findByRole("listitem", { name: "Texting registration for Ada Bakery" });

    const ada = within(rowFor("Ada Bakery"));
    expect(ada.getByRole("button", { name: "Reconcile with carrier" })).toBeInTheDocument();
    expect(ada.getByRole("button", { name: "Cancel and refund" })).toBeInTheDocument();

    const bo = within(rowFor("Bo Cafe"));
    expect(bo.getByRole("button", { name: "Cancel and refund" })).toBeInTheDocument();
    expect(bo.queryByRole("button", { name: "Reconcile with carrier" })).toBeNull();

    for (const orgName of ["Cy Shop", "Di Deli"]) {
      expect(within(rowFor(orgName)).queryAllByRole("button")).toHaveLength(0);
    }
  });

  it("shows the list but no action buttons to a reviewer", async () => {
    renderOps(stubsFor(REVIEWER));
    await openTextingTab();

    const ada = await screen.findByRole("listitem", { name: "Texting registration for Ada Bakery" });
    expect(ada).toHaveTextContent("needs attention · Ada Bakery");
    expect(within(ada).queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Cancel and refund" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reconcile with carrier" })).toBeNull();
  });

  it("reconciles through the carrier and shows the outcome verbatim", async () => {
    const client = renderOps(
      stubsFor(ADMIN, {
        "/api/v1/ops/texting-registrations/reg-att/reconcile": {
          stage: "brand_filed",
          outcome: "linked the brand Telnyx created",
        },
      }),
    );
    await openTextingTab();

    const ada = within(await screen.findByRole("listitem", { name: "Texting registration for Ada Bakery" }));
    await userEvent.click(ada.getByRole("button", { name: "Reconcile with carrier" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/ops/texting-registrations/reg-att/reconcile" && c.init.method === "POST",
        ),
      ).toBe(true),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(/^linked the brand Telnyx created$/);
  });

  it("shows a reconcile failure verbatim", async () => {
    renderOps(
      stubsFor(ADMIN, {
        "/api/v1/ops/texting-registrations/reg-att/reconcile": () =>
          new Error("Filing was attempted minutes ago; try again later."),
      }),
    );
    await openTextingTab();

    const ada = within(await screen.findByRole("listitem", { name: "Texting registration for Ada Bakery" }));
    await userEvent.click(ada.getByRole("button", { name: "Reconcile with carrier" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      /^Filing was attempted minutes ago; try again later\.$/,
    );
  });

  it("cancels after confirmation and shows the refund detail verbatim", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const client = renderOps(
      stubsFor(ADMIN, {
        "/api/v1/ops/texting-registrations/reg-paid/cancel": {
          stage: "cancelled",
          detail: "Refunded $44.00; nothing was filed.",
        },
      }),
    );
    await openTextingTab();

    const bo = within(await screen.findByRole("listitem", { name: "Texting registration for Bo Cafe" }));
    await userEvent.click(bo.getByRole("button", { name: "Cancel and refund" }));

    expect(confirm).toHaveBeenCalledWith(
      "Cancel the texting registration for Bo Cafe and refund what the carrier never charged?",
    );
    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/ops/texting-registrations/reg-paid/cancel" && c.init.method === "POST",
        ),
      ).toBe(true),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(/^Refunded \$44\.00; nothing was filed\.$/);
  });

  it("does nothing when the cancel confirmation is declined", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const client = renderOps(stubsFor(ADMIN));
    await openTextingTab();

    const bo = within(await screen.findByRole("listitem", { name: "Texting registration for Bo Cafe" }));
    await userEvent.click(bo.getByRole("button", { name: "Cancel and refund" }));

    expect(confirm).toHaveBeenCalledWith(
      "Cancel the texting registration for Bo Cafe and refund what the carrier never charged?",
    );
    expect(client.calls.filter((c) => c.path.endsWith("/cancel"))).toHaveLength(0);
    expect(screen.queryByRole("status")).toBeNull();
  });
});
