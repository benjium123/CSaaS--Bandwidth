import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VerifyBusinessPage } from "@/pages/VerifyBusinessPage";
import type { KycBusiness, KycPerson, KycProfile, KycStatus } from "@/api/kyc";
import { makeStubClient, renderWithProviders } from "@/test/harness";

/**
 * Personal identity redo.
 *
 * Three ways a personal account can be asked to repeat identity verification even though the self
 * person is still marked "verified":
 *   - needs_info with `id_verification` in `missing` (the reviewer asked for a fresh check);
 *   - reverification_due where the last verification predates `next_reverification_at`;
 *   - suspended where the annual identity is stale (verified_at absent or earlier than the
 *     cutoff). A fresh verification at/after the cutoff is accepted.
 * In all cases the Verify button must be offered, the identity section must not read as
 * complete, and Submit must stay disabled.
 */

const ME = {
  id: "u1",
  email: "me@example.com",
  full_name: "Me",
  second_factor_required: false,
  memberships: [
    {
      org_id: "org-1",
      org_name: "Personal",
      org_slug: "personal",
      role_name: "owner",
      permissions: ["org:read", "org:update"],
    },
  ],
  permissions: ["org:read", "org:update"],
};

const BUSINESS: KycBusiness = {
  country: "US",
  legal_name: "Jane Smith",
  dba_name: null,
  entity_type: null,
  registration_number: null,
  tax_id: null,
  incorporation_date: null,
  registered_address: null,
  operating_address: null,
  website: null,
  business_email: "jane@example.com",
  business_phone: "+15555550100",
};

function person(over: Partial<KycPerson> = {}): KycPerson {
  return {
    id: "p1",
    role: "owner",
    full_name: "Jane Smith",
    email: "jane@example.com",
    ownership_percent: null,
    is_user: true,
    is_you: true,
    status: "verified",
    verified_name: "Jane Smith",
    document_country: "US",
    verified_at: "2026-01-01T00:00:00+00:00",
    last_error: null,
    residential_address: null,
    ...over,
  };
}

function profile(status: KycStatus, over: Partial<KycProfile> = {}): KycProfile {
  return {
    status,
    account_type: "individual",
    supported_countries: ["US"],
    business: BUSINESS,
    use_case: {
      description: "Calling my customers",
      vertical: "personal",
      who_you_contact: "My customers",
      list_source: "My own records",
      monthly_calls: 10,
      monthly_texts: 0,
      destination_countries: ["US"],
      sample_script: null,
    },
    use_case_pending: null,
    persons: [person()],
    documents: [],
    checks: {},
    agreement: { current_version: "1", accepted_version: "1", accepted_at: "2026-01-01T00:00:00+00:00" },
    missing: [],
    info_request: null,
    submitted_at: null,
    decided_at: null,
    decision_reason: null,
    limits: null,
    deposit_required_cents: null,
    next_reverification_at: null,
    ...over,
  };
}

function render(p: KycProfile, verifyResult: unknown) {
  return renderWithProviders(
    <VerifyBusinessPage />,
    makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/kyc/profile": p,
      "/api/v1/kyc/persons/p1/verify": verifyResult,
    }),
  );
}

function identityStep(): HTMLElement {
  const el = document.getElementById("kyc-step-3");
  if (!el) throw new Error("no kyc-step-3");
  return el;
}

const identityDone = () => (identityStep().textContent ?? "").includes("✓");

const submitButton = () => screen.queryByRole("button", { name: /submit for review/i });

describe("Individual reverification — needs_info with a stale verified self", () => {
  it("offers Verify, does not mark identity complete, and keeps Submit disabled", async () => {
    render(profile("needs_info", { missing: ["id_verification"] }), new Error("verify failed"));

    // The Verify button must be visible even though the self person is still "verified".
    const button = await screen.findByRole("button", { name: /verify my id/i });
    expect(button).toBeTruthy();

    // The identity section must not be marked complete.
    expect(identityDone()).toBe(false);

    // Submit must stay disabled because id_verification is still missing.
    expect(submitButton()).toBeDisabled();

    // Clicking Verify invokes the /verify endpoint; the Error stub prevents navigation.
    await userEvent.click(button);
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  });
});

describe("Individual reverification — reverification_due freshness", () => {
  it("offers Verify and stays incomplete when the last verification predates the cutoff", async () => {
    render(
      profile("reverification_due", {
        next_reverification_at: "2026-06-01T00:00:00+00:00",
        persons: [person({ verified_at: "2026-01-01T00:00:00+00:00" })],
      }),
      new Error("verify failed"),
    );

    expect(await screen.findByRole("button", { name: /verify my id/i })).toBeTruthy();
    expect(identityDone()).toBe(false);
  });

  it("marks identity complete when the last verification is at/after the cutoff", async () => {
    render(
      profile("reverification_due", {
        next_reverification_at: "2026-06-01T00:00:00+00:00",
        persons: [person({ verified_at: "2026-07-01T00:00:00+00:00" })],
      }),
      new Error("verify failed"),
    );

    // Wait for the page to render, then assert the identity section reads complete.
    await screen.findByText("Your ID check");
    expect(identityDone()).toBe(true);
  });

  it("stays incomplete when the cutoff is absent", async () => {
    render(
      profile("reverification_due", {
        next_reverification_at: null,
        persons: [person({ verified_at: "2026-07-01T00:00:00+00:00" })],
      }),
      new Error("verify failed"),
    );

    await screen.findByText("Your ID check");
    expect(identityDone()).toBe(false);
  });
});

describe("Individual reverification — suspended with a stale annual identity", () => {
  it("offers Verify, does not mark identity complete, and keeps Submit disabled", async () => {
    render(
      profile("suspended", {
        next_reverification_at: "2026-06-01T00:00:00+00:00",
        persons: [person({ verified_at: "2026-01-01T00:00:00+00:00" })],
      }),
      new Error("verify failed"),
    );

    // The Verify button must be visible even though the self person is still "verified".
    const button = await screen.findByRole("button", { name: /verify my id/i });
    expect(button).toBeTruthy();

    // The identity section must not be marked complete.
    expect(identityDone()).toBe(false);

    // Suspended accounts are not editable, so there is no Submit button at all.
    expect(submitButton()).toBeNull();

    // Clicking Verify invokes the /verify endpoint; the Error stub prevents navigation.
    await userEvent.click(button);
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  });

  it("does not offer Verify when the annual identity is fresh", async () => {
    render(
      profile("suspended", {
        next_reverification_at: "2026-06-01T00:00:00+00:00",
        persons: [person({ verified_at: "2026-07-01T00:00:00+00:00" })],
      }),
      new Error("verify failed"),
    );

    // Wait for the page to render, then assert the Verify button is absent.
    await screen.findByText("Your ID check");
    expect(screen.queryByRole("button", { name: /verify my id/i })).toBeNull();
  });
});
