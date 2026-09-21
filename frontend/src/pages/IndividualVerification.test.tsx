import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VerifyBusinessPage } from "./VerifyBusinessPage";
import type { Me } from "@/auth/AuthContext";
import type { KycBusiness, KycPerson, KycProfile, KycUseCase } from "@/api/kyc";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const PROFILE_PATH = "/api/v1/kyc/profile";
const BUSINESS_PATH = "/api/v1/kyc/profile/business";
const USE_CASE_PATH = "/api/v1/kyc/profile/use-case";
const PERSONS_PATH = "/api/v1/kyc/persons";
const SUBMIT_PATH = "/api/v1/kyc/submit";

const ORG_UPDATE = ["org:update"];

function emptyBusiness(overrides: Partial<KycBusiness> = {}): KycBusiness {
  return {
    country: null,
    legal_name: null,
    dba_name: null,
    entity_type: null,
    registration_number: null,
    tax_id: null,
    incorporation_date: null,
    registered_address: null,
    operating_address: null,
    website: null,
    business_email: null,
    business_phone: null,
    ...overrides,
  };
}

function emptyUseCase(overrides: Partial<KycUseCase> = {}): KycUseCase {
  return {
    description: "",
    vertical: "",
    who_you_contact: "",
    list_source: "",
    monthly_calls: 0,
    monthly_texts: 0,
    destination_countries: ["US"],
    sample_script: "",
    ...overrides,
  };
}

function person(overrides: Partial<KycPerson> = {}): KycPerson {
  return {
    id: "person-1",
    role: "owner",
    full_name: "Ada Lovelace",
    email: "ada@example.com",
    ownership_percent: null,
    is_user: true,
    is_you: true,
    status: "not_started",
    verified_name: null,
    document_country: null,
    verified_at: null,
    last_error: null,
    ...overrides,
  };
}

function profile(overrides: Partial<KycProfile> = {}): KycProfile {
  return {
    status: "draft",
    account_type: "individual",
    supported_countries: ["US", "CA", "GB"],
    business: emptyBusiness(),
    use_case: null,
    use_case_pending: null,
    persons: [],
    documents: [],
    checks: {},
    agreement: { current_version: "v1", accepted_version: null, accepted_at: null },
    missing: [],
    info_request: null,
    submitted_at: null,
    decided_at: null,
    decision_reason: null,
    limits: null,
    deposit_required_cents: null,
    next_reverification_at: null,
    ...overrides,
  };
}

/** The POST body the personal PeopleStep sends. `is_me` is not on KycPerson. */
type PersonCreateInput = {
  role: KycPerson["role"];
  full_name: string;
  email: string | null;
  is_me: boolean;
  ownership_percent?: number | null;
};

interface StubOptions {
  profile?: KycProfile;
  permissions?: string[];
}

/**
 * One stub per KYC path. The harness matches by LONGEST prefix, so the profile key also
 * answers its own children; each handler branches on the method and the trailing segment
 * rather than relying on a shorter key that could shadow the longer path.
 */
function stubRoutes({
  profile: initial = profile(),
  permissions = ORG_UPDATE,
}: StubOptions = {}) {
  let current = initial;

  const profileRoute: RouteStub = (path, init) => {
    const method = init.method ?? "GET";
    if (method === "GET") return current;
    if (path === BUSINESS_PATH) {
      current = { ...current, business: { ...current.business, ...(init.json as Partial<KycBusiness>) } };
      return current.business;
    }
    if (path === USE_CASE_PATH) {
      current = { ...current, use_case: init.json as KycUseCase };
      return current.use_case;
    }
    return current;
  };

  const personsRoute: RouteStub = (path, init) => {
    const method = init.method ?? "GET";
    // The verify branch must be handled BEFORE the create branch: a POST to
    // /persons/<id>/verify also starts with the persons prefix, and creating a person for it
    // would be wrong. Returning an Error keeps jsdom from navigating on window.location.assign.
    if (method === "POST" && path.endsWith("/verify")) {
      return new Error("verify stub");
    }
    if (method === "POST") {
      const body = (init.json ?? {}) as PersonCreateInput;
      const created = person({
        id: "person-new",
        role: body.role ?? "owner",
        full_name: body.full_name ?? "",
        email: body.email ?? null,
        ownership_percent: body.ownership_percent ?? null,
        is_you: body.is_me ?? false,
        is_user: body.is_me ?? false,
        status: "not_started",
      });
      current = { ...current, persons: [...current.persons, created] };
      return created;
    }
    return current.persons;
  };

  // hasPermission reads Me.permissions, so the grant lives here - NOT on capabilities.
  const me: Me = {
    id: "u-1",
    email: "ada@example.com",
    full_name: "Ada Lovelace",
    permissions,
    memberships: [
      {
        org_id: "org-1",
        org_name: "Ada's Workspace",
        org_slug: "ada-workspace",
        role_name: "owner",
        account_type: "individual",
      },
    ],
  };

  return {
    "/api/v1/auth/me": me,
    "/api/v1/me/capabilities": {
      permissions,
      org: { has_provider: true, has_number: true, member_count: 1, registration_state: "approved" },
    },
    [PROFILE_PATH]: profileRoute,
    [PERSONS_PATH]: personsRoute,
    [SUBMIT_PATH]: { status: "submitted" },
  };
}

