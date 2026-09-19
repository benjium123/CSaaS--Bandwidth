import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Me } from "@/auth/AuthContext";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { base64urlToBuffer, bufferToBase64url, toRequestOptions } from "@/lib/webauthn";
import { VerificationBanner } from "@/components/kyc/VerificationBanner";
import { StepUpDialog } from "@/components/security/StepUpDialog";
import { VerifyBusinessPage } from "@/pages/VerifyBusinessPage";
import { OpsPage } from "@/pages/OpsPage";
import { LoginPage } from "@/pages/LoginPage";
import { OrgPickerPage } from "@/pages/OrgPickerPage";
import type { KycProfile } from "@/api/kyc";

const ME: Me = {
  id: "u1",
  email: "owner@acme.test",
  full_name: "Owner",
  memberships: [{ org_id: "org-1", org_name: "Acme", org_slug: "acme", role_name: "owner" }],
  // Top-level, exactly where /auth/me puts it (MembershipOut carries no permissions).
  // This is the workspace OWNER taking their own business through verification: reading
  // the workspace (VerificationBanner only fetches /kyc/profile with `org:read`) and
  // editing/submitting the application (VerifyBusinessPage's `editable` needs
  // `org:update`). Deliberately NOT the owner's full 34-item list - nothing in this file
  // exercises calls, contacts or members, and a blanket list would hide a gate regression.
  permissions: ["org:read", "org:update"],
  totp_enabled: true,
  has_passkey: false,
  is_platform_operator: false,
};

function profile(overrides: Partial<KycProfile> = {}): KycProfile {
  return {
    status: "draft",
    business: {
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
    },
    use_case: null,
    use_case_pending: null,
    persons: [],
    documents: [],
    checks: {},
    agreement: { current_version: "2026-09-16", accepted_version: null, accepted_at: null },
    missing: ["legal_name", "owner", "documents", "agreement", "use_case.description"],
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

describe("webauthn helpers", () => {
  it("round-trips base64url and converts request options", () => {
    const bytes = new Uint8Array([0, 250, 255, 1, 62, 63]);
    const encoded = bufferToBase64url(bytes.buffer);
    expect(encoded).not.toMatch(/[+/=]/);
    expect(new Uint8Array(base64urlToBuffer(encoded))).toEqual(bytes);

    const opts = toRequestOptions({
      challenge: encoded,
      allowCredentials: [{ id: encoded, type: "public-key" }],
    });
    expect(opts.challenge).toBeInstanceOf(ArrayBuffer);
    expect((opts.allowCredentials ?? [])[0].id).toBeInstanceOf(ArrayBuffer);
  });
});

describe("VerificationBanner", () => {
  it("tells a draft workspace to get verified", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/kyc/profile": profile(),
    });
    renderWithProviders(<VerificationBanner />, client);
    expect(await screen.findByText(/Verify your business to start calling/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Get verified" })).toBeInTheDocument();
  });

  it("is hidden once approved", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/kyc/profile": profile({ status: "approved", missing: [] }),
    });
    renderWithProviders(<VerificationBanner />, client);
    await waitFor(() => expect(client.calls.some((c) => c.path === "/api/v1/kyc/profile")).toBe(true));
    expect(screen.queryByRole("status", { name: "Business verification" })).toBeNull();
  });

  it("shows suspension without an action button", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/kyc/profile": profile({ status: "suspended", missing: [] }),
    });
    renderWithProviders(<VerificationBanner />, client);
    expect(await screen.findByText("This account is suspended")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("VerifyBusinessPage", () => {
  it("only offers the countries the platform verifies", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/kyc/profile": profile({ supported_countries: ["US", "GB"] }),
    });
    renderWithProviders(<VerifyBusinessPage />, client);
    const select = await screen.findByRole("combobox", { name: "Country of registration" });
    const options = Array.from(select.querySelectorAll("option")).map((o) => o.textContent);
    expect(options).toContain("United States");
    expect(options).toContain("United Kingdom");
    expect(options).not.toContain("Canada");
  });

  it("lists what is missing and keeps submit disabled", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/kyc/profile": profile(),
    });
    renderWithProviders(<VerifyBusinessPage />, client);
    expect(await screen.findByText("Still needed before you can submit:")).toBeInTheDocument();
    expect(screen.getByText("At least one business document")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit for review" })).toBeDisabled();
  });

  it("shows the reviewer's request when more information is needed", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/kyc/profile": profile({ status: "needs_info", info_request: "Upload your EIN letter" }),
    });
    renderWithProviders(<VerifyBusinessPage />, client);
    expect(await screen.findByText("Upload your EIN letter")).toBeInTheDocument();
  });

  it("submits a complete application", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/kyc/profile": profile({ missing: [] }),
      "/api/v1/kyc/submit": profile({ status: "submitted", missing: [] }),
    });
    renderWithProviders(<VerifyBusinessPage />, client);
    const button = await screen.findByRole("button", { name: "Submit for review" });
    await userEvent.click(button);
    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/kyc/submit" && c.init.method === "POST")).toBe(true),
    );
  });
});

