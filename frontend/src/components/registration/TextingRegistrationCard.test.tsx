import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type {
  RegistrationBrand,
  RegistrationCampaign,
  TextingQuote,
  TextingRegistration,
} from "@/api/registration";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

import { TEXTING_ASSERTIONS, TEXTING_TERMS_LABEL } from "./copy";
import { TextingRegistrationCard } from "./TextingRegistrationCard";

const MANAGER_PERMISSIONS = ["compliance:read", "compliance:manage"];

/** The AuthProvider fetches this itself; production wrappers wait for a signed-in business. */
const AUTH_ME = {
  id: "u-1",
  email: "u@example.com",
  full_name: "Test User",
  permissions: [],
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org One",
      org_slug: "org-one",
      role_name: "owner",
      account_type: "business",
    },
  ],
};

/** The pricing catalogue the texting card renders. `standard` prices 3 months upfront. */
const QUOTES: { standard: TextingQuote; sole_proprietor: TextingQuote } = {
  standard: {
    fee_tier: "standard",
    brand_fee_cents: 450,
    campaign_review_cents: 1500,
    service_fee_cents: 500,
    monthly_cents: 1000,
    upfront_months: 3,
    due_today_cents: 5450,
  },
  sole_proprietor: {
    fee_tier: "sole_proprietor",
    brand_fee_cents: 450,
    campaign_review_cents: 1500,
    service_fee_cents: 500,
    monthly_cents: 200,
    upfront_months: 3,
    due_today_cents: 3050,
  },
};

/** The yes/no the mixed-campaign test answers; the last key (autoRenewal) is answered last. */
const ANSWERS: Record<string, boolean> = {
  subscriberOptin: true,
  subscriberOptout: true,
  subscriberHelp: true,
  numberPool: false,
  directLending: true,
  embeddedLink: false,
  embeddedPhone: true,
  ageGated: true,
  autoRenewal: false,
};

function capabilities(permissions: string[]) {
  return {
    permissions,
    org: {
      has_provider: true,
      has_number: true,
      member_count: 2,
      registration_state: "approved",
    },
  };
}

function brand(overrides: Partial<RegistrationBrand> = {}): RegistrationBrand {
  return {
    id: "brand-1",
    name: "Acme Inc",
    entity_type: "PRIVATE_PROFIT",
    status: "draft",
    carrier_refs: {},
    last_error: null,
    missing_for_submission: [],
    ...overrides,
  };
}

function campaign(overrides: Partial<RegistrationCampaign> = {}): RegistrationCampaign {
  return {
    id: "camp-1",
    brand_id: "brand-1",
    name: "Acme Alerts",
    use_case: "MIXED",
    status: "draft",
    carrier_refs: {},
    last_error: null,
    number_count: 0,
    missing_for_submission: [],
    ...overrides,
  };
}

/** `expired` is the cheapest stage that draws the checkout form; fee_tier rides the quote. */
function registration(overrides: Partial<TextingRegistration> = {}): TextingRegistration {
  return {
    id: "reg-1",
    stage: "expired",
    brand_id: "brand-1",
    campaign_id: "camp-1",
    brand_status: null,
    campaign_status: null,
    checkout_url: null,
    otp_sent_at: null,
    detail: null,
    ...QUOTES.standard,
    ...overrides,
  };
}

interface RouteOptions {
  registration?: TextingRegistration | null;
  brands?: RegistrationBrand[];
  campaigns?: RegistrationCampaign[];
  permissions?: string[];
  checkout?: RouteStub | unknown;
  otp?: RouteStub | unknown;
}

/**
 * The harness matches by LONGEST prefix, so "/texting/reg-1/otp/resend" wins over
 * "/texting/reg-1/otp" and "/texting/checkout" wins over "/texting".
 */