function businessCalls(client: ReturnType<typeof makeStubClient>) {
  return client.calls.filter((call) => call.path === BUSINESS_PATH && call.init.method === "PUT");
}

function useCaseCalls(client: ReturnType<typeof makeStubClient>) {
  return client.calls.filter((call) => call.path === USE_CASE_PATH && call.init.method === "PUT");
}

function personPosts(client: ReturnType<typeof makeStubClient>) {
  return client.calls.filter((call) => call.path === PERSONS_PATH && call.init.method === "POST");
}

describe("IndividualVerificationPage", () => {
  it("saves the three personal fields and reuses the signed-in email", async () => {
    const client = makeStubClient(
      stubRoutes({
        profile: profile({
          business: emptyBusiness({
            country: "US",
            legal_name: "Ada Lovelace",
            business_email: "ada@example.com",
            business_phone: "+15550100",
            // Fields the personal form must NOT send.
            dba_name: "Ada Co",
            entity_type: "llc",
            registration_number: "12345",
            tax_id: "99-9999999",
            website: "https://ada.example.com",
          }),
        }),
      }),
    );
    renderWithProviders(<VerifyBusinessPage />, client);

    // Await a real positive render before touching the form.
    expect(await screen.findByLabelText("Full legal name")).toBeInTheDocument();
    expect(screen.getByLabelText("Country")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /email/i })).toBeNull();
    expect(screen.getByLabelText("Phone number")).toBeInTheDocument();

    const country = screen.getByLabelText("Country");
    await userEvent.clear(country);
    await userEvent.type(country, "de");
    expect(country).toHaveValue("DE");

    await userEvent.click(screen.getByRole("button", { name: "Save and continue" }));

    await waitFor(() => expect(businessCalls(client)).toHaveLength(1));
    const body = businessCalls(client)[0].init.json as Record<string, unknown>;
    expect(body).toEqual({
      country: "DE",
      legal_name: "Ada Lovelace",
      business_email: "ada@example.com",
      business_phone: "+15550100",
    });
    expect(body).not.toHaveProperty("dba_name");
    expect(body).not.toHaveProperty("entity_type");
    expect(body).not.toHaveProperty("registration_number");
    expect(body).not.toHaveProperty("tax_id");
    expect(body).not.toHaveProperty("website");
    expect(body).not.toHaveProperty("registered_address");
  });

  it("forces personal use case and hides company and text fields", async () => {
    const client = makeStubClient(
      stubRoutes({
        profile: profile({
          use_case: emptyUseCase({
            description: "Calling customers",
            vertical: "home_services",
            who_you_contact: "Customers",
            list_source: "Bookings",
            monthly_calls: 10,
            monthly_texts: 500,
          }),
        }),
      }),
    );
    renderWithProviders(<VerifyBusinessPage />, client);

    expect(await screen.findByLabelText("What will you use calling for")).toBeInTheDocument();
    // Company-only fields are absent on the personal path.
    expect(screen.queryByLabelText("Line of business")).toBeNull();
    expect(screen.queryByLabelText("Texts per month")).toBeNull();
    expect(screen.queryByLabelText("Legal business name")).toBeNull();

    const calls = screen.getByLabelText("Calls per month");
    expect(calls).toHaveAttribute("inputmode", "numeric");
    await userEvent.clear(calls);
    await userEvent.type(calls, "125");
    expect(calls).toHaveValue(125);

    await userEvent.click(screen.getByRole("button", { name: "Save use case" }));

    await waitFor(() => expect(useCaseCalls(client)).toHaveLength(1));
    const body = useCaseCalls(client)[0].init.json as KycUseCase;
    expect(body.vertical).toBe("personal");
    expect(body.monthly_texts).toBe(0);
    expect(body.monthly_calls).toBe(125);
    expect(body.description).toBe("Calling customers");
  });

  it("starts identity verification from saved details without asking for name or email again", async () => {
    const client = makeStubClient(stubRoutes({
      profile: profile({
        business: emptyBusiness({
          country: "DE",
          legal_name: "Ada Lovelace",
          business_email: "ada@example.com",
          business_phone: "+491234567",
        }),
      }),
    }));
    renderWithProviders(<VerifyBusinessPage />, client);

    expect(await screen.findByRole("button", { name: "Start ID check" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /email/i })).toBeNull();
    expect(screen.queryByLabelText("Role")).toBeNull();
    expect(screen.queryByLabelText("Ownership percent")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Start ID check" }));

    await waitFor(() => expect(personPosts(client)).toHaveLength(1));
    const body = personPosts(client)[0].init.json as Record<string, unknown>;
    expect(body).toEqual({
      role: "owner",
      full_name: "Ada Lovelace",
      email: "ada@example.com",
      is_me: true,
    });
    expect(body).not.toHaveProperty("ownership_percent");
    expect(await screen.findByRole("alert")).toHaveTextContent("verify stub");
  });

  it("does not offer the add form once a person exists", async () => {
    const client = makeStubClient(
      stubRoutes({ profile: profile({ persons: [person({ status: "not_started" })] }) }),
    );
    renderWithProviders(<VerifyBusinessPage />, client);

    expect(await screen.findByText(/Ada Lovelace/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start ID check" })).toBeNull();
    expect(screen.queryByLabelText("Person email")).toBeNull();
  });

  it("keeps submit unavailable while missing is empty but no verified self owner exists", async () => {
    const client = makeStubClient(
      stubRoutes({ profile: profile({ missing: [], persons: [] }) }),
    );
    renderWithProviders(<VerifyBusinessPage />, client);

    // Wait for the page to render before asserting the disabled state.
    expect(await screen.findByRole("button", { name: "Submit for review" })).toBeDisabled();
    expect(screen.getByText("Identity verification")).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/didit/i);
  });

  it("never enables submit for a verified person who is not you", async () => {
    const client = makeStubClient(
      stubRoutes({
        profile: profile({
          missing: [],
          persons: [person({ is_you: false, is_user: true, status: "verified" })],
        }),
      }),
    );
    renderWithProviders(<VerifyBusinessPage />, client);

    expect(await screen.findByText(/Ada Lovelace/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit for review" })).toBeDisabled();
  });

  it("enables submit once the self owner is verified", async () => {
    const client = makeStubClient(
      stubRoutes({
        profile: profile({
          missing: [],
          persons: [person({ is_you: true, status: "verified" })],
        }),
      }),
    );
    renderWithProviders(<VerifyBusinessPage />, client);

    const submit = await screen.findByRole("button", { name: "Submit for review" });
    expect(submit).toBeEnabled();

    await userEvent.click(submit);
    await waitFor(() =>
      expect(
        client.calls.some((call) => call.path === SUBMIT_PATH && call.init.method === "POST"),
      ).toBe(true),
    );
  });

  it("shows the reverify button for a stale verified self and attempts the verify endpoint", async () => {
    const client = makeStubClient(
      stubRoutes({
        profile: profile({
          status: "reverification_due",
          missing: [],
          persons: [person({ is_you: true, status: "verified" })],
        }),
      }),
    );
    renderWithProviders(<VerifyBusinessPage />, client);

    const button = await screen.findByRole("button", { name: "Verify my ID" });
    await userEvent.click(button);

    // The stub returns an Error for /verify, so the mutation surfaces an alert instead of
    // navigating; the POST itself is what we assert.
    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/kyc/persons/person-1/verify" &&
            call.init.method === "POST",
        ),
      ).toBe(true),
    );
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("keeps the business original fields and document upload on the business path", async () => {
    const client = makeStubClient(
      stubRoutes({
        profile: profile({
          account_type: "business",
          business: emptyBusiness({ country: "US", legal_name: "Acme Inc" }),
          persons: [person({ is_you: true, status: "verified" })],
        }),
      }),
    );
    renderWithProviders(<VerifyBusinessPage />, client);

    expect(await screen.findByLabelText("Legal business name")).toBeInTheDocument();
    expect(screen.getByLabelText("Business type")).toBeInTheDocument();
    expect(screen.getByLabelText("Registration number")).toBeInTheDocument();
    expect(screen.getByLabelText("Tax ID")).toBeInTheDocument();
    expect(screen.getByLabelText("Website")).toBeInTheDocument();
    expect(screen.getByLabelText("Document file")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload" })).toBeInTheDocument();
  });
});