describe("StepUpDialog", () => {
  it("opens on step_up_required and starts a selfie check", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/kyc/step-up": { id: "s1", url: "https://verify.stripe.test/s1", status: "pending" },
    });
    renderWithProviders(<StepUpDialog />, client);
    await waitFor(() => expect(client.onStepUpRequired).toBeTypeOf("function"));
    client.onStepUpRequired!({ kind: "recent_selfie", action: "api_key_create", message: "" });
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(/create an API key/)).toBeInTheDocument();
  });

  it("claims nothing about someone's second factors until they are known", async () => {
    // Audit finding 12: `!me?.totp_enabled` is TRUE while me is still loading, so the
    // "you have no way to confirm" copy - and a button that discards the pending step-up -
    // used to be shown to people who own a passkey.
    const client = makeStubClient({ "/api/v1/auth/me": new Promise(() => {}) as never });
    renderWithProviders(<StepUpDialog />, client);
    await waitFor(() => expect(client.onStepUpRequired).toBeTypeOf("function"));
    client.onStepUpRequired!({ kind: "recent_2fa", action: "suspend", message: "" });
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText(/don't have a way to confirm/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Set up a second factor" })).toBeNull();
  });

  it("offers a way out when neither second factor can be used here", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": { ...ME, totp_enabled: false, has_passkey: true },
    });
    renderWithProviders(<StepUpDialog />, client);
    await waitFor(() => expect(client.onStepUpRequired).toBeTypeOf("function"));
    client.onStepUpRequired!({ kind: "recent_2fa", action: "suspend", message: "" });
    // jsdom has no WebAuthn, so passkeysSupported() is false: the dead-end case.
    expect(await screen.findByText(/can't use passkeys/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set up a second factor" })).toBeInTheDocument();
  });

  it("asks for an authenticator code for recent_2fa", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/auth/2fa/step-up": { ok: true },
    });
    renderWithProviders(<StepUpDialog />, client);
    await waitFor(() => expect(client.onStepUpRequired).toBeTypeOf("function"));
    client.onStepUpRequired!({ kind: "recent_2fa", action: "suspend", message: "" });
    const input = await screen.findByLabelText("Authenticator code");
    await userEvent.type(input, "123456");
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/you're confirmed/)).toBeInTheDocument();
  });
});

describe("OpsPage", () => {
  it("refuses non-operators", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": ME });
    renderWithProviders(<OpsPage />, client);
    expect(await screen.findByText(/for platform operators only/)).toBeInTheDocument();
  });

  // Same shape as the StepUpDialog null-state guard above, on a second surface: `me` is
  // null until /auth/me answers, so `!me?.is_platform_operator` was true during load and a
  // genuine operator was told the console was not for them. Holding /auth/me unresolved
  // forever is what makes this able to fail - the refusal must not be rendered from a value
  // that is merely unloaded.
  it("does not tell an operator the console is not for them while /auth/me is in flight", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": new Promise(() => {}) as never });
    renderWithProviders(<OpsPage />, client);
    expect(await screen.findByText(/Checking your access/)).toBeInTheDocument();
    expect(screen.queryByText(/for platform operators only/)).toBeNull();
  });

  it("shows the review queue to operators", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": { ...ME, is_platform_operator: true },
      "/api/v1/ops/queue": {
        open_security_alerts: 0,
        applications: [
          {
            org_id: "org-9",
            org_name: "Scammy",
            legal_name: "Debt Relief Now LLC",
            country: "US",
            status: "submitted",
            risk_tier: "high",
            risk_reasons: ["High-risk line of business: debt relief"],
            video_call_required: true,
            video_call_done: false,
            use_case_change_pending: false,
            submitted_at: null,
            ai_recommendation: "reject",
            ai_confidence: 91,
          },
        ],
      },
    });
    renderWithProviders(<OpsPage />, client);
    expect(await screen.findByText("Debt Relief Now LLC")).toBeInTheDocument();
    expect(screen.getByText(/video call needed/)).toBeInTheDocument();
    expect(screen.getByText("AI: reject 91%")).toBeInTheDocument();
  });
});

describe("OrgPickerPage", () => {
  it("links an operator with no workspace to the operator console", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": { ...ME, memberships: [], is_platform_operator: true },
    });
    renderWithProviders(<OrgPickerPage />, client);
    expect(await screen.findByRole("link", { name: "Open the operator console" })).toHaveAttribute("href", "/ops");
  });

  it("shows no operator link to customers", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": ME });
    renderWithProviders(<OrgPickerPage />, client);
    expect(await screen.findByText("Acme")).toBeInTheDocument(); // me has loaded
    expect(screen.queryByRole("link", { name: "Open the operator console" })).toBeNull();
  });
});

describe("LoginPage passkey sign-in", () => {
  it("offers the passkey when the account has one", async () => {
    const client = makeStubClient({
      "/api/v1/auth/login": {
        access_token: null,
        requires_2fa: true,
        pending_token: "pending",
        methods: ["passkey"],
      },
    });
    client.auth.token = null;
    renderWithProviders(<LoginPage />, client);
    await userEvent.type(screen.getByLabelText("Email"), "a@b.test");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("button", { name: "Use your passkey" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Authenticator code")).toBeNull();
  });
});