function routes({
  registration: reg = null,
  brands = [brand()],
  campaigns = [campaign()],
  permissions = MANAGER_PERMISSIONS,
  checkout = { checkout_url: "https://checkout.example/pay" },
  otp = {},
}: RouteOptions = {}): Record<string, RouteStub | unknown> {
  return {
    "/api/v1/auth/me": AUTH_ME,
    "/api/v1/me/capabilities": capabilities(permissions),
    "/api/v1/registration/brands": brands,
    "/api/v1/registration/campaigns": campaigns,
    "/api/v1/registration/texting": { registration: reg, quotes: QUOTES },
    "/api/v1/registration/texting/checkout": checkout,
    "/api/v1/registration/texting/reg-1/otp": otp,
    "/api/v1/registration/texting/reg-1/otp/resend": otp,
  };
}

function expectedAssertions(): Record<string, boolean> {
  const assertions: Record<string, boolean> = {};
  for (const assertion of TEXTING_ASSERTIONS) {
    assertions[assertion.key] = ANSWERS[assertion.key];
  }
  assertions.termsAndConditions = true;
  return assertions;
}

async function answer(question: string, value: "Yes" | "No"): Promise<void> {
  await userEvent.click(
    within(screen.getByRole("group", { name: question })).getByLabelText(value),
  );
}

const OTP_INTRO =
  "The carrier texted a 6-digit code to the owner's mobile. It is valid for 24 hours.";

describe("TextingRegistrationCard checkout and pricing", () => {
  it("enables Pay and register only when the mixed-campaign form is complete, then posts the sorted payload and redirects", async () => {
    const assign = vi.fn();
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...original, assign },
    });

    try {
      const client = makeStubClient(
        routes({
          brands: [brand({ id: "brand-1", name: "Acme Inc" })],
          campaigns: [campaign({ id: "camp-1", brand_id: "brand-1", use_case: "MIXED" })],
        }),
      );
      renderWithProviders(<TextingRegistrationCard />, client);

      await screen.findByLabelText("Business profile");
      expect(screen.getByRole("button", { name: "Pay and register" })).toBeDisabled();

      await userEvent.selectOptions(screen.getByLabelText("Business profile"), "brand-1");
      await userEvent.selectOptions(screen.getByLabelText("Campaign"), "camp-1");

      // Clicked out of canonical order: the submitted list must still be sorted.
      await userEvent.click(screen.getByLabelText("Marketing"));
      await userEvent.click(screen.getByLabelText("Account notifications"));

      for (const assertion of TEXTING_ASSERTIONS.slice(0, 8)) {
        await answer(assertion.question, ANSWERS[assertion.key] ? "Yes" : "No");
      }
      expect(screen.getByRole("button", { name: "Pay and register" })).toBeDisabled();

      const ninth = TEXTING_ASSERTIONS[8];
      await answer(ninth.question, ANSWERS[ninth.key] ? "Yes" : "No");
      expect(screen.getByRole("button", { name: "Pay and register" })).toBeDisabled();

      await userEvent.click(screen.getByLabelText(TEXTING_TERMS_LABEL));
      const pay = screen.getByRole("button", { name: "Pay and register" });
      expect(pay).toBeEnabled();

      await userEvent.click(pay);

      await waitFor(() => {
        expect(
          client.calls.some(
            (call) =>
              call.path === "/api/v1/registration/texting/checkout" &&
              call.init.method === "POST",
          ),
        ).toBe(true);
      });

      const post = client.calls.find(
        (call) =>
          call.path === "/api/v1/registration/texting/checkout" &&
          call.init.method === "POST",
      );
      if (!post) throw new Error("no POST to /api/v1/registration/texting/checkout was recorded");

      expect(post.init.json).toEqual({
        brand_id: "brand-1",
        campaign_id: "camp-1",
        first_name: "",
        last_name: "",
        mobile_phone: null,
        sub_usecases: ["ACCOUNT_NOTIFICATION", "MARKETING"],
        assertions: expectedAssertions(),
      });

      await waitFor(() => expect(assign).toHaveBeenCalledWith("https://checkout.example/pay"));
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: original });
    }
  });

  it("shows the sole-proprietor fields, note and priced quote once a sole-proprietor brand is chosen", async () => {
    const client = makeStubClient(
      routes({
        brands: [brand({ id: "brand-1", name: "Acme Inc", entity_type: "SOLE_PROPRIETOR" })],
        campaigns: [campaign({ id: "camp-1", brand_id: "brand-1" })],
      }),
    );
    renderWithProviders(<TextingRegistrationCard />, client);

    await userEvent.selectOptions(await screen.findByLabelText("Business profile"), "brand-1");

    expect(screen.getByLabelText("First name")).toBeInTheDocument();
    expect(screen.getByLabelText("Last name")).toBeInTheDocument();
    expect(screen.getByLabelText("Owner's mobile")).toBeInTheDocument();
    expect(screen.getByText("Sole proprietors can text from one number.")).toBeInTheDocument();
    expect(screen.getByText("Choose 1 to 5.")).toBeInTheDocument();
    expect(screen.getByTestId("texting-quote").textContent).toBe(
      "$30.50 today (includes the first 3 months). Then $2.00/month from month four. Not refundable once filed.",
    );
  });

  it("a read-only user sees no Pay and register button and issues no writes", async () => {
    const client = makeStubClient(routes({ permissions: ["compliance:read"], registration: null }));
    renderWithProviders(<TextingRegistrationCard />, client);

    expect(
      await screen.findByText("Ask a workspace admin to register this workspace for texting."),
    ).toBeInTheDocument();

    expect(screen.queryByRole("button", { name: "Pay and register" })).toBeNull();
    expect(screen.queryByRole("form", { name: "Register for texting" })).toBeNull();
    expect(client.calls.every((call) => (call.init.method ?? "GET") === "GET")).toBe(true);
  });
});

