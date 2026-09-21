import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { OnboardingPage } from "@/pages/OnboardingPage";
import type { KycPerson, KycProfile, KycStatus } from "@/api/kyc";
import { makeStubClient, renderWithProviders } from "@/test/harness";

/**
 * The individual journey.
 *
 * Every absence assertion is paired with a presence assertion on the same render, so a
 * component that failed to mount cannot make the suite green.
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
      role_name: "admin",
      permissions: ["org:read", "org:update"],
    },
  ],
  permissions: ["org:read", "org:update"],
};

function person(over: Partial<KycPerson> = {}): KycPerson {
  return {
    id: "p1",
    role: "owner",
    full_name: "Jane Smith",
    email: null,
    ownership_percent: null,
    is_user: true,
    is_you: true,
    status: "not_started",
    verified_name: null,
    document_country: null,
    verified_at: null,
    last_error: null,
    residential_address: null,
    ...over,
  };
}

function profile(status: KycStatus, over: Partial<KycProfile> = {}): KycProfile {
  return {
    status,
    account_type: "individual",
    business: {
      country: null, legal_name: null, dba_name: null, entity_type: null,
      registration_number: null, tax_id: null, incorporation_date: null,
      registered_address: null, operating_address: null, website: null,
      business_email: null, business_phone: null,
    },
    use_case: null,
    use_case_pending: null,
    persons: [],
    documents: [],
    checks: {},
    agreement: { current_version: "1", accepted_version: null, accepted_at: null },
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

function render(p: KycProfile) {
  return renderWithProviders(
    <OnboardingPage />,
    makeStubClient({ "/api/v1/auth/me": ME, "/api/v1/kyc/profile": p }),
  );
}

function step(title: string): HTMLElement {
  const li = screen.getByText(title).closest("li");
  if (!li) throw new Error("no step row for " + title);
  return li as HTMLElement;
}
const tagOf = (title: string) => step(title).querySelector(".ob-step-tag")?.textContent?.trim();
const nodeOf = (title: string) => step(title).querySelector(".ob-node")?.textContent?.trim();

describe("IndividualOnboarding — personal steps carry no business documents", () => {
  it("renders personal steps and no documents or ownership step", async () => {
    render(profile("draft", { missing: ["legal_name", "agreement"] }));
    expect(await screen.findByText("Your details")).toBeTruthy();
    expect(screen.getByText("Prove it's you")).toBeTruthy();
    expect(screen.getByText("Agreement")).toBeTruthy();
    // Paired absences: the business-only steps must not appear on an individual journey.
    expect(screen.queryByText("Documents")).toBeNull();
    expect(screen.queryByText("Owners")).toBeNull();
  });

  it("uses the real personal label for the personal-details step", async () => {
    render(profile("draft", { missing: ["legal_name"] }));
    // The step is open by default because it is the first incomplete one.
    expect(await screen.findByText("Full legal name")).toBeTruthy();
  });
});

describe("IndividualOnboarding — the ID check is pending until a verified applicant exists", () => {
  it("does not mark the ID step Done when missing is empty and there is no person", async () => {
    // The exact bug: an individual profile with `missing: []` and no person row. The server
    // cannot name `owner` or `id_verification` before a person exists, so an empty list is
    // not evidence - the applicant step must be todo and the ID step must be waiting.
    render(profile("draft", { missing: [], persons: [] }));
    await screen.findByText("Prove it's you");
    expect(tagOf("Prove it's you")).not.toBe("Done");
    expect(step("Prove it's you").className).not.toContain("is-done");
    expect(nodeOf("Prove it's you")).not.toBe("✓");
    expect(tagOf("Prove it's you")).toBe("Add yourself first");
    expect(step("Prove it's you").className).toContain("is-waiting");
    // And the applicant step is the one that opens, not the ID step.
    expect(tagOf("You")).toBe("1 left");
  });

  it("opens the applicant step when only `owner` is missing", async () => {
    render(profile("draft", { missing: ["owner"], persons: [] }));
    await screen.findByText("You");
    expect(tagOf("You")).toBe("1 left");
    expect(step("You").className).toContain("is-open");
    expect(tagOf("Prove it's you")).toBe("Add yourself first");
  });

  it("does not mark the ID step Done when the applicant exists but is not verified", async () => {
    render(
      profile("draft", {
        missing: [],
        persons: [person({ status: "pending" })],
      }),
    );
    await screen.findByText("Prove it's you");
    expect(tagOf("Prove it's you")).toBe("1 left");
    expect(step("Prove it's you").className).not.toContain("is-done");
  });

  it("marks the ID step Done only once the applicant is verified and the key is absent", async () => {
    render(
      profile("draft", {
        missing: ["agreement"],
        persons: [person({ status: "verified", verified_at: "2026-01-01T00:00:00+00:00" })],
      }),
    );
    await screen.findByText("Prove it's you");
    expect(tagOf("Prove it's you")).toBe("Done");
    expect(step("Prove it's you").className).toContain("is-done");
    expect(nodeOf("Prove it's you")).toBe("✓");
  });

  it("does not mark the ID step Done for a verified person who is not the self owner", async () => {
    // Malformed fixture: a verified person with the wrong role and not the viewer. Neither
    // the applicant step nor the ID step may read Done off somebody else's row.
    render(
      profile("draft", {
        missing: [],
        persons: [
          person({
            role: "beneficial_owner",
            is_you: false,
            status: "verified",
            verified_at: "2026-01-01T00:00:00+00:00",
          }),
        ],
      }),
    );
    await screen.findByText("Prove it's you");
    expect(tagOf("Prove it's you")).not.toBe("Done");
    expect(step("Prove it's you").className).not.toContain("is-done");
    expect(tagOf("Prove it's you")).toBe("Add yourself first");
    expect(tagOf("You")).toBe("1 left");
    // And no submit button, because the augmented missing is not empty.
    expect(screen.queryByRole("button", { name: "Submit for review" })).toBeNull();
  });
});

describe("IndividualOnboarding — a verified person still waits on a super-admin", () => {
  it("says awaiting super-admin approval after submission, even with a verified person", async () => {
    render(
      profile("submitted", {
        submitted_at: "2026-09-10T00:00:00+00:00",
        persons: [person({ status: "verified", verified_at: "2026-09-01T00:00:00+00:00" })],
      }),
    );
    expect(await screen.findByText("Awaiting super-admin approval")).toBeTruthy();
    expect(screen.getByText(/completing the ID check is not approval/)).toBeTruthy();
    // No SMS/MMS promise anywhere on the waiting screen.
    expect(screen.queryByText(/texting unlock/i)).toBeNull();
  });
});

describe("IndividualOnboarding — approved is calling-only", () => {
  it("states calling is on and texting is not, with no SMS promise", async () => {
    render(profile("approved", { decided_at: "2026-09-12T00:00:00+00:00" }));
    expect(await screen.findByText("You're verified")).toBeTruthy();
    expect(screen.getByText(/Calling is on\. Texting is not available/)).toBeTruthy();
    expect(screen.queryByText(/texting are on/i)).toBeNull();
  });

  it("does not promise texting in the pending use-case notice", async () => {
    render(
      profile("approved", {
        use_case_pending: {
          description: "Appointment reminders", vertical: "healthcare",
          who_you_contact: "patients", list_source: "bookings",
          monthly_calls: 100, monthly_texts: 0, destination_countries: ["US"],
        },
      }),
    );
    expect(await screen.findByText(/with a super-admin/)).toBeTruthy();
    expect(screen.queryByText(/calling and texting is with a reviewer/i)).toBeNull();
  });
});

describe("IndividualOnboarding — business behavior is preserved", () => {
  it("still renders the business steps for a business profile", async () => {
    render(
      profile("draft", {
        account_type: "business",
        missing: ["legal_name", "owner", "agreement"],
      }),
    );
    expect(await screen.findByText("Your business")).toBeTruthy();
    expect(screen.getByText("Owners")).toBeTruthy();
    expect(screen.getByText("Documents")).toBeTruthy();
  });
});
