import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/api/client";
import type { SecurityPolicyOut } from "@/api/identity";
import { OrgSecurityPolicyCard } from "./OrgSecurityPolicyCard";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

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

const POLICY: SecurityPolicyOut = {
  require_2fa: false,
  require_2fa_grace_until: null,
  ip_allowlist: null,
  sso: null,
};

const POLICY_WITH_IP: SecurityPolicyOut = {
  ...POLICY,
  ip_allowlist: ["10.0.0.0/8"],
};

const POLICY_WITH_SSO: SecurityPolicyOut = {
  ...POLICY,
  sso: {
    issuer: "https://idp.example.com",
    client_id: "cid",
    domain: "acme.com",
    enforce: false,
    default_role_id: null,
    client_secret_set: true,
  },
};

function clientWithPolicy(
  permissions: string[],
  policy: SecurityPolicyOut = POLICY,
  securityRoute?: RouteStub,
) {
  return makeStubClient({
    "/api/v1/me/capabilities": caps(permissions),
    "/api/v1/auth/me": ME,
    "/api/v1/orgs/current/security": securityRoute ?? policy,
  });
}

describe("OrgSecurityPolicyCard", () => {
  it("renders nothing without settings:read", async () => {
    const client = clientWithPolicy([]);

    renderWithProviders(<OrgSecurityPolicyCard />, client);

    await waitFor(() => {
      expect(client.calls.some((call) => call.path === "/api/v1/me/capabilities")).toBe(true);
    });

    expect(screen.queryByText("Workspace security")).toBeNull();
    expect(client.calls.every((call) => !call.path.includes("/security"))).toBe(true);
  });

  it("explains that the IP allowlist also restricts API keys", async () => {
    const client = clientWithPolicy(["settings:read"]);

    renderWithProviders(<OrgSecurityPolicyCard />, client);

    await screen.findByText(
      (_, element) =>
        element?.textContent ===
        "Sign-ins and API keys are both restricted to these ranges. Leave this empty to allow every address.",
    );
  });

  it("a 422 two_factor_required_for_actor lands under the Require 2FA checkbox, not as a generic error", async () => {
    const client = clientWithPolicy(["settings:read", "settings:write"], POLICY, (_path, init) => {
      if (init.method === "PATCH") {
        throw new ApiError(422, "two_factor_required_for_actor", "server sentence");
      }
      return POLICY;
    });

    renderWithProviders(<OrgSecurityPolicyCard />, client);

    const checkbox = await screen.findByLabelText("Require two-factor authentication");
    await userEvent.click(checkbox);

    const alert = await screen.findByRole("alert");

    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(alert).toHaveTextContent(
      "Set up two-factor authentication on your own account before you can require it for everyone in this workspace.",
    );
    expect(screen.queryByText("server sentence")).toBeNull();
    expect(alert.previousElementSibling as HTMLElement).toContainElement(checkbox);
  });

  it("a 422 ip_allowlist_would_lock_you_out lands under the IP ranges box", async () => {
    const client = clientWithPolicy(["settings:read", "settings:write"], POLICY, (_path, init) => {
      if (init.method === "PATCH") {
        throw new ApiError(422, "ip_allowlist_would_lock_you_out", "server sentence");
      }
      return POLICY;
    });

    renderWithProviders(<OrgSecurityPolicyCard />, client);

    const textarea = await screen.findByLabelText("Allowed IP ranges");
    await userEvent.type(textarea, "10.0.0.0/8");
    await userEvent.click(screen.getByRole("button", { name: "Save IP ranges" }));

    const alert = await screen.findByRole("alert");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(alert).toHaveTextContent(
      "Your current IP address is outside the ranges you are saving. Add a range that includes it, or you will lock yourself out.",
    );
    expect(screen.queryByText("server sentence")).toBeNull();
  });

  it("clearing the box saves null rather than an empty list", async () => {
    const client = clientWithPolicy(
      ["settings:read", "settings:write"],
      POLICY_WITH_IP,
      (_path, init) => {
        if (init.method === "PATCH") return POLICY_WITH_IP;
        return POLICY_WITH_IP;
      },
    );

    renderWithProviders(<OrgSecurityPolicyCard />, client);

    const textarea = await screen.findByLabelText("Allowed IP ranges");
    await userEvent.clear(textarea);
    await userEvent.click(screen.getByRole("button", { name: "Save IP ranges" }));

    await waitFor(() => {
      const call = client.calls.find(
        (entry) =>
          entry.path === "/api/v1/orgs/current/security" && entry.init.method === "PATCH",
      );
      expect(call).toBeDefined();
    });

    const patchCall = client.calls.find(
      (entry) =>
        entry.path === "/api/v1/orgs/current/security" && entry.init.method === "PATCH",
    )!;
    expect(patchCall.init.json).toEqual({ ip_allowlist: null });
  });

  it("a blank client secret is omitted so a saved secret is not wiped", async () => {
    const client = clientWithPolicy(
      ["settings:read", "settings:write"],
      POLICY_WITH_SSO,
      (_path, init) => {
        if (init.method === "PATCH") return POLICY_WITH_SSO;
        return POLICY_WITH_SSO;
      },
    );

    renderWithProviders(<OrgSecurityPolicyCard />, client);

    const clientId = await screen.findByLabelText("Client ID");
    await userEvent.clear(clientId);
    await userEvent.type(clientId, "new-cid");
    await userEvent.click(screen.getByRole("button", { name: "Save single sign-on" }));

    await waitFor(() => {
      const call = client.calls.find(
        (entry) =>
          entry.path === "/api/v1/orgs/current/security" && entry.init.method === "PATCH",
      );
      expect(call).toBeDefined();
    });

    const patchCall = client.calls.find(
      (entry) =>
        entry.path === "/api/v1/orgs/current/security" && entry.init.method === "PATCH",
    )!;
    const body = patchCall.init.json as { sso: Record<string, unknown> };
    expect(Object.keys(body.sso)).not.toContain("client_secret");
  });

  it("read-only members cannot edit", async () => {
    const client = clientWithPolicy(["settings:read"]);

    renderWithProviders(<OrgSecurityPolicyCard />, client);

    const textarea = await screen.findByLabelText("Allowed IP ranges");
    expect(textarea).toBeDisabled();
    expect(
      screen.getByText(/You can view this, but only an admin can make changes here\./),
    ).toBeTruthy();
  });
});