describe("TextingRegistrationCard stages", () => {
  it("verifies the carrier code with a digits-only PIN", async () => {
    const client = makeStubClient(
      routes({
        registration: registration({ stage: "otp_pending", fee_tier: "sole_proprietor" }),
        otp: registration({ stage: "brand_approved", fee_tier: "sole_proprietor" }),
      }),
    );
    renderWithProviders(<TextingRegistrationCard />, client);

    expect(await screen.findByText(OTP_INTRO)).toBeInTheDocument();

    const verify = screen.getByRole("button", { name: "Verify" });
    expect(verify).toBeDisabled();

    const input = screen.getByLabelText("Verification code");
    await userEvent.type(input, "12ab3456");
    expect(input).toHaveValue("123456");
    expect(verify).toBeEnabled();

    await userEvent.click(verify);

    const otpPath = "/api/v1/registration/texting/reg-1/otp";
    await waitFor(() => {
      expect(
        client.calls.some((call) => call.path === otpPath && call.init.method === "POST"),
      ).toBe(true);
    });

    const post = client.calls.find(
      (call) => call.path === otpPath && call.init.method === "POST",
    );
    if (!post) throw new Error("no POST to the otp path was recorded");
    expect(post.init.json).toEqual({ pin: "123456" });
  });

  it("shows the carrier's message when the code is wrong", async () => {
    const client = makeStubClient(
      routes({
        registration: registration({ stage: "otp_pending", fee_tier: "sole_proprietor" }),
        otp: new Error("That code is not right. Try again."),
      }),
    );
    renderWithProviders(<TextingRegistrationCard />, client);

    await screen.findByText(OTP_INTRO);
    await userEvent.type(screen.getByLabelText("Verification code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));

    expect(await screen.findByText("That code is not right. Try again.")).toBeInTheDocument();
  });

  it("resends the carrier code with no body and confirms it", async () => {
    const client = makeStubClient(
      routes({
        registration: registration({ stage: "otp_pending", fee_tier: "sole_proprietor" }),
        otp: registration({ stage: "otp_pending", fee_tier: "sole_proprietor" }),
      }),
    );
    renderWithProviders(<TextingRegistrationCard />, client);

    await screen.findByText(OTP_INTRO);
    await userEvent.click(screen.getByRole("button", { name: "Send a new code" }));

    const resendPath = "/api/v1/registration/texting/reg-1/otp/resend";
    await waitFor(() => {
      expect(client.calls.some((call) => call.path === resendPath)).toBe(true);
    });

    const resend = client.calls.find((call) => call.path === resendPath);
    if (!resend) throw new Error("no POST to the resend path was recorded");
    expect(resend.init.method).toBe("POST");
    expect(resend.init.json).toBeUndefined();

    expect(await screen.findByText("A new code is on its way.")).toBeInTheDocument();
  });

  it("marks every step done for an active sole-proprietor registration", async () => {
    const client = makeStubClient(
      routes({ registration: registration({ stage: "active", fee_tier: "sole_proprietor" }) }),
    );
    renderWithProviders(<TextingRegistrationCard />, client);

    const list = await screen.findByRole("list", { name: "Texting registration progress" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(6);
    for (const item of items) {
      expect(item).toHaveAttribute("data-done", "true");
    }

    const labels = [
      "Payment received",
      "Business submitted to the carrier",
      "Owner's mobile verified",
      "Business approved",
      "Campaign submitted",
      "Carriers approved texting",
    ];
    for (const label of labels) {
      expect(within(list).getByText(label)).toBeInTheDocument();
    }

    expect(
      screen.getByText(
        "Texting is live. Your numbers are being added to your campaign automatically.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the standard five steps for a filed brand", async () => {
    const client = makeStubClient(routes({ registration: registration({ stage: "brand_filed" }) }));
    renderWithProviders(<TextingRegistrationCard />, client);

    const list = await screen.findByRole("list", { name: "Texting registration progress" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(5);

    expect(items.map((li) => li.querySelector("span:last-child")?.textContent)).toEqual([
      "Payment received",
      "Business submitted to the carrier",
      "Business approved",
      "Campaign submitted",
      "Carriers approved texting",
    ]);
    expect(items.map((li) => li.getAttribute("data-done"))).toEqual([
      "true",
      "true",
      "false",
      "false",
      "false",
    ]);
    expect(screen.queryByText("Owner's mobile verified")).toBeNull();
  });

  it("shows the attention detail and the follow-up notice", async () => {
    const client = makeStubClient(
      routes({
        registration: registration({
          stage: "needs_attention",
          detail: "Carrier asked for a new website.",
        }),
      }),
    );
    renderWithProviders(<TextingRegistrationCard />, client);

    expect(await screen.findByText("Carrier asked for a new website.")).toBeInTheDocument();
    expect(screen.getByText("Our team has been notified.")).toBeInTheDocument();
  });

  it("resumes an open checkout through the onCheckout prop", async () => {
    const onCheckout = vi.fn();
    const client = makeStubClient(
      routes({
        registration: registration({
          stage: "checkout",
          checkout_url: "https://checkout.example/resume",
        }),
      }),
    );
    renderWithProviders(<TextingRegistrationCard onCheckout={onCheckout} />, client);

    expect(await screen.findByText("Your payment page is ready.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue to payment" }));
    expect(onCheckout).toHaveBeenCalledWith("https://checkout.example/resume");
  });

  it("shows the rejection reason and the editable form for a manager", async () => {
    const client = makeStubClient(
      routes({
        registration: registration({ stage: "brand_rejected", detail: "EIN did not match." }),
      }),
    );
    renderWithProviders(<TextingRegistrationCard />, client);

    expect(
      await screen.findByText("Your last attempt was not approved: EIN did not match."),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("form", { name: "Register for texting" }),
    ).toBeInTheDocument();
  });

  it("shows the rejection reason but no form for a read-only user", async () => {
    const client = makeStubClient(
      routes({
        permissions: ["compliance:read"],
        registration: registration({ stage: "brand_rejected", detail: "EIN did not match." }),
      }),
    );
    renderWithProviders(<TextingRegistrationCard />, client);

    expect(
      await screen.findByText("Your last attempt was not approved: EIN did not match."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Register for texting" })).toBeNull();
  });
});
