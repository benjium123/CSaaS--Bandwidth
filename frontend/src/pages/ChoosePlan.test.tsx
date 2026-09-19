import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AuthSurface } from "@/components/auth/AuthShell";
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

/**
 * The layout the page sits in.
 *
 * WHAT THESE CANNOT DO: vitest runs with `css: false`, so no assertion here can see the
 * overflow that prompted this change - jsdom lays nothing out and choosePlan.css is never
 * parsed. These pin the two structural facts the fix rests on, and the browser check is
 * still a separate, manual job.
 *
 * Each absence is paired with a presence on an ORDINARY AuthSurface in the same file, so
 * a typo that made the aside's headline unfindable would fail the pair rather than let
 * the absence pass vacuously.
 */
describe("ChoosePlanPage sits in the wide AuthSurface, not the two-column one", () => {
  const ASIDE_HEADLINE = "on one line.";

  it("an ordinary AuthSurface renders the aside headline", () => {
    const { container } = renderWithProviders(
      <AuthSurface>
        <p>ordinary page body</p>
      </AuthSurface>,
      makeStubClient({ "/api/v1/auth/me": ME }),
    );

    expect(screen.getByText("ordinary page body")).toBeTruthy();
    expect(container.textContent).toContain(ASIDE_HEADLINE);
  });

  it("the loaded plan page renders no aside content", async () => {
    const { container } = renderPlans([plan({ code: "starter", name: "Starter" })]);

    expect(await screen.findByText("Starter")).toBeTruthy();
    expect(container.textContent).not.toContain(ASIDE_HEADLINE);
  });

  it("the pending and error states render no aside content either", async () => {
    const pending = renderPendingPlans();
    await waitFor(() => {
      expect(screen.getByLabelText("Loading plans")).toBeInTheDocument();
    });
    expect(pending.container.textContent).not.toContain(ASIDE_HEADLINE);
    pending.unmount();

    const failed = renderErrorPlans("Plans are temporarily unavailable.");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Plans are temporarily unavailable.");
    expect(failed.container.textContent).not.toContain(ASIDE_HEADLINE);
  });

  it("the theme toggle is still reachable on the wide page", async () => {
    renderPlans([plan({ code: "starter", name: "Starter" })]);

    expect(await screen.findByText("Starter")).toBeTruthy();
    expect(screen.getAllByRole("button").some((b) => /theme|dark|light/i.test(b.getAttribute("aria-label") ?? b.textContent ?? ""))).toBe(true);
  });

  it("choosePlan.css contains no viewport-unit breakout", () => {
    // vitest's root is the frontend package, and `import.meta.url` here is a dev-server
    // URL rather than a file one, so resolve from the root explicitly.
    const css = readFileSync(resolve(process.cwd(), "src/pages/choosePlan.css"), "utf8");
    expect(css).toContain(".cp-plans");
    expect(css.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/\d*vw\b|margin-inline/);
  });
});
