import { describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ApiClient } from "@/api/client";
import { AuthProvider } from "@/auth/AuthContext";
import { SsoCallbackPage } from "@/pages/SsoCallbackPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import {
  SamlSsoCard,
  ScimTokensCard,
  VerifiedDomainsCard,
} from "./EnterpriseSsoCards";

const caps = (permissions: string[]) => ({
  permissions,
  org: {
    has_provider: false,
    has_number: false,
    member_count: 1,
    registration_state: "none",
  },
});

const ME = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [
    { org_id: "org-1", org_name: "Org", org_slug: "acme", role_name: "owner" },
  ],
};

const PENDING_DOMAIN = {
  id: "d1",
  domain: "acme.com",
  verified: false,
  verified_at: null,
  last_checked_at: null,
  txt_name: "_csaas-verify.acme.com",
  txt_value: "csaas-verify=abc",
};

describe("VerifiedDomainsCard", () => {
  it("renders nothing without settings:read", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": caps([]),
      "/api/v1/auth/me": ME,
    });
    renderWithProviders(<VerifiedDomainsCard />, client);
    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/me/capabilities"),
      ).toBe(true),
    );
    expect(screen.queryByText("Verified domains")).toBeNull();
  });

  it("shows the TXT record to publish and checks DNS", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": caps(["settings:read", "settings:write"]),
      "/api/v1/auth/me": ME,
      "/api/v1/orgs/current/domains": [PENDING_DOMAIN],
      "/api/v1/orgs/current/domains/d1/verify": {
        ...PENDING_DOMAIN,
        verified: true,
      },
    });
    renderWithProviders(<VerifiedDomainsCard />, client);

    expect(
      await screen.findByDisplayValue("_csaas-verify.acme.com"),
    ).toBeTruthy();
    expect(screen.getByDisplayValue("csaas-verify=abc")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Check DNS" }));
    await waitFor(() =>
      expect(
        client.calls.some(
          (c) =>
            c.path === "/api/v1/orgs/current/domains/d1/verify" &&
            c.init.method === "POST",
        ),
      ).toBe(true),
    );
  });
});

describe("SamlSsoCard", () => {
  it("shows the values for the identity provider and saves SAML settings", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": caps(["settings:read", "settings:write"]),
      "/api/v1/auth/me": ME,
      "/api/v1/orgs/current/security": (_p: string, init: RequestInit) =>
        init.method === "PATCH"
          ? {
              require_2fa: false,
              require_2fa_grace_until: null,
              ip_allowlist: null,
              sso: null,
            }
          : {
              require_2fa: false,
              require_2fa_grace_until: null,
              ip_allowlist: null,
              sso: null,
            },
      "/api/v1/orgs/current/sso/saml": {
        sp_entity_id: "https://app/api/v1/auth/saml/acme/metadata",
        acs_url: "https://app/api/v1/auth/saml/acme/acs",
        metadata_url: "https://app/api/v1/auth/saml/acme/metadata",
        start_url: "https://app/api/v1/auth/saml/acme/start",
      },
    });
    renderWithProviders(<SamlSsoCard />, client);

    expect(
      await screen.findByDisplayValue("https://app/api/v1/auth/saml/acme/acs"),
    ).toBeTruthy();
    await userEvent.type(
      screen.getByLabelText("Identity provider entity ID"),
      "https://idp/meta",
    );
    await userEvent.type(
      screen.getByLabelText("Identity provider sign-in URL"),
      "https://idp/sso",
    );
    await userEvent.type(screen.getByLabelText("Signing certificate"), "MIIC");
    await userEvent.type(
      screen.getByLabelText("SAML email domain"),
      "acme.com",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Save SAML settings" }),
    );

    await waitFor(() => {
      const patch = client.calls.find(
        (c) =>
          c.path === "/api/v1/orgs/current/security" &&
          c.init.method === "PATCH",
      );
      expect(patch?.init.json).toEqual({
        sso: {
          protocol: "saml",
          idp_entity_id: "https://idp/meta",
          idp_sso_url: "https://idp/sso",
          domain: "acme.com",
          idp_x509_cert: "MIIC",
        },
      });
    });
  });
});

describe("ScimTokensCard", () => {
  it("shows a new token once", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": caps(["members:update"]),
      "/api/v1/auth/me": ME,
      "/api/v1/orgs/current/scim-tokens": (_p: string, init: RequestInit) =>
        init.method === "POST"
          ? {
              id: "t1",
              name: "Okta",
              prefix: "abcd1234",
              created_at: "2026-09-17T00:00:00Z",
              last_used_at: null,
              revoked_at: null,
              token: "scim_abcd1234_secret",
              base_url: "https://app/scim/v2",
            }
          : [],
    });
    renderWithProviders(<ScimTokensCard />, client);

    await userEvent.type(
      await screen.findByLabelText("SCIM token name"),
      "Okta",
    );
    await userEvent.click(screen.getByRole("button", { name: "Create token" }));
    expect(
      await screen.findByDisplayValue("scim_abcd1234_secret"),
    ).toBeTruthy();
    expect(screen.getByDisplayValue("https://app/scim/v2")).toBeTruthy();
  });
});

function renderCallback(client: ApiClient, entry: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/auth/sso/callback" element={<SsoCallbackPage />} />
            <Route path="/inbox" element={<div>Inbox</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("SsoCallbackPage (SAML)", () => {
  it("finishes a SAML sign-in without a code exchange", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": ME });
    renderCallback(client, "/auth/sso/callback?saml=1&org_id=org-1");
    await screen.findByText("Inbox");
    expect(client.auth.orgId).toBe("org-1");
    expect(
      client.calls.some((c) => c.path.startsWith("/api/v1/auth/sso/callback")),
    ).toBe(false);
  });

  it("explains an unverified domain", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": ME });
    renderCallback(client, "/auth/sso/callback?error=sso_domain_unverified");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "has not verified its email domain",
    );
  });
});
