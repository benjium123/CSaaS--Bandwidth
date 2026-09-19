import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChoosePlanPage } from "@/pages/ChoosePlanPage";
import type { Plan } from "@/api/plans";
import { makeStubClient, renderWithProviders } from "@/test/harness";

/**
 * The choose-a-plan screen.
 *
 * Every absence assertion is paired with a presence assertion on the same render.
 * The screen's job is to show exactly what the server sent: unreleased plans are still
 * visible and named, the skeleton is a silence rather than a claim, and a failure is the
 * server's own sentence.
 */

const ME = {
  id: "u1",
  email: "ops@acme.co",
  full_name: "Ops",
  second_factor_required: false,
  memberships: [
    {
      org_id: "org-1",
      org_name: "Acme",
      org_slug: "acme",
      role_name: "admin",
      permissions: ["org:read", "org:update"],
    },
  ],
  permissions: ["org:read", "org:update"],
};

const CHECKOUT_URL = "https://checkout.example/url";

function plan(over: Partial<Plan> = {}): Plan {
  return {
    code: "starter",
    name: "Starter",
    included: { sms_segments: 1000, call_minutes: 120 },
    overage_rates: {},
    monthly_price_micros: 0,
    stripe_price_id: "price_starter",
    is_active: true,
    ...over,
  };
}

function renderPlans(plans: Plan[], onCheckout?: (url: string) => void) {
  const client = makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/billing/plans": plans,
    "/api/v1/billing/subscription/checkout": () => ({ checkout_url: CHECKOUT_URL }),
  });
  const result = renderWithProviders(
    <ChoosePlanPage onCheckout={onCheckout} />,
    client,
  );
  return { ...result, client };
}

function renderPendingPlans() {
  return renderWithProviders(
    <ChoosePlanPage />,
    makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/billing/plans": () => new Promise<never>(() => {}),
    }),
  );
}

function renderErrorPlans(message: string) {
  return renderWithProviders(
    <ChoosePlanPage />,
    makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/billing/plans": new Error(message),
    }),
  );
}

describe("ChoosePlanPage", () => {
  it("three plans render with their names and included allowances", async () => {
    renderPlans([
      plan({
        code: "starter",
        name: "Starter",
        included: { sms_segments: 1000, call_minutes: 120 },
      }),
      plan({
        code: "growth",
        name: "Growth",
        included: { mms_messages: 250, phone_numbers: 2 },
      }),
      plan({
        code: "scale",
        name: "Scale",
        included: { seats: 5, call_minutes: 5000, voicemail_boxes: 3 },
      }),
    ]);

    expect(await screen.findByText("Starter")).toBeTruthy();
    expect(screen.getByText("Growth")).toBeTruthy();
    expect(screen.getByText("Scale")).toBeTruthy();

    expect(screen.getByText("SMS segments")).toBeTruthy();
    expect(screen.getByText("MMS messages")).toBeTruthy();
    expect(screen.getByText("Phone numbers")).toBeTruthy();
    expect(screen.getByText("Seats")).toBeTruthy();
    expect(screen.getAllByText("Call minutes").length).toBeGreaterThan(0);
    expect(screen.getByText("voicemail boxes")).toBeTruthy();

    expect(screen.getByText("1,000")).toBeTruthy();
    expect(screen.getByText("250")).toBeTruthy();
    expect(screen.getByText("5")).toBeTruthy();
  });

  it("a plan with stripe_price_id null renders a DISABLED button plus the not-yet-published explanation, and the container text contains no currency-and-digit and no Free", async () => {
    const { container } = renderPlans([
      plan({ code: "starter", name: "Starter", stripe_price_id: null }),
    ]);

    expect(await screen.findByText("Starter")).toBeTruthy();
    expect(
      screen.getByText(
        "Pricing for this plan is not published yet. You cannot subscribe to it today.",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Not yet available" })).toBeDisabled();
    expect(container.textContent).not.toMatch(/[$\u00a3\u20ac]\s?\d/);
    expect(container.textContent).not.toMatch(/\bFree\b/i);
  });

  it("a plan WITH a stripe_price_id renders an ENABLED button", async () => {
    renderPlans([plan({ code: "starter", name: "Starter", stripe_price_id: "price_starter" })]);

    expect(await screen.findByRole("button", { name: "Choose Starter" })).toBeEnabled();
  });

  it("clicking an enabled button POSTs to the checkout endpoint with that plan's code", async () => {
    const onCheckout = vi.fn();
    const { client } = renderPlans(
      [plan({ code: "pro", name: "Pro", stripe_price_id: "price_pro" })],
      onCheckout,
    );

    const button = await screen.findByRole("button", { name: "Choose Pro" });
    await userEvent.click(button);

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/billing/subscription/checkout",
      );
      expect(call).toBeTruthy();
      expect(call?.init.method).toBe("POST");
      expect(call?.init.json).toEqual({ plan_code: "pro" });
      expect(onCheckout).toHaveBeenCalledWith(CHECKOUT_URL);
    });
  });

  it("while the query is pending, no plan names and no no-plans text appear, but the skeleton does", async () => {
    renderPendingPlans();

    await waitFor(() => {
      expect(screen.getByLabelText("Loading plans")).toBeInTheDocument();
    });
    expect(screen.queryAllByRole("button", { name: /^Choose / })).toHaveLength(0);
    expect(screen.queryByText(/no plans to choose from/i)).toBeNull();
    expect(screen.queryByText(/not published yet/i)).toBeNull();
  });

  it("a failing request renders the server's message verbatim", async () => {
    const message = "Plans are temporarily unavailable, try again in a minute.";
    renderErrorPlans(message);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(message);
    expect(screen.queryByText(/something went wrong/i)).toBeNull();
  });
});
